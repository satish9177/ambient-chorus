"""Explicit, deterministic state-transition services."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from chorus.domain.entities import (
    ActionExecution,
    ActionExecutionState,
    CaseState,
    Commitment,
    CommitmentStatus,
    CommunityCase,
)
from chorus.domain.errors import StateTransitionError
from chorus.domain.time import require_utc

CASE_EDGES: frozenset[tuple[CaseState, CaseState]] = frozenset(
    {
        (CaseState.CANDIDATE, CaseState.AWAITING_MANDATES),
        (CaseState.CANDIDATE, CaseState.CLOSED_UNRESOLVED),
        (CaseState.AWAITING_MANDATES, CaseState.INVESTIGATING),
        (CaseState.AWAITING_MANDATES, CaseState.CLOSED_UNRESOLVED),
        (CaseState.INVESTIGATING, CaseState.READY_FOR_ACTION),
        (CaseState.INVESTIGATING, CaseState.CLOSED_UNRESOLVED),
        (CaseState.READY_FOR_ACTION, CaseState.INVESTIGATING),
        (CaseState.READY_FOR_ACTION, CaseState.ACTION_PROPOSED),
        (CaseState.READY_FOR_ACTION, CaseState.CLOSED_UNRESOLVED),
        (CaseState.ACTION_PROPOSED, CaseState.READY_FOR_ACTION),
        (CaseState.ACTION_PROPOSED, CaseState.INVESTIGATING),
        (CaseState.ACTION_PROPOSED, CaseState.ACTIONED),
        (CaseState.ACTION_PROPOSED, CaseState.CLOSED_UNRESOLVED),
        (CaseState.ACTIONED, CaseState.VERIFYING),
        (CaseState.ACTIONED, CaseState.READY_FOR_ACTION),
        (CaseState.ACTIONED, CaseState.CLOSED_UNRESOLVED),
        (CaseState.VERIFYING, CaseState.RESOLVED),
        (CaseState.VERIFYING, CaseState.READY_FOR_ACTION),
        (CaseState.VERIFYING, CaseState.CLOSED_UNRESOLVED),
        (CaseState.RESOLVED, CaseState.INVESTIGATING),
        (CaseState.CLOSED_UNRESOLVED, CaseState.INVESTIGATING),
    }
)

ACTION_EXECUTION_EDGES: frozenset[tuple[ActionExecutionState, ActionExecutionState]] = frozenset(
    {
        (ActionExecutionState.DRAFT, ActionExecutionState.APPROVED),
        (ActionExecutionState.DRAFT, ActionExecutionState.FAILED),
        (ActionExecutionState.APPROVED, ActionExecutionState.SENDING),
        (ActionExecutionState.APPROVED, ActionExecutionState.FAILED),
        (ActionExecutionState.SENDING, ActionExecutionState.SENT),
        (ActionExecutionState.SENDING, ActionExecutionState.FAILED),
        (ActionExecutionState.SENDING, ActionExecutionState.SEND_UNKNOWN),
        (ActionExecutionState.SEND_UNKNOWN, ActionExecutionState.SENT),
        (ActionExecutionState.SEND_UNKNOWN, ActionExecutionState.FAILED),
    }
)

COMMITMENT_EDGES: frozenset[tuple[CommitmentStatus, CommitmentStatus]] = frozenset(
    {
        (CommitmentStatus.PENDING, CommitmentStatus.DUE),
        (CommitmentStatus.PENDING, CommitmentStatus.CANCELLED),
        (CommitmentStatus.DUE, CommitmentStatus.FULFILLED),
        (CommitmentStatus.DUE, CommitmentStatus.MISSED),
        (CommitmentStatus.DUE, CommitmentStatus.CANCELLED),
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CaseTransitionContext:
    """Deterministic evidence supplied to a case transition guard."""

    actor_is_human: bool = False
    candidate_accepted: bool = False
    mandate_proposals_for_all: bool = False
    any_mandate_decision: bool = False
    reports_retained: bool = False
    validated_assessment: bool = False
    independent_source_count: int = 0
    no_material_different_issue: bool = False
    has_compilable_purpose: bool = False
    readiness_lost: bool = False
    current_view_and_proposal_match: bool = False
    proposal_invalidated: bool = False
    readiness_remains: bool = False
    execution_sent: bool = False
    approval_consumed: bool = False
    commitment_or_verification_exists: bool = False
    another_action_needed: bool = False
    affected_contributor_verified: bool = False
    commitment_missed: bool = False
    fixed_close_reason: bool = False
    active_sending_execution: bool = False
    new_evidence: bool = False
    explicit_reopen: bool = False


def _case_guard(source: CaseState, target: CaseState, context: CaseTransitionContext) -> bool:
    if target is CaseState.CLOSED_UNRESOLVED:
        return (
            context.actor_is_human
            and context.fixed_close_reason
            and not context.active_sending_execution
        )
    if (source, target) == (CaseState.CANDIDATE, CaseState.AWAITING_MANDATES):
        return context.candidate_accepted and context.mandate_proposals_for_all
    if (source, target) == (CaseState.AWAITING_MANDATES, CaseState.INVESTIGATING):
        return context.any_mandate_decision and context.reports_retained
    if (source, target) == (CaseState.INVESTIGATING, CaseState.READY_FOR_ACTION):
        return (
            context.validated_assessment
            and context.independent_source_count >= 2
            and context.no_material_different_issue
            and context.has_compilable_purpose
        )
    if target is CaseState.INVESTIGATING and source in {
        CaseState.READY_FOR_ACTION,
        CaseState.ACTION_PROPOSED,
    }:
        return context.readiness_lost
    if (source, target) == (CaseState.READY_FOR_ACTION, CaseState.ACTION_PROPOSED):
        return context.current_view_and_proposal_match
    if (source, target) == (CaseState.ACTION_PROPOSED, CaseState.READY_FOR_ACTION):
        return context.proposal_invalidated and context.readiness_remains
    if (source, target) == (CaseState.ACTION_PROPOSED, CaseState.ACTIONED):
        return context.execution_sent and context.approval_consumed
    if (source, target) == (CaseState.ACTIONED, CaseState.VERIFYING):
        return context.commitment_or_verification_exists
    if (source, target) == (CaseState.ACTIONED, CaseState.READY_FOR_ACTION):
        return context.another_action_needed
    if (source, target) == (CaseState.VERIFYING, CaseState.RESOLVED):
        return context.actor_is_human and context.affected_contributor_verified
    if (source, target) == (CaseState.VERIFYING, CaseState.READY_FOR_ACTION):
        return context.commitment_missed
    if source in {CaseState.RESOLVED, CaseState.CLOSED_UNRESOLVED}:
        return context.actor_is_human and context.new_evidence and context.explicit_reopen
    return False


def transition_case(
    case: CommunityCase,
    target: CaseState,
    *,
    expected_version: int,
    reason_code: str,
    now: datetime,
    context: CaseTransitionContext,
) -> CommunityCase:
    """Return the next immutable case version or fail without coercion."""

    require_utc(now)
    edge = (case.state, target)
    if (
        case.version != expected_version
        or edge not in CASE_EDGES
        or not _case_guard(*edge, context)
    ):
        raise StateTransitionError(str(case.case_id))
    return replace(
        case,
        state=target,
        state_reason_code=reason_code,
        version=case.version + 1,
        updated_at=now,
        resolved_at=now if target is CaseState.RESOLVED else case.resolved_at,
        closed_at=now if target is CaseState.CLOSED_UNRESOLVED else case.closed_at,
    )


MANDATE_MUTABLE_CASE_STATES: frozenset[CaseState] = frozenset(
    {
        CaseState.CANDIDATE,
        CaseState.AWAITING_MANDATES,
        CaseState.INVESTIGATING,
        CaseState.READY_FOR_ACTION,
        CaseState.ACTION_PROPOSED,
        CaseState.ACTIONED,
        CaseState.VERIFYING,
    }
)
"""Where an authorization decision may still be recorded against a case.

Everything except the two terminal states. ``RESOLVED`` and ``CLOSED_UNRESOLVED`` are excluded
because the state machine reopens a terminal case only through an explicit human reopen
command, and a mandate decision is not one: accepting it would bump the version of a case
nothing may act on and leave the pointer describing an authorization that no longer has a
subject. A decision against a terminal case is refused with the case left exactly as it was.

``ACTIONED`` and ``VERIFYING`` are included deliberately. A revocation there cannot unsend the
message that was already sent, and the frozen contract says so plainly -- but it still governs
every future export, so refusing to record it would be the worse answer.
"""


def bump_case_authorization(
    case: CommunityCase,
    *,
    expected_version: int,
    reason_code: str,
    now: datetime,
) -> CommunityCase:
    """Increment the case version for an authorization-sensitive change of no state.

    The frozen compiler contract requires a mandate decision to move the case version so that
    every previously compiled view and every proposal bound to the old version becomes stale.
    Most decisions imply no *state* change, and :func:`transition_case` cannot express that:
    every edge it knows is a real edge with its own guard, and ``(state, state)`` is not one.

    Coercing a self-edge into the transition table would have been worse than a second
    function. It would put an unguarded pair into a table whose entire value is that every pair
    in it is guarded, and the first reader to add ``(READY_FOR_ACTION, READY_FOR_ACTION)`` for
    convenience would have opened a path that skips readiness reconciliation entirely.
    """

    require_utc(now)
    if case.version != expected_version or case.state not in MANDATE_MUTABLE_CASE_STATES:
        raise StateTransitionError(str(case.case_id))
    return replace(
        case,
        state_reason_code=reason_code,
        version=case.version + 1,
        updated_at=now,
    )


def transition_action_execution(
    execution: ActionExecution,
    target: ActionExecutionState,
    *,
    expected_version: int,
    now: datetime,
    reconciliation_proof: bool = False,
) -> ActionExecution:
    """Advance one-attempt execution; ambiguous state requires reconciliation proof."""

    require_utc(now)
    edge = (execution.state, target)
    is_reconciliation = execution.state is ActionExecutionState.SEND_UNKNOWN
    if (
        execution.version != expected_version
        or edge not in ACTION_EXECUTION_EDGES
        or (is_reconciliation and not reconciliation_proof)
    ):
        raise StateTransitionError(str(execution.execution_id))
    return replace(execution, state=target, version=execution.version + 1, updated_at=now)


def transition_commitment(
    commitment: Commitment,
    target: CommitmentStatus,
    *,
    expected_version: int,
    now: datetime,
    actor_is_human: bool = False,
) -> Commitment:
    """Advance commitment status; cancellation is a human-only decision."""

    require_utc(now)
    edge = (commitment.status, target)
    if (
        commitment.version != expected_version
        or edge not in COMMITMENT_EDGES
        or (target is CommitmentStatus.CANCELLED and not actor_is_human)
    ):
        raise StateTransitionError(str(commitment.commitment_id))
    return replace(commitment, status=target, version=commitment.version + 1, updated_at=now)
