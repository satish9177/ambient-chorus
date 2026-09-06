"""F07 -- ``requested_deadline`` must be strictly after ``view.generated_at``.

ADR-021 § 10 states the bound. The contract type can only prove the *shape* -- a timezone-aware
UTC instant -- because a contract has no view to compare against, so the semantic validator is
the only layer that can own the comparison, and it did not.

Codex also found the test that claimed to cover this sending ``requested_deadline=None``, which
exercises the absent-deadline path and says nothing about a past one. That test has been
renamed to what it actually asserts; the bound is asserted here, at microsecond resolution on
both sides of the boundary and exactly on it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from chorus.application.services.action_validation import (
    ValidatedProposal,
    validate_action_result,
)
from chorus.domain.entities import Purpose
from chorus.ports.agents import ActionRejection, AgentContractViolationError
from tests.repair.pinned import pinned_view
from tests.repair.validation_support import NAMESPACE, draft, invocation, result

MICROSECOND = timedelta(microseconds=1)


def _validate(deadline: datetime | None) -> ValidatedProposal:
    view = pinned_view()
    return validate_action_result(
        invocation=invocation(view),
        result=result(draft(requested_deadline=deadline, view=view), view=view),
        view=view,
        namespace=NAMESPACE,
        destination_id=view.destination.destination_id,
        purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
        expected_view_hash=view.view_hash.value,
    )


def _rejection_codes(deadline: datetime | None) -> tuple[str, ...]:
    with pytest.raises(AgentContractViolationError) as raised:
        _validate(deadline)
    return tuple(str(code) for code in raised.value.reason_codes)


# ---------------------------------------------------------------------------------------
# The boundary, at microsecond resolution
# ---------------------------------------------------------------------------------------


def test_one_microsecond_before_generated_at_is_rejected() -> None:
    generated_at = pinned_view().generated_at

    assert ActionRejection.DEADLINE_NOT_AFTER_VIEW.value in _rejection_codes(
        generated_at - MICROSECOND
    )


def test_exactly_generated_at_is_rejected() -> None:
    """Equality is a rejection, the same direction view and mandate expiry already take."""

    generated_at = pinned_view().generated_at

    assert ActionRejection.DEADLINE_NOT_AFTER_VIEW.value in _rejection_codes(generated_at)


def test_one_microsecond_after_generated_at_is_allowed() -> None:
    generated_at = pinned_view().generated_at
    validated = _validate(generated_at + MICROSECOND)

    assert validated.requested_deadline == generated_at + MICROSECOND


def test_a_deadline_well_before_the_view_is_rejected() -> None:
    assert ActionRejection.DEADLINE_NOT_AFTER_VIEW.value in _rejection_codes(
        pinned_view().generated_at - timedelta(days=30)
    )


def test_an_absent_deadline_remains_legal() -> None:
    """The schema permits it and the renderer has fixed copy for it."""

    assert _validate(None).requested_deadline is None


# ---------------------------------------------------------------------------------------
# The shape rule the contract already owned stays intact
# ---------------------------------------------------------------------------------------


def test_a_naive_deadline_is_still_refused_by_the_contract() -> None:
    """The timezone-aware UTC requirement is unchanged; only the comparison is new."""

    generated_at = pinned_view().generated_at
    with pytest.raises(ValueError):
        draft(requested_deadline=(generated_at + timedelta(days=7)).replace(tzinfo=None))


def test_a_non_utc_deadline_is_still_refused_by_the_contract() -> None:
    from datetime import timezone

    generated_at = pinned_view().generated_at
    shifted = (generated_at + timedelta(days=7)).astimezone(timezone(timedelta(hours=5)))
    with pytest.raises(ValueError):
        draft(requested_deadline=shifted)


def test_the_validated_proposal_carries_the_instant_it_checked() -> None:
    """A boolean would have forced the caller back to the unvalidated draft for the value."""

    deadline = pinned_view().generated_at + timedelta(days=7)

    assert _validate(deadline).requested_deadline == deadline
