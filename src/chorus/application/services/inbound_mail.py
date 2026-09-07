"""Turn one authenticated inbound delivery into a correlated artifact, or refuse it.

Why this module exists rather than an HTTP body
------------------------------------------------
``POST /v1/demo/external-replies`` used to take ``{case_id, action_id, channel_message_id,
received_at, from_destination_id, subject, text}``. Every field a reply's provenance depends on
was supplied by the caller: ``from_destination_id`` *asserted* who wrote it, ``case_id`` and
``action_id`` *asserted* what it answered, and ``text`` was the body a commitment would be
extracted from. Anybody holding the demo token could therefore manufacture a management promise
about any case, attributed to the approved destination -- which is T18 with the attacker handed
the pen, and the identical shape Phase 8 removed one boundary over
([ADR-026](../../../../docs/adr/ADR-026-inbound-reply-trust-and-correlation.md) § Context).

There is no such endpoint and there must not be one. The demo route takes a **fixture
selector**, and the fixture is fed through this same attester over the local authenticator, so
the demo exercises the boundary instead of bypassing it.

Four named stages, and the order is frozen
-------------------------------------------
::

    UNTRUSTED TRANSPORT PAYLOAD    a delivery: mechanism, resource ARN, opaque envelope
            |
            v  InboundMailTransportAuthenticator   (Phase 11 supplies the only implementation)
    TRUSTED DELIVERY               the transport itself proved the origin
            |
            v  InboundMailAttester    verdicts, decode, correlate, attest
    AUTHENTICATED CORRELATED ARTIFACT   AttestedInboundReply, minted only here
            |
            v  InboundMailEvidenceVerifier   (held by IngestExternalReply, can only check)
    APPLICATION / DOMAIN LOGIC     persistence, extraction, commitment, case state

Authenticate, then gate on verdicts, then decode, then correlate, then attest. Never any other
order: decoding first means a forged envelope has already been interpreted by the time anybody
asks where it came from, and every field of that interpretation is the forger's.

The verdict gate is what turns "who wrote this" from a header into a fact, and it is why a
forged ``In-Reply-To`` gets nowhere: the forger would also have to pass DMARC as the approved
destination's domain.

Two halves that are never the same object
------------------------------------------
:class:`InboundMailAttester` mints; :class:`InboundMailEvidenceVerifier` can only check. They
share a process-local key generated inside :func:`inbound_mail_trust_boundary` and handed to
nobody, so a hand-built :class:`AttestedInboundReply`, a replayed attestation from another
deployment's ARN, and a correlation that never crossed the attester are all the same thing to
the verifier: not evidence. Constructing the class by hand is possible -- Python has no private
constructors and pretending otherwise is theatre -- and useless.

**Phase 9 ships no deployed authenticator.** With none, the attester refuses everything,
``IngestExternalReply`` has no verifier to satisfy, and a delivered reply is simply not
evidence. That is the correct outcome, not a gap.

No address ever reaches this module
------------------------------------
The destination-address secret belongs to the sender alone, and the inbound path must not become
a second holder. The two comparisons of § 3 are therefore comparisons of **digests** against the
non-secret safe destination configuration, and no audit row, log line, or persisted item on this
path has a field to put an address in. The residual -- an attacker who already knows an address
can confirm it -- is accepted in the ADR: the digest is a comparison token, never a credential.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import Message
from email.policy import compat32
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol

from chorus.application.services.action_grounding import normalize
from chorus.application.services.action_renderer import TEMPLATE_VERSION, render_preview
from chorus.domain.entities import (
    INBOUND_RECEIPT_FAIL,
    INBOUND_RECEIPT_PASS,
    ActionExecutionState,
    CaseState,
)
from chorus.domain.ids import (
    ActionId,
    CaseId,
    CommunityId,
    ExecutionId,
    Namespace,
    Sha256Digest,
)
from chorus.ports.errors import NotFoundError
from chorus.ports.inbound_mail import (
    InboundMailTransportAuthenticator,
    InboundMailTransportContext,
)
from chorus.ports.objects import MAX_INBOUND_REPLY_BYTES
from chorus.ports.records import OutboundMessageLocator, SafeInboundMailConfiguration
from chorus.ports.repositories import CoreRepositoryPort, ShareableRepositoryPort
from chorus.ports.scopes import ActionScope, NamespaceScope

INBOUND_PARTY_DIGEST_SCHEMA = "inbound-reply-party/v1"
"""The tag inside every address digest, so a digest from this scheme is only ever compared
with another one from this scheme."""

MAX_EXTRACTED_TEXT_BYTES = 8 * 1024
"""The frozen bound on the normalized, quote-stripped plain text (ADR-026 § 4)."""

MAX_MESSAGE_IDS = 32
"""How many thread references one reply may offer for correlation.

A bound rather than an unbounded set, because ``References`` is attacker-influenced and each
member costs a key in a batch get. A reply that names more than a long thread's worth of
messages is not a reply this system needs to correlate.
"""

_ANGLE_ADDRESSED = re.compile(r"<([^<>]{1,320})>")
"""RFC 5322 ``msg-id`` as it appears in ``In-Reply-To`` and ``References``."""


def address_digest(namespace: Namespace, address: str) -> Sha256Digest:
    """``sha256("inbound-reply-party/v1" | namespace | normalize(address))``.

    The one function that turns an address into the only form this path is allowed to hold. It
    is deliberately **not** reversible and deliberately not a secret: it exists so the sender
    and recipient comparisons can be made by a principal that never learns an address.

    Normalization is the same NFC-and-whitespace fold every other comparison in this system
    uses, so a display-form address and its canonical form do not silently differ. Nothing more
    is done -- no domain lowercasing beyond the casefold ``normalize`` already applies, no
    plus-tag stripping, no dot folding: each of those is a guess that two different mailboxes
    are one mailbox.
    """

    payload = "\x1f".join((INBOUND_PARTY_DIGEST_SCHEMA, namespace.value, normalize(address)))
    return Sha256Digest(f"sha256:{sha256(payload.encode('utf-8')).hexdigest()}")


class InboundMailTrustFailure(StrEnum):
    """Why a delivery never became an artifact. Closed codes; never the delivery's content."""

    TRANSPORT_UNAVAILABLE = "TRANSPORT_UNAVAILABLE"
    """No authenticator is wired, so nothing here can establish origin. Phase 11 owes one."""

    FOREIGN_TRANSPORT_SOURCE = "FOREIGN_TRANSPORT_SOURCE"
    """Authentic, perhaps -- but carried by a mechanism or resource this deployment never used."""

    TRANSPORT_UNAUTHENTICATED = "TRANSPORT_UNAUTHENTICATED"
    """The authenticator looked at this delivery and did not recognise it as ours."""

    INBOUND_VERDICT_FAILED = "INBOUND_VERDICT_FAILED"
    """SPF, DKIM, or DMARC did not pass, or spam/virus screening failed."""


class InboundReplyRejection(StrEnum):
    """Why an authenticated delivery is not a reply about anything.

    Thirteen closed codes: one decode, eight correlation, and four body. Each is safe to log,
    audit, and count, and none of them can carry the offending envelope, address, or text.
    """

    MALFORMED_ENVELOPE = "MALFORMED_ENVELOPE"

    REPLY_UNCORRELATED = "REPLY_UNCORRELATED"
    REPLY_CORRELATION_AMBIGUOUS = "REPLY_CORRELATION_AMBIGUOUS"
    REPLY_EXECUTION_NOT_SENT = "REPLY_EXECUTION_NOT_SENT"
    REPLY_MESSAGE_ID_MISMATCH = "REPLY_MESSAGE_ID_MISMATCH"
    REPLY_CASE_TERMINAL = "REPLY_CASE_TERMINAL"
    REPLY_CASE_NOT_ACTIONED = "REPLY_CASE_NOT_ACTIONED"
    REPLY_SENDER_NOT_DESTINATION = "REPLY_SENDER_NOT_DESTINATION"
    REPLY_RECIPIENT_NOT_OURS = "REPLY_RECIPIENT_NOT_OURS"

    REPLY_NO_PLAIN_TEXT = "REPLY_NO_PLAIN_TEXT"
    REPLY_ATTACHMENT_PRESENT = "REPLY_ATTACHMENT_PRESENT"
    REPLY_TOO_LARGE = "REPLY_TOO_LARGE"
    REPLY_HEADERS_TRUNCATED = "REPLY_HEADERS_TRUNCATED"
    REPLY_TOO_MANY_REFERENCES = "REPLY_TOO_MANY_REFERENCES"


class InboundMailUntrusted(Exception):
    """This delivery is not evidence, and it never reached the decoder.

    Deliberately distinct from :class:`InboundReplyRejected`: that one means "the delivery was
    unusable", and this means "there was never a reason to read it". An operator seeing the two
    collapsed into one code could not tell a malformed notification from somebody probing the
    boundary, and only the second is an attack.
    """

    __slots__ = ("failure",)

    def __init__(self, failure: InboundMailTrustFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure

    @property
    def safe_code(self) -> str:
        return self.failure.value


class InboundReplyRejected(Exception):
    """An authenticated delivery that decoding, correlation, or the body rules refused.

    Every one of them fails closed, persists nothing in the case, and carries a closed reason
    code and no content.
    """

    __slots__ = ("rejection",)

    def __init__(self, rejection: InboundReplyRejection) -> None:
        super().__init__(rejection.value)
        self.rejection = rejection

    @property
    def safe_code(self) -> str:
        return self.rejection.value


class InboundRawMessageReader(Protocol):
    """Fetch the raw MIME the receipt action stored, by the bucket and key it names.

    A port because the deployed source is the SES receipt bucket, which Phase 11 builds, and the
    local source is a reviewed fixture. Neither the bucket nor the key is caller-supplied at the
    application boundary: both are read out of the authenticated receipt envelope.
    """

    async def read(self, *, bucket: str, key: str) -> bytes:
        """Return the raw bytes, or raise ``NotFoundError``."""
        ...


# ---------------------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ReceiptVerdicts:
    """The five SES receipt verdicts, recorded exactly as the envelope stated them."""

    spf: str
    dkim: str
    dmarc: str
    spam: str
    virus: str

    @property
    def accepted(self) -> bool:
        """SPF, DKIM and DMARC all ``PASS``, and neither screening verdict ``FAIL``.

        The asymmetry is SES's own: spam and virus verdicts have states other than pass and
        fail -- ``GRAY``, ``PROCESSING_FAILED`` -- and requiring ``PASS`` from them would refuse
        deliveries the service never screened. Requiring ``PASS`` from the three authentication
        verdicts is what makes the sender comparison meaningful at all.
        """

        authenticated = (self.spf, self.dkim, self.dmarc)
        screened = (self.spam, self.virus)
        return all(value == INBOUND_RECEIPT_PASS for value in authenticated) and all(
            value != INBOUND_RECEIPT_FAIL for value in screened
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DecodedReceipt:
    """Every field read out of the receipt envelope, and nothing a caller supplied."""

    message_id: str
    in_reply_to: tuple[str, ...]
    references: tuple[str, ...]
    subject_length: int
    """The subject is read **for length only and never persisted** (ADR-026 § 5)."""
    source: str
    destination: str
    bucket_name: str
    object_key: str
    headers_truncated: bool
    received_at: datetime
    verdicts: ReceiptVerdicts

    @property
    def thread_references(self) -> tuple[str, ...]:
        """Every message identifier this reply offers, ``In-Reply-To`` first, deduplicated.

        Refuses closed rather than truncating when the deduplicated count exceeds
        :data:`MAX_MESSAGE_IDS`. A silent truncation could pad an over-limit reference set with
        unknown identifiers so that a *second* genuine match landed past the cutoff -- turning
        what should be a ``REPLY_CORRELATION_AMBIGUOUS`` refusal into an apparently unique,
        successful correlation. A security decision must never be made against a set that was
        quietly cut down first.
        """

        deduplicated = tuple(dict.fromkeys(self.in_reply_to + self.references))
        if len(deduplicated) > MAX_MESSAGE_IDS:
            raise InboundReplyRejected(InboundReplyRejection.REPLY_TOO_MANY_REFERENCES)
        return deduplicated


def _text(mapping: Mapping[str, Any], name: str) -> str:
    value = mapping.get(name)
    if not isinstance(value, str) or not value:
        raise InboundReplyRejected(InboundReplyRejection.MALFORMED_ENVELOPE)
    return value


def _mapping(mapping: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = mapping.get(name)
    if not isinstance(value, dict):
        raise InboundReplyRejected(InboundReplyRejection.MALFORMED_ENVELOPE)
    return value


def _verdict(receipt: Mapping[str, Any], name: str) -> str:
    status = _mapping(receipt, name).get("status")
    if not isinstance(status, str) or not status:
        raise InboundReplyRejected(InboundReplyRejection.MALFORMED_ENVELOPE)
    return status


def _message_ids(value: Any) -> tuple[str, ...]:
    """Read a header that is either one msg-id string or a list of them.

    A field shaped differently than SES publishes it makes the whole envelope unusable, so an
    absent header is an empty tuple and anything else is a rejection -- an envelope this code
    had to guess about is an envelope somebody could have constructed.
    """

    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(_ANGLE_ADDRESSED.findall(value)) or ()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        found: list[str] = []
        for item in value:
            found.extend(_ANGLE_ADDRESSED.findall(item))
        return tuple(found)
    raise InboundReplyRejected(InboundReplyRejection.MALFORMED_ENVELOPE)


def _receipt_instant(value: str) -> datetime:
    """Parse the receipt timestamp, which SES publishes with millisecond precision.

    Not :func:`chorus.domain.time.parse_utc`: that one is the *canonical persisted* form and
    requires exactly six fractional digits, which SES does not emit. The instant is normalized
    to UTC here and re-serialized canonically by the codec, so exactly one representation is
    ever stored.
    """

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InboundReplyRejected(InboundReplyRejection.MALFORMED_ENVELOPE) from error
    if parsed.tzinfo is None:
        raise InboundReplyRejected(InboundReplyRejection.MALFORMED_ENVELOPE)
    return parsed.astimezone(UTC)


def decode_receipt_envelope(envelope: Mapping[str, Any]) -> DecodedReceipt:
    """Read every field out of one SES receipt envelope. Nothing is defaulted or inferred."""

    mail = _mapping(envelope, "mail")
    receipt = _mapping(envelope, "receipt")
    headers = _mapping(mail, "commonHeaders")
    action = _mapping(receipt, "action")

    truncated = mail.get("headersTruncated")
    if not isinstance(truncated, bool):
        raise InboundReplyRejected(InboundReplyRejection.MALFORMED_ENVELOPE)

    destinations = mail.get("destination")
    if not isinstance(destinations, list) or not all(
        isinstance(item, str) for item in destinations
    ):
        raise InboundReplyRejected(InboundReplyRejection.MALFORMED_ENVELOPE)
    if len(destinations) != 1:
        # A delivery addressed to more than our own inbound mailbox is not solely ours, and the
        # comparison § 3 makes has no single value to make. Refused with the recipient code
        # rather than the malformed one, because the envelope is well formed and the *routing*
        # is what disqualifies it.
        raise InboundReplyRejected(InboundReplyRejection.REPLY_RECIPIENT_NOT_OURS)

    subject = headers.get("subject")
    if subject is not None and not isinstance(subject, str):
        raise InboundReplyRejected(InboundReplyRejection.MALFORMED_ENVELOPE)

    return DecodedReceipt(
        message_id=_text(headers, "messageId"),
        in_reply_to=_message_ids(headers.get("inReplyTo")),
        references=_message_ids(headers.get("references")),
        subject_length=len(subject or ""),
        source=_text(mail, "source"),
        destination=destinations[0],
        bucket_name=_text(action, "bucketName"),
        object_key=_text(action, "objectKey"),
        headers_truncated=truncated,
        received_at=_receipt_instant(_text(mail, "timestamp")),
        verdicts=ReceiptVerdicts(
            spf=_verdict(receipt, "spfVerdict"),
            dkim=_verdict(receipt, "dkimVerdict"),
            dmarc=_verdict(receipt, "dmarcVerdict"),
            spam=_verdict(receipt, "spamVerdict"),
            virus=_verdict(receipt, "virusVerdict"),
        ),
    )


def message_id_local_part(message_id: str) -> str:
    """The RFC 5322 local part of a ``msg-id``, which for an SES send *is* its message ID.

    SES composes the outbound ``Message-ID`` itself and sets it to
    ``<{messageId}@{region}.amazonses.com>``, so the local part of a correspondent's
    ``In-Reply-To`` is exactly the identifier the send recorded. That is the whole of the
    correlation channel: no message tag reaches a recipient, and the reply-to address carries no
    per-execution component (ADR-026 § Context).
    """

    bare = message_id.strip().removeprefix("<").removesuffix(">")
    local, separator, _domain = bare.rpartition("@")
    return local if separator else bare


# ---------------------------------------------------------------------------------------
# Body extraction
# ---------------------------------------------------------------------------------------

_ALLOWED_CONTENT_TYPES = frozenset({"text/plain", "text/html"})
"""The only leaf media types a reply may contain. ``text/html`` is **never parsed**."""


def _refuse_attachments(message: Message) -> None:
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_disposition() == "attachment":
            raise InboundReplyRejected(InboundReplyRejection.REPLY_ATTACHMENT_PRESENT)
        if part.get_content_type().lower() not in _ALLOWED_CONTENT_TYPES:
            raise InboundReplyRejected(InboundReplyRejection.REPLY_ATTACHMENT_PRESENT)


def _plain_text(message: Message) -> str:
    """The whole part when the message is ``text/plain``, or the first ``text/plain`` part.

    ``text/html`` is never parsed. An HTML-to-text converter is a parser surface pointed at
    untrusted input, and adding one requires its own ADR stating what parser is being added and
    what it is being pointed at (ADR-026 § 4, Revisit condition).
    """

    for part in message.walk():
        if part.get_content_type().lower() != "text/plain":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    raise InboundReplyRejected(InboundReplyRejection.REPLY_NO_PLAIN_TEXT)


_MIN_REFLECTED_RUN_WORDS = 3
"""The shortest run of consecutive words that counts as reflected CHORUS content.

Three, because it is the smallest run a coincidental match is implausible for: two words --
``"the report"``, ``"by 2030"`` -- recur constantly in ordinary correspondence, and stripping on
that would delete arbitrary fragments from a genuine reply. Three consecutive words repeating our
own outbound text *in our own word order* is reflow, not coincidence.
"""


def _reflected_word_mask(reply_words: list[str], outbound_words: tuple[str, ...]) -> list[bool]:
    """Mark every reply word that is part of some run also found, in order, in ``outbound_words``.

    Matching runs across the whole flattened word sequence -- not line by line -- is what makes
    this robust to a mail client that rewrapped, split, or joined our lines: reflow changes where
    the newlines fall and never changes the order of the words themselves. A minimal maximal-run
    algorithm (the standard "longest common substrings" table, marking every run of at least
    :data:`_MIN_REFLECTED_RUN_WORDS`) is used rather than a single fixed window, because a short
    quoted fragment -- shorter than the window -- would otherwise survive whole.
    """

    reply_count, outbound_count = len(reply_words), len(outbound_words)
    mask = [False] * reply_count
    if outbound_count < _MIN_REFLECTED_RUN_WORDS:
        return mask
    previous_row = [0] * (outbound_count + 1)
    for i in range(1, reply_count + 1):
        current_row = [0] * (outbound_count + 1)
        for j in range(1, outbound_count + 1):
            if reply_words[i - 1] != outbound_words[j - 1]:
                continue
            run = previous_row[j - 1] + 1
            current_row[j] = run
            if run == _MIN_REFLECTED_RUN_WORDS:
                for k in range(i - run, i):
                    mask[k] = True
            elif run > _MIN_REFLECTED_RUN_WORDS:
                mask[i - 1] = True
        previous_row = current_row
    return mask


def strip_quoted_outbound(reply_text: str, outbound_text: str) -> str:
    """Delete every reply line that is quoted, or that reflects text we ourselves sent.

    **This is not tidiness.** Without it, a reply that carries our own message contains our own
    ``requested_deadline`` and our own claim text, and a model extracting a commitment could
    ground it against text *we* wrote and attribute the promise to management (T37). The outbound
    body is reconstructible from the immutable proposal, view, and template version -- so
    ingestion regenerates the very bytes the approval bound and needs no ``On ... wrote:``
    heuristic to find them.

    Detection runs on **words**, not lines. A mail client that rewrapped our message -- joined
    two of our lines, split one of ours into two, or merely refolded its whitespace -- produces
    text whose *lines* no longer equal anything we sent, but whose *word sequence*, read across
    the whole surviving message, still contains our words in our order. Matching there is what
    catches the reflow a line-equality check misses (Astra P1-2), without an embedding, a
    similarity score, or a second model call: two normalized words are either the same word or
    they are not.

    Quote-marked lines (``>``) are dropped outright, since a client-inserted quote marker is
    already an unambiguous signal with nothing left to preserve on that line. Every other line is
    kept, minus whatever word run within it the outbound message-order match identifies as
    reflected -- so a line that mixes a genuine sentence with a trailing echo of our own text
    keeps its genuine words rather than being discarded whole (ADR-026 § 4).

    The result is the normalized surviving words, rejoined one line per surviving reply line and
    the lines joined by newlines -- exactly the value persisted as ``extracted_text`` and exactly
    the value the extraction spans index into.
    """

    outbound_words = tuple(normalize(outbound_text).split())

    surviving_lines: list[list[str]] = []
    for line in reply_text.splitlines():
        if line.lstrip().startswith(">"):
            continue
        normalized = normalize(line)
        if not normalized:
            continue
        surviving_lines.append(normalized.split())

    flat_words = [word for line_words in surviving_lines for word in line_words]
    reflected = _reflected_word_mask(flat_words, outbound_words)

    kept: list[str] = []
    cursor = 0
    for line_words in surviving_lines:
        span = reflected[cursor : cursor + len(line_words)]
        cursor += len(line_words)
        survivors = [
            word for word, is_reflected in zip(line_words, span, strict=True) if not is_reflected
        ]
        if survivors:
            kept.append(" ".join(survivors))
    return "\n".join(kept)


# ---------------------------------------------------------------------------------------
# The attested artifact
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundReplyEvidence:
    """One decoded, correlated inbound reply, offered as an artifact about one execution.

    On its own this type is **not** trusted and nothing accepts it. It is the decoded and
    correlated payload; what ``IngestExternalReply`` takes is the attested wrapper below, which
    a caller cannot mint.
    """

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    execution_id: ExecutionId
    destination_id: str
    registry_version: int
    routing_token: Any
    inbound_message_id_hash: Sha256Digest
    sender_address_digest: Sha256Digest
    recipient_address_digest: Sha256Digest
    transport: str
    verdicts: ReceiptVerdicts
    received_at: datetime
    raw_sha256: Sha256Digest
    raw_byte_length: int
    extracted_text: str
    """The normalized, quote-stripped plain text. May be empty -- a reply consisting only of our
    own quoted message leaves nothing, and an empty text is an artifact no span can index into
    rather than a reason to refuse the record of a delivery that really happened."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AttestedInboundReply:
    """A correlated artifact plus the boundary's attestation that it came off an authentic wire.

    The attestation is an HMAC over the decoded and correlated fields *and* the ``source_arn``
    they arrived through, so it binds the artifact to one deployment's transport as well as to
    one observation. It carries ``raw_mime`` because the bytes have to reach the object store,
    and the bytes are bound by the ``raw_sha256`` that is inside the MAC.
    """

    evidence: InboundReplyEvidence
    source_arn: str
    attestation: str
    raw_mime: bytes

    def __post_init__(self) -> None:
        if not self.source_arn or not self.attestation:
            raise ValueError("an attested reply names its transport and carries an attestation")


def _attestation(key: bytes, evidence: InboundReplyEvidence, source_arn: str) -> str:
    """The MAC binding one correlated artifact to one deployment's inbound transport.

    Canonical JSON with sorted keys, so the bytes signed are a function of the values alone --
    the same discipline every other digest in this repository uses, for the same reason.
    """

    payload = json.dumps(
        {
            "action_id": str(evidence.action_id),
            "case_id": str(evidence.case_id),
            "community_id": str(evidence.community_id),
            "destination_id": evidence.destination_id,
            "execution_id": str(evidence.execution_id),
            "extracted_text_sha256": sha256(evidence.extracted_text.encode("utf-8")).hexdigest(),
            "inbound_message_id_hash": evidence.inbound_message_id_hash.value,
            "namespace": evidence.namespace.value,
            "raw_byte_length": evidence.raw_byte_length,
            "raw_sha256": evidence.raw_sha256.value,
            "received_at": evidence.received_at.isoformat(),
            "recipient_address_digest": evidence.recipient_address_digest.value,
            "registry_version": evidence.registry_version,
            "routing_token": str(evidence.routing_token),
            "sender_address_digest": evidence.sender_address_digest.value,
            "source_arn": source_arn,
            "transport": evidence.transport,
            "verdicts": [
                evidence.verdicts.spf,
                evidence.verdicts.dkim,
                evidence.verdicts.dmarc,
                evidence.verdicts.spam,
                evidence.verdicts.virus,
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(key, payload, sha256).hexdigest()


class InboundMailEvidenceVerifier:
    """The checking half. ``IngestExternalReply`` holds one and can do nothing else with it.

    It cannot mint, so a command that holds the verifier -- or a caller who reaches the command
    -- gains no way to manufacture a reply. That asymmetry is the whole mechanism.
    """

    __slots__ = ("_key", "_source_arn")

    def __init__(self, *, key: bytes, source_arn: str) -> None:
        self._key = key
        self._source_arn = source_arn

    def attests(self, attested: AttestedInboundReply) -> bool:
        """True only for an artifact this boundary's attester minted, off this deployment's wire.

        The raw bytes are re-digested rather than trusted: they travel beside the artifact and
        are not themselves in the MAC, so the digest inside it is what makes them checkable.
        """

        if attested.source_arn != self._source_arn:
            return False
        digest = f"sha256:{sha256(attested.raw_mime).hexdigest()}"
        if digest != attested.evidence.raw_sha256.value:
            return False
        if len(attested.raw_mime) != attested.evidence.raw_byte_length:
            return False
        expected = _attestation(self._key, attested.evidence, attested.source_arn)
        return hmac.compare_digest(expected, attested.attestation)


class InboundMailAttester:
    """The minting half. Only the inbound entry point is given one.

    Four things have to hold before an envelope is even read: an authenticator must exist, the
    delivery must have come through the mechanism and resource this deployment configured, the
    authenticator must recognise it, and every receipt verdict must pass. Only then is the
    envelope decoded, only then is the reply correlated, and only then is an attestation issued.
    """

    __slots__ = (
        "_authenticator",
        "_config",
        "_core",
        "_from_identity_id",
        "_key",
        "_namespace",
        "_raw_messages",
        "_shareable",
        "_source_arn",
        "_template_version",
        "_transport",
    )

    def __init__(
        self,
        *,
        key: bytes,
        transport: str,
        source_arn: str,
        authenticator: InboundMailTransportAuthenticator | None,
        raw_messages: InboundRawMessageReader,
        core: CoreRepositoryPort,
        shareable: ShareableRepositoryPort,
        config: SafeInboundMailConfiguration,
        namespace: Namespace,
        from_identity_id: str,
        template_version: str = TEMPLATE_VERSION,
    ) -> None:
        self._key = key
        self._transport = transport
        self._source_arn = source_arn
        self._authenticator = authenticator
        self._raw_messages = raw_messages
        self._core = core
        self._shareable = shareable
        self._config = config
        self._namespace = namespace
        self._from_identity_id = from_identity_id
        self._template_version = template_version

    async def attest(self, context: InboundMailTransportContext) -> AttestedInboundReply:
        """Authenticate, gate on verdicts, decode, correlate, attest -- in that order, always."""

        if self._authenticator is None:
            raise InboundMailUntrusted(InboundMailTrustFailure.TRANSPORT_UNAVAILABLE)
        if context.transport != self._transport or context.source_arn != self._source_arn:
            raise InboundMailUntrusted(InboundMailTrustFailure.FOREIGN_TRANSPORT_SOURCE)
        if not await self._authenticator.authenticate(context):
            raise InboundMailUntrusted(InboundMailTrustFailure.TRANSPORT_UNAUTHENTICATED)

        receipt = decode_receipt_envelope(context.envelope)
        if not receipt.verdicts.accepted:
            raise InboundMailUntrusted(InboundMailTrustFailure.INBOUND_VERDICT_FAILED)
        if receipt.headers_truncated:
            # A truncated header set may have dropped the ``References`` this correlation
            # depends on, so what is present cannot be read as the whole of what was sent.
            raise InboundReplyRejected(InboundReplyRejection.REPLY_HEADERS_TRUNCATED)

        raw = await self._read_raw(receipt)
        message = message_from_bytes(raw, policy=compat32)
        _refuse_attachments(message)
        reply_text = _plain_text(message)

        locator = await self._correlate(receipt)
        outbound_text = await self._regenerate_outbound(locator)
        extracted = strip_quoted_outbound(reply_text, outbound_text)
        if len(extracted.encode("utf-8")) > MAX_EXTRACTED_TEXT_BYTES:
            raise InboundReplyRejected(InboundReplyRejection.REPLY_TOO_LARGE)

        evidence = InboundReplyEvidence(
            namespace=locator.namespace,
            community_id=locator.community_id,
            case_id=locator.case_id,
            action_id=locator.action_id,
            execution_id=locator.execution_id,
            destination_id=str(locator.destination_id),
            registry_version=locator.registry_version,
            routing_token=locator.routing_token,
            inbound_message_id_hash=Sha256Digest(
                f"sha256:{sha256(receipt.message_id.encode('utf-8')).hexdigest()}"
            ),
            sender_address_digest=address_digest(locator.namespace, receipt.source),
            recipient_address_digest=address_digest(locator.namespace, receipt.destination),
            transport=context.transport,
            verdicts=receipt.verdicts,
            received_at=receipt.received_at,
            raw_sha256=Sha256Digest(f"sha256:{sha256(raw).hexdigest()}"),
            raw_byte_length=len(raw),
            extracted_text=extracted,
        )
        return AttestedInboundReply(
            evidence=evidence,
            source_arn=context.source_arn,
            attestation=_attestation(self._key, evidence, context.source_arn),
            raw_mime=raw,
        )

    # -- the pieces --------------------------------------------------------------------

    async def _read_raw(self, receipt: DecodedReceipt) -> bytes:
        try:
            raw = await self._raw_messages.read(bucket=receipt.bucket_name, key=receipt.object_key)
        except NotFoundError as error:
            raise InboundReplyRejected(InboundReplyRejection.MALFORMED_ENVELOPE) from error
        if len(raw) > MAX_INBOUND_REPLY_BYTES:
            # Refused whole, and the bytes are not retained. A reply larger than the frozen cap
            # is not truncated to fit, because a message shortened to fit is not the message
            # anybody sent.
            raise InboundReplyRejected(InboundReplyRejection.REPLY_TOO_LARGE)
        return raw

    async def _correlate(self, receipt: DecodedReceipt) -> OutboundMessageLocator:
        """Resolve the thread references to exactly one locator, then check the five agreements."""

        references = receipt.thread_references
        if not references:
            # A manager who composes a fresh message rather than replying produces no
            # ``In-Reply-To``, correlates to nothing, and creates no commitment. That is an
            # availability cost paid deliberately for the property that nothing attaches to a
            # case without proof (ADR-026 § Residual risk).
            raise InboundReplyRejected(InboundReplyRejection.REPLY_UNCORRELATED)

        # The namespace is the deployment's own and is never read out of a delivery: an
        # envelope that could name its namespace could reach another tenant's rows. It is also
        # inside every locator digest, so even a hand-assembled key cannot cross the boundary.
        candidates = tuple(message_id_local_part(value) for value in references)
        locators = await self._shareable.load_outbound_message_locators(
            NamespaceScope(namespace=self._namespace), candidates
        )
        if not locators:
            raise InboundReplyRejected(InboundReplyRejection.REPLY_UNCORRELATED)
        distinct = {locator.execution_id for locator in locators}
        if len(distinct) != 1:
            raise InboundReplyRejected(InboundReplyRejection.REPLY_CORRELATION_AMBIGUOUS)
        locator = locators[0]

        await self._require_agreements(locator, receipt)
        return locator

    async def _require_agreements(
        self, locator: OutboundMessageLocator, receipt: DecodedReceipt
    ) -> None:
        """The five agreements of ADR-026 § 3. All must hold or the reply is refused whole."""

        action_scope = ActionScope(
            namespace=locator.namespace,
            community_id=locator.community_id,
            case_id=locator.case_id,
            action_id=locator.action_id,
        )
        # Agreement 1. Each load revalidates the stored envelope, the decoded identity, and the
        # address the row claims for itself against the scope it was asked for, so a locator
        # naming a foreign case raises ``CrossCaseViolationError`` here rather than producing an
        # artifact that names a case it did not prove it belongs to.
        execution = await self._shareable.load_execution(action_scope, locator.execution_id)
        case = await self._core.load_case(action_scope.case_scope)

        # Agreement 2.
        if execution.state is not ActionExecutionState.SENT:
            raise InboundReplyRejected(InboundReplyRejection.REPLY_EXECUTION_NOT_SENT)
        if execution.ses_message_id != locator.ses_message_id:
            raise InboundReplyRejected(InboundReplyRejection.REPLY_MESSAGE_ID_MISMATCH)

        # Agreement 3. A terminal case is reopened by an explicit human command and by nothing
        # that arrives on a wire -- the same rule the Monitor obeys.
        if case.state in {CaseState.RESOLVED, CaseState.CLOSED_UNRESOLVED}:
            raise InboundReplyRejected(InboundReplyRejection.REPLY_CASE_TERMINAL)
        if case.state not in {CaseState.ACTIONED, CaseState.VERIFYING}:
            raise InboundReplyRejected(InboundReplyRejection.REPLY_CASE_NOT_ACTIONED)

        # Agreements 4 and 5, made against digests so no address reaches this principal.
        destination = self._config.destination
        if (
            str(locator.destination_id) != str(destination.destination_id)
            or locator.registry_version != destination.registry_version
            or locator.routing_token != destination.routing_token
        ):
            raise InboundReplyRejected(InboundReplyRejection.REPLY_SENDER_NOT_DESTINATION)
        sender = address_digest(locator.namespace, receipt.source)
        if not hmac.compare_digest(sender.value, self._config.destination_address_digest.value):
            raise InboundReplyRejected(InboundReplyRejection.REPLY_SENDER_NOT_DESTINATION)
        recipient = address_digest(locator.namespace, receipt.destination)
        if not hmac.compare_digest(recipient.value, self._config.inbound_address_digest.value):
            raise InboundReplyRejected(InboundReplyRejection.REPLY_RECIPIENT_NOT_OURS)

    async def _regenerate_outbound(self, locator: OutboundMessageLocator) -> str:
        """Rebuild the exact outbound plain-text body this reply is answering.

        A pure function of the immutable proposal, its bound view, the template version, and the
        sending identity handle -- the same four inputs the human preview was rendered from
        (ADR-022 § 3), so the bytes regenerated here are the bytes the approval bound.
        """

        action_scope = ActionScope(
            namespace=locator.namespace,
            community_id=locator.community_id,
            case_id=locator.case_id,
            action_id=locator.action_id,
        )
        proposal = await self._shareable.load_proposal(action_scope)
        view = await self._shareable.load_view(action_scope.case_scope, proposal.view_id)
        rendered = render_preview(
            proposal,
            view,
            from_identity_id=self._from_identity_id,
            template_version=self._template_version,
        )
        return rendered.text_body


def inbound_mail_trust_boundary(
    *,
    transport: str,
    source_arn: str,
    authenticator: InboundMailTransportAuthenticator | None,
    raw_messages: InboundRawMessageReader,
    core: CoreRepositoryPort,
    shareable: ShareableRepositoryPort,
    config: SafeInboundMailConfiguration,
    namespace: Namespace,
    from_identity_id: str,
    template_version: str = TEMPLATE_VERSION,
) -> tuple[InboundMailAttester, InboundMailEvidenceVerifier]:
    """Build one boundary as two halves over one process-local key.

    The key is generated here and handed to nobody: it exists only inside the pair, so the only
    way to obtain a verifiable attestation is to hold the attester -- which composition gives to
    the inbound entry point and to nothing else. It is deliberately **not** configuration. A
    secret that lived in an environment variable would be a secret an operator could copy into a
    request, and the point of this value is that it never leaves the process that ingests.
    """

    key = secrets.token_bytes(32)
    attester = InboundMailAttester(
        key=key,
        transport=transport,
        source_arn=source_arn,
        authenticator=authenticator,
        raw_messages=raw_messages,
        core=core,
        shareable=shareable,
        config=config,
        namespace=namespace,
        from_identity_id=from_identity_id,
        template_version=template_version,
    )
    return attester, InboundMailEvidenceVerifier(key=key, source_arn=source_arn)


__all__ = [
    "INBOUND_PARTY_DIGEST_SCHEMA",
    "MAX_EXTRACTED_TEXT_BYTES",
    "MAX_MESSAGE_IDS",
    "AttestedInboundReply",
    "DecodedReceipt",
    "InboundMailAttester",
    "InboundMailEvidenceVerifier",
    "InboundMailTrustFailure",
    "InboundMailUntrusted",
    "InboundRawMessageReader",
    "InboundReplyEvidence",
    "InboundReplyRejected",
    "InboundReplyRejection",
    "ReceiptVerdicts",
    "address_digest",
    "decode_receipt_envelope",
    "inbound_mail_trust_boundary",
    "message_id_local_part",
    "strip_quoted_outbound",
]
