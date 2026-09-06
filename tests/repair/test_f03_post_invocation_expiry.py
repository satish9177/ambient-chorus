"""F03 -- a view that expires while the model is answering must fail closed.

Codex's probe was exact:

```text
pre-invoke view is fresh
Action model starts
injected clock advances to view.expires_at
Action returns
proposal commits          <- the defect
```

Expiry is the one authorization fact no storage condition can express. The apply conditions on
the case row's ``version``, ``authorization_version``, and ``state``, and on the current-view
pointer's exact identity, and every one of those is satisfied by a view whose ``expires_at``
has simply passed -- because the passage of time mutates no row for a condition to notice.

The repair adds a second, separate clock read immediately before the proposal is staged. It is
used for authorization freshness only and is never written anywhere; ``now``, the canonical
artifact instant, is untouched. Equality at expiry means expired.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from chorus.application.errors import StaleAuthorizationError
from chorus.domain.entities import CaseState, CommunityCase
from chorus.ports.pagination import PageRequest
from chorus.ports.records import AgentInvocationOutcome
from chorus.ports.scopes import ActionScope, CaseScope
from tests.fixtures.action import ActionHarness

pytestmark = pytest.mark.anyio

MICROSECOND = timedelta(microseconds=1)


async def _advance_to(harness: ActionHarness, instant: datetime) -> None:
    async def move(_invocation: object) -> None:
        harness.compile.clock.instant = instant

    harness.agent.on_invoke = move


async def _assert_nothing_persisted(harness: ActionHarness, before: CommunityCase) -> None:
    """Every artifact the apply would have written, asserted absent."""

    after = await harness.compile.core.load_case(harness.scope)
    assert after.state is CaseState.READY_FOR_ACTION
    assert after.version == before.version
    assert after.authorization_version == before.authorization_version

    assert await harness.compile.shareable.load_current_action_pointer(harness.scope) is None
    history = await harness.compile.shareable.read_action_history(
        harness.scope, PageRequest(limit=10)
    )
    assert history.items == ()

    scope = CaseScope(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
    )
    record = await harness.compile.core.load_agent_invocation(scope, harness.invocation_id)
    # A durable record exists so the invocation is never re-asked, and it is a *failure*: no
    # successful invocation artifact, and therefore nothing a recovery path could read as
    # proof that an apply committed.
    assert record is not None
    assert record.outcome is AgentInvocationOutcome.FAILED
    assert record.result_refs == ()

    # Exactly one model call. An expired view is never regenerated against a newer one.
    assert len(harness.agent.invocations) == 1


# ---------------------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------------------


async def test_freshness_now_before_expiry_is_allowed(harness: ActionHarness) -> None:
    await harness.prepare()
    await _advance_to(harness, harness.view.expires_at - MICROSECOND)

    result = await harness.propose_action().execute(await harness.command())

    assert result.claim_count == 1
    assert await harness.compile.shareable.load_current_action_pointer(harness.scope) is not None


async def test_freshness_now_exactly_at_expiry_is_rejected(harness: ActionHarness) -> None:
    """Codex's probe, reproduced: the clock lands *on* ``expires_at`` mid-invocation."""

    await harness.prepare()
    before = await harness.compile.core.load_case(harness.scope)
    await _advance_to(harness, harness.view.expires_at)

    with pytest.raises(StaleAuthorizationError) as raised:
        await harness.propose_action().execute(await harness.command())

    assert "VIEW_EXPIRED" in raised.value.reason_codes
    await _assert_nothing_persisted(harness, before)


async def test_freshness_now_after_expiry_is_rejected(harness: ActionHarness) -> None:
    await harness.prepare()
    before = await harness.compile.core.load_case(harness.scope)
    await _advance_to(harness, harness.view.expires_at + timedelta(hours=1))

    with pytest.raises(StaleAuthorizationError):
        await harness.propose_action().execute(await harness.command())

    await _assert_nothing_persisted(harness, before)


# ---------------------------------------------------------------------------------------
# The authority model is unchanged
# ---------------------------------------------------------------------------------------


async def test_the_canonical_artifact_instant_is_still_the_entry_time_reading(
    harness: ActionHarness,
) -> None:
    """``freshness_now`` is a sample, not a second source of timestamps.

    The proposal, the ``DRAFT`` execution, the pointer, the locator, the audit row, and the
    invocation record are all stamped with the one canonical ``now`` the command read at entry,
    so one apply still produces one coherent set of rows.
    """

    await harness.prepare()
    entry_instant = harness.compile.clock.instant

    async def tick(_invocation: object) -> None:
        # Moves, but stays well inside the view's validity window.
        harness.compile.clock.instant = entry_instant + timedelta(seconds=30)

    harness.agent.on_invoke = tick
    await harness.propose_action().execute(await harness.command())

    pointer = await harness.compile.shareable.load_current_action_pointer(harness.scope)
    assert pointer is not None
    proposal = await harness.compile.shareable.load_proposal(
        ActionScope(
            namespace=harness.scope.namespace,
            community_id=harness.scope.community_id,
            case_id=harness.scope.case_id,
            action_id=pointer.action_id,
        )
    )

    assert proposal.created_at == entry_instant
    assert pointer.created_at == entry_instant


async def test_an_expiry_after_the_apply_does_not_undo_a_committed_proposal(
    harness: ActionHarness,
) -> None:
    """The sample is taken before staging; a clock that moves later changes nothing."""

    await harness.prepare()
    result = await harness.propose_action().execute(await harness.command())
    harness.compile.clock.instant = harness.view.expires_at + timedelta(days=1)

    pointer = await harness.compile.shareable.load_current_action_pointer(harness.scope)

    assert pointer is not None
    assert pointer.action_id == result.action_id
