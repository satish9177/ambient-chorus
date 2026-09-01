"""Error objects must survive being raised, chained, and re-raised.

Python assigns ``__traceback__`` on an exception while it propagates. An error type that
rejects that assignment fails precisely when something has already gone wrong, so this is
checked directly rather than assumed.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest

from chorus.domain.errors import DomainError, IntegrityError, StateTransitionError
from chorus.ports.errors import (
    CrossCaseViolationError,
    IdempotencyConflictError,
    InvalidCursorError,
    ModelLimitExceededError,
    NotFoundError,
    PersistenceConflictError,
    PersistenceError,
    PersistenceErrorCode,
    TransactionLimitExceededError,
    UnauditedMutationError,
    UnknownTransactionOutcomeError,
)

ERRORS: tuple[Exception, ...] = (
    NotFoundError("REF"),
    PersistenceConflictError("REF"),
    IdempotencyConflictError("REF"),
    CrossCaseViolationError("REF"),
    TransactionLimitExceededError("REF"),
    UnauditedMutationError("REF"),
    ModelLimitExceededError("REF"),
    InvalidCursorError("REF"),
    UnknownTransactionOutcomeError("REF"),
    IntegrityError("REF"),
    StateTransitionError("REF"),
)
IDS = tuple(type(error).__name__ for error in ERRORS)


@contextlib.contextmanager
def reraising() -> Iterator[None]:
    """A generator context manager, which is the path that mutates ``__traceback__``."""

    yield


@pytest.mark.parametrize("error", ERRORS, ids=IDS)
def test_an_error_propagates_through_a_context_manager(error: Exception) -> None:
    with pytest.raises(type(error)), reraising():
        raise error


@pytest.mark.parametrize("error", ERRORS, ids=IDS)
def test_an_error_accepts_a_traceback_assignment(error: Exception) -> None:
    error.__traceback__ = None

    assert error.__traceback__ is None


@pytest.mark.parametrize("error", ERRORS, ids=IDS)
def test_an_error_can_be_chained(error: Exception) -> None:
    with pytest.raises(type(error)) as raised:
        try:
            raise ValueError("cause")
        except ValueError as cause:
            raise error from cause

    assert isinstance(raised.value.__cause__, ValueError)


@pytest.mark.parametrize("error", ERRORS, ids=IDS)
def test_an_error_renders_only_its_code_and_reference(error: Exception) -> None:
    assert str(error).endswith("REF")
    assert "REF" in repr(error)


def test_a_persistence_error_without_a_reference_renders_its_code_alone() -> None:
    assert str(NotFoundError()) == PersistenceErrorCode.NOT_FOUND.value
    assert NotFoundError().entity_ref is None


def test_the_taxonomy_is_closed_under_one_base() -> None:
    for error in ERRORS:
        assert isinstance(error, PersistenceError | DomainError)
    assert UnknownTransactionOutcomeError().retryable is False
    assert UnknownTransactionOutcomeError().code is (
        PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME
    )
