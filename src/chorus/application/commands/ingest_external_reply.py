"""Transaction A: one authenticated correlated reply becomes one immutable private artifact.

Seven participants, Core and Audit, run by the application worker composition of the inbound
entry point ([ADR-026](../../../../docs/adr/ADR-026-inbound-reply-trust-and-correlation.md) § 6):

1. the ``EvidenceRoot``, create-only, content-addressed over the raw MIME;
2. its create-only root-ID locator (ADR-017);
3. the ``EvidenceItem`` carrying the immutable ``ExternalSourceBinding``, create-only;
4. the guarded case update -- **state unchanged**, ``version + 1``, ``authorization_version + 1``,
   conditioned on the exact ``version`` and the exact state the loader read;
5. a ``ConditionCheck`` that no live send fence holds the case;
6. the ``reply.received`` audit event;
7. the completed ``INGEST_REPLY`` idempotency record, and this plan's commit proof.

Why both counters move
-----------------------
[ADR-020](../../../../docs/adr/ADR-020-case-authorization-version.md) § 2 row 13. Row 12 already
reasons that "the new evidence that justifies a reopen bumped the authorization epoch when it
landed", so evidence is authorization-sensitive by that table's own logic -- and staling an
in-flight compile is the conservative direction. Participant 5 exists for the same reason the
mandate-decision transaction has one: bumping the epoch underneath a live fence would stale a
send at the worst possible instant.

Why no ``Report`` and no ``Fact``
----------------------------------
``independent_sources()`` counts active, non-duplicate *reports*. A reply that creates none
cannot touch ``corroboration_source_count``, cannot corroborate a fact, and cannot move
readiness. That is asserted by test over the staged plan, not left to follow from the absence
of code.

Why the bytes are written first
--------------------------------
The raw MIME goes to the private evidence bucket **before** the transaction and confers no
authority until one commits -- exactly as a compile's sanitized derivative does
([ADR-018](../../../../docs/adr/ADR-018-safe-evidence-and-compile-commit.md)). It is
content-addressed, so writing it twice is the same write and an ambiguous PUT is repeatable
rather than duplicable. It is never in DynamoDB: a 256 KiB body against a 400 KiB item limit is
reason enough, and every projection that reads a case partition would then be one field away
from carrying a stranger's email.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from chorus.application import observability
from chorus.application.services.identity import derive_evidence_root_id
from chorus.application.services.inbound_mail import (
    AttestedInboundReply,
    InboundMailEvidenceVerifier,
    InboundMailTrustFailure,
    InboundMailUntrusted,
    InboundReplyRejected,
    InboundReplyRejection,
)
from chorus.application.services.mandate_terms import key_hash
from chorus.domain.entities import (
    ActorType,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    CaseState,
    CommunityCase,
    DerivationKind,
    EvidenceItem,
    EvidenceRoot,
    ExternalSourceBinding,
    ExtractionStatus,
    MalwareScanStatus,
)
from chorus.domain.ids import (
    ActionId,
    CaseId,
    CommunityId,
    EvidenceItemId,
    IdGenerator,
    Namespace,
    SensitiveStr,
    Sha256Digest,
)
from chorus.domain.state import bump_case_authorization
from chorus.ports.clock import Clock
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotencyStatus,
    IdempotentCommand,
)
from chorus.ports.objects import INBOUND_REPLY_MEDIA_TYPE, ObjectStorePort, inbound_reply_key
from chorus.ports.records import EvidenceRootLocator
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    IdempotencyRepositoryPort,
)
from chorus.ports.scopes import CaseScope, CommunityScope, NamespaceScope
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork

INGEST_TRANSACTION = "ingest-external-reply"

INGEST_PARTICIPANTS = 7
"""Root, root locator, evidence item, case bump, fence check, audit event, and commit proof."""

REPLY_RECEIVED_REASON_CODE = "REPLY_RECEIVED"
INBOUND_REPLY_EVIDENCE_TYPE = "EVIDENCE_ITEM"


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestExternalReplyCommand:
    """One attested delivery, plus the actor and correlation the entry point supplies.

    There is deliberately no case, action, destination, sender, subject, or body field. Every
    one of those is inside the attested artifact, established by the attester and checkable by
    the verifier -- which is the whole difference between this command and the caller-supplied
    body it replaces.
    """

    attested: AttestedInboundReply
    actor_id_hash: Sha256Digest
    correlation_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestExternalReplyResult:
    """What one ingestion produced, or what a replay found already there."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    evidence_id: EvidenceItemId
    case_version: int
    authorization_version: int
    replayed: bool


@dataclass(slots=True)
class IngestExternalReply:
    """Persist one attested reply, or replay the outcome an earlier delivery recorded."""

    core: CoreRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    objects: ObjectStorePort
    unit_of_work: UnitOfWork
    clock: Clock
    ids: IdGenerator
    evidence_trust: InboundMailEvidenceVerifier | None
    """The checking half of the boundary, or ``None`` for a deployment with none wired.

    ``None`` is the honest Phase-9 deployment on the AWS path: no authenticator exists, so no
    attester exists, so nothing can mint an artifact -- and a command holding no verifier
    refuses every delivery rather than accepting one on trust.
    """

    async def execute(self, command: IngestExternalReplyCommand) -> IngestExternalReplyResult:
        if self.evidence_trust is None or not self.evidence_trust.attests(command.attested):
            # A hand-built artifact, a replayed attestation from another deployment's ARN, and a
            # correlation that never crossed the attester are all the same thing here.
            raise InboundMailUntrusted(InboundMailTrustFailure.TRANSPORT_UNAVAILABLE)

        evidence = command.attested.evidence
        scope = CaseScope(
            namespace=evidence.namespace,
            community_id=evidence.community_id,
            case_id=evidence.case_id,
        )
        key = self._key(command)
        replayed = await self._replay(key, command)
        if replayed is not None:
            return replayed

        # Written before the transaction, conferring no authority until one commits. An object
        # already at the content address is this same delivery's bytes, so the conflict is the
        # success case rather than an error.
        with suppress(PersistenceConflictError):
            await self.objects.put_inbound_reply(
                namespace=evidence.namespace,
                community_id=evidence.community_id,
                case_id=evidence.case_id,
                raw_sha256=evidence.raw_sha256,
                content=command.attested.raw_mime,
            )

        now = self.clock.now()
        case = await self.core.load_case(scope)
        if case.state not in {CaseState.ACTIONED, CaseState.VERIFYING}:
            # The attester already checked this against the same row. Re-checking here is what
            # keeps a case that moved *between* attestation and persistence from being written
            # to at all -- and it reports the same closed code the attester would have, because
            # a caller cannot tell the two moments apart and should not have to.
            raise InboundReplyRejected(
                InboundReplyRejection.REPLY_CASE_TERMINAL
                if case.state in {CaseState.RESOLVED, CaseState.CLOSED_UNRESOLVED}
                else InboundReplyRejection.REPLY_CASE_NOT_ACTIONED
            )

        evidence_id = self.ids.new(EvidenceItemId)
        root = self._root(evidence.namespace, evidence.community_id, evidence.raw_sha256, now=now)
        item = self._item(command, evidence_id=evidence_id, root=root, now=now)
        bumped = bump_case_authorization(
            case,
            expected_version=case.version,
            reason_code=REPLY_RECEIVED_REASON_CODE,
            now=now,
        )

        community_scope = CommunityScope(
            namespace=evidence.namespace, community_id=evidence.community_id
        )
        operations = (
            self.core.stage_create_evidence_root(community_scope, root),
            self.core.stage_create_evidence_root_locator(
                community_scope,
                EvidenceRootLocator(
                    namespace=root.namespace,
                    community_id=root.community_id,
                    root_id=root.root_id,
                    root_sha256=root.root_sha256,
                    created_at=now,
                ),
            ),
            self.core.stage_create_evidence_item(scope, item),
            self.core.stage_update_case(
                scope,
                bumped,
                expected_version=case.version,
                expected_authorization_version=case.authorization_version,
                expected_state=case.state,
            ),
            self.core.stage_require_no_live_send_fence(scope, now=now),
            self.audit.stage_append_case_event(
                scope, self._audit_event(command, case=bumped, evidence_id=evidence_id, now=now)
            ),
            self.idempotency.stage_create_completed(
                key,
                request_hash=evidence.raw_sha256,
                result_entity_refs=(
                    EntityRef(
                        entity_type=INBOUND_REPLY_EVIDENCE_TYPE,
                        entity_id=evidence_id.value,
                        version=item.version,
                    ),
                    EntityRef(
                        entity_type="COMMUNITY_CASE",
                        entity_id=evidence.case_id.value,
                        version=bumped.version,
                    ),
                ),
                response_status=202,
                now=now,
            ),
        )
        if len(operations) != INGEST_PARTICIPANTS:  # pragma: no cover - arithmetic guard
            raise PersistenceConflictError("EVIDENCE_ITEM")
        await self.unit_of_work.commit(
            TransactionPlan(
                name=INGEST_TRANSACTION,
                operations=operations,
                audit_required=True,
                commit_proof=self.idempotency.commit_proof(key, request_hash=evidence.raw_sha256),
            )
        )
        observability.reply_received(
            namespace=evidence.namespace,
            community_id=evidence.community_id,
            case_id=evidence.case_id,
            correlation_id=command.correlation_id,
            evidence_id=evidence_id.value,
            execution_id=evidence.execution_id.value,
            inbound_message_id_hash=evidence.inbound_message_id_hash,
        )
        return IngestExternalReplyResult(
            namespace=evidence.namespace,
            community_id=evidence.community_id,
            case_id=evidence.case_id,
            action_id=evidence.action_id,
            evidence_id=evidence_id,
            case_version=bumped.version,
            authorization_version=bumped.authorization_version,
            replayed=False,
        )

    # -- replay ---------------------------------------------------------------------------

    def _key(self, command: IngestExternalReplyCommand) -> IdempotencyKey:
        """Keyed on the digest of the inbound ``Message-ID``, in the ``CASE`` partition.

        The message identifier is the one value a duplicate delivery of one message shares and
        two different messages never do, so it is what makes at-least-once transport safe here.
        It is hashed before it becomes a key segment, because provider text never enters a key.
        """

        evidence = command.attested.evidence
        return IdempotencyKey(
            partition=IdempotencyPartition(
                kind=IdempotencyPartitionKind.CASE,
                namespace=evidence.namespace,
                case_id=evidence.case_id,
            ),
            command=IdempotentCommand.INGEST_REPLY,
            actor_id_hash=command.actor_id_hash,
            key_hash=key_hash(f"ingest-reply\x1f{evidence.inbound_message_id_hash.value}"),
        )

    async def _replay(
        self, key: IdempotencyKey, command: IngestExternalReplyCommand
    ) -> IngestExternalReplyResult | None:
        """Answer from the recorded outcome, or ``None`` when this delivery is the first.

        The recorded ``request_hash`` is the raw MIME digest, so a *different* message arriving
        under a colliding key is a conflict rather than a replay -- and a redelivery of the same
        bytes reads its own answer without a second object write, a second root, a second audit
        event, or a second model call downstream.
        """

        record = await self.idempotency.load(key)
        if record is None or record.status is not IdempotencyStatus.COMPLETED:
            return None
        evidence = command.attested.evidence
        if record.request_hash != evidence.raw_sha256:
            raise PersistenceConflictError("EVIDENCE_ITEM")
        evidence_ref = next(
            (
                ref
                for ref in record.result_entity_refs
                if ref.entity_type == INBOUND_REPLY_EVIDENCE_TYPE
            ),
            None,
        )
        case_ref = next(
            (ref for ref in record.result_entity_refs if ref.entity_type == "COMMUNITY_CASE"),
            None,
        )
        if evidence_ref is None or case_ref is None:  # pragma: no cover - written together
            raise PersistenceConflictError("EVIDENCE_ITEM")
        case = await self.core.load_case(
            CaseScope(
                namespace=evidence.namespace,
                community_id=evidence.community_id,
                case_id=evidence.case_id,
            )
        )
        return IngestExternalReplyResult(
            namespace=evidence.namespace,
            community_id=evidence.community_id,
            case_id=evidence.case_id,
            action_id=evidence.action_id,
            evidence_id=EvidenceItemId(evidence_ref.entity_id),
            case_version=case.version,
            authorization_version=case.authorization_version,
            replayed=True,
        )

    # -- entities -------------------------------------------------------------------------

    def _root(
        self,
        namespace: Namespace,
        community_id: CommunityId,
        raw_sha256: Sha256Digest,
        *,
        now: datetime,
    ) -> EvidenceRoot:
        """The content-addressed origin of this reply's bytes, ``ORIGINAL`` and parentless.

        A byte-identical redelivery collapses to the same root by the mechanism that already
        exists -- the content address and the derived identifier -- rather than by a rule this
        command adds.
        """

        return EvidenceRoot(
            root_id=derive_evidence_root_id(
                namespace=namespace, community_id=community_id, root_sha256=raw_sha256
            ),
            community_id=community_id,
            namespace=namespace,
            root_sha256=raw_sha256,
            media_type=INBOUND_REPLY_MEDIA_TYPE,
            first_observed_at=now,
            derivation_kind=DerivationKind.ORIGINAL,
            parent_root_id=None,
            created_at=now,
            updated_at=now,
        )

    def _item(
        self,
        command: IngestExternalReplyCommand,
        *,
        evidence_id: EvidenceItemId,
        root: EvidenceRoot,
        now: datetime,
    ) -> EvidenceItem:
        """The immutable artifact: a binding and no resident owner.

        ``malware_scan_status = CLEAN`` is justified by the ``virusVerdict`` gate the attester
        already applied and by the refusal of every non-text part; ``extraction_status =
        COMPLETE`` because the text is extracted deterministically before anything is written.

        ``extracted_text`` is ``None`` when the quote-stripped text is empty -- a reply
        consisting only of our own quoted message leaves nothing, and an artifact with no text
        is one no span can index into, which is exactly the outcome T37 wants.
        """

        evidence = command.attested.evidence
        return EvidenceItem(
            evidence_id=evidence_id,
            root_id=root.root_id,
            community_id=evidence.community_id,
            case_id=evidence.case_id,
            namespace=evidence.namespace,
            submitted_by_contributor_id=None,
            source_message_id=None,
            private_object_key=SensitiveStr(
                inbound_reply_key(
                    namespace=evidence.namespace,
                    community_id=evidence.community_id,
                    case_id=evidence.case_id,
                    raw_sha256=evidence.raw_sha256,
                )
            ),
            media_type=INBOUND_REPLY_MEDIA_TYPE,
            byte_length=evidence.raw_byte_length,
            sha256=evidence.raw_sha256,
            captured_at=evidence.received_at,
            uploaded_at=now,
            derived_from_evidence_id=None,
            malware_scan_status=MalwareScanStatus.CLEAN,
            extraction_status=ExtractionStatus.COMPLETE,
            extracted_text=(
                SensitiveStr(evidence.extracted_text) if evidence.extracted_text else None
            ),
            version=1,
            created_at=now,
            updated_at=now,
            external_source_binding=ExternalSourceBinding(
                destination_id=evidence.destination_id,
                registry_version=evidence.registry_version,
                routing_token=evidence.routing_token,
                correlated_action_id=evidence.action_id,
                correlated_execution_id=evidence.execution_id,
                inbound_message_id_hash=evidence.inbound_message_id_hash,
                sender_address_digest=evidence.sender_address_digest,
                recipient_address_digest=evidence.recipient_address_digest,
                transport=evidence.transport,
                transport_source_arn=command.attested.source_arn,
                spf=evidence.verdicts.spf,
                dkim=evidence.verdicts.dkim,
                dmarc=evidence.verdicts.dmarc,
                spam=evidence.verdicts.spam,
                virus=evidence.verdicts.virus,
                received_at=evidence.received_at,
                correlation_proof=command.attested.attestation,
            ),
        )

    def _audit_event(
        self,
        command: IngestExternalReplyCommand,
        *,
        case: CommunityCase,
        evidence_id: EvidenceItemId,
        now: datetime,
    ) -> AuditEvent:
        evidence = command.attested.evidence
        return AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=evidence.namespace,
            community_id=evidence.community_id,
            case_id=evidence.case_id,
            # ``AWS_SERVICE``: the authority that made this artifact possible is the transport
            # that authenticated it, not a person and not an agent.
            actor_type=ActorType.AWS_SERVICE,
            actor_id_hash=command.actor_id_hash,
            event_type=observability.EventName.REPLY_RECEIVED,
            occurred_at=now,
            correlation_id=command.correlation_id,
            causation_id=None,
            idempotency_key_hash=evidence.inbound_message_id_hash,
            entity_refs=(
                AuditEntityRef(
                    entity_type=INBOUND_REPLY_EVIDENCE_TYPE,
                    entity_id=evidence_id.value,
                    version=1,
                ),
                AuditEntityRef(
                    entity_type="COMMUNITY_CASE",
                    entity_id=evidence.case_id.value,
                    version=case.version,
                ),
                AuditEntityRef(
                    entity_type="ACTION_EXECUTION",
                    entity_id=evidence.execution_id.value,
                    version=None,
                ),
            ),
            decision=AuditDecision.ALLOW,
            reason_codes=(REPLY_RECEIVED_REASON_CODE,),
            safe_details=AuditDetails(count=None, rule_id=None),
            input_hash=evidence.raw_sha256,
            output_hash=None,
        )


@dataclass(slots=True)
class RecordReplyRejection:
    """Write the one ``reply.rejected`` audit row a refused delivery earns, and nothing else.

    In the **namespace** partition, because a delivery refused at the transport has no case to
    name and attributing a stranger's probe to a real case would be worse than recording it
    unattached. Nothing about the case changes, no evidence is written, and the row carries a
    closed reason code and no content -- not the sender, not the recipient, not the subject, not
    the body, not any part of the raw MIME.
    """

    audit: AuditRepositoryPort
    unit_of_work: UnitOfWork
    clock: Clock
    ids: IdGenerator

    async def execute(
        self,
        *,
        namespace: Namespace,
        actor_id_hash: Sha256Digest,
        correlation_id: UUID,
        reason_code: str,
    ) -> None:
        now = self.clock.now()
        scope = NamespaceScope(namespace=namespace)
        event = AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=namespace,
            community_id=None,
            case_id=None,
            actor_type=ActorType.AWS_SERVICE,
            actor_id_hash=actor_id_hash,
            event_type=observability.EventName.REPLY_REJECTED,
            occurred_at=now,
            correlation_id=correlation_id,
            causation_id=None,
            idempotency_key_hash=None,
            entity_refs=(),
            decision=AuditDecision.DENY,
            reason_codes=(reason_code,),
            safe_details=AuditDetails(count=None, rule_id=None),
            input_hash=None,
            output_hash=None,
        )
        await self.unit_of_work.commit(
            TransactionPlan(
                name="record-reply-rejection",
                operations=(self.audit.stage_append_namespace_event(scope, event),),
                audit_required=True,
            )
        )
        observability.reply_rejected(
            namespace=namespace,
            community_id=None,
            case_id=None,
            correlation_id=correlation_id,
            reason_codes=(reason_code,),
        )


__all__ = [
    "INGEST_PARTICIPANTS",
    "INGEST_TRANSACTION",
    "REPLY_RECEIVED_REASON_CODE",
    "IngestExternalReply",
    "IngestExternalReplyCommand",
    "IngestExternalReplyResult",
    "RecordReplyRejection",
]
