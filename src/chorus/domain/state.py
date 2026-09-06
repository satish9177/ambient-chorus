"""Explicit, deterministic state-transition services."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from chorus.domain.entities import (
    EXECUTION_FIELD_PRESENCE,
    ActionExecution,
    ActionExecutionState,
    CaseState,
    Commitment,
    CommitmentStatus,
    CommunityCase,
)
from chorus.domain.errors import StateTransitionError
from chorus.domain.ids import ApprovalId, Sha256Digest
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

AUTHORIZATION_SENSITIVE_CASE_EDGES: frozenset[tuple[CaseState, CaseState]] = frozenset(
    {
        (CaseState.CANDIDATE, CaseState.AWAITING_MANDATES),
        (CaseState.AWAITING_MANDATES, CaseState.INVESTIGATING),
        (CaseState.INVESTIGATING, CaseState.READY_FOR_ACTION),
        (CaseState.READY_FOR_ACTION, CaseState.INVESTIGATING),
        (CaseState.ACTION_PROPOSED, CaseState.INVESTIGATING),
    }
)
"""The edges that also move ``CommunityCase.authorization_version`` (ADR-020 § 2).

The governing rule is one sentence and the table is its consequence: **lifecycle progress is
not itself disclosure authority**. A case moving through its state machine records what has
*happened to* the case; it does not change which facts exist, what their statuses are, which
mandates authorize them, or what a compiled view was allowed to say.

Each member is an edge whose own command necessarily changes one of those inputs in the same
transaction:

* ``CANDIDATE -> AWAITING_MANDATES`` creates mandate version 1 and its current pointers;
* ``AWAITING_MANDATES -> INVESTIGATING`` is caused by a mandate decision;
* the two readiness edges and the ``ACTION_PROPOSED -> INVESTIGATING`` readiness-lost edge are
  caused either by an investigation apply, which writes fact evidence statuses and the
  assessment pointer, or by a mandate withdrawal.

Everything else -- including ``READY_FOR_ACTION -> ACTION_PROPOSED``, the edge that motivated
the split -- carries the epoch forward unchanged. Under one counter, recording that a proposal
exists staled the very view that authorized it, so the first send of every case failed closed
and the second succeeded. That is not a design; it is a deadlock nobody had reached yet.

Widening this set is a *decision*, made by adding a row to ADR-020 § 2 with its reason stated.
It is never a blanket "state changes bump authorization" rule, because that is the reading the
split exists to refuse.
"""


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
    # Two counters, and which one an edge moves is read from the frozen table rather than
    # decided here. Every edge moves the OCC version; only an edge whose command genuinely
    # changes a case-owned disclosure input moves the authorization epoch.
    authorization_bump = 1 if edge in AUTHORIZATION_SENSITIVE_CASE_EDGES else 0
    return replace(
        case,
        state=target,
        state_reason_code=reason_code,
        version=case.version + 1,
        authorization_version=case.authorization_version + authorization_bump,
        updated_at=now,
        resolved_at=now if target is CaseState.RESOLVED else case.resolved_at,
        closed_at=now if target is CaseState.CLOSED_UNRESOLVED else case.closed_at,
    )


def case_edge_bumps_authorization(source: CaseState, target: CaseState) -> bool:
    """Whether this edge advances the disclosure-authority epoch as well as the row version.

    Exposed so the answer is read from one table by every caller and every test, rather than
    re-derived at each site from a reading of ADR-020 § 2.
    """

    return (source, target) in AUTHORIZATION_SENSITIVE_CASE_EDGES


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
        # Always both. This function exists only for authorization-sensitive changes of no
        # state, so an epoch that did not move here would be a mandate decision or an
        # investigation result that left every bound view looking fresh.
        authorization_version=case.authorization_version + 1,
        updated_at=now,
    )


def transition_action_execution(
    execution: ActionExecution,
    target: ActionExecutionState,
    *,
    expected_version: int,
    now: datetime,
    reconciliation_proof: bool = False,
    approval_id: ApprovalId | None = None,
    idempotency_key: str | None = None,
    rendered_message_hash: Sha256Digest | None = None,
    ses_request_token_hash: Sha256Digest | None = None,
    ses_message_id: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    failure_code: str | None = None,
    reconciled_at: datetime | None = None,
) -> ActionExecution:
    """Advance one-attempt execution; ambiguous state requires reconciliation proof.

    The nine optional arguments are the values a target state newly requires -- an
    ``approval_id`` and a send ``idempotency_key`` at ``APPROVED``, a rendered hash and an SES
    token at ``SENDING``, a ``finished_at`` at every terminal state. They are enumerated rather
    than taken as ``**kwargs`` because the set is closed by the presence table, and a keyword
    bag would accept a misspelling silently.

    They are applied in the same construction as the state change, so the entity's presence
    table validates the *result* rather than an intermediate shape that would fail on its way
    to a legal one. ``None`` means "leave as it was", never "clear it": clearing is what
    :func:`require_monotonic_presence` refuses.
    """

    require_utc(now)
    edge = (execution.state, target)
    is_reconciliation = execution.state is ActionExecutionState.SEND_UNKNOWN
    if (
        execution.version != expected_version
        or edge not in ACTION_EXECUTION_EDGES
        or (is_reconciliation and not reconciliation_proof)
    ):
        raise StateTransitionError(str(execution.execution_id))
    supplied = {
        "approval_id": approval_id,
        "idempotency_key": idempotency_key,
        "rendered_message_hash": rendered_message_hash,
        "ses_request_token_hash": ses_request_token_hash,
        "ses_message_id": ses_message_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "failure_code": failure_code,
        "reconciled_at": reconciled_at,
    }
    moved = replace(
        execution,
        state=target,
        version=execution.version + 1,
        updated_at=now,
        **{name: value for name, value in supplied.items() if value is not None},  # type: ignore[arg-type]
    )
    require_monotonic_presence(execution, moved)
    return moved


def require_monotonic_presence(before: ActionExecution, after: ActionExecution) -> None:
    """Refuse a transition that clears or rewrites a field that was already set.

    Presence is monotonic (ADR-022 § 1): once an approval, a send key, a rendered hash, or an
    SES token has been written down it describes something that actually happened, and a later
    state cannot make it un-happen. Unsetting one would leave a record that disagrees with the
    events it exists to record; rewriting one would let a second render quietly replace the
    bytes a human approved.
    """

    for name in EXECUTION_FIELD_PRESENCE:
        previous = getattr(before, name)
        current = getattr(after, name)
        if previous is not None and current != previous:
            raise StateTransitionError(str(before.execution_id))


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
