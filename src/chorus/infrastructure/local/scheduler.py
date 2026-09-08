"""The manual in-memory deadline scheduler: a real port, a real name, no AWS.

It implements :class:`~chorus.ports.scheduler.DeadlineSchedulerPort` exactly, so the application
code under test is byte-for-byte the code that runs against EventBridge Scheduler. What changes
is only who answers.

The interesting behaviour is the **create-if-absent** rule and the outcome queue. A repeated
create under the derived name returns :class:`ScheduleAlreadyExists`, which is what makes a
retry after a lost response a replay rather than a duplicate; and ``outcomes`` scripts failures
no live service would produce on demand -- a rejection, an unavailability, and the ambiguous
answer that is reconciled by ``describe_schedule`` on the exact name.

``created`` is what a test asserts on: **exactly one schedule request under the deterministic
name and client token** is one of Phase 9's exit criteria, and a test that inferred it from a
durable projection could not tell one create from two under the same name.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from chorus.ports.scheduler import (
    DueScheduleRequest,
    ScheduleAlreadyExists,
    ScheduleCreated,
    ScheduleCreateFailed,
    ScheduleDescription,
    ScheduleOutcome,
)


@dataclass(slots=True)
class InMemoryDeadlineScheduler:
    """Record one-time schedules by name and answer with scripted or default outcomes."""

    schedules: dict[str, DueScheduleRequest] = field(default_factory=dict)
    created: list[DueScheduleRequest] = field(default_factory=list)
    """Every deliberate create request, in order. The number a test asserts on."""

    outcomes: deque[ScheduleOutcome] = field(default_factory=deque)
    """Scripted answers, consumed in order before the default rule applies."""

    describe_calls: list[str] = field(default_factory=list)
    store_on_failure: bool = False
    """Whether a scripted failure still records the schedule.

    ``True`` is the **lost response**: the schedule really exists and the caller was told
    nothing, which is the one scheduler outcome a caller cannot tell from a failure. It is what
    makes the reconciliation path -- ``describe_schedule`` on the exact name -- testable rather
    than merely written down.
    """

    async def create_due_schedule(self, request: DueScheduleRequest) -> ScheduleOutcome:
        self.created.append(request)
        if self.outcomes:
            outcome = self.outcomes.popleft()
            if isinstance(outcome, ScheduleCreateFailed) and self.store_on_failure:
                self.schedules.setdefault(request.schedule_name, request)
            return outcome
        existing = self.schedules.get(request.schedule_name)
        if existing is not None:
            # Under a derived name this is always a replay of the same request, which is
            # precisely why the port has no update verb for it to be anything else.
            return ScheduleAlreadyExists(schedule_name=request.schedule_name)
        self.schedules[request.schedule_name] = request
        return ScheduleCreated(schedule_name=request.schedule_name)

    async def describe_schedule(self, name: str) -> ScheduleDescription | None:
        self.describe_calls.append(name)
        request = self.schedules.get(name)
        if request is None:
            return None
        return ScheduleDescription(
            schedule_name=name,
            at_utc=request.at_utc,
            event_id=request.payload.event_id,
        )

    @property
    def create_count(self) -> int:
        """How many deliberate create requests were made, whatever their outcome."""

        return len(self.created)

    def reset(self) -> None:
        """Drop every recorded schedule and request log.

        Used only by the local demo reset, which erases the namespace these schedules belong
        to; V1 has one demo namespace, so clearing the lot is exact rather than approximate.
        """

        self.schedules.clear()
        self.created.clear()
        self.outcomes.clear()
        self.describe_calls.clear()


__all__ = ["InMemoryDeadlineScheduler"]
