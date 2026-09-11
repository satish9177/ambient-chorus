"""Turn the durable clock row into the one ``Clock`` a unit of work is allowed to read.

Two objects, and the split between them is the whole design.

:class:`ScopedLogicalClock` is a :class:`~chorus.domain.time.Clock`. It answers synchronously,
because a use case asks for the instant it is acting at in the middle of a transaction and
cannot await -- and it answers with the reading bound to the current *unit of work*, which is
one Lambda invocation or one HTTP request. Every timestamp written by that unit therefore names
the same logical instant. Outside a bound scope it **raises**: an unbound clock is a clock with
no authority, and the alternative -- a default, a seed, or the wall clock -- is precisely what
ADR-029 SS 4 forbids.

:class:`PersistentDemoClock` is the demo route's whole relationship with logical time. It reads
the authoritative row, computes the target, and asks the store for the guarded forward
compare-and-swap. It decides nothing: the forward rule, the version fence, and the generation
fence are conditions the store evaluates.

The binding is a :class:`~contextvars.ContextVar` rather than an attribute, so two concurrent
requests in one process cannot read each other's instant, and a test can bind and unbind without
touching process-wide state that outlives it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from chorus.domain.time import require_utc
from chorus.ports.demo_clock import (
    DemoClockNotAdvancedError,
    DemoClockRecord,
    DemoClockStorePort,
    DemoClockUnavailableError,
)


@dataclass(frozen=True, slots=True)
class ScopedLogicalClock:
    """The authoritative logical reading of the current unit of work, or nothing at all."""

    variable: ContextVar[datetime | None] = field(
        default_factory=lambda: ContextVar("chorus_logical_now", default=None)
    )

    def now(self) -> datetime:
        instant = self.variable.get()
        if instant is None:
            # Never a fallback. A process-local clock, ``SystemClock``, or an event's timestamp
            # would each answer here, and ADR-029 SS 4 names all three by name as the things
            # that must not happen.
            raise DemoClockUnavailableError("no authoritative logical instant is bound")
        return instant

    @contextmanager
    def bound_to(self, instant: datetime) -> Iterator[datetime]:
        """Bind one reading for the duration of one unit of work, then restore what was there.

        Restoring the previous token rather than clearing matters in the API process, where a
        request runs inside a task that may itself be nested; a bare reset would leave an outer
        scope unbound rather than as it was.
        """

        require_utc(instant)
        token = self.variable.set(instant)
        try:
            yield instant
        finally:
            self.variable.reset(token)


@dataclass(frozen=True, slots=True)
class PersistentDemoClock:
    """Advance the deployed demo clock by a delta, and publish the new reading.

    ``scope`` is optional because the two callers differ: the API route advances *and* wants
    every later write in the same request stamped with the new reading, while a caller with no
    scoped clock simply wants the number. When present, the new instant is rebound so the
    request that moved time does not go on writing rows stamped before it.
    """

    store: DemoClockStorePort
    scope: ScopedLogicalClock | None = None

    async def read(self) -> DemoClockRecord:
        """The current authoritative record, strongly consistent, or a typed refusal."""

        return await self.store.read()

    async def advance(self, delta: timedelta) -> datetime:
        """Move logical time forward by ``delta`` and return the reading now stored.

        A non-positive delta is refused before any read: "advance the clock by nothing" is a
        request that reads as successful and changes nothing, and a presenter watching a
        deadline that did not arrive deserves the error instead.

        There is deliberately no retry on a lost compare-and-swap. The conflict is definite --
        nothing was written -- and whether to ask again is the caller's decision, because only
        the caller knows whether the reading it was going to act on is still the one it wants.
        """

        if delta <= timedelta(0):
            raise DemoClockNotAdvancedError("the demo clock only moves forward")
        current = await self.store.read()
        moved = await self.store.advance(current, to=current.logical_time + delta)
        if self.scope is not None:
            self.scope.variable.set(moved.logical_time)
        return moved.logical_time


__all__ = ["PersistentDemoClock", "ScopedLogicalClock"]
