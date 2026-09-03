"""The three Phase 3 routes: ingest, poll, and read the feed.

The API is a transport mapping, so these tests assert the mapping and the guards around it --
the actor gate, idempotency, the ``202`` contract, the correlation header, and the shape of a
Problem Details response. Everything about what discovery *means* is tested against the use
cases, not here.
"""

from __future__ import annotations

import pytest
from tests.contract.api.conftest import ApiHarness

from chorus.domain.entities import ApplicationOperationStatus

pytestmark = pytest.mark.anyio


async def _seeded(api: ApiHarness) -> None:
    await api.harness.seed()


def _payload(api: ApiHarness, count: int = 3) -> dict[str, object]:
    messages = api.harness.adapter.messages()[:count]
    return {
        "community_id": str(api.harness.community_id),
        "messages": [
            {
                "adapter": "SYNTHETIC",
                "channel_message_id": message.channel_message_id,
                "contributor_id": str(
                    api.harness.contributor_id(message.contributor_pseudonym or "")
                ),
                "sent_at": message.sent_at.isoformat().replace("+00:00", "Z"),
                "text": message.text,
                "attachments": [],
            }
            for message in messages
        ],
    }


async def test_ingesting_a_batch_returns_202_and_an_operation_to_poll(
    api: ApiHarness,
) -> None:
    await _seeded(api)

    response = api.client.post(
        "/v1/ingest/messages",
        json=_payload(api),
        headers=api.presenter_headers(**{"Idempotency-Key": "api-key-00000001"}),
    )

    assert response.status_code == 202
    body = response.json()
    assert len(body["messages"]) == 3
    assert body["accepted_count"] == 3
    assert body["replayed_count"] == 0
    assert body["operation"]["status"] == "PENDING"
    assert body["operation"]["poll_url"].startswith("/v1/operations/")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Correlation-Id"]


async def test_the_monitor_job_is_handed_over_rather_than_run_in_the_request(
    api: ApiHarness,
) -> None:
    await _seeded(api)

    api.client.post(
        "/v1/ingest/messages",
        json=_payload(api),
        headers=api.presenter_headers(**{"Idempotency-Key": "api-key-00000002"}),
    )

    assert len(api.dispatcher.jobs) == 1  # type: ignore[union-attr]
    assert len(api.dispatcher.jobs[0].message_locators) == 3  # type: ignore[union-attr]


async def test_replaying_a_batch_reports_the_original_identifiers(
    api: ApiHarness,
) -> None:
    await _seeded(api)
    headers = api.presenter_headers(**{"Idempotency-Key": "api-key-00000003"})

    first = api.client.post("/v1/ingest/messages", json=_payload(api), headers=headers).json()
    again = api.client.post("/v1/ingest/messages", json=_payload(api), headers=headers).json()

    assert [item["message_id"] for item in first["messages"]] == [
        item["message_id"] for item in again["messages"]
    ]
    assert again["replayed_count"] == 3


async def test_the_same_channel_identity_with_new_content_is_a_409(
    api: ApiHarness,
) -> None:
    await _seeded(api)
    headers = api.presenter_headers(**{"Idempotency-Key": "api-key-00000004"})
    api.client.post("/v1/ingest/messages", json=_payload(api), headers=headers)
    tampered = _payload(api)
    tampered["messages"][0]["text"] = "Something else entirely."  # type: ignore[index]

    response = api.client.post(
        "/v1/ingest/messages",
        json=tampered,
        headers=api.presenter_headers(**{"Idempotency-Key": "api-key-00000005"}),
    )

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "IDEMPOTENCY_CONFLICT"
    assert body["retryable"] is False
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_a_request_without_an_actor_is_refused(api: ApiHarness) -> None:
    await _seeded(api)

    response = api.client.post(
        "/v1/ingest/messages",
        json=_payload(api),
        headers={"Idempotency-Key": "api-key-00000006"},
    )

    assert response.status_code == 401


async def test_a_non_presenter_actor_cannot_reach_the_private_feed(
    api: ApiHarness,
) -> None:
    await _seeded(api)

    response = api.client.get(
        f"/v1/feed?community_id={api.harness.community_id}",
        headers={"X-Chorus-Demo-Actor": "resident_a"},
    )

    assert response.status_code == 403


async def test_an_unknown_actor_is_refused(api: ApiHarness) -> None:
    await _seeded(api)

    response = api.client.get(
        f"/v1/feed?community_id={api.harness.community_id}",
        headers={"X-Chorus-Demo-Actor": "root"},
    )

    assert response.status_code == 403


async def test_a_missing_idempotency_key_is_a_validation_failure(
    api: ApiHarness,
) -> None:
    await _seeded(api)

    response = api.client.post(
        "/v1/ingest/messages", json=_payload(api), headers=api.presenter_headers()
    )

    assert response.status_code == 422


async def test_an_unknown_request_field_is_refused(api: ApiHarness) -> None:
    await _seeded(api)
    payload = _payload(api)
    payload["case_id"] = "00000000-0000-4000-8000-000000000000"

    response = api.client.post(
        "/v1/ingest/messages",
        json=payload,
        headers=api.presenter_headers(**{"Idempotency-Key": "api-key-00000007"}),
    )

    assert response.status_code == 422


async def test_the_request_contract_has_no_place_to_name_a_case_or_a_report(
    api: ApiHarness,
) -> None:
    """Discovery cannot be supplied by the caller, so there is no field to supply it in."""

    from chorus_api.routes.ingest import IngestMessageRequest, IngestMessagesRequest

    assert set(IngestMessagesRequest.model_fields) == {"community_id", "messages"}
    assert not {"report_id", "case_id", "issue_type"} & set(IngestMessageRequest.model_fields)


async def test_the_feed_returns_every_message_with_no_signal_before_discovery(
    api: ApiHarness,
) -> None:
    await _seeded(api)
    api.client.post(
        "/v1/ingest/messages",
        json=_payload(api),
        headers=api.presenter_headers(**{"Idempotency-Key": "api-key-00000008"}),
    )

    response = api.client.get(
        f"/v1/feed?community_id={api.harness.community_id}", headers=api.presenter_headers()
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 3
    assert all(item["chorus_signal"] is None for item in body["items"])
    assert all(item["pseudonym"] for item in body["items"])


async def test_an_operation_poll_reports_status_and_never_agent_output(
    api: ApiHarness,
) -> None:
    await _seeded(api)
    body = api.client.post(
        "/v1/ingest/messages",
        json=_payload(api),
        headers=api.presenter_headers(**{"Idempotency-Key": "api-key-00000009"}),
    ).json()

    response = api.client.get(body["operation"]["poll_url"], headers=api.presenter_headers())

    assert response.status_code == 200
    operation = response.json()
    assert operation["status"] == ApplicationOperationStatus.PENDING.value
    assert set(operation) == {
        "operation_id",
        "kind",
        "status",
        "result_refs",
        "error_code",
        "created_at",
        "updated_at",
    }
    assert response.headers["Retry-After"] == "1"


async def test_an_unknown_operation_is_a_404(api: ApiHarness) -> None:
    await _seeded(api)

    response = api.client.get(
        "/v1/operations/00000000-0000-4000-8000-000000000000",
        headers=api.presenter_headers(),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


async def test_the_full_discovery_path_is_visible_through_the_api(
    live_api: ApiHarness,
) -> None:
    """Ingest, let the worker run, poll to SUCCEEDED, and read the signal off the feed."""

    await live_api.harness.seed()
    corpus = live_api.harness.adapter.messages()
    body: dict[str, object] = {}
    for index in range(0, len(corpus), 25):
        payload = {
            "community_id": str(live_api.harness.community_id),
            "messages": [
                {
                    "adapter": "SYNTHETIC",
                    "channel_message_id": message.channel_message_id,
                    "contributor_id": str(
                        live_api.harness.contributor_id(message.contributor_pseudonym or "")
                    ),
                    "sent_at": message.sent_at.isoformat().replace("+00:00", "Z"),
                    "text": message.text,
                    "attachments": [
                        {
                            "evidence_id": str(attachment.evidence_id),
                            "media_type": attachment.media_type,
                            "byte_length": attachment.byte_length,
                            "sha256": attachment.sha256.value,
                        }
                        for attachment in message.attachments
                    ],
                }
                for message in corpus[index : index + 25]
            ],
        }
        body = live_api.client.post(
            "/v1/ingest/messages",
            json=payload,
            headers=live_api.presenter_headers(**{"Idempotency-Key": f"api-live-key-{index:04d}"}),
        ).json()

    await live_api.dispatcher.drain()  # type: ignore[union-attr]

    operation = live_api.client.get(
        f"/v1/operations/{body['operation']['operation_id']}",  # type: ignore[index]
        headers=live_api.presenter_headers(),
    ).json()
    assert operation["status"] == "SUCCEEDED"
    assert len(operation["result_refs"]) == 1

    feed = live_api.client.get(
        f"/v1/feed?community_id={live_api.harness.community_id}&limit=100",
        headers=live_api.presenter_headers(),
    ).json()
    signalled = [item for item in feed["items"] if item["chorus_signal"] is not None]
    assert len(feed["items"]) == 24
    assert signalled
    assert {item["chorus_signal"]["candidate_case_id"] for item in signalled} == set(
        operation["result_refs"]
    )
    assert all(item["chorus_signal"]["status"] == "CANDIDATE" for item in signalled)
    assert all(item["chorus_signal"]["related_count"] >= 2 for item in signalled)
