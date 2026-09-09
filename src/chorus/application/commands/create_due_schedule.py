"""Transaction B-prime: create the deterministic one-time schedule, then record it exists.

The sequence is frozen (ADR-028 § 4): transaction B commits the commitment ``PENDING``
and its projection ``PENDING_SCHEDULE``;
``create_due_schedule`` runs **outside and after** it; then this two-participant transaction
moves the projection to ``CREATED`` and writes ``schedule.created``.

A failure writes ``schedule.failed``, increments ``attempts``, records the typed code, and
leaves the case visibly unscheduled with a banner. **The commitment stays ``PENDING``** -- what
failed is the alarm clock, not the promise, and encoding an infrastructure outcome as a domain
status is how the two come to be confused.

Retry uses the **same name and the same client token**, both derived from
``{commitment_id, generation}``. A lost create response is reconciled by ``describe_schedule``
on that exact name and configuration, never by creating a second differently named schedule --
which is why the port has no ``delete_schedule`` and no ``update_schedule`` for one to be
cleaned up with.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from chorus.application import observability
from chorus.application.services.commitment_schedule import (
    demo_schedule_instant,
    due_schedule_request,
)
from chorus.domain.entities import (
    ActorType,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    Commitment,
)
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import CommunityId, IdGenerator, Namespace, Sha256Digest
from chorus.ports.clock import Clock
from chorus.ports.records import CommitmentScheduleProjection, CommitmentScheduleStatus
from chorus.ports.repositories import AuditRepositoryPort, ShareableRepositoryPort
from chorus.ports.scheduler import (
    DeadlineSchedulerPort,
    ScheduleCreateFailed,
    ScheduleFailureCode,
)
from chorus.ports.scopes import CaseScope
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork

SCHEDULE_TRANSACTION = "record-schedule-created"
SCHEDULE_FAILURE_TRANSACTION = "record-schedule-failed"

SCHEDULE_PARTICIPANTS = 2
"""The guarded projection update and its audit event. Nothing else moves."""

SCHEDULE_CREATED_REASON_CODE = "SCHEDULE_CREATED"


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateDueScheduleCommand:
    """Schedule one commitment's deadline. Every scheduler value is derived, none passed in."""

    namespace: Namespace
    community_id: CommunityId
    commitment: Commitment
    actor_id_hash: Sha256Digest
    correlation_id: UUID
    logical_now: datetime | None = None
    """The demo's logical clock reading, or ``None`` outside the demo.

    When present the adapter's demo mapping applies -- ``actual_now + max(10 minutes,
    logical_due - logical_now)`` -- and both values are audited. A **real** one-time schedule is
    still created: the demo does not fake the resource.
    """

    @property
    def scope(self) -> CaseScope:
        return CaseScope(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.commitment.case_id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateDueScheduleResult:
    status: CommitmentScheduleStatus
    schedule_name: str
    attempts: int
    failure_code: str | None


@dataclass(slots=True)
class CreateDueSchedule:
    """Ask the scheduler once, reconcile a lost answer by name, and record the outcome.

    Two clocks, and they answer different questions (P1/P2-2, Phase 11 batch 4 repair).
    ``clock`` is the authoritative **logical** clock -- the same one the commitment, the
    projection, and every audit event in this command are stamped with. ``wall_clock`` answers
    a completely different question: "what real instant should EventBridge Scheduler fire at?"
    A real one-time schedule is a wall-clock resource regardless of what the demo's logical
    clock reads, and confusing the two is exactly the defect this split repairs -- a worker
    running on a logical clock that reads 2030 must not compute ``actual_now`` from that clock,
    or the schedule it asks EventBridge Scheduler to create lands in 2030 real time and never
    fires. ``wall_clock`` is never used for anything the commitment, the projection, or an
    audit row remembers; it exists for exactly one arithmetic step, below.
    """

    shareable: ShareableRepositoryPort
    audit: AuditRepositoryPort
    unit_of_work: UnitOfWork
    scheduler: DeadlineSchedulerPort
    clock: Clock
    wall_clock: Clock
    ids: IdGenerator
    scheduler_environment: str

    async def execute(self, command: CreateDueScheduleCommand) -> CreateDueScheduleResult:
        commitment = command.commitment
        projection = await self.shareable.load_commitment_schedule(
            command.scope, commitment.commitment_id
        )
        if projection is None:
            # The projection is created by the commitment transaction, so its absence means the
            # commitment this command names was never applied.
            raise IntegrityError("COMMITMENT_SCHEDULE")
        if projection.status is CommitmentScheduleStatus.CREATED:
            return CreateDueScheduleResult(
                status=projection.status,
                schedule_name=projection.schedule_name,
                attempts=projection.attempts,
                failure_code=None,
            )

        # ``actual_now``: real wall-clock time, and only ever wall-clock time. This is the one
        # place ``wall_clock`` is read, and the one place it may be -- everywhere else in this
        # command, ``self.clock`` (the logical clock) is authoritative (P1/P2-2).
        actual_now = self.wall_clock.now()
        at_utc = (
            commitment.due_at
            if command.logical_now is None
            else demo_schedule_instant(
                actual_now=actual_now,
                logical_now=command.logical_now,
                logical_due_at=commitment.due_at,
            )
        )
        request = due_schedule_request(
            environment=self.scheduler_environment,
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=commitment.case_id,
            commitment_id=commitment.commitment_id,
            generation=commitment.schedule_generation,
            due_at=commitment.due_at,
            at_utc=at_utc,
        )
        if request.schedule_name != commitment.scheduler_name:  # pragma: no cover
            # The name on the row and the name derived now must be the same value, or a retry
            # would create a second schedule for one commitment.
            raise IntegrityError("COMMITMENT_SCHEDULE")

        outcome = await self.scheduler.create_due_schedule(request)
        if isinstance(outcome, ScheduleCreateFailed):
            # A lost response is indistinguishable from a failure here, so the exact name is
            # asked about before anything is recorded -- never a second differently named
            # schedule.
            described = await self.scheduler.describe_schedule(request.schedule_name)
            if described is None:
                return await self._record_failure(command, projection, outcome.reason_code)
        return await self._record_created(command, projection)

    async def _record_created(
        self, command: CreateDueScheduleCommand, projection: CommitmentScheduleProjection
    ) -> CreateDueScheduleResult:
        now = self.clock.now()
        moved = CommitmentScheduleProjection(
            namespace=projection.namespace,
            community_id=projection.community_id,
            case_id=projection.case_id,
            commitment_id=projection.commitment_id,
            status=CommitmentScheduleStatus.CREATED,
            schedule_name=projection.schedule_name,
            generation=projection.generation,
            attempts=projection.attempts + 1,
            version=projection.version + 1,
            created_at=projection.created_at,
            updated_at=now,
            last_error_code=None,
        )
        operations = (
            self.shareable.stage_update_commitment_schedule(
                command.scope, moved, expected_version=projection.version
            ),
            self.audit.stage_append_case_event(
                command.scope,
                self._audit_event(
                    command,
                    now=now,
                    event_type=observability.EventName.SCHEDULE_CREATED,
                    reason_codes=(SCHEDULE_CREATED_REASON_CODE,),
                    decision=AuditDecision.ALLOW,
                    attempts=moved.attempts,
                ),
            ),
        )
        if len(operations) != SCHEDULE_PARTICIPANTS:  # pragma: no cover - arithmetic guard
            raise IntegrityError("COMMITMENT_SCHEDULE")
        await self.unit_of_work.commit(
            TransactionPlan(name=SCHEDULE_TRANSACTION, operations=operations, audit_required=True)
        )
        observability.schedule_created(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.commitment.case_id,
            correlation_id=command.correlation_id,
            commitment_id=command.commitment.commitment_id.value,
            generation=moved.generation,
        )
        return CreateDueScheduleResult(
            status=moved.status,
            schedule_name=moved.schedule_name,
            attempts=moved.attempts,
            failure_code=None,
        )

    async def _record_failure(
        self,
        command: CreateDueScheduleCommand,
        projection: CommitmentScheduleProjection,
        reason_code: ScheduleFailureCode,
    ) -> CreateDueScheduleResult:
        now = self.clock.now()
        moved = CommitmentScheduleProjection(
            namespace=projection.namespace,
            community_id=projection.community_id,
            case_id=projection.case_id,
            commitment_id=projection.commitment_id,
            status=CommitmentScheduleStatus.PENDING_SCHEDULE,
            schedule_name=projection.schedule_name,
            generation=projection.generation,
            attempts=projection.attempts + 1,
            version=projection.version + 1,
            created_at=projection.created_at,
            updated_at=now,
            last_error_code=reason_code.value,
        )
        operations = (
            self.shareable.stage_update_commitment_schedule(
                command.scope, moved, expected_version=projection.version
            ),
            self.audit.stage_append_case_event(
                command.scope,
                self._audit_event(
                    command,
                    now=now,
                    event_type=observability.EventName.SCHEDULE_FAILED,
                    reason_codes=(reason_code.value,),
                    decision=AuditDecision.DENY,
                    attempts=moved.attempts,
                ),
            ),
        )
        await self.unit_of_work.commit(
            TransactionPlan(
                name=SCHEDULE_FAILURE_TRANSACTION, operations=operations, audit_required=True
            )
        )
        observability.schedule_failed(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.commitment.case_id,
            correlation_id=command.correlation_id,
            commitment_id=command.commitment.commitment_id.value,
            attempts=moved.attempts,
            reason_codes=(reason_code.value,),
        )
        return CreateDueScheduleResult(
            status=moved.status,
            schedule_name=moved.schedule_name,
            attempts=moved.attempts,
            failure_code=reason_code.value,
        )

    def _audit_event(
        self,
        command: CreateDueScheduleCommand,
        *,
        now: datetime,
        event_type: str,
        reason_codes: tuple[str, ...],
        decision: AuditDecision,
        attempts: int,
    ) -> AuditEvent:
        return AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.commitment.case_id,
            actor_type=ActorType.SYSTEM,
            actor_id_hash=command.actor_id_hash,
            event_type=event_type,
            occurred_at=now,
            correlation_id=command.correlation_id,
            causation_id=None,
            idempotency_key_hash=None,
            entity_refs=(
                AuditEntityRef(
                    entity_type="COMMITMENT",
                    entity_id=command.commitment.commitment_id.value,
                    version=command.commitment.version,
                ),
            ),
            decision=decision,
            reason_codes=reason_codes,
            safe_details=AuditDetails(count=attempts, rule_id=None),
            input_hash=None,
            output_hash=None,
        )


__all__ = [
    "SCHEDULE_CREATED_REASON_CODE",
    "SCHEDULE_FAILURE_TRANSACTION",
    "SCHEDULE_PARTICIPANTS",
    "SCHEDULE_TRANSACTION",
    "CreateDueSchedule",
    "CreateDueScheduleCommand",
    "CreateDueScheduleResult",
]
