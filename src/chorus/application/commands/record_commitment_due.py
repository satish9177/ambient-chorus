"""The deadline watcher: an unsigned event, a re-checked commitment, and one edge.

**The event is not signed and it is not trusted.** It carries no MAC and needs none, because the
watcher grants it exactly one power: naming which commitment to load. Every other field is
re-verified against the strongly loaded row before anything moves
([ADR-028](../../../../docs/adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) § 2).

Adding a second HMAC boundary here would be theatre, and the contrast with
[ADR-026](../../../../docs/adr/ADR-026-inbound-reply-trust-and-correlation.md) is the argument.
There, the attester exists because the payload carries a value **nothing durable can confirm** --
a stranger's authorship. Here every field is a value the commitment row already holds, so
re-reading it is strictly stronger than verifying a signature over it. What keeps a stranger from
invoking the watcher is IAM: only the scheduler execution role and the demo-clock route may
invoke the function, and a caller who could do that could already do worse.

The due check, in order (§ 3)
------------------------------
1. strong-load the commitment; not found is a success no-op, ``WATCHER_UNKNOWN_COMMITMENT``;
2. verify namespace, case, generation, due event ID, and due time; any mismatch is a success
   no-op, ``WATCHER_STALE_GENERATION`` -- **the commitment is not changed and the schedule is not
   recreated**;
3. ``status != PENDING`` is a success no-op, ``WATCHER_REPLAY``. This one branch covers duplicate
   scheduler delivery, late delivery, the demo clock racing the real schedule, and a commitment
   a human already satisfied;
4. ``clock.now() < due_at`` is a success no-op, ``WATCHER_EARLY``. An early firing is not an
   error and is **not rescheduled**: the real one-time schedule fires again at its own time, and
   a rescheduling watcher is a watcher that can be made to schedule;
5. otherwise transaction **D**.

Transaction D -- three participants, Shareable / Audit
-------------------------------------------------------
1. the guarded ``PENDING -> DUE`` update, conditioned on the exact ``version``,
   ``status == PENDING``, and ``due_event_id == event.event_id``;
2. the verification-request projection, create-only -- being create-only is what makes "exactly
   one verification request" a property rather than a hope;
3. the ``commitment.due`` audit event.

There is **no idempotency record**: the commitment row conditioned on its own ``due_event_id``
*is* the proof, and a fourth participant would be a second answer to a question already settled.

The watcher takes **no case edge** and writes no case row in either table.
``ACTIONED -> VERIFYING`` happened at creation. Its whole data-plane authority is the Shareable
``NS#n#CASE#k`` partition, which is what its IAM row says and what a test asserts over the
staged plan.

And it never marks a commitment ``MISSED``. Time passage is evidence about the clock, not about
the elevator -- the single most tempting shortcut in this phase, refused here as well as in the
state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from chorus.application import observability
from chorus.domain.entities import (
    ActorType,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    Commitment,
    CommitmentStatus,
)
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import CommunityId, IdGenerator, Sha256Digest
from chorus.domain.state import transition_commitment
from chorus.ports.clock import Clock
from chorus.ports.errors import NotFoundError
from chorus.ports.records import VerificationRequest
from chorus.ports.repositories import AuditRepositoryPort, ShareableRepositoryPort
from chorus.ports.scheduler import CommitmentDueEvent
from chorus.ports.scopes import CaseScope
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork

DUE_TRANSACTION = "record-commitment-due"

DUE_PARTICIPANTS = 3
"""The guarded ``PENDING -> DUE`` update, the create-only request, and the audit event."""

COMMITMENT_DUE_REASON_CODE = "COMMITMENT_DUE"

TRIGGER_SCHEDULE = "SCHEDULE"
TRIGGER_DEMO_CLOCK = "DEMO_CLOCK"
"""Which path invoked the watcher, recorded as an audit field and never as an authority.

The demo route invokes the same function with the same event; it does not mutate a commitment,
and it cannot: the demo route holds no repository write path to ``COMMITMENT#``.
"""


class WatcherOutcome(StrEnum):
    """Why one watcher invocation ended the way it did. Every branch but ``DUE`` is a no-op."""

    DUE = "DUE"
    WATCHER_UNKNOWN_COMMITMENT = "WATCHER_UNKNOWN_COMMITMENT"
    WATCHER_STALE_GENERATION = "WATCHER_STALE_GENERATION"
    WATCHER_REPLAY = "WATCHER_REPLAY"
    WATCHER_EARLY = "WATCHER_EARLY"


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordCommitmentDueCommand:
    """One delivered due event, plus the community it belongs to and who invoked the watcher.

    ``community_id`` is deployment context rather than an event field: the event names a
    namespace and a case, and the watcher reads a Shareable case partition, so a community a
    delivery could choose would be a scope a delivery could choose.
    """

    event: CommitmentDueEvent
    community_id: CommunityId
    actor_id_hash: Sha256Digest
    correlation_id: UUID
    trigger: str = TRIGGER_SCHEDULE


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordCommitmentDueResult:
    outcome: WatcherOutcome
    commitment_status: CommitmentStatus | None
    commitment_version: int | None

    @property
    def changed(self) -> bool:
        return self.outcome is WatcherOutcome.DUE


@dataclass(slots=True)
class RecordCommitmentDue:
    """Move one commitment ``PENDING -> DUE``, or change nothing and say why."""

    shareable: ShareableRepositoryPort
    audit: AuditRepositoryPort
    unit_of_work: UnitOfWork
    clock: Clock
    ids: IdGenerator

    async def execute(self, command: RecordCommitmentDueCommand) -> RecordCommitmentDueResult:
        event = command.event
        scope = CaseScope(
            namespace=event.namespace,
            community_id=command.community_id,
            case_id=event.case_id,
        )
        try:
            commitment = await self.shareable.load_commitment(scope, event.commitment_id)
        except NotFoundError:
            return self._no_op(command, WatcherOutcome.WATCHER_UNKNOWN_COMMITMENT, None)

        if not self._agrees(commitment, event):
            return self._no_op(command, WatcherOutcome.WATCHER_STALE_GENERATION, commitment)
        if commitment.status is not CommitmentStatus.PENDING:
            return self._no_op(command, WatcherOutcome.WATCHER_REPLAY, commitment)
        if self.clock.now() < commitment.due_at:
            return self._no_op(command, WatcherOutcome.WATCHER_EARLY, commitment)
        return await self._mark_due(command, scope, commitment)

    @staticmethod
    def _agrees(commitment: Commitment, event: CommitmentDueEvent) -> bool:
        """Every field the event restates, re-verified against the row that actually holds it."""

        return (
            commitment.case_id == event.case_id
            and commitment.schedule_generation == event.expected_generation
            and commitment.due_event_id == event.event_id
            and commitment.due_at == event.logical_due_at
        )

    async def _mark_due(
        self,
        command: RecordCommitmentDueCommand,
        scope: CaseScope,
        commitment: Commitment,
    ) -> RecordCommitmentDueResult:
        now = self.clock.now()
        # ``actor_is_human`` is deliberately absent: ``PENDING -> DUE`` is the state machine's
        # one system-actor edge, and every other outcome is a person's.
        due = transition_commitment(
            commitment,
            CommitmentStatus.DUE,
            expected_version=commitment.version,
            now=now,
        )
        operations = (
            self.shareable.stage_update_commitment(
                scope,
                due,
                expected_version=commitment.version,
                expected_status=CommitmentStatus.PENDING,
                expected_due_event_id=command.event.event_id,
            ),
            self.shareable.stage_create_verification_request(
                scope,
                VerificationRequest(
                    namespace=scope.namespace,
                    community_id=scope.community_id,
                    case_id=scope.case_id,
                    commitment_id=commitment.commitment_id,
                    generation=commitment.schedule_generation,
                    due_event_id=commitment.due_event_id,
                    requested_at=now,
                ),
            ),
            self.audit.stage_append_case_event(scope, self._audit_event(command, due, now=now)),
        )
        if len(operations) != DUE_PARTICIPANTS:  # pragma: no cover - arithmetic guard
            raise IntegrityError("COMMITMENT")
        await self.unit_of_work.commit(
            TransactionPlan(
                name=DUE_TRANSACTION,
                operations=operations,
                audit_required=True,
                # No commit proof. The commitment row conditioned on its own due event is the
                # proof, so an ambiguous outcome is settled by reloading the row rather than by
                # a record whose only job would be to say the same thing.
                commit_proof=None,
            )
        )
        observability.commitment_due(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            correlation_id=command.correlation_id,
            commitment_id=commitment.commitment_id.value,
            generation=commitment.schedule_generation,
        )
        return RecordCommitmentDueResult(
            outcome=WatcherOutcome.DUE,
            commitment_status=due.status,
            commitment_version=due.version,
        )

    def _no_op(
        self,
        command: RecordCommitmentDueCommand,
        outcome: WatcherOutcome,
        commitment: Commitment | None,
    ) -> RecordCommitmentDueResult:
        """Success, and nothing changed. Nothing is rescheduled and no row is written."""

        observability.commitment_replayed(
            namespace=command.event.namespace,
            community_id=command.community_id,
            case_id=command.event.case_id,
            correlation_id=command.correlation_id,
            commitment_id=command.event.commitment_id.value,
            reason_codes=(outcome.value,),
            trigger=command.trigger,
        )
        return RecordCommitmentDueResult(
            outcome=outcome,
            commitment_status=None if commitment is None else commitment.status,
            commitment_version=None if commitment is None else commitment.version,
        )

    def _audit_event(
        self, command: RecordCommitmentDueCommand, commitment: Commitment, *, now: datetime
    ) -> AuditEvent:
        return AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=command.event.namespace,
            community_id=command.community_id,
            case_id=command.event.case_id,
            actor_type=ActorType.AWS_SERVICE,
            actor_id_hash=command.actor_id_hash,
            event_type=observability.EventName.COMMITMENT_DUE,
            occurred_at=now,
            correlation_id=command.correlation_id,
            causation_id=command.event.event_id,
            idempotency_key_hash=None,
            entity_refs=(
                AuditEntityRef(
                    entity_type="COMMITMENT",
                    entity_id=commitment.commitment_id.value,
                    version=commitment.version,
                ),
            ),
            decision=AuditDecision.ALLOW,
            reason_codes=(COMMITMENT_DUE_REASON_CODE, command.trigger),
            safe_details=AuditDetails(count=commitment.schedule_generation, rule_id=None),
            input_hash=None,
            output_hash=None,
        )


__all__ = [
    "COMMITMENT_DUE_REASON_CODE",
    "DUE_PARTICIPANTS",
    "DUE_TRANSACTION",
    "TRIGGER_DEMO_CLOCK",
    "TRIGGER_SCHEDULE",
    "RecordCommitmentDue",
    "RecordCommitmentDueCommand",
    "RecordCommitmentDueResult",
    "WatcherOutcome",
]
