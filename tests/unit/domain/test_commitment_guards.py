"""The two guard corrections of ADR-027 § 5, and the one system edge that survives them.

Both were **latent** rather than live: nothing constructed the calls before Phase 9, and Phase 9
is the phase that would otherwise have written the first system-actor caller. That is exactly
when a latent defect stops being latent, which is why they are corrected here and asserted over
the whole edge set rather than one case at a time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from chorus.domain.entities import (
    CaseState,
    Commitment,
    CommitmentStatus,
    CommunityCase,
)
from chorus.domain.errors import StateTransitionError
from chorus.domain.ids import (
    ActionId,
    CaseId,
    CommitmentId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    Namespace,
)
from chorus.domain.state import (
    COMMITMENT_EDGES,
    HUMAN_ONLY_COMMITMENT_STATUSES,
    CaseTransitionContext,
    transition_case,
    transition_commitment,
)

NOW = datetime(2030, 1, 15, 12, 0, tzinfo=UTC)
NAMESPACE = Namespace("TEST_GUARDS")


def commitment(status: CommitmentStatus = CommitmentStatus.DUE) -> Commitment:
    return Commitment(
        commitment_id=CommitmentId(uuid4()),
        case_id=CaseId(uuid4()),
        action_id=ActionId(uuid4()),
        source_evidence_id=EvidenceItemId(uuid4()),
        obligor="property management",
        action_text="restore elevator b to service by 2030-01-14",
        due_at=datetime(2030, 1, 14, 23, 59, 59, 999_999, tzinfo=UTC),
        verification_method="AFFECTED_CONTRIBUTOR_CONFIRMATION",
        status=status,
        scheduler_name="chorus-test-0000000a-" + str(uuid4()) + "-1",
        schedule_generation=1,
        due_event_id=uuid4(),
        verified_by_contributor_id=None,
        verification_evidence_id=None,
        outcome_note=None,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def case(state: CaseState) -> CommunityCase:
    return CommunityCase(
        case_id=CaseId(uuid4()),
        community_id=CommunityId(uuid4()),
        namespace=NAMESPACE,
        title="Elevator B out of service",
        issue_type="ELEVATOR_OUTAGE",
        state=state,
        report_ids=(),
        fact_ids=(),
        assessment_id=None,
        current_view_id=None,
        current_action_id=None,
        corroboration_source_count=2,
        state_reason_code="SEEDED",
        version=1,
        authorization_version=1,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.parametrize(
    "target", [CommitmentStatus.FULFILLED, CommitmentStatus.MISSED, CommitmentStatus.CANCELLED]
)
def test_every_commitment_outcome_but_due_is_human_only(target: CommitmentStatus) -> None:
    """T39: a model that could mark a promise kept could report a fixed elevator that is not."""

    source = (
        CommitmentStatus.PENDING if target is CommitmentStatus.CANCELLED else CommitmentStatus.DUE
    )
    with pytest.raises(StateTransitionError):
        transition_commitment(
            commitment(source), target, expected_version=1, now=NOW, actor_is_human=False
        )

    moved = transition_commitment(
        commitment(source), target, expected_version=1, now=NOW, actor_is_human=True
    )
    assert moved.status is target


def test_pending_to_due_is_the_one_system_actor_edge() -> None:
    """Time passage produces a verification request; it never produces an outcome."""

    moved = transition_commitment(
        commitment(CommitmentStatus.PENDING),
        CommitmentStatus.DUE,
        expected_version=1,
        now=NOW,
    )

    assert moved.status is CommitmentStatus.DUE
    system_edges = {
        edge for edge in COMMITMENT_EDGES if edge[1] not in HUMAN_ONLY_COMMITMENT_STATUSES
    }
    assert system_edges == {(CommitmentStatus.PENDING, CommitmentStatus.DUE)}


def test_a_verification_records_who_decided_in_the_same_construction() -> None:
    contributor = ContributorId(uuid4())
    evidence = EvidenceItemId(uuid4())

    moved = transition_commitment(
        commitment(),
        CommitmentStatus.FULFILLED,
        expected_version=1,
        now=NOW,
        actor_is_human=True,
        verified_by_contributor_id=contributor,
        verification_evidence_id=evidence,
        outcome_note="The technician attended.",
    )

    assert moved.verified_by_contributor_id == contributor
    assert moved.verification_evidence_id == evidence
    assert moved.outcome_note == "The technician attended."


def test_verifying_to_ready_for_action_requires_a_human_actor() -> None:
    """The second latent guard: the documents always said this outcome was a person's."""

    with pytest.raises(StateTransitionError):
        transition_case(
            case(CaseState.VERIFYING),
            CaseState.READY_FOR_ACTION,
            expected_version=1,
            reason_code="COMMITMENT_MISSED",
            now=NOW,
            context=CaseTransitionContext(commitment_missed=True),
        )

    moved = transition_case(
        case(CaseState.VERIFYING),
        CaseState.READY_FOR_ACTION,
        expected_version=1,
        reason_code="COMMITMENT_MISSED",
        now=NOW,
        context=CaseTransitionContext(actor_is_human=True, commitment_missed=True),
    )
    assert moved.state is CaseState.READY_FOR_ACTION


def test_verifying_to_resolved_still_requires_a_human_and_an_affected_contributor() -> None:
    with pytest.raises(StateTransitionError):
        transition_case(
            case(CaseState.VERIFYING),
            CaseState.RESOLVED,
            expected_version=1,
            reason_code="COMMITMENT_FULFILLED",
            now=NOW,
            context=CaseTransitionContext(affected_contributor_verified=True),
        )


@pytest.mark.parametrize("target", [CaseState.RESOLVED, CaseState.READY_FOR_ACTION])
def test_neither_verification_edge_moves_the_authorization_epoch(target: CaseState) -> None:
    """ADR-020 row 10. A verification outcome changes no disclosure input."""

    before = case(CaseState.VERIFYING)
    moved = transition_case(
        before,
        target,
        expected_version=1,
        reason_code="COMMITMENT_OUTCOME",
        now=NOW,
        context=CaseTransitionContext(
            actor_is_human=True,
            affected_contributor_verified=target is CaseState.RESOLVED,
            commitment_missed=target is CaseState.READY_FOR_ACTION,
        ),
    )

    assert moved.version == before.version + 1
    assert moved.authorization_version == before.authorization_version


def test_actioned_to_verifying_is_reachable_only_with_a_commitment() -> None:
    with pytest.raises(StateTransitionError):
        transition_case(
            case(CaseState.ACTIONED),
            CaseState.VERIFYING,
            expected_version=1,
            reason_code="COMMITMENT_CREATED",
            now=NOW,
            context=CaseTransitionContext(),
        )


def test_actioned_never_reaches_resolved_directly() -> None:
    """Invariant 13, carried through the phase that could most easily erode it."""

    with pytest.raises(StateTransitionError):
        transition_case(
            case(CaseState.ACTIONED),
            CaseState.RESOLVED,
            expected_version=1,
            reason_code="ANYTHING",
            now=NOW,
            context=CaseTransitionContext(actor_is_human=True, affected_contributor_verified=True),
        )
