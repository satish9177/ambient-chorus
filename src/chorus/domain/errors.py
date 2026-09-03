"""Closed, content-safe domain errors."""

from __future__ import annotations

from enum import StrEnum


class DomainErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    STATE_TRANSITION_ERROR = "STATE_TRANSITION_ERROR"
    INTEGRITY_ERROR = "INTEGRITY_ERROR"
    STALE_VERSION = "STALE_VERSION"


class DomainError(Exception):
    """Base exception containing only safe code and opaque references.

    This is a plain exception rather than a dataclass on purpose. An ordinary ``raise`` of a
    frozen dataclass exception does work: CPython sets ``__traceback__`` and ``__cause__``
    through the C API, which never consults ``__setattr__``. The part of the exception
    protocol written in Python does not. ``contextlib._GeneratorContextManager`` re-raises by
    assigning ``exc.__traceback__``, which a frozen dataclass refuses, so an error crossing
    any ``@contextmanager`` boundary would be replaced by an unrelated failure at exactly the
    point it was meant to be reported.
    """

    __slots__ = ("code", "entity_ref")

    code: DomainErrorCode
    entity_ref: str | None

    def __init__(self, code: DomainErrorCode, entity_ref: str | None = None) -> None:
        super().__init__(code.value if entity_ref is None else f"{code.value}: {entity_ref}")
        self.code = code
        self.entity_ref = entity_ref

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code.value!r}, entity_ref={self.entity_ref!r})"


class ValidationError(DomainError):
    """A command or boundary value violates a closed domain invariant.

    It carries an opaque reference and never the rejected value, so a malformed ambient
    message cannot smuggle its own text into an error string, a log line, or an API response.
    """

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(DomainErrorCode.VALIDATION_ERROR, entity_ref)


class StateTransitionError(DomainError):
    """An illegal edge or unsatisfied deterministic transition guard."""

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(DomainErrorCode.STATE_TRANSITION_ERROR, entity_ref)


class IntegrityError(DomainError):
    """Stored ownership, hash, or schema data violates a hard invariant."""

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(DomainErrorCode.INTEGRITY_ERROR, entity_ref)
