from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.fixtures.elevator import NOW, _uuid, build_elevator_fixture

from chorus.domain.entities import (
    ActionExecution,
    ActionExecutionState,
    CaseState,
    CommitmentStatus,
)
from chorus.domain.errors import StateTransitionError
from chorus.domain.ids import ApprovalId, ExecutionId, Sha256Digest
from chorus.domain.state import (
    ACTION_EXECUTION_EDGES,
    CASE_EDGES,
    COMMITMENT_EDGES,
    CaseTransitionContext,
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
