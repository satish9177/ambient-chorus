"""The two counters, and the exhaustive ADR-020 § 2 bump table as executable assertions.

The governing invariant is one sentence: **lifecycle progress is not itself disclosure
authority**. Everything here is a consequence of it.

Two tests carry the weight, and they are deliberately mirrors of each other:

* :func:`test_lifecycle_transition_never_bumps_authorization_version` sweeps the **whole** edge
  set, so a future edge cannot quietly acquire an authorization bump by being added to the
  table without a decision;
* :func:`test_authorization_sensitive_edges_bump_both_counters` is the other direction, so an
  edge cannot quietly *lose* one either.

A bump table that is only checked where somebody remembered to check it is not a table.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.fixtures.elevator import NOW, _uuid

from chorus.domain.entities import CaseState, CommunityCase
from chorus.domain.errors import StateTransitionError
from chorus.domain.ids import CaseId, CommunityId, Namespace
from chorus.domain.state import (
    AUTHORIZATION_SENSITIVE_CASE_EDGES,
    CASE_EDGES,
    MANDATE_MUTABLE_CASE_STATES,
    CaseTransitionContext,
    bump_case_authorization,
    case_edge_bumps_authorization,
    transition_case,
)

ALL_GUARDS = CaseTransitionContext(
    actor_is_human=True,
    candidate_accepted=True,
    mandate_proposals_for_all=True,
    any_mandate_decision=True,
    reports_retained=True,
    validated_assessment=True,
    independent_source_count=2,
    no_material_different_issue=True,
    has_compilable_purpose=True,
    readiness_lost=True,
    current_view_and_proposal_match=True,
    proposal_invalidated=True,
    readiness_remains=True,
    execution_sent=True,
    approval_consumed=True,
    commitment_or_verification_exists=True,
    another_action_needed=True,
    affected_contributor_verified=True,
    commitment_missed=True,
    fixed_close_reason=True,
    active_sending_execution=False,
    new_evidence=True,
    explicit_reopen=True,
)


def _case(state: CaseState, *, version: int = 3, epoch: int = 2) -> CommunityCase:
    """A case whose two counters differ, so a test cannot pass by confusing them.

    ``version`` and ``epoch`` are deliberately unequal. Under one counter every assertion below
    would have been vacuously satisfied by reading the same number twice.
    """

    return CommunityCase(
        case_id=CaseId(_uuid("authorization-case")),
        community_id=CommunityId(_uuid("authorization-community")),
        namespace=Namespace("TEST_AUTHORIZATION"),
        title="Recurring elevator failures",
        issue_type="ELEVATOR_FAILURE",
        state=state,
        report_ids=(),
        fact_ids=(),
        assessment_id=None,
        current_view_id=None,
        current_action_id=None,
        corroboration_source_count=2,
        state_reason_code="SEEDED",
        version=version,
        authorization_version=epoch,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
    )


# ---------------------------------------------------------------------------------------
# The two counters are independent
# ---------------------------------------------------------------------------------------


def test_both_counters_start_at_one_and_are_strictly_positive() -> None:
    case = _case(CaseState.CANDIDATE, version=1, epoch=1)

    assert case.version == 1
    assert case.authorization_version == 1

    with pytest.raises(ValueError):
        _case(CaseState.CANDIDATE, epoch=0)


def test_the_authorization_version_is_required_and_never_defaults() -> None:
    """A default of ``1`` would be a guessed-low epoch: a view that looks fresher than it is."""

    assert "authorization_version" in CommunityCase.__dataclass_fields__
    field = CommunityCase.__dataclass_fields__["authorization_version"]
    assert field.default is field.default_factory is __import__("dataclasses").MISSING


# ---------------------------------------------------------------------------------------
# The bump table, both directions, over the whole edge set
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "target"), sorted(CASE_EDGES, key=str))
def test_the_bump_table_decides_every_edge_in_the_frozen_set(
    source: CaseState, target: CaseState
) -> None:
    """Swept over the whole edge set, so no edge escapes the decision.

    The transition service reads the answer from one table rather than deciding per call; this
    proves the table and the service agree for every pair the machine admits.
    """

    case = _case(source)
    moved = transition_case(
        case,
        target,
        expected_version=case.version,
        reason_code="TEST_EDGE",
        now=NOW,
        context=ALL_GUARDS,
    )
    expected_bump = 1 if case_edge_bumps_authorization(source, target) else 0

    assert moved.version == case.version + 1
    assert moved.authorization_version == case.authorization_version + expected_bump


def test_lifecycle_transition_never_bumps_authorization_version() -> None:
    """Evaluation test 36, asserted over the whole edge set.

    Every edge *outside* the authorization-sensitive table carries the epoch forward unchanged.
    A future edge added to ``CASE_EDGES`` without a decision therefore lands here, in the
    lifecycle half, where it belongs by default -- and a future edge added to the sensitive
    table without a reason has to be justified in ADR-020 § 2 first.
    """

    lifecycle = sorted(CASE_EDGES - AUTHORIZATION_SENSITIVE_CASE_EDGES, key=str)
    assert lifecycle, "the machine must contain lifecycle-only edges"

    for source, target in lifecycle:
        case = _case(source)
        moved = transition_case(
            case,
            target,
            expected_version=case.version,
            reason_code="TEST_EDGE",
            now=NOW,
            context=ALL_GUARDS,
        )
        assert moved.authorization_version == case.authorization_version, (source, target)
        assert moved.version == case.version + 1, (source, target)


def test_authorization_sensitive_command_bumps_both_counters() -> None:
    """Evaluation test 37, the mirror of the one above."""

    for source, target in sorted(AUTHORIZATION_SENSITIVE_CASE_EDGES, key=str):
        assert (source, target) in CASE_EDGES, "a sensitive edge must be a real edge"
        case = _case(source)
        moved = transition_case(
            case,
            target,
            expected_version=case.version,
            reason_code="TEST_EDGE",
            now=NOW,
            context=ALL_GUARDS,
        )
        assert moved.authorization_version == case.authorization_version + 1, (source, target)
        assert moved.version == case.version + 1, (source, target)


def test_the_proposal_edge_is_lifecycle_only() -> None:
    """The edge that motivated the split, named explicitly rather than left to the sweep.

    ``READY_FOR_ACTION -> ACTION_PROPOSED`` records that a proposal exists. It changes no fact,
    no status, no mandate, and no count -- so under one counter it staled the very view that
    authorized it, and the first send of every case failed closed.
    """

    edge = (CaseState.READY_FOR_ACTION, CaseState.ACTION_PROPOSED)

    assert edge in CASE_EDGES
    assert edge not in AUTHORIZATION_SENSITIVE_CASE_EDGES

    case = _case(CaseState.READY_FOR_ACTION)
    moved = transition_case(
        case,
        CaseState.ACTION_PROPOSED,
        expected_version=case.version,
        reason_code="ACTION_PROPOSED",
        now=NOW,
        context=CaseTransitionContext(current_view_and_proposal_match=True),
    )

    assert moved.version == case.version + 1
    assert moved.authorization_version == case.authorization_version


def test_every_readiness_and_mandate_edge_is_authorization_sensitive() -> None:
    """Rows 3, 4, and 5 of the table, restated so a removal is visible rather than silent."""

    assert {
        (CaseState.CANDIDATE, CaseState.AWAITING_MANDATES),
        (CaseState.AWAITING_MANDATES, CaseState.INVESTIGATING),
        (CaseState.INVESTIGATING, CaseState.READY_FOR_ACTION),
        (CaseState.READY_FOR_ACTION, CaseState.INVESTIGATING),
        (CaseState.ACTION_PROPOSED, CaseState.INVESTIGATING),
    } == AUTHORIZATION_SENSITIVE_CASE_EDGES


# ---------------------------------------------------------------------------------------
# The change-of-no-state path
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(MANDATE_MUTABLE_CASE_STATES, key=str))
def test_bump_case_authorization_always_moves_both(state: CaseState) -> None:
    """This function exists *only* for authorization-sensitive changes of no state.

    An epoch that did not move here would be a mandate decision or an investigation result that
    left every bound view looking fresh.
    """

    case = _case(state)
    bumped = bump_case_authorization(
        case, expected_version=case.version, reason_code="MANDATE_DECIDED", now=NOW
    )

    assert bumped.version == case.version + 1
    assert bumped.authorization_version == case.authorization_version + 1
    assert bumped.state is case.state


@pytest.mark.parametrize("state", [CaseState.RESOLVED, CaseState.CLOSED_UNRESOLVED])
def test_a_terminal_case_refuses_an_authorization_bump(state: CaseState) -> None:
    """A decision against a terminal case leaves it exactly as it was."""

    case = _case(state)

    with pytest.raises(StateTransitionError):
        bump_case_authorization(
            case, expected_version=case.version, reason_code="MANDATE_DECIDED", now=NOW
        )


def test_a_stale_expected_version_refuses_both_paths() -> None:
    case = _case(CaseState.READY_FOR_ACTION)

    with pytest.raises(StateTransitionError):
        transition_case(
            case,
            CaseState.ACTION_PROPOSED,
            expected_version=case.version + 1,
            reason_code="ACTION_PROPOSED",
            now=NOW,
            context=CaseTransitionContext(current_view_and_proposal_match=True),
        )
    with pytest.raises(StateTransitionError):
        bump_case_authorization(case, expected_version=case.version + 1, reason_code="X", now=NOW)


def test_neither_counter_is_ever_decremented_by_any_path() -> None:
    """Monotonic, and asserted rather than assumed: nothing here can go backwards."""

    for source, target in sorted(CASE_EDGES, key=str):
        case = _case(source)
        moved = transition_case(
            case,
            target,
            expected_version=case.version,
            reason_code="TEST_EDGE",
            now=NOW,
            context=ALL_GUARDS,
        )
        assert moved.version > case.version
        assert moved.authorization_version >= case.authorization_version
