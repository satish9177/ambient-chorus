"""The deployed demo access boundary: the digest is compared, and nothing else escapes.

Four properties, and each one is the difference between a gate and a decoration:

* the right token is accepted and every other input is not;
* an unreadable, malformed, or missing secret is a **refusal** rather than an open door;
* neither the token nor the stored digest reaches a return value, an exception message, or a
  log line;
* the repository contains no token value -- the secret holds a *hash*, and the deployed system
  never stores the credential it accepts.

Every token here is an obvious fake and the Secrets Manager client is an explicit stub. Nothing
resolves a credential and nothing reaches AWS.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from botocore.exceptions import ClientError

from chorus.infrastructure.secrets.demo_access import (
    DEMO_ACCESS_SECRET_SCHEMA,
    SecretsManagerDemoAccess,
    parse_secret,
    token_digest,
)
from chorus.ports.access import AccessTokenUnavailableError

FAKE_TOKEN = "fake-demo-token-not-a-credential"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:000000000000:secret:chorus-demo-access-AbCdEf"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeSecrets:
    """Answers with a scripted secret string, or raises whatever the test scripted."""

    def __init__(self, payload: str | None = None, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def get_secret_value(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"SecretString": self.payload}


def secret_body(token: str = FAKE_TOKEN) -> str:
    return json.dumps({"schema": DEMO_ACCESS_SECRET_SCHEMA, "token_sha256": token_digest(token)})


def verifier(client: FakeSecrets) -> SecretsManagerDemoAccess:
    return SecretsManagerDemoAccess(client=client, secret_id=SECRET_ARN)


# -- the one accepting case ------------------------------------------------------------------


async def test_the_configured_token_authenticates() -> None:
    assert await verifier(FakeSecrets(secret_body())).verify(FAKE_TOKEN) is True


async def test_a_different_token_is_refused() -> None:
    assert await verifier(FakeSecrets(secret_body())).verify("some-other-token") is False


async def test_an_empty_token_is_refused_without_reading_the_secret() -> None:
    """A bare ``Authorization: Bearer`` must not cost a Secrets Manager call."""

    client = FakeSecrets(secret_body())
    assert await verifier(client).verify("") is False
    assert client.calls == 0


# -- every failure is a refusal --------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not json", id="not-json"),
        pytest.param('["a", "list"]', id="not-an-object"),
        pytest.param('{"schema": "demo-access-token/v99", "token_sha256": "x"}', id="wrong-schema"),
        pytest.param(f'{{"schema": "{DEMO_ACCESS_SECRET_SCHEMA}"}}', id="no-digest"),
        pytest.param(
            f'{{"schema": "{DEMO_ACCESS_SECRET_SCHEMA}", "token_sha256": "sha256:tooshort"}}',
            id="short-digest",
        ),
        pytest.param(
            f'{{"schema": "{DEMO_ACCESS_SECRET_SCHEMA}", "token_sha256": "sha256:{"Z" * 64}"}}',
            id="non-hex-digest",
        ),
    ],
)
async def test_a_malformed_secret_fails_closed(payload: str) -> None:
    with pytest.raises(AccessTokenUnavailableError):
        await verifier(FakeSecrets(payload)).verify(FAKE_TOKEN)


async def test_an_sdk_error_fails_closed() -> None:
    """An unreadable secret is a refusal. Treating it as "no token required" opens the API."""

    error = ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetSecretValue")
    with pytest.raises(AccessTokenUnavailableError):
        await verifier(FakeSecrets(error=error)).verify(FAKE_TOKEN)


async def test_a_secret_with_no_string_value_fails_closed() -> None:
    with pytest.raises(AccessTokenUnavailableError):
        await verifier(FakeSecrets(payload=None)).verify(FAKE_TOKEN)


async def test_a_failed_read_is_not_cached() -> None:
    """A transient outage must not become a permanently unusable API."""

    error = ClientError({"Error": {"Code": "ThrottlingException"}}, "GetSecretValue")
    client = FakeSecrets(error=error)
    subject = verifier(client)
    for _ in range(2):
        with pytest.raises(AccessTokenUnavailableError):
            await subject.verify(FAKE_TOKEN)
    assert client.calls == 2


async def test_a_successful_read_is_cached_per_instance() -> None:
    client = FakeSecrets(secret_body())
    subject = verifier(client)
    assert await subject.verify(FAKE_TOKEN) is True
    assert await subject.verify(FAKE_TOKEN) is True
    assert client.calls == 1
    # The cache is an instance attribute, so a fresh provider shares nothing with this one.
    assert verifier(FakeSecrets(secret_body()))._digest is None


# -- nothing escapes -------------------------------------------------------------------------


async def test_no_failure_message_contains_the_token_or_the_digest() -> None:
    error = ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetSecretValue")
    with pytest.raises(AccessTokenUnavailableError) as raised:
        await verifier(FakeSecrets(error=error)).verify(FAKE_TOKEN)

    rendered = f"{raised.value!r} {raised.value}"
    assert FAKE_TOKEN not in rendered
    assert token_digest(FAKE_TOKEN) not in rendered
    assert "AccessDenied" not in rendered


async def test_verification_logs_nothing_at_all(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        await verifier(FakeSecrets(secret_body())).verify(FAKE_TOKEN)
    assert caplog.records == []


async def test_the_verifier_returns_only_a_boolean() -> None:
    """A verifier that could hand its caller the credential back is one bug from leaking it."""

    assert await verifier(FakeSecrets(secret_body())).verify(FAKE_TOKEN) is True
    assert set(dir(SecretsManagerDemoAccess)) & {"digest", "token", "secret"} == set()


def test_the_secret_value_is_a_digest_and_the_parser_returns_only_that() -> None:
    parsed = parse_secret(secret_body())
    assert parsed == token_digest(FAKE_TOKEN)
    assert FAKE_TOKEN not in parsed
