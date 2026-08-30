"""Closed, content-safe domain errors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DomainErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    STATE_TRANSITION_ERROR = "STATE_TRANSITION_ERROR"
    INTEGRITY_ERROR = "INTEGRITY_ERROR"
    STALE_VERSION = "STALE_VERSION"


@dataclass(frozen=True, slots=True)
class DomainError(Exception):
    """Base exception containing only safe code and opaque references."""

    code: DomainErrorCode
    entity_ref: str | None = None

    def __str__(self) -> str:
        if self.entity_ref is None:
            return self.code.value
        return f"{self.code.value}: {self.entity_ref}"


class StateTransitionError(DomainError):
    """An illegal edge or unsatisfied deterministic transition guard."""

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(DomainErrorCode.STATE_TRANSITION_ERROR, entity_ref)


class IntegrityError(DomainError):
    """Stored ownership, hash, or schema data violates a hard invariant."""

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(DomainErrorCode.INTEGRITY_ERROR, entity_ref)
