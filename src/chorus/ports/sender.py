"""The external send boundary: one destination registry, one payload shape, one attempt.

Three things live here and nothing else does.

**The registry**, which is the only place an email address exists in this system. The sender
resolves ``{destination_id, registry_version, routing_token}`` -- all three of which are inside
``preview_hash`` -- to exactly one address, and ``from_identity_id`` to exactly one
``{from_address, reply_to_address, identity_arn}`` entry. Binding the opaque identity handle in
the preview digest is therefore how an approval binds the whole letterhead -- sender and reply
path together -- while no artifact a model, a human preview, an audit row, or a log line can see
ever contains either address ([ADR-025](../../../docs/adr/ADR-025-one-deliberate-ses-attempt.md)
SS 6).

**The payload**, frozen field by field. ``Content.Simple``, never ``Content.Raw``: SES composes
the ``multipart/alternative`` structure and every header itself, so the sender never builds a
header line and header injection is structurally impossible rather than merely defended
against. Exactly one recipient, no ``Cc``, no ``Bcc``, asserted at construction.

**The outcome**, classified rather than raised. ``FAILED`` requires proof; everything else is
``SEND_UNKNOWN``. A received error response proves SES processed the request and declined it; a
connection that never established proves at the transport layer that no request was
transmitted. Everything between those two proofs, and every exception class nobody enumerated,
is unknown -- and the last part is why :class:`SesUnknown` is what an adapter returns when it
does not recognise what happened, rather than what somebody remembered to add.

The port returns an outcome and **does not raise for a transport condition**, because an
exception escaping into the send command would have to be classified there anyway, and one
classification is better than two. The command still wraps the call, so an adapter that breaks
its own contract lands on the safe side rather than on a stack trace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from chorus.domain.entities import DestinationKind
from chorus.domain.ids import DestinationId

MAX_EMAIL_TAG_VALUE_LENGTH = 256
_EMAIL_TAG_VALUE = re.compile(r"^[A-Za-z0-9_-]+$")
"""SES admits only these characters in an email-tag value.

Frozen here rather than discovered at the first live send. It is the whole reason the execution
tag is bare lowercase hex with no ``sha256:`` prefix: a colon would be rejected at the API
([ADR-025](../../../docs/adr/ADR-025-one-deliberate-ses-attempt.md) SS 7).
"""

_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _address(value: str, label: str) -> str:
    if not _ADDRESS.fullmatch(value):
        raise ValueError(f"{label} is not an email address")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SendingIdentity:
    """One verified sending identity, resolved from the registry secret by the sender alone.

    ``identity_id`` is the opaque handle the preview digest binds. The two addresses and the
    ARN are what it resolves to, and they exist only inside the sender process: no proposal, no
    approval, no execution, no audit row, and no log line has a field to put one in.
    """

    identity_id: str
    from_address: str
    reply_to_address: str
    identity_arn: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.identity_id) <= 120:
            raise ValueError("sending identity handle length is invalid")
        _address(self.from_address, "from address")
        _address(self.reply_to_address, "reply-to address")
        if not self.identity_arn:
            raise ValueError("a sending identity names its ARN")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedDestination:
    """The one allowlisted recipient this routing triple resolves to.

    The triple is checked by the registry, not by the caller: ``destination_id``,
    ``registry_version``, and ``routing_token`` are all inside ``preview_hash``, so a registry
    that resolved a *near* match would silently route a message the human approved for one
    recipient to another.
    """

    destination_id: DestinationId
    kind: DestinationKind
    registry_version: int
    routing_token: UUID
    display_label: str
    address: str

    def __post_init__(self) -> None:
        if self.registry_version < 1:
            raise ValueError("destination registry version must be positive")
        _address(self.address, "destination address")


class DestinationRegistryError(Exception):
    """The registry refused to resolve. It never falls back and never guesses.

    Carries a closed reason code and nothing else -- specifically not the value that failed to
    match, because that value is an address or a token.
    """

    __slots__ = ("reason_code",)

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class DestinationRegistryPort(Protocol):
    """Resolve the two things only the sender may know."""

    async def resolve_destination(
        self, *, destination_id: DestinationId, registry_version: int, routing_token: UUID
    ) -> ResolvedDestination:
        """Return the single allowlisted entry for this exact triple, or refuse.

        Any mismatch on any of the three denies. A registry that answered on
        ``destination_id`` alone would let a rotated entry route an approved message.
        """

    async def resolve_sending_identity(self, identity_id: str) -> SendingIdentity:
        """Return the single ``{from, reply-to, ARN}`` entry for this handle, or refuse."""


@dataclass(frozen=True, slots=True, kw_only=True)
class EmailTag:
    """One SES configuration-set email tag, validated against the API's own grammar."""

    name: str
    value: str

    def __post_init__(self) -> None:
        for field_name, text in (("name", self.name), ("value", self.value)):
            if not 1 <= len(text) <= MAX_EMAIL_TAG_VALUE_LENGTH:
                raise ValueError(f"email tag {field_name} length is invalid")
            if not _EMAIL_TAG_VALUE.fullmatch(text):
                raise ValueError(f"email tag {field_name} contains a character SES rejects")


@dataclass(frozen=True, slots=True, kw_only=True)
class SesEmailRequest:
    """The frozen SESv2 ``SendEmail`` request, with every omission deliberate.

    ``Content.Simple`` only. ``Content.Raw`` and ``Content.Template`` are never used and have no
    field here, so there is nowhere for a future change to put a hand-built MIME body.
    ``Destination.CcAddresses``, ``Destination.BccAddresses``,
    ``FeedbackForwardingEmailAddress``, and ``ListManagementOptions`` are omitted, and their
    omission is enforced by their absence rather than by an assertion somebody has to run.

    Exactly one recipient and exactly one ``Reply-To`` are asserted at construction. That is
    where the single-recipient rule is enforceable without publishing an address into a
    synthesized CloudFormation template, which is why the IAM allow is deliberately *not*
    narrowed with ``ses:Recipients`` (ADR-024 SS 5).
    """

    from_email_address: str
    to_addresses: tuple[str, ...]
    reply_to_addresses: tuple[str, ...]
    configuration_set_name: str
    email_tags: tuple[EmailTag, ...]
    subject: str
    text_body: str
    html_body: str
    charset: str = "UTF-8"

    def __post_init__(self) -> None:
        _address(self.from_email_address, "from address")
        if len(self.to_addresses) != 1:
            # The frozen bound, asserted before the call rather than hoped for. V1 sends to one
            # allowlisted recipient; a list is a campaign, and this system does not have one.
            raise ValueError("exactly one recipient is permitted")
        if len(self.reply_to_addresses) != 1:
            raise ValueError("exactly one reply-to address is permitted")
        _address(self.to_addresses[0], "destination address")
        _address(self.reply_to_addresses[0], "reply-to address")
        if not self.configuration_set_name:
            raise ValueError("a configuration set is required")
        if self.charset != "UTF-8":
            raise ValueError("the frozen charset is UTF-8")
        if not self.subject or not self.text_body or not self.html_body:
            raise ValueError("subject and both bodies are required")
        names = tuple(tag.name for tag in self.email_tags)
        if len(set(names)) != len(names):
            raise ValueError("email tag names must be unique")


class SendFailureCode(StrEnum):
    """The closed set of *definite* failure codes, each requiring its own proof.

    ``SES_REJECTED`` and ``SES_DEFINITE_FAILURE`` both mean a response was received and SES
    declined the request; the split records whether the cause was the request's shape or the
    service's condition. ``SES_UNREACHABLE`` means the connection never established, which is
    proof at the transport layer that no request was transmitted.

    Everything not on this list is not a failure code at all -- it is
    :class:`SesUnknown` ([ADR-025](../../../docs/adr/ADR-025-one-deliberate-ses-attempt.md) SS 8).
    """

    SES_REJECTED = "SES_REJECTED"
    SES_DEFINITE_FAILURE = "SES_DEFINITE_FAILURE"
    SES_UNREACHABLE = "SES_UNREACHABLE"


class SendUnknownReason(StrEnum):
    """Why an attempt's outcome is unknown. None of these authorizes a second attempt."""

    SES_TIMEOUT = "SES_TIMEOUT"
    SES_TRANSPORT_AMBIGUOUS = "SES_TRANSPORT_AMBIGUOUS"
    SENDER_PROCESS_LOST = "SENDER_PROCESS_LOST"


@dataclass(frozen=True, slots=True, kw_only=True)
class SesAccepted:
    """SES returned a message ID. The only observation that means the message was queued."""

    message_id: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.message_id) <= 256:
            raise ValueError("an SES message identifier is required")


@dataclass(frozen=True, slots=True, kw_only=True)
class SesDefiniteFailure:
    """Proof exists that this request was not queued for delivery."""

    failure_code: SendFailureCode
    detail_safe: str | None = None

    def __post_init__(self) -> None:
        if self.detail_safe is not None and not 1 <= len(self.detail_safe) <= 64:
            raise ValueError("a safe failure detail is a short closed code")


@dataclass(frozen=True, slots=True, kw_only=True)
class SesUnknown:
    """No proof either way. The safe side, and the default for anything unenumerated."""

    reason_code: SendUnknownReason = SendUnknownReason.SES_TRANSPORT_AMBIGUOUS


type SesOutcome = SesAccepted | SesDefiniteFailure | SesUnknown


class EmailSenderPort(Protocol):
    """Make at most one deliberate SES attempt and classify what happened."""

    async def send(self, request: SesEmailRequest) -> SesOutcome:
        """Send once and return a classified outcome.

        Implementations **must not raise for a transport condition**: an unrecognised failure
        is :class:`SesUnknown`, because the safe side has to be the default rather than the
        remembered case. They must also never repeat the call internally -- no SDK retry, no
        backoff loop -- because the whole safety property is about the number of deliberate
        attempts, and a retry inside the adapter is a second attempt the caller cannot see.
        """


__all__ = [
    "MAX_EMAIL_TAG_VALUE_LENGTH",
    "DestinationRegistryError",
    "DestinationRegistryPort",
    "EmailSenderPort",
    "EmailTag",
    "ResolvedDestination",
    "SendFailureCode",
    "SendUnknownReason",
    "SendingIdentity",
    "SesAccepted",
    "SesDefiniteFailure",
    "SesEmailRequest",
    "SesOutcome",
    "SesUnknown",
]
