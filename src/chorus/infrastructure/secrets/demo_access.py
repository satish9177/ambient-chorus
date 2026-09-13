"""The deployed API's demo bearer token, resolved from one Secrets Manager secret.

The API role holds ``secretsmanager:GetSecretValue`` on **one** ARN -- the demo access secret --
and an explicit deny on the sender's destination registry (deployment contract SS 8.1). This
adapter is the whole of what that grant is for.

The secret holds a **digest, not the token**
---------------------------------------------
``CHORUS_DEMO_ACCESS_SECRET_ARN`` names a secret whose contents are the demo access token's
*hash* (deployment contract SS 13). So the deployed system never stores the credential it
accepts: a presented token is hashed and the digests are compared. The token itself exists only
in the presenter's browser and in the request that carries it.

The comparison is :func:`hmac.compare_digest` over the two hex digests. Constant-time comparison
of a digest rather than of the token is the stronger arrangement anyway -- both operands are
fixed-length and neither is secret-length-revealing.

What never happens here
------------------------
The token, the secret, the digest, and the SDK's response never enter a log line, an exception
message, a return value, or a response body. Every failure raises the one opaque
:class:`~chorus.ports.access.AccessTokenUnavailableError`, and every mismatch returns ``False``.
An unreadable secret is a **refusal**, never an open door.

The cache, and its exact bound
-------------------------------
A successfully parsed digest is held in the adapter instance for the life of the execution
environment, because a Secrets Manager read on every request costs a presenter latency on every
click and buys nothing -- the value cannot change without a redeploy of the demo. Only a
*successful* read is cached: a failure is retried next time, so a transient outage does not
become a permanently broken API. It is an instance attribute rather than a module global, so a
test replaces the provider by constructing another one and nothing survives between tests.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Final

from botocore.exceptions import BotoCoreError, ClientError

from chorus.ports.access import AccessTokenUnavailableError

SECRETS_SERVICE_NAME: Final = "secretsmanager"

DEMO_ACCESS_SECRET_SCHEMA: Final = "demo-access-token/v1"  # noqa: S105 - a schema token
"""The secret's own declared shape, checked before any field is read.

A versioned secret payload rather than a bare string, so the day a second field is needed the
old shape is refused instead of half-read -- and so a secret rotated to some other purpose
cannot be silently accepted as this one.
"""

DIGEST_PREFIX: Final = "sha256:"
DIGEST_HEX_LENGTH: Final = 64

SINGLE_ATTEMPT_RETRIES: dict[str, Any] = {"mode": "standard", "total_max_attempts": 3}
"""Three attempts, and here that is deliberate rather than copied.

Everywhere a *deliberate external effect* is made -- the SES send, the compiler fence -- the
client is pinned to one attempt, because a retry underneath the caller is an effect nothing
records. A secret read has no effect at all: it is idempotent, it changes nothing, and a
throttle that failed the whole request would take the API down for a reason that resolves
itself.
"""


def token_digest(token: str) -> str:
    """The canonical ``sha256:<hex>`` digest of a presented token."""

    return f"{DIGEST_PREFIX}{sha256(token.encode('utf-8')).hexdigest()}"


def parse_secret(payload: str) -> str:
    """Read the stored digest out of the secret's JSON body, or refuse the secret.

    Strict in every direction: not JSON, not an object, a wrong schema token, a missing field,
    a non-string field, or a digest that is not exactly ``sha256:`` plus sixty-four lowercase
    hex characters all raise. There is no lenient path, because the lenient path is the one
    where a truncated or partially-rotated secret is accepted as a credential check.
    """

    try:
        body = json.loads(payload)
    except ValueError as error:
        raise AccessTokenUnavailableError("the demo access secret is not JSON") from error
    if not isinstance(body, dict):
        raise AccessTokenUnavailableError("the demo access secret is not an object")
    if body.get("schema") != DEMO_ACCESS_SECRET_SCHEMA:
        raise AccessTokenUnavailableError("the demo access secret names an unknown schema")
    digest = body.get("token_sha256")
    if not isinstance(digest, str) or not digest.startswith(DIGEST_PREFIX):
        raise AccessTokenUnavailableError("the demo access secret carries no digest")
    hexadecimal = digest[len(DIGEST_PREFIX) :]
    if len(hexadecimal) != DIGEST_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in hexadecimal
    ):
        raise AccessTokenUnavailableError("the demo access secret digest is malformed")
    return digest


@dataclass(slots=True)
class SecretsManagerDemoAccess:
    """Verify a presented bearer token against the one configured demo access secret."""

    client: Any
    secret_id: str
    _digest: str | None = field(default=None, init=False, repr=False)

    async def verify(self, presented: str) -> bool:
        """``True`` only for the exact token whose digest the secret holds.

        An empty presented token is refused before the secret is read, so a request with an
        ``Authorization: Bearer`` and nothing after it cannot cost a Secrets Manager call.
        """

        if not presented:
            return False
        return hmac.compare_digest(token_digest(presented), await self._load())

    async def _load(self) -> str:
        if self._digest is not None:
            return self._digest
        try:
            response = self.client.get_secret_value(SecretId=self.secret_id)
        except (ClientError, BotoCoreError):
            # Nothing from the SDK crosses this boundary -- not the message, not the error code,
            # not the secret identifier's contents. The API's answer is the same for every
            # cause, because the caller must not be able to probe which one it was.
            raise AccessTokenUnavailableError("the demo access secret is unreadable") from None
        payload = response.get("SecretString") if isinstance(response, dict) else None
        if not isinstance(payload, str):
            raise AccessTokenUnavailableError("the demo access secret has no string value")
        digest = parse_secret(payload)
        self._digest = digest
        return digest


def create_secrets_client(*, region_name: str, endpoint_url: str | None = None) -> Any:
    """The Secrets Manager client, constructed only where one is really needed."""

    import boto3
    from botocore.config import Config

    return boto3.client(
        SECRETS_SERVICE_NAME,
        region_name=region_name,
        endpoint_url=endpoint_url,
        config=Config(retries=SINGLE_ATTEMPT_RETRIES),
    )


__all__ = [
    "DEMO_ACCESS_SECRET_SCHEMA",
    "DIGEST_HEX_LENGTH",
    "DIGEST_PREFIX",
    "SECRETS_SERVICE_NAME",
    "SINGLE_ATTEMPT_RETRIES",
    "SecretsManagerDemoAccess",
    "create_secrets_client",
    "parse_secret",
    "token_digest",
]
