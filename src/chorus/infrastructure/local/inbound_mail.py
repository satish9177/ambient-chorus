"""The local inbound transport authenticator, and the raw-message store the fixtures use.

Both exist so the Phase-9 boundary can be exercised end to end without the Phase-11 transport,
and **neither is ever the deployed path**. That is the same split
:mod:`chorus.infrastructure.local.sender` already uses, and the reason is identical: an
always-yes authenticator that shipped in the application would be indistinguishable, at the call
site, from a real one.

:class:`LocalInboundMailAuthenticator` **refuses at construction** outside ``test`` and
``development``. It is not a permissive stand-in that happens not to be wired in production; it
is an object a production composition cannot build. A static test additionally asserts that the
AWS composition root passes ``authenticator=None``, so the two guarantees are independent: one
in the type, one in the wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from chorus.ports.errors import NotFoundError
from chorus.ports.inbound_mail import InboundMailTransportContext
from chorus.settings import Environment

LOCAL_INBOUND_ENVIRONMENTS: frozenset[Environment] = frozenset(
    {Environment.TEST, Environment.DEVELOPMENT}
)
"""The only environments a local authenticator may exist in.

ADR-026 § 1 names ``local`` and ``development``; this repository's :class:`Environment` has no
``local`` member and calls that environment ``test``, so the pair is spelled with the members
that exist. ``demo`` is deliberately absent: the demo is a deployed environment, and its inbound
authenticity is Phase 11's to supply.
"""


@dataclass(slots=True)
class LocalInboundMailAuthenticator:
    """Authenticate a local delivery by the transport pair the composition configured.

    It answers ``True`` for a delivery that arrived through the exact transport and resource
    this composition named, and ``False`` for anything else -- which is the same *shape* of
    answer the Phase-11 authenticator gives, so the code above it is unchanged between them.
    What it does not do is prove anything: there is no signature to verify locally, and it says
    so rather than implying otherwise.

    ``accepts`` exists so a local run can exercise the refusal branch on demand. It is the
    counterpart of :class:`chorus.infrastructure.local.sender.ScriptedSender`'s outcome queue,
    and like it, it is a seam rather than a policy.
    """

    environment: Environment
    transport: str
    source_arn: str
    accepts: bool = True
    calls: int = 0

    def __post_init__(self) -> None:
        if self.environment not in LOCAL_INBOUND_ENVIRONMENTS:
            raise ValueError("a local inbound authenticator exists only in test or development")
        if not self.transport or not self.source_arn:
            raise ValueError("a local authenticator names the transport pair it accepts")

    async def authenticate(self, context: InboundMailTransportContext) -> bool:
        self.calls += 1
        if not self.accepts:
            return False
        return context.transport == self.transport and context.source_arn == self.source_arn


@dataclass(slots=True)
class InMemoryInboundRawStore:
    """The raw MIME a receipt action would have written, keyed exactly as the envelope names it.

    Deployed, the bytes live in the SES receipt bucket and Phase 11 builds both. Here they live
    in a dictionary addressed by the same ``{bucket, key}`` pair, so the attester's read is the
    same read against either -- and neither bucket nor key is ever caller-supplied at the
    application boundary: both are read out of the authenticated receipt envelope.
    """

    objects: dict[tuple[str, str], bytes] = field(default_factory=dict)
    reads: int = 0

    def put(self, *, bucket: str, key: str, content: bytes) -> None:
        self.objects[(bucket, key)] = content

    async def read(self, *, bucket: str, key: str) -> bytes:
        self.reads += 1
        content = self.objects.get((bucket, key))
        if content is None:
            raise NotFoundError("INBOUND_RAW_MESSAGE")
        return content


__all__ = [
    "LOCAL_INBOUND_ENVIRONMENTS",
    "InMemoryInboundRawStore",
    "LocalInboundMailAuthenticator",
]
