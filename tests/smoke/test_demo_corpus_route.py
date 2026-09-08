"""P2-7: `GET /v1/demo/corpus` serves the exact seeded corpus, in ingest-ready shape.

Before this route, the only way a browser could replay the seeded corpus to trigger the
Monitor was to hold its own mirror of the checked-in fixture file, which could silently drift
from the one `POST /demo/reset` actually seeds. This test pins that the route's own output can
be posted to `/ingest/messages` unmodified and is treated as an exact replay of what reset
already wrote -- proving there is exactly one copy of this data, not two.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from tests.smoke.conftest import PRESENTER

from chorus.composition.local import LocalComposition

pytestmark = pytest.mark.anyio


def _reset(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/v1/demo/reset",
        headers={**PRESENTER, "Idempotency-Key": "corpus-route-reset-0001"},
        json={"namespace": "DEMO", "confirm": "RESET DEMO", "seed_version": "elevator/v1"},
    )
    assert response.status_code == 200, response.text
    return cast("dict[str, Any]", response.json())


async def test_the_corpus_route_requires_the_presenter_persona(client: TestClient) -> None:
    _reset(client)
    denied = client.get("/v1/demo/corpus", headers={"X-Chorus-Demo-Actor": "resident_a"})
    assert denied.status_code == 403, denied.text


async def test_the_corpus_matches_the_reset_receipt(client: TestClient) -> None:
    reset_body = _reset(client)

    response = client.get("/v1/demo/corpus", headers=PRESENTER)
    assert response.status_code == 200, response.text
    corpus = response.json()

    assert corpus["seed_version"] == reset_body["seed_version"]
    assert corpus["corpus_sha256"] == reset_body["corpus_sha256"]
    assert corpus["community_id"] == reset_body["community_id"]
    assert len(corpus["messages"]) == reset_body["counts"]["messages"] == 24

    # Every resident contributor id the route resolves must agree with reset's own mapping --
    # this is the same seeded identity, read through a second route, never a guess.
    contributor_by_actor = {c["actor"]: c["contributor_id"] for c in reset_body["contributors"]}
    resident_contributor_ids = set(contributor_by_actor.values())
    corpus_contributor_ids = {
        m["contributor_id"] for m in corpus["messages"] if m["contributor_id"]
    }
    assert resident_contributor_ids <= corpus_contributor_ids


async def test_the_corpus_route_output_replays_exactly_into_ingest(
    client: TestClient, composition: LocalComposition
) -> None:
    reset_body = _reset(client)
    corpus = client.get("/v1/demo/corpus", headers=PRESENTER).json()

    ingest_response = client.post(
        "/v1/ingest/messages",
        headers={**PRESENTER, "Idempotency-Key": "corpus-route-ingest-0001"},
        json={"community_id": corpus["community_id"], "messages": corpus["messages"]},
    )
    assert ingest_response.status_code == 202, ingest_response.text
    ingest_body = ingest_response.json()

    # Every message is recognized as an exact replay of what reset already seeded -- proving
    # the route's own output is byte-identical to the corpus reset wrote, not a re-derivation.
    assert ingest_body["accepted_count"] == 0
    assert ingest_body["replayed_count"] == len(corpus["messages"]) == 24

    await composition.dispatcher.drain()
    operation = client.get(
        f"/v1/operations/{ingest_body['operation']['operation_id']}", headers=PRESENTER
    ).json()
    assert operation["status"] == "SUCCEEDED", operation

    feed = client.get(
        "/v1/feed", headers=PRESENTER, params={"community_id": reset_body["community_id"]}
    ).json()
    assert any(item["chorus_signal"] for item in feed["items"]), "the Monitor found no pattern"
