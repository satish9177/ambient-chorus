"""Two workers, one operation: exactly one wins, and a lost worker is ended exactly once.

Both properties are about races that a *deterministic* clock makes worse rather than better.
The operation claim used to be a transaction whose client request token was derived from the
plan, and DynamoDB treats a repeat of that token inside ten minutes as an idempotent replay --
so two workers composing the byte-identical claim at one injected instant were both told they
had won, and both would have invoked the model. The fix is a bare conditional write, and the
test that proves it has to hold the clock still rather than advance it.

The recovery half is the mirror image. A ``RUNNING`` operation is ambiguous by construction, so
a redelivery may do exactly one thing once the claim is older than any worker could still be:
record that the attempt is over. It may never start a second invocation from that state, and a
slow original worker may never overwrite the terminal record with a late success.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from datetime import timedelta

import pytest
from tests.fixtures.monitor import MonitorHarness

from chorus.application.operations import (
    MAX_WORKER_EXECUTION_WINDOW,
    OPERATION_STALE_ERROR_CODE,
    ApplicationOperations,
)
from chorus.domain.entities import ApplicationOperation
from chorus.domain.entities import ApplicationOperationStatus as Status
from chorus.domain.errors import StateTransitionError
from chorus.domain.ids import Sha256Digest
from chorus.infrastructure.local.monitor_agent import LexicalFakeMonitorAgent
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.operations import MonitorOperationJob
from chorus.ports.records import MessageFeedEntry
from chorus.ports.scopes import NamespaceScope
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio

REQUEST_HASH = Sha256Digest("sha256:" + "a" * 64)
ACTOR_HASH = Sha256Digest("sha256:" + "b" * 64)


async def _pending(harness: MonitorHarness) -> ApplicationOperation:
    await harness.seed()
    return await harness.bound_operation(actor_id_hash=ACTOR_HASH, request_hash=REQUEST_HASH)


async def _pending_over(
    harness: MonitorHarness, locators: tuple[MessageFeedEntry, ...]
) -> ApplicationOperation:
    return await harness.bound_operation(
        locators, actor_id_hash=ACTOR_HASH, request_hash=REQUEST_HASH
    )


# ---------------------------------------------------------------------------------------
# M12 -- exclusivity at one clock instant
# ---------------------------------------------------------------------------------------


async def test_two_claims_at_the_exact_same_clock_instant_produce_one_winner(
    harness: MonitorHarness,
) -> None:
    """No time passes between the two attempts, and the second still loses."""

    operation = await _pending(harness)
    instant = harness.clock.now()

    first = await harness.operations.claim(operation)
    assert harness.clock.now() == instant, "the clock did not move; the condition did the work"

    with pytest.raises((StateTransitionError, PersistenceConflictError)):
        await harness.operations.claim(operation)

    current = await harness.operations.load(
        namespace=harness.namespace, operation_id=operation.operation_id
    )
    assert current.version == first.version
    assert current.status is Status.RUNNING


async def test_concurrent_claims_of_one_operation_produce_one_winner(
    harness: MonitorHarness,
) -> None:
    operation = await _pending(harness)

    outcomes = await asyncio.gather(
        harness.operations.claim(operation),
        harness.operations.claim(operation),
        harness.operations.claim(operation),
        return_exceptions=True,
    )

    winners = [item for item in outcomes if isinstance(item, ApplicationOperation)]
    losers = [item for item in outcomes if isinstance(item, Exception)]
    assert len(winners) == 1
    assert len(losers) == 2
    assert all(isinstance(item, StateTransitionError | PersistenceConflictError) for item in losers)


async def test_two_redelivered_workers_invoke_the_model_once(
    harness: MonitorHarness,
) -> None:
    """The claim is what stands between an at-least-once dispatcher and a duplicate model call."""

    await harness.seed()
    locators = await harness.ingest_feed()
    operation = await _pending_over(harness, locators)
    agent = LexicalFakeMonitorAgent()
    worker = harness.worker(agent)
    job = _job(harness, operation, locators)

    await worker.execute(job)
    await worker.execute(job)

    assert len(agent.invocations) == 1


async def test_a_transaction_would_not_have_been_exclusive_here(
    harness: MonitorHarness,
) -> None:
    """The mutation test, run forwards: show what the rejected implementation does.

    Two byte-identical transactions carry the same derived client request token, and DynamoDB
    treats the second as an idempotent replay of the first for ten minutes. Both callers are
    told they succeeded. That is correct behaviour for a *retry* and catastrophic for a
    *claim*, and it is why the operation transition is a bare conditional write.

    If somebody moves the claim back onto ``UnitOfWork.commit``, the claim tests above still
    pass in isolation -- the second caller sees a stale version and stops -- but only because
    the first caller advanced the row. This test pins the underlying mechanism rather than
    the symptom: the same plan committed twice does not fail.
    """

    operation = await _pending(harness)
    scope = NamespaceScope(namespace=harness.namespace)
    claimed = replace(
        operation,
        status=Status.RUNNING,
        version=operation.version + 1,
        updated_at=harness.clock.now(),
    )
    plan = TransactionPlan(
        name="claim-via-transaction",
        operations=(
            harness.core.stage_update_operation(scope, claimed, expected_version=operation.version),
        ),
        audit_required=False,
    )

    await harness.unit_of_work.commit(plan)
    # The identical plan again, at the same instant: no exception, because the token matched.
    await harness.unit_of_work.commit(plan)

    # And the conditional write the real claim uses does refuse it.
    with pytest.raises(PersistenceConflictError):
        await harness.core.apply_operation_transition(
            scope, claimed, expected_version=operation.version
        )


async def test_the_claim_path_writes_conditionally_rather_than_transactionally() -> None:
    """A structural check, because the difference is invisible in the observable outcome."""

    source = inspect.getsource(ApplicationOperations._transition)

    assert "apply_operation_transition" in source
    assert "TransactionPlan" not in source
    assert "unit_of_work" not in source


# ---------------------------------------------------------------------------------------
# M14 -- a lost worker's attempt ends once, and never restarts itself
# ---------------------------------------------------------------------------------------


async def test_a_fresh_running_operation_is_left_entirely_alone(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    locators = await harness.ingest_feed()
    operation = await _pending_over(harness, locators)
    running = await harness.operations.claim(operation)
    agent = LexicalFakeMonitorAgent()

    returned = await harness.worker(agent).execute(_job(harness, operation, locators))

    assert returned.status is Status.RUNNING
    assert returned.version == running.version
    assert agent.invocations == [], "a live attempt is not a second attempt"


async def test_a_stale_running_operation_becomes_terminal_exactly_once(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    locators = await harness.ingest_feed()
    operation = await _pending_over(harness, locators)
    await harness.operations.claim(operation)
    harness.clock.instant += MAX_WORKER_EXECUTION_WINDOW + timedelta(seconds=1)
    agent = LexicalFakeMonitorAgent()

    recovered = await harness.worker(agent).execute(_job(harness, operation, locators))

    assert recovered.status is Status.FAILED
    assert recovered.error_code == OPERATION_STALE_ERROR_CODE
    assert agent.invocations == [], "recovery records an ending; it never starts the work again"


async def test_two_recovery_workers_on_one_stale_operation_produce_one_transition(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    operation = await _pending(harness)
    running = await harness.operations.claim(operation)
    harness.clock.instant += MAX_WORKER_EXECUTION_WINDOW + timedelta(seconds=1)

    outcomes = await asyncio.gather(
        harness.operations.abandon_if_stale(running),
        harness.operations.abandon_if_stale(running),
        return_exceptions=True,
    )

    transitioned = [item for item in outcomes if isinstance(item, ApplicationOperation)]
    assert len(transitioned) == 1
    current = await harness.operations.load(
        namespace=harness.namespace, operation_id=operation.operation_id
    )
    assert current.version == running.version + 1, "one transition, one version bump"
    assert current.status is Status.FAILED


async def test_the_original_worker_cannot_overwrite_a_recovered_terminal_state(
    harness: MonitorHarness,
) -> None:
    """The slow worker comes back with a stale expected version and loses, as it must."""

    await harness.seed()
    operation = await _pending(harness)
    running = await harness.operations.claim(operation)
    harness.clock.instant += MAX_WORKER_EXECUTION_WINDOW + timedelta(seconds=1)
    recovered = await harness.operations.abandon_if_stale(running)
    assert recovered is not None

    with pytest.raises(PersistenceConflictError):
        await harness.operations.succeed(running, result_refs=())

    current = await harness.operations.load(
        namespace=harness.namespace, operation_id=operation.operation_id
    )
    assert current.status is Status.FAILED
    assert current.error_code == OPERATION_STALE_ERROR_CODE


async def test_the_stale_window_is_wider_than_the_whole_timeout_hierarchy() -> None:
    """A window shorter than a legitimate run would declare working workers dead."""

    from runtimes.monitor.entrypoint import timeout_hierarchy

    from chorus.settings import Settings

    model_timeout, runtime_budget = timeout_hierarchy()
    application_timeout = Settings(agent_mode="fake").agent_timeout_seconds
    # Two agent invocations at the outer timeout is the worst legitimate case.
    assert MAX_WORKER_EXECUTION_WINDOW.total_seconds() > 2 * application_timeout
    assert model_timeout < runtime_budget < application_timeout


def _job(
    harness: MonitorHarness,
    operation: ApplicationOperation,
    locators: tuple[MessageFeedEntry, ...],
) -> MonitorOperationJob:
    return harness.job_for(operation, locators)
