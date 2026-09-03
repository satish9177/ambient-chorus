"""Adversarial probes against the real HTTP surface: what a refusal is allowed to say.

Every test here posts a body containing a sentinel that reads like the private content this
system exists to protect -- a health condition and an apartment number -- and then asserts the
sentinel is nowhere in the response. The sentinel is deliberately memorable rather than random:
a reviewer skimming a failure should be able to see at a glance that a real disclosure would
have looked exactly like this.

The framework's default validation handler serializes each rejected value under an ``input``
key, so before this suite existed a malformed ingest request answered with the message text it
had just refused. That is the defect these probes close, and they are written to keep failing
if it ever comes back through a different door: a new field, a different error type, a larger
body, a numeric edge case the JSON parser accepts and the model does not.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from chorus_api.problem_details import TRANSPORT_FIELD_NAMES
from tests.contract.api.conftest import ApiHarness

pytestmark = pytest.mark.anyio

SENTINEL = "MOTHER-HAS-A-HEART-CONDITION-APT-4B"
"""Text that must never appear in a response, a header, or an error body."""

IDENTIFIER_SENTINELS: tuple[str, ...] = (
    "PRIVATE_HEALTH_DETAIL",
    "motherLeelaAsthma4B",
    "SENTINEL_LEELA_ASTHMA_4B",
)
"""Sentinels shaped like *declared field names*, which is the whole point of them.

The earlier probes were all hyphenated, so a field-path filter that asked "does this segment
look like a Python identifier?" rejected them and the suite passed while the defect stood.
Every one of these would have satisfied that filter, and each is exactly the kind of thing a
caller would choose if they wanted an error response to repeat a private detail back at them:
a health condition, a named person with a condition and an apartment, a coined constant.

They are used as JSON object keys, as URL path segments, and at every nesting depth the
transport schema has, because a path segment is echoed at whatever depth the framework
reports it.
"""

COMMUNITY = "00000000-0000-4000-8000-000000000001"
DIGEST = "sha256:" + "0" * 64
MAX_PROBLEM_BYTES = 8_192
"""A bounded refusal. A response that grows with the request is a channel, not an error."""


def _message(**changes: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "channel_message_id": "feed-900",
        "sent_at": "2030-01-01T00:00:00Z",
        "text": SENTINEL,
    }
    message.update(changes)
    return message


def _body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"community_id": COMMUNITY, "messages": [_message()]}
    body.update(changes)
    return body


def _post(api: ApiHarness, content: str) -> Any:
    return api.client.post(
        "/v1/ingest/messages",
        content=content,
        headers=api.presenter_headers(
            **{"Idempotency-Key": "probe-key-000001", "Content-Type": "application/json"}
        ),
    )


def _assert_safe_refusal(response: Any) -> dict[str, Any]:
    """Every property a refusal must have, asserted in one place."""

    assert 400 <= response.status_code < 500, response.status_code
    assert SENTINEL not in response.text
    assert SENTINEL not in str(dict(response.headers))
    assert len(response.content) <= MAX_PROBLEM_BYTES
    problem: dict[str, Any] = response.json()
    assert str(problem["type"]).startswith("urn:chorus:error:")
    assert "correlation_id" in problem
    # No key anywhere in the body may carry a rejected value.
    rendered = json.dumps(problem)
    assert "input" not in problem
    assert "ctx" not in rendered
    return problem


def _numeric_probe(literal: str) -> str:
    """A body whose declared byte length is a JSON value Python parses and Pydantic will not.

    ``NaN`` and the infinities are accepted by the standard JSON parser and rejected by the
    model, which is exactly the seam where a framework default would have echoed the enclosing
    object -- sentinel and all -- back to the caller.
    """

    return (
        f'{{"community_id":"{COMMUNITY}","messages":[{{"channel_message_id":"{SENTINEL}",'
        f'"sent_at":"2030-01-01T00:00:00Z","text":"x","attachments":[{{"evidence_id":'
        f'"00000000-0000-4000-8000-000000000002","media_type":"image/png",'
        f'"byte_length":{literal},"sha256":"{DIGEST}"}}]}}]}}'
    )


PROBES: dict[str, str] = {
    "bad_contributor_uuid": json.dumps(
        _body(messages=[_message(contributor_id="not-a-uuid", text=SENTINEL)])
    ),
    "message_too_long": json.dumps(_body(messages=[_message(text=SENTINEL * 400)])),
    "invalid_nested_attachment": json.dumps(
        _body(
            messages=[
                _message(
                    attachments=[
                        {
                            "evidence_id": SENTINEL,
                            "media_type": "image/png",
                            "byte_length": 1,
                            "sha256": DIGEST,
                        }
                    ]
                )
            ]
        )
    ),
    "nan": _numeric_probe("NaN"),
    "infinity": _numeric_probe("Infinity"),
    "negative_infinity": _numeric_probe("-Infinity"),
    "unknown_field_named_like_private_text": json.dumps(_body(**{SENTINEL: "leak"})),
    "malformed_json": f'{{"community_id": "{COMMUNITY}", "messages": [ {{"text": "{SENTINEL}"',
    "giant_message_array": json.dumps(
        _body(messages=[_message(channel_message_id=f"feed-{index:04d}") for index in range(400)])
    ),
    "empty_body": "",
    "json_array_instead_of_object": json.dumps([{"text": SENTINEL}]),
}


def _application_log(caplog: pytest.LogCaptureFixture) -> str:
    """Everything CHORUS itself wrote while the request was handled.

    Scoped to the application's own loggers on purpose. The test client's HTTP library logs
    the request line it just sent, which contains whatever URL the *test* chose -- asserting
    against that would be asserting that httpx does not know what it was asked to fetch.
    What matters is that nothing on the CHORUS side copied caller text into a log record.
    """

    return chr(10).join(
        f"{record.name}:{record.getMessage()}:{record.__dict__}"
        for record in caplog.records
        if record.name.startswith(("chorus", "uvicorn", "starlette", "fastapi"))
    )


def _identifier_probes() -> dict[str, tuple[str, str]]:
    """Bodies whose *unknown field names* are identifier-shaped private-looking sentinels.

    One probe per (sentinel, depth) pair: top level, inside a message, and inside an
    attachment. Each returns the body to post and the sentinel it must not echo.
    """

    probes: dict[str, tuple[str, str]] = {}
    for sentinel in IDENTIFIER_SENTINELS:
        probes[f"top_level_{sentinel}"] = (json.dumps(_body(**{sentinel: "leak"})), sentinel)
        probes[f"nested_message_{sentinel}"] = (
            json.dumps(_body(messages=[_message(**{sentinel: "leak"})])),
            sentinel,
        )
        probes[f"nested_attachment_{sentinel}"] = (
            json.dumps(
                _body(
                    messages=[
                        _message(
                            attachments=[
                                {
                                    "evidence_id": "00000000-0000-4000-8000-000000000002",
                                    "media_type": "image/png",
                                    "byte_length": 1,
                                    "sha256": DIGEST,
                                    sentinel: "leak",
                                }
                            ]
                        )
                    ]
                ),
            ),
            sentinel,
        )
    return probes


IDENTIFIER_PROBES = _identifier_probes()


@pytest.mark.parametrize("probe", sorted(IDENTIFIER_PROBES), ids=sorted(IDENTIFIER_PROBES))
def test_an_identifier_shaped_unknown_field_is_never_echoed(
    api: ApiHarness, probe: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The regression: safety was decided from syntax, and the attacker chooses the syntax.

    An unexpected field's *key* is caller-supplied, and the old filter accepted any segment
    that looked like an identifier -- so a body carrying a field called
    ``PRIVATE_HEALTH_DETAIL`` had that name returned in the response, which is precisely the
    disclosure the validation handler exists to prevent.
    """

    body, sentinel = IDENTIFIER_PROBES[probe]
    with caplog.at_level(logging.DEBUG):
        response = _post(api, body)

    assert 400 <= response.status_code < 500
    assert sentinel not in response.text
    assert sentinel.lower() not in response.text.lower()
    assert sentinel not in str(dict(response.headers))
    assert sentinel not in _application_log(caplog)
    problem = response.json()
    assert problem["errors"], "an unexpected field is still reported"
    assert any(item["category"] == "UNEXPECTED_FIELD" for item in problem["errors"])
    assert any("?" in item["path"] for item in problem["errors"])


@pytest.mark.parametrize("sentinel", IDENTIFIER_SENTINELS)
def test_an_unknown_url_is_refused_without_repeating_the_url(
    api: ApiHarness, sentinel: str, caplog: pytest.LogCaptureFixture
) -> None:
    """``instance`` used to be the raw request path, so a 404 answered with the caller's words."""

    with caplog.at_level(logging.DEBUG):
        response = api.client.get(f"/v1/{sentinel}", headers=api.presenter_headers())

    assert response.status_code == 404
    assert sentinel not in response.text
    assert sentinel not in str(dict(response.headers))
    assert sentinel not in _application_log(caplog)
    problem = response.json()
    assert problem["code"] == "NOT_FOUND"
    assert "instance" not in problem, "no route matched, so there is no safe template to give"


@pytest.mark.parametrize("sentinel", IDENTIFIER_SENTINELS)
def test_an_invalid_operation_path_never_repeats_the_segment_it_refused(
    api: ApiHarness, sentinel: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A path *parameter* is caller text too, even when a route did match."""

    with caplog.at_level(logging.DEBUG):
        response = api.client.get(f"/v1/operations/{sentinel}", headers=api.presenter_headers())

    assert 400 <= response.status_code < 500
    assert sentinel not in response.text
    assert sentinel not in str(dict(response.headers))
    assert sentinel not in _application_log(caplog)
    problem = response.json()
    # The route template may be reported, because it is written in this repository. The value
    # the caller put into it may not be: the parameter is still spelled as its placeholder.
    instance = problem.get("instance", "")
    assert instance in {"", "/operations/{operation_id}", "/v1/operations/{operation_id}"}
    for item in problem.get("errors", []):
        assert sentinel not in item["path"]


@pytest.mark.parametrize("probe", sorted(PROBES), ids=sorted(PROBES))
def test_a_rejected_request_never_describes_what_it_rejected(api: ApiHarness, probe: str) -> None:
    response = _post(api, PROBES[probe])

    problem = _assert_safe_refusal(response)
    assert response.status_code < 500, "a malformed body is a client error, never a crash"
    assert problem["status"] == response.status_code


def test_a_validation_problem_names_only_safe_codes_paths_and_categories(
    api: ApiHarness,
) -> None:
    """What a caller *is* told: which field, which kind of rule, and nothing about the value."""

    response = _post(api, PROBES["bad_contributor_uuid"])

    problem = response.json()
    assert problem["code"] == "VALIDATION_ERROR"
    assert problem["errors"], "a validation problem should still be actionable"
    for item in problem["errors"]:
        assert set(item) == {"code", "path", "category"}
        assert item["code"].replace("_", "").isalnum()
        # Every segment is a name this application declared, a bounded index, or a redaction.
        # "Looks like an identifier" is deliberately not one of the accepted answers.
        assert all(
            segment in TRANSPORT_FIELD_NAMES or segment.isdigit() or segment in {"?", "..."}
            for segment in item["path"].split(".")
        )
        assert item["category"] in {
            "MISSING",
            "UNEXPECTED_FIELD",
            "WRONG_TYPE",
            "OUT_OF_RANGE",
            "MALFORMED_SYNTAX",
            "NOT_ALLOWED_VALUE",
            "INVALID_VALUE",
        }


def test_an_unexpected_field_is_reported_without_naming_it(api: ApiHarness) -> None:
    """The offending *key* is caller-supplied too, so it is a path segment nobody echoes."""

    response = _post(api, PROBES["unknown_field_named_like_private_text"])

    problem = _assert_safe_refusal(response)
    paths = {item["path"] for item in problem["errors"]}
    assert paths, "an unexpected field is still reported"
    assert not any(SENTINEL.lower() in path.lower() for path in paths)


def test_a_huge_rejected_body_still_produces_a_bounded_response(api: ApiHarness) -> None:
    response = _post(api, PROBES["giant_message_array"])

    problem = _assert_safe_refusal(response)
    assert len(problem["errors"]) <= 20
    assert len(response.content) < 4_096


def test_a_missing_actor_header_is_a_problem_document_not_a_framework_default(
    api: ApiHarness,
) -> None:
    response = api.client.post(
        "/v1/ingest/messages",
        json=_body(),
        headers={"Idempotency-Key": "probe-key-000001"},
    )

    assert response.status_code == 401
    problem = response.json()
    assert problem["code"] == "UNAUTHENTICATED"
    assert problem["type"] == "urn:chorus:error:unauthenticated"
    assert "detail" not in {key for key in problem if key not in problem}
    assert response.headers["content-type"].startswith("application/problem+json")


def test_an_unknown_actor_is_refused_without_echoing_what_was_sent(api: ApiHarness) -> None:
    response = api.client.get(
        "/v1/feed",
        params={"community_id": COMMUNITY},
        headers={"X-Chorus-Demo-Actor": SENTINEL},
    )

    assert response.status_code == 403
    assert SENTINEL not in response.text
    assert response.json()["code"] == "FORBIDDEN"
