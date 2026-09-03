"""Bounded pagination contracts.

Cursors are opaque, signed, and bound to one namespace and one access pattern. Callers may
not construct a cursor value; they receive one from a repository page and return it verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from chorus.ports.limits import MAX_PAGE_SIZE


class QueryBinding(StrEnum):
    """Closed set of paginated access patterns a cursor may be replayed against."""

    CORE_COMMUNITY_FEED = "CORE_COMMUNITY_FEED"
    CORE_COMMUNITY_FEED_SIGNALS = "CORE_COMMUNITY_FEED_SIGNALS"
    CORE_CASE_FACTS = "CORE_CASE_FACTS"
    CORE_CASE_REPORTS = "CORE_CASE_REPORTS"
    CORE_CASE_ASSESSMENTS = "CORE_CASE_ASSESSMENTS"
    CORE_CASE_MANDATE_POINTERS = "CORE_CASE_MANDATE_POINTERS"
    SHAREABLE_VIEW_HISTORY = "SHAREABLE_VIEW_HISTORY"
    SHAREABLE_ACTION_HISTORY = "SHAREABLE_ACTION_HISTORY"
    SHAREABLE_ACTION_LINEAGE = "SHAREABLE_ACTION_LINEAGE"
    SHAREABLE_CASE_COMMITMENTS = "SHAREABLE_CASE_COMMITMENTS"
    AUDIT_CASE_EVENTS = "AUDIT_CASE_EVENTS"
    AUDIT_NAMESPACE_EVENTS = "AUDIT_NAMESPACE_EVENTS"


@dataclass(frozen=True, slots=True)
class PageCursor:
    """Opaque signed continuation token; it carries no private user content."""

    value: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.value) <= 2048:
            raise ValueError("cursor length is invalid")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, kw_only=True)
class PageRequest:
    """A bounded page request; V1 never returns more than 100 items."""

    limit: int = MAX_PAGE_SIZE
    cursor: PageCursor | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_PAGE_SIZE:
            raise ValueError("page size must be between 1 and the frozen maximum")


@dataclass(frozen=True, slots=True, kw_only=True)
class Page[ItemT]:
    """One bounded result page plus the cursor required to continue it."""

    items: tuple[ItemT, ...]
    next_cursor: PageCursor | None
