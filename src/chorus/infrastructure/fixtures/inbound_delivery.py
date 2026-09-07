"""Turn a reviewed fixture selector into a delivery, so the demo goes through the boundary.

``POST /v1/demo/external-replies`` names a **fixture**, and this is what that name resolves to:
a receipt envelope and the raw MIME behind it, staged exactly where a deployed receipt action
would have put them, threaded to the ``ses_message_id`` the real send actually recorded. The
route then feeds the result through the *same* attester a deployed delivery uses, over the local
authenticator, so the demo exercises the trust boundary instead of bypassing it
([ADR-026](../../../../docs/adr/ADR-026-inbound-reply-trust-and-correlation.md) § Alternatives).

Two values are deliberately **not** the caller's and not the fixture's. The ``In-Reply-To`` comes
from the execution the case actually sent, because that is the correlation channel; and the
addresses come from the deployment's local configuration, because the digests they have to match
are the deployment's. Everything else is the reviewed message, unchanged.

The resolver is injected
-------------------------
:class:`FixtureReplyDeliverySource` is told which case to thread a reply to rather than
discovering one. A demo route that searched for "a case with a sent execution" would be a route
that picks its own target, which is a smaller version of the defect the fixture selector exists
to remove.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from chorus.domain.ids import CaseId, CommunityId, Namespace
from chorus.infrastructure.fixtures.inbound_replies import (
    INBOUND_FIXTURE_BUCKET,
    build_raw_message,
    build_receipt_envelope,
    outbound_message_id,
    reviewed_reply,
)
from chorus.infrastructure.local.inbound_mail import InMemoryInboundRawStore
from chorus.ports.clock import Clock
from chorus.ports.inbound_mail import InboundMailTransportContext
from chorus.ports.scopes import CaseScope


class DemoReplyDeliverySource(Protocol):
    """Resolve one reviewed fixture selector into one delivery, or refuse the selector."""

    async def deliver(
        self, fixture_id: str, *, idempotency_key: str
    ) -> InboundMailTransportContext:
        """Return the delivery, or raise ``KeyError`` for a selector nobody reviewed.

        ``idempotency_key`` is the caller's HTTP ``Idempotency-Key``, and it is what the
        delivery's own transport identity is derived from (Astra P2-5): a delivery minted with a
        freshly randomized ``Message-ID`` on every call defeats every idempotency check
        downstream, because ``IngestExternalReply`` keys its own replay on exactly that
        identifier. Deriving it from the caller's key instead is what lets an identical retry
        resolve to the identical delivery, and a retry under the same key with a *different*
        fixture resolve to the identical ``Message-ID`` over *different* content -- which
        ``IngestExternalReply`` already refuses as a conflict.
        """
        ...


type SentMessageResolver = Callable[[CaseScope], Awaitable[str]]
"""Answer "what identifier did this case's send record", from durable state.

A callable rather than a repository handle, so this module needs no read grant of its own and a
composition decides what "the demo case" means.
"""


def _deterministic_message_id(
    *, namespace: Namespace, case_id: CaseId, idempotency_key: str
) -> str:
    """The transport ``Message-ID`` an identical retry must also mint (Astra P2-5).

    A digest of the caller's Idempotency-Key alone -- not the fixture selector, so that a retry
    under the same key naming a *different* fixture still lands on this same identifier, over
    genuinely different raw MIME bytes. ``IngestExternalReply`` keys its own replay on the digest
    of this identifier and separately compares the raw content's own digest against what an
    earlier delivery under that key recorded, so same-key-different-content already surfaces as
    ``PersistenceConflictError`` there -- this function only has to make the identical-retry case
    reach that check with an identical identity instead of a fresh random one.
    """

    digest = sha256(
        f"reply-delivery\x1f{namespace.value}\x1f{case_id}\x1f{idempotency_key}".encode()
    ).hexdigest()
    return f"<reply-{digest}@manager.invalid>"


@dataclass(slots=True)
class FixtureReplyDeliverySource:
    """Stage one reviewed reply as a delivery against the deployment's own configuration."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    transport: str
    source_arn: str
    manager_address: str
    inbound_address: str
    raw_messages: InMemoryInboundRawStore
    resolve_sent_message_id: SentMessageResolver
    clock: Clock

    async def deliver(
        self, fixture_id: str, *, idempotency_key: str
    ) -> InboundMailTransportContext:
        reply = reviewed_reply(fixture_id)
        scope = CaseScope(
            namespace=self.namespace, community_id=self.community_id, case_id=self.case_id
        )
        ses_message_id = await self.resolve_sent_message_id(scope)
        message_id = _deterministic_message_id(
            namespace=self.namespace, case_id=self.case_id, idempotency_key=idempotency_key
        )
        object_key = f"inbound/{self.namespace.value}/{message_id.strip('<>')}"
        self.raw_messages.put(
            bucket=INBOUND_FIXTURE_BUCKET,
            key=object_key,
            content=build_raw_message(
                reply,
                message_id=message_id,
                in_reply_to=outbound_message_id(ses_message_id),
                from_address=self.manager_address,
                to_address=self.inbound_address,
            ),
        )
        return InboundMailTransportContext(
            transport=self.transport,
            source_arn=self.source_arn,
            envelope=build_receipt_envelope(
                message_id=message_id,
                in_reply_to=outbound_message_id(ses_message_id),
                subject=reply.subject,
                source=self.manager_address,
                destination=self.inbound_address,
                received_at=self.clock.now(),
                object_key=object_key,
            ),
        )


__all__ = [
    "DemoReplyDeliverySource",
    "FixtureReplyDeliverySource",
    "SentMessageResolver",
]
