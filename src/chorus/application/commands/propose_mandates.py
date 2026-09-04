"""Candidate acceptance: the command that asks every affected contributor for a mandate.

A discovered case is a claim that several people's reports describe one problem. Nobody has yet
been asked whether any of it may leave the building, and until they are asked there is nothing
for them to answer. This command is that asking, and it is also the human acceptance of the
candidate -- one action, one transaction, for the reason set out in
[ADR-013](../../../../docs/adr/ADR-013-mandate-proposal-endpoint.md): the frozen guard requires
proposals to exist *at the instant* the case leaves ``CANDIDATE``, and splitting the two would
create a state the machine does not describe.

What it proposes is never a negotiation and never a model's opinion. Each grant carries
:func:`~chorus.privacy.policy.proposed_scope` for that fact's type and sensitivity -- the
least-permissive useful default -- which is a *different value* from
:func:`~chorus.privacy.policy.policy_maximum_scope`, the ceiling no later decision may exceed. A
general incident fact is offered ``ANONYMOUS_CASE`` under an ``EXTERNAL_ACTION`` ceiling, and a
photo description is offered ``INTERNAL_ONLY`` under the same ceiling. Reaching a ceiling is an
``ADJUST`` its owner makes deliberately, never something approving a proposal quietly does.

No agent contributes to any of it. The Monitor has no field in which to name a scope, a purpose,
or a set of facts that may travel: ADR-014 removed the one it had rather than leave a scope
sitting in a schema the model is handed.

Every fact the contributor owns appears, including the ones policy keeps internal. The mandate
thread is where a person sees what was collected about them, and omitting the health detail and
the apartment number would hide exactly the two entries they most need to see are locked.
"""

from __future__ import annotations

from collections.abc import Iterable
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
    key_hash,
    proposal_request_hash,
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
from chorus.domain.errors import StateTransitionError, ValidationError
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
from chorus.domain.mandates import CurrentMandatePointer, DisclosureMandate
from chorus.domain.state import CaseTransitionContext, transition_case
from chorus.domain.time import Clock
from chorus.ports.errors import IdempotencyConflictError, PersistenceConflictError
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotencyRecord,
    IdempotencyStatus,
    IdempotentCommand,
)
from chorus.ports.records import StoredCurrentMandatePointer
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    IdempotencyRepositoryPort,
)
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import WriteOperation
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork
from chorus.privacy.mandates import (
    PROPOSAL_IDENTITY_GRANT,
    build_proposed_grants,
    grantable_facts,
    validate_destinations_and_purposes,
)
from chorus.privacy.policy import ALLOWED_PURPOSES, MandateDenialCode

CANDIDATE_ACCEPTED_REASON_CODE = "CANDIDATE_ACCEPTED"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposeMandatesCommand:
    """Accept one candidate case and propose a mandate to every participating owner."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    expected_case_version: int
    actor_id_hash: Sha256Digest
    idempotency_key: str
    destination_id: DestinationId
    correlation_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposedMandate:
    """One durable proposal, described without any of the facts it is about."""

    mandate_id: MandateId
    version: int
    contributor_id: ContributorId
    status: MandateStatus
    terms_hash: Sha256Digest
    fact_grant_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposeMandatesResult:
    """What the accepted case looks like afterwards."""

    case_id: CaseId
    case_version: int
    state: CaseState
    proposals: tuple[ProposedMandate, ...]
    replayed: bool


@dataclass(slots=True)
class ProposeMandates:
    """Turn a discovered candidate into an answerable question for each contributor."""

    core: CoreRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    clock: Clock
    ids: IdGenerator

    async def execute(self, command: ProposeMandatesCommand) -> ProposeMandatesResult:
        _validate_key(command.idempotency_key)
        scope = CaseScope(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
        )
        key = self._key(command)
        request_hash = proposal_request_hash(
            case_id=command.case_id, expected_case_version=command.expected_case_version
        )

        replay = await self._replay(key, request_hash, scope)
        if replay is not None:
            return replay

        now = self.clock.now()
        case = await self.core.load_case(scope)
        if case.version != command.expected_case_version:
            raise StaleAuthorizationError(("STALE_CASE_VERSION",))
        if case.state is not CaseState.CANDIDATE:
            # The guard is CANDIDATE-only rather than "not yet accepted": a case that already
            # moved on has proposals, and re-proposing would mint a second version-1 pointer
            # for a mandate whose history has already started.
            raise StateTransitionError(str(command.case_id))

        facts = await self.core.load_facts(scope, case.fact_ids)
        proposals = self._build_proposals(command, case_facts=facts, now=now)
        if not proposals:
            # An AWAITING_MANDATES case nobody can decide is a dead end: no decision can ever
            # arrive, so it can never reach INVESTIGATING. Refusing leaves it a candidate,
            # which is at least a state a human can act on.
            raise PolicyDeniedError((MandateDenialCode.NO_GRANTABLE_FACT.value,))

        accepted = transition_case(
            case,
            CaseState.AWAITING_MANDATES,
            expected_version=command.expected_case_version,
            reason_code=CANDIDATE_ACCEPTED_REASON_CODE,
            now=now,
            context=CaseTransitionContext(
                actor_is_human=True,
                candidate_accepted=True,
                # True by construction: the same transaction writes a proposal for every
                # contributor owning an active fact, and the transition is refused above when
                # that set is empty. The flag is not a promise made elsewhere.
                mandate_proposals_for_all=True,
            ),
        )

        operations: list[WriteOperation] = []
        for mandate in proposals:
            operations.append(self.core.stage_append_mandate_version(scope, mandate))
            operations.append(
                self.core.stage_replace_current_mandate_pointer(
                    scope,
                    StoredCurrentMandatePointer(
                        namespace=command.namespace,
                        community_id=command.community_id,
                        pointer=CurrentMandatePointer(
                            mandate_id=mandate.mandate_id,
                            version=mandate.version,
                            case_id=mandate.case_id,
                            contributor_id=mandate.contributor_id,
                            terms_hash=mandate.terms_hash,
                        ),
                        status=mandate.status,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    ),
                    expected=None,
                )
            )
        operations.append(
            self.core.stage_update_case(
                scope, accepted, expected_version=command.expected_case_version
            )
        )
        # Asking for a mandate changes what the case's authorization state will be, so it takes
        # the same fence condition every authorization-sensitive mutation takes.
        operations.append(self.core.stage_require_no_live_send_fence(scope, now=now))
        operations.append(
            self.audit.stage_append_case_event(
                scope, self._audit_event(command, accepted, proposals, now=now)
            )
        )
        operations.append(
            self.idempotency.stage_create_completed(
                key,
                request_hash=request_hash,
                result_entity_refs=tuple(
                    EntityRef(
                        entity_type="DISCLOSURE_MANDATE",
                        entity_id=mandate.mandate_id.value,
                        version=mandate.version,
                    )
                    for mandate in proposals
                ),
                response_status=200,
                now=now,
            )
        )
        plan = TransactionPlan(
            name="propose-mandates",
            operations=tuple(operations),
            audit_required=True,
            commit_proof=self.idempotency.commit_proof(key, request_hash=request_hash),
        )
        try:
            await self.unit_of_work.commit(plan)
        except PersistenceConflictError:
            # A concurrent duplicate, a stale case version, or a fence that appeared between
            # the load and the commit. Classify rather than assume, so a caller is told which.
            resolved = await self._replay(key, request_hash, scope)
            if resolved is not None:
                return resolved
            fence = await self.core.load_send_fence(scope)
            if fence is not None and now < fence.expires_at:
                # Asking for a mandate changes the case's authorization state, so it loses the
                # same race a revocation loses -- and it is told the same retryable answer,
                # rather than a bare conflict a caller cannot act on.
                raise SendAuthorizationInProgressError(("SEND_FENCE_ACTIVE",)) from None
            raise

        observability.mandate_requested(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            case_version=accepted.version,
            correlation_id=command.correlation_id,
            actor_id_hash=command.actor_id_hash,
            proposal_count=len(proposals),
            fact_count=sum(len(mandate.fact_grants) for mandate in proposals),
        )
        return ProposeMandatesResult(
            case_id=command.case_id,
            case_version=accepted.version,
            state=accepted.state,
            proposals=_ordered(_describe(mandate) for mandate in proposals),
            replayed=False,
        )

    def _build_proposals(
        self,
        command: ProposeMandatesCommand,
        *,
        case_facts: tuple[Fact, ...],
        now: datetime,
    ) -> tuple[DisclosureMandate, ...]:
        """One sealed ``PROPOSED`` version per contributor owning an active fact."""

        owners = sorted(
            {
                fact.contributor_id
                for fact in case_facts
                if fact.case_id == command.case_id
                and fact.community_id == command.community_id
                and fact.namespace == command.namespace
            },
            key=str,
        )
        purposes = tuple(sorted(ALLOWED_PURPOSES, key=str))
        destinations = (command.destination_id,)
        denials = validate_destinations_and_purposes(
            destination_ids=destinations,
            purposes=purposes,
            allowed_destination_id=command.destination_id,
        )
        if denials:  # pragma: no cover - a misconfigured composition root, not a request
            raise PolicyDeniedError(tuple(code.value for code in denials))

        proposals: list[DisclosureMandate] = []
        for contributor_id in owners:
            owned = grantable_facts(
                case_facts,
                contributor_id=contributor_id,
                case_id=command.case_id,
                community_id=command.community_id,
                namespace=command.namespace,
            )
            if not owned:
                # Every fact this contributor owns is withdrawn. There is nothing to decide,
                # and a proposal with no grants would be a question with no subject.
                continue
            try:
                draft = DisclosureMandate(
                    mandate_id=self.ids.new(MandateId),
                    version=1,
                    case_id=command.case_id,
                    community_id=command.community_id,
                    contributor_id=contributor_id,
                    namespace=command.namespace,
                    status=MandateStatus.PROPOSED,
                    fact_grants=build_proposed_grants(owned),
                    identity_grant=PROPOSAL_IDENTITY_GRANT,
                    allowed_destination_ids=destinations,
                    allowed_purposes=purposes,
                    valid_from=now,
                    expires_at=None,
                    proposed_at=now,
                    decided_at=None,
                    revoked_at=None,
                    decision_actor_id=None,
                    supersedes_version=None,
                    terms_hash=PLACEHOLDER_TERMS_HASH,
                    created_at=now,
                    updated_at=now,
                )
            except ValueError as error:
                raise ValidationError("DISCLOSURE_MANDATE") from error
            proposals.append(seal(draft))
        return tuple(proposals)

    def _audit_event(
        self,
        command: ProposeMandatesCommand,
        accepted: CommunityCase,
        proposals: tuple[DisclosureMandate, ...],
        *,
        now: datetime,
    ) -> AuditEvent:
        """One safe append-only row: identifiers, versions, and a count."""

        return AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            actor_type=ActorType.HUMAN,
            actor_id_hash=command.actor_id_hash,
            event_type=observability.EventName.MANDATE_REQUESTED,
            occurred_at=now,
            correlation_id=command.correlation_id or self.ids.new_uuid(),
            causation_id=None,
            idempotency_key_hash=key_hash(command.idempotency_key),
            entity_refs=(
                AuditEntityRef(
                    entity_type="COMMUNITY_CASE",
                    entity_id=command.case_id.value,
                    version=accepted.version,
                ),
                *(
                    AuditEntityRef(
                        entity_type="DISCLOSURE_MANDATE",
                        entity_id=mandate.mandate_id.value,
                        version=mandate.version,
                    )
                    for mandate in proposals
                ),
            ),
            decision=AuditDecision.ALLOW,
            reason_codes=(CANDIDATE_ACCEPTED_REASON_CODE,),
            safe_details=AuditDetails(count=len(proposals), rule_id="mandate-proposal/v1"),
            input_hash=proposal_request_hash(
                case_id=command.case_id,
                expected_case_version=command.expected_case_version,
            ),
            output_hash=None,
        )

    async def _replay(
        self, key: IdempotencyKey, request_hash: Sha256Digest, scope: CaseScope
    ) -> ProposeMandatesResult | None:
        """Answer from the durable record, or classify a key bound to another request."""

        record = await self.idempotency.load(key)
        if record is None:
            return None
        if record.request_hash != request_hash:
            raise IdempotencyConflictError("DISCLOSURE_MANDATE")
        if record.status is not IdempotencyStatus.COMPLETED:
            return None
        return await self._result_from(record, scope)

    async def _result_from(
        self, record: IdempotencyRecord, scope: CaseScope
    ) -> ProposeMandatesResult:
        """Rebuild the recorded answer by reading the state the transaction committed."""

        case = await self.core.load_case(scope)
        proposals: list[ProposedMandate] = []
        for ref in record.result_entity_refs:
            if ref.entity_type != "DISCLOSURE_MANDATE" or ref.version is None:
                continue  # pragma: no cover - the record only ever holds mandate refs
            mandate = await self.core.load_mandate_version(
                scope, MandateId(ref.entity_id), ref.version
            )
            proposals.append(_describe(mandate))
        return ProposeMandatesResult(
            case_id=scope.case_id,
            case_version=case.version,
            state=case.state,
            proposals=_ordered(proposals),
            replayed=True,
        )

    @staticmethod
    def _key(command: ProposeMandatesCommand) -> IdempotencyKey:
        return IdempotencyKey(
            partition=IdempotencyPartition(
                kind=IdempotencyPartitionKind.CASE,
                namespace=command.namespace,
                case_id=command.case_id,
            ),
            command=IdempotentCommand.DECIDE_MANDATE,
            actor_id_hash=command.actor_id_hash,
            key_hash=key_hash(f"propose\x1f{command.idempotency_key}"),
        )


def _ordered(proposals: Iterable[ProposedMandate]) -> tuple[ProposedMandate, ...]:
    """Order proposals by owner, so a fresh answer and its replay are byte-identical.

    The fresh path builds them in owner order and the replay path reads them back from the
    idempotency record's references, which have their own order. Sorting both here is what
    makes "the same key returns the same result" true of the whole response rather than only
    of the durable state behind it.
    """

    return tuple(sorted(proposals, key=lambda item: str(item.contributor_id)))


def _describe(mandate: DisclosureMandate) -> ProposedMandate:
    return ProposedMandate(
        mandate_id=mandate.mandate_id,
        version=mandate.version,
        contributor_id=mandate.contributor_id,
        status=mandate.status,
        terms_hash=mandate.terms_hash,
        fact_grant_count=len(mandate.fact_grants),
    )


def _validate_key(value: str) -> None:
    if not 8 <= len(value) <= 128 or not value.isprintable() or not value.isascii():
        raise ValidationError("IDEMPOTENCY_KEY")
