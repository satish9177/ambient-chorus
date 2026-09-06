"""Withdrawing an approval, and clearing a definite failure. Five participants either way.

The two verbs the approvals route cannot express
------------------------------------------------
**Withdraw** takes an ``APPROVED`` execution to ``FAILED / APPROVAL_WITHDRAWN``. It exists
because an approval that cannot be taken back before anything external has happened makes the
fifteen-minute expiry the only way to change one's mind, which is a worse answer than a race
whose loser is told plainly.

**Clear** takes an already-terminal ``FAILED`` execution and moves nothing but the pointer. It
exists because the failure matrix's own remedy for a definite send failure -- "create and
approve a fresh proposal" -- was unreachable without it: a new proposal requires an
``INVALIDATED`` pointer whose execution is terminal ``FAILED``, and the only path that set a
pointer to ``INVALIDATED`` was rejection, which is defined over a ``DRAFT`` (ADR-023 SS 8).

Withdrawal is a race, and the compare-and-swap is the referee
--------------------------------------------------------------
A human withdrawing at ``APPROVED@v`` and a sender claiming at ``APPROVED@v`` are two
conditional writes to one row; exactly one commits. If the sender wins, the withdrawal is a
conflict and the message goes; if the human wins, the sender's claim is a conflict and it never
renders a payload for SES. There is no window in which both believe they won, and there is
deliberately **no attempt to make the human always win** -- that would require holding a lock
across an external call.

What this route refuses, and why each refusal is different
-----------------------------------------------------------
``SENDING`` is refused because a send is in flight and the honest answer to "may I take it back"
is that nobody can. ``SENT`` is refused because a sent message cannot be recalled.
``SEND_UNKNOWN`` is refused because it is a quarantine: only reconciliation resolves it, to
``SENT`` or to ``FAILED``, and a ``FAILED`` outcome is then clearable like any other.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from chorus.application import observability
from chorus.application.services.action_authorization import (
    invalidation_key_hash,
    invalidation_request_hash,
    send_key,
)
from chorus.application.services.case_readiness import (
    ReadinessDecision,
    evaluate_invalidation_readiness,
    stage_case_after_invalidation,
)
from chorus.domain.entities import (
    ActionExecution,
    ActionExecutionState,
    ActionProposalStatus,
    ActorType,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    CaseState,
    CommunityCase,
)
from chorus.domain.errors import DomainError, DomainErrorCode, IntegrityError
from chorus.domain.ids import (
    ActionId,
    CaseId,
    CommunityId,
    ExecutionId,
    IdGenerator,
    Namespace,
    Sha256Digest,
)
from chorus.domain.state import transition_action_execution
from chorus.ports.clock import Clock
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.idempotency import EntityRef, IdempotencyKey
from chorus.ports.records import ActionPointerExpectation, CurrentActionPointer
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    IdempotencyRepositoryPort,
    ShareableRepositoryPort,
)
from chorus.ports.scopes import ActionScope, CaseScope
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork

INVALIDATE_TRANSACTION = "invalidate-action"

INVALIDATE_PARTICIPANTS = 5
"""Shape B-prime: B without the ``Approval`` row.

Execution transition **or** a ``ConditionCheck`` that it is already terminal; pointer to
``INVALIDATED``; ``action.invalidated`` audit; idempotency record; and the case participant in
its Put-or-Check form. Five either way, because both alternations swap one participant for
another in the same position.
"""

WITHDRAWN_CODE = "APPROVAL_WITHDRAWN"
CLEARED_CODE = "TERMINAL_EXECUTION_CLEARED"

INVALIDATABLE_STATES: frozenset[ActionExecutionState] = frozenset(
    {ActionExecutionState.DRAFT, ActionExecutionState.APPROVED, ActionExecutionState.FAILED}
)
"""The three states a human may clear, and the three that are absent are the whole rule.

``SENDING``, ``SENT``, and ``SEND_UNKNOWN`` are not here. Expressed as a set rather than as
three ``if`` statements so a state added to the enum is refused by default rather than
accidentally admitted.
"""


class InvalidationDenial(StrEnum):
    """Why an invalidation was refused. Closed codes only."""

    CROSS_CASE = "CROSS_CASE_VIOLATION"
    NO_CURRENT_PROPOSAL = "NO_CURRENT_PROPOSAL"
    PROPOSAL_NOT_CURRENT = "PROPOSAL_NOT_CURRENT"
    PROPOSAL_HASH_MISMATCH = "PROPOSAL_HASH_MISMATCH"
    EXECUTION_VERSION_MISMATCH = "EXECUTION_VERSION_MISMATCH"
    SEND_IN_FLIGHT = "SEND_IN_FLIGHT"
    ALREADY_SENT = "ALREADY_SENT"
    SEND_OUTCOME_UNKNOWN = "SEND_OUTCOME_UNKNOWN"


class InvalidationDeniedError(DomainError):
    """An invalidation refused before anything was staged, under one closed code."""

    __slots__ = ("denial",)

    def __init__(self, denial: InvalidationDenial) -> None:
        super().__init__(DomainErrorCode.VALIDATION_ERROR, denial.value)
        self.denial = denial

    @property
    def safe_code(self) -> str:
        return self.denial.value


_REFUSALS: dict[ActionExecutionState, InvalidationDenial] = {
    ActionExecutionState.SENDING: InvalidationDenial.SEND_IN_FLIGHT,
    ActionExecutionState.SENT: InvalidationDenial.ALREADY_SENT,
    ActionExecutionState.SEND_UNKNOWN: InvalidationDenial.SEND_OUTCOME_UNKNOWN,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class InvalidateActionCommand:
    """The frozen invalidation body: an expected version and the proposal it names."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    expected_execution_version: int
    proposal_hash: Sha256Digest
    actor_id_hash: Sha256Digest
    correlation_id: UUID
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.expected_execution_version < 1:
            raise ValueError("expected_execution_version must be positive")
        if not self.idempotency_key:
            raise ValueError("an invalidation names the key it was made under")

    @property
    def scope(self) -> CaseScope:
        return CaseScope(
            namespace=self.namespace, community_id=self.community_id, case_id=self.case_id
        )

    @property
    def action_scope(self) -> ActionScope:
        return ActionScope(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
            action_id=self.action_id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class InvalidateActionResult:
    """What the pointer, the execution, and the case now are."""

    action_id: ActionId
    execution_id: ExecutionId
    execution_state: ActionExecutionState
    execution_version: int
    pointer_status: ActionProposalStatus
    case_state: CaseState
    case_version: int
    authorization_version: int
    reason_code: str


@dataclass(slots=True)
class InvalidateAction:
    """Clear one proposal so the case can hold another, without ever unsending anything."""

    core: CoreRepositoryPort
    shareable: ShareableRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    clock: Clock
    ids: IdGenerator

    async def execute(self, command: InvalidateActionCommand) -> InvalidateActionResult:
        now = self.clock.now()
        pointer, execution, case = await self._load_and_prove(command)
        readiness = await evaluate_invalidation_readiness(
            shareable=self.shareable, scope=command.scope, case=case, now=now
        )
        cleared = execution.state is ActionExecutionState.FAILED
        reason_code = CLEARED_CODE if cleared else WITHDRAWN_CODE
        key, request_hash = self._transaction_key(command)
        next_execution = (
            execution
            if cleared
            else transition_action_execution(
                execution,
                ActionExecutionState.FAILED,
                expected_version=execution.version,
                now=now,
                finished_at=now,
                failure_code=reason_code,
            )
        )
        operations = (
            # Alternation one: a guarded transition when there is something to move, and a
            # read-only assertion when the row is already terminal. A ``PutItem`` in the second
            # case would rewrite a record of something that already happened, which monotonic
            # presence exists to refuse.
            self.shareable.stage_require_execution(
                command.action_scope, execution, expected_version=execution.version
            )
            if cleared
            else self.shareable.stage_update_execution(
                command.action_scope, next_execution, expected_version=execution.version
            ),
            self.shareable.stage_replace_current_action_pointer(
                command.scope,
                _invalidated(pointer, now=now),
                expected=ActionPointerExpectation(
                    row_version=pointer.version, proposal_hash=pointer.proposal_hash
                ),
            ),
            self.audit.stage_append_case_event(
                command.scope,
                self._audit_event(
                    command, execution=next_execution, reason_code=reason_code, now=now
                ),
            ),
            self.idempotency.stage_create_completed(
                key,
                request_hash=request_hash,
                result_entity_refs=(
                    EntityRef(
                        entity_type="ACTION_EXECUTION",
                        entity_id=next_execution.execution_id.value,
                        version=next_execution.version,
                    ),
                ),
                response_status=200,
                now=now,
            ),
            # Alternation two: Put when readiness remains, Check when it does not. Same
            # position, same count.
            stage_case_after_invalidation(
                core=self.core, scope=command.scope, case=case, readiness=readiness, now=now
            ),
        )
        if len(operations) != INVALIDATE_PARTICIPANTS:  # pragma: no cover - arithmetic guard
            raise IntegrityError("ACTION_EXECUTION")
        try:
            await self.unit_of_work.commit(
                TransactionPlan(
                    name=INVALIDATE_TRANSACTION,
                    operations=operations,
                    audit_required=True,
                    commit_proof=self.idempotency.commit_proof(key, request_hash=request_hash),
                )
            )
        except PersistenceConflictError:
            # The sender claimed this execution first, or the pointer moved. The withdrawal
            # loses and is told so; nothing was written and the message, if any, goes.
            observability.approval_conflict(
                namespace=command.namespace,
                community_id=command.community_id,
                case_id=command.case_id,
                correlation_id=command.correlation_id,
                actor_id_hash=command.actor_id_hash,
                reason_codes=(InvalidationDenial.EXECUTION_VERSION_MISMATCH.value,),
            )
            raise
        return InvalidateActionResult(
            action_id=command.action_id,
            execution_id=next_execution.execution_id,
            execution_state=next_execution.state,
            execution_version=next_execution.version,
            pointer_status=ActionProposalStatus.INVALIDATED,
            case_state=readiness.next_case.state,
            case_version=readiness.next_case.version,
            authorization_version=readiness.next_case.authorization_version,
            reason_code=reason_code,
        )

    async def _load_and_prove(
        self, command: InvalidateActionCommand
    ) -> tuple[CurrentActionPointer, ActionExecution, CommunityCase]:
        """Scope, pointer identity, execution state, and expected version. Nothing else.

        No freshness check of any kind. Clearing a proposal is the repair for a proposal that
        has gone stale, so a staleness check here would make the stale case unrepairable --
        the same reason rejection re-checks nothing beyond these three.
        """

        pointer = await self.shareable.load_current_action_pointer(command.scope)
        if pointer is None:
            raise InvalidationDeniedError(InvalidationDenial.NO_CURRENT_PROPOSAL)
        if pointer.action_id != command.action_id:
            raise InvalidationDeniedError(InvalidationDenial.PROPOSAL_NOT_CURRENT)
        if pointer.proposal_hash != command.proposal_hash:
            raise InvalidationDeniedError(InvalidationDenial.PROPOSAL_HASH_MISMATCH)
        if pointer.case_id != command.case_id:  # pragma: no cover - pointer is case-keyed
            raise InvalidationDeniedError(InvalidationDenial.CROSS_CASE)

        execution = await self.shareable.load_execution(command.action_scope, pointer.execution_id)
        refusal = _REFUSALS.get(execution.state)
        if refusal is not None:
            raise InvalidationDeniedError(refusal)
        if execution.state not in INVALIDATABLE_STATES:  # pragma: no cover - closed enum
            raise InvalidationDeniedError(InvalidationDenial.SEND_IN_FLIGHT)
        if execution.version != command.expected_execution_version:
            raise InvalidationDeniedError(InvalidationDenial.EXECUTION_VERSION_MISMATCH)
        case = await self.core.load_case(command.scope)
        return pointer, execution, case

    def _transaction_key(
        self, command: InvalidateActionCommand
    ) -> tuple[IdempotencyKey, Sha256Digest]:
        """The invalidation's commit proof, in the execution partition it is about.

        Under the ``SEND_ACTION`` family because an invalidation is a statement about a send
        attempt that will not happen, and in the ``EXECUTION`` partition so the record sits
        where the execution does. Its **own** domain separator, so one client key cannot
        address both an invalidation and a send under the same family.
        """

        key = send_key(
            namespace=command.namespace,
            action_id=command.action_id,
            actor_id_hash=command.actor_id_hash,
            key_hash=invalidation_key_hash(command.idempotency_key),
        )
        request_hash = invalidation_request_hash(
            case_id=command.case_id,
            action_id=command.action_id,
            expected_execution_version=command.expected_execution_version,
            proposal_hash=command.proposal_hash,
        )
        return key, request_hash

    def _audit_event(
        self,
        command: InvalidateActionCommand,
        *,
        execution: ActionExecution,
        reason_code: str,
        now: datetime,
    ) -> AuditEvent:
        return AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            actor_type=ActorType.HUMAN,
            actor_id_hash=command.actor_id_hash,
            event_type="action.invalidated",
            occurred_at=now,
            correlation_id=command.correlation_id,
            causation_id=None,
            idempotency_key_hash=invalidation_key_hash(command.idempotency_key),
            entity_refs=(
                AuditEntityRef(
                    entity_type="ACTION_EXECUTION",
                    entity_id=execution.execution_id.value,
                    version=execution.version,
                ),
                AuditEntityRef(
                    entity_type="ACTION_PROPOSAL", entity_id=command.action_id.value, version=None
                ),
            ),
            decision=AuditDecision.DENY,
            reason_codes=(reason_code,),
            safe_details=AuditDetails(count=None, rule_id=None),
            input_hash=command.proposal_hash,
            output_hash=None,
        )


def _invalidated(pointer: CurrentActionPointer, *, now: datetime) -> CurrentActionPointer:
    """The same pointer at ``INVALIDATED``, carrying forward which proposal was cleared."""

    return replace(
        pointer,
        status=ActionProposalStatus.INVALIDATED,
        version=pointer.version + 1,
        updated_at=now,
    )


__all__ = [
    "CLEARED_CODE",
    "INVALIDATABLE_STATES",
    "INVALIDATE_PARTICIPANTS",
    "WITHDRAWN_CODE",
    "InvalidateAction",
    "InvalidateActionCommand",
    "InvalidateActionResult",
    "InvalidationDenial",
    "InvalidationDeniedError",
    "ReadinessDecision",
]
