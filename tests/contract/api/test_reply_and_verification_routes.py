"""The two Phase-9 surfaces, checked at the transport: what a caller may say, and to whom.

The reply route's body is the whole point. It carries **one field**, a fixture selector, and
there is no schema here that could accept a case, an action, a destination, a sender, a received
time, a subject, or a body -- the six provenance fields the old body carried are gone from the
API surface entirely ([ADR-026](../../../docs/adr/ADR-026-inbound-reply-trust-and-correlation.md)
§ Context).

The verification route's body is the other half: it carries no contributor identifier, because
who is deciding is resolved from the authenticated persona and whether they are *affected* is
decided against loaded case facts. A body that could name either would be a body that could
impersonate the person the whole edge exists to require.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from chorus_api.routes.commitments import VerifyCommitmentRequest
from chorus_api.routes.replies import DeliverFixtureReplyRequest
from pydantic import ValidationError
from tests.contract.api.conftest import ApiHarness

pytestmark = pytest.mark.anyio

FORBIDDEN_REPLY_FIELDS = (
    "case_id",
    "action_id",
    "channel_message_id",
    "received_at",
    "from_destination_id",
    "subject",
    "text",
)
"""Exactly the body ADR-026 removed. Each one is asserted absent, not merely unused."""


def test_the_reply_body_carries_one_field_and_it_is_a_selector() -> None:
    assert set(DeliverFixtureReplyRequest.model_fields) == {"fixture_id"}


@pytest.mark.parametrize("field", FORBIDDEN_REPLY_FIELDS)
def test_the_reply_body_refuses_every_provenance_field_it_used_to_take(field: str) -> None:
    """T18 with the attacker handed the pen. There is nowhere to put any of it."""

    with pytest.raises(ValidationError):
        DeliverFixtureReplyRequest(fixture_id="manager-promise", **{field: "anything"})


def test_a_fixture_selector_is_a_key_and_never_a_path() -> None:
    """A selector that could carry a separator would be a selector that could name a file."""

    for value in ("../etc/passwd", "manager promise", "MANAGER-PROMISE", "manager/promise"):
        with pytest.raises(ValidationError):
            DeliverFixtureReplyRequest(fixture_id=value)


def test_the_verification_body_carries_no_contributor_identifier() -> None:
    """Who is deciding comes from the authenticated persona, never from the request."""

    fields = set(VerifyCommitmentRequest.model_fields)

    assert fields == {"expected_version", "outcome", "note", "fixture_evidence_id"}
    assert not fields & {"contributor_id", "actor", "verified_by", "pseudonym"}


@pytest.mark.parametrize("outcome", ["CANCELLED", "DISPUTED", "PENDING", "RESOLVED"])
def test_the_verification_body_admits_only_the_two_human_outcomes(outcome: str) -> None:
    """There is no cancellation route in V1, and there is no ``DISPUTED`` status at all."""

    with pytest.raises(ValidationError):
        VerifyCommitmentRequest(expected_version=1, outcome=outcome)


async def test_only_the_presenter_may_deliver_a_fixture_reply(api: ApiHarness) -> None:
    response = api.client.post(
        "/v1/demo/external-replies",
        json={"fixture_id": "manager-promise"},
        headers=api.actor_headers("resident_a", **{"Idempotency-Key": "reply-key-0001"}),
    )

    assert response.status_code == 403


async def test_an_unwired_inbound_boundary_answers_503_rather_than_202(
    api: ApiHarness,
) -> None:
    """A deployment with no attester has nothing that can turn a delivery into evidence.

    Answering ``202`` and quietly writing nothing would be the worst of the three available
    answers, and it is the one a route with no explicit refusal would give.
    """

    response = api.client.post(
        "/v1/demo/external-replies",
        json={"fixture_id": "manager-promise"},
        headers=api.presenter_headers(**{"Idempotency-Key": "reply-key-0001"}),
    )

    assert response.status_code == 503


async def test_only_a_resident_may_verify_a_commitment(api: ApiHarness) -> None:
    """Watching a commitment and saying whether it was kept are different powers."""

    response = api.client.post(
        f"/v1/cases/{uuid4()}/commitments/{uuid4()}/verification",
        json={"expected_version": 1, "outcome": "FULFILLED"},
        headers=api.presenter_headers(**{"Idempotency-Key": "verify-key-0001"}),
    )

    assert response.status_code == 403


async def test_an_unwired_verification_surface_answers_503(api: ApiHarness) -> None:
    response = api.client.post(
        f"/v1/cases/{uuid4()}/commitments/{uuid4()}/verification",
        json={"expected_version": 1, "outcome": "FULFILLED"},
        headers=api.actor_headers("resident_a", **{"Idempotency-Key": "verify-key-0001"}),
    )

    assert response.status_code == 503


async def test_the_demo_clock_requires_the_presenter_and_a_wired_clock(api: ApiHarness) -> None:
    forbidden = api.client.post(
        "/v1/demo/clock/advance",
        json={"case_id": str(uuid4()), "commitment_id": str(uuid4()), "advance_seconds": 60},
        headers=api.actor_headers("resident_a"),
    )
    unwired = api.client.post(
        "/v1/demo/clock/advance",
        json={"case_id": str(uuid4()), "commitment_id": str(uuid4()), "advance_seconds": 60},
        headers=api.presenter_headers(),
    )

    assert forbidden.status_code == 403
    assert unwired.status_code == 503


async def test_the_demo_clock_only_moves_forward_and_only_so_far(api: ApiHarness) -> None:
    """An unbounded advance would take every future deadline past due at once."""

    for seconds in (0, -60, 60 * 60 * 24 * 61):
        response = api.client.post(
            "/v1/demo/clock/advance",
            json={
                "case_id": str(uuid4()),
                "commitment_id": str(uuid4()),
                "advance_seconds": seconds,
            },
            headers=api.presenter_headers(),
        )
        assert response.status_code == 422


def test_the_three_phase_nine_routes_are_mounted(api: ApiHarness) -> None:
    """Read off the generated schema, which is the surface a caller actually sees."""

    paths = set(api.app.openapi()["paths"])

    assert "/v1/demo/external-replies" in paths
    assert "/v1/cases/{case_id}/commitments/{commitment_id}/verification" in paths
    assert "/v1/demo/clock/advance" in paths


def test_no_route_accepts_a_caller_supplied_reply_body(api: ApiHarness) -> None:
    """The six provenance fields are gone from the whole API surface, not just from one model."""

    schema = api.app.openapi()
    reply_body = schema["paths"]["/v1/demo/external-replies"]["post"]["requestBody"]
    reference = reply_body["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    properties = set(schema["components"]["schemas"][reference]["properties"])

    assert properties == {"fixture_id"}
    assert not properties & set(FORBIDDEN_REPLY_FIELDS)
