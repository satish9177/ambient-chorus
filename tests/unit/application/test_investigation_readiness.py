"""The readiness predicate: four deterministic terms, evaluated in one frozen order.

Every term here is checkable without a model. The only two model-originated values the
predicate reads are ``linkage_decision`` and contradiction ``materiality``, and both are
consumed through fixed tables and both can only block.

``recommended_case_disposition`` is absent from this file for the same reason it is absent from
the predicate: it is not a term, and there is nothing here for it to be a term of.
"""

from __future__ import annotations

import pytest

from chorus.application.services.investigation_readiness import (
    ReadinessOutcome,
    ReadinessReason,
    evaluate_readiness,
)
from chorus.contracts.investigation import LinkageDecision
from chorus.domain.entities import ContradictionMateriality


def ready(
    *,
    independent_source_count: int = 2,
    linkage_decision: LinkageDecision = LinkageDecision.SAME_ISSUE,
    contradiction_materialities: tuple[ContradictionMateriality, ...] = (),
    has_compilable_purpose: bool = True,
) -> ReadinessOutcome:
    """Evaluate readiness with every term satisfied except the ones a test names.

    A typed helper rather than a mapping splatted into the call: the predicate's parameters
    are the four terms, and a test that mistyped one should fail to type-check rather than
    fail at runtime for a reason that looks like a policy disagreement.
    """

    return evaluate_readiness(
        independent_source_count=independent_source_count,
        linkage_decision=linkage_decision,
        contradiction_materialities=contradiction_materialities,
        has_compilable_purpose=has_compilable_purpose,
    )


def test_every_term_satisfied_is_ready() -> None:
    outcome = ready()
    assert outcome.ready is True
    assert outcome.reason_code == ReadinessReason.READY


def test_one_contributor_and_many_reports_is_never_ready() -> None:
    """Case Z, and named test scenario 7."""

    outcome = ready(independent_source_count=1)
    assert outcome.ready is False
    assert outcome.reason_code == ReadinessReason.CORROBORATION_MIN_NOT_MET


@pytest.mark.parametrize(
    ("linkage", "expected"),
    [
        (LinkageDecision.SAME_ISSUE, True),
        (LinkageDecision.DIFFERENT_ISSUES, False),
        (LinkageDecision.UNCERTAIN, False),
    ],
)
def test_linkage_blocks_unless_it_is_same_issue(linkage: LinkageDecision, expected: bool) -> None:
    """Cases P, Q, R. ``UNCERTAIN`` blocks: an ambiguous linkage is a missing authorization."""

    outcome = ready(linkage_decision=linkage)
    assert outcome.ready is expected
    if not expected:
        assert outcome.reason_code == ReadinessReason.DIFFERENT_ISSUE_UNRESOLVED


@pytest.mark.parametrize(
    ("materiality", "expected"),
    [
        (ContradictionMateriality.LOW, True),
        (ContradictionMateriality.MEDIUM, False),
        (ContradictionMateriality.HIGH, False),
    ],
)
def test_contradiction_materiality_governs_readiness(
    materiality: ContradictionMateriality, expected: bool
) -> None:
    """Cases D2 and D3. ``LOW`` is nonfatal and leaves a downstream caveat obligation."""

    outcome = ready(contradiction_materialities=(materiality,))
    assert outcome.ready is expected
    assert outcome.contradictions_ok is expected
    if not expected:
        assert outcome.reason_code == ReadinessReason.CONTRADICTION_UNRESOLVED


def test_one_blocking_contradiction_among_many_low_ones_still_blocks() -> None:
    outcome = ready(
        contradiction_materialities=(
            ContradictionMateriality.LOW,
            ContradictionMateriality.LOW,
            ContradictionMateriality.MEDIUM,
        )
    )
    assert outcome.ready is False


def test_no_compilable_purpose_blocks_a_case_that_is_otherwise_ready() -> None:
    outcome = ready(has_compilable_purpose=False)
    assert outcome.ready is False
    assert outcome.reason_code == ReadinessReason.NO_COMPILABLE_PURPOSE


def test_the_failing_term_is_named_in_the_frozen_order() -> None:
    """The recorded reason must be the same code on every run, not whichever term ran first."""

    outcome = evaluate_readiness(
        independent_source_count=1,
        linkage_decision=LinkageDecision.UNCERTAIN,
        contradiction_materialities=(ContradictionMateriality.HIGH,),
        has_compilable_purpose=False,
    )
    assert outcome.reason_code == ReadinessReason.CORROBORATION_MIN_NOT_MET

    outcome = evaluate_readiness(
        independent_source_count=2,
        linkage_decision=LinkageDecision.UNCERTAIN,
        contradiction_materialities=(ContradictionMateriality.HIGH,),
        has_compilable_purpose=False,
    )
    assert outcome.reason_code == ReadinessReason.DIFFERENT_ISSUE_UNRESOLVED

    outcome = evaluate_readiness(
        independent_source_count=2,
        linkage_decision=LinkageDecision.SAME_ISSUE,
        contradiction_materialities=(ContradictionMateriality.HIGH,),
        has_compilable_purpose=False,
    )
    assert outcome.reason_code == ReadinessReason.CONTRADICTION_UNRESOLVED
