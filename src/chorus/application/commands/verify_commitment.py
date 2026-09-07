"""Transaction F: the one path by which a promise is satisfied or missed, and a case resolved.

Five participants, Core / Shareable / Audit, run by the application
([ADR-027](../../../../docs/adr/ADR-027-commitment-extraction-grounding-and-authority.md) § 7):

1. the guarded commitment update ``DUE -> FULFILLED`` or ``DUE -> MISSED``, conditioned on the
   exact ``version`` and ``status == DUE``, recording ``verified_by_contributor_id``,
   ``verification_evidence_id?``, and ``outcome_note?``;
2. the guarded case update ``VERIFYING -> RESOLVED`` or ``VERIFYING -> READY_FOR_ACTION``,
   conditioned on the exact ``version``, ``authorization_version``, and ``state``, moving
   ``version`` only;
3. the current action pointer -- moved to ``INVALIDATED`` on ``MISSED``, conditioned on its exact
   row version and ``proposal_hash``; a ``ConditionCheck`` on the same row version on
   ``FULFILLED``. **The count does not move between the branches**, the ADR-025
   rejection/withdrawal precedent;
4. the ``commitment.fulfilled`` or ``commitment.missed`` audit event;
5. the completed ``VERIFY_COMMITMENT`` idempotency record, this plan's commit proof.

Participant 3 exists because a subsequent action needs a fresh view, proposal, and approval.
Leaving the pointer live would let a case return to ``READY_FOR_ACTION`` with a spent proposal
still current.

Who may do this
----------------
The **affected contributor**: a contributor owning at least one ``ACTIVE`` fact in the case,
checked deterministically against loaded case facts and **never** a claim in the request body.
It is the only source in the whole system that may satisfy a commitment, may mark one missed, or
may resolve a case -- not the extraction model, not a later reply, not new ambient evidence, and
not the passage of the deadline (ADR-027 § 8, T39).

There is no cancellation route. The two ``CANCELLED`` edges stay in ``COMMITMENT_EDGES`` --
removing a legal edge is a bigger change than not calling it -- and no command constructs one,
which a test asserts. A cancelled commitment would leave the case ``VERIFYING`` until a human
closed it, and V1 has no endpoint for that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from chorus.application import observability
from chorus.application.services.mandate_terms import key_hash
from chorus.domain.entities import (
    ActionProposalStatus,
    ActorType,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    CaseState,
    Commitment,
    CommitmentStatus,
    CommunityCase,
)
from chorus.domain.errors import DomainError, DomainErrorCode, IntegrityError
from chorus.domain.facts import FactStatus
from chorus.domain.ids import (
    CaseId,
    CommitmentId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    IdGenerator,
    Namespace,
    Sha256Digest,
)
from chorus.domain.state import CaseTransitionContext, transition_case, transition_commitment
from chorus.ports.clock import Clock
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotencyStatus,
    IdempotentCommand,
)
from chorus.ports.records import ActionPointerExpectation, CurrentActionPointer
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    IdempotencyRepositoryPort,
    ShareableRepositoryPort,
)
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import CheckItem, PutItem
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork

VERIFY_TRANSACTION = "verify-commitment"

VERIFY_PARTICIPANTS = 5
"""Commitment, case, action pointer, audit event, and commit proof -- on **both** branches."""

FULFILLED_REASON_CODE = "COMMITMENT_FULFILLED"
MISSED_REASON_CODE = "COMMITMENT_MISSED"


class VerificationOutcome(StrEnum):
    """The two decisions the affected contributor may take, and there is no third."""

    FULFILLED = "FULFILLED"
    MISSED = "MISSED"


class VerificationRefusal(StrEnum):
    """Why a verification was declined, with the commitment and case left exactly as found."""

    COMMITMENT_NOT_DUE = "COMMITMENT_NOT_DUE"
    CASE_NOT_VERIFYING = "CASE_NOT_VERIFYING"
    ACTOR_NOT_AFFECTED = "ACTOR_NOT_AFFECTED"
    STALE_COMMITMENT_VERSION = "STALE_COMMITMENT_VERSION"


class VerificationRefusedError(DomainError):
    """The verification declined, under one closed code, having written nothing."""

    __slots__ = ("refusal",)

    def __init__(self, refusal: VerificationRefusal) -> None:
        super().__init__(DomainErrorCode.STATE_TRANSITION_ERROR, refusal.value)
        self.refusal = refusal

    @property
    def safe_code(self) -> str:
        return self.refusal.value


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifyCommitmentCommand:
    """One human decision about one commitment.

    ``contributor_id`` is resolved from the authenticated persona by the transport and is never
    read from the body: a body that could name whose decision this is would be a body that could
    impersonate the person the whole edge exists to require.
    """

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    commitment_id: CommitmentId
    contributor_id: ContributorId
    expected_version: int
    outcome: VerificationOutcome
    actor_id_hash: Sha256Digest
    correlation_id: UUID
    idempotency_key: str
    note: str | None = None
    verification_evidence_id: EvidenceItemId | None = None

    @property
    def scope(self) -> CaseScope:
        return CaseScope(
            namespace=self.namespace, community_id=self.community_id, case_id=self.case_id
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifyCommitmentResult:
    commitment_status: CommitmentStatus
    commitment_version: int
    case_state: CaseState
    case_version: int
    action_pointer_invalidated: bool
    replayed: bool


@dataclass(slots=True)
class VerifyCommitment:
    """Record one affected contributor's decision, and the case edge that follows from it."""

    core: CoreRepositoryPort
    shareable: ShareableRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    clock: Clock
    ids: IdGenerator

    async def execute(self, command: VerifyCommitmentCommand) -> VerifyCommitmentResult:
        key = self._key(command)
        replayed = await self._replay(key, command)
        if replayed is not None:
            return replayed

        commitment = await self.shareable.load_commitment(command.scope, command.commitment_id)
        case = await self.core.load_case(command.scope)
        if commitment.version != command.expected_version:
            raise VerificationRefusedError(VerificationRefusal.STALE_COMMITMENT_VERSION)
        if commitment.status is not CommitmentStatus.DUE:
            raise VerificationRefusedError(VerificationRefusal.COMMITMENT_NOT_DUE)
        if case.state is not CaseState.VERIFYING:
            raise VerificationRefusedError(VerificationRefusal.CASE_NOT_VERIFYING)
        if not await self._is_affected(command, case):
            raise VerificationRefusedError(VerificationRefusal.ACTOR_NOT_AFFECTED)

        pointer = await self.shareable.load_current_action_pointer(command.scope)
        if pointer is None:  # pragma: no cover - a VERIFYING case always has one
            raise IntegrityError("CURRENT_ACTION_POINTER")
        return await self._verify(command, case, commitment, pointer, key=key)

    # -- authorization ---------------------------------------------------------------------

    async def _is_affected(self, command: VerifyCommitmentCommand, case: CommunityCase) -> bool:
        """Own at least one ``ACTIVE`` fact in this case, checked against loaded facts.

        Deterministic and never a claim in the request body. The whole point of the human guard
        is that somebody with a stake in the outcome says whether the elevator was fixed, so
        "who is affected" has to come from durable state rather than from the request that
        wants the answer.
        """

        if not case.fact_ids:
            return False
        facts = await self.core.load_facts(command.scope, case.fact_ids)
        return any(
            fact.contributor_id == command.contributor_id and fact.status is FactStatus.ACTIVE
            for fact in facts
        )

    # -- the transaction ---------------------------------------------------------------------

    async def _verify(
        self,
        command: VerifyCommitmentCommand,
        case: CommunityCase,
        commitment: Commitment,
        pointer: CurrentActionPointer,
        *,
        key: IdempotencyKey,
    ) -> VerifyCommitmentResult:
        now = self.clock.now()
        fulfilled = command.outcome is VerificationOutcome.FULFILLED
        moved = transition_commitment(
            commitment,
            CommitmentStatus.FULFILLED if fulfilled else CommitmentStatus.MISSED,
            expected_version=commitment.version,
            now=now,
            # The guard ADR-027 § 5 added. A system actor cannot reach either outcome.
            actor_is_human=True,
            verified_by_contributor_id=command.contributor_id,
            verification_evidence_id=command.verification_evidence_id,
            outcome_note=command.note,
        )
        target = CaseState.RESOLVED if fulfilled else CaseState.READY_FOR_ACTION
        next_case = transition_case(
            case,
            target,
            expected_version=case.version,
            reason_code=FULFILLED_REASON_CODE if fulfilled else MISSED_REASON_CODE,
            now=now,
            context=CaseTransitionContext(
                actor_is_human=True,
                affected_contributor_verified=fulfilled,
                commitment_missed=not fulfilled,
            ),
        )
        if next_case.authorization_version != case.authorization_version:  # pragma: no cover
            # ADR-020 row 10. A verification outcome changes no disclosure input.
            raise IntegrityError("COMMUNITY_CASE")

        expectation = ActionPointerExpectation(
            row_version=pointer.version, proposal_hash=pointer.proposal_hash
        )
        operations = (
            self.shareable.stage_update_commitment(
                command.scope,
                moved,
                expected_version=commitment.version,
                expected_status=CommitmentStatus.DUE,
            ),
            self.core.stage_update_case(
                command.scope,
                next_case,
                expected_version=case.version,
                expected_authorization_version=case.authorization_version,
                expected_state=CaseState.VERIFYING,
            ),
            self._pointer_participant(
                command, pointer, expectation=expectation, fulfilled=fulfilled, now=now
            ),
            self.audit.stage_append_case_event(
                command.scope,
                self._audit_event(command, case=next_case, commitment=moved, now=now),
            ),
            self.idempotency.stage_create_completed(
                key,
                request_hash=self._request_hash(command),
                result_entity_refs=(
                    EntityRef(
                        entity_type="COMMITMENT",
                        entity_id=command.commitment_id.value,
                        version=moved.version,
                    ),
                    EntityRef(
                        entity_type="COMMUNITY_CASE",
                        entity_id=command.case_id.value,
                        version=next_case.version,
                    ),
                ),
                response_status=200,
                now=now,
            ),
        )
        if len(operations) != VERIFY_PARTICIPANTS:  # pragma: no cover - arithmetic guard
            raise IntegrityError("COMMITMENT")
        await self.unit_of_work.commit(
            TransactionPlan(
                name=VERIFY_TRANSACTION,
                operations=operations,
                audit_required=True,
                commit_proof=self.idempotency.commit_proof(
                    key, request_hash=self._request_hash(command)
                ),
            )
        )
        observability.commitment_verified(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            commitment_id=command.commitment_id.value,
            actor_id_hash=command.actor_id_hash,
            fulfilled=fulfilled,
            case_version=next_case.version,
        )
        return VerifyCommitmentResult(
            commitment_status=moved.status,
            commitment_version=moved.version,
            case_state=next_case.state,
            case_version=next_case.version,
            action_pointer_invalidated=not fulfilled,
            replayed=False,
        )

    def _pointer_participant(
        self,
        command: VerifyCommitmentCommand,
        pointer: CurrentActionPointer,
        *,
        expectation: ActionPointerExpectation,
        fulfilled: bool,
        now: datetime,
    ) -> CheckItem | PutItem:
        """One participant on both branches: a condition on ``FULFILLED``, a move on ``MISSED``.

        The count is what matters. A branch that staged four participants and a branch that
        staged five would make the transaction's shape a signal about which outcome a case
        took, and the arithmetic assertion could then only be written per branch.
        """

        if fulfilled:
            return self.shareable.stage_require_current_action_pointer(
                command.scope, expected=expectation
            )
        invalidated = CurrentActionPointer(
            namespace=pointer.namespace,
            community_id=pointer.community_id,
            case_id=pointer.case_id,
            action_id=pointer.action_id,
            execution_id=pointer.execution_id,
            proposal_hash=pointer.proposal_hash,
            view_id=pointer.view_id,
            view_hash=pointer.view_hash,
            case_version=pointer.case_version,
            authorization_version=pointer.authorization_version,
            status=ActionProposalStatus.INVALIDATED,
            version=pointer.version + 1,
            created_at=pointer.created_at,
            updated_at=now,
        )
        return self.shareable.stage_replace_current_action_pointer(
            command.scope, invalidated, expected=expectation
        )

    # -- idempotency -------------------------------------------------------------------------

    def _key(self, command: VerifyCommitmentCommand) -> IdempotencyKey:
        return IdempotencyKey(
            partition=IdempotencyPartition(
                kind=IdempotencyPartitionKind.CASE,
                namespace=command.namespace,
                case_id=command.case_id,
            ),
            command=IdempotentCommand.VERIFY_COMMITMENT,
            actor_id_hash=command.actor_id_hash,
            key_hash=key_hash(f"verify-commitment\x1f{command.idempotency_key}"),
        )

    @staticmethod
    def _request_hash(command: VerifyCommitmentCommand) -> Sha256Digest:
        """The identity of this decision: which commitment, at which version, decided how.

        The optional note is deliberately outside it. Two submissions of one decision that
        differ only in wording are the same decision, and treating them as a conflict would
        refuse a contributor who retried after correcting a typo.
        """

        return key_hash(
            "\x1f".join(
                (
                    "verify-commitment/v1",
                    str(command.case_id),
                    str(command.commitment_id),
                    str(command.expected_version),
                    command.outcome.value,
                )
            )
        )

    async def _replay(
        self, key: IdempotencyKey, command: VerifyCommitmentCommand
    ) -> VerifyCommitmentResult | None:
        record = await self.idempotency.load(key)
        if record is None or record.status is not IdempotencyStatus.COMPLETED:
            return None
        if record.request_hash != self._request_hash(command):
            raise VerificationRefusedError(VerificationRefusal.STALE_COMMITMENT_VERSION)
        commitment = await self.shareable.load_commitment(command.scope, command.commitment_id)
        case = await self.core.load_case(command.scope)
        return VerifyCommitmentResult(
            commitment_status=commitment.status,
            commitment_version=commitment.version,
            case_state=case.state,
            case_version=case.version,
            action_pointer_invalidated=commitment.status is CommitmentStatus.MISSED,
            replayed=True,
        )

    def _audit_event(
        self,
        command: VerifyCommitmentCommand,
        *,
        case: CommunityCase,
        commitment: Commitment,
        now: datetime,
    ) -> AuditEvent:
        fulfilled = command.outcome is VerificationOutcome.FULFILLED
        return AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            actor_type=ActorType.HUMAN,
            actor_id_hash=command.actor_id_hash,
            event_type=(
                observability.EventName.COMMITMENT_FULFILLED
                if fulfilled
                else observability.EventName.COMMITMENT_MISSED
            ),
            occurred_at=now,
            correlation_id=command.correlation_id,
            causation_id=None,
            idempotency_key_hash=key_hash(command.idempotency_key),
            entity_refs=(
                AuditEntityRef(
                    entity_type="COMMITMENT",
                    entity_id=commitment.commitment_id.value,
                    version=commitment.version,
                ),
                AuditEntityRef(
                    entity_type="COMMUNITY_CASE",
                    entity_id=command.case_id.value,
                    version=case.version,
                ),
            ),
            decision=AuditDecision.ALLOW,
            reason_codes=(FULFILLED_REASON_CODE if fulfilled else MISSED_REASON_CODE,),
            safe_details=AuditDetails(count=None, rule_id=None),
            input_hash=None,
            output_hash=None,
        )


__all__ = [
    "FULFILLED_REASON_CODE",
    "MISSED_REASON_CODE",
    "VERIFY_PARTICIPANTS",
    "VERIFY_TRANSACTION",
    "VerificationOutcome",
    "VerificationRefusal",
    "VerificationRefusedError",
    "VerifyCommitment",
    "VerifyCommitmentCommand",
    "VerifyCommitmentResult",
]
