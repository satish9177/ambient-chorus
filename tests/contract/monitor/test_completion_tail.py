"""The completion tail: what happens after the last data write, and what it must never cost.

Every test here is about the same narrow window. A Monitor operation has done all of its
user-visible work -- every report, fact, and feed signal committed -- and only the *recording*
of that fact is still outstanding. Three things can go wrong in that window, and the earlier
implementation got each of them wrong in the same direction, by treating completed work as
failed work:

* the successful invocation record could not be written, so a fully applied operation was
  settled ``FAILED``;
* the operation's ``SUCCEEDED`` transition was refused, so the operation sat ``RUNNING`` until
  the stale rule aged it into ``FAILED``;
* the transition's response was lost, with the same ending.

The fix is one idea, applied twice. Recording the successful invocation is the **final step of
the apply plan**, committed in the same transaction that completes apply progress -- so
``progress.is_complete`` implies the record is durable and there is no window between them. And
once that record exists, the status transition is pure transcription: any worker that finds a
``RUNNING`` operation with a finalized invocation finishes it, fresh claim or stale, with no
model call and no further mutation.

Every test asserts the model was invoked exactly once across the whole lifecycle, because "the
operation eventually succeeded" is worth nothing if it succeeded by paying for a second pass
over private community text.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.fixtures.faults import (
    FaultInjectingDriver,
    TransactBehaviour,
    WriteBehaviour,
    monitor_finalization,
    operation_transitions,
)
from tests.fixtures.monitor import MonitorHarness
from tests.fixtures.monitor_answers import THREE_GROUPS, GroupSpec, grouped_answer

from chorus.application.operations import (
    MAX_WORKER_EXECUTION_WINDOW,
    OPERATION_STALE_ERROR_CODE,
)
from chorus.domain.entities import ApplicationOperation, ApplicationOperationStatus
from chorus.infrastructure.local.monitor_agent import ScriptedMonitorAgent
from chorus.ports.operations import MonitorOperationJob
from chorus.ports.records import AgentInvocationOutcome, MessageFeedEntry
from chorus.ports.scopes import OperationScope

pytestmark = pytest.mark.anyio

DATA_STEPS = len(THREE_GROUPS)
"""Three groups, one apply step each -- and then the finalization step makes four."""


# ---------------------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------------------


def _responder(groups: tuple[GroupSpec, ...] = THREE_GROUPS) -> ScriptedMonitorAgent:
    return ScriptedMonitorAgent(
        responder=lambda invocation: grouped_answer(invocation.payload, groups)
    )


async def _dispatched(
    harness: MonitorHarness,
) -> tuple[ApplicationOperation, MonitorOperationJob, tuple[MessageFeedEntry, ...]]:
    await harness.seed()
    locators = await harness.ingest_feed()
    operation, job = await harness.dispatched(locators)
    return operation, job, locators


def _scope(harness: MonitorHarness, job: MonitorOperationJob) -> OperationScope:
    return OperationScope(namespace=harness.namespace, operation_id=job.operation_id)


def _finalization_fails(harness: MonitorHarness, script: list[TransactBehaviour]) -> MonitorHarness:
    """A harness whose *finalization* transaction follows ``script``; data steps all commit."""

    faulty = FaultInjectingDriver(
        inner=harness.driver, script=script, scripted=monitor_finalization
    )
    return MonitorHarness(driver=faulty, namespace=harness.namespace, clock=harness.clock)


def _transitions_fail(harness: MonitorHarness, script: list[WriteBehaviour]) -> MonitorHarness:
    """A harness whose operation-status writes follow ``script``; everything else commits.

    The first entry is always consumed by the claim, because claiming is a status transition
    too. Scripts here therefore read ``[SUCCEED, <what the SUCCEEDED transition does>, ...]``.
    """

    faulty = FaultInjectingDriver(
        inner=harness.driver, write_script=script, write_scripted=operation_transitions
    )
    return MonitorHarness(driver=faulty, namespace=harness.namespace, clock=harness.clock)


def _fresh(harness: MonitorHarness) -> MonitorHarness:
    """A second harness over the same storage, standing in for a second worker process."""

    return MonitorHarness(driver=harness.driver, namespace=harness.namespace, clock=harness.clock)


async def _finalized(harness: MonitorHarness, job: MonitorOperationJob) -> bool:
    record = await harness.core.load_operation_agent_invocation(
        _scope(harness, job), job.invocation_id
    )
    return record is not None and record.outcome is AgentInvocationOutcome.SUCCEEDED


# ---------------------------------------------------------------------------------------
# CASE 1 -- the finalization write fails
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        pytest.param([TransactBehaviour.DEFINITE_FAILURE], id="definite_refusal"),
        pytest.param(
            [TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY, TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY],
            id="unprovable_outcome",
        ),
    ],
)
async def test_a_failed_finalization_leaves_a_resumable_operation_not_a_failed_one(
    harness: MonitorHarness, script: list[TransactBehaviour]
) -> None:
    """Codex's reproduction: every data step committed, and the run was recorded as a failure.

    Nothing about that operation had gone wrong. The model answered once, the answer was
    frozen, and every report, fact, and signal it implied was durable. Only the record saying
    so could not be written -- and settling the operation ``FAILED`` for that abandoned valid
    committed state and made the remainder reachable only by minting a new invocation.

    It is now an *interruption*, exactly like a failed data step, because that is what it is:
    one bounded deterministic write still owed against an answer that is already paid for.
    """

    _, job, _ = await _dispatched(harness)

    agent = _responder()
    interrupted = await _finalization_fails(harness, script).worker(agent).execute(job)

    assert interrupted.status is ApplicationOperationStatus.PENDING
    assert len(agent.invocations) == 1

    progress = await harness.core.load_monitor_progress(_scope(harness, job), job.invocation_id)
    assert progress is not None
    assert (progress.completed_steps, progress.total_steps) == (DATA_STEPS, DATA_STEPS + 1)
    assert not progress.is_complete, "the plan is not complete until finalization commits"
    assert not await _finalized(harness, job)

    resumed_agent = _responder()
    finished = await _fresh(harness).worker(resumed_agent).execute(job)

    assert finished.status is ApplicationOperationStatus.SUCCEEDED
    assert resumed_agent.invocations == [], "finalizing is deterministic work, not a question"
    assert len(finished.result_refs) == 3

    final = await harness.core.load_monitor_progress(_scope(harness, job), job.invocation_id)
    assert final is not None and final.is_complete
    assert await _finalized(harness, job)


async def test_complete_progress_always_means_the_invocation_record_is_durable(
    harness: MonitorHarness,
) -> None:
    """The invariant, stated directly: there is no state with one and not the other.

    The two are written by one transaction, so the only thing a test can do is assert they
    move together -- before finalization neither exists, after it both do.
    """

    _, job, _ = await _dispatched(harness)

    interrupted = await (
        _finalization_fails(harness, [TransactBehaviour.DEFINITE_FAILURE])
        .worker(_responder())
        .execute(job)
    )
    assert interrupted.status is ApplicationOperationStatus.PENDING
    progress = await harness.core.load_monitor_progress(_scope(harness, job), job.invocation_id)
    assert progress is not None and not progress.is_complete
    assert not await _finalized(harness, job)

    await _fresh(harness).worker(_responder()).execute(job)

    progress = await harness.core.load_monitor_progress(_scope(harness, job), job.invocation_id)
    assert progress is not None and progress.is_complete
    assert await _finalized(harness, job)


async def test_a_finalization_only_redelivery_writes_no_second_report_or_audit_row(
    harness: MonitorHarness,
) -> None:
    """The redelivery owes one step, so it must write one step and nothing else."""

    _, job, _ = await _dispatched(harness)
    await (
        _finalization_fails(harness, [TransactBehaviour.DEFINITE_FAILURE])
        .worker(_responder())
        .execute(job)
    )
    finished = await _fresh(harness).worker(_responder()).execute(job)
    assert finished.status is ApplicationOperationStatus.SUCCEEDED

    from chorus.domain.ids import CaseId
    from chorus.ports.pagination import PageRequest
    from chorus.ports.scopes import CaseScope

    for ref in finished.result_refs:
        scope = CaseScope(
            namespace=harness.namespace,
            community_id=harness.community_id,
            case_id=CaseId(ref),
        )
        case = await harness.core.load_case(scope)
        assert len(case.report_ids) == len(set(case.report_ids)) == 2
        assert len(case.fact_ids) == len(set(case.fact_ids)) == 2
        events = await harness.audit.read_case_events(scope, PageRequest(limit=100))
        assert len(events.items) == 1, "one decision, one audit row, however many deliveries"


# ---------------------------------------------------------------------------------------
# CASE 2 and 3 -- the status transition fails, or its response is lost
# ---------------------------------------------------------------------------------------


async def test_a_definitely_refused_success_transition_is_finished_by_a_redelivery(
    harness: MonitorHarness,
) -> None:
    """Finalization is durable and the status write is refused twice; the work still stands.

    This is the second half of Codex's tail. The old behaviour left the operation ``RUNNING``
    with a completed plan under it, and the only rule that ever looked at such an operation
    again was stale recovery -- which recorded it ``FAILED``. A redelivery now transcribes the
    outcome that is already durable.
    """

    _, job, _ = await _dispatched(harness)

    agent = _responder()
    stranded = await (
        _transitions_fail(
            harness,
            [
                WriteBehaviour.SUCCEED,
                WriteBehaviour.DEFINITE_FAILURE,
                WriteBehaviour.DEFINITE_FAILURE,
            ],
        )
        .worker(agent)
        .execute(job)
    )

    assert stranded.status is ApplicationOperationStatus.RUNNING
    assert len(agent.invocations) == 1
    assert await _finalized(harness, job), "the apply itself completed; only the status did not"

    resumed_agent = _responder()
    finished = await _fresh(harness).worker(resumed_agent).execute(job)

    assert finished.status is ApplicationOperationStatus.SUCCEEDED
    assert finished.error_code is None
    assert len(finished.result_refs) == 3
    assert resumed_agent.invocations == []


async def test_a_refused_success_transition_is_retried_inside_the_same_delivery(
    harness: MonitorHarness,
) -> None:
    """One refusal is settled without waiting for a redelivery at all."""

    _, job, _ = await _dispatched(harness)

    agent = _responder()
    finished = await (
        _transitions_fail(harness, [WriteBehaviour.SUCCEED, WriteBehaviour.DEFINITE_FAILURE])
        .worker(agent)
        .execute(job)
    )

    assert finished.status is ApplicationOperationStatus.SUCCEEDED
    assert len(agent.invocations) == 1


@pytest.mark.parametrize(
    ("behaviour", "applied"),
    [
        pytest.param(WriteBehaviour.AMBIGUOUS_AFTER_APPLY, True, id="lost_response_after_apply"),
        pytest.param(
            WriteBehaviour.AMBIGUOUS_WITHOUT_APPLY, False, id="lost_response_without_apply"
        ),
    ],
)
async def test_a_lost_success_transition_response_is_resolved_by_reading_the_operation(
    harness: MonitorHarness, behaviour: WriteBehaviour, applied: bool
) -> None:
    """Ambiguity about a *status* write is settled by reading, never by guessing.

    Both readings end in the same place, which is the point: the status write carries no
    payload, so re-applying it is free and skipping it is safe. What must never happen is the
    operation being recorded as failed because nobody could tell which of the two happened.
    """

    _, job, _ = await _dispatched(harness)

    agent = _responder()
    settled = await (
        _transitions_fail(harness, [WriteBehaviour.SUCCEED, behaviour]).worker(agent).execute(job)
    )

    assert settled.status is ApplicationOperationStatus.SUCCEEDED
    assert len(settled.result_refs) == 3
    assert len(agent.invocations) == 1
    assert await _finalized(harness, job)
    assert applied or settled.version >= 3, "a write that never landed was made again"


# ---------------------------------------------------------------------------------------
# CASE 4 -- staleness must never outrank a finished plan
# ---------------------------------------------------------------------------------------


async def test_a_finalized_running_operation_past_the_stale_window_succeeds(
    harness: MonitorHarness,
) -> None:
    """The exact inversion the old rule produced: finished work, recorded as stale failure.

    Stale recovery exists for an attempt nobody can account for. An operation whose invocation
    record says it succeeded is accounted for completely, so there is nothing for the rule to
    decide -- and deciding anyway meant the operations that had completed *everything* were
    exactly the ones being marked failed.
    """

    _, job, _ = await _dispatched(harness)

    stranded = await (
        _transitions_fail(
            harness,
            [
                WriteBehaviour.SUCCEED,
                WriteBehaviour.DEFINITE_FAILURE,
                WriteBehaviour.DEFINITE_FAILURE,
            ],
        )
        .worker(_responder())
        .execute(job)
    )
    assert stranded.status is ApplicationOperationStatus.RUNNING
    assert await _finalized(harness, job)

    harness.clock.instant += MAX_WORKER_EXECUTION_WINDOW + timedelta(seconds=1)

    agent = _responder()
    recovered = await _fresh(harness).worker(agent).execute(job)

    assert recovered.status is ApplicationOperationStatus.SUCCEEDED
    assert recovered.error_code != OPERATION_STALE_ERROR_CODE
    assert recovered.error_code is None
    assert agent.invocations == []


async def test_a_stale_running_operation_with_no_finalized_plan_still_fails(
    harness: MonitorHarness,
) -> None:
    """The conservative rule is preserved exactly where it still applies.

    A worker that vanished before finalizing left nothing saying the invocation succeeded, so
    "the worker is gone" and "the worker is still going" remain indistinguishable and the only
    safe automatic act is still to record that the attempt is over.
    """

    operation, job, locators = await _dispatched(harness)
    await harness.operations.claim(operation)
    harness.clock.instant += MAX_WORKER_EXECUTION_WINDOW + timedelta(seconds=1)

    agent = _responder()
    ended = await harness.worker(agent).execute(job)

    assert ended.status is ApplicationOperationStatus.FAILED
    assert ended.error_code == OPERATION_STALE_ERROR_CODE
    assert agent.invocations == []
    assert locators, "the scenario needs a real batch to be about"
