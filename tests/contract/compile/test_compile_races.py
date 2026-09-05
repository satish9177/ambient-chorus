"""Idempotency, concurrency, and the pointer that must never move backwards.

Everything here is about a second attempt: a redelivery, a twin that lost a race, a compile
that read the world and committed late, and a send that arrived in between. The rule these
share is that the durable record decides, never the order two callers happened to arrive in.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.fixtures.compile import CompileHarness, harness_uuid, photo_bytes
from tests.fixtures.elevator import NOW

from chorus.application.commands.compile_view import (
    DENY_FIXED_TRANSACTION_PARTICIPANTS,
    CompileView,
)
from chorus.application.errors import PolicyDeniedError, SendAuthorizationInProgressError
from chorus.domain.ids import ActionId, ApprovalId, ExecutionId, Uuid5Generator
from chorus.ports.errors import IdempotencyConflictError, PersistenceConflictError
from chorus.ports.pagination import PageRequest
from chorus.ports.records import SendFence, ViewPointerExpectation
from chorus.ports.unit_of_work import TransactionPlan
from chorus.privacy.compiler import PrivacyCompiler

pytestmark = pytest.mark.anyio


async def _seed(harness: CompileHarness) -> CompileView:
    raw = photo_bytes()
    await harness.seed(evidence_items=harness.align_photo_digest(raw), photo=raw)
    return harness.compile_view()


# -- idempotency -----------------------------------------------------------------------


async def test_the_same_key_and_request_replays_the_same_result(
    harness: CompileHarness,
) -> None:
    """Matrix AA. A completed compile is answered from the record, never recomputed."""

    compile_view = await _seed(harness)
    command = harness.command()

    first = await compile_view.execute(command)
    sanitizer_calls = harness.sanitizer.calls
    put_calls = harness.objects.put_calls

    second = await compile_view.execute(command)

    assert second.replayed is True
    assert first.view is not None
    assert second.compile_id == first.compile_id
    assert second.audit_event_id == first.audit_event_id
    assert {entry.fact_id for entry in second.included} == {
        entry.fact_id for entry in first.included
    }
    # No gate ran again, no identifier was minted, and no second object was written.
    assert harness.sanitizer.calls == sanitizer_calls
    assert harness.objects.put_calls == put_calls
    history = await harness.shareable.read_view_history(harness.scope, PageRequest(limit=10))
    assert len(history.items) == 1


async def test_a_replay_does_not_move_the_pointer_a_second_time(
    harness: CompileHarness,
) -> None:
    compile_view = await _seed(harness)
    command = harness.command()

    await compile_view.execute(command)
    before = await harness.shareable.load_current_view_pointer(harness.scope)
    await compile_view.execute(command)
    after = await harness.shareable.load_current_view_pointer(harness.scope)

    assert before is not None
    assert after == before
    assert after.version == 1


async def test_the_same_key_with_a_different_request_is_a_conflict(
    harness: CompileHarness,
) -> None:
    """Matrix AB. One key, one request, and ``compile_id`` is part of what a request is."""

    compile_view = await _seed(harness)
    await compile_view.execute(harness.command())

    with pytest.raises(IdempotencyConflictError):
        await compile_view.execute(harness.command(compile_id=harness_uuid("compile:different")))


async def test_a_completed_denial_replays_as_the_same_denial(
    harness: CompileHarness,
) -> None:
    """A refusal is durable, so a redelivery is answered rather than re-decided."""

    compile_view = await _seed(harness)
    command = harness.command(expected_case_version=harness.case.version + 1)

    with pytest.raises(PolicyDeniedError) as first:
        await compile_view.execute(command)
    calls = harness.sanitizer.calls

    with pytest.raises(PolicyDeniedError) as second:
        await compile_view.execute(command)

    assert second.value.reason_codes == first.value.reason_codes
    # The replay is answered from the projection: no state was loaded and no image re-sanitized.
    assert harness.sanitizer.calls == calls


async def test_the_deny_transaction_has_exactly_the_frozen_participant_count(
    harness: CompileHarness,
) -> None:
    """Matrix AS, the denial half."""

    compile_view = await _seed(harness)
    sizes: dict[str, int] = {}
    inner = harness.unit_of_work

    class Counting:
        async def commit(self, plan: TransactionPlan) -> None:
            sizes[plan.name] = len(plan.operations)
            await inner.commit(plan)

        async def resolve_outcome(self, plan: TransactionPlan) -> object:
            return await inner.resolve_outcome(plan)

    compile_view.unit_of_work = Counting()  # type: ignore[assignment]

    with pytest.raises(PolicyDeniedError):
        await compile_view.execute(harness.command(expected_case_version=harness.case.version + 1))

    assert sizes == {"compile-view-deny": DENY_FIXED_TRANSACTION_PARTICIPANTS}


# -- the case moving under a compile ----------------------------------------------------


async def test_a_case_that_moves_after_the_strong_read_fails_the_transaction(
    harness: CompileHarness,
) -> None:
    """Matrix I and J.

    The compile reads a consistent world, decides, and then the case moves before it commits.
    The request-time gate cannot see that -- it already passed -- so only the transaction's
    version condition can refuse it, and it must. A mandate decision bumps the case version in
    the same transaction that records it, which is why this one condition also covers J.
    """

    compile_view = await _seed(harness)
    original = harness.unit_of_work
    scope = harness.scope
    core = harness.core
    case = harness.case

    class MovingTheCase:
        async def commit(self, plan: TransactionPlan) -> None:
            if plan.name == "compile-view-allow":
                await original.commit(
                    TransactionPlan(
                        name="concurrent-case-bump",
                        operations=(
                            core.stage_update_case(
                                scope,
                                replace(case, version=case.version + 1, updated_at=NOW),
                                expected_version=case.version,
                            ),
                        ),
                        audit_required=False,
                    )
                )
            await original.commit(plan)

        async def resolve_outcome(self, plan: TransactionPlan) -> object:
            return await original.resolve_outcome(plan)

    compile_view.unit_of_work = MovingTheCase()  # type: ignore[assignment]

    with pytest.raises(Exception) as error:
        await compile_view.execute(harness.command())

    assert "PERSISTENCE_CONFLICT" in str(error.value)
    assert await harness.shareable.load_current_view_pointer(harness.scope) is None


async def test_a_compile_never_mutates_the_core_case_or_its_version(
    harness: CompileHarness,
) -> None:
    """Matrix AR. The compiler's only Core write is the fence; the case is check-only."""

    compile_view = await _seed(harness)
    before = await harness.core.load_case(harness.scope)

    await compile_view.execute(harness.command())

    after = await harness.core.load_case(harness.scope)
    assert after == before
    assert after.version == before.version
    assert after.current_view_id is None


# -- the current pointer ----------------------------------------------------------------


async def test_a_second_compile_atomically_becomes_current(harness: CompileHarness) -> None:
    """Matrix AE."""

    compile_view = await _seed(harness)
    first = await compile_view.execute(harness.command())
    second = await compile_view.execute(
        harness.command(compile_id=harness_uuid("compile:2"), idempotency_key="compile-key-0002")
    )

    pointer = await harness.shareable.load_current_view_pointer(harness.scope)
    assert first.view is not None
    assert second.view is not None
    assert first.view.view_id != second.view.view_id
    assert pointer is not None
    assert pointer.view_id == second.view.view_id
    assert pointer.version == 2

    history = await harness.shareable.read_view_history(harness.scope, PageRequest(limit=10))
    assert {locator.view_id for locator in history.items} == {
        first.view.view_id,
        second.view.view_id,
    }


async def test_a_stale_compile_cannot_roll_the_pointer_backwards(
    harness: CompileHarness,
) -> None:
    """Matrix AD, and the reason the pointer condition binds the hash as well as the version.

    The stale compile reads pointer *N*, a newer compile installs *N+1*, and only then does the
    stale one try to commit. Both halves of its condition are now wrong, so its whole
    transaction fails -- there is no partial state in which an older view became current again.
    """

    compile_view = await _seed(harness)
    winner = await compile_view.execute(harness.command())
    winner_view = winner.view
    assert winner_view is not None
    winner_hash = winner_view.view_hash
    original = harness.unit_of_work
    shareable = harness.shareable
    scope = harness.scope

    class InstallingANewerPointer:
        installed = False

        async def commit(self, plan: TransactionPlan) -> None:
            if plan.name == "compile-view-allow" and not self.installed:
                self.installed = True
                current = await shareable.load_current_view_pointer(scope)
                assert current is not None
                await original.commit(
                    TransactionPlan(
                        name="concurrent-pointer-move",
                        operations=(
                            shareable.stage_replace_current_view_pointer(
                                scope,
                                replace(
                                    current,
                                    version=current.version + 1,
                                    view_hash=winner_hash,
                                    updated_at=NOW + timedelta(seconds=1),
                                ),
                                expected=ViewPointerExpectation(
                                    row_version=current.version, view_hash=current.view_hash
                                ),
                            ),
                        ),
                        audit_required=False,
                    )
                )
            await original.commit(plan)

        async def resolve_outcome(self, plan: TransactionPlan) -> object:
            return await original.resolve_outcome(plan)

    compile_view.unit_of_work = InstallingANewerPointer()  # type: ignore[assignment]

    with pytest.raises(Exception) as error:
        await compile_view.execute(
            harness.command(
                compile_id=harness_uuid("compile:stale"), idempotency_key="compile-key-stale"
            )
        )

    assert "PERSISTENCE_CONFLICT" in str(error.value)
    pointer = await harness.shareable.load_current_view_pointer(harness.scope)
    assert pointer is not None
    assert pointer.version == 2


# -- the send fence ----------------------------------------------------------------------


async def test_a_live_send_fence_refuses_a_compile_retryably(
    harness: CompileHarness,
) -> None:
    """Matrix AG. Either the send holds the case or the compile commits; never both."""

    compile_view = await _seed(harness)
    await harness.core.acquire_send_fence(
        harness.scope,
        SendFence(
            namespace=harness.scope.namespace,
            community_id=harness.scope.community_id,
            case_id=harness.scope.case_id,
            execution_id=ExecutionId(harness_uuid("execution")),
            action_id=ActionId(harness_uuid("action")),
            approval_id=ApprovalId(harness_uuid("approval")),
            view_id=harness_uuid("fence-view"),  # type: ignore[arg-type]
            authorization_snapshot_hash=harness.command().actor_id_hash,
            acquired_at=NOW,
            expires_at=NOW + timedelta(seconds=60),
        ),
    )

    with pytest.raises(SendAuthorizationInProgressError) as error:
        await compile_view.execute(harness.command())

    assert error.value.retryable is True
    assert await harness.shareable.load_current_view_pointer(harness.scope) is None


async def test_an_expired_fence_no_longer_blocks_a_compile(harness: CompileHarness) -> None:
    """The fence is a sixty-second window, not a lock somebody can forget to release."""

    compile_view = await _seed(harness)
    await harness.core.acquire_send_fence(
        harness.scope,
        SendFence(
            namespace=harness.scope.namespace,
            community_id=harness.scope.community_id,
            case_id=harness.scope.case_id,
            execution_id=ExecutionId(harness_uuid("execution")),
            action_id=ActionId(harness_uuid("action")),
            approval_id=ApprovalId(harness_uuid("approval")),
            view_id=harness_uuid("fence-view"),  # type: ignore[arg-type]
            authorization_snapshot_hash=harness.command().actor_id_hash,
            acquired_at=NOW - timedelta(minutes=5),
            expires_at=NOW - timedelta(seconds=1),
        ),
    )

    result = await compile_view.execute(harness.command())

    assert result.view is not None


async def test_a_stale_release_cannot_clear_another_execution_s_fence(
    harness: CompileHarness,
) -> None:
    """Matrix AH, over the same primitive Phase 8 will use. No second fence mechanism exists."""

    await _seed(harness)
    holder = ExecutionId(harness_uuid("execution:holder"))
    await harness.core.acquire_send_fence(
        harness.scope,
        SendFence(
            namespace=harness.scope.namespace,
            community_id=harness.scope.community_id,
            case_id=harness.scope.case_id,
            execution_id=holder,
            action_id=ActionId(harness_uuid("action")),
            approval_id=ApprovalId(harness_uuid("approval")),
            view_id=harness_uuid("fence-view"),  # type: ignore[arg-type]
            authorization_snapshot_hash=harness.command().actor_id_hash,
            acquired_at=NOW,
            expires_at=NOW + timedelta(seconds=60),
        ),
    )

    with pytest.raises(PersistenceConflictError):
        await harness.core.release_send_fence(
            harness.scope, ExecutionId(harness_uuid("execution:stale"))
        )

    still_there = await harness.core.load_send_fence(harness.scope)
    assert still_there is not None
    assert still_there.execution_id == holder


async def test_a_replay_answers_with_the_persisted_artifact_not_a_recomputed_one(
    harness: CompileHarness,
) -> None:
    """Gate 22: an ALLOW is never returned unpersisted, and a replay is where that bites.

    The identifier generator is ordinarily UUIDv4, so a second attempt at the same logical
    compile mints a *different* view identity. If the replay path answered with what it had just
    computed, the caller would receive an artifact that exists nowhere -- valid-looking, hashed,
    and absent from storage. So the answer is loaded back from the record the winner wrote.
    """

    compile_view = await _seed(harness)
    command = harness.command()
    first = await compile_view.execute(command)
    assert first.view is not None

    # A fresh generator for the second attempt: the same logical compile, different minted IDs.
    compile_view.compiler = PrivacyCompiler(
        id_generator_factory=lambda _: Uuid5Generator(
            namespace=harness_uuid("second-attempt"), prefix="compile"
        )
    )

    second = await compile_view.execute(command)

    assert second.replayed is True
    assert second.view is not None
    assert second.view.view_id == first.view.view_id
    assert second.view.view_hash == first.view.view_hash

    stored = await harness.shareable.load_view(harness.scope, second.view.view_id)
    assert stored == second.view


async def test_a_twin_that_loses_the_commit_race_is_answered_with_the_winners_artifact(
    harness: CompileHarness,
) -> None:
    """The same rule, reached through a conditional failure rather than a pre-commit read.

    The twin gets all the way to its transaction, is refused by the create-only conditions the
    winner already satisfied, and must then be told about the winner's view -- not its own.
    """

    compile_view = await _seed(harness)
    command = harness.command()
    winner = await harness.compile_view().execute(command)
    assert winner.view is not None
    original = harness.unit_of_work

    class LosingTheRace:
        async def commit(self, plan: TransactionPlan) -> None:
            await original.commit(plan)

        async def resolve_outcome(self, plan: TransactionPlan) -> object:
            return await original.resolve_outcome(plan)

    compile_view.unit_of_work = LosingTheRace()  # type: ignore[assignment]
    # Skip the pre-commit replay check so the attempt genuinely reaches its transaction.
    compile_view.compiler = PrivacyCompiler(
        id_generator_factory=lambda _: Uuid5Generator(
            namespace=harness_uuid("racing-twin"), prefix="compile"
        )
    )

    loser = await compile_view.execute(command)

    assert loser.replayed is True
    assert loser.view is not None
    assert loser.view.view_id == winner.view.view_id
