"""Freshness, races, idempotency, re-proposal, and recovery -- the four ways this can go wrong.

Two of these deserve saying out loud, because they are the tests that distinguish a system that
checks freshness from one that only appears to.

**Stale before invocation costs nothing.** Every refusal in the first section happens with the
model call count still at zero. A design that discovered staleness after invoking would have
spent a pass over a view it then refused to use.

**Stale during invocation costs exactly one.** The pointer and the case are re-proved as
*conditions* inside the apply transaction, so a compile or a mandate decision landing while the
model is answering fails the whole transaction -- and **no second invocation follows**. That is
the one race the pre-invocation checks structurally cannot see, and the ``on_invoke`` callback
on the scripted agent is what makes it reachable in a test at all.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from tests.fixtures.action import ActionHarness
from tests.fixtures.elevator import NOW

from chorus.application.commands.propose_action import (
    ProposalDenial,
    ProposalDeniedError,
)
from chorus.application.errors import (
    SendAuthorizationInProgressError,
    StaleAuthorizationError,
)
from chorus.domain.entities import (
    ActionExecutionState,
    ActionProposalStatus,
    ApplicationOperationStatus,
    CaseState,
)
from chorus.domain.ids import ActionId, ApprovalId, ExecutionId, ViewId
from chorus.domain.state import CaseTransitionContext, bump_case_authorization, transition_case
from chorus.ports.records import ActionPointerExpectation, SendFence
from chorus.ports.scopes import ActionScope
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------------------
# Stale before invocation: zero model calls
# ---------------------------------------------------------------------------------------


async def test_a_stale_expected_case_version_refuses_before_any_model_call(
    harness: ActionHarness,
) -> None:
    await harness.prepare()
    command = await harness.command(expected_case_version=99)

    with pytest.raises(ProposalDeniedError) as caught:
        await harness.propose_action().execute(command)

    assert caught.value.denial is ProposalDenial.STALE_CASE_VERSION
    assert harness.agent.invocations == []


async def test_a_case_that_is_not_ready_refuses_before_any_model_call(
    harness: ActionHarness,
) -> None:
    await harness.prepare()
    case = await harness.compile.core.load_case(harness.scope)
    moved = transition_case(
        case,
        CaseState.INVESTIGATING,
        expected_version=case.version,
        reason_code="READINESS_LOST",
        now=NOW,
        context=CaseTransitionContext(readiness_lost=True),
    )
    await _commit(
        harness,
        harness.compile.core.stage_update_case(harness.scope, moved, expected_version=case.version),
    )

    with pytest.raises(ProposalDeniedError) as caught:
        await harness.propose_action().execute(
            await harness.command(expected_case_version=moved.version)
        )

    assert caught.value.denial is ProposalDenial.CASE_NOT_READY
    assert harness.agent.invocations == []


async def test_a_view_that_is_not_current_refuses_before_any_model_call(
    harness: ActionHarness,
) -> None:
    """The caller names the view it believes is current; a newer one is a stale request.

    Refused rather than silently proposed against the newer view, because a caller who compiled,
    reviewed, and then asked is asking about the artifact they reviewed.
    """

    await harness.prepare()
    command = await harness.command(view_id=harness.view.view_id, view_hash=None)
    command = replace(command, view_hash=harness.view.view_hash)
    stale = replace(command, view_id=_other_view_id())

    with pytest.raises(ProposalDeniedError) as caught:
        await harness.propose_action().execute(stale)

    assert caught.value.denial is ProposalDenial.VIEW_NOT_CURRENT
    assert harness.agent.invocations == []


async def test_an_expired_view_refuses_before_any_model_call(
    harness: ActionHarness,
) -> None:
    """Equality at expiry means expired, matching the mandate and view expiry rule."""

    await harness.prepare()
    harness.compile.clock.instant = harness.view.expires_at

    with pytest.raises(ProposalDeniedError) as caught:
        await harness.propose_action().execute(await harness.command())

    assert caught.value.denial is ProposalDenial.VIEW_EXPIRED
    assert harness.agent.invocations == []


async def test_a_moved_authorization_epoch_refuses_before_any_model_call(
    harness: ActionHarness,
) -> None:
    """The three-way comparison: case, view, and pointer must agree.

    A mandate decision landing between the compile and the request moves the case's epoch and
    not the view's, and the request is refused with nothing spent.
    """

    await harness.prepare()
    case = await harness.compile.core.load_case(harness.scope)
    bumped = bump_case_authorization(
        case, expected_version=case.version, reason_code="MANDATE_DECIDED", now=NOW
    )
    await _commit(
        harness,
        harness.compile.core.stage_update_case(
            harness.scope, bumped, expected_version=case.version
        ),
    )

    with pytest.raises(ProposalDeniedError) as caught:
        await harness.propose_action().execute(
            await harness.command(expected_case_version=bumped.version)
        )

    assert caught.value.denial is ProposalDenial.STALE_AUTHORIZATION
    assert harness.agent.invocations == []


async def test_a_live_send_fence_refuses_before_any_model_call(
    harness: ActionHarness,
) -> None:
    await harness.prepare()
    await harness.compile.core.acquire_send_fence(
        harness.scope,
        SendFence(
            namespace=harness.scope.namespace,
            community_id=harness.scope.community_id,
            case_id=harness.scope.case_id,
            execution_id=_execution_id(),
            action_id=ActionId(uuid4()),
            approval_id=_approval_id(),
            view_id=harness.view.view_id,
            authorization_snapshot_hash=harness.view.authorization_snapshot_hash,
            acquired_at=NOW,
            expires_at=NOW + timedelta(seconds=60),
        ),
    )

    with pytest.raises(SendAuthorizationInProgressError):
        await harness.propose_action().execute(await harness.command())

    assert harness.agent.invocations == []


# ---------------------------------------------------------------------------------------
# Stale during invocation: exactly one model call, nothing persisted
# ---------------------------------------------------------------------------------------


async def test_current_view_pointer_move_during_invocation_persists_nothing(
    harness: ActionHarness,
) -> None:
    """The named Phase-7 race (evaluation test 39), and the reason participant 5 exists.

    An entire model invocation sits between the pointer read and the write. Here a second
    compile commits *while the model is answering*, so only the apply transaction's
    ``VIEW_CURRENT`` condition can refuse -- and it must refuse the whole transaction: no
    proposal, no ``DRAFT`` execution, no pointer movement, no case transition.

    **No second invocation follows.** A stale authorization is never retried, because repeating
    the request would spend another pass on a view that has already been superseded.
    """

    await harness.prepare()
    before = await harness.compile.core.load_case(harness.scope)

    async def recompile(_invocation: object) -> None:
        await harness.compile.compile_view().execute(
            harness.compile.command(compile_id=uuid4(), idempotency_key="compile-key-midflight")
        )

    harness.agent.on_invoke = recompile

    with pytest.raises(StaleAuthorizationError):
        await harness.propose_action().execute(await harness.command())

    assert len(harness.agent.invocations) == 1

    after = await harness.compile.core.load_case(harness.scope)
    pointer = await harness.compile.shareable.load_current_action_pointer(harness.scope)

    assert after.state is CaseState.READY_FOR_ACTION
    assert after.version == before.version
    assert pointer is None


async def test_case_change_during_invocation_persists_nothing(
    harness: ActionHarness,
) -> None:
    """The other half of the mid-flight race: the *case* moves rather than the view.

    Participant 9 conditions on the exact ``version``, ``authorization_version``, and ``state``
    the validator read, so a mandate decision landing mid-invocation fails the transaction
    whole -- which is what stops a proposal being bound to an authority that has been withdrawn.
    """

    await harness.prepare()
    before = await harness.compile.core.load_case(harness.scope)

    async def revoke(_invocation: object) -> None:
        case = await harness.compile.core.load_case(harness.scope)
        bumped = bump_case_authorization(
            case, expected_version=case.version, reason_code="MANDATE_DECIDED", now=NOW
        )
        await _commit(
            harness,
            harness.compile.core.stage_update_case(
                harness.scope, bumped, expected_version=case.version
            ),
        )

    harness.agent.on_invoke = revoke

    with pytest.raises(StaleAuthorizationError):
        await harness.propose_action().execute(await harness.command())

    assert len(harness.agent.invocations) == 1

    after = await harness.compile.core.load_case(harness.scope)
    assert after.state is CaseState.READY_FOR_ACTION
    assert after.authorization_version == before.authorization_version + 1
    assert await harness.compile.shareable.load_current_action_pointer(harness.scope) is None


# ---------------------------------------------------------------------------------------
# Re-proposal
# ---------------------------------------------------------------------------------------


async def test_second_proposal_against_live_draft_conflicts_without_model_call(
    harness: ActionHarness,
) -> None:
    """A pending human decision is never discarded by a second model call (ADR-022 § 6).

    Phase 7 does not invalidate a prior ``DRAFT`` and does not fail its execution. Clearing a
    proposal is the explicit human reject-or-edit path, which is Phase 8's.
    """

    await harness.prepare()
    await harness.propose_action().execute(await harness.command())
    calls = len(harness.agent.invocations)

    # The case is ACTION_PROPOSED now, so a second request is refused on state before the
    # pointer rule is even reached. Return the case to READY_FOR_ACTION to isolate the rule
    # this test is actually about.
    await _return_to_ready(harness)
    harness.invocation_id = uuid4()

    with pytest.raises(ProposalDeniedError) as caught:
        await harness.propose_action().execute(await harness.command())

    assert caught.value.denial is ProposalDenial.LIVE_DRAFT_PROPOSAL
    assert len(harness.agent.invocations) == calls


async def test_a_second_proposal_may_replace_an_invalidated_pointer_whose_execution_failed(
    harness: ActionHarness,
) -> None:
    """The one path Phase 7 allows, and it requires *both* halves.

    An invalidated pointer whose ``DRAFT`` execution is still live would leave a second
    execution beside it; a failed execution under a still-``DRAFT`` pointer would mean the
    human's decision has not been recorded yet.
    """

    await harness.prepare()
    first = await harness.propose_action().execute(await harness.command())
    await _return_to_ready(harness)
    await _invalidate(harness, first.action_id, first.execution_id)

    harness.invocation_id = uuid4()
    second = await harness.propose_action().execute(
        await harness.command(idempotency_key="propose-key-0002")
    )

    assert second.action_id != first.action_id
    pointer = await harness.compile.shareable.load_current_action_pointer(harness.scope)
    assert pointer is not None
    assert pointer.action_id == second.action_id


async def test_an_invalidated_pointer_with_a_live_draft_still_conflicts(
    harness: ActionHarness,
) -> None:
    await harness.prepare()
    first = await harness.propose_action().execute(await harness.command())
    await _return_to_ready(harness)
    await _invalidate(harness, first.action_id, first.execution_id, fail_execution=False)

    harness.invocation_id = uuid4()
    with pytest.raises(ProposalDeniedError) as caught:
        await harness.propose_action().execute(
            await harness.command(idempotency_key="propose-key-0002")
        )

    assert caught.value.denial is ProposalDenial.LIVE_DRAFT_PROPOSAL


async def test_a_stale_completion_never_rolls_the_action_pointer_backwards(
    harness: ActionHarness,
) -> None:
    """The pointer replace is conditioned on the exact row it read.

    A late attempt holding an older expectation cannot win, so ``ACTION_CURRENT`` only ever
    moves forward.
    """

    await harness.prepare()
    first = await harness.propose_action().execute(await harness.command())
    pointer = await harness.compile.shareable.load_current_action_pointer(harness.scope)
    assert pointer is not None

    stale = replace(pointer, version=pointer.version + 1, action_id=ActionId(uuid4()))
    from chorus.ports.errors import PersistenceConflictError

    with pytest.raises(PersistenceConflictError):
        await _commit(
            harness,
            harness.compile.shareable.stage_replace_current_action_pointer(
                harness.scope,
                stale,
                expected=ActionPointerExpectation(
                    row_version=pointer.version - 1 if pointer.version > 1 else pointer.version,
                    proposal_hash=harness.view.view_hash,
                ),
            ),
        )

    unchanged = await harness.compile.shareable.load_current_action_pointer(harness.scope)
    assert unchanged is not None
    assert unchanged.action_id == first.action_id


# ---------------------------------------------------------------------------------------
# Idempotency and recovery
# ---------------------------------------------------------------------------------------


async def test_a_redelivery_answers_from_the_durable_record_and_calls_no_model(
    harness: ActionHarness,
) -> None:
    """Participant 6 is what a redelivery reads, and it is read *before* the model.

    A redelivered job that reached the model first would already have spent a second pass by the
    time it discovered the answer existed.
    """

    await harness.prepare()
    first = await harness.propose_action().execute(await harness.command())
    calls = len(harness.agent.invocations)

    replayed = await harness.propose_action().execute(await harness.command())

    assert replayed.replayed is True
    assert replayed.action_id == first.action_id
    assert replayed.execution_id == first.execution_id
    assert replayed.proposal_hash == first.proposal_hash
    assert replayed.preview_hash == first.preview_hash
    assert len(harness.agent.invocations) == calls


async def test_lost_operation_status_recovers_from_durable_invocation_record(
    harness: ActionHarness,
) -> None:
    """The named Phase-7 recovery test (evaluation test 43). **Zero model calls.**

    The apply committed; the worker's ``RUNNING -> SUCCEEDED`` write was lost. A redelivery
    proves the handover, reads the durable ``ACTION`` invocation record, concludes the apply is
    already durable, and transcribes the status -- rather than spending a second model pass to
    find out what it could have read.
    """

    await harness.prepare()
    started = await harness.start_operation()
    job = await harness.job(started)
    worker = harness.worker()

    operation = await worker.execute(job)
    assert operation.status is ApplicationOperationStatus.SUCCEEDED
    calls = len(harness.agent.invocations)

    # Rewind the status projection to RUNNING, which is exactly what a lost status write leaves
    # behind: complete state under an operation that looks unfinished.
    operations = harness.operations()
    running = replace(
        operation,
        status=ApplicationOperationStatus.RUNNING,
        result_refs=(),
        version=operation.version + 1,
        updated_at=operation.updated_at,
    )
    await harness.compile.core.apply_operation_transition(
        harness.scope.namespace_scope, running, expected_version=operation.version
    )

    recovered = await worker.execute(job)

    assert recovered.status is ApplicationOperationStatus.SUCCEEDED
    assert len(harness.agent.invocations) == calls
    assert operations is not None


async def test_a_misrouted_job_claims_nothing_and_invokes_nothing(
    harness: ActionHarness,
) -> None:
    """A job whose binding disagrees with the durable operation is refused before any claim."""

    await harness.prepare()
    started = await harness.start_operation()
    job = await harness.job(started, invocation_id=uuid4())

    operation = await harness.worker().execute(job)

    assert operation.status is ApplicationOperationStatus.PENDING
    assert harness.agent.invocations == []


async def test_the_binding_hash_covers_the_exact_view(harness: ActionHarness) -> None:
    """A job naming a different view under a valid-looking request is refused.

    The request hash names the *command*; the binding names the exact view one invocation may
    propose against, and those differ precisely where a redelivery could substitute one.
    """

    await harness.prepare()
    started = await harness.start_operation()
    job = replace(await harness.job(started), view_hash=harness.view.authorization_snapshot_hash)

    operation = await harness.worker().execute(job)

    assert operation.status is ApplicationOperationStatus.PENDING
    assert harness.agent.invocations == []


async def test_a_failed_invocation_is_durable_so_it_is_never_re_asked(
    harness: ActionHarness,
) -> None:
    """ "This invocation is over" has to survive the failure that made it so."""

    await harness.prepare()
    harness.agent.responder = lambda invocation: (_ for _ in ()).throw(
        AssertionError("unreachable")
    )
    from chorus.ports.agents import AgentTimeoutError

    harness.agent.failures = [AgentTimeoutError(), AgentTimeoutError()]

    from chorus.ports.agents import AgentError

    with pytest.raises(AgentError):
        await harness.propose_action().execute(await harness.command())

    record = await harness.compile.core.load_agent_invocation(harness.scope, harness.invocation_id)
    assert record is not None
    assert record.output_hash is None

    with pytest.raises(AgentError):
        await harness.propose_action().execute(await harness.command())

    # Two failures were scripted for the first call's licensed retry; the replay called nothing.
    assert len(harness.agent.invocations) == 2


async def test_one_transient_failure_is_retried_under_the_same_invocation_identity(
    harness: ActionHarness,
) -> None:
    """The retry reuses the identity, the payload, the view, and the prompt artifact.

    So the durable record still describes one logical attempt and the input hash does not move.
    """

    await harness.prepare()
    from chorus.ports.agents import AgentTimeoutError

    harness.agent.failures = [AgentTimeoutError()]

    result = await harness.propose_action().execute(await harness.command())

    assert len(harness.agent.invocations) == 2
    identities = {invocation.invocation_id for invocation in harness.agent.invocations}
    payloads = {invocation.payload.view_hash for invocation in harness.agent.invocations}
    assert identities == {harness.invocation_id}
    assert len(payloads) == 1
    assert result.replayed is False


async def test_a_contract_violation_is_never_retried(harness: ActionHarness) -> None:
    """Repeating the request would produce the same unusable answer and spend another pass."""

    await harness.prepare()
    from chorus.ports.agents import ActionRejection, AgentContractViolationError

    harness.agent.failures = [AgentContractViolationError((ActionRejection.SCHEMA_INVALID,))]

    with pytest.raises(AgentContractViolationError):
        await harness.propose_action().execute(await harness.command())

    assert len(harness.agent.invocations) == 1


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------


async def _commit(harness: ActionHarness, *operations: object) -> None:
    await harness.compile.unit_of_work.commit(
        TransactionPlan(
            name="race-seed",
            operations=tuple(operations),  # type: ignore[arg-type]
            audit_required=False,
        )
    )


async def _return_to_ready(harness: ActionHarness) -> None:
    """Move the case back to ``READY_FOR_ACTION`` without touching the action pointer.

    Used to isolate the re-proposal rule from the state check that would otherwise fire first.
    """

    case = await harness.compile.core.load_case(harness.scope)
    if case.state is CaseState.READY_FOR_ACTION:
        return
    moved = transition_case(
        case,
        CaseState.READY_FOR_ACTION,
        expected_version=case.version,
        reason_code="PROPOSAL_INVALIDATED",
        now=NOW,
        context=CaseTransitionContext(proposal_invalidated=True, readiness_remains=True),
    )
    await _commit(
        harness,
        harness.compile.core.stage_update_case(harness.scope, moved, expected_version=case.version),
    )


async def _invalidate(
    harness: ActionHarness, action_id: object, execution_id: object, *, fail_execution: bool = True
) -> None:
    """Stand in for the Phase-8 human reject path, so Phase 7's own rule can be exercised.

    Phase 7 never performs this itself -- that is the whole of ADR-022 § 6 -- so the test writes
    it directly rather than reaching for a use case that deliberately does not exist yet.
    """

    scope = ActionScope(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
        action_id=action_id,  # type: ignore[arg-type]
    )
    pointer = await harness.compile.shareable.load_current_action_pointer(harness.scope)
    assert pointer is not None
    invalidated = replace(
        pointer, status=ActionProposalStatus.INVALIDATED, version=pointer.version + 1
    )
    operations: list[object] = [
        harness.compile.shareable.stage_replace_current_action_pointer(
            harness.scope,
            invalidated,
            expected=ActionPointerExpectation(
                row_version=pointer.version, proposal_hash=pointer.proposal_hash
            ),
        )
    ]
    if fail_execution:
        execution = await harness.compile.shareable.load_execution(scope, execution_id)  # type: ignore[arg-type]
        failed = replace(
            execution,
            state=ActionExecutionState.FAILED,
            failure_code="PROPOSAL_INVALIDATED",
            finished_at=NOW,
            version=execution.version + 1,
        )
        operations.append(
            harness.compile.shareable.stage_update_execution(
                scope, failed, expected_version=execution.version
            )
        )
    await _commit(harness, *operations)


def _other_view_id() -> ViewId:
    return ViewId(uuid4())


def _execution_id() -> ExecutionId:
    return ExecutionId(uuid4())


def _approval_id() -> ApprovalId:
    return ApprovalId(uuid4())
