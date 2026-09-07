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

Durability, stated plainly
---------------------------
ADR-028 § 5 places the logical clock in the **demo manifest partition**. That partition is in
the frozen persistence mapping and nothing creates it yet -- the demo manifest and its reset lock
arrive with the demo deployment. This implementation is therefore process-local: it is the same
object the watcher's ``Clock`` slot takes, with the same monotonic rule, and what it is missing
is the durable row. A restart resets it to its seed, which for a single-presenter demo is a
visible inconvenience rather than a correctness problem -- and the watcher's own step-4
comparison is against the *commitment row*, so a reset clock makes the watcher refuse an early
firing, never accept a wrong one.
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

    def advance(self, delta: timedelta) -> datetime:
        """Move time forward by a non-negative delta, and record that it happened.

        A negative or zero delta is refused rather than ignored: "advance the clock by nothing"
        is a request that reads as successful and changes nothing, and a presenter watching a
        deadline that did not arrive deserves the error instead.
        """

        if delta <= timedelta(0):
            raise ValueError("the demo clock only moves forward")
        self.instant = self.instant + delta
        self.advances.append(delta)
        return self.instant

    def advance_to(self, instant: datetime) -> datetime:
        """Move time forward to an exact instant, refusing anything at or before now."""

        require_utc(instant)
        return self.advance(instant - self.instant)


__all__ = ["LogicalDemoClock"]
