from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from tests.fixtures.elevator import NOW, _uuid, build_elevator_fixture

from chorus.domain.entities import (
    ActionExecution,
    ActionExecutionState,
    CaseState,
    CommitmentStatus,
    CommunityCase,
)
from chorus.domain.errors import StateTransitionError
from chorus.domain.ids import (
    ApprovalId,
    CaseId,
    CommunityId,
    ExecutionId,
    Namespace,
    Sha256Digest,
)
from chorus.domain.state import (
    ACTION_EXECUTION_EDGES,
    CASE_EDGES,
    COMMITMENT_EDGES,
    MANDATE_MUTABLE_CASE_STATES,
    CaseTransitionContext,
    bump_case_authorization,
    transition_action_execution,
    transition_case,
    transition_commitment,
)

ALL_CASE_GUARDS = CaseTransitionContext(
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


@pytest.mark.parametrize(("source", "target"), sorted(CASE_EDGES, key=lambda edge: str(edge)))
def test_case_every_legal_transition_is_accepted(source: CaseState, target: CaseState) -> None:
    case = replace(build_elevator_fixture().context.case, state=source, version=7)

    updated = transition_case(
        case,
        target,
        expected_version=7,
        reason_code="TEST_TRANSITION",
        now=NOW,
        context=ALL_CASE_GUARDS,
    )

    assert updated.state is target
    assert updated.version == 8


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (source, target)
        for source in CaseState
        for target in CaseState
        if (source, target) not in CASE_EDGES
    ],
)
def test_case_every_illegal_transition_fails(source: CaseState, target: CaseState) -> None:
    case = replace(build_elevator_fixture().context.case, state=source, version=7)

    with pytest.raises(StateTransitionError):
        transition_case(
            case,
            target,
            expected_version=7,
            reason_code="ILLEGAL",
            now=NOW,
            context=ALL_CASE_GUARDS,
        )


def test_actioned_cannot_transition_directly_to_resolved() -> None:
    case = replace(build_elevator_fixture().context.case, state=CaseState.ACTIONED)

    with pytest.raises(StateTransitionError):
        transition_case(
            case,
            CaseState.RESOLVED,
            expected_version=case.version,
            reason_code="NOT_ALLOWED",
            now=NOW,
            context=ALL_CASE_GUARDS,
        )


def _execution(state: ActionExecutionState) -> ActionExecution:
    fixture = build_elevator_fixture()
    digest = Sha256Digest("sha256:" + "1" * 64)
    action_id = fixture.missed_commitment.action_id
    assert action_id is not None
    return ActionExecution(
        execution_id=ExecutionId(_uuid("execution:test")),
        action_id=action_id,
        case_id=fixture.context.case.case_id,
        approval_id=ApprovalId(_uuid("approval:test")),
        proposal_hash=digest,
        view_hash=digest,
        idempotency_key="execution-test",
        state=state,
        rendered_message_hash=digest,
        ses_request_token_hash=digest,
        ses_message_id=None,
        started_at=None,
        finished_at=None,
        failure_code=None,
        failure_detail_safe=None,
        reconciled_at=None,
        version=1,
        created_at=NOW - timedelta(minutes=1),
        updated_at=NOW - timedelta(minutes=1),
    )


@pytest.mark.parametrize(
    ("source", "target"), sorted(ACTION_EXECUTION_EDGES, key=lambda edge: str(edge))
)
def test_action_execution_legal_edges_are_guarded(
    source: ActionExecutionState, target: ActionExecutionState
) -> None:
    result = transition_action_execution(
        _execution(source),
        target,
        expected_version=1,
        now=NOW,
        reconciliation_proof=source is ActionExecutionState.SEND_UNKNOWN,
    )

    assert result.state is target


def test_send_unknown_cannot_change_without_reconciliation() -> None:
    with pytest.raises(StateTransitionError):
        transition_action_execution(
            _execution(ActionExecutionState.SEND_UNKNOWN),
            ActionExecutionState.SENT,
            expected_version=1,
            now=NOW,
        )


@pytest.mark.parametrize(("source", "target"), sorted(COMMITMENT_EDGES, key=lambda edge: str(edge)))
def test_commitment_legal_edges_are_guarded(
    source: CommitmentStatus, target: CommitmentStatus
) -> None:
    commitment = replace(build_elevator_fixture().missed_commitment, status=source, version=1)

    updated = transition_commitment(
        commitment,
        target,
        expected_version=1,
        now=NOW,
        actor_is_human=target is CommitmentStatus.CANCELLED,
    )

    assert updated.status is target


# -- authorization-sensitive case version bumps --------------------------------------------

MANDATE_BUMP_REASON = "MANDATE_DECIDED"
BUMP_NOW = NOW


def _case_in(state: CaseState, *, version: int = 3) -> CommunityCase:
    return CommunityCase(
        case_id=CaseId(UUID("11111111-1111-4111-8111-111111111111")),
        community_id=CommunityId(UUID("22222222-2222-4222-8222-222222222222")),
        namespace=Namespace("TEST_STATE_BUMP"),
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
        created_at=BUMP_NOW,
        updated_at=BUMP_NOW,
    )


@pytest.mark.parametrize("state", sorted(MANDATE_MUTABLE_CASE_STATES, key=str))
def test_an_authorization_bump_moves_the_version_and_keeps_the_state(
    state: CaseState,
) -> None:
    """Every non-terminal state accepts the bump, and none of them changes state because of it."""

    case = _case_in(state)

    bumped = bump_case_authorization(
        case, expected_version=3, reason_code=MANDATE_BUMP_REASON, now=BUMP_NOW
    )

    assert bumped.version == 4
    assert bumped.state is state
    assert bumped.state_reason_code == MANDATE_BUMP_REASON
    assert bumped.updated_at == BUMP_NOW


@pytest.mark.parametrize("state", [CaseState.RESOLVED, CaseState.CLOSED_UNRESOLVED])
def test_a_terminal_case_refuses_an_authorization_bump(state: CaseState) -> None:
    """A resolved or closed case has nothing left for an authorization change to govern.

    The state machine reopens a terminal case only through an explicit human reopen command,
    and a mandate decision is not one -- so recording one here would bump a version no artifact
    is bound to, and leave a pointer describing consent with no subject.
    """

    with pytest.raises(StateTransitionError):
        bump_case_authorization(
            _case_in(state), expected_version=3, reason_code=MANDATE_BUMP_REASON, now=BUMP_NOW
        )


def test_an_authorization_bump_is_guarded_on_the_expected_version() -> None:
    with pytest.raises(StateTransitionError):
        bump_case_authorization(
            _case_in(CaseState.INVESTIGATING),
            expected_version=2,
            reason_code=MANDATE_BUMP_REASON,
            now=BUMP_NOW,
        )


def test_the_mandate_mutable_states_are_exactly_the_non_terminal_ones() -> None:
    terminal = {CaseState.RESOLVED, CaseState.CLOSED_UNRESOLVED}
    assert set(MANDATE_MUTABLE_CASE_STATES) == set(CaseState) - terminal
