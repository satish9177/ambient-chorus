"""The commitment watcher's Lambda entry point, and the one thing it must never have two of.

[ADR-028](../../../docs/adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) § 5 requires the
scheduler path and the demo-clock path to reach the **same** watcher application logic, because
a second implementation would make the early-firing comparison mean two different things on the
two paths. That is asserted here by construction: both build the identical
``commitment-watcher-request/v1`` envelope, both go through the identical decoder, and the test
compares the commands that come out.

The clock rules of [ADR-029](../../../docs/adr/ADR-029-deployed-demo-clock-authority.md) § 4 are
asserted the same way -- missing, corrupt, and unreachable all fail closed and typed, and
nothing anywhere falls back to a process-local clock, to ``SystemClock``, or to the event's own
timestamp.

No Scheduler runs, no Lambda is invoked, and no AWS client is constructed: the composition is
handed in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from functions.commitment_watcher.composition import WatcherComposition
from functions.commitment_watcher.handler import (
    MALFORMED_EVENT,
    run,
)
from functions.envelope import InvocationFailedError

from chorus.application.commands.record_commitment_due import (
    TRIGGER_DEMO_CLOCK,
    TRIGGER_SCHEDULE,
    RecordCommitmentDueCommand,
    RecordCommitmentDueResult,
    WatcherOutcome,
)
from chorus.application.services.commitment_schedule import due_event
from chorus.application.watcher_contract import (
    WATCHER_OPERATION,
    WATCHER_REQUEST_SCHEMA,
    RemoteRecordCommitmentDue,
    WatcherRequestError,
    decode_watcher_request,
    encode_watcher_request,
    encode_watcher_result,
)
from chorus.domain.ids import CaseId, CommitmentId, CommunityId, Namespace, Sha256Digest
from chorus.infrastructure.persistent_clock import ScopedLogicalClock
from chorus.ports.demo_clock import DemoClockRecord, DemoClockUnavailableError
from chorus.ports.errors import ExternalDependencyError

NAMESPACE = Namespace("DEMO")
COMMUNITY = CommunityId(UUID("11111111-1111-4111-8111-111111111111"))
CASE = CaseId(UUID("22222222-2222-4222-8222-222222222222"))
COMMITMENT = CommitmentId(UUID("33333333-3333-4333-8333-333333333333"))
ACTOR = Sha256Digest(f"sha256:{'a' * 64}")
SEED = datetime(2030, 1, 14, 9, 0, tzinfo=UTC)
DUE = SEED + timedelta(days=3)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecordingWatcher:
    """The real use case's call shape, recording what each path actually handed it."""

    def __init__(self, outcome: WatcherOutcome = WatcherOutcome.DUE) -> None:
        self.outcome = outcome
        self.commands: list[RecordCommitmentDueCommand] = []
        self.instants: list[datetime] = []
        self.clock: Any = None

    async def execute(self, command: RecordCommitmentDueCommand) -> RecordCommitmentDueResult:
        self.commands.append(command)
        if self.clock is not None:
            # Reading time inside the use case is what an unbound scope would refuse.
            self.instants.append(self.clock.now())
        return RecordCommitmentDueResult(
            outcome=self.outcome, commitment_status=None, commitment_version=None
        )


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
        raise AssertionError("the watcher must never advance the clock")


def composition(
    watcher: RecordingWatcher, clock: StubClockStore | None = None
) -> WatcherComposition:
    scope = ScopedLogicalClock()
    watcher.clock = scope
    return WatcherComposition(
        watcher=watcher,  # type: ignore[arg-type]
        clock_store=clock or StubClockStore(),
        scope=scope,
    )


def command(trigger: str = TRIGGER_SCHEDULE) -> RecordCommitmentDueCommand:
    return RecordCommitmentDueCommand(
        event=due_event(
            namespace=NAMESPACE,
            case_id=CASE,
            commitment_id=COMMITMENT,
            generation=1,
            due_at=DUE,
        ),
        community_id=COMMUNITY,
        actor_id_hash=ACTOR,
        correlation_id=uuid4(),
        trigger=trigger,
    )


def envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {"operation": WATCHER_OPERATION, "payload": payload}


# -- the two paths converge --------------------------------------------------------------------


async def test_the_scheduler_path_and_the_demo_clock_path_reach_one_watcher() -> None:
    """Same envelope, same decoder, same use case. Only the audited trigger differs."""

    watcher = RecordingWatcher()
    graph = composition(watcher)

    for trigger in (TRIGGER_SCHEDULE, TRIGGER_DEMO_CLOCK):
        await run(envelope(encode_watcher_request(command(trigger))), built=graph)

    scheduled, demo = watcher.commands
    assert scheduled.event == demo.event
    assert scheduled.community_id == demo.community_id
    assert (scheduled.trigger, demo.trigger) == (TRIGGER_SCHEDULE, TRIGGER_DEMO_CLOCK)


async def test_a_valid_request_returns_the_frozen_result_schema() -> None:
    watcher = RecordingWatcher(WatcherOutcome.WATCHER_REPLAY)
    result = await run(envelope(encode_watcher_request(command())), built=composition(watcher))

    assert result["schema"] == "commitment-watcher-result/v1"
    assert result["outcome"] == WatcherOutcome.WATCHER_REPLAY.value


async def test_the_api_and_the_scheduler_receive_one_result_schema() -> None:
    """The API parses the same body the scheduler path returns; there is no second shape."""

    outcome = RecordCommitmentDueResult(
        outcome=WatcherOutcome.DUE, commitment_status=None, commitment_version=4
    )
    body = encode_watcher_result(outcome)

    class Invoker:
        async def invoke(self, *, operation: str, payload: dict[str, object]) -> dict[str, object]:
            assert operation == WATCHER_OPERATION
            return body

    assert await RemoteRecordCommitmentDue(invoker=Invoker()).execute(command()) == outcome


# -- the clock -----------------------------------------------------------------------------------


async def test_the_watcher_evaluates_against_the_authoritative_reading() -> None:
    watcher = RecordingWatcher()
    clock = StubClockStore(instant=SEED + timedelta(days=4))
    await run(envelope(encode_watcher_request(command())), built=composition(watcher, clock))

    assert watcher.instants == [SEED + timedelta(days=4)]
    assert clock.reads == 1


async def test_a_missing_clock_fails_the_invocation_and_runs_nothing() -> None:
    """P2-6: an infrastructure outage fails the invocation, not the answer.

    A normal return from an async invocation is an acknowledgement -- Scheduler would never
    try again. So this must raise rather than return, letting AWS's own retry/DLQ machinery
    see it, and nothing about the commitment is loaded or claimed before it does.
    """

    watcher = RecordingWatcher()
    with pytest.raises(InvocationFailedError):
        await run(
            envelope(encode_watcher_request(command())),
            built=composition(watcher, StubClockStore(instant=None)),
        )

    assert watcher.commands == []


async def test_a_clock_sdk_error_also_fails_the_invocation() -> None:
    """Not only an absent row: any :class:`DemoClockError` from the store is retryable."""

    class ExplodingClockStore:
        async def read(self) -> DemoClockRecord:
            raise DemoClockUnavailableError("throttled")

        async def advance(self, expected: DemoClockRecord, *, to: datetime) -> DemoClockRecord:
            raise AssertionError("the watcher must never advance the clock")

    watcher = RecordingWatcher()
    scope = ScopedLogicalClock()
    watcher.clock = scope
    graph = WatcherComposition(
        watcher=watcher,  # type: ignore[arg-type]
        clock_store=ExplodingClockStore(),
        scope=scope,
    )
    with pytest.raises(InvocationFailedError):
        await run(envelope(encode_watcher_request(command())), built=graph)
    assert watcher.commands == []


async def test_there_is_no_system_clock_fallback() -> None:
    """The scope raises outside a binding, so a missing reading cannot become wall time."""

    scope = ScopedLogicalClock()
    with pytest.raises(DemoClockUnavailableError):
        scope.now()


async def test_the_event_timestamp_is_never_used_as_logical_time() -> None:
    """ADR-028 § 2-3: the event is unsigned, and reading a clock does not make it authoritative."""

    watcher = RecordingWatcher()
    clock = StubClockStore(instant=SEED)
    request = encode_watcher_request(command())
    forged = request["event"]
    assert isinstance(forged, dict)
    # A forged, far-future due instant in the event body changes nothing about what the watcher
    # believes the time is.
    forged["logical_due_at"] = "2099-01-01T00:00:00.000000Z"
    await run(envelope(request), built=composition(watcher, clock))

    assert watcher.instants == [SEED]


# -- refusals -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        pytest.param("not an object", id="not-an-object"),
        pytest.param({"payload": {}}, id="no-operation"),
        pytest.param({"operation": "SomethingElse", "payload": {}}, id="unknown-operation"),
        pytest.param({"operation": WATCHER_OPERATION}, id="no-payload"),
        pytest.param({"operation": WATCHER_OPERATION, "payload": {}}, id="empty-payload"),
    ],
)
async def test_a_malformed_event_is_refused_before_any_side_effect(event: object) -> None:
    watcher = RecordingWatcher()
    result = await run(event, built=composition(watcher))

    assert result == {"status": "REFUSED", "reason_code": MALFORMED_EVENT}
    assert watcher.commands == []


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda body: body.__setitem__("schema", "commitment-watcher-request/v9"),
            id="unknown-schema",
        ),
        pytest.param(
            lambda body: body["event"].__setitem__("schema_version", "commitment-due/v9"),
            id="unknown-event-schema",
        ),
        pytest.param(
            lambda body: body.__setitem__("trigger", "SOMETHING_ELSE"), id="unknown-trigger"
        ),
        pytest.param(
            lambda body: body["event"].__setitem__("expected_generation", 0),
            id="non-positive-generation",
        ),
        pytest.param(
            lambda body: body.__setitem__("community_id", "not-a-uuid"), id="bad-community"
        ),
        pytest.param(
            lambda body: body["event"].__setitem__("logical_due_at", "2030-01-14"),
            id="non-canonical-instant",
        ),
    ],
)
async def test_a_request_the_decoder_rejects_never_reaches_the_watcher(
    mutate: Any,
) -> None:
    body = encode_watcher_request(command())
    mutate(body)
    with pytest.raises(WatcherRequestError):
        decode_watcher_request(body)

    watcher = RecordingWatcher()
    assert (await run(envelope(body), built=composition(watcher)))["status"] == "REFUSED"
    assert watcher.commands == []


def test_the_request_schema_is_versioned_on_the_wire() -> None:
    assert encode_watcher_request(command())["schema"] == WATCHER_REQUEST_SCHEMA


# -- what the remote adapter refuses to believe ------------------------------------------------


async def test_a_clock_outage_becomes_the_apis_accepted_dependency_failure_end_to_end() -> None:
    """Section 12: the SAME handler serves both callers, and the synchronous adapter
    reclassifies its failure into what the API already knows how to answer.

    A fake Lambda client stands in for AWS's own behavior: it runs the *production* handler
    body and, when the handler raises, reports ``FunctionError`` exactly as a real asynchronous
    Lambda runtime would for an uncaught exception. ``RemoteRecordCommitmentDue`` -- the
    demo-clock route's own dependency -- must turn that into the same
    ``ExternalDependencyError`` the API's registered ``PersistenceError`` handler already maps
    to a 503, with no route-level change required.
    """

    import json as _json

    from chorus.application.watcher_contract import RemoteRecordCommitmentDue
    from chorus.infrastructure.lambdas.invoker import SynchronousLambdaInvoker

    graph = composition(RecordingWatcher(), StubClockStore(instant=None))

    class FakeLambdaRuntime:
        """Behaves like AWS Lambda: runs the handler body, reports FunctionError if it raises.

        Run in a fresh thread with its own event loop -- exactly as a real Lambda invocation is
        a genuinely separate execution context from the caller's, and this test's own async
        body is already running one loop this cannot nest inside.
        """

        def invoke(self, **request: Any) -> dict[str, Any]:
            import asyncio
            import io
            from concurrent.futures import ThreadPoolExecutor

            event = _json.loads(request["Payload"])

            def run_handler() -> None:
                asyncio.run(run(event, built=graph))

            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(run_handler).result()
            except InvocationFailedError:
                return {
                    "StatusCode": 200,
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                    "FunctionError": "Unhandled",
                    "Payload": io.BytesIO(b"{}"),
                }
            raise AssertionError("expected the handler to raise")  # pragma: no cover

    remote_watcher = RemoteRecordCommitmentDue(
        invoker=SynchronousLambdaInvoker(client=FakeLambdaRuntime(), function_name="fake")
    )
    with pytest.raises(ExternalDependencyError):
        await remote_watcher.execute(command())


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"schema": "commitment-watcher-result/v9"}, id="wrong-schema"),
        pytest.param({"schema": "commitment-watcher-result/v1"}, id="no-outcome"),
        pytest.param(
            {"schema": "commitment-watcher-result/v1", "outcome": "INVENTED"},
            id="unknown-outcome",
        ),
        pytest.param(
            {
                "schema": "commitment-watcher-result/v1",
                "outcome": "DUE",
                "commitment_status": "INVENTED",
            },
            id="unknown-status",
        ),
    ],
)
async def test_a_malformed_answer_raises_rather_than_becoming_an_outcome(
    body: dict[str, Any],
) -> None:
    class Invoker:
        async def invoke(self, *, operation: str, payload: dict[str, object]) -> dict[str, object]:
            return body

    with pytest.raises(ExternalDependencyError):
        await RemoteRecordCommitmentDue(invoker=Invoker()).execute(command())
