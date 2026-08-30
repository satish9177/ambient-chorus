"""UTC normalization and injected clocks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


def require_utc(value: datetime) -> datetime:
    """Validate an aware UTC instant without silently changing its meaning."""

    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("datetime must be timezone-aware UTC")
    return value.astimezone(UTC)


def format_utc(value: datetime) -> str:
    """Serialize UTC with exactly six fractional digits and a Z suffix."""

    return require_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    """Parse only the canonical RFC 3339 representation."""

    if len(value) < 8 or not value.endswith("Z") or "." not in value:
        raise ValueError("datetime is not canonical RFC 3339 UTC")
    fraction = value.rsplit(".", maxsplit=1)[1][:-1]
    if len(fraction) != 6 or not fraction.isdigit():
        raise ValueError("datetime must have exactly six fractional digits")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if format_utc(parsed) != value:
        raise ValueError("datetime is not canonical RFC 3339 UTC")
    return parsed


class Clock(Protocol):
    """Command-scoped time source."""

    def now(self) -> datetime:
        """Return an aware UTC instant."""


@dataclass(frozen=True, slots=True)
class FixedClock:
    """Deterministic test/demo clock."""

    instant: datetime

    def __post_init__(self) -> None:
        require_utc(self.instant)

    def now(self) -> datetime:
        return self.instant


@dataclass(frozen=True, slots=True)
class SystemClock:
    """Production clock implementation used only through injection."""

    def now(self) -> datetime:
        return datetime.now(UTC)
