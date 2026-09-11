"""The operation worker's Lambda entry point, and what a duplicate delivery actually does.

Two things are proved here, and the second is the reason the first matters.

**The event boundary is exact.** ``worker-job/v1`` round-trips every one of the five job types
field for field, an unknown schema version or kind fails closed, and the kind is read from the
envelope's **declared** field -- so a payload cannot be shaped into a different operation than
the one it names. Every refusal happens before an operation is loaded or claimed.

**A duplicate delivery is safe, and it is safe for a durable reason.** AWS asynchronous
invocation may deliver the same job twice, and the handler holds no cache and no seen-set on
purpose: what makes a repeat harmless is the operation's own conditional ``PENDING -> RUNNING``
claim. So the duplicate test runs the *production* handler over the *real* worker and the real
storage driver, delivers the identical event twice, and asserts that the model was invoked
exactly once.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from functions.envelope import InvocationFailedError
from functions.worker.composition import WorkerComposition
from functions.worker.handler import MALFORMED_EVENT, run
from tests.fixtures.drivers import storage_driver
from tests.fixtures.monitor import MonitorHarness

from chorus.application.jobs import (
    WORKER_JOB_SCHEMA,
    WorkerJobError,
    WorkerJobKind,
    decode_job,
    encode_job,
)
from chorus.domain.entities import ApplicationOperationStatus
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CaseId,
    CommunityId,
    EvidenceItemId,
    ExecutionId,
    MessageId,
    Namespace,
    OperationId,
    Sha256Digest,
    ViewId,
)
from chorus.infrastructure.local.monitor_agent import LexicalFakeMonitorAgent
from chorus.infrastructure.persistent_clock import ScopedLogicalClock
from chorus.ports.demo_clock import DemoClockRecord, DemoClockUnavailableError
from chorus.ports.operations import (
    InvestigationOperationJob,
    MonitorOperationJob,
    ProposeActionOperationJob,
    SendActionOperationJob,
)
from chorus.ports.records import MessageFeedEntry

SEED = datetime(2030, 1, 14, 9, 0, tzinfo=UTC)
DIGEST = Sha256Digest(f"sha256:{'b' * 64}")

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class StubClockStore:
    def __init__(self, *, instant: datetime | None = SEED) -> None:
        self.instant = instant
        self.reads = 0

    async def read(self) -> DemoClockRecord:
        self.reads += 1
        if self.instant is None:
            raise DemoClockUnavailableError("no clock row")
        return DemoClockRecord(
            logical_time=self.instant,
            version=1,
            reset_generation=1,
            seed_instant=SEED,
            advance_count=0,
        )

    async def advance(self, expected: DemoClockRecord, *, to: datetime) -> DemoClockRecord:
        raise AssertionError("the worker must never advance the clock")


class CountingAgent:
    """The local Monitor stand-in, wrapped so the invocation count is unambiguous.

    Composition rather than subclassing: the stand-in already keeps its own ``invocations``
    list, and a subclass that shadowed it would be counting one thing while the parent appended
    to another.
    """

    def __init__(self) -> None:
        self.inner = LexicalFakeMonitorAgent()

    @property
    def calls(self) -> int:
        return len(self.inner.invocations)

    async def invoke_monitor(self, invocation: Any) -> Any:
        return await self.inner.invoke_monitor(invocation)


def envelope(job: object) -> dict[str, Any]:
    payload = encode_job(job)
    return {"operation": str(payload["kind"]), "payload": payload}


# -- the wire contract ----------------------------------------------------------------------


def monitor_job() -> MonitorOperationJob:
    return MonitorOperationJob(
        operation_id=OperationId(UUID("11111111-1111-4111-8111-111111111111")),
        namespace=Namespace("DEMO"),
        community_id=CommunityId(uuid4()),
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        actor_id_hash=DIGEST,
        request_hash=DIGEST,
        message_locators=(MessageFeedEntry(message_id=MessageId(uuid4()), sent_at=SEED),),
    )


def every_job() -> list[tuple[WorkerJobKind, object]]:
    community = CommunityId(uuid4())
    case = CaseId(uuid4())
    return [
        (WorkerJobKind.MONITOR, monitor_job()),
        (
            WorkerJobKind.INVESTIGATE,
            InvestigationOperationJob(
                operation_id=OperationId(uuid4()),
                namespace=Namespace("DEMO"),
                community_id=community,
                case_id=case,
                invocation_id=uuid4(),
                correlation_id=uuid4(),
                actor_id_hash=DIGEST,
                request_hash=DIGEST,
                expected_case_version=2,
                reason="REOPEN",
                idempotency_key="investigate-key",
            ),
        ),
        (
            WorkerJobKind.PROPOSE_ACTION,
            ProposeActionOperationJob(
                operation_id=OperationId(uuid4()),
                namespace=Namespace("DEMO"),
                community_id=community,
                case_id=case,
                invocation_id=uuid4(),
                correlation_id=uuid4(),
                actor_id_hash=DIGEST,
                request_hash=DIGEST,
                expected_case_version=3,
                view_id=ViewId(uuid4()),
                view_hash=DIGEST,
                idempotency_key="propose-key",
            ),
        ),
        (
            WorkerJobKind.SEND_ACTION,
            SendActionOperationJob(
                operation_id=OperationId(uuid4()),
                namespace=Namespace("DEMO"),
                community_id=community,
                case_id=case,
                action_id=ActionId(uuid4()),
                execution_id=ExecutionId(uuid4()),
                approval_id=ApprovalId(uuid4()),
                correlation_id=uuid4(),
                actor_id_hash=DIGEST,
                request_hash=DIGEST,
                expected_execution_version=1,
                idempotency_key="send-key",
            ),
        ),
    ]


def extraction_job() -> object:
    from chorus.application.commands.extract_commitment_operation import (
        ExtractCommitmentJob,
    )

    return ExtractCommitmentJob(
        operation_id=OperationId(uuid4()),
        namespace=Namespace("DEMO"),
        community_id=CommunityId(uuid4()),
        case_id=CaseId(uuid4()),
        action_id=ActionId(uuid4()),
        evidence_id=EvidenceItemId(uuid4()),
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        actor_id_hash=DIGEST,
        request_hash=DIGEST,
        evidence_sha256=DIGEST,
    )


def test_every_job_kind_round_trips_field_for_field() -> None:
    for kind, job in [*every_job(), (WorkerJobKind.EXTRACT_COMMITMENT, extraction_job())]:
        payload = encode_job(job)
        assert payload["schema"] == WORKER_JOB_SCHEMA
        assert payload["kind"] == kind.value
        decoded_kind, decoded = decode_job(payload)
        assert decoded_kind is kind
        assert decoded == job


def test_the_event_carries_no_content_only_identity() -> None:
    """Identifiers, versions, digests and instants -- and no message text anywhere."""

    import json

    for _, job in [*every_job(), (WorkerJobKind.EXTRACT_COMMITMENT, extraction_job())]:
        rendered = json.dumps(encode_job(job))
        assert "text" not in rendered
        assert "body" not in rendered
        assert "address" not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not an object", id="not-an-object"),
        pytest.param({}, id="empty"),
        pytest.param({"schema": "worker-job/v9", "kind": "MONITOR", "job": {}}, id="bad-schema"),
        pytest.param(
            {"schema": WORKER_JOB_SCHEMA, "kind": "INVENTED", "job": {}}, id="unknown-kind"
        ),
        pytest.param({"schema": WORKER_JOB_SCHEMA, "kind": "MONITOR"}, id="no-job"),
        pytest.param(
            {"schema": WORKER_JOB_SCHEMA, "kind": "MONITOR", "job": {"namespace": "DEMO"}},
            id="incomplete-job",
        ),
    ],
)
def test_a_malformed_event_is_refused_by_the_decoder(payload: object) -> None:
    with pytest.raises(WorkerJobError):
        decode_job(payload)


def test_a_monitor_job_naming_no_message_is_refused() -> None:
    payload = encode_job(monitor_job())
    payload["job"]["message_locators"] = []  # type: ignore[index]
    with pytest.raises(WorkerJobError):
        decode_job(payload)


def test_the_kind_is_declared_and_never_inferred_from_the_payload() -> None:
    """A Monitor body under an ``INVESTIGATE`` kind is refused, not re-interpreted."""

    payload = encode_job(monitor_job())
    payload["kind"] = WorkerJobKind.INVESTIGATE.value
    with pytest.raises(WorkerJobError):
        decode_job(payload)


def test_encoding_refuses_anything_that_is_not_one_of_the_five() -> None:
    with pytest.raises(WorkerJobError):
        encode_job({"looks": "like a job"})


# -- the handler ------------------------------------------------------------------------------


class RecordingRunner:
    def __init__(self) -> None:
        self.jobs: list[object] = []

    async def execute(self, job: object) -> Any:
        self.jobs.append(job)
        return _StubOperation()


class _StubOperation:
    operation_id = OperationId(UUID("44444444-4444-4444-8444-444444444444"))
    error_code = None

    class kind:
        value = "MONITOR"

    class status:
        value = "SUCCEEDED"


def stub_composition(clock: StubClockStore | None = None) -> tuple[WorkerComposition, Any]:
    runner = RecordingRunner()
    return (
        WorkerComposition(
            runners=dict.fromkeys(WorkerJobKind, runner),
            clock_store=clock or StubClockStore(),
            scope=ScopedLogicalClock(),
        ),
        runner,
    )


async def test_a_valid_job_reaches_its_runner_and_reports_safely() -> None:
    graph, runner = stub_composition()
    job = monitor_job()
    result = await run(envelope(job), built=graph)

    assert runner.jobs == [job]
    assert result["status"] == "COMPLETED"
    assert set(result) == {"status", "operation_id", "kind", "operation_status", "error_code"}


async def test_a_malformed_event_runs_nothing() -> None:
    graph, runner = stub_composition()
    result = await run({"operation": "MONITOR", "payload": {}}, built=graph)

    assert result == {"status": "REFUSED", "reason_code": MALFORMED_EVENT}
    assert runner.jobs == []


async def test_an_unknown_operation_is_refused_before_decoding() -> None:
    graph, runner = stub_composition()
    result = await run(
        {"operation": "DeleteEverything", "payload": encode_job(monitor_job())}, built=graph
    )

    assert result["status"] == "REFUSED"
    assert runner.jobs == []


async def test_an_unreadable_clock_fails_the_invocation_before_any_job_runs() -> None:
    """P2-6: a clock outage fails the invocation so AWS's own async retry sees it.

    A normal return would be read as "delivered" and never retried, silently losing the job.
    Safe to raise here specifically because it happens before any runner is ever called -- no
    operation claimed, no SES call reachable, so ``SEND_ACTION``'s own quarantine semantics are
    untouched.
    """

    graph, runner = stub_composition(StubClockStore(instant=None))
    with pytest.raises(InvocationFailedError):
        await run(envelope(monitor_job()), built=graph)

    assert runner.jobs == []


async def test_a_clock_sdk_error_also_fails_the_invocation() -> None:
    class ExplodingClockStore:
        async def read(self) -> DemoClockRecord:
            raise DemoClockUnavailableError("throttled")

        async def advance(self, expected: DemoClockRecord, *, to: datetime) -> DemoClockRecord:
            raise AssertionError("the worker must never advance the clock")

    graph, runner = stub_composition()
    graph = WorkerComposition(
        runners=graph.runners, clock_store=ExplodingClockStore(), scope=graph.scope
    )
    with pytest.raises(InvocationFailedError):
        await run(envelope(monitor_job()), built=graph)
    assert runner.jobs == []


# -- duplicate delivery, over the real worker and real storage ---------------------------------


@pytest.fixture
def harness() -> Any:
    drivers = storage_driver("memory", prefix="worker-duplicate")
    driver = next(drivers)
    yield MonitorHarness(driver=driver)
    for _ in drivers:  # pragma: no cover - driver teardown
        pass


async def test_the_same_event_delivered_twice_invokes_the_model_once(harness: Any) -> None:
    """The production handler, the real Monitor worker, and one durable operation.

    Nothing about this is arranged by the handler: the second delivery finds the operation no
    longer ``PENDING``, and the conditional claim is what refuses it. That is the duplicate
    boundary the deployment contract § 14 names, exercised end to end.
    """

    await harness.seed()
    locators = await harness.ingest_feed()
    operation, job = await harness.dispatched(locators)
    agent = CountingAgent()
    graph = WorkerComposition(
        runners={WorkerJobKind.MONITOR: harness.worker(agent)},
        clock_store=StubClockStore(),
        scope=ScopedLogicalClock(),
    )

    event = envelope(job)
    first = await run(event, built=graph)
    second = await run(event, built=graph)

    assert agent.calls == 1
    assert first["operation_id"] == second["operation_id"] == str(operation.operation_id)
    settled = await harness.operations.load(
        namespace=harness.namespace, operation_id=operation.operation_id
    )
    assert settled.status is ApplicationOperationStatus.SUCCEEDED


async def test_a_duplicate_delivery_leaves_the_operation_where_the_first_left_it(
    harness: Any,
) -> None:
    """Externally meaningful state is unchanged by the repeat, not merely "not re-run"."""

    await harness.seed()
    locators = await harness.ingest_feed()
    operation, job = await harness.dispatched(locators)
    graph = WorkerComposition(
        runners={WorkerJobKind.MONITOR: harness.worker(CountingAgent())},
        clock_store=StubClockStore(),
        scope=ScopedLogicalClock(),
    )

    event = envelope(job)
    await run(event, built=graph)
    after_first = await harness.operations.load(
        namespace=harness.namespace, operation_id=operation.operation_id
    )
    await run(event, built=graph)
    after_second = await harness.operations.load(
        namespace=harness.namespace, operation_id=operation.operation_id
    )

    # Same version, same result references: the second delivery wrote nothing at all.
    assert after_second.version == after_first.version
    assert after_second.result_refs == after_first.result_refs
