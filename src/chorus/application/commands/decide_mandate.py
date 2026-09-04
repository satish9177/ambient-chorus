"""One immutable authorization decision, and the exact transaction that makes it durable.

A contributor answering a mandate is the single most consequential write in this system: it is
the only place a person says what may be said about them. Everything here follows from that.

**Nothing is edited.** A decision appends version N+1 and moves the current pointer to it. The
version it supersedes stays byte-for-byte as it was, so the history is an audit trail rather
than a field somebody overwrote.

**Ownership is proved, not assumed.** The case must be in the caller's namespace and community,
the mandate must belong to that case, the acting contributor must own the mandate, and every
granted fact must be an active fact of that case owned by that same contributor. A foreign or
absent identifier gets the same answer, because telling them apart is an oracle.

**Policy caps the answer.** A contributor may narrow whatever was proposed. Nothing they can
send widens it past the deterministic policy/v1 ceiling for each fact -- and identity is capped
separately, because permission to describe an incident is not permission to name the person who
lived it.

**The order against a send is explicit.** The transaction condition-checks that no unexpired
send fence holds the case. Either the decision commits and a later send finds its snapshot
stale, or the send holds the fence and the decision is refused for at most sixty seconds. Both
sides can never believe they won.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from chorus.application import observability
from chorus.application.errors import (
    PolicyDeniedError,
    SendAuthorizationInProgressError,
    StaleAuthorizationError,
)
from chorus.application.services.mandate_terms import (
    PLACEHOLDER_TERMS_HASH,
    decision_request_hash,
    key_hash,
    seal,
)
from chorus.domain.entities import (
    ActorType,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    CaseState,
    CommunityCase,
    MandateStatus,
)
from chorus.domain.errors import IntegrityError, ValidationError
from chorus.domain.facts import Fact
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    DestinationId,
    IdGenerator,
    MandateId,
    Namespace,
    Sha256Digest,
)
from chorus.domain.mandates import (
    CurrentMandatePointer,
    DisclosureMandate,
    FactGrant,
    IdentityGrant,
    MandateDecision,
    decide_mandate,
    mandate_is_expired,
    next_mandate_status,
    withdraws_authorization,
)
from chorus.domain.state import (
    MANDATE_MUTABLE_CASE_STATES,
    CaseTransitionContext,
    bump_case_authorization,
    transition_case,
)
from chorus.domain.time import Clock
from chorus.ports.errors import (
    IdempotencyConflictError,
    NotFoundError,
    PersistenceConflictError,
)
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotencyStatus,
    IdempotentCommand,
)
from chorus.ports.records import MandatePointerExpectation, StoredCurrentMandatePointer
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    IdempotencyRepositoryPort,
)
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import WriteOperation
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork
from chorus.privacy.mandates import validate_destinations_and_purposes, validate_requested_grants

MANDATE_DECIDED_REASON_CODE = "MANDATE_DECIDED"
READINESS_LOST_REASON_CODE = "MANDATE_AUTHORIZATION_WITHDRAWN"
FIRST_DECISION_REASON_CODE = "MANDATE_DECISION_RECORDED"


@dataclass(frozen=True, slots=True, kw_only=True)
class DecideMandateCommand:
    """One contributor's answer to one exact mandate version."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    mandate_id: MandateId
    actor_contributor_id: ContributorId
    actor_id_hash: Sha256Digest
    expected_version: int
    decision: MandateDecision
    fact_grants: tuple[FactGrant, ...]
    identity_grant: IdentityGrant
    expires_at: datetime | None
    idempotency_key: str
    destination_id: DestinationId
    """The destination policy/v1 currently allows, supplied by the composition root.

    Carried on the command rather than read off the stored mandate, because a check that
    compared a record against itself would pass for any record. The destination a mandate was
    proposed under has to still be the destination this deployment recognises: a registry that
    moved on is exactly the case where a carried-forward value must stop authorizing.
    """
    correlation_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DecideMandateResult:
    """The new immutable version and the case version it moved."""

    mandate_id: MandateId
    version: int
    status: MandateStatus
    terms_hash: Sha256Digest
    supersedes_version: int | None
    decided_at: datetime | None
    revoked_at: datetime | None
    case_version: int
    case_state: CaseState
    replayed: bool


@dataclass(slots=True)
class DecideMandate:
    """Record one contributor decision as an immutable version, atomically."""

    core: CoreRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    clock: Clock
    ids: IdGenerator

    async def execute(self, command: DecideMandateCommand) -> DecideMandateResult:
        _validate_key(command.idempotency_key)
        scope = CaseScope(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
        )
        key = self._key(command)
        request_hash = _request_hash(command)

        replay = await self._replay(key, request_hash, scope)
        if replay is not None:
            return replay

        now = self.clock.now()
        case = await self.core.load_case(scope)
        pointer = await self._owned_pointer(scope, command)
        current = await self.core.load_mandate_version(
            scope, command.mandate_id, pointer.pointer.version
        )
        self._verify_pointer_integrity(pointer, current)

        if pointer.pointer.version != command.expected_version:
            # The contributor decided against a version that is no longer current. Reloading
            # and applying their answer to the latest terms would be applying consent they
            # never read.
            raise StaleAuthorizationError(("STALE_MANDATE_VERSION",))
        if case.state not in MANDATE_MUTABLE_CASE_STATES:
            # RESOLVED and CLOSED_UNRESOLVED. Recording an authorization decision against a
            # case nothing may act on would bump a version no artifact is bound to and leave
            # a pointer describing consent that has no subject left.
            raise StaleAuthorizationError(("CASE_TERMINAL",))
        if mandate_is_expired(current, now):
            # Checked before the edge table, so an expired mandate answers MANDATE_EXPIRED
            # rather than the generic refusal every closed edge produces.
            raise PolicyDeniedError(("MANDATE_EXPIRED",))

        # The edge is settled before the terms are, and the order is deliberate. A decision on
        # a refused or revoked mandate is refused because the mandate is terminal, not because
        # its grants failed validation -- and a terminal version grants nothing, so checking
        # terms first would report a policy denial for a command that was never about policy.
        next_mandate_status(current.status, command.decision)

        proposal = await self._proposal(scope, current)
        await self._check_policy(
            command, scope=scope, proposal=proposal, current=current, case=case, now=now
        )
        next_version = self._build_version(command, current=current, now=now)
        reconciled = self._reconcile_case(command, case=case, current=current, now=now)

        operations = self._operations(
            command,
            scope=scope,
            pointer=pointer,
            next_version=next_version,
            reconciled=reconciled,
            key=key,
            request_hash=request_hash,
            now=now,
        )
        plan = TransactionPlan(
            name="decide-mandate",
            operations=operations,
            audit_required=True,
            commit_proof=self.idempotency.commit_proof(key, request_hash=request_hash),
        )
        try:
            await self.unit_of_work.commit(plan)
        except PersistenceConflictError:
            resolved = await self._classify_conflict(key, request_hash, scope, now=now)
            if resolved is not None:
                return resolved
            raise

        observability.mandate_decided(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            case_version=reconciled.version,
            correlation_id=command.correlation_id,
            actor_id_hash=command.actor_id_hash,
            decision=command.decision.value,
            mandate_version=next_version.version,
            granted_fact_count=len(next_version.fact_grants),
            identity_shared=next_version.identity_grant.externally_shareable,
        )
        return _result(next_version, reconciled, replayed=False)

    # -- authority ---------------------------------------------------------------------

    async def _owned_pointer(
        self, scope: CaseScope, command: DecideMandateCommand
    ) -> StoredCurrentMandatePointer:
        """Load the current pointer and prove this caller is entitled to decide it.

        Every failure here answers ``NOT_FOUND``. A mandate from another case, a mandate that
        does not exist, and a mandate belonging to a neighbour are three different mistakes and
        one answer, because separate answers would let a caller walk identifiers and learn
        which cases exist and who is in them.
        """

        try:
            pointer = await self.core.load_current_mandate_pointer(scope, command.mandate_id)
        except NotFoundError:
            raise NotFoundError("DISCLOSURE_MANDATE") from None
        if (
            pointer.pointer.case_id != command.case_id
            or pointer.pointer.contributor_id != command.actor_contributor_id
            or pointer.namespace != command.namespace
            or pointer.community_id != command.community_id
        ):
            raise NotFoundError("DISCLOSURE_MANDATE")
        return pointer

    @staticmethod
    def _verify_pointer_integrity(
        pointer: StoredCurrentMandatePointer, current: DisclosureMandate
    ) -> None:
        """Refuse when the pointer and the version it names disagree about anything.

        The pointer restates the mandate's identity, version, terms hash, contributor, and
        status. Two copies of a value are not a check -- they agree by construction until
        something rewrites one of them -- so the disagreement is the whole signal. A tampered
        ``terms_hash`` on either row, or a pointer aimed at a version belonging to somebody
        else, fails closed rather than authorizing a decision against terms nobody agreed to.
        """

        if (
            pointer.pointer.mandate_id != current.mandate_id
            or pointer.pointer.version != current.version
            or pointer.pointer.case_id != current.case_id
            or pointer.pointer.contributor_id != current.contributor_id
            or pointer.pointer.terms_hash != current.terms_hash
            or pointer.status is not current.status
            or pointer.namespace != current.namespace
            or pointer.community_id != current.community_id
        ):
            raise IntegrityError("CURRENT_MANDATE_POINTER")

    # -- terms -------------------------------------------------------------------------

    def _build_version(
        self, command: DecideMandateCommand, *, current: DisclosureMandate, now: datetime
    ) -> DisclosureMandate:
        """Build the sealed next version, after policy has already accepted its terms."""

        draft = decide_mandate(
            current,
            decision=command.decision,
            fact_grants=command.fact_grants,
            identity_grant=command.identity_grant,
            expires_at=command.expires_at,
            actor_id=command.actor_contributor_id,
            now=now,
            terms_hash=PLACEHOLDER_TERMS_HASH,
        )
        return seal(draft)

    async def _proposal(self, scope: CaseScope, current: DisclosureMandate) -> DisclosureMandate:
        """Load version 1, which is what decides *which facts* this mandate is about.

        Membership comes from the proposal rather than from the current version, and the
        difference is not academic. An adjustment supplies a complete replacement set, so a
        contributor who drops a fact leaves a current version that no longer mentions it --
        and reading membership from there would make their own fact permanently ungrantable,
        turning one narrow decision into an irreversible one.

        Scope is a separate question with a separate answer: the policy ceiling caps it, and
        the proposal's own scopes never do, or Resident B could not raise the photo they were
        offered at ``INTERNAL_ONLY`` to the ``EXTERNAL_ACTION`` the demo requires.
        """

        if current.version == 1:
            return current
        return await self.core.load_mandate_version(scope, current.mandate_id, 1)

    async def _check_policy(
        self,
        command: DecideMandateCommand,
        *,
        scope: CaseScope,
        proposal: DisclosureMandate,
        current: DisclosureMandate,
        case: CommunityCase,
        now: datetime,
    ) -> None:
        """Refuse terms policy/v1 does not permit, before any version is built.

        Facts are loaded from the *case*, not from the request. A request naming a fact is a
        claim; the case aggregate is what decides whether that claim is about something real,
        in this case, still active, and owned by the person deciding.
        """

        denials = list(
            validate_destinations_and_purposes(
                destination_ids=current.allowed_destination_ids,
                purposes=current.allowed_purposes,
                allowed_destination_id=command.destination_id,
            )
        )
        requested_ids = {grant.fact_id for grant in command.fact_grants}
        known: tuple[Fact, ...] = ()
        if requested_ids:
            in_case = tuple(fact_id for fact_id in case.fact_ids if fact_id in requested_ids)
            known = await self.core.load_facts(scope, in_case) if in_case else ()
        denials.extend(
            validate_requested_grants(
                fact_grants=command.fact_grants,
                identity_grant=command.identity_grant,
                expires_at=command.expires_at,
                proposed_fact_ids=frozenset(grant.fact_id for grant in proposal.fact_grants),
                facts_by_id={fact.fact_id: fact for fact in known},
                contributor_id=command.actor_contributor_id,
                case_id=command.case_id,
                community_id=command.community_id,
                namespace=command.namespace,
                now=now,
            )
        )
        if denials:
            codes = tuple(sorted({code.value for code in denials}))
            observability.mandate_denied(
                namespace=command.namespace,
                community_id=command.community_id,
                case_id=command.case_id,
                correlation_id=command.correlation_id,
                actor_id_hash=command.actor_id_hash,
                reason_codes=codes,
            )
            raise PolicyDeniedError(codes)

    # -- readiness ---------------------------------------------------------------------

    def _reconcile_case(
        self,
        command: DecideMandateCommand,
        *,
        case: CommunityCase,
        current: DisclosureMandate,
        now: datetime,
    ) -> CommunityCase:
        """Decide deterministically what this decision does to the case, and do exactly that.

        The system decides, not the contributor and not a model. Three outcomes, each from a
        guard the frozen state machine already states:

        * an ``AWAITING_MANDATES`` case that has just received its first non-proposed decision
          becomes ``INVESTIGATING``;
        * a case that was ready to act, or had a proposal out, loses that readiness when a
          contributor withdraws authorization, and returns to ``INVESTIGATING``;
        * everything else keeps its state and takes the version bump alone, because the frozen
          compiler contract requires every authorization-sensitive change to stale the views
          and proposals bound to the previous version.

        An approval never removes readiness. A case that was not ready does not become ready
        here either: readiness needs a validated assessment and a compile preflight, and
        neither is something a mandate decision can supply.
        """

        if case.state is CaseState.AWAITING_MANDATES:
            return transition_case(
                case,
                CaseState.INVESTIGATING,
                expected_version=case.version,
                reason_code=FIRST_DECISION_REASON_CODE,
                now=now,
                context=CaseTransitionContext(
                    any_mandate_decision=True,
                    reports_retained=bool(case.report_ids),
                ),
            )
        if withdraws_authorization(
            decision=command.decision,
            previous=current.fact_grants,
            requested=command.fact_grants,
        ) and case.state in {
            CaseState.READY_FOR_ACTION,
            CaseState.ACTION_PROPOSED,
        }:
            return transition_case(
                case,
                CaseState.INVESTIGATING,
                expected_version=case.version,
                reason_code=READINESS_LOST_REASON_CODE,
                now=now,
                context=CaseTransitionContext(readiness_lost=True),
            )
        return bump_case_authorization(
            case,
            expected_version=case.version,
            reason_code=MANDATE_DECIDED_REASON_CODE,
            now=now,
        )

    # -- persistence -------------------------------------------------------------------

    def _operations(
        self,
        command: DecideMandateCommand,
        *,
        scope: CaseScope,
        pointer: StoredCurrentMandatePointer,
        next_version: DisclosureMandate,
        reconciled: CommunityCase,
        key: IdempotencyKey,
        request_hash: Sha256Digest,
        now: datetime,
    ) -> tuple[WriteOperation, ...]:
        """The one frozen mandate-decision transaction, in the order the contract states it."""

        return (
            # 1. the immutable new version, create-only
            self.core.stage_append_mandate_version(scope, next_version),
            # 2. the current pointer, guarded on both the row version and the mandate version
            #    it is moving off, so two concurrent decisions cannot both advance it
            self.core.stage_replace_current_mandate_pointer(
                scope,
                StoredCurrentMandatePointer(
                    namespace=command.namespace,
                    community_id=command.community_id,
                    pointer=CurrentMandatePointer(
                        mandate_id=next_version.mandate_id,
                        version=next_version.version,
                        case_id=next_version.case_id,
                        contributor_id=next_version.contributor_id,
                        terms_hash=next_version.terms_hash,
                    ),
                    status=next_version.status,
                    version=pointer.version + 1,
                    created_at=pointer.created_at,
                    updated_at=now,
                ),
                expected=MandatePointerExpectation(
                    row_version=pointer.version,
                    mandate_version=pointer.pointer.version,
                ),
            ),
            # 3. the case version, guarded, so every view and proposal bound to the old one
            #    becomes stale in the same instant this authorization changed
            self.core.stage_update_case(scope, reconciled, expected_version=reconciled.version - 1),
            # 4. the send-fence condition: this is the total order against an outbound send
            self.core.stage_require_no_live_send_fence(scope, now=now),
            # 5. the safe audit row, append-only
            self.audit.stage_append_case_event(
                scope,
                self._audit_event(
                    command, next_version=next_version, reconciled=reconciled, now=now
                ),
            ),
            # 6. the idempotency record, which is also this plan's commit proof
            self.idempotency.stage_create_completed(
                key,
                request_hash=request_hash,
                result_entity_refs=(
                    EntityRef(
                        entity_type="DISCLOSURE_MANDATE",
                        entity_id=next_version.mandate_id.value,
                        version=next_version.version,
                    ),
                ),
                response_status=200,
                now=now,
            ),
        )

    def _audit_event(
        self,
        command: DecideMandateCommand,
        *,
        next_version: DisclosureMandate,
        reconciled: CommunityCase,
        now: datetime,
    ) -> AuditEvent:
        """Identifiers, versions, hashes, counts, and closed codes. Never a term.

        The decision word and the resulting status are enum values; the granted-fact count is a
        number. What is deliberately absent is the fact identifiers themselves, the scopes
        chosen, and the terms: the mandate version row already holds all of that in the private
        zone, and the audit table has a wider read audience and a different retention.
        """

        return AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            actor_type=ActorType.HUMAN,
            actor_id_hash=command.actor_id_hash,
            event_type=observability.EventName.MANDATE_DECIDED,
            occurred_at=now,
            correlation_id=command.correlation_id or self.ids.new_uuid(),
            causation_id=None,
            idempotency_key_hash=key_hash(command.idempotency_key),
            entity_refs=(
                AuditEntityRef(
                    entity_type="DISCLOSURE_MANDATE",
                    entity_id=next_version.mandate_id.value,
                    version=next_version.version,
                ),
                AuditEntityRef(
                    entity_type="COMMUNITY_CASE",
                    entity_id=command.case_id.value,
                    version=reconciled.version,
                ),
            ),
            decision=AuditDecision.ALLOW,
            reason_codes=tuple(dict.fromkeys((command.decision.value, next_version.status.value))),
            safe_details=AuditDetails(
                count=len(next_version.fact_grants), rule_id="mandate-decision/v1"
            ),
            input_hash=_request_hash(command),
            output_hash=next_version.terms_hash,
        )

    # -- replay ------------------------------------------------------------------------

    async def _replay(
        self, key: IdempotencyKey, request_hash: Sha256Digest, scope: CaseScope
    ) -> DecideMandateResult | None:
        """Answer from the durable record, or classify a key bound to another request."""

        record = await self.idempotency.load(key)
        if record is None:
            return None
        if record.request_hash != request_hash:
            raise IdempotencyConflictError("DISCLOSURE_MANDATE")
        if record.status is not IdempotencyStatus.COMPLETED:
            return None
        ref = next(
            (
                item
                for item in record.result_entity_refs
                if item.entity_type == "DISCLOSURE_MANDATE"
            ),
            None,
        )
        if ref is None or ref.version is None:  # pragma: no cover - the record always holds one
            raise IntegrityError("IDEMPOTENCY_RECORD")
        mandate = await self.core.load_mandate_version(scope, MandateId(ref.entity_id), ref.version)
        case = await self.core.load_case(scope)
        return _result(mandate, case, replayed=True)

    async def _classify_conflict(
        self,
        key: IdempotencyKey,
        request_hash: Sha256Digest,
        scope: CaseScope,
        *,
        now: datetime,
    ) -> DecideMandateResult | None:
        """Say which of three things the conditional failure actually was.

        A rejected transaction is one of: a concurrent duplicate of this very command, a live
        send fence, or a genuine stale-version conflict. They need different answers -- a
        replay, a retryable 409, and a non-retryable 409 -- so the classification reads state
        rather than guessing from the failure.

        The fence is checked second, and that ordering matters: a duplicate that lost the race
        to its own twin has a completed record and should be told it succeeded, even if a fence
        happened to appear afterwards.
        """

        resolved = await self._replay(key, request_hash, scope)
        if resolved is not None:
            return resolved
        fence = await self.core.load_send_fence(scope)
        if fence is not None and now < fence.expires_at:
            raise SendAuthorizationInProgressError(("SEND_FENCE_ACTIVE",))
        return None

    # -- helpers -----------------------------------------------------------------------

    @staticmethod
    def _key(command: DecideMandateCommand) -> IdempotencyKey:
        return IdempotencyKey(
            partition=IdempotencyPartition(
                kind=IdempotencyPartitionKind.CASE,
                namespace=command.namespace,
                case_id=command.case_id,
            ),
            command=IdempotentCommand.DECIDE_MANDATE,
            actor_id_hash=command.actor_id_hash,
            key_hash=key_hash(f"decide\x1f{command.idempotency_key}"),
        )


def _request_hash(command: DecideMandateCommand) -> Sha256Digest:
    return decision_request_hash(
        case_id=command.case_id,
        mandate_id=command.mandate_id,
        contributor_id=command.actor_contributor_id,
        expected_version=command.expected_version,
        decision=command.decision,
        fact_grants=command.fact_grants,
        identity_grant=command.identity_grant,
        expires_at=command.expires_at,
    )


def _result(
    mandate: DisclosureMandate, case: CommunityCase, *, replayed: bool
) -> DecideMandateResult:
    return DecideMandateResult(
        mandate_id=mandate.mandate_id,
        version=mandate.version,
        status=mandate.status,
        terms_hash=mandate.terms_hash,
        supersedes_version=mandate.supersedes_version,
        decided_at=mandate.decided_at,
        revoked_at=mandate.revoked_at,
        case_version=case.version,
        case_state=case.state,
        replayed=replayed,
    )


def _validate_key(value: str) -> None:
    if not 8 <= len(value) <= 128 or not value.isprintable() or not value.isascii():
        raise ValidationError("IDEMPOTENCY_KEY")
