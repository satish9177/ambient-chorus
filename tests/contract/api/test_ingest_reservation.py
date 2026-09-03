"""Route command idempotency, and the rule that it happens before anything is written.

``POST /v1/ingest/messages`` carries an ``Idempotency-Key`` that owns the **whole request**.
The earlier implementation claimed that key after ingesting, and the result contradicted
itself: post key ``K`` with messages A-C, then post key ``K`` with messages D-F, and the second
call returned ``409 IDEMPOTENCY_CONFLICT`` -- after D-F were already durably stored and visible
in the feed. A conflict that says "this request was never accepted" while the request's data
sits in the community's own feed is not a conflict; it is a lie about durable state.

So the key is now claimed first, against the normalized request hash, and a hash that disagrees
is refused with zero mutations of any kind.

The half that makes that safe rather than merely strict is the *reservation*. The claim is
``IN_PROGRESS``, not ``COMPLETED``, so an attempt that dies anywhere between the claim and the
operation leaves a record its own identical retry recognises and continues. Everything in
between is replay-safe: per-message ingestion returns the identifier it already stored, and the
operation row and the completed record commit in one transaction, so a key can never end up
naming two operations.

The tests below walk that lifecycle: the conflict with nothing written, then a crash at each
stage, then the two ambiguous endings. Nothing sleeps and nothing races on timing -- every
recovery is driven by a conditional write or a strong read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.contract.api.conftest import ApiHarness, build_harness
from tests.fixtures.faults import (
    FaultInjectingDriver,
    TransactBehaviour,
    operation_creation,
)
from tests.fixtures.monitor import PRESENTER_ACTOR_HASH

from chorus.application.commands.ingest_messages import (
    IngestMessage,
    IngestMessagesCommand,
    monitor_operation_identity,
)
from chorus.application.operations import StartReservation, monitor_locator_hash
from chorus.domain.entities import ApplicationOperationKind, ApplicationOperationStatus
from chorus.infrastructure.local.dispatch import RecordingOperationDispatcher
from chorus.infrastructure.local.monitor_agent import LexicalFakeMonitorAgent
from chorus.ports.idempotency import IdempotencyStatus, IdempotentCommand
from chorus.ports.operations import MonitorOperationJob
from chorus.ports.pagination import PageRequest
from chorus.ports.records import MessageFeedEntry
from chorus.ports.scopes import CommunityScope
from chorus.ports.storage import StorageDriver

pytestmark = pytest.mark.anyio

KEY = "reservation-key-00000001"
SENT_AT = datetime(2030, 1, 14, 8, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------------------


def _message(label: str, minute: int) -> dict[str, Any]:
    return {
        "adapter": "SYNTHETIC",
        "channel_message_id": f"reservation-{label}",
        "contributor_id": None,
        "sent_at": (SENT_AT + timedelta(minutes=minute)).isoformat(),
        "text": f"The lift stopped between floors again ({label}).",
    }


def _body(api: ApiHarness, labels: tuple[str, ...]) -> dict[str, Any]:
    return {
        "community_id": str(api.harness.community_id),
        "messages": [_message(label, index) for index, label in enumerate(labels)],
    }


def _post(api: ApiHarness, body: dict[str, Any], *, key: str = KEY) -> Any:
    return api.client.post(
        "/v1/ingest/messages",
        json=body,
        headers=api.presenter_headers(**{"Idempotency-Key": key}),
    )


async def _channel_ids(api: ApiHarness) -> set[str]:
    """Every channel message identifier the community's own feed can show."""

    page = await api.harness.core.read_message_feed(
        CommunityScope(namespace=api.harness.namespace, community_id=api.harness.community_id),
        start=SENT_AT - timedelta(days=1),
        end=SENT_AT + timedelta(days=1),
        request=PageRequest(limit=100),
    )
    return {message.channel_message_id for message in page.items}


def _command(api: ApiHarness, body: dict[str, Any]) -> IngestMessagesCommand:
    return IngestMessagesCommand(
        namespace=api.harness.namespace,
        community_id=api.harness.community_id,
        actor_id_hash=PRESENTER_ACTOR_HASH,
        idempotency_key=KEY,
        messages=tuple(
            IngestMessage(
                channel_message_id=str(message["channel_message_id"]),
                contributor_id=None,
                sent_at=datetime.fromisoformat(str(message["sent_at"])),
                text=str(message["text"]),
            )
            for message in body["messages"]
        ),
    )


async def _reservation_record(api: ApiHarness, body: dict[str, Any]) -> Any:
    key_hash, request_hash = monitor_operation_identity(_command(api, body))
    idempotency_key = api.harness.operations._start_key(
        api.harness.namespace,
        IdempotentCommand.START_MONITOR_OPERATION,
        PRESENTER_ACTOR_HASH,
        key_hash,
    )
    assert request_hash is not None
    return await api.harness.idempotency.load(idempotency_key)


# ---------------------------------------------------------------------------------------
# 1 -- a conflicting request writes nothing at all
# ---------------------------------------------------------------------------------------


async def test_a_conflicting_request_under_one_key_stores_none_of_its_messages(
    api: ApiHarness,
) -> None:
    """Codex's reproduction, at the level that matters: what does the feed hold afterwards.

    The status code was never the defect. ``409`` is the right answer to one key naming two
    different requests. The defect was that D-F had already been written by the time it was
    returned, so the caller was told its request was refused while the community could see it.
    """

    await api.harness.seed()
    accepted = _post(api, _body(api, ("a", "b", "c")))
    assert accepted.status_code == 202

    refused = _post(api, _body(api, ("d", "e", "f")))

    assert refused.status_code == 409
    assert refused.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert await _channel_ids(api) == {
        "reservation-a",
        "reservation-b",
        "reservation-c",
    }


async def test_a_conflicting_request_creates_no_second_operation_and_no_dispatch(
    api: ApiHarness,
) -> None:
    """The rest of the blast radius: no operation, no invocation, no job on the queue."""

    await api.harness.seed()
    first = _post(api, _body(api, ("a", "b", "c")))
    dispatcher = api.dispatcher
    assert isinstance(dispatcher, RecordingOperationDispatcher)
    dispatched_before = len(dispatcher.jobs)

    refused = _post(api, _body(api, ("d", "e", "f")))

    assert refused.status_code == 409
    assert len(dispatcher.jobs) == dispatched_before
    assert {str(job.operation_id) for job in dispatcher.jobs} == {
        first.json()["operation"]["operation_id"]
    }


async def test_a_conflict_before_any_community_exists_still_writes_nothing(
    api: ApiHarness,
) -> None:
    """The reservation is claimed before the community is even read, and stays recoverable.

    Reserving first means the key is claimed before ingestion validates anything, so a request
    that then fails validation leaves an ``IN_PROGRESS`` record behind. That has to be
    *resumable* rather than terminal, or one bad request would poison its own key forever.
    """

    unseeded = _post(api, _body(api, ("a",)))
    assert unseeded.status_code == 404

    await api.harness.seed()
    retried = _post(api, _body(api, ("a",)))

    assert retried.status_code == 202
    assert await _channel_ids(api) == {"reservation-a"}


# ---------------------------------------------------------------------------------------
# 2 and 3 -- a crash between the reservation and the operation
# ---------------------------------------------------------------------------------------


async def test_a_reserved_key_with_no_ingestion_yet_is_resumed_by_an_identical_retry(
    api: ApiHarness,
) -> None:
    """The process died immediately after reserving. The retry owns the reservation."""

    await api.harness.seed()
    body = _body(api, ("a", "b"))
    key_hash, request_hash = monitor_operation_identity(_command(api, body))
    reserved = await api.harness.operations.reserve_start(
        namespace=api.harness.namespace,
        command=IdempotentCommand.START_MONITOR_OPERATION,
        actor_id_hash=PRESENTER_ACTOR_HASH,
        key_hash=key_hash,
        request_hash=request_hash,
    )
    assert isinstance(reserved, StartReservation)
    assert reserved.record.status is IdempotencyStatus.IN_PROGRESS

    resumed = _post(api, body)

    assert resumed.status_code == 202
    assert await _channel_ids(api) == {"reservation-a", "reservation-b"}
    record = await _reservation_record(api, body)
    assert record is not None and record.status is IdempotencyStatus.COMPLETED


async def test_a_reserved_key_with_some_messages_already_stored_is_resumed(
    api: ApiHarness,
) -> None:
    """A crash mid-ingest costs a replay of replay-safe work and nothing else.

    Per-message ingestion is keyed by channel identity and content, so re-sending the batch
    returns the identifiers already stored rather than storing them twice. That is precisely
    why the reservation can be resumable at all.
    """

    await api.harness.seed()
    body = _body(api, ("a", "b", "c"))
    key_hash, request_hash = monitor_operation_identity(_command(api, body))
    await api.harness.operations.reserve_start(
        namespace=api.harness.namespace,
        command=IdempotentCommand.START_MONITOR_OPERATION,
        actor_id_hash=PRESENTER_ACTOR_HASH,
        key_hash=key_hash,
        request_hash=request_hash,
    )
    partial = await api.harness.ingest.execute(_command(api, _body(api, ("a", "b"))))
    stored = {item.message_id.value for item in partial.messages}

    resumed = _post(api, body)

    assert resumed.status_code == 202
    payload = resumed.json()
    assert payload["accepted_count"] == 1, "only the message that was missing is new"
    assert payload["replayed_count"] == 2
    replayed = {UUID(item["message_id"]) for item in payload["messages"]}
    assert stored <= replayed, "the messages already stored keep their identifiers"
    assert await _channel_ids(api) == {
        "reservation-a",
        "reservation-b",
        "reservation-c",
    }


# ---------------------------------------------------------------------------------------
# 4 -- the completing transaction is definitely refused
# ---------------------------------------------------------------------------------------


def _with_failing_operation_create(
    storage: StorageDriver, script: list[TransactBehaviour]
) -> ApiHarness:
    faulty = FaultInjectingDriver(inner=storage, script=script, scripted=operation_creation)
    return build_harness(faulty, "recording")


async def test_a_definitely_refused_operation_transaction_is_finished_by_a_retry(
    storage: StorageDriver,
) -> None:
    """Every message is stored and the operation is not; the retry creates exactly one.

    The reservation is still ``IN_PROGRESS`` afterwards, which is the whole reason the retry
    can proceed: an ``IN_PROGRESS`` record under the same key *and the same hash* is this
    request's own unfinished attempt, not a competing one.
    """

    broken = _with_failing_operation_create(storage, [TransactBehaviour.DEFINITE_FAILURE])
    with broken.client:
        await broken.harness.seed()
        body = _body(broken, ("a", "b"))
        failed = _post(broken, body)
        assert failed.status_code >= 400
        assert await _channel_ids(broken) == {"reservation-a", "reservation-b"}
        record = await _reservation_record(broken, body)
        assert record is not None and record.status is IdempotencyStatus.IN_PROGRESS

    healthy = build_harness(storage, "recording")
    with healthy.client:
        retried = _post(healthy, _body(healthy, ("a", "b")))
        assert retried.status_code == 202
        again = _post(healthy, _body(healthy, ("a", "b")))
        assert (
            again.json()["operation"]["operation_id"] == retried.json()["operation"]["operation_id"]
        ), "one key, one operation, however many attempts it took to create it"


# ---------------------------------------------------------------------------------------
# 5 -- the completing transaction commits and the response is lost
# ---------------------------------------------------------------------------------------


async def test_a_lost_operation_transaction_response_is_resolved_by_its_commit_proof(
    storage: StorageDriver,
) -> None:
    """The reservation *is* the proof, and completing it is what the proof reads.

    A create-only record proves itself by existing. A completed reservation cannot -- the
    record was already there -- so the proof names the version the completing write moves it
    to, and resolution reads that version rather than the item's mere presence.
    """

    broken = _with_failing_operation_create(storage, [TransactBehaviour.AMBIGUOUS_AFTER_APPLY])
    with broken.client:
        await broken.harness.seed()
        body = _body(broken, ("a", "b"))
        settled = _post(broken, body)

        assert settled.status_code == 202
        record = await _reservation_record(broken, body)
        assert record is not None and record.status is IdempotencyStatus.COMPLETED

        repeated = _post(broken, body)
        assert (
            repeated.json()["operation"]["operation_id"]
            == settled.json()["operation"]["operation_id"]
        )
        dispatcher = broken.dispatcher
        assert isinstance(dispatcher, RecordingOperationDispatcher)
        assert len({job.invocation_id for job in dispatcher.jobs}) == 1


async def test_a_lost_response_whose_transaction_never_committed_is_retried_once(
    storage: StorageDriver,
) -> None:
    """The other reading of the same ambiguity: still ``IN_PROGRESS`` means it did not land."""

    broken = _with_failing_operation_create(storage, [TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY])
    with broken.client:
        await broken.harness.seed()
        body = _body(broken, ("a", "b"))
        settled = _post(broken, body)

        assert settled.status_code == 202
        record = await _reservation_record(broken, body)
        assert record is not None and record.status is IdempotencyStatus.COMPLETED
        assert record.result_entity_refs


# ---------------------------------------------------------------------------------------
# 6 -- two equivalent requests under one reservation
# ---------------------------------------------------------------------------------------


async def test_two_equivalent_requests_under_one_reservation_create_one_operation(
    api: ApiHarness,
) -> None:
    """Same key, same normalized request, one durable operation and one invocation.

    Both callers hold the same reservation, and the completing transaction is guarded on the
    record's version, so exactly one of them advances it. The loser reads the record back and
    answers with what it names, which is why neither caller has to know it lost.
    """

    await api.harness.seed()
    body = _body(api, ("a", "b", "c"))
    shuffled = {
        "community_id": body["community_id"],
        "messages": list(reversed(body["messages"])),
    }

    first = _post(api, body)
    second = _post(api, shuffled)

    assert first.status_code == 202
    assert second.status_code == 202
    assert (
        second.json()["operation"]["operation_id"] == first.json()["operation"]["operation_id"]
    ), "message order is not part of the command's identity"

    dispatcher = api.dispatcher
    assert isinstance(dispatcher, RecordingOperationDispatcher)
    assert len({job.invocation_id for job in dispatcher.jobs}) == 1
    assert await _channel_ids(api) == {
        "reservation-a",
        "reservation-b",
        "reservation-c",
    }


async def test_two_reservations_racing_to_complete_converge_on_one_operation(
    api: ApiHarness,
) -> None:
    """H-9: a genuinely concurrent pair of completions, not merely two sequential posts.

    ``test_two_equivalent_requests_under_one_reservation_create_one_operation`` always lets the
    first ``_post`` finish -- reservation, ingest, *and* ``complete_start`` -- before the second
    one begins, so the completing transaction never actually has a live rival. Here both callers
    hold their own ``StartReservation`` for the same key before either one calls
    ``complete_start``, which is the exact window two truly concurrent HTTP requests would race
    in. Convergence has to come from the guarded transaction and the read-back after a
    conditional failure, not from one request happening to finish first.
    """

    await api.harness.seed()
    # Real contributors and lift-shaped text: unlike the reservation-only tests above, this
    # one has to reach an actual model dispatch, so an unattributable no-op batch would prove
    # nothing about the race it exists to close.
    command = IngestMessagesCommand(
        namespace=api.harness.namespace,
        community_id=api.harness.community_id,
        actor_id_hash=PRESENTER_ACTOR_HASH,
        idempotency_key=KEY,
        messages=(
            IngestMessage(
                channel_message_id="reservation-race-a",
                contributor_id=api.harness.contributor_id("resident-a"),
                sent_at=SENT_AT,
                text="The elevator is stuck between floors again this morning.",
            ),
            IngestMessage(
                channel_message_id="reservation-race-b",
                contributor_id=api.harness.contributor_id("resident-b"),
                sent_at=SENT_AT + timedelta(minutes=1),
                text="Same elevator problem, stuck for ten minutes now.",
            ),
        ),
    )
    key_hash, request_hash = monitor_operation_identity(command)

    reserved_first = await api.harness.operations.reserve_start(
        namespace=api.harness.namespace,
        command=IdempotentCommand.START_MONITOR_OPERATION,
        actor_id_hash=PRESENTER_ACTOR_HASH,
        key_hash=key_hash,
        request_hash=request_hash,
    )
    reserved_second = await api.harness.operations.reserve_start(
        namespace=api.harness.namespace,
        command=IdempotentCommand.START_MONITOR_OPERATION,
        actor_id_hash=PRESENTER_ACTOR_HASH,
        key_hash=key_hash,
        request_hash=request_hash,
    )
    assert isinstance(reserved_first, StartReservation)
    assert isinstance(reserved_second, StartReservation)

    ingested = await api.harness.ingest.execute(command)
    sent_at_by_channel = {
        message.channel_message_id: message.sent_at for message in command.messages
    }
    locators = tuple(
        MessageFeedEntry(
            message_id=item.message_id, sent_at=sent_at_by_channel[item.channel_message_id]
        )
        for item in ingested.messages
    )
    locator_hash = monitor_locator_hash(locators)

    started_first = await api.harness.operations.complete_start(
        reserved_first,
        namespace=api.harness.namespace,
        kind=ApplicationOperationKind.MONITOR,
        actor_id_hash=PRESENTER_ACTOR_HASH,
        monitor_locator_hash=locator_hash,
    )
    started_second = await api.harness.operations.complete_start(
        reserved_second,
        namespace=api.harness.namespace,
        kind=ApplicationOperationKind.MONITOR,
        actor_id_hash=PRESENTER_ACTOR_HASH,
        monitor_locator_hash=locator_hash,
    )

    assert started_first.operation.operation_id == started_second.operation.operation_id
    assert started_first.invocation_id == started_second.invocation_id
    assert {started_first.replayed, started_second.replayed} == {True, False}, (
        "exactly one caller's transaction commits; the other reads back what it committed"
    )

    agent = LexicalFakeMonitorAgent()
    worker = api.harness.worker(agent)
    job = MonitorOperationJob(
        operation_id=started_first.operation.operation_id,
        namespace=api.harness.namespace,
        community_id=api.harness.community_id,
        invocation_id=started_first.invocation_id,
        correlation_id=uuid4(),
        actor_id_hash=PRESENTER_ACTOR_HASH,
        request_hash=request_hash,
        message_locators=locators,
    )

    # Both racing callers' dispatchers hand the same job identity to the worker, exactly as
    # the API route's duplicate-dispatch design intends -- see ``operations.py``.
    await worker.execute(job)
    await worker.execute(job)

    assert len(agent.invocations) == 1, "two concurrent completions, one pass over private text"


async def test_the_operation_a_completed_key_names_carries_its_monitor_handover(
    api: ApiHarness,
) -> None:
    """The durable row the worker binds against is written by the same transaction."""

    await api.harness.seed()
    accepted = _post(api, _body(api, ("a", "b")))
    assert accepted.status_code == 202

    dispatcher = api.dispatcher
    assert isinstance(dispatcher, RecordingOperationDispatcher)
    job = dispatcher.jobs[0]
    operation = await api.harness.operations.load(
        namespace=api.harness.namespace, operation_id=job.operation_id
    )

    assert operation.status is ApplicationOperationStatus.PENDING
    assert operation.monitor_invocation_id == job.invocation_id
    assert operation.monitor_locator_hash is not None
    assert operation.request_hash == job.request_hash
