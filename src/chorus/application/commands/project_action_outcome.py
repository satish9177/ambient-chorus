"""Shape G: the private case projection to ``ACTIONED``, run by the application worker.

Why the *worker* owns this, and not the sender
-----------------------------------------------
The sender has no Core access at all, so it cannot move a case. The application worker performs
this projection after sender return, or on replay after a lost return, from the one durable
fact that settles it: the execution row. That split is what makes "the sender needs no Core
access" true rather than aspirational, and it is why a worker crash cannot lose a sent result --
the send is already recorded where the worker can read it.

Four participants, and the second is the one that matters
-----------------------------------------------------------
1. the case ``ACTION_PROPOSED@{v,a} -> ACTIONED@{v+1,a}``, conditional on exact ``version``,
   ``authorization_version``, and ``state``;
2. a Shareable **ConditionCheck** that the execution is ``SENT`` at its exact version;
3. the ``action.actioned`` audit event;
4. the projection commit proof, in the ``EXECUTION`` partition.

Participant 2 is a condition rather than a read because an execution that moved between the
worker's read and its write would otherwise let a case be marked ``ACTIONED`` on the strength of
a state it no longer holds. It writes nothing: the execution is the sender's row, and the worker
has no business changing it.

``authorization_version`` is carried forward unchanged. Recording a send outcome changes no
fact, status, mandate, or count (ADR-020 SS 2 row 7), and bumping the epoch here would stale
every view in the case for having successfully sent a message.

``FAILED`` and ``SEND_UNKNOWN`` take **no case edge at all.** The case stays ``ACTION_PROPOSED``
and the surface shows the safe execution banner. There is deliberately no "failed" case state:
a definite failure is cleared by the human through the invalidation route, and an ambiguous one
is a quarantine nothing resolves without proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from chorus.application.services.action_authorization import (
    send_key,
    send_projection_key_hash,
    send_request_hash,
)
from chorus.domain.entities import (
    ActionExecution,
    ActionExecutionState,
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
    ApprovalId,
    CaseId,
    CommunityId,
    ExecutionId,
    IdGenerator,
    Namespace,
    Sha256Digest,
)
from chorus.domain.state import CaseTransitionContext, transition_case
from chorus.ports.clock import Clock
from chorus.ports.idempotency import EntityRef, IdempotencyKey
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    IdempotencyRepositoryPort,
    ShareableRepositoryPort,
)
from chorus.ports.scopes import ActionScope, CaseScope
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork

PROJECTION_TRANSACTION = "project-action-outcome"

PROJECTION_PARTICIPANTS = 4
"""Case Put, execution ConditionCheck, audit event, and the projection commit proof."""

ACTIONED_REASON_CODE = "ACTION_SENT"


class ProjectionRefusal(StrEnum):
    """Why the projection declined to move the case. Every one leaves the case exactly as found."""

    EXECUTION_NOT_SENT = "EXECUTION_NOT_SENT"
    CASE_NOT_ACTION_PROPOSED = "CASE_NOT_ACTION_PROPOSED"
    ALREADY_PROJECTED = "ALREADY_PROJECTED"


class ProjectionRefusedError(DomainError):
    """The projection declined, under one closed code, having written nothing."""

    __slots__ = ("refusal",)

    def __init__(self, refusal: ProjectionRefusal) -> None:
        super().__init__(DomainErrorCode.STATE_TRANSITION_ERROR, refusal.value)
        self.refusal = refusal

    @property
    def safe_code(self) -> str:
        return self.refusal.value


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectActionOutcomeCommand:
    """Project one execution's terminal outcome onto its private case."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    execution_id: ExecutionId
    actor_id_hash: Sha256Digest
    correlation_id: UUID

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
class ProjectActionOutcomeResult:
    case_state: CaseState
    case_version: int
    authorization_version: int
    execution_state: ActionExecutionState
    projected: bool
    """``False`` when the outcome legitimately takes no case edge -- ``FAILED`` and
    ``SEND_UNKNOWN`` both leave the case ``ACTION_PROPOSED``, and that is a result rather than
    an error."""


@dataclass(slots=True)
class ProjectActionOutcome:
    """Move ``ACTION_PROPOSED -> ACTIONED`` when, and only when, the execution is ``SENT``."""

    core: CoreRepositoryPort
    shareable: ShareableRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    clock: Clock
    ids: IdGenerator

    async def execute(self, command: ProjectActionOutcomeCommand) -> ProjectActionOutcomeResult:
        execution = await self.shareable.load_execution(command.action_scope, command.execution_id)
        case = await self.core.load_case(command.scope)
        if case.state is CaseState.ACTIONED:
            # Already projected. A replay reads this and reports it rather than retrying: the
            # transaction's own conditions would refuse anyway, and reporting is the honest
            # answer to "did this happen".
            return ProjectActionOutcomeResult(
                case_state=case.state,
                case_version=case.version,
                authorization_version=case.authorization_version,
                execution_state=execution.state,
                projected=False,
            )
        if execution.state is not ActionExecutionState.SENT:
            # No case edge at all. FAILED and SEND_UNKNOWN are terminal for the execution and
            # leave the case proposed with its safe banner.
            return ProjectActionOutcomeResult(
                case_state=case.state,
                case_version=case.version,
                authorization_version=case.authorization_version,
                execution_state=execution.state,
                projected=False,
            )
        if case.state is not CaseState.ACTION_PROPOSED:
            raise ProjectionRefusedError(ProjectionRefusal.CASE_NOT_ACTION_PROPOSED)
        return await self._project(command, case, execution)

    async def _project(
        self,
        command: ProjectActionOutcomeCommand,
        case: CommunityCase,
        execution: ActionExecution,
    ) -> ProjectActionOutcomeResult:
        now = self.clock.now()
        actioned = transition_case(
            case,
            CaseState.ACTIONED,
            expected_version=case.version,
            reason_code=ACTIONED_REASON_CODE,
            now=now,
            context=CaseTransitionContext(
                execution_sent=True,
                # Consumption is a property of the execution, not a field on the approval: the
                # row reaching SENDING *is* the consumption, and SENT implies it (ADR-023 SS 1).
                approval_consumed=True,
            ),
        )
        if actioned.authorization_version != case.authorization_version:  # pragma: no cover
            # Asserted rather than assumed. Recording a send outcome changes no disclosure
            # input, so an epoch that moved here would stale every view in the case.
            raise IntegrityError("COMMUNITY_CASE")

        key, request_hash = self._key(command, execution)
        operations = (
            self.core.stage_update_case(
                command.scope,
                actioned,
                expected_version=case.version,
                expected_authorization_version=case.authorization_version,
                expected_state=CaseState.ACTION_PROPOSED,
            ),
            self.shareable.stage_require_execution(
                command.action_scope, execution, expected_version=execution.version
            ),
            self.audit.stage_append_case_event(
                command.scope,
                self._audit_event(command, case=actioned, execution=execution, now=now),
            ),
            self.idempotency.stage_create_completed(
                key,
                request_hash=request_hash,
                result_entity_refs=(
                    EntityRef(
                        entity_type="COMMUNITY_CASE",
                        entity_id=command.case_id.value,
                        version=actioned.version,
                    ),
                    EntityRef(
                        entity_type="ACTION_EXECUTION",
                        entity_id=execution.execution_id.value,
                        version=execution.version,
                    ),
                ),
                response_status=200,
                now=now,
            ),
        )
        if len(operations) != PROJECTION_PARTICIPANTS:  # pragma: no cover - arithmetic guard
            raise IntegrityError("COMMUNITY_CASE")
        await self.unit_of_work.commit(
            TransactionPlan(
                name=PROJECTION_TRANSACTION,
                operations=operations,
                audit_required=True,
                commit_proof=self.idempotency.commit_proof(key, request_hash=request_hash),
            )
        )
        return ProjectActionOutcomeResult(
            case_state=actioned.state,
            case_version=actioned.version,
            authorization_version=actioned.authorization_version,
            execution_state=execution.state,
            projected=True,
        )

    def _key(
        self, command: ProjectActionOutcomeCommand, execution: ActionExecution
    ) -> tuple[IdempotencyKey, Sha256Digest]:
        """Domain 6, keyed on the execution's own send key rather than on any client key.

        A projection can be attempted by a worker replay any number of times and from any
        delivery, so a record naming the *attempt* is the only one that is replay-safe
        regardless of who asked.
        """

        send_execution_key = execution.idempotency_key
        approval_id = execution.approval_id
        if send_execution_key is None or approval_id is None:  # pragma: no cover
            raise IntegrityError("ACTION_EXECUTION")
        key = send_key(
            namespace=command.namespace,
            action_id=command.action_id,
            actor_id_hash=command.actor_id_hash,
            key_hash=send_projection_key_hash(send_execution_key),
        )
        request_hash = send_request_hash(
            case_id=command.case_id,
            action_id=command.action_id,
            execution_id=command.execution_id,
            approval_id=ApprovalId(approval_id.value),
            expected_execution_version=execution.version,
        )
        return key, request_hash

    def _audit_event(
        self,
        command: ProjectActionOutcomeCommand,
        *,
        case: CommunityCase,
        execution: ActionExecution,
        now: datetime,
    ) -> AuditEvent:
        return AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            actor_type=ActorType.SYSTEM,
            actor_id_hash=command.actor_id_hash,
            event_type="action.actioned",
            occurred_at=now,
            correlation_id=command.correlation_id,
            causation_id=None,
            idempotency_key_hash=None,
            entity_refs=(
                AuditEntityRef(
                    entity_type="COMMUNITY_CASE",
                    entity_id=command.case_id.value,
                    version=case.version,
                ),
                AuditEntityRef(
                    entity_type="ACTION_EXECUTION",
                    entity_id=execution.execution_id.value,
                    version=execution.version,
                ),
            ),
            decision=AuditDecision.ALLOW,
            reason_codes=(ACTIONED_REASON_CODE,),
            safe_details=AuditDetails(count=None, rule_id=None),
            input_hash=execution.rendered_message_hash,
            output_hash=execution.ses_request_token_hash,
        )


__all__ = [
    "ACTIONED_REASON_CODE",
    "PROJECTION_PARTICIPANTS",
    "ProjectActionOutcome",
    "ProjectActionOutcomeCommand",
    "ProjectActionOutcomeResult",
    "ProjectionRefusal",
    "ProjectionRefusedError",
]
