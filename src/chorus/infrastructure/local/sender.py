"""Local senders and the destination registry, for ``test`` and ``development``.

In those environments the sender writes to a filesystem outbox and makes **no network call**,
which the environment contract already requires. The one-attempt rule, the claim compare-and-
swap, the classification table, and the fence all apply identically there, so every ambiguous
path is exercised without SES ever being reachable.

The scripted sender is the interesting one. Its whole purpose is to produce outcomes no live
service would cooperate in producing on demand -- an ambiguous transport failure, a definite
rejection, an acceptance whose response is then lost -- and to count how many deliberate calls
were made, which is the number the safety property is actually about.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from chorus.domain.entities import DestinationKind
from chorus.domain.ids import DestinationId
from chorus.ports.sender import (
    DestinationRegistryError,
    ResolvedDestination,
    SendingIdentity,
    SesAccepted,
    SesEmailRequest,
    SesOutcome,
)

REGISTRY_DENIAL_DESTINATION = "DESTINATION_REGISTRY_CHANGED"
REGISTRY_DENIAL_ROUTING = "ROUTING_TOKEN_CHANGED"
REGISTRY_DENIAL_IDENTITY = "SENDER_IDENTITY_CHANGED"

DEMO_ROUTING_TOKEN = UUID("00000000-0000-0000-0000-000000000000")
"""The safe placeholder token the checked-in configuration already uses."""


@dataclass(slots=True)
class InMemoryDestinationRegistry:
    """One allowlisted destination and one sending identity, and no way to reach a second.

    Deliberately holds exactly one of each. The registry's job is to *deny* anything that is
    not the entry the compiler authorized, and a structure that could hold many would make the
    denial a lookup miss rather than a refusal to resolve a near match.
    """

    destination: ResolvedDestination
    identity: SendingIdentity

    async def resolve_destination(
        self, *, destination_id: DestinationId, registry_version: int, routing_token: UUID
    ) -> ResolvedDestination:
        """Resolve only the exact triple. Any one of the three moving denies."""

        current = self.destination
        if current.destination_id != destination_id or current.registry_version != registry_version:
            raise DestinationRegistryError(REGISTRY_DENIAL_DESTINATION)
        if current.routing_token != routing_token:
            raise DestinationRegistryError(REGISTRY_DENIAL_ROUTING)
        return current

    async def resolve_sending_identity(self, identity_id: str) -> SendingIdentity:
        if self.identity.identity_id != identity_id:
            raise DestinationRegistryError(REGISTRY_DENIAL_IDENTITY)
        return self.identity


def demo_registry(
    *,
    destination_id: str = "property_manager:demo",
    registry_version: int = 1,
    routing_token: UUID = DEMO_ROUTING_TOKEN,
    display_label: str = "Property Management",
    address: str = "property-manager@chorus.invalid",
    identity_id: str = "chorus-demo-sender",
    from_address: str = "chorus@chorus.invalid",
    reply_to_address: str = "chorus-replies@chorus.invalid",
) -> InMemoryDestinationRegistry:
    """The local registry, addressed in the reserved ``.invalid`` TLD.

    ``.invalid`` is reserved by RFC 2606 and can never resolve, so a local run that somehow
    reached a real transport would fail rather than deliver. A plausible-looking placeholder
    domain would be a real mailbox somebody else owns.
    """

    return InMemoryDestinationRegistry(
        destination=ResolvedDestination(
            destination_id=DestinationId(destination_id),
            kind=DestinationKind.PROPERTY_MANAGER,
            registry_version=registry_version,
            routing_token=routing_token,
            display_label=display_label,
            address=address,
        ),
        identity=SendingIdentity(
            identity_id=identity_id,
            from_address=from_address,
            reply_to_address=reply_to_address,
            identity_arn=f"arn:aws:ses:us-east-1:000000000000:identity/{identity_id}",
        ),
    )


@dataclass(slots=True)
class FilesystemOutboxSender:
    """Write the exact payload to a file and report acceptance. No network call, ever.

    The file is what a developer reads to see the message that "went out", and it is written
    **only** here: neither body is persisted in any table, and this outbox is a local artifact
    outside the three-table zone model rather than a fourth store.
    """

    directory: Path
    calls: list[SesEmailRequest] = field(default_factory=list)

    async def send(self, request: SesEmailRequest) -> SesOutcome:
        self.calls.append(request)
        self.directory.mkdir(parents=True, exist_ok=True)
        message_id = f"local-{uuid4()}"
        path = self.directory / f"{message_id}.json"
        path.write_text(
            json.dumps(
                {
                    "message_id": message_id,
                    "from": request.from_email_address,
                    "to": list(request.to_addresses),
                    "reply_to": list(request.reply_to_addresses),
                    "configuration_set": request.configuration_set_name,
                    "tags": {tag.name: tag.value for tag in request.email_tags},
                    "subject": request.subject,
                    "text_body": request.text_body,
                    "html_body": request.html_body,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return SesAccepted(message_id=message_id)


@dataclass(slots=True)
class ScriptedSender:
    """Answer with scripted outcomes, and count deliberate calls.

    ``calls`` is the number the whole phase is about: **at most one deliberate SES attempt per
    approved execution**. A test asserts on it directly rather than inferring it from a durable
    state, because a definite failure that reached SES and one that never did produce the same
    ``FAILED`` row and are very different events.

    ``raises`` scripts an exception *escaping* the adapter, which is the one thing the port
    forbids -- and exists so the caller's own fail-safe default can be exercised.
    """

    outcomes: deque[SesOutcome] = field(default_factory=deque)
    raises: deque[BaseException] = field(default_factory=deque)
    calls: list[SesEmailRequest] = field(default_factory=list)
    default: SesOutcome | None = None

    async def send(self, request: SesEmailRequest) -> SesOutcome:
        self.calls.append(request)
        if self.raises:
            raise self.raises.popleft()
        if self.outcomes:
            return self.outcomes.popleft()
        if self.default is not None:
            return self.default
        return SesAccepted(message_id=f"scripted-{len(self.calls)}")

    @property
    def call_count(self) -> int:
        """How many deliberate SES attempts this sender was asked to make."""

        return len(self.calls)


__all__ = [
    "DEMO_ROUTING_TOKEN",
    "REGISTRY_DENIAL_DESTINATION",
    "REGISTRY_DENIAL_IDENTITY",
    "REGISTRY_DENIAL_ROUTING",
    "FilesystemOutboxSender",
    "InMemoryDestinationRegistry",
    "ScriptedSender",
    "demo_registry",
]
