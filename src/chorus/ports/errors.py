"""Closed persistence error taxonomy.

Every adapter translates its SDK exceptions into exactly one of these types at the
infrastructure boundary. Errors carry a safe enum code and an opaque entity reference only;
they never carry stored values, request payloads, credentials, or SDK response text.
"""

from __future__ import annotations

from enum import StrEnum


class PersistenceErrorCode(StrEnum):
    """Safe codes mapped by the API layer to the frozen error table."""

    NOT_FOUND = "NOT_FOUND"
    PERSISTENCE_CONFLICT = "PERSISTENCE_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    CROSS_CASE_VIOLATION = "CROSS_CASE_VIOLATION"
    TRANSACTION_LIMIT_EXCEEDED = "TRANSACTION_LIMIT_EXCEEDED"
    UNAUDITED_MUTATION = "UNAUDITED_MUTATION"
    MODEL_LIMIT_EXCEEDED = "MODEL_LIMIT_EXCEEDED"
    INVALID_CURSOR = "INVALID_CURSOR"
    DEPENDENCY_REJECTED = "DEPENDENCY_REJECTED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    UNKNOWN_TRANSACTION_OUTCOME = "UNKNOWN_TRANSACTION_OUTCOME"


class PersistenceError(Exception):
    """Base persistence failure carrying only a safe code and opaque reference.

    This is a plain exception rather than a dataclass on purpose. An ordinary ``raise`` of a
    frozen dataclass exception does work: CPython sets ``__traceback__`` and ``__cause__``
    through the C API, which never consults ``__setattr__``. The part of the exception
    protocol written in Python does not. ``contextlib._GeneratorContextManager`` re-raises by
    assigning ``exc.__traceback__``, which a frozen dataclass refuses, so an error crossing
    any ``@contextmanager`` boundary would be replaced by an unrelated failure at exactly the
    point it was meant to be reported.
    """

    __slots__ = ("code", "entity_ref", "retryable")

    code: PersistenceErrorCode
    entity_ref: str | None
    retryable: bool

    def __init__(
        self,
        code: PersistenceErrorCode,
        entity_ref: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(code.value if entity_ref is None else f"{code.value}: {entity_ref}")
        self.code = code
        self.entity_ref = entity_ref
        self.retryable = retryable

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code.value!r}, "
            f"entity_ref={self.entity_ref!r}, retryable={self.retryable!r})"
        )


class NotFoundError(PersistenceError):
    """A required record is absent; callers must not enumerate foreign resources."""

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(PersistenceErrorCode.NOT_FOUND, entity_ref, False)


class PersistenceConflictError(PersistenceError):
    """An optimistic version, create-only, or fence condition failed."""

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(PersistenceErrorCode.PERSISTENCE_CONFLICT, entity_ref, False)


class IdempotencyConflictError(PersistenceError):
    """The same idempotency key is already bound to a different request hash."""

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(PersistenceErrorCode.IDEMPOTENCY_CONFLICT, entity_ref, False)


class CrossCaseViolationError(PersistenceError):
    """A loaded record does not belong to the requested namespace/community/case."""

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(PersistenceErrorCode.CROSS_CASE_VIOLATION, entity_ref, False)


class TransactionLimitExceededError(PersistenceError):
    """A transaction plan exceeds the DynamoDB V1 operation bound; rejected locally."""

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(PersistenceErrorCode.TRANSACTION_LIMIT_EXCEEDED, entity_ref, False)


class UnauditedMutationError(PersistenceError):
    """An audit-required plan does not contain its atomic append-only audit write."""

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(PersistenceErrorCode.UNAUDITED_MUTATION, entity_ref, False)


class ModelLimitExceededError(PersistenceError):
    """A V1 per-case model bound would be exceeded by this write."""

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(PersistenceErrorCode.MODEL_LIMIT_EXCEEDED, entity_ref, False)


class InvalidCursorError(PersistenceError):
    """A pagination cursor is malformed, unsigned, tampered with, or foreign."""

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(PersistenceErrorCode.INVALID_CURSOR, entity_ref, False)


class ExternalDependencyError(PersistenceError):
    """A definite storage dependency failure with no ambiguous side effect."""

    def __init__(
        self,
        entity_ref: str | None = None,
        *,
        code: PersistenceErrorCode = PersistenceErrorCode.DEPENDENCY_UNAVAILABLE,
        retryable: bool = True,
    ) -> None:
        super().__init__(code, entity_ref, retryable)


class UnknownTransactionOutcomeError(ExternalDependencyError):
    """The transaction may or may not have committed; never retry without proof."""

    def __init__(self, entity_ref: str | None = None) -> None:
        super().__init__(
            entity_ref,
            code=PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME,
            retryable=False,
        )
