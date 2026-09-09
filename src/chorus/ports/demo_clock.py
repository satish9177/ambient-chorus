"""The deployed demo clock as a port: one durable row, read strongly, moved forward once.

[ADR-029](../../../docs/adr/ADR-029-deployed-demo-clock-authority.md) freezes a single
authoritative logical clock for the deployed demo, living at the exact literal Shareable
partition ``NS#DEMO#CLOCK``. This module is the boundary the rest of the system sees: a record,
a store that reads and compare-and-swaps it, and the three failures a reader is allowed to have.

Why a store port and not a ``Clock``
------------------------------------
:class:`chorus.domain.time.Clock` is synchronous by design -- a use case asks for the instant it
is acting at, mid-transaction, and cannot await. A durable clock cannot answer synchronously, so
the two are deliberately different things: the store is read **once per invocation** (or once
per request), and the instant it returns is then supplied to the object graph as an ordinary
``Clock``. One reading per unit of work is also what makes a run reproducible -- every timestamp
an invocation writes names the same logical instant rather than a moving one.

There is deliberately **no reset and no reseed on this port**. Restoring the seed instant is the
one legitimate backward transition and it belongs to the dedicated reset principal behind the
``DEMO_RESET_LOCK`` (ADR-029 SS 3); a normal advance path that could also move time backwards
would make the forward rule a convention rather than a condition. A test asserts the absence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from chorus.domain.time import require_utc


class DemoClockError(Exception):
    """The authoritative logical clock could not be read or could not be moved.

    Its own family, and never a fallback. ADR-029 SS 4 names the three things a failed clock
    read must never do -- reach for a process-local clock, reach for ``SystemClock``, or trust
    the scheduler event's timestamp -- and an exception is what makes "fails closed" the only
    reachable behaviour rather than the intended one.
    """


class DemoClockUnavailableError(DemoClockError):
    """The clock row is missing, malformed, or the store could not be reached.

    One type for all three because the caller's response is identical and must be: refuse. A
    caller that could tell "no row" from "throttled" would be a caller with a reason to treat
    one of them as benign.
    """


class DemoClockConflictError(DemoClockError):
    """Another writer moved the clock first; this advance applied nothing.

    Deterministic and definite: the conditional write is evaluated by the store, so a losing
    writer knows it lost and knows nothing was written. It never becomes a blind overwrite and
    it is never retried inside the adapter -- how much of its own window a caller has already
    spent is the caller's knowledge, not the store's.
    """


class DemoClockNotAdvancedError(DemoClockError):
    """The requested reading is at or before the stored one.

    Refused locally *as well as* by the stored condition. The store's condition is the
    authority -- it is what holds every concurrent writer -- and this is the error a presenter
    actually sees, because "advance to now" reads as successful and changes nothing.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoClockRecord:
    """The durable clock row, field for field as ADR-029 SS 1 freezes it.

    ``reset_generation`` is the fence that makes ``version`` safe. Reset begins a fresh version
    sequence, so a version alone necessarily revisits low numbers and an advance in flight from
    a previous demo run could carry one that is live again; the generation never repeats, so an
    advance that names the old one fails whatever the version happens to be.

    ``seed_instant`` is carried rather than derived so a reset has one frozen value to restore
    and this port has no opinion about what that value should be.
    """

    logical_time: datetime
    version: int
    reset_generation: int
    seed_instant: datetime
    advance_count: int

    def __post_init__(self) -> None:
        require_utc(self.logical_time)
        require_utc(self.seed_instant)
        if self.version < 1:
            raise ValueError("a demo clock record carries a positive version")
        if self.reset_generation < 1:
            raise ValueError("a demo clock record carries a positive reset generation")
        if self.advance_count < 0:
            raise ValueError("a demo clock record cannot have advanced a negative number of times")
        if self.logical_time < self.seed_instant:
            raise ValueError("a demo clock cannot read earlier than its own seed instant")


class DemoClockStorePort(Protocol):
    """Read the one authoritative clock row, and move it forward under its own fences."""

    async def read(self) -> DemoClockRecord:
        """Return the current record from a **strongly consistent** read, or raise.

        Never returns ``None`` and never returns a default. An eventually consistent read of the
        authority a deadline is judged against is a deadline judged against a guess (ADR-029
        SS 4), and a defaulted record is a fabricated one.
        """

    async def advance(self, expected: DemoClockRecord, *, to: datetime) -> DemoClockRecord:
        """Move the clock to ``to``, conditioned on ``expected``, and return what is stored now.

        The write applies only when the stored row still carries ``expected.version`` and
        ``expected.reset_generation`` **and** its logical time is strictly earlier than ``to``.
        All three are conditions the store evaluates, so a concurrent advance loses
        deterministically instead of overwriting.
        """


class DemoClockPort(Protocol):
    """What the demo-clock route holds: move logical time forward by a bounded delta."""

    async def advance(self, delta: timedelta) -> datetime:
        """Advance by a strictly positive delta and return the new logical reading."""


__all__ = [
    "DemoClockConflictError",
    "DemoClockError",
    "DemoClockNotAdvancedError",
    "DemoClockPort",
    "DemoClockRecord",
    "DemoClockStorePort",
    "DemoClockUnavailableError",
]
