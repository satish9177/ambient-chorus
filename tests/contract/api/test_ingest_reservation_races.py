"""The ingest reservation, raced for real rather than interleaved by hand.

The companion suite in ``test_ingest_reservation.py`` drives reservation and completion by hand
in one task. That proves the *shape* of the race is handled; it cannot prove the persistence
primitives actually provide the atomicity the shape depends on, because nothing ever contends.

These tests contend:

* many concurrent tasks through the whole application path (``asyncio.gather``);
* many concurrent OS threads through the real HTTP route, against DynamoDB Local, which is an
  external server with genuine concurrent request handling.

The question is never "did it usually work". It is: which durable write is the one that can only
succeed once, and does everybody else converge on that winner's answer.

Promoted from an independent reviewer's H-9 falsification probe, which failed to reproduce.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from tests.contract.api.conftest import ApiHarness, build_harness
from tests.fixtures.drivers import DYNAMODB_LOCAL_AVAILABLE
from tests.fixtures.monitor import PRESENTER_ACTOR_HASH

from chorus.application.commands.ingest_messages import (
    IngestMessage,
    IngestMessagesCommand,
    monitor_operation_identity,
)
from chorus.application.operations import StartReservation, monitor_locator_hash
from chorus.domain.entities import ApplicationOperationKind
from chorus.infrastructure.local.monitor_agent import LexicalFakeMonitorAgent
from chorus.ports.errors import IdempotencyConflictError
from chorus.ports.idempotency import IdempotentCommand
from chorus.ports.operations import MonitorOperationJob
from chorus.ports.records import MessageFeedEntry

pytestmark = pytest.mark.anyio

KEY = "h9-probe-key-0001"
SENT_AT = datetime(2030, 1, 20, 9, 0, tzinfo=UTC)
RACERS = 8


def _command(api: ApiHarness, *, key: str = KEY, suffix: str = "") -> IngestMessagesCommand:
    return IngestMessagesCommand(
        namespace=api.harness.namespace,
        community_id=api.harness.community_id,
        actor_id_hash=PRESENTER_ACTOR_HASH,
        idempotency_key=key,
        messages=(
            IngestMessage(
                channel_message_id=f"h9-probe-a{suffix}",
                contributor_id=api.harness.contributor_id("resident-a"),
                sent_at=SENT_AT,
                text="The elevator is stuck between floors again this morning.",
            ),
            IngestMessage(
                channel_message_id=f"h9-probe-b{suffix}",
                contributor_id=api.harness.contributor_id("resident-b"),
                sent_at=SENT_AT + timedelta(minutes=1),
                text="Same elevator problem, stuck for ten minutes now.",
            ),
        ),
    )


# ---------------------------------------------------------------------------------------
# PROBE 1 -- N concurrent tasks, whole application path, one shared key
# ---------------------------------------------------------------------------------------


async def test_concurrent_reservations_converge_on_one_operation(
    api: ApiHarness,
) -> None:
    """Eight racers, all awaiting concurrently, sharing one key and one payload."""

    await api.harness.seed()
    command = _command(api)
    key_hash, request_hash = monitor_operation_identity(command)

    async def racer() -> Any:
        reserved = await api.harness.operations.reserve_start(
            namespace=api.harness.namespace,
            command=IdempotentCommand.START_MONITOR_OPERATION,
            actor_id_hash=PRESENTER_ACTOR_HASH,
            key_hash=key_hash,
            request_hash=request_hash,
        )
        ingested = await api.harness.ingest.execute(command)
        sent_by_channel = {m.channel_message_id: m.sent_at for m in command.messages}
        locators = tuple(
            MessageFeedEntry(
                message_id=item.message_id, sent_at=sent_by_channel[item.channel_message_id]
            )
            for item in ingested.messages
        )
        if not isinstance(reserved, StartReservation):
            return reserved, locators
        started = await api.harness.operations.complete_start(
            reserved,
            namespace=api.harness.namespace,
            kind=ApplicationOperationKind.MONITOR,
            actor_id_hash=PRESENTER_ACTOR_HASH,
            agent_binding_hash=monitor_locator_hash(locators),
        )
        return started, locators

    outcomes = await asyncio.gather(*(racer() for _ in range(RACERS)))

    operation_ids = {started.operation.operation_id for started, _ in outcomes}
    invocation_ids = {started.invocation_id for started, _ in outcomes}
    assert len(operation_ids) == 1, f"one logical operation, got {operation_ids}"
    assert len(invocation_ids) == 1, f"one invocation identity, got {invocation_ids}"
    winners = [started for started, _ in outcomes if not started.replayed]
    assert len(winners) == 1, f"exactly one caller may own the completion, got {len(winners)}"

    # Exactly one model dispatch, driven by every racer's own job identity.
    agent = LexicalFakeMonitorAgent()
    worker = api.harness.worker(agent)
    started, locators = outcomes[0]
    jobs = [
        MonitorOperationJob(
            operation_id=started.operation.operation_id,
            namespace=api.harness.namespace,
            community_id=api.harness.community_id,
            invocation_id=started.invocation_id,
            correlation_id=uuid4(),
            actor_id_hash=PRESENTER_ACTOR_HASH,
            request_hash=request_hash,
            message_locators=locators,
        )
        for _ in range(RACERS)
    ]
    await asyncio.gather(*(worker.execute(job) for job in jobs))
    assert len(agent.invocations) == 1, (
        f"{RACERS} concurrent deliveries produced {len(agent.invocations)} passes over "
        "private text; exactly one is required"
    )


# ---------------------------------------------------------------------------------------
# PROBE 2 -- same key, DIFFERENT payload, concurrently
# ---------------------------------------------------------------------------------------


async def test_same_key_different_payload_conflicts_under_concurrency(
    api: ApiHarness,
) -> None:
    """One key, two different requests, racing. The loser must be refused, not merged."""

    await api.harness.seed()
    first = _command(api, suffix="-x")
    second = _command(api, suffix="-y")
    first_hashes = monitor_operation_identity(first)
    second_hashes = monitor_operation_identity(second)
    assert first_hashes[0] == second_hashes[0], "same key hash"
    assert first_hashes[1] != second_hashes[1], "different request hash"

    async def racer(hashes: tuple[Any, Any]) -> object:
        try:
            return await api.harness.operations.reserve_start(
                namespace=api.harness.namespace,
                command=IdempotentCommand.START_MONITOR_OPERATION,
                actor_id_hash=PRESENTER_ACTOR_HASH,
                key_hash=hashes[0],
                request_hash=hashes[1],
            )
        except IdempotencyConflictError as error:
            return error

    results = await asyncio.gather(
        *(racer(first_hashes) for _ in range(4)),
        *(racer(second_hashes) for _ in range(4)),
    )
    conflicts = [item for item in results if isinstance(item, IdempotencyConflictError)]
    reservations = [item for item in results if isinstance(item, StartReservation)]
    assert conflicts, "the payload that lost the key must be refused"
    assert reservations, "the payload that won the key must proceed"
    assert len({r.request_hash for r in reservations}) == 1, (
        "two different payloads must never both hold the same key"
    )
    assert len(conflicts) == 4 and len(reservations) == 4


# ---------------------------------------------------------------------------------------
# PROBE 3 -- crash between reservation and completion must not poison the key
# ---------------------------------------------------------------------------------------


async def test_crash_between_reservation_and_completion_is_recoverable(
    api: ApiHarness,
) -> None:
    """Reserve, die, restart, retry the identical request: is the key still usable?"""

    await api.harness.seed()
    command = _command(api)
    key_hash, request_hash = monitor_operation_identity(command)

    # Attempt one: reserve, then "crash" -- nothing else happens.
    abandoned = await api.harness.operations.reserve_start(
        namespace=api.harness.namespace,
        command=IdempotentCommand.START_MONITOR_OPERATION,
        actor_id_hash=PRESENTER_ACTOR_HASH,
        key_hash=key_hash,
        request_hash=request_hash,
    )
    assert isinstance(abandoned, StartReservation)

    # Attempt two through five: a restarted process, identical request each time.
    seen: list[Any] = []
    for _ in range(4):
        reserved = await api.harness.operations.reserve_start(
            namespace=api.harness.namespace,
            command=IdempotentCommand.START_MONITOR_OPERATION,
            actor_id_hash=PRESENTER_ACTOR_HASH,
            key_hash=key_hash,
            request_hash=request_hash,
        )
        ingested = await api.harness.ingest.execute(command)
        sent_by_channel = {m.channel_message_id: m.sent_at for m in command.messages}
        locators = tuple(
            MessageFeedEntry(
                message_id=item.message_id, sent_at=sent_by_channel[item.channel_message_id]
            )
            for item in ingested.messages
        )
        if isinstance(reserved, StartReservation):
            seen.append(
                await api.harness.operations.complete_start(
                    reserved,
                    namespace=api.harness.namespace,
                    kind=ApplicationOperationKind.MONITOR,
                    actor_id_hash=PRESENTER_ACTOR_HASH,
                    agent_binding_hash=monitor_locator_hash(locators),
                )
            )
        else:
            seen.append(reserved)

    assert len({item.operation.operation_id for item in seen}) == 1, "one operation, four retries"
    assert len({item.invocation_id for item in seen}) == 1
    assert [item.replayed for item in seen] == [False, True, True, True]


# ---------------------------------------------------------------------------------------
# PROBE 4 -- real OS threads through the real HTTP route (DynamoDB Local)
# ---------------------------------------------------------------------------------------


@pytest.mark.skipif(
    not DYNAMODB_LOCAL_AVAILABLE, reason="a true concurrency probe needs a real storage server"
)
def test_true_thread_concurrency_through_the_http_route() -> None:
    """Eight OS threads POST the identical request at the same instant.

    This is not an interleaving the test chose. Eight ``TestClient`` calls run on eight
    threads against DynamoDB Local, whose conditional writes are evaluated by a server this
    process does not control. If exactly one operation comes back, the exclusivity is coming
    from the storage condition rather than from the test's ordering.
    """

    from tests.fixtures.drivers import storage_driver

    driver_iter = storage_driver("dynamodb-local", prefix="h9probe")
    driver = next(driver_iter)
    try:
        api = build_harness(driver, "recording")
        with api.client:
            asyncio.run(api.harness.seed())
            body = {
                "community_id": str(api.harness.community_id),
                "messages": [
                    {
                        "adapter": "SYNTHETIC",
                        "channel_message_id": "h9-thread-001",
                        "contributor_id": str(api.harness.contributor_id("resident-a")),
                        "sent_at": "2030-01-20T09:00:00Z",
                        "text": "The elevator is stuck between floors again this morning.",
                        "attachments": [],
                    },
                    {
                        "adapter": "SYNTHETIC",
                        "channel_message_id": "h9-thread-002",
                        "contributor_id": str(api.harness.contributor_id("resident-b")),
                        "sent_at": "2030-01-20T09:01:00Z",
                        "text": "Same elevator problem, stuck for ten minutes now.",
                        "attachments": [],
                    },
                ],
            }
            headers = api.presenter_headers(**{"Idempotency-Key": "h9-thread-key-0001"})

            start = threading.Barrier(RACERS)
            responses: list[Any] = [None] * RACERS

            def post(index: int) -> None:
                start.wait()
                responses[index] = api.client.post(
                    "/v1/ingest/messages", json=body, headers=headers
                )

            threads = [threading.Thread(target=post, args=(index,)) for index in range(RACERS)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            statuses = [item.status_code for item in responses]
            assert set(statuses) <= {202, 409}, f"unexpected statuses: {statuses}"
            accepted = [item for item in responses if item.status_code == 202]
            assert accepted, "at least one identical caller must be accepted"
            operation_ids = {item.json()["operation"]["operation_id"] for item in accepted}
            assert len(operation_ids) == 1, (
                f"{RACERS} true-concurrent identical requests produced {len(operation_ids)} "
                f"operations: {operation_ids}"
            )
            assert len(accepted) == RACERS, (
                f"every identical caller should be accepted; statuses were {statuses}"
            )
            # Exactly one job handed over, however many callers raced.
            jobs = api.dispatcher.jobs  # type: ignore[union-attr]
            assert len({job.invocation_id for job in jobs}) == 1, (
                f"{len(jobs)} dispatches named "
                f"{len({job.invocation_id for job in jobs})} invocation identities"
            )
    finally:
        for _ in driver_iter:
            pass
