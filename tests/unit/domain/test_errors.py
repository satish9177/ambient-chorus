"""The external contract of the domain error hierarchy.

``DomainError`` is an ordinary exception class rather than a frozen dataclass. An ordinary
``raise`` of a frozen dataclass exception does work -- CPython sets ``__traceback__`` and
``__cause__`` through the C API, which never consults ``__setattr__``. What does not work is
the part of the exception protocol written in Python: ``contextlib._GeneratorContextManager``
re-raises through ``exc.__traceback__ = traceback``, an ordinary attribute assignment that a
frozen dataclass refuses. The original error is then replaced by an unrelated failure at
precisely the boundary that was supposed to report it, which
``test_a_frozen_dataclass_error_breaks_the_generator_context_manager_protocol`` demonstrates
against a reconstruction of the old shape.

The fix is only safe if everything callers relied on still holds, so the surface is pinned
here rather than argued for: the two public attributes, the rendered text, the subclass codes,
and the promise that nothing but a safe code and an opaque reference is ever rendered.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from chorus.domain.errors import (
    DomainError,
    DomainErrorCode,
    IntegrityError,
    StateTransitionError,
)

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from collections.abc import Iterator


@dataclass(frozen=True, slots=True)
class _FrozenDataclassError(Exception):
    """The shape ``DomainError`` used to have, kept only so the regression stays proven."""

    code: DomainErrorCode
    entity_ref: str | None = None


@contextmanager
def _boundary() -> Iterator[None]:
    """The commonest Python-level exception boundary an application has."""

    yield


def test_the_base_error_exposes_its_code_and_reference() -> None:
    error = DomainError(DomainErrorCode.VALIDATION_ERROR, "CASE")

    assert error.code is DomainErrorCode.VALIDATION_ERROR
    assert error.entity_ref == "CASE"


def test_the_reference_is_optional() -> None:
    error = DomainError(DomainErrorCode.VALIDATION_ERROR)

    assert error.entity_ref is None
    assert str(error) == "VALIDATION_ERROR"


def test_the_rendered_text_is_the_code_and_the_reference() -> None:
    assert str(DomainError(DomainErrorCode.STALE_VERSION, "MANDATE")) == "STALE_VERSION: MANDATE"


def test_keyword_construction_still_works() -> None:
    """Callers written against the dataclass form used keyword arguments."""

    error = DomainError(code=DomainErrorCode.INTEGRITY_ERROR, entity_ref="FACT")

    assert (error.code, error.entity_ref) == (DomainErrorCode.INTEGRITY_ERROR, "FACT")


def test_a_state_transition_error_carries_its_own_code() -> None:
    error = StateTransitionError("EXECUTION")

    assert isinstance(error, DomainError)
    assert error.code is DomainErrorCode.STATE_TRANSITION_ERROR
    assert error.entity_ref == "EXECUTION"
    assert str(error) == "STATE_TRANSITION_ERROR: EXECUTION"


def test_an_integrity_error_carries_its_own_code() -> None:
    error = IntegrityError("VIEW")

    assert isinstance(error, DomainError)
    assert error.code is DomainErrorCode.INTEGRITY_ERROR
    assert error.entity_ref == "VIEW"
    assert str(error) == "INTEGRITY_ERROR: VIEW"


def test_a_subclass_reference_is_optional_too() -> None:
    assert str(IntegrityError()) == "INTEGRITY_ERROR"
    assert IntegrityError().entity_ref is None


def test_an_error_declares_exactly_the_two_public_attributes() -> None:
    """The declared surface is the contract callers may read.

    ``__slots__`` cannot forbid an instance dictionary here, because ``BaseException``
    supplies one to every exception. What it does do is state the surface, so the check is
    that the declaration matches the two attributes and nothing has been added beside them.
    """

    assert DomainError.__slots__ == ("code", "entity_ref")
    assert StateTransitionError.__dict__.get("__slots__") is None
    assert IntegrityError.__dict__.get("__slots__") is None


def test_nothing_but_the_code_and_reference_is_rendered() -> None:
    error = StateTransitionError("OPAQUE_REF")

    for rendered in (str(error), repr(error), str(error.args)):
        assert "OPAQUE_REF" in rendered
    assert set(error.args) == {"STATE_TRANSITION_ERROR: OPAQUE_REF"}


def test_an_error_survives_propagation_and_chaining() -> None:
    with pytest.raises(IntegrityError) as raised:
        try:
            raise ValueError("cause")
        except ValueError as cause:
            raise IntegrityError("FACT") from cause

    assert isinstance(raised.value.__cause__, ValueError)
    assert raised.value.__traceback__ is not None
    assert raised.value.code is DomainErrorCode.INTEGRITY_ERROR


def test_a_frozen_dataclass_error_breaks_the_generator_context_manager_protocol() -> None:
    """The regression that ordinary exception classes exist to avoid.

    A plain ``raise`` of the old shape works, so the reason for the change is not that every
    raise was broken. It is that ``contextlib`` re-raises by assigning ``__traceback__``
    through Python attribute assignment, which a frozen dataclass refuses -- and the caller
    then sees that refusal instead of the domain error.
    """

    with pytest.raises(_FrozenDataclassError):
        raise _FrozenDataclassError(DomainErrorCode.INTEGRITY_ERROR, "FACT")

    with pytest.raises(TypeError) as raised, _boundary():
        raise _FrozenDataclassError(DomainErrorCode.INTEGRITY_ERROR, "FACT")

    assert not isinstance(raised.value, _FrozenDataclassError)


def test_a_domain_error_survives_the_generator_context_manager_protocol() -> None:
    """The same path with the shipped class reports the error the caller actually raised."""

    with pytest.raises(IntegrityError) as raised, _boundary():
        raise IntegrityError("FACT")

    assert raised.value.code is DomainErrorCode.INTEGRITY_ERROR
    assert raised.value.entity_ref == "FACT"
    assert raised.value.__traceback__ is not None


def test_a_persistence_error_survives_the_same_protocol_path() -> None:
    """The ports taxonomy is an ordinary exception hierarchy for exactly the same reason."""

    from chorus.ports.errors import NotFoundError, PersistenceErrorCode

    with pytest.raises(NotFoundError) as raised, _boundary():
        raise NotFoundError("FACT")

    assert raised.value.code is PersistenceErrorCode.NOT_FOUND
    assert raised.value.retryable is False
