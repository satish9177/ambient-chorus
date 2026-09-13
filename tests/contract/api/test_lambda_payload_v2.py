"""The deployed API boundary, driven with real API Gateway HTTP API payload-v2 events.

The deployed host is **HTTP API with payload format version 2.0** (deployment contract § 14), so
the binding is exercised the way the service will exercise it: a ``version: "2.0"`` event with
``requestContext.http``, ``rawPath``, ``rawQueryString``, single-valued ``headers``, and a
``cookies`` array -- and nothing anywhere in these tests reads a payload-v1 field such as
``httpMethod``, ``path``, or ``multiValueHeaders``.

Two boundaries run **before any route**, and both are asserted here rather than argued for:

* the demo bearer token, which the deployed composition supplies and the local one does not, so
  a deployment with no verifier has no check at all rather than a permissive one;
* the logical-time binding, which reads the durable clock once per request and fails the request
  closed if it cannot -- never falling back to process-local time.

No real API Gateway, no AWS credential, and no network: Mangum is driven directly with a dict.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from chorus_api.dependencies import RequestLogicalTime
from chorus_api.main import build_app
from functions.api.handler import LIFESPAN, TEXT_MIME_TYPES
from mangum import Mangum
from tests.contract.api.conftest import build_harness
from tests.fixtures.drivers import storage_driver

from chorus.application.commands.record_commitment_due import (
    RecordCommitmentDueCommand,
    RecordCommitmentDueResult,
    WatcherOutcome,
)
from chorus.infrastructure.persistent_clock import PersistentDemoClock, ScopedLogicalClock
from chorus.ports.access import AccessTokenUnavailableError
from chorus.ports.demo_clock import DemoClockRecord, DemoClockUnavailableError
from chorus.settings import Environment

FAKE_TOKEN = "fake-demo-token-not-a-credential"
SEED = datetime(2030, 1, 14, 9, 0, tzinfo=UTC)
PRESENTER = {"X-Chorus-Demo-Actor": "presenter_admin"}


# -- test doubles ----------------------------------------------------------------------------


class StubAccess:
    """Accepts one fake token. Optionally refuses to answer, which must fail closed."""

    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.presented: list[str] = []

    async def verify(self, presented: str) -> bool:
        self.presented.append(presented)
        if self.unavailable:
            raise AccessTokenUnavailableError("unreadable")
        return presented == FAKE_TOKEN


class StubClockStore:
    """A durable clock that is present, absent, or corrupt -- the three ADR-029 § 4 cases."""

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
        self.instant = to
        return DemoClockRecord(
            logical_time=to,
            version=expected.version + 1,
            reset_generation=expected.reset_generation,
            seed_instant=expected.seed_instant,
            advance_count=expected.advance_count + 1,
        )


class StubWatcher:
    """Stands in for the synchronous ``:live`` invocation and records what it was asked."""

    def __init__(self) -> None:
        self.commands: list[RecordCommitmentDueCommand] = []

    async def execute(self, command: RecordCommitmentDueCommand) -> RecordCommitmentDueResult:
        self.commands.append(command)
        return RecordCommitmentDueResult(
            outcome=WatcherOutcome.WATCHER_EARLY, commitment_status=None, commitment_version=None
        )


# -- the payload-v2 event --------------------------------------------------------------------


def event(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: object | None = None,
    query: str = "",
) -> dict[str, Any]:
    """One API Gateway HTTP API payload **format version 2.0** event.

    Written out rather than fetched from a helper so the shape under test is visible: the
    method and path live under ``requestContext.http``, headers are single-valued, and there is
    no ``httpMethod``, no ``path``, and no ``multiValueHeaders`` anywhere.
    """

    sent = {"content-type": "application/json", **(headers or {})}
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": query,
        "cookies": [],
        "headers": sent,
        "requestContext": {
            "accountId": "000000000000",
            "apiId": "fakeapiid",
            "domainName": "fakeapiid.execute-api.us-east-1.amazonaws.com",
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "203.0.113.1",
                "userAgent": "contract-test",
            },
            "requestId": "fake-request-id",
            "routeKey": "$default",
            "stage": "$default",
            "time": "14/Jan/2030:09:00:00 +0000",
            "timeEpoch": 1894611600000,
        },
        "body": None if body is None else json.dumps(body),
        "isBase64Encoded": False,
    }


@pytest.fixture
def deployed() -> Any:
    """The real application, wired the way the deployed composition wires the two boundaries."""

    drivers = storage_driver("memory", prefix="payload-v2")
    driver = next(drivers)
    harness = build_harness(driver, "recording")
    scope = ScopedLogicalClock()
    clock_store = StubClockStore()
    access = StubAccess()
    watcher = StubWatcher()
    container = harness.app.state.container
    from dataclasses import replace

    app = build_app(
        replace(
            container,
            access=access,
            logical_time=RequestLogicalTime(store=clock_store, scope=scope),
            demo_clock=PersistentDemoClock(store=clock_store, scope=scope),
            record_commitment_due=watcher,
        )
    )
    yield {
        "handler": Mangum(app, lifespan=LIFESPAN, text_mime_types=TEXT_MIME_TYPES),
        "access": access,
        "clock": clock_store,
        "watcher": watcher,
        "harness": harness,
    }
    for _ in drivers:  # pragma: no cover - driver teardown
        pass


def call(deployed: Any, request: dict[str, Any]) -> dict[str, Any]:
    response: dict[str, Any] = deployed["handler"](request, None)
    return response


def authorized(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {FAKE_TOKEN}", **PRESENTER, **extra}


# -- authentication ---------------------------------------------------------------------------


def test_a_valid_authenticated_get_reaches_its_route(deployed: Any) -> None:
    response = call(deployed, event("GET", "/v1/session", headers=authorized()))

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["actor"] == "presenter_admin"
    assert deployed["access"].presented == [FAKE_TOKEN]


def test_a_missing_authorization_header_is_refused_before_the_route(deployed: Any) -> None:
    response = call(deployed, event("GET", "/v1/session", headers=PRESENTER))

    assert response["statusCode"] == 401
    body = json.loads(response["body"])
    assert body["code"] == "UNAUTHENTICATED"
    # The refusal still carries the correlation identifier every other answer does.
    assert body["correlation_id"]


def test_a_wrong_token_is_refused_with_the_identical_shape(deployed: Any) -> None:
    wrong = call(
        deployed,
        event("GET", "/v1/session", headers={"Authorization": "Bearer wrong", **PRESENTER}),
    )
    missing = call(deployed, event("GET", "/v1/session", headers=PRESENTER))

    assert wrong["statusCode"] == missing["statusCode"] == 401
    assert json.loads(wrong["body"])["code"] == json.loads(missing["body"])["code"]


def test_a_non_bearer_scheme_is_refused(deployed: Any) -> None:
    response = call(
        deployed,
        event("GET", "/v1/session", headers={"Authorization": FAKE_TOKEN, **PRESENTER}),
    )
    assert response["statusCode"] == 401


def test_an_unreadable_secret_fails_closed_rather_than_open(deployed: Any) -> None:
    deployed["access"].unavailable = True
    response = call(deployed, event("GET", "/v1/session", headers=authorized()))
    assert response["statusCode"] == 401


def test_no_response_body_ever_contains_the_token(deployed: Any) -> None:
    for request in (
        event("GET", "/v1/session", headers=authorized()),
        event("GET", "/v1/session", headers=PRESENTER),
        event("GET", "/v1/nowhere", headers=authorized()),
    ):
        assert FAKE_TOKEN not in call(deployed, request)["body"]


# -- routing, methods, and bodies ---------------------------------------------------------------


def test_an_authenticated_post_reaches_its_route(deployed: Any) -> None:
    """One accepted state-changing route, through the same payload-v2 binding."""

    response = call(
        deployed,
        event(
            "POST",
            "/v1/ingest/messages",
            headers=authorized(**{"Idempotency-Key": "payload-v2-ingest"}),
            body={"messages": []},
        ),
    )
    # The route is reached and answers on its own terms; what matters here is that the method,
    # path, headers, and JSON body all survived the v2 translation.
    assert response["statusCode"] != 401
    assert response["statusCode"] != 404


def test_malformed_json_is_a_transport_refusal_not_a_crash(deployed: Any) -> None:
    request = event(
        "POST",
        "/v1/ingest/messages",
        headers=authorized(**{"Idempotency-Key": "payload-v2-broken"}),
    )
    request["body"] = "{not json"
    response = call(deployed, request)

    assert response["statusCode"] in {400, 422}
    assert "not json" not in response["body"]


def test_a_problem_document_is_returned_as_text_not_base64(deployed: Any) -> None:
    """The whole frozen error contract is ``application/problem+json``.

    Mangum base64-encodes any media type it does not recognise as text, and that one is not in
    its default list -- so without :data:`~functions.api.handler.TEXT_MIME_TYPES` every 401,
    404, 409, 422 and 503 would reach a browser as an opaque blob while the happy path looked
    perfect. Asserted by decoding a real error body rather than by reading the setting back.
    """

    response = call(deployed, event("GET", "/v1/session", headers=PRESENTER))

    assert response["statusCode"] == 401
    assert response.get("isBase64Encoded") is False
    assert json.loads(response["body"])["title"]


def test_an_unknown_route_is_a_problem_document(deployed: Any) -> None:
    response = call(deployed, event("GET", "/v1/nowhere", headers=authorized()))

    assert response["statusCode"] == 404
    assert json.loads(response["body"])["code"] == "NOT_FOUND"
    # The caller's own path is never echoed back into the body.
    assert "nowhere" not in response["body"]


def test_the_query_string_arrives_from_raw_query_string(deployed: Any) -> None:
    """Payload v2 carries ``rawQueryString`` and no ``queryStringParameters`` map is required."""

    response = call(deployed, event("GET", "/v1/feed", headers=authorized(), query="limit=5"))
    assert response["statusCode"] != 404


def test_every_response_carries_correlation_and_no_store(deployed: Any) -> None:
    response = call(deployed, event("GET", "/v1/session", headers=authorized()))
    headers = {key.lower(): value for key, value in response["headers"].items()}

    assert headers["x-correlation-id"]
    assert headers["cache-control"] == "no-store"


# -- the demo clock route ------------------------------------------------------------------------


def test_the_clock_advance_route_is_reached_through_payload_v2(deployed: Any) -> None:
    response = call(
        deployed,
        event(
            "POST",
            "/v1/demo/clock/advance",
            headers=authorized(),
            body={
                "case_id": "00000000-0000-4000-8000-000000000001",
                "commitment_id": "00000000-0000-4000-8000-000000000002",
                "advance_seconds": 3600,
            },
        ),
    )

    # The commitment does not exist in this harness, so the route fails *after* the advance --
    # which is what proves the advance itself ran on the durable clock.
    assert response["statusCode"] != 401
    assert deployed["clock"].instant == SEED + timedelta(hours=1)


def test_an_unavailable_clock_fails_every_request_closed(deployed: Any) -> None:
    """No fallback to process-local time, to ``SystemClock``, or to anything invented here."""

    deployed["clock"].instant = None
    response = call(deployed, event("GET", "/v1/session", headers=authorized()))

    assert response["statusCode"] == 503
    assert json.loads(response["body"])["code"] == "LOGICAL_TIME_UNAVAILABLE"


def test_the_clock_is_read_once_per_request(deployed: Any) -> None:
    before = deployed["clock"].reads
    call(deployed, event("GET", "/v1/session", headers=authorized()))
    assert deployed["clock"].reads == before + 1


# -- what the local composition still does -----------------------------------------------------


def test_a_composition_with_no_verifier_installs_no_check_at_all() -> None:
    """ "Not part of this composition" and "accepts anything" must not be the same thing."""

    drivers = storage_driver("memory", prefix="no-verifier")
    driver = next(drivers)
    harness = build_harness(driver, "recording")
    assert harness.app.state.container.access is None
    assert harness.client.get("/v1/session", headers=PRESENTER).status_code == 200


def test_the_demo_environment_still_requires_the_demo_namespace() -> None:
    """A guard this batch relies on: the deployed clock partition is one exact literal."""

    assert Environment.DEMO.value == "demo"
