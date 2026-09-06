"""What a delivered SES event may say about its own origin, and who is allowed to say it.

Parsing an envelope proves that somebody produced well-formed JSON. It does not prove that SES
produced it. ``SEND_UNKNOWN -> SENT`` is the one edge in the phase that turns an outside claim
into a durable statement that a message left the system, so the value it consumes must be
established by something that authenticated the *transport*, not by something that read the
*payload* (ADR-025 SS 10, T35).

This module is that separation written as two types:

* :class:`SesEventTransportContext` -- one delivery exactly as it arrived: the mechanism that
  carried it, the event-destination resource it came through, and the untrusted body. Every
  field is an **observation**, and none of them is a conclusion. There is deliberately no
  ``authenticated`` flag: a boolean a caller sets is the forgery, not the defence.
* :class:`SesEventTransportAuthenticator` -- the narrow authority that decides whether a
  delivery really came from the deployment's own event destination.

**Phase 8 declares this port and implements no authenticator at all.** That absence is the
design and not an omission: an authenticator is a statement about a deployed transport --- an
SNS signature verified against the topic's signing certificate, an EventBridge rule's own
invocation identity, a queue whose only writer is the configuration set's event destination ---
and none of those resources exists until Phase 11 builds them (ADR-025 SS 16). Wiring a
permissive stand-in now would be indistinguishable, at the call site, from the real thing.

The consequence is intentional and is asserted by the regressions: with no authenticator, the
attester refuses everything, ``ReconcileSendOutcome`` has no verifier to satisfy, and a
quarantined execution simply stays quarantined. Uncertainty remaining unknown is the correct
outcome (ADR-025 SS 9); a quarantine resolved by an unauthenticated mapping is not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True, kw_only=True)
class SesEventTransportContext:
    """One delivery: how it arrived, what it arrived through, and what it said.

    ``transport`` and ``source_arn`` describe the *channel* and are compared against the values
    the deployment configured, so a genuine notification carried by somebody else's topic is a
    foreign delivery rather than an accepted one. ``envelope`` is the SES body and is
    **untrusted until decoded**; nothing reads it before the transport has been authenticated.
    """

    transport: str
    """The delivery mechanism, as the event source names it -- ``aws:sns``, ``aws:sqs``."""

    source_arn: str
    """The topic, queue, or rule ARN the notification was delivered through."""

    envelope: Mapping[str, Any]
    """The raw configuration-set event body. Never trusted, and never read before the rest is."""

    def __post_init__(self) -> None:
        if not self.transport or not self.source_arn:
            raise ValueError("a delivery names the transport and the resource it arrived through")


class SesEventTransportAuthenticator(Protocol):
    """Decide whether one delivery genuinely came from the deployment's own event destination.

    Asynchronous because the deployed implementations are: verifying an SNS signature means
    fetching and caching the topic's signing certificate. An implementation returns ``False``
    rather than raising for an unauthenticated delivery -- that is an ordinary outcome on this
    boundary, not an error -- and raises only when it cannot reach a decision at all.
    """

    async def authenticate(self, context: SesEventTransportContext) -> bool:
        """Return ``True`` only when the transport itself proves the origin."""
        ...


__all__ = ["SesEventTransportAuthenticator", "SesEventTransportContext"]
