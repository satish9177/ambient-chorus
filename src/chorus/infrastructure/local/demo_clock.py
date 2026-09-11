"""The demo's one logical clock: monotonic, advanced only by the demo route.

The deployment supplies **exactly one** :class:`~chorus.ports.clock.Clock` to the watcher
([ADR-028](../../../../docs/adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) § 5). In
``demo`` it is this one; everywhere else it is :class:`~chorus.domain.time.SystemClock`. The
watcher never holds two and never chooses between them, so the early-firing comparison means the
same thing on both paths -- which is the whole reason this is a clock rather than a flag the
watcher reads.

Monotonic, and that is enforced rather than assumed. A presenter can move time forward; nothing
can move it back, because a clock that went backwards would make an already-``DUE`` commitment
look early and a fired schedule look unfired.

**It is a real authority in the demo namespace, and it is bounded.** Advancing time reaches
``PENDING -> DUE`` and stops: both outcomes still require a contributor, so a presenter can make
a deadline arrive and cannot make a promise kept.

Durability, and which environments this one is for
--------------------------------------------------
This clock is **process-local**, and that is now a statement about where it may be used rather
than a limitation it apologises for.
[ADR-029](../../../../docs/adr/ADR-029-deployed-demo-clock-authority.md) supersedes ADR-028 § 5
on where the deployed clock lives: it is a durable Shareable row at the exact literal partition
``NS#DEMO#CLOCK``, read strongly and moved by one guarded compare-and-swap
(:class:`chorus.infrastructure.dynamodb.demo_clock.DynamoDbDemoClockStore`). In a deployed
system the watcher is a separate Lambda from the API that advanced the clock, so a process-local
clock there is not merely non-durable -- it is *a different clock*, in a different process.

So this remains the ``test``/``development`` adapter and the local demo's, where one process
holds the whole system and a restart resetting the clock to its seed is a visible inconvenience
rather than a correctness problem. It is never constructed in a deployed ``demo`` composition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from chorus.domain.time import require_utc


@dataclass(slots=True)
class LogicalDemoClock:
    """A monotonic clock the demo route advances, and nothing else may move."""

    instant: datetime
    advances: list[timedelta] = field(default_factory=list)

    def __post_init__(self) -> None:
        require_utc(self.instant)

    def now(self) -> datetime:
        return self.instant

    async def advance(self, delta: timedelta) -> datetime:
        """Move time forward by a non-negative delta, and record that it happened.

        A negative or zero delta is refused rather than ignored: "advance the clock by nothing"
        is a request that reads as successful and changes nothing, and a presenter watching a
        deadline that did not arrive deserves the error instead.

        ``async`` although nothing here awaits, so this and the deployed
        :class:`~chorus.infrastructure.persistent_clock.PersistentDemoClock` satisfy one
        :class:`~chorus.ports.demo_clock.DemoClockPort`. The deployed advance is a strongly
        consistent read followed by a conditional write, and a route that had to know which
        clock it was holding would be a route with a branch on the deployment.
        """

        if delta <= timedelta(0):
            raise ValueError("the demo clock only moves forward")
        self.instant = self.instant + delta
        self.advances.append(delta)
        return self.instant

    async def advance_to(self, instant: datetime) -> datetime:
        """Move time forward to an exact instant, refusing anything at or before now."""

        require_utc(instant)
        return await self.advance(instant - self.instant)


__all__ = ["LogicalDemoClock"]
