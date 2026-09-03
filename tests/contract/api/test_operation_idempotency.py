"""The idempotency key binds the operation, and discovery converges across batch shapes.

Two properties are asserted through the real FastAPI application, because both were previously
true only of code that nothing exercised end to end.

The first is that ``Idempotency-Key`` means what a caller assumes it means. A retried POST must
not mint a second operation, a second invocation identity, or a second agent execution --
otherwise the safest thing a nervous client can do, retrying, is the most expensive thing it
can do: another pass over private community text.

The second is that *how* a batch is delivered does not change what is discovered. The same
twenty-four messages posted once and posted as four requests of six describe the same building
and the same failing lift, and a system that answered "one case" to the first and "three
fragments" to the second would be reporting an artefact of its own request boundaries.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.contract.api.conftest import ApiHarness

from chorus.domain.entities import ApplicationOperationStatus
from chorus.domain.ids import CaseId, OperationId
from chorus.infrastructure.local.dispatch import (
    DispatchFailedError,
    RecordingOperationDispatcher,
)
from chorus.infrastructure.local.monitor_agent import LexicalFakeMonitorAgent
from chorus.ports.pagination import PageRequest
from chorus.ports.scopes import CaseScope

pytestmark = pytest.mark.anyio

KEY = "operation-idempotency-key-0001"


def _payload(api: ApiHarness, *, count: int = 3, offset: int = 0) -> dict[str, Any]:
    corpus = api.harness.adapter.messages()
    return {
        "community_id": str(api.harness.community_id),
        "messages": [
            {
                "channel_message_id": message.channel_message_id,
                "contributor_id": (
                    None
                    if message.contributor_pseudonym is None
                    else str(api.harness.contributor_id(message.contributor_pseudonym))
                ),
                "sent_at": message.sent_at.isoformat().replace("+00:00", "Z"),
                "text": message.text,
            }
            for message in corpus[offset : offset + count]
        ],
    }


def _post(api: ApiHarness, body: dict[str, Any], *, key: str = KEY) -> Any:
    return api.client.post(
        "/v1/ingest/messages",
        json=body,
        headers=api.presenter_headers(**{"Idempotency-Key": key}),
    )


async def test_three_identical_posts_reach_one_operation_and_one_invocation(
    api: ApiHarness,
) -> None:
    """Repeats reach one operation and one invocation identity, however often they dispatch.

    The recording dispatcher never runs the worker, so the operation stays ``PENDING`` and
    every repeat re-dispatches it. That is the intended behaviour and not the property under
    test: what a caller is promised is one ``operation_id`` and one ``invocation_id``, and
    what stops a second model call is the worker's conditional claim, not the dispatcher's
    restraint.
    """

    await api.harness.seed()
    body = _payload(api)

    responses = [_post(api, body) for _ in range(3)]

    assert [response.status_code for response in responses] == [202, 202, 202]
    operation_ids = {response.json()["operation"]["operation_id"] for response in responses}
    assert len(operation_ids) == 1

    dispatcher = api.dispatcher
    assert isinstance(dispatcher, RecordingOperationDispatcher)
    assert {str(job.operation_id) for job in dispatcher.jobs} == operation_ids
    assert len({job.invocation_id for job in dispatcher.jobs}) == 1


async def test_repeated_dispatch_of_one_pending_operation_still_runs_the_model_once(
    api: ApiHarness,
) -> None:
    """Duplicate dispatch is safe because the claim, not the dispatcher, is the boundary."""

    await api.harness.seed()
    body = _payload(api)

    _post(api, body)
    _post(api, body)

    dispatcher = api.dispatcher
    assert isinstance(dispatcher, RecordingOperationDispatcher)
    assert len(dispatcher.jobs) == 2, "an undispatched PENDING operation is dispatched again"

    agent = LexicalFakeMonitorAgent()
    worker = api.harness.worker(agent)
    first = await worker.execute(dispatcher.jobs[0])
    second = await worker.execute(dispatcher.jobs[1])

    assert first.status is ApplicationOperationStatus.SUCCEEDED
    assert second.status is ApplicationOperationStatus.SUCCEEDED
    assert second.operation_id == first.operation_id
    assert len(agent.invocations) == 1, "two deliveries, one pass over private text"


async def test_a_dispatch_failure_is_recovered_by_the_next_identical_post(
    api: ApiHarness,
) -> None:
    """The regression: a lost dispatch used to strand a persisted operation forever.

    The durable operation is written before anything is handed over, so a dispatcher that
    fails afterwards leaves a record that looks entirely healthy -- ``PENDING``, with an
    invocation identity bound to it -- and no worker anywhere that knows about it. The old
    rule dispatched only when the request had *created* the operation, so every retry after
    that point politely declined to start work that had never been started.
    """

    await api.harness.seed()
    body = _payload(api)
    dispatcher = api.dispatcher
    assert isinstance(dispatcher, RecordingOperationDispatcher)
    dispatcher.failures = 1

    with pytest.raises(DispatchFailedError):
        _post(api, body)
    assert dispatcher.jobs == [], "the handover never reached anything"

    second = _post(api, body)

    assert second.status_code == 202
    assert len(dispatcher.jobs) == 1, "the same job identity is handed over again"
    operation_id = second.json()["operation"]["operation_id"]
    assert str(dispatcher.jobs[0].operation_id) == operation_id
    assert second.json()["operation"]["status"] == "PENDING"

    finished = await api.harness.worker(LexicalFakeMonitorAgent()).execute(dispatcher.jobs[0])
    assert finished.status is ApplicationOperationStatus.SUCCEEDED
    assert str(finished.operation_id) == operation_id


async def test_a_replayed_post_reuses_the_original_invocation_identity(
    api: ApiHarness,
) -> None:
    """The invocation identity survives the replay, not just the operation identity."""

    await api.harness.seed()
    body = _payload(api)

    first = _post(api, body)
    dispatcher = api.dispatcher
    assert isinstance(dispatcher, RecordingOperationDispatcher)
    original_invocation = dispatcher.jobs[0].invocation_id

    second = _post(api, body)

    assert second.json()["operation"] == first.json()["operation"]
    assert {job.invocation_id for job in dispatcher.jobs} == {original_invocation}


async def test_the_same_key_with_a_different_body_is_a_conflict(api: ApiHarness) -> None:
    await api.harness.seed()

    _post(api, _payload(api, count=3, offset=0))
    conflicting = _post(api, _payload(api, count=3, offset=3))

    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "IDEMPOTENCY_CONFLICT"
    dispatcher = api.dispatcher
    assert isinstance(dispatcher, RecordingOperationDispatcher)
    assert len(dispatcher.jobs) == 1


async def test_the_request_hash_ignores_message_order_but_not_message_content(
    api: ApiHarness,
) -> None:
    """``[A,B]`` and ``[B,A]`` are one command; different content is still a conflict.

    The endpoint takes a *batch*, and Monitor processing canonicalizes and orders it anyway,
    so array order is a transport detail. A client that shuffled its array on a retry -- an
    ORM iterating a set, a proxy re-serializing -- must not be told that its own request
    conflicts with itself, because the only safe response to that is a new idempotency key,
    which is exactly the second model invocation the key exists to prevent.

    The earlier version of this test never actually reordered anything: it posted the same
    body twice and then edited a field, so it proved only that identical bodies replay.
    """

    await api.harness.seed()
    body = _payload(api, count=3)
    reversed_body = {**body, "messages": list(reversed(body["messages"]))}
    assert reversed_body["messages"] != body["messages"], "the probe must actually reorder"

    first = _post(api, body)
    replayed = _post(api, reversed_body)

    assert first.status_code == 202
    assert replayed.status_code == 202
    assert replayed.json()["operation"] == first.json()["operation"]

    # Attachment order is a transport detail too, and for the same reason. Fresh messages,
    # because re-posting an already-ingested channel message with attachments it did not have
    # is a genuinely different message and conflicts for a different, correct reason.
    fresh = _payload(api, count=1, offset=8)
    with_attachments = {
        **fresh,
        "messages": [{**fresh["messages"][0], "attachments": [_attachment(0), _attachment(1)]}],
    }
    shuffled_attachments = {
        **fresh,
        "messages": [{**fresh["messages"][0], "attachments": [_attachment(1), _attachment(0)]}],
    }
    assert _post(api, with_attachments, key="attachment-order-key-01").status_code == 202
    assert _post(api, shuffled_attachments, key="attachment-order-key-01").status_code == 202

    # Content that genuinely differs is still a conflict.
    edited = {**body, "messages": [{**body["messages"][0], "text": "something else entirely"}]}
    assert _post(api, edited).status_code == 409


def _attachment(index: int) -> dict[str, Any]:
    return {
        "evidence_id": f"00000000-0000-4000-8000-00000000000{index}",
        "media_type": "image/png",
        "byte_length": 1024 + index,
        "sha256": "sha256:" + str(index) * 64,
    }


async def test_a_replayed_post_still_returns_the_messages_it_persisted(
    api: ApiHarness,
) -> None:
    await api.harness.seed()
    body = _payload(api)

    first = _post(api, body).json()
    second = _post(api, body).json()

    assert [item["message_id"] for item in first["messages"]] == [
        item["message_id"] for item in second["messages"]
    ]
    assert second["accepted_count"] == 0
    assert second["replayed_count"] == len(body["messages"])


# ---------------------------------------------------------------------------------------
# Split-batch convergence
# ---------------------------------------------------------------------------------------


async def _case_shape(api: ApiHarness, operation_id: str) -> tuple[int, set[str]]:
    """How many cases one run produced, and which contributors they linked."""

    operation = await api.harness.operations.load(
        namespace=api.harness.namespace, operation_id=OperationId(_uuid(operation_id))
    )
    contributors: set[str] = set()
    for case_ref in operation.result_refs:
        scope = CaseScope(
            namespace=api.harness.namespace,
            community_id=api.harness.community_id,
            case_id=CaseId(case_ref),
        )
        reports = await api.harness.core.read_case_reports(scope, PageRequest(limit=100))
        contributors |= {str(report.contributor_id) for report in reports.items}
    return len(operation.result_refs), contributors


def _uuid(value: str) -> Any:
    from uuid import UUID

    return UUID(value)


async def test_one_batch_of_twenty_four_converges_on_a_single_candidate(
    live_api: ApiHarness,
) -> None:
    await live_api.harness.seed()
    corpus = live_api.harness.adapter.messages()

    responses = [
        _post(
            live_api,
            _payload(live_api, count=len(corpus[:24]), offset=0),
            key="one-batch-key-000001",
        )
    ]
    await live_api.dispatcher.drain()  # type: ignore[union-attr]

    assert responses[0].status_code == 202
    count, contributors = await _case_shape(
        live_api, responses[0].json()["operation"]["operation_id"]
    )
    assert count == 1
    assert len(contributors) >= 3


async def test_four_batches_of_six_converge_on_the_same_single_candidate(
    live_api: ApiHarness,
) -> None:
    """The batch boundary is a transport detail; it must not become a case boundary.

    Each request sees only its own six messages *plus* the bounded recent-message window the
    use case builds, which is what lets the fourth batch recognise the pattern the first three
    started. Without that window each request would be a separate world and this test would
    find four fragments.
    """

    await live_api.harness.seed()

    operation_ids: list[str] = []
    for index in range(4):
        response = _post(
            live_api,
            _payload(live_api, count=6, offset=index * 6),
            key=f"split-batch-key-{index:04d}",
        )
        assert response.status_code == 202
        operation_ids.append(response.json()["operation"]["operation_id"])
        await live_api.dispatcher.drain()  # type: ignore[union-attr]

    linked: set[str] = set()
    cases: set[str] = set()
    for operation_id in operation_ids:
        operation = await live_api.harness.operations.load(
            namespace=live_api.harness.namespace, operation_id=OperationId(_uuid(operation_id))
        )
        cases |= {str(ref) for ref in operation.result_refs}
        for case_ref in operation.result_refs:
            scope = CaseScope(
                namespace=live_api.harness.namespace,
                community_id=live_api.harness.community_id,
                case_id=CaseId(case_ref),
            )
            reports = await live_api.harness.core.read_case_reports(scope, PageRequest(limit=100))
            linked |= {str(report.contributor_id) for report in reports.items}

    assert len(cases) == 1, "four requests must not produce four fragmented cases"
    assert len(linked) >= 3


async def test_the_ingest_request_cannot_name_a_case_to_extend(api: ApiHarness) -> None:
    """A client that could name a case would be doing the discovering."""

    await api.harness.seed()
    body = {**_payload(api), "candidate_case_ids": [str(api.harness.community_id)]}

    response = _post(api, body)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
