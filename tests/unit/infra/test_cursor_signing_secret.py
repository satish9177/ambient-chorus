"""P2-5, Phase 11 batch 4 repair: the pagination cursor key is one stable secret, not a
per-cold-start random value.

Before this repair, every deployed API execution environment called ``secrets.token_bytes(32)``
at cold start and handed the result straight to
:class:`~chorus.infrastructure.dynamodb.cursor.SignedCursorCodec`. Two concurrent containers, or
one container recycled between a browser's page-one and page-two requests, would then disagree
on the key: a cursor issued by one is a cursor **rejected** by the other, even though nothing
about the request was wrong. Three properties prove the fix:

* a key loaded from the same secret validates a cursor issued under that same load, no matter
  how many separate :class:`SignedCursorCodec` instances are built from it (cross-container
  cursor validation);
* a key loaded from a *different* secret rejects it (wrong-secret rejection stays intact -- the
  fix is a stable key, not a weaker one);
* an unreadable or malformed secret fails composition closed, with no fallback to a key
  generated fresh for the occasion.

Every Secrets Manager client here is an explicit stub. Nothing resolves a credential and
nothing reaches AWS.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from chorus.domain.ids import Namespace
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.secrets.cursor_signing import (
    CURSOR_SIGNING_SECRET_SCHEMA,
    CursorSigningKeyUnavailableError,
    load_cursor_signing_key,
    parse_cursor_signing_secret,
)
from chorus.ports.pagination import QueryBinding

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:000000000000:secret:chorus-cursor-signing-AbCdEf"
FIRST_KEY = b"\x01" * 32
SECOND_KEY = b"\x02" * 32


class FakeSecrets:
    """Answers with a scripted secret string, or raises whatever the test scripted."""

    def __init__(self, payload: str | None = None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0

    def get_secret_value(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"SecretString": self.payload}


def secret_body(key: bytes = FIRST_KEY) -> str:
    return json.dumps(
        {
            "schema": CURSOR_SIGNING_SECRET_SCHEMA,
            "key_base64": base64.b64encode(key).decode("ascii"),
        }
    )


def load(client: FakeSecrets) -> bytes:
    return load_cursor_signing_key(client=client, secret_id=SECRET_ARN)


# -- cross-container validation and wrong-secret rejection ------------------------------------


def test_two_codecs_built_from_the_same_loaded_key_accept_each_others_cursors() -> None:
    """Simulates two separate execution environments, each cold-starting and independently
    calling Secrets Manager -- as two real containers would."""

    first_container = SignedCursorCodec(secret=load(FakeSecrets(secret_body())))
    second_container = SignedCursorCodec(secret=load(FakeSecrets(secret_body())))

    cursor = first_container.issue(
        namespace=Namespace("DEMO"),
        binding=QueryBinding.AUDIT_CASE_EVENTS,
        partition_key="p",
        sort_key="k",
    )
    assert (
        second_container.verify(
            cursor,
            namespace=Namespace("DEMO"),
            binding=QueryBinding.AUDIT_CASE_EVENTS,
            partition_key="p",
        )
        == "k"
    )


def test_a_codec_built_from_a_different_secret_still_rejects_the_cursor() -> None:
    """The fix is a stable key, not a weaker check: a genuinely foreign key must still fail."""

    from chorus.ports.errors import InvalidCursorError

    issuer = SignedCursorCodec(secret=load(FakeSecrets(secret_body(FIRST_KEY))))
    other = SignedCursorCodec(secret=load(FakeSecrets(secret_body(SECOND_KEY))))

    cursor = issuer.issue(
        namespace=Namespace("DEMO"),
        binding=QueryBinding.AUDIT_CASE_EVENTS,
        partition_key="p",
        sort_key="k",
    )
    with pytest.raises(InvalidCursorError):
        other.verify(
            cursor,
            namespace=Namespace("DEMO"),
            binding=QueryBinding.AUDIT_CASE_EVENTS,
            partition_key="p",
        )


# -- every failure is a refusal, none of them silently generate a key ------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not json", id="not-json"),
        pytest.param('["a", "list"]', id="not-an-object"),
        pytest.param('{"schema": "cursor-signing-key/v99", "key_base64": "x"}', id="wrong-schema"),
        pytest.param(f'{{"schema": "{CURSOR_SIGNING_SECRET_SCHEMA}"}}', id="no-key"),
        pytest.param(
            f'{{"schema": "{CURSOR_SIGNING_SECRET_SCHEMA}", "key_base64": "not-base64!!"}}',
            id="not-base64",
        ),
        pytest.param(
            (
                f'{{"schema": "{CURSOR_SIGNING_SECRET_SCHEMA}", '
                f'"key_base64": "{base64.b64encode(b"short").decode()}"}}'
            ),
            id="too-short",
        ),
    ],
)
def test_a_malformed_secret_fails_composition_closed(payload: str) -> None:
    with pytest.raises(CursorSigningKeyUnavailableError):
        load(FakeSecrets(payload))


def test_an_sdk_error_fails_composition_closed() -> None:
    """Unreadable is a refusal, never a signal to generate a key on the spot."""

    error = ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetSecretValue")
    with pytest.raises(CursorSigningKeyUnavailableError):
        load(FakeSecrets(error=error))


def test_a_secret_with_no_string_value_fails_composition_closed() -> None:
    with pytest.raises(CursorSigningKeyUnavailableError):
        load(FakeSecrets(payload=None))


def test_no_failure_message_contains_the_key_material() -> None:
    error = ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetSecretValue")
    with pytest.raises(CursorSigningKeyUnavailableError) as raised:
        load(FakeSecrets(error=error))
    rendered = f"{raised.value!r} {raised.value}"
    assert base64.b64encode(FIRST_KEY).decode("ascii") not in rendered
    assert "AccessDenied" not in rendered


# -- the parser in isolation -------------------------------------------------------------------


def test_the_parser_returns_exactly_the_decoded_key_bytes() -> None:
    assert parse_cursor_signing_secret(secret_body(FIRST_KEY)) == FIRST_KEY
