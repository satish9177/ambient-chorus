"""Clock port re-exported so adapters depend on ports rather than domain internals."""

from __future__ import annotations

from chorus.domain.time import Clock, FixedClock, SystemClock

__all__ = ["Clock", "FixedClock", "SystemClock"]
