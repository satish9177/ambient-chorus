"""The compiler boundary's fence operations, over the one primitive Phase 8 will also use.

Phase 6 composes these and builds no caller. What is tested is ownership: who holds the fence,
who may return it, and what a stale process gets when it tries. The send-time revalidation --
view, proposal, approval, snapshot -- is Phase 8 and is deliberately not here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.fixtures.compile import CompileHarness, harness_uuid
from tests.fixtures.elevator import NOW

from chorus.application.errors import SendAuthorizationInProgressError
from chorus.application.services.send_fence import SendAuthorizationFence
from chorus.domain.ids import ActionId, ApprovalId, ExecutionId, Sha256Digest, ViewId
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.records import SendFence

pytestmark = pytest.mark.anyio


def _fence(harness: CompileHarness, label: str, *, seconds: int = 60) -> SendFence:
    return SendFence(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
        execution_id=ExecutionId(harness_uuid(f"execution:{label}")),
        action_id=ActionId(harness_uuid("action")),
        approval_id=ApprovalId(harness_uuid("approval")),
        view_id=ViewId(harness_uuid("fence-view")),
        authorization_snapshot_hash=Sha256Digest("sha256:" + "c" * 64),
        acquired_at=NOW,
        expires_at=NOW + timedelta(seconds=seconds),
    )


def _boundary(harness: CompileHarness) -> SendAuthorizationFence:
    return SendAuthorizationFence(core=harness.core, clock=harness.clock)


async def test_a_fence_is_acquired_and_returned_by_its_holder(harness: CompileHarness) -> None:
    boundary = _boundary(harness)
    fence = _fence(harness, "holder")

    outcome = await boundary.acquire(harness.scope, fence)
    assert outcome.replayed is False
    assert outcome.fence.execution_id == fence.execution_id

    await boundary.release(harness.scope, fence.execution_id)
    assert await harness.core.load_send_fence(harness.scope) is None


async def test_the_same_execution_replays_its_fence_without_extending_it(
    harness: CompileHarness,
) -> None:
    """A redelivery must not quietly widen the sixty-second authorization window."""

    boundary = _boundary(harness)
    fence = _fence(harness, "holder")
    first = await boundary.acquire(harness.scope, fence)

    second = await boundary.acquire(
        harness.scope, replace(fence, expires_at=NOW + timedelta(seconds=600))
    )

    assert second.replayed is True
    assert second.fence.expires_at == first.fence.expires_at


async def test_a_second_execution_cannot_take_a_live_fence(harness: CompileHarness) -> None:
    boundary = _boundary(harness)
    await boundary.acquire(harness.scope, _fence(harness, "holder"))

    with pytest.raises(PersistenceConflictError):
        await boundary.acquire(harness.scope, _fence(harness, "intruder"))


async def test_a_stale_release_cannot_clear_another_executions_fence(
    harness: CompileHarness,
) -> None:
    """Matrix AH, at the boundary rather than at the repository."""

    boundary = _boundary(harness)
    holder = _fence(harness, "holder")
    await boundary.acquire(harness.scope, holder)

    with pytest.raises(PersistenceConflictError):
        await boundary.release(harness.scope, ExecutionId(harness_uuid("execution:stale")))

    remaining = await harness.core.load_send_fence(harness.scope)
    assert remaining is not None
    assert remaining.execution_id == holder.execution_id


async def test_an_expired_fence_can_be_taken_over(harness: CompileHarness) -> None:
    boundary = _boundary(harness)
    await harness.core.acquire_send_fence(
        harness.scope,
        replace(
            _fence(harness, "abandoned"),
            acquired_at=NOW - timedelta(minutes=5),
            expires_at=NOW - timedelta(seconds=1),
        ),
    )

    outcome = await boundary.acquire(harness.scope, _fence(harness, "successor"))

    assert outcome.replayed is False
    assert outcome.fence.execution_id == ExecutionId(harness_uuid("execution:successor"))


async def test_require_clear_refuses_retryably_while_a_fence_is_live(
    harness: CompileHarness,
) -> None:
    boundary = _boundary(harness)
    await boundary.acquire(harness.scope, _fence(harness, "holder"))

    with pytest.raises(SendAuthorizationInProgressError) as error:
        await boundary.require_clear(harness.scope)

    assert error.value.retryable is True


async def test_require_clear_passes_at_the_exact_expiry_instant(
    harness: CompileHarness,
) -> None:
    """Expiry is compared in exact microseconds, and equality is expired."""

    boundary = _boundary(harness)
    fence = _fence(harness, "holder")
    await boundary.acquire(harness.scope, fence)

    await boundary.require_clear(harness.scope, now=fence.expires_at)

    with pytest.raises(SendAuthorizationInProgressError):
        await boundary.require_clear(
            harness.scope, now=fence.expires_at - timedelta(microseconds=1)
        )
