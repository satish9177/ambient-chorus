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


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def epoch_micros(value: datetime) -> int:
    """Return exact whole microseconds since the epoch.

    This is a storage-condition helper, not a serialization format: canonical persisted
    instants remain the RFC 3339 strings produced by :func:`format_utc`. It exists so an
    authorization deadline can be compared by a numeric storage condition without the
    second-truncation that ``int(datetime.timestamp())`` performs, which would otherwise let
    a comparison treat an instant as reached up to a second before it actually is. Integer
    arithmetic is used throughout so no float rounding can move the boundary.
    """

    delta = require_utc(value) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def epoch_seconds_ceiling(value: datetime) -> int:
    """Return epoch seconds rounded up, for TTL cleanup fields only.

    Rounding up keeps a DynamoDB TTL from ever naming an instant earlier than the value it
    represents. TTL is cleanup and never authorization, so this is a tidiness guarantee
    rather than a security one.
    """

    micros = epoch_micros(value)
    return -(-micros // 1_000_000)


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
