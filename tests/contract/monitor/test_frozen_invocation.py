"""The three frozen stages of a Monitor operation, exercised through the real worker.

Every test here runs :class:`MonitorOperationWorker` rather than calling ``RunMonitor``
directly, because the properties under test are the ones the worker owns: which operation
status an interruption leaves behind, whether a redelivery resumes or restarts, and how many
times a model is invoked across the whole lifecycle. A test that reached past the worker into
the use case could assert none of that -- which is how the earlier partial-progress test came
to pass while a redelivered operation was in fact terminal and unfinishable.

The rule every one of them is about:

    a partially applied Monitor operation must never need another model invocation.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from tests.fixtures.faults import (
    FaultInjectingDriver,
    TransactBehaviour,
    monitor_apply_steps,
)
from tests.fixtures.monitor import MonitorHarness
from tests.fixtures.monitor_answers import (
    THREE_GROUPS,
    GroupSpec,
    extension_answer,
    grouped_answer,
    new_case_answer,
    two_case_extension_answer,
)

from chorus.application.commands.run_monitor import NO_ATTRIBUTABLE_MESSAGES
from chorus.application.services.monitor_apply import PARTIAL_APPLY_CONFLICT_CODE
from chorus.application.services.monitor_snapshots import (
    FrozenMonitorInput,
    FrozenMonitorPlan,
    MonitorSnapshots,
)
from chorus.contracts.monitor import MonitorOutput
from chorus.domain.entities import (
    ApplicationOperation,
    ApplicationOperationKind,
    ApplicationOperationStatus,
    CaseState,
)
from chorus.domain.ids import CaseId, Sha256Digest
from chorus.infrastructure.local.monitor_agent import ScriptedMonitorAgent
from chorus.ports.agents import AgentTimeoutError, MonitorInvocation
from chorus.ports.ambient import AmbientMessage
from chorus.ports.errors import PersistenceErrorCode
from chorus.ports.operations import MonitorOperationJob
from chorus.ports.pagination import PageRequest
from chorus.ports.records import MessageFeedEntry, MonitorSnapshotKind
from chorus.ports.scopes import CaseScope, CommunityScope, OperationScope
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio

FOREIGN_HASH = Sha256Digest("sha256:" + "9" * 64)

FAR_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)
"""Past every instant the frozen corpus and these fixtures can produce."""


# ---------------------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------------------


async def _operation(
    harness: MonitorHarness,
    locators: tuple[MessageFeedEntry, ...] = (),
    *,
    kind: ApplicationOperationKind = ApplicationOperationKind.MONITOR,
) -> ApplicationOperation:
    """The operation as the route creates it: bound to its invocation and its locator set."""

    return await harness.bound_operation(locators, kind=kind)


def _job(
    harness: MonitorHarness,
    operation: ApplicationOperation,
    locators: tuple[MessageFeedEntry, ...],
) -> MonitorOperationJob:
    return harness.job_for(operation, locators)


def _responder(groups: tuple[GroupSpec, ...]) -> ScriptedMonitorAgent:
    return ScriptedMonitorAgent(
        responder=lambda invocation: grouped_answer(invocation.payload, groups)
    )


def _scope(harness: MonitorHarness, case_id: CaseId) -> CaseScope:
    return CaseScope(
        namespace=harness.namespace, community_id=harness.community_id, case_id=case_id
    )


def _operation_scope(harness: MonitorHarness, job: MonitorOperationJob) -> OperationScope:
    return OperationScope(namespace=harness.namespace, operation_id=job.operation_id)


def _snapshots(harness: MonitorHarness) -> MonitorSnapshots:
    return MonitorSnapshots(core=harness.core, unit_of_work=harness.unit_of_work)


async def _load_plan(
    harness: MonitorHarness, scope: OperationScope, invocation_id: UUID
) -> FrozenMonitorPlan | None:
    """Load the frozen plan the way the use case does: proved against its own frozen input.

    A plan is never readable on its own -- proving it *is* this invocation's plan requires the
    input it was reasoned about -- so a test that reached for one without the input would be
    exercising a path production does not have.
    """

    snapshots = _snapshots(harness)
    frozen_input = await snapshots.load_input(scope, invocation_id)
    if frozen_input is None:
        assert not await snapshots.has_plan(scope, invocation_id)
        return None
    return await snapshots.load_plan(scope, invocation_id, frozen_input=frozen_input)


async def _seeded(harness: MonitorHarness) -> tuple[MessageFeedEntry, ...]:
    await harness.seed()
    return await harness.ingest_feed()


def _interrupted_after(harness: MonitorHarness, script: list[TransactBehaviour]) -> MonitorHarness:
    """A harness whose *apply steps* follow ``script`` and whose other writes all succeed."""

    faulty = FaultInjectingDriver(
        inner=harness.driver, script=script, read_script=[], scripted=monitor_apply_steps
    )
    return MonitorHarness(driver=faulty, namespace=harness.namespace)


def _fresh(harness: MonitorHarness) -> MonitorHarness:
    """A second harness over the same storage, standing in for a second worker process."""

    return MonitorHarness(driver=harness.driver, namespace=harness.namespace, clock=harness.clock)


async def _ingest_fresh(
    harness: MonitorHarness, *, count: int, label: str
) -> tuple[MessageFeedEntry, ...]:
    """Ingest ``count`` new attributable messages strictly after everything already stored.

    The anchor of a Monitor batch is the earliest of its *new* messages, and the recent-context
    window is everything strictly before that. So two batches sharing a start instant would
    hide each other: the second run would not see the first run's messages, would not be
    offered the case they created, and could not extend it. Reading the newest stored message
    keeps each batch genuinely later than the last.
    """

    last = await _newest_instant(harness)
    pseudonyms = [seed.pseudonym for seed in harness.adapter.contributor_seeds]
    batch = tuple(
        AmbientMessage(
            adapter="SYNTHETIC",
            channel_message_id=f"{label}-{index:03d}",
            contributor_pseudonym=pseudonyms[index % len(pseudonyms)],
            sent_at=last + timedelta(minutes=index + 1),
            text=f"The lift stopped between floors again ({label} {index}).",
        )
        for index in range(count)
    )
    result = await harness.ingest_messages(batch, idempotency_key=f"fresh-{label}-000001")
    sent_at = {message.channel_message_id: message.sent_at for message in batch}
    return tuple(
        MessageFeedEntry(message_id=item.message_id, sent_at=sent_at[item.channel_message_id])
        for item in result.messages
    )


async def _newest_instant(harness: MonitorHarness) -> datetime:
    scope = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    newest = await harness.core.read_recent_messages(scope, before=FAR_FUTURE, limit=1)
    if newest:
        return newest[0].sent_at
    return harness.adapter.messages()[-1].sent_at


def _ids(locators: tuple[MessageFeedEntry, ...]) -> tuple[UUID, ...]:
    return tuple(locator.message_id.value for locator in locators)


# ---------------------------------------------------------------------------------------
# HIGH 2 -- a partial apply is resumable, and resuming costs no model call
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "expected_steps"),
    [
        pytest.param(
            [TransactBehaviour.SUCCEED, TransactBehaviour.DEFINITE_FAILURE],
            1,
            id="failure_after_the_first_step",
        ),
        pytest.param(
            [
                TransactBehaviour.SUCCEED,
                TransactBehaviour.SUCCEED,
                TransactBehaviour.DEFINITE_FAILURE,
            ],
            2,
            id="failure_in_the_middle",
        ),
        pytest.param(
            [
                TransactBehaviour.SUCCEED,
                TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY,
                TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY,
            ],
            1,
            id="unprovable_outcome_after_a_committed_step",
        ),
        pytest.param(
            [
                TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY,
                TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY,
            ],
            0,
            id="unprovable_outcome_on_the_first_step",
        ),
    ],
)
async def test_an_interrupted_apply_becomes_pending_and_resumes_without_a_second_model_call(
    harness: MonitorHarness,
    script: list[TransactBehaviour],
    expected_steps: int,
) -> None:
    """Codex's reproduction, plus every neighbouring shape of the same interruption.

    The old worker recorded any storage failure during apply as ``FAILED`` -- a verdict on an
    answer that was in fact valid, already frozen, and already partly committed. A redelivery
    then found a terminal operation and returned it unchanged, so the remaining steps could
    never be written by anything at all.
    """

    locators = await _seeded(harness)
    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)

    agent = _responder(THREE_GROUPS)
    interrupted = await _interrupted_after(harness, script).worker(agent).execute(job)

    assert interrupted.status is ApplicationOperationStatus.PENDING, (
        "an interrupted frozen plan is eligible to resume, not finished"
    )
    assert len(agent.invocations) == 1

    scope = _operation_scope(harness, job)
    progress = await harness.core.load_monitor_progress(scope, job.invocation_id)
    if expected_steps == 0:
        assert progress is None
    else:
        assert progress is not None
        assert progress.completed_steps == expected_steps

    # The redelivery, through a second agent instance so a model call would be visible.
    resumed_agent = _responder(THREE_GROUPS)
    finished = await _fresh(harness).worker(resumed_agent).execute(job)

    assert finished.status is ApplicationOperationStatus.SUCCEEDED
    assert resumed_agent.invocations == [], "the frozen plan is applied, never re-answered"
    assert len(finished.result_refs) == 3

    final = await harness.core.load_monitor_progress(scope, job.invocation_id)
    assert final is not None
    assert final.is_complete
    await _assert_no_duplicates(harness, tuple(CaseId(ref) for ref in finished.result_refs))


async def test_a_lost_response_whose_proof_says_it_did_not_commit_is_simply_finished(
    harness: MonitorHarness,
) -> None:
    """Ambiguity that can be *resolved* is not an interruption; it is a completed transaction.

    The distinction is the whole point of carrying a commit proof. An outcome the proof
    settles needs no lifecycle change at all -- the step is retried exactly once, the operation
    finishes, and the model is still invoked once.
    """

    locators = await _seeded(harness)
    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)

    agent = _responder(THREE_GROUPS)
    settled = await (
        _interrupted_after(harness, [TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY])
        .worker(agent)
        .execute(job)
    )

    assert settled.status is ApplicationOperationStatus.SUCCEEDED
    assert len(settled.result_refs) == 3
    assert len(agent.invocations) == 1


async def test_a_worker_that_vanished_mid_apply_is_finished_without_a_second_invocation(
    harness: MonitorHarness,
) -> None:
    """A crash records nothing at all, which is the case a worker cannot handle for itself.

    The operation is left ``RUNNING`` with durable progress under it. A redelivery inside the
    execution window leaves it strictly alone; once the window has passed the stale-recovery
    rule ends the attempt -- and the frozen plan is still there, so finishing it costs nothing.
    """

    locators = await _seeded(harness)
    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)

    crashing = _interrupted_after(
        harness, [TransactBehaviour.SUCCEED, TransactBehaviour.DEFINITE_FAILURE]
    )
    claimed = await crashing.operations.claim(operation)
    with pytest.raises(Exception):  # noqa: B017 - the shape of the interruption is not the point
        # Straight into the use case: a crash is precisely the case where no worker gets to
        # record an outcome, so routing this through one would be testing the wrong thing.
        await crashing.run_monitor(_responder(THREE_GROUPS)).execute(
            crashing.monitor_command(
                locators, invocation_id=job.invocation_id, operation_id=job.operation_id
            )
        )

    running = await harness.operations.load(
        namespace=harness.namespace, operation_id=job.operation_id
    )
    assert running.status is ApplicationOperationStatus.RUNNING
    assert running.version == claimed.version

    scope = _operation_scope(harness, job)
    progress = await harness.core.load_monitor_progress(scope, job.invocation_id)
    assert progress is not None and progress.completed_steps == 1

    # Inside the execution window a redelivery does nothing at all.
    untouched = await harness.worker(_responder(THREE_GROUPS)).execute(job)
    assert untouched.status is ApplicationOperationStatus.RUNNING

    # Past it, the lost attempt is recorded as over -- and only that.
    harness.clock.advance(seconds=int(timedelta(minutes=10).total_seconds()))
    ended = await harness.worker(_responder(THREE_GROUPS)).execute(job)
    assert ended.status is ApplicationOperationStatus.FAILED

    # The plan outlives the attempt, so finishing it is deterministic work and no model call.
    resumed_agent = _responder(THREE_GROUPS)
    result = await harness.run_monitor(resumed_agent).execute(
        harness.monitor_command(
            locators, invocation_id=job.invocation_id, operation_id=job.operation_id
        )
    )
    assert resumed_agent.invocations == []
    assert len(result.case_ids) == 3
    await _assert_no_duplicates(harness, result.case_ids)


async def _assert_no_duplicates(harness: MonitorHarness, case_ids: tuple[CaseId, ...]) -> None:
    """Reports, facts, signals, and audit rows each exist exactly once."""

    assert case_ids
    for case_id in case_ids:
        scope = _scope(harness, case_id)
        case = await harness.core.load_case(scope)
        assert len(case.report_ids) == len(set(case.report_ids)) == 2
        assert len(case.fact_ids) == len(set(case.fact_ids)) == 2
        reports = await harness.core.read_case_reports(scope, PageRequest(limit=100))
        assert len(reports.items) == 2
        facts = await harness.core.read_case_facts(scope, PageRequest(limit=100))
        assert len(facts.items) == 2
        events = await harness.audit.read_case_events(scope, PageRequest(limit=100))
        assert len(events.items) == 1, "one decision, one audit row, however many deliveries"

    signals = await harness.core.read_feed_signals(
        CommunityScope(namespace=harness.namespace, community_id=harness.community_id),
        PageRequest(limit=100),
    )
    message_ids = [signal.message_id for signal in signals.items]
    assert len(message_ids) == len(set(message_ids))


# ---------------------------------------------------------------------------------------
# HIGH 3 -- one invocation identity always means one MonitorInput
# ---------------------------------------------------------------------------------------


async def test_a_resumed_run_reasons_over_the_frozen_input_not_the_current_community(
    harness: MonitorHarness,
) -> None:
    """The reproduction: the first run saw no candidate summaries, and then the world moved.

    Rebuilding context on a redelivery is not a neutral optimisation to skip. The second build
    legitimately sees more -- the case the first run created is now an extension candidate --
    so one invocation identity would be answered against a payload the original reasoning
    never saw, and a frozen answer would be applied to a different question.
    """

    locators = await _seeded(harness)
    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)

    first_agent = _responder(THREE_GROUPS)
    interrupted = await (
        _interrupted_after(harness, [TransactBehaviour.SUCCEED, TransactBehaviour.DEFINITE_FAILURE])
        .worker(first_agent)
        .execute(job)
    )
    assert interrupted.status is ApplicationOperationStatus.PENDING

    first_input = first_agent.invocations[0].payload
    assert first_input.candidate_case_summaries == (), "nothing existed to extend yet"

    # The world moved: the committed step created a case, and its feed signals now make that
    # case an extension candidate for exactly these messages.
    signals = await harness.core.read_feed_signals(
        CommunityScope(namespace=harness.namespace, community_id=harness.community_id),
        PageRequest(limit=100),
    )
    assert signals.items, "the committed step really did change what a rebuild would see"

    frozen = await _load_input(harness, job)
    assert frozen.invocation.payload == first_input

    resumed_agent = _responder(THREE_GROUPS)
    finished = await _fresh(harness).worker(resumed_agent).execute(job)

    assert finished.status is ApplicationOperationStatus.SUCCEEDED
    assert resumed_agent.invocations == []

    reloaded = await _load_input(harness, job)
    assert reloaded.input_hash == frozen.input_hash
    assert reloaded.invocation.model_dump_json() == frozen.invocation.model_dump_json()
    assert reloaded.invocation.payload.candidate_case_summaries == ()


async def test_a_redelivery_after_a_lost_model_answer_reuses_the_frozen_input(
    harness: MonitorHarness,
) -> None:
    """The failure happens *before* any result exists, which is the harder half.

    Nothing has been applied and nothing validated, so a rebuilt context would be accepted
    without complaint -- and would quietly be a different question. The snapshot is what makes
    the second attempt ask the first attempt's question.
    """

    locators = await _seeded(harness)
    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)

    lost = ScriptedMonitorAgent(
        responder=lambda invocation: grouped_answer(invocation.payload, THREE_GROUPS),
        failures=[AgentTimeoutError(), AgentTimeoutError()],
    )
    failed = await harness.worker(lost).execute(job)

    assert failed.status is ApplicationOperationStatus.FAILED
    assert len(lost.invocations) == 2, "one licensed retry, both against the same payload"
    assert lost.invocations[0].payload == lost.invocations[1].payload

    frozen = await _load_input(harness, job)
    assert frozen.invocation.payload == lost.invocations[0].payload

    # Later ambient traffic arrives, so a rebuilt context would now differ.
    await _ingest_fresh(harness, count=2, label="later")

    replayed = _responder(THREE_GROUPS)
    await harness.worker(replayed).execute(job)

    reloaded = await _load_input(harness, job)
    assert reloaded.input_hash == frozen.input_hash
    assert reloaded.invocation.model_dump_json() == frozen.invocation.model_dump_json()


async def test_a_command_reusing_one_invocation_identity_for_other_work_is_refused(
    harness: MonitorHarness,
) -> None:
    """Answering a different command from a frozen payload would be the quiet failure."""

    locators = await _seeded(harness)
    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)
    await harness.worker(_responder(THREE_GROUPS)).execute(job)

    agent = _responder(THREE_GROUPS)
    settled = await harness.worker(agent).execute(replace(job, message_locators=locators[:2]))

    assert settled.status is ApplicationOperationStatus.SUCCEEDED, "the first run stands"
    assert agent.invocations == []


async def _load_input(harness: MonitorHarness, job: MonitorOperationJob) -> FrozenMonitorInput:
    frozen = await _snapshots(harness).load_input(_operation_scope(harness, job), job.invocation_id)
    assert frozen is not None
    return frozen


# ---------------------------------------------------------------------------------------
# HIGH 5 -- capacity is a property of the resulting case, not of the proposal
# ---------------------------------------------------------------------------------------


async def test_an_answer_that_would_overfill_an_existing_case_writes_nothing_at_all(
    harness: MonitorHarness,
) -> None:
    """Seven existing facts plus a hundred proposed ones is refused before any mutation.

    Counting only what an answer proposes is not a capacity check. The frozen bound is on the
    *case*, so the number that matters is the one the case would end at -- and discovering
    that at the storage layer, mid-apply, would mean earlier steps had already committed.
    """

    existing = await _case_with_facts(harness, facts=7, label="overfill-seed")
    before = await harness.core.load_case(_scope(harness, existing))
    assert len(before.fact_ids) == 7

    settled, job = await _extend(harness, existing, messages=25, facts=100, label="overfill")

    assert settled.status is ApplicationOperationStatus.FAILED
    assert settled.error_code == PersistenceErrorCode.MODEL_LIMIT_EXCEEDED.value

    after = await harness.core.load_case(_scope(harness, existing))
    assert len(after.fact_ids) == 7, "not one fact was written"
    assert after.report_ids == before.report_ids, "and not one report"
    assert after.version == before.version
    assert (
        await harness.core.load_monitor_progress(_operation_scope(harness, job), job.invocation_id)
        is None
    ), "a refused answer leaves no apply progress"
    assert await _load_plan(harness, _operation_scope(harness, job), job.invocation_id) is None, (
        "and no frozen plan, because there was nothing legal to freeze"
    )


async def test_an_answer_that_exactly_fills_the_remaining_capacity_is_allowed(
    harness: MonitorHarness,
) -> None:
    """Seven existing plus ninety-three new is one hundred, and one hundred is permitted."""

    existing = await _case_with_facts(harness, facts=7, label="exact-seed")

    settled, _ = await _extend(harness, existing, messages=25, facts=93, label="exact")

    assert settled.status is ApplicationOperationStatus.SUCCEEDED, settled.error_code
    case = await harness.core.load_case(_scope(harness, existing))
    assert len(case.fact_ids) == 100


async def test_a_replayed_answer_at_the_bound_does_not_count_its_own_slots_twice(
    harness: MonitorHarness,
) -> None:
    """Deterministic slots make the projected total a set rather than a running sum."""

    existing = await _case_with_facts(harness, facts=7, label="replay-seed")
    locators = await _ingest_fresh(harness, count=25, label="replay-extend")

    first, _ = await _extend_with(harness, existing, locators, facts=93)
    assert first.status is ApplicationOperationStatus.SUCCEEDED, first.error_code

    second, _ = await _extend_with(harness, existing, locators, facts=93)

    assert second.status is ApplicationOperationStatus.SUCCEEDED, (
        "an exact replay of an answer that fit still fits"
    )
    case = await harness.core.load_case(_scope(harness, existing))
    assert len(case.fact_ids) == 100


async def _case_with_facts(harness: MonitorHarness, *, facts: int, label: str) -> CaseId:
    """Discover one case carrying exactly ``facts`` facts, through the ordinary apply path."""

    await harness.seed()
    locators = await _ingest_fresh(harness, count=2, label=label)
    message_ids = _ids(locators)
    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)
    agent = ScriptedMonitorAgent(
        responder=lambda invocation: new_case_answer(
            invocation.payload,
            message_ids=message_ids,
            fact_count=facts,
            group_ref=label,
            title=f"Discovered by {label}",
        )
    )

    settled = await harness.worker(agent).execute(job)

    assert settled.status is ApplicationOperationStatus.SUCCEEDED, settled.error_code
    assert len(settled.result_refs) == 1
    return CaseId(settled.result_refs[0])


async def _extend(
    harness: MonitorHarness, case_id: CaseId, *, messages: int, facts: int, label: str
) -> tuple[ApplicationOperation, MonitorOperationJob]:
    locators = await _ingest_fresh(harness, count=messages, label=label)
    return await _extend_with(harness, case_id, locators, facts=facts)


async def _extend_with(
    harness: MonitorHarness,
    case_id: CaseId,
    locators: tuple[MessageFeedEntry, ...],
    *,
    facts: int,
) -> tuple[ApplicationOperation, MonitorOperationJob]:
    message_ids = _ids(locators)
    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)
    agent = ScriptedMonitorAgent(
        responder=lambda invocation: extension_answer(
            invocation.payload,
            case_id=case_id.value,
            message_ids=message_ids,
            fact_count=facts,
        )
    )
    return await harness.worker(agent).execute(job), job


# ---------------------------------------------------------------------------------------
# MEDIUM -- a job is bound to its operation before anything is claimed
# ---------------------------------------------------------------------------------------


async def test_a_job_for_another_command_family_is_neither_claimed_nor_executed(
    harness: MonitorHarness,
) -> None:
    """A ``PROPOSE_ACTION`` operation handed to the Monitor worker is left entirely alone."""

    locators = await _seeded(harness)
    foreign = await _operation(harness, kind=ApplicationOperationKind.PROPOSE_ACTION)
    job = _job(harness, foreign, locators)
    agent = _responder(THREE_GROUPS)

    returned = await harness.worker(agent).execute(job)

    assert returned.status is ApplicationOperationStatus.PENDING
    assert returned.kind is ApplicationOperationKind.PROPOSE_ACTION
    assert returned.version == foreign.version, "the operation was not even claimed"
    assert returned.result_refs == ()
    assert agent.invocations == []
    assert (
        await harness.core.load_operation_agent_invocation(
            _operation_scope(harness, job), job.invocation_id
        )
        is None
    ), "no Monitor result was recorded against somebody else's operation"


@pytest.mark.parametrize("field", ["actor_id_hash", "request_hash"])
async def test_a_job_that_disagrees_with_its_operation_is_refused(
    harness: MonitorHarness, field: str
) -> None:
    """Identity is bound before the claim, so a misrouted delivery ends nothing."""

    locators = await _seeded(harness)
    operation = await _operation(harness, locators)
    original = _job(harness, operation, locators)
    job = (
        replace(original, actor_id_hash=FOREIGN_HASH)
        if field == "actor_id_hash"
        else replace(original, request_hash=FOREIGN_HASH)
    )
    agent = _responder(THREE_GROUPS)

    returned = await harness.worker(agent).execute(job)

    assert returned.status is ApplicationOperationStatus.PENDING
    assert returned.version == operation.version
    assert agent.invocations == []


async def test_a_second_invocation_identity_cannot_take_over_a_started_operation(
    harness: MonitorHarness,
) -> None:
    """One operation reaches one model invocation, whatever a delivered job claims."""

    locators = await _seeded(harness)
    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)
    await (
        _interrupted_after(harness, [TransactBehaviour.SUCCEED, TransactBehaviour.DEFINITE_FAILURE])
        .worker(_responder(THREE_GROUPS))
        .execute(job)
    )

    agent = _responder(THREE_GROUPS)
    returned = await harness.worker(agent).execute(replace(job, invocation_id=uuid4()))

    assert agent.invocations == []
    assert returned.status is ApplicationOperationStatus.PENDING


# ---------------------------------------------------------------------------------------
# MEDIUM -- an unattributable batch is ambient noise, not a fault
# ---------------------------------------------------------------------------------------


async def test_a_batch_with_no_attributable_message_succeeds_as_a_no_op(
    harness: MonitorHarness,
) -> None:
    """No model call, no durable state, no crash, and no operation stranded in ``RUNNING``."""

    await harness.seed()
    anonymous = AmbientMessage(
        adapter="SYNTHETIC",
        channel_message_id="anonymous-001",
        contributor_pseudonym=None,
        sent_at=harness.adapter.messages()[-1].sent_at + timedelta(minutes=1),
        text="Someone left a note about the lift on the notice board.",
    )
    ingested = await harness.ingest_messages((anonymous,), idempotency_key="anonymous-000001")
    locator = MessageFeedEntry(
        message_id=ingested.messages[0].message_id, sent_at=anonymous.sent_at
    )
    operation = await _operation(harness, (locator,))
    job = _job(harness, operation, (locator,))
    agent = _responder(THREE_GROUPS)

    settled = await harness.worker(agent).execute(job)

    assert settled.status is ApplicationOperationStatus.SUCCEEDED
    assert settled.result_refs == ()
    assert settled.error_code is None
    assert agent.invocations == [], "there was nothing to reason about"

    signals = await harness.core.read_feed_signals(
        CommunityScope(namespace=harness.namespace, community_id=harness.community_id),
        PageRequest(limit=100),
    )
    assert signals.items == ()
    scope = _operation_scope(harness, job)
    assert await harness.core.load_monitor_progress(scope, job.invocation_id) is None
    assert await _load_plan(harness, scope, job.invocation_id) is None


def test_the_no_op_reason_code_is_a_closed_safe_code() -> None:
    assert NO_ATTRIBUTABLE_MESSAGES == "NO_ATTRIBUTABLE_MESSAGES"
    assert NO_ATTRIBUTABLE_MESSAGES.replace("_", "").isalnum()


# ---------------------------------------------------------------------------------------
# Multi-step concurrency -- progress never overrides a version assumption
# ---------------------------------------------------------------------------------------


async def test_a_case_changed_between_two_steps_ends_as_a_partial_apply_conflict(
    harness: MonitorHarness,
) -> None:
    """Progress says how far we got. It does not say the world stood still while we got there.

    Step one commits; something else then bumps the case step two expects. The resumed attempt
    must neither re-plan under the old invocation nor ask the model again: it records a
    conflict that says plainly the operation was not atomic, and leaves the state earlier
    steps committed exactly as it is.
    """

    first_case = await _case_with_facts(harness, facts=1, label="concurrent-a")
    second_case = await _case_with_facts(harness, facts=1, label="concurrent-b")
    locators = await _ingest_fresh(harness, count=2, label="concurrent-extend")
    message_ids = _ids(locators)

    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)
    links = ((first_case.value, message_ids[0]), (second_case.value, message_ids[1]))
    responder = lambda invocation: two_case_extension_answer(invocation.payload, links=links)  # noqa: E731

    interrupted = await (
        _interrupted_after(harness, [TransactBehaviour.SUCCEED, TransactBehaviour.DEFINITE_FAILURE])
        .worker(ScriptedMonitorAgent(responder=responder))
        .execute(job)
    )
    assert interrupted.status is ApplicationOperationStatus.PENDING

    plan = await _load_plan(harness, _operation_scope(harness, job), job.invocation_id)
    assert plan is not None
    assert len(plan.steps) == 2
    outstanding = CaseId(plan.steps[1].case_id)

    await _bump_case(harness, outstanding)

    agent = ScriptedMonitorAgent(responder=responder)
    settled = await _fresh(harness).worker(agent).execute(job)

    assert settled.status is ApplicationOperationStatus.FAILED
    assert settled.error_code == PARTIAL_APPLY_CONFLICT_CODE
    assert agent.invocations == [], "a conflict is never resolved by asking again"

    progress = await harness.core.load_monitor_progress(
        _operation_scope(harness, job), job.invocation_id
    )
    assert progress is not None
    assert progress.completed_steps == 1, "the committed step stands and no further step ran"


async def test_a_case_made_terminal_between_two_steps_is_a_partial_apply_conflict(
    harness: MonitorHarness,
) -> None:
    """The other way a case can stop being extendable: it finished.

    Version staleness and state ineligibility are different refusals with the same duty. A case
    that was resolved while this invocation was mid-apply is not a case whose remaining step can
    be "caught up" -- appending a report to a closed case would reopen a decision nobody made.
    So the resumed attempt refuses, says so as a partial-apply conflict because committed work
    stands, and neither re-plans under the old invocation nor asks the model again.
    """

    first_case = await _case_with_facts(harness, facts=1, label="terminal-a")
    second_case = await _case_with_facts(harness, facts=1, label="terminal-b")
    locators = await _ingest_fresh(harness, count=2, label="terminal-extend")
    message_ids = _ids(locators)

    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)
    links = ((first_case.value, message_ids[0]), (second_case.value, message_ids[1]))
    responder = lambda invocation: two_case_extension_answer(invocation.payload, links=links)  # noqa: E731

    interrupted = await (
        _interrupted_after(harness, [TransactBehaviour.SUCCEED, TransactBehaviour.DEFINITE_FAILURE])
        .worker(ScriptedMonitorAgent(responder=responder))
        .execute(job)
    )
    assert interrupted.status is ApplicationOperationStatus.PENDING

    scope = _operation_scope(harness, job)
    plan = await _load_plan(harness, scope, job.invocation_id)
    assert plan is not None and len(plan.steps) == 2
    outstanding = CaseId(plan.steps[1].case_id)
    committed = CaseId(plan.steps[0].case_id)
    before = await harness.core.load_case(_scope(harness, committed))

    await _resolve_case(harness, outstanding)
    resolved = await harness.core.load_case(_scope(harness, outstanding))
    assert resolved.state is CaseState.RESOLVED

    agent = ScriptedMonitorAgent(responder=responder)
    settled = await _fresh(harness).worker(agent).execute(job)

    assert settled.status is ApplicationOperationStatus.FAILED
    assert settled.error_code == PARTIAL_APPLY_CONFLICT_CODE
    assert agent.invocations == [], "a terminal case is never re-planned by asking again"

    after = await harness.core.load_case(_scope(harness, committed))
    assert after.version == before.version, "the step that committed stays exactly as it was"
    assert after.report_ids == before.report_ids
    progress = await harness.core.load_monitor_progress(scope, job.invocation_id)
    assert progress is not None and progress.completed_steps == 1
    assert not progress.is_complete
    still_resolved = await harness.core.load_case(_scope(harness, outstanding))
    assert still_resolved.version == resolved.version, "and nothing was written to the other"


async def test_resuming_an_extension_does_not_read_its_own_committed_step_as_interference(
    harness: MonitorHarness,
) -> None:
    """The version an agent saw is the version *before* this invocation started moving it.

    Two existing cases, one step each. The first step commits and bumps its case; the resumed
    attempt then checks that case against the version the agent was shown, which is now one
    behind -- our own doing. Reading that as concurrent interference would make every
    multi-step extension permanently unfinishable, and the operation could never leave
    ``PENDING`` however many times it was redelivered.
    """

    first_case = await _case_with_facts(harness, facts=1, label="rebind-a")
    second_case = await _case_with_facts(harness, facts=1, label="rebind-b")
    before = {
        case_id: (await harness.core.load_case(_scope(harness, case_id))).version
        for case_id in (first_case, second_case)
    }
    locators = await _ingest_fresh(harness, count=2, label="rebind-extend")
    message_ids = _ids(locators)

    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)
    links = ((first_case.value, message_ids[0]), (second_case.value, message_ids[1]))

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        return two_case_extension_answer(invocation.payload, links=links)

    interrupted = await (
        _interrupted_after(harness, [TransactBehaviour.SUCCEED, TransactBehaviour.DEFINITE_FAILURE])
        .worker(ScriptedMonitorAgent(responder=responder))
        .execute(job)
    )
    assert interrupted.status is ApplicationOperationStatus.PENDING

    agent = ScriptedMonitorAgent(responder=responder)
    finished = await _fresh(harness).worker(agent).execute(job)

    assert finished.status is ApplicationOperationStatus.SUCCEEDED, finished.error_code
    assert agent.invocations == []
    for case_id, version in before.items():
        case = await harness.core.load_case(_scope(harness, case_id))
        assert case.version == version + 1, "each case advanced by exactly its own one step"


async def test_a_case_spanning_several_steps_is_audited_once_across_a_resume(
    harness: MonitorHarness,
) -> None:
    """Which step is a case's *first* comes from the frozen plan, not from the resumed one.

    A resumed attempt derives a shorter plan, and the first remaining step of a case sits at
    index zero in it. Reading that as "this is the case's first step" appends a second audit
    event and a second case-scoped invocation record for one decision -- and because the audit
    row is addressed by its own occurrence instant, the duplicate lands at a different sort key
    where nothing conditional stops it.
    """

    existing = await _case_with_facts(harness, facts=1, label="multistep-seed")
    locators = await _ingest_fresh(harness, count=25, label="multistep")
    message_ids = _ids(locators)

    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        return extension_answer(
            invocation.payload,
            case_id=existing.value,
            message_ids=message_ids,
            fact_count=50,
        )

    interrupted = await (
        _interrupted_after(harness, [TransactBehaviour.SUCCEED, TransactBehaviour.DEFINITE_FAILURE])
        .worker(ScriptedMonitorAgent(responder=responder))
        .execute(job)
    )
    assert interrupted.status is ApplicationOperationStatus.PENDING

    plan = await _load_plan(harness, _operation_scope(harness, job), job.invocation_id)
    assert plan is not None
    assert len(plan.steps) > 2, "the scenario only means something if one case spans steps"
    assert [step.first_for_case for step in plan.steps].count(True) == 1

    agent = ScriptedMonitorAgent(responder=responder)
    finished = await _fresh(harness).worker(agent).execute(job)

    assert finished.status is ApplicationOperationStatus.SUCCEEDED, finished.error_code
    assert agent.invocations == []

    scope = _scope(harness, existing)
    events = await harness.audit.read_case_events(scope, PageRequest(limit=100))
    # The case already carries the audit row from its own discovery, so the count that matters
    # is how many rows *this* invocation caused.
    caused = [event for event in events.items if event.causation_id == job.invocation_id]
    assert len(caused) == 1, "one linkage decision, one audit row, two deliveries"
    assert caused[0].event_type == "report.linked"
    case = await harness.core.load_case(scope)
    assert len(case.fact_ids) == 51
    assert len(case.report_ids) == len(set(case.report_ids))


async def _bump_case(harness: MonitorHarness, case_id: CaseId) -> None:
    """An unrelated command advances the case, exactly as a concurrent write would."""

    await _rewrite_case(harness, case_id)


async def _resolve_case(harness: MonitorHarness, case_id: CaseId) -> None:
    """An unrelated command moves the case to a terminal state, as a resolution would."""

    await _rewrite_case(harness, case_id, state=CaseState.RESOLVED)


async def _rewrite_case(
    harness: MonitorHarness, case_id: CaseId, *, state: CaseState | None = None
) -> None:
    scope = _scope(harness, case_id)
    case = await harness.core.load_case(scope)
    updated = replace(
        case,
        state=case.state if state is None else state,
        version=case.version + 1,
        updated_at=harness.clock.now(),
    )
    await harness.unit_of_work.commit(
        TransactionPlan(
            name="external-case-bump",
            operations=(
                harness.core.stage_update_case(scope, updated, expected_version=case.version),
            ),
            audit_required=False,
        )
    )


# ---------------------------------------------------------------------------------------
# Snapshot mechanics
# ---------------------------------------------------------------------------------------


async def test_both_frozen_stages_are_persisted_under_the_operation_partition(
    harness: MonitorHarness,
) -> None:
    locators = await _seeded(harness)
    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)

    await harness.worker(_responder(THREE_GROUPS)).execute(job)

    scope = _operation_scope(harness, job)
    for kind in (MonitorSnapshotKind.MONITOR_INPUT, MonitorSnapshotKind.MONITOR_PLAN):
        manifest = await harness.core.load_monitor_snapshot_manifest(
            scope, kind=kind, invocation_id=job.invocation_id
        )
        assert manifest is not None
        assert manifest.chunk_count >= 1
        assert manifest.community_id == harness.community_id
        chunks = await harness.core.load_monitor_snapshot_chunks(scope, manifest)
        assert len(chunks) == manifest.chunk_count
        rendered = "".join(chunk.content.reveal() for chunk in chunks)
        assert len(rendered.encode("utf-8")) == manifest.byte_length


async def test_a_snapshot_never_renders_its_content_in_a_repr(harness: MonitorHarness) -> None:
    """A snapshot holds private material, so nothing may print it by accident."""

    locators = await _seeded(harness)
    operation = await _operation(harness, locators)
    job = _job(harness, operation, locators)
    await harness.worker(_responder(THREE_GROUPS)).execute(job)

    scope = _operation_scope(harness, job)
    manifest = await harness.core.load_monitor_snapshot_manifest(
        scope, kind=MonitorSnapshotKind.MONITOR_INPUT, invocation_id=job.invocation_id
    )
    assert manifest is not None
    chunks = await harness.core.load_monitor_snapshot_chunks(scope, manifest)

    rendered = repr(chunks) + repr(manifest)
    for message in harness.adapter.messages():
        assert message.text not in rendered
