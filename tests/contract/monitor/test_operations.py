"""Durable operation state: claim, replay, terminal status, and recorded failure.

Asynchronous delivery may repeat, so the interesting cases are all about a *second* arrival:
one that races the first, one that arrives after success, one that arrives after a failure.
None of them may cause a second invocation of the model over the same private payload.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from tests.fixtures.monitor import MonitorHarness

from chorus.application.commands.run_monitor_operation import MonitorOperationWorker
from chorus.domain.entities import ApplicationOperationStatus
from chorus.domain.ids import (
    CommunityId,
    MessageId,
    Namespace,
    OperationId,
    Sha256Digest,
)
from chorus.infrastructure.local.dispatch import (
    InProcessOperationDispatcher,
    RecordingOperationDispatcher,
)
from chorus.infrastructure.local.monitor_agent import (
    LexicalFakeMonitorAgent,
    ScriptedMonitorAgent,
    build_lexical_output,
)
from chorus.ports.agents import (
    AgentContractViolationError,
    AgentDependencyError,
    AgentRejection,
    AgentTimeoutError,
)
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.operations import MonitorOperationJob
from chorus.ports.records import MessageFeedEntry

pytestmark = pytest.mark.anyio


async def _job(harness: MonitorHarness) -> MonitorOperationJob:
    await harness.seed()
    locators = await harness.ingest_feed()
    command = harness.monitor_command(locators)
    operation = await harness.bound_operation(locators, invocation_id=command.invocation_id)
    return harness.job_for(operation, locators, correlation_id=command.correlation_id)


async def test_a_successful_operation_records_the_case_it_produced(
    harness: MonitorHarness,
) -> None:
    job = await _job(harness)

    finished = await harness.worker(LexicalFakeMonitorAgent()).execute(job)

    assert finished.status is ApplicationOperationStatus.SUCCEEDED
    assert len(finished.result_refs) == 1
    assert finished.error_code is None


async def test_a_completed_operation_replays_without_invoking_the_model_again(
    harness: MonitorHarness,
) -> None:
    job = await _job(harness)
    agent = ScriptedMonitorAgent(responder=build_lexical_output)
    worker = harness.worker(agent)

    first = await worker.execute(job)
    again = await worker.execute(job)

    assert first.status is ApplicationOperationStatus.SUCCEEDED
    assert again.status is ApplicationOperationStatus.SUCCEEDED
    assert again.version == first.version
    assert len(agent.invocations) == 1


async def test_only_one_of_two_concurrent_deliveries_claims_the_operation(
    harness: MonitorHarness,
) -> None:
    """Two workers holding the same version: the version condition decides between them."""

    job = await _job(harness)
    operation = await harness.operations.load(
        namespace=harness.namespace, operation_id=job.operation_id
    )

    claimed = await harness.operations.claim(operation)
    # A second worker arrives a moment later holding the same stale version. The moment
    # matters: an identical retry of one request is deduplicated by design, and what has to
    # be rejected is a genuinely *second* write against a version that has already moved.
    harness.clock.advance(seconds=1)

    assert claimed.status is ApplicationOperationStatus.RUNNING
    with pytest.raises(PersistenceConflictError):
        await harness.operations.claim(operation)


async def test_a_redelivery_that_loses_the_claim_stops_instead_of_invoking(
    harness: MonitorHarness,
) -> None:
    job = await _job(harness)
    operation = await harness.operations.load(
        namespace=harness.namespace, operation_id=job.operation_id
    )
    await harness.operations.claim(operation)
    agent = ScriptedMonitorAgent(responder=build_lexical_output)

    observed = await MonitorOperationWorker(
        operations=harness.operations, run_monitor=harness.run_monitor(agent)
    ).execute(job)

    assert observed.status is ApplicationOperationStatus.RUNNING
    assert agent.invocations == []


async def test_a_timeout_that_exhausts_its_one_retry_fails_the_operation(
    harness: MonitorHarness,
) -> None:
    job = await _job(harness)
    agent = ScriptedMonitorAgent(
        responder=build_lexical_output,
        failures=[AgentTimeoutError(), AgentTimeoutError()],
    )

    finished = await harness.worker(agent).execute(job)

    assert finished.status is ApplicationOperationStatus.FAILED
    assert finished.error_code == "AGENT_TIMEOUT"
    assert finished.result_refs == ()
    assert len(agent.invocations) == 2


async def test_an_invocation_dependency_failure_is_recorded_not_raised(
    harness: MonitorHarness,
) -> None:
    job = await _job(harness)
    agent = ScriptedMonitorAgent(
        responder=build_lexical_output,
        failures=[
            AgentDependencyError(retryable=False),
            AgentDependencyError(retryable=False),
        ],
    )

    finished = await harness.worker(agent).execute(job)

    assert finished.status is ApplicationOperationStatus.FAILED
    assert finished.error_code == "AGENT_DEPENDENCY_ERROR"
    assert len(agent.invocations) == 1


async def test_a_contract_violation_fails_the_operation_and_writes_no_case(
    harness: MonitorHarness,
) -> None:
    job = await _job(harness)
    agent = ScriptedMonitorAgent(
        responder=build_lexical_output,
        failures=[AgentContractViolationError((AgentRejection.SOURCE_SPAN_INVALID,))],
    )

    finished = await harness.worker(agent).execute(job)

    assert finished.status is ApplicationOperationStatus.FAILED
    assert finished.error_code == "AGENT_CONTRACT_VIOLATION"
    assert finished.result_refs == ()


async def test_a_failed_operation_is_terminal_and_never_re_runs(
    harness: MonitorHarness,
) -> None:
    job = await _job(harness)
    agent = ScriptedMonitorAgent(
        responder=build_lexical_output,
        failures=[AgentContractViolationError((AgentRejection.SCHEMA_INVALID,))],
    )
    worker = harness.worker(agent)

    failed = await worker.execute(job)
    again = await worker.execute(job)

    assert again.status is ApplicationOperationStatus.FAILED
    assert again.version == failed.version
    assert len(agent.invocations) == 1


async def test_a_recording_dispatcher_hands_over_a_job_without_running_it(
    harness: MonitorHarness,
) -> None:
    job = await _job(harness)
    dispatcher = RecordingOperationDispatcher()

    await dispatcher.dispatch_monitor(job)

    assert dispatcher.jobs == [job]
    operation = await harness.operations.load(
        namespace=harness.namespace, operation_id=job.operation_id
    )
    assert operation.status is ApplicationOperationStatus.PENDING


async def test_an_in_process_dispatcher_runs_the_job_out_of_band(
    harness: MonitorHarness,
) -> None:
    job = await _job(harness)
    dispatcher = InProcessOperationDispatcher(worker=harness.worker(LexicalFakeMonitorAgent()))

    await dispatcher.dispatch_monitor(job)
    await dispatcher.drain()

    operation = await harness.operations.load(
        namespace=harness.namespace, operation_id=job.operation_id
    )
    assert operation.status is ApplicationOperationStatus.SUCCEEDED


async def test_an_operation_job_must_name_each_message_once() -> None:
    locator = MessageFeedEntry(
        message_id=MessageId(uuid4()), sent_at=datetime(2030, 1, 8, tzinfo=UTC)
    )
    with pytest.raises(ValueError, match="once"):
        MonitorOperationJob(
            operation_id=OperationId(uuid4()),
            namespace=Namespace("TEST_JOB"),
            community_id=CommunityId(uuid4()),
            invocation_id=uuid4(),
            correlation_id=uuid4(),
            actor_id_hash=Sha256Digest("sha256:" + "e" * 64),
            request_hash=Sha256Digest("sha256:" + "f" * 64),
            message_locators=(locator, locator),
        )
