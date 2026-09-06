"""What happens to the case when a human clears a proposal, and what deliberately does not.

Three human verbs clear a proposal -- reject, withdraw, and clear -- and all three end the same
way: the current action pointer moves to ``INVALIDATED``, which is the only thing that frees the
case for a new proposal (ADR-022 SS 6). What differs is the case, and the frozen guard is
``proposal_invalidated and readiness_remains``:

* **readiness remains** -> ``ACTION_PROPOSED -> READY_FOR_ACTION``, ``version N -> N+1``,
  ``authorization_version A -> A``. Lifecycle only.
* **readiness does not remain** -> **no case edge is taken at all.** The case stays
  ``ACTION_PROPOSED`` and the participant that would have written it is a ``ConditionCheck`` on
  the exact ``version``, ``authorization_version``, and ``state`` instead.

The second branch is the one worth stating out loud. ``ACTION_PROPOSED -> INVESTIGATING`` is the
readiness-lost edge, it bumps ``authorization_version``, and it is owned by the deterministic
readiness reconciliation. A human clearing a draft message is **not** an authorization event, so
this transaction must not mint one: two different facts would be committed by one command that
only decided one of them (ADR-023 SS 9).

The participant **count does not change between the branches** -- a ``PutItem`` becomes a
``CheckItem`` in the same position -- so one arithmetic assertion over the staged plan holds for
both.

What "readiness remains" is asked here, precisely
--------------------------------------------------
Not the full ``INVESTIGATING -> READY_FOR_ACTION`` predicate. That predicate needs a validated
assessment bound to the current case version, a recomputed corroboration count, a linkage
decision, contradiction materialities, and a compile preflight -- and re-running it inside a
rejection would put a whole investigation's worth of Core reads and a preflight compile behind a
human clicking "no".

The narrower question this asks is the one the case edge actually turns on: **may a new proposal
still be made against current authority?** That is true exactly when a current view pointer
exists, names an unexpired view, and stands at the case's own ``authorization_version``. It is
false in precisely the situation ADR-023 SS 9 names as the reason the second branch exists -- a
mandate revoked since the proposal was made, which moved the epoch past the view's and left the
case's readiness for the reconciliation that owns it.

It is deterministic, it reads two rows, and it never returns ``True`` for a case whose authority
has moved. Where it is conservative it is conservative in the safe direction: it leaves the case
``ACTION_PROPOSED`` with a cleared pointer, which the readiness reconciliation resolves, rather
than asserting a readiness nothing verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from chorus.domain.entities import CaseState, CommunityCase
from chorus.domain.state import CaseTransitionContext, transition_case
from chorus.ports.repositories import CoreRepositoryPort, ShareableRepositoryPort
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import CheckItem, PutItem

INVALIDATION_REASON_CODE = "ACTION_PROPOSAL_INVALIDATED"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadinessDecision:
    """Whether the clearing transaction takes a case edge, and the case it will report.

    ``next_case`` is the case *after* this transaction on both branches: the transitioned row
    when readiness remains, and the unchanged row when it does not. One field rather than a
    nullable one, because every caller has to report a case state either way and a ``None``
    would make each of them re-derive the unchanged answer.
    """

    remains: bool
    next_case: CommunityCase


async def evaluate_invalidation_readiness(
    *,
    shareable: ShareableRepositoryPort,
    scope: CaseScope,
    case: CommunityCase,
    now: datetime,
) -> ReadinessDecision:
    """Decide which branch a clearing verb takes, from two strongly read rows.

    A case that is not ``ACTION_PROPOSED`` takes no edge either: the only edge this decides is
    ``ACTION_PROPOSED -> READY_FOR_ACTION``, and a case somewhere else is not on it.
    """

    remains = case.state is CaseState.ACTION_PROPOSED and await _current_authority_stands(
        shareable=shareable, scope=scope, case=case, now=now
    )
    if not remains:
        return ReadinessDecision(remains=False, next_case=case)
    next_case = transition_case(
        case,
        CaseState.READY_FOR_ACTION,
        expected_version=case.version,
        reason_code=INVALIDATION_REASON_CODE,
        now=now,
        context=CaseTransitionContext(proposal_invalidated=True, readiness_remains=True),
    )
    return ReadinessDecision(remains=True, next_case=next_case)


async def _current_authority_stands(
    *,
    shareable: ShareableRepositoryPort,
    scope: CaseScope,
    case: CommunityCase,
    now: datetime,
) -> bool:
    """A current, unexpired view at the case's own epoch. Equality at expiry means expired."""

    pointer = await shareable.load_current_view_pointer(scope)
    if pointer is None:
        return False
    if pointer.authorization_version != case.authorization_version:
        return False
    return now < pointer.expires_at


def stage_case_after_invalidation(
    *,
    core: CoreRepositoryPort,
    scope: CaseScope,
    case: CommunityCase,
    readiness: ReadinessDecision,
    now: datetime,
) -> PutItem | CheckItem:
    """The one case participant, in whichever of its two forms this branch calls for.

    Returned from one function so the two branches cannot drift into different conditions, and
    so the count is structurally identical: exactly one participant either way.
    """

    if readiness.remains:
        return core.stage_update_case(
            scope,
            readiness.next_case,
            expected_version=case.version,
            expected_authorization_version=case.authorization_version,
            expected_state=case.state,
        )
    return core.stage_require_case_version(
        scope,
        expected_version=case.version,
        expected_authorization_version=case.authorization_version,
        expected_state=case.state,
    )


__all__ = [
    "INVALIDATION_REASON_CODE",
    "ReadinessDecision",
    "evaluate_invalidation_readiness",
    "stage_case_after_invalidation",
]
