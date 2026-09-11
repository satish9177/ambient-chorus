"""The pagination cursor HMAC key, resolved from one Secrets Manager secret.

``CHORUS_CURSOR_SIGNING_SECRET_ARN`` names the secret whose contents are the key
:class:`~chorus.infrastructure.dynamodb.cursor.SignedCursorCodec` signs and verifies every
pagination cursor with. Before this module existed, the deployed API generated that key with
``secrets.token_bytes(32)`` at every cold start -- a fresh, unshared key per execution
environment. A cursor a browser is holding when its serving container recycles, or one issued
by a different concurrent container answering a previous page of the same list, would then
fail to verify on whichever container answers the next request: a real page-two request,
rejected as tampered, for no reason the caller did anything wrong (Phase 11 batch 4 repair,
P2-5).

Its own secret, not a field of the demo access secret
--------------------------------------------------------
The two secrets have unrelated blast radii -- a leaked cursor key lets a caller forge a page
token over rows it can already see the first page of; a leaked access digest lets a caller pass
the bearer check entirely -- and unrelated rotation schedules. Folding one into the other would
make rotating either one also touch the other for no reason born of the data.

Resolved eagerly, not on first pagination request
----------------------------------------------------
Every other secret in this package (:mod:`chorus.infrastructure.secrets.demo_access`,
:mod:`chorus.infrastructure.secrets.destination_registry`) is read lazily, cached only on
success, because a request that never needs the secret should never pay for it. A cursor key is
different: :class:`~chorus.infrastructure.dynamodb.cursor.SignedCursorCodec` takes its secret as
a plain, synchronous constructor argument and is then held, and shared by every repository built
against it, for the container's entire life -- there is no later moment to retry a failed read
into. So :func:`load_cursor_signing_key` is called once, synchronously, while the deployed
request path is composed (:func:`functions.api.composition.build_api_container`), and an
unreadable or malformed secret fails that composition closed: the execution environment never
starts serving requests with a key nothing durable agrees on, and (because the composition
result is cached only on success) the next invocation on the same warm container simply retries
it.

What never happens here
------------------------
The key, the secret, and the SDK's response never enter a log line, an exception message, or a
return value. Every failure raises the one opaque :class:`CursorSigningKeyUnavailableError`.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Final

from botocore.exceptions import BotoCoreError, ClientError

CURSOR_SIGNING_SECRET_SCHEMA: Final = "cursor-signing-key/v1"  # noqa: S105 - a schema token
"""The secret's own declared shape, checked before its key is read.

A versioned secret payload rather than a bare string, so a secret rotated to some other purpose
cannot be silently accepted as a cursor-signing key, and so the day the shape needs to change
the old one is refused instead of half-read.
"""

MIN_KEY_BYTES: Final = 32
"""The same floor :class:`~chorus.infrastructure.dynamodb.cursor.SignedCursorCodec` enforces on
its own ``secret`` field -- checked here too, so a too-short key is refused at composition time
with a diagnosis that names the secret, rather than surfacing later as the codec's own generic
``ValueError``.
"""


class CursorSigningKeyUnavailableError(RuntimeError):
    """The cursor-signing key could not be read from Secrets Manager or could not be understood.

    Raised while the request path is composed, before any route can issue or verify a cursor --
    an unreadable secret, an unparseable body, a wrong schema token, or a key shorter than
    :data:`MIN_KEY_BYTES` are all this one refusal, with no fallback to a key generated fresh
    for the occasion.
    """


def parse_cursor_signing_secret(payload: str) -> bytes:
    """Read the signing key out of the secret's JSON body, or refuse the secret.

    Strict in every direction: not JSON, not an object, a wrong schema token, a missing or
    non-string ``key_base64`` field, a value that is not valid base64, or a decoded key shorter
    than :data:`MIN_KEY_BYTES` all raise. There is no lenient path, because the lenient path is
    the one where a truncated or partially-rotated secret is accepted as a signing key.
    """

    try:
        body = json.loads(payload)
    except ValueError as error:
        raise CursorSigningKeyUnavailableError("the cursor signing secret is not JSON") from error
    if not isinstance(body, dict):
        raise CursorSigningKeyUnavailableError("the cursor signing secret is not an object")
    if body.get("schema") != CURSOR_SIGNING_SECRET_SCHEMA:
        raise CursorSigningKeyUnavailableError("the cursor signing secret names an unknown schema")
    encoded = body.get("key_base64")
    if not isinstance(encoded, str) or not encoded:
        raise CursorSigningKeyUnavailableError("the cursor signing secret carries no key")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise CursorSigningKeyUnavailableError(
            "the cursor signing secret key is not valid base64"
        ) from error
    if len(key) < MIN_KEY_BYTES:
        raise CursorSigningKeyUnavailableError("the cursor signing secret key is too short")
    return key


def load_cursor_signing_key(*, client: Any, secret_id: str) -> bytes:
    """Fetch and parse the cursor-signing key -- one blocking call, made exactly once.

    ``client`` is a plain (synchronous) Secrets Manager client, called directly rather than
    through an async port: the caller,
    :func:`functions.api.composition.build_api_container`, is itself synchronous, and this
    function is invoked exactly once per execution environment on its way to a
    :class:`~chorus.infrastructure.dynamodb.cursor.SignedCursorCodec` that outlives it.
    """

    try:
        response = client.get_secret_value(SecretId=secret_id)
    except (ClientError, BotoCoreError) as error:
        raise CursorSigningKeyUnavailableError("the cursor signing secret is unreadable") from error
    payload = response.get("SecretString") if isinstance(response, dict) else None
    if not isinstance(payload, str):
        raise CursorSigningKeyUnavailableError("the cursor signing secret has no string value")
    return parse_cursor_signing_secret(payload)


__all__ = [
    "CURSOR_SIGNING_SECRET_SCHEMA",
    "MIN_KEY_BYTES",
    "CursorSigningKeyUnavailableError",
    "load_cursor_signing_key",
    "parse_cursor_signing_secret",
]
