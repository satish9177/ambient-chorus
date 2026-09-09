"""The two things only the sender may know, resolved from its one Secrets Manager secret.

``CHORUS_DESTINATION_REGISTRY_SECRET_ARN`` names the secret whose contents are the verified
destination address and the sending identity's from/reply-to addresses and ARN (deployment
contract § 13). The sender role holds ``secretsmanager:GetSecretValue`` on that ARN and on
nothing else, and every other principal in the system is denied it by name -- because an address
is the one piece of the outbound path that must not exist outside the process that sends.

This adapter is an allowlist, not a lookup
--------------------------------------------
:meth:`resolve_destination` answers only for the exact
``(destination_id, registry_version, routing_token)`` triple the secret holds. All three are
inside ``preview_hash``, so a registry that resolved a *near* match would silently route a
message a human approved for one recipient to another. A mismatch on any of the three refuses.

The recipient therefore **cannot come from an invocation payload**. The send command carries no
recipient field at all, and this is the only place an address exists -- so "an untrusted caller
cannot choose who receives an approved message" is a property of the object graph.

What never happens here
------------------------
No address, no secret, and no SDK response text enters a log line, an exception message, or a
return value beyond the frozen ``ResolvedDestination``/``SendingIdentity`` values the send path
requires. Every failure is one opaque refusal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError

from chorus.domain.entities import DestinationKind
from chorus.domain.ids import DestinationId
from chorus.ports.errors import ExternalDependencyError, PersistenceErrorCode
from chorus.ports.sender import ResolvedDestination, SendingIdentity

DESTINATION_REGISTRY_SCHEMA: Final = "destination-registry/v1"
"""The secret's declared shape, checked before a field is read."""


class DestinationRegistryUnavailableError(ExternalDependencyError):
    """The registry could not be read or could not be understood. Never a resolution.

    ``retryable=False``: a send is mid-flight with a claimed execution, and a generic retry
    would re-enter the send order rather than record the definite pre-SES failure the send
    command already knows how to write.
    """

    def __init__(self) -> None:
        super().__init__(
            "DESTINATION_REGISTRY",
            code=PersistenceErrorCode.DEPENDENCY_REJECTED,
            retryable=False,
        )


@dataclass(slots=True)
class SecretsManagerDestinationRegistry:
    """Resolve the destination and the sending identity from one configured secret."""

    client: Any
    secret_id: str
    _entry: tuple[ResolvedDestination, SendingIdentity] | None = field(
        default=None, init=False, repr=False
    )

    async def resolve_destination(
        self, *, destination_id: DestinationId, registry_version: int, routing_token: UUID
    ) -> ResolvedDestination:
        destination, _ = await self._load()
        if (
            destination.destination_id != destination_id
            or destination.registry_version != registry_version
            or destination.routing_token != routing_token
        ):
            # All three, and no partial match. The triple is what the approval bound.
            raise DestinationRegistryUnavailableError()
        return destination

    async def resolve_sending_identity(self, identity_id: str) -> SendingIdentity:
        _, identity = await self._load()
        if identity.identity_id != identity_id:
            raise DestinationRegistryUnavailableError()
        return identity

    async def _load(self) -> tuple[ResolvedDestination, SendingIdentity]:
        """Read and parse the secret once per execution environment, on first use.

        Only a *successful* parse is cached, so a transient failure is retried on the next send
        rather than becoming a permanently unusable sender.
        """

        if self._entry is not None:
            return self._entry
        try:
            response = self.client.get_secret_value(SecretId=self.secret_id)
        except (ClientError, BotoCoreError):
            raise DestinationRegistryUnavailableError() from None
        payload = response.get("SecretString") if isinstance(response, dict) else None
        if not isinstance(payload, str):
            raise DestinationRegistryUnavailableError()
        entry = parse_registry_secret(payload)
        self._entry = entry
        return entry


def parse_registry_secret(payload: str) -> tuple[ResolvedDestination, SendingIdentity]:
    """Parse the registry secret strictly, or refuse it.

    Strict in every direction, and the value objects' own invariants do the rest: an address
    that is not an address, a non-positive registry version, and an over-long label are all
    refused by :class:`~chorus.ports.sender.ResolvedDestination` before this returns.
    """

    try:
        body = json.loads(payload)
    except ValueError as error:
        raise DestinationRegistryUnavailableError() from error
    if not isinstance(body, dict) or body.get("schema") != DESTINATION_REGISTRY_SCHEMA:
        raise DestinationRegistryUnavailableError()
    try:
        destination = ResolvedDestination(
            destination_id=DestinationId(_text(body, "destination_id")),
            kind=DestinationKind(_text(body, "kind")),
            registry_version=_number(body, "registry_version"),
            routing_token=UUID(_text(body, "routing_token")),
            display_label=_text(body, "display_label"),
            address=_text(body, "address"),
        )
        identity = SendingIdentity(
            identity_id=_text(body, "identity_id"),
            from_address=_text(body, "from_address"),
            reply_to_address=_text(body, "reply_to_address"),
            identity_arn=_text(body, "identity_arn"),
        )
    except (TypeError, ValueError) as error:
        raise DestinationRegistryUnavailableError() from error
    return destination, identity


def _text(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise DestinationRegistryUnavailableError()
    return value


def _number(body: dict[str, Any], name: str) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DestinationRegistryUnavailableError()
    return value


__all__ = [
    "DESTINATION_REGISTRY_SCHEMA",
    "DestinationRegistryUnavailableError",
    "SecretsManagerDestinationRegistry",
    "parse_registry_secret",
]
