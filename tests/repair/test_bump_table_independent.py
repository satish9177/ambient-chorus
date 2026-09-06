"""An ADR-020 § 2 bump table encoded independently of the production table it tests.

Codex found the existing "exhaustive" sweep deriving its expected answer from
``case_edge_bumps_authorization`` -- the very function under test. A table compared with itself
is satisfied by any table, including a wrong one: rename one member of
``AUTHORIZATION_SENSITIVE_CASE_EDGES`` and the sweep stays green while the system's disclosure
freshness quietly changes.

This module is the oracle written from the **document**. ``EXPECTED_BUMPS`` below is
transcribed by hand from ADR-020 § 2's twelve rows and imports nothing from
``chorus.domain.state`` except the enum it has to name the states with and the edge set it has
to enumerate a domain over. ``AUTHORIZATION_SENSITIVE_CASE_EDGES`` and
``case_edge_bumps_authorization`` are deliberately **not** imported: an oracle that read them
would be the circular test again.

This is a test-quality repair. It changes no production behaviour, and if the two tables ever
disagree the question to ask is which one ADR-020 § 2 actually says.
"""

from __future__ import annotations

import pytest

from chorus.domain.entities import CaseState, CommunityCase
from chorus.domain.ids import CaseId, CommunityId, Namespace
from chorus.domain.state import (
    CASE_EDGES,
    MANDATE_MUTABLE_CASE_STATES,
    CaseTransitionContext,
    bump_case_authorization,
    transition_case,
)
from tests.fixtures.elevator import NOW, _uuid
from tests.repair.bump_oracle import (
    ADR_ROW_BY_EDGE,
    EXPECTED_BUMPS,
    NO_STATE_CHANGE_ROWS,
)

ALL_GUARDS = CaseTransitionContext(
    actor_is_human=True,
    candidate_accepted=True,
    mandate_proposals_for_all=True,
    any_mandate_decision=True,
    reports_retained=True,
    validated_assessment=True,
    independent_source_count=4,
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


def _case(state: CaseState) -> CommunityCase:
    """A case whose two counters differ, so a test cannot pass by confusing them."""

    return CommunityCase(
        case_id=CaseId(_uuid("repair-bump-case")),
        community_id=CommunityId(_uuid("repair-bump-community")),
        namespace=Namespace("TEST_REPAIR_PHASE7"),
        title="Recurring elevator failures",
        issue_type="ELEVATOR_FAILURE",
        state=state,
        report_ids=(),
        fact_ids=(),
        assessment_id=None,
        current_view_id=None,
        current_action_id=None,
        corroboration_source_count=4,
        state_reason_code="SEEDED",
        version=3,
        authorization_version=7,
        created_at=NOW,
        updated_at=NOW,
    )


# ---------------------------------------------------------------------------------------
# The oracle covers the machine, exactly
# ---------------------------------------------------------------------------------------


def test_the_oracle_names_every_v1_case_write_edge() -> None:
    """A new edge in the machine with no row in ADR-020 § 2 fails here.

    ``CASE_EDGES`` is used as the *domain* to enumerate, never as the answer. That is the one
    thing an independent oracle may take from the implementation: which questions to ask.
    """

    assert set(EXPECTED_BUMPS) == CASE_EDGES


def test_every_edge_carries_the_adr_row_it_came_from() -> None:
    """Each expectation is attributable to a numbered row, so no member is folklore."""

    assert set(ADR_ROW_BY_EDGE) == CASE_EDGES
    assert all(1 <= row <= 12 for row in ADR_ROW_BY_EDGE.values())


# ---------------------------------------------------------------------------------------
# Every edge, against the document's answer
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "target"), sorted(CASE_EDGES, key=str))
def test_each_edge_matches_the_adr_expectation(source: CaseState, target: CaseState) -> None:
    """Both counters, compared against the hand-transcribed table."""

    case = _case(source)
    moved = transition_case(
        case,
        target,
        expected_version=case.version,
        reason_code="TEST_EDGE",
        now=NOW,
        context=ALL_GUARDS,
    )
    expected = EXPECTED_BUMPS[(source, target)]

    assert moved.version == case.version + 1, (source, target)
    assert moved.authorization_version == case.authorization_version + (1 if expected else 0), (
        source,
        target,
        f"ADR-020 section 2 row {ADR_ROW_BY_EDGE[(source, target)]}",
    )


def test_the_document_and_the_implementation_agree_on_the_sensitive_set() -> None:
    """State the disagreement as a set difference, so a failure names the edges.

    The implementation's table is read *here and only here*, after every edge has already been
    checked against the document above -- so this assertion reports a disagreement rather than
    being the thing that decides the answer.
    """

    from chorus.domain.state import AUTHORIZATION_SENSITIVE_CASE_EDGES

    from_document = {edge for edge, bumps in EXPECTED_BUMPS.items() if bumps}

    assert from_document == AUTHORIZATION_SENSITIVE_CASE_EDGES


# ---------------------------------------------------------------------------------------
# The rows that change no state
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(MANDATE_MUTABLE_CASE_STATES, key=str))
def test_the_change_of_no_state_rows_move_both_counters(state: CaseState) -> None:
    """Rows 2, 4, and 5 of § 2 also have a no-state-change form, and it bumps both.

    ``bump_case_authorization`` is the only path those take, and the document is unambiguous:
    a new report linkage, a mandate decision that moves no state, and an investigation apply
    that leaves the case where it is are all authorization-sensitive.
    """

    assert NO_STATE_CHANGE_ROWS == (2, 4, 5)

    case = _case(state)
    bumped = bump_case_authorization(
        case, expected_version=case.version, reason_code="MANDATE_DECIDED", now=NOW
    )

    assert bumped.version == case.version + 1
    assert bumped.authorization_version == case.authorization_version + 1
    assert bumped.state is state


def test_the_proposal_edge_is_lifecycle_only_by_the_documents_own_row() -> None:
    """Row 6, quoted: "records that a proposal exists; changes no fact, status, mandate, or
    count". It is the edge the whole split exists for."""

    edge = (CaseState.READY_FOR_ACTION, CaseState.ACTION_PROPOSED)

    assert ADR_ROW_BY_EDGE[edge] == 6
    assert EXPECTED_BUMPS[edge] is False
