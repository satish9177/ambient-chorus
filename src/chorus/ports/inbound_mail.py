"""What a delivered inbound message may say about its own origin, and who may say it.

Parsing a receipt envelope proves that somebody produced well-formed JSON. It does not prove
that SES delivered it. An inbound reply is the one value in this phase that turns an outside
party's prose into a durable management promise, so the value it consumes must be established
by something that authenticated the *transport*, not by something that read the *payload*
([ADR-026](../../../docs/adr/ADR-026-inbound-reply-trust-and-correlation.md) SS 1, T36).

This module is that separation written as two types, and it declares nothing else:

* :class:`InboundMailTransportContext` -- one delivery exactly as it arrived: the mechanism
  that carried it, the receipt-rule/topic/queue resource it came through, and the untrusted
  envelope. Every field is an **observation**, never a conclusion. There is deliberately no
  ``authenticated`` flag: a boolean a caller sets is the forgery, not the defence -- the
  sentence :mod:`chorus.ports.ses_events` already carries, restated here because it is the
  whole reason this port exists.
* :class:`InboundMailTransportAuthenticator` -- the narrow authority that decides whether a
  delivery really came from the deployment's own inbound transport.

**Phase 9 ships no deployed authenticator.** Phase 11 owes exactly one object: an
implementation of this protocol over the SES receipt-rule transport it builds. Until it
exists there is no authenticator to construct an attester with, no attester to mint an
artifact, and no verifier on ``IngestExternalReply`` -- and a delivered reply is therefore
simply not evidence. A reply that cannot be authenticated producing no commitment is the
correct outcome, not a gap.

A **local** authenticator lives in :mod:`chorus.infrastructure.local.inbound_mail`, is
constructed only by the local/development composition root, and refuses at construction in
any other environment. That is the same split :mod:`chorus.infrastructure.local.sender`
already uses, and it is why the local fake cannot become a permissive stand-in at the
deployed call site.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundMailTransportContext:
    """One delivery: how it arrived, what it arrived through, and what it said.

    ``transport`` and ``source_arn`` describe the *channel* and are compared against the values
    the deployment configured, so a genuine notification carried by somebody else's receipt
    rule set is a foreign delivery rather than an accepted one. ``envelope`` is the SES receipt
    body and is **untrusted until decoded**; nothing reads it before the transport has been
    authenticated.
    """

    transport: str
    """The delivery mechanism, as the event source names it -- ``aws:ses-receipt``, ``aws:sns``,
    ``aws:sqs``."""

    source_arn: str
    """The receipt rule set, topic, or queue the delivery arrived through."""

    envelope: Mapping[str, Any]
    """The raw SES receipt body. Never trusted, and never read before the rest is."""

    def __post_init__(self) -> None:
        if not self.transport or not self.source_arn:
            raise ValueError("a delivery names the transport and the resource it arrived through")


class InboundMailTransportAuthenticator(Protocol):
    """Decide whether one delivery genuinely came from the deployment's own inbound transport.

    Asynchronous because the deployed implementations are: verifying an SNS signature means
    fetching and caching the topic's signing certificate. An implementation returns ``False``
    rather than raising for an unauthenticated delivery -- that is an ordinary outcome on this
    boundary, not an error -- and raises only when it cannot reach a decision at all.
    """

    async def authenticate(self, context: InboundMailTransportContext) -> bool:
        """Return ``True`` only when the transport itself proves the origin."""
        ...


__all__ = ["InboundMailTransportAuthenticator", "InboundMailTransportContext"]
