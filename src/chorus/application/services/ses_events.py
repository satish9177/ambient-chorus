"""Turn one authenticated SES configuration-set event into evidence, or refuse it.

Why this module exists rather than an HTTP body
------------------------------------------------
``SEND_UNKNOWN -> SENT`` requires an SES message identifier, and a message identifier is a value
**only SES can produce**. An endpoint that accepted ``{configuration_set, execution_tag,
message_id}`` from a caller would therefore be an endpoint through which anybody holding the
demo token could resolve a quarantine by typing three strings -- which is T35 with the attacker
handed the pen. There is no such endpoint and there must not be one.

Structure is not provenance, and this module keeps the two apart
-----------------------------------------------------------------
Decoding is the *cheap* half. :func:`decode_configuration_set_event` proves that a mapping has
the shape SES publishes and that its fields correlate with one execution; it cannot prove that
SES is where the mapping came from, because a well-formed dictionary is something anybody can
type. A repair pass found exactly that gap: a caller-built ``Delivery`` envelope carrying an
invented ``mail.messageId`` decoded cleanly, correlated cleanly, and moved a quarantined row to
``SENT`` under the invented identifier.

The boundary therefore has two halves that are never the same object:

* :class:`SesEventAttester` -- held **only** by the event adapter. It requires an authenticated
  transport (:mod:`chorus.ports.ses_events`), requires the delivery to have arrived through the
  deployment's own event destination, decodes the envelope, and mints
  :class:`AttestedSesEventEvidence`.
* :class:`SesEventEvidenceVerifier` -- held by ``ReconcileSendOutcome``. It can only *check*.

The two share a process-local attestation key that neither the API, the worker, nor any command
argument can reach, and the attestation is an HMAC over the decoded fields. So a hand-built
:class:`SesEventEvidence`, a hand-built :class:`AttestedSesEventEvidence`, a replayed
attestation from another deployment's ARN, and a decoded payload that never crossed the adapter
are all the same thing to the verifier: not evidence. The check is a MAC comparison rather than
a naming convention on purpose --- a leading underscore is a documentation style, not a boundary.

What "trusted evidence" means, exactly
---------------------------------------
Evidence is a **configuration-set event notification delivered by SES itself**, through the
event destination the deployment configures. Every field of :class:`SesEventEvidence` is read
out of that envelope and none of it is caller-supplied:

* the configuration set comes from the event's own ``ses:configuration-set`` tag, never from a
  parameter;
* the execution tag comes from the event's ``chorus_execution`` tag, and is then required to
  equal the recomputed ADR-025 SS 7 derivation for the execution being reconciled;
* the message identifier comes from ``mail.messageId``;
* acceptance comes from the enumerated ``eventType``, not from a boolean anybody passed in.

Phase 8 owns this boundary and Phase 11 owns the subscription that feeds it -- an SNS topic or
EventBridge rule attached to the ``chorus-{environment}`` configuration set, with the same
static-now, live-in-Phase-11 split every other AWS resource in this phase uses. The boundary is
the part that has to be right; the plumbing is the part that has to be deployed.

Concretely, **Phase 11 owes exactly one object**: a
:class:`chorus.ports.ses_events.SesEventTransportAuthenticator` over the transport it builds.
Until it exists there is no authenticator to construct an attester with, no attester to mint
evidence, and no verifier on the command -- and a ``SEND_UNKNOWN`` row therefore stays
``SEND_UNKNOWN``, which is what ADR-025 SS 9 asks for.

Which event types are evidence, and which are deliberately not
---------------------------------------------------------------
``Send`` and ``Delivery`` prove SES accepted the message: both carry the identifier SES issued
when it took it. ``Rendering Failure`` proves the opposite -- SES never queued anything.

Every other type -- ``Bounce``, ``Complaint``, ``Reject``, ``DeliveryDelay``, ``Open``,
``Click``, ``Subscription`` -- is refused here rather than interpreted. Several of them do imply
acceptance, and that is exactly why enumerating them is a decision somebody should make on
purpose with the reason written down, not a default this module drifts into. Until then the
quarantine stays a quarantine, which ADR-025 SS 9 already names as the correct outcome:
uncertainty remains unknown rather than being resolved by anything short of proof.
"""

from __future__ import annotations

import hmac
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any

from chorus.ports.ses_events import SesEventTransportAuthenticator, SesEventTransportContext

CONFIGURATION_SET_TAG = "ses:configuration-set"
EXECUTION_TAG_NAME = "chorus_execution"

ACCEPTANCE_EVENT_TYPES = frozenset({"Send", "Delivery"})
"""Event types that prove SES took the message and issued an identifier for it."""

NON_ACCEPTANCE_EVENT_TYPES = frozenset({"Rendering Failure"})
"""Event types that prove SES never queued anything."""


class SesEventRejection(StrEnum):
    """Why an envelope was not usable as evidence. Closed codes; never the envelope's content."""

    MALFORMED_ENVELOPE = "MALFORMED_ENVELOPE"
    UNINTERPRETED_EVENT_TYPE = "UNINTERPRETED_EVENT_TYPE"
    MISSING_EXECUTION_TAG = "MISSING_EXECUTION_TAG"
    MISSING_CONFIGURATION_SET = "MISSING_CONFIGURATION_SET"
    MISSING_MESSAGE_ID = "MISSING_MESSAGE_ID"


class SesEventRejected(Exception):
    """This envelope is not evidence about anything. It carries a closed code and no content.

    Deliberately not a ``ReconciliationRefusedError``: that type means "the reconciliation
    declined to move a row", and this means "there was never a usable observation here". Two
    different facts, and collapsing them would let a malformed envelope read as a considered
    refusal in an audit trail.
    """

    __slots__ = ("rejection",)

    def __init__(self, rejection: SesEventRejection) -> None:
        super().__init__(rejection.value)
        self.rejection = rejection

    @property
    def safe_code(self) -> str:
        return self.rejection.value


def _first_tag(tags: Mapping[str, Any], name: str) -> str | None:
    """SES publishes each tag as a list of values. Exactly one is expected; more is not evidence."""

    values = tags.get(name)
    if isinstance(values, str):
        return values or None
    if isinstance(values, list) and len(values) == 1 and isinstance(values[0], str):
        return values[0] or None
    return None


def decode_configuration_set_event(
    envelope: Mapping[str, Any],
) -> tuple[str, str, str | None, bool]:
    """Read ``(configuration_set, execution_tag, message_id, accepted)`` out of one event.

    Nothing here is defaulted and nothing is inferred. A field that is missing or shaped
    differently than SES publishes it makes the whole envelope unusable, because an envelope
    this module had to guess about is an envelope somebody could have constructed.
    """

    event_type = envelope.get("eventType")
    mail = envelope.get("mail")
    if not isinstance(event_type, str) or not isinstance(mail, dict):
        raise SesEventRejected(SesEventRejection.MALFORMED_ENVELOPE)
    tags = mail.get("tags")
    if not isinstance(tags, dict):
        raise SesEventRejected(SesEventRejection.MALFORMED_ENVELOPE)

    if event_type in ACCEPTANCE_EVENT_TYPES:
        accepted = True
    elif event_type in NON_ACCEPTANCE_EVENT_TYPES:
        accepted = False
    else:
        raise SesEventRejected(SesEventRejection.UNINTERPRETED_EVENT_TYPE)

    configuration_set = _first_tag(tags, CONFIGURATION_SET_TAG)
    if configuration_set is None:
        raise SesEventRejected(SesEventRejection.MISSING_CONFIGURATION_SET)
    execution_tag = _first_tag(tags, EXECUTION_TAG_NAME)
    if execution_tag is None:
        raise SesEventRejected(SesEventRejection.MISSING_EXECUTION_TAG)

    message_id = mail.get("messageId")
    identifier = message_id if isinstance(message_id, str) and message_id else None
    if accepted and identifier is None:
        # An acceptance event with no identifier is not an acceptance anybody can act on, and
        # ``SEND_UNKNOWN -> SENT`` has nothing to record without one.
        raise SesEventRejected(SesEventRejection.MISSING_MESSAGE_ID)
    return configuration_set, execution_tag, identifier, accepted


@dataclass(frozen=True, slots=True, kw_only=True)
class SesEventEvidence:
    """One configuration-set event, decoded, offered as proof about one execution.

    It carries the three things proof requires and nothing that could stand in for them. There
    is no "trusted" flag and no free-text note: an operator attestation is a separate field with
    its own reason code, so a claim made by a person can never be mistaken for one made by SES.

    On its own this type is **not** trusted and nothing accepts it. It is the decoded payload,
    and a decoded payload is only a shape; what ``ReconcileSendOutcome`` takes is the attested
    wrapper below, which a caller cannot mint.
    """

    configuration_set: str
    execution_tag: str
    message_id: str | None
    accepted: bool
    """Whether the event says SES accepted the message. ``False`` is proof of the opposite."""

    def __post_init__(self) -> None:
        if not self.configuration_set or not self.execution_tag:
            raise ValueError("an SES event names its configuration set and execution tag")
        if self.accepted and not self.message_id:
            raise ValueError("an acceptance event carries a message identifier")


@dataclass(frozen=True, slots=True, kw_only=True)
class AttestedSesEventEvidence:
    """Decoded evidence, plus the adapter's attestation that it came off an authenticated wire.

    The attestation is an HMAC over the decoded fields *and* the event-destination ARN they
    arrived through, so it binds the evidence to one deployment's transport as well as to one
    observation. Constructing this class is possible -- Python has no private constructors, and
    pretending otherwise would be the security theatre this repair exists to remove -- and
    constructing it is useless, because the attestation on a hand-built instance is a string no
    verifier will reproduce.
    """

    evidence: SesEventEvidence
    source_arn: str
    attestation: str

    def __post_init__(self) -> None:
        if not self.source_arn or not self.attestation:
            raise ValueError("attested evidence names its transport and carries an attestation")


class SesEventTrustFailure(StrEnum):
    """Why a delivery never became evidence. Closed codes; never the delivery's content."""

    TRANSPORT_UNAVAILABLE = "TRANSPORT_UNAVAILABLE"
    """No authenticator is wired, so nothing here can establish origin. Phase 11 owes one."""

    TRANSPORT_UNAUTHENTICATED = "TRANSPORT_UNAUTHENTICATED"
    """The authenticator looked at this delivery and did not recognise it as SES's."""

    FOREIGN_TRANSPORT_SOURCE = "FOREIGN_TRANSPORT_SOURCE"
    """Authentic, perhaps -- but carried by a mechanism or resource this deployment never used."""


class SesEventUntrusted(Exception):
    """This delivery is not evidence about anything, and it never reached the decoder.

    Deliberately distinct from :class:`SesEventRejected`: that one means "the envelope was
    unusable", and this means "there was never a reason to read the envelope". An operator
    seeing the two collapsed into one code could not tell a malformed notification from an
    unauthenticated one, and only the second is somebody probing the boundary.
    """

    __slots__ = ("failure",)

    def __init__(self, failure: SesEventTrustFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure

    @property
    def safe_code(self) -> str:
        return self.failure.value


def _attestation(key: bytes, evidence: SesEventEvidence, source_arn: str) -> str:
    """The MAC binding one decoded observation to one deployment's event destination.

    Canonical JSON with sorted keys, so the bytes signed are a function of the values alone --
    the same discipline every other digest in this repository uses, for the same reason.
    """

    payload = json.dumps(
        {
            "accepted": evidence.accepted,
            "configuration_set": evidence.configuration_set,
            "execution_tag": evidence.execution_tag,
            "message_id": evidence.message_id,
            "source_arn": source_arn,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(key, payload, sha256).hexdigest()


class SesEventAttester:
    """The minting half of the boundary. Only the SES event adapter is given one.

    Three things have to hold before an envelope is even read: an authenticator must exist, the
    delivery must have come through the mechanism and the event-destination resource this
    deployment configured, and the authenticator must recognise it. Only then is the envelope
    decoded, and only then is an attestation issued.
    """

    __slots__ = ("_authenticator", "_key", "_source_arn", "_transport")

    def __init__(
        self,
        *,
        key: bytes,
        transport: str,
        source_arn: str,
        authenticator: SesEventTransportAuthenticator | None,
    ) -> None:
        self._key = key
        self._transport = transport
        self._source_arn = source_arn
        self._authenticator = authenticator

    async def attest(self, context: SesEventTransportContext) -> AttestedSesEventEvidence:
        """Authenticate the transport, then decode, then attest -- and never the other way round.

        The order is the point. Decoding first would mean a forged envelope had already been
        interpreted by the time anybody asked where it came from, and every field in that
        interpretation would be the forger's.
        """

        if self._authenticator is None:
            raise SesEventUntrusted(SesEventTrustFailure.TRANSPORT_UNAVAILABLE)
        if context.transport != self._transport or context.source_arn != self._source_arn:
            raise SesEventUntrusted(SesEventTrustFailure.FOREIGN_TRANSPORT_SOURCE)
        if not await self._authenticator.authenticate(context):
            raise SesEventUntrusted(SesEventTrustFailure.TRANSPORT_UNAUTHENTICATED)

        configuration_set, execution_tag, message_id, accepted = decode_configuration_set_event(
            context.envelope
        )
        evidence = SesEventEvidence(
            configuration_set=configuration_set,
            execution_tag=execution_tag,
            message_id=message_id,
            accepted=accepted,
        )
        return AttestedSesEventEvidence(
            evidence=evidence,
            source_arn=context.source_arn,
            attestation=_attestation(self._key, evidence, context.source_arn),
        )


class SesEventEvidenceVerifier:
    """The checking half. ``ReconcileSendOutcome`` holds one and can do nothing else with it.

    It cannot mint, so a command that holds the verifier -- or a caller who reaches the command
    -- gains no way to manufacture an acceptance. That asymmetry is the whole mechanism.
    """

    __slots__ = ("_key", "_source_arn")

    def __init__(self, *, key: bytes, source_arn: str) -> None:
        self._key = key
        self._source_arn = source_arn

    def attests(self, attested: AttestedSesEventEvidence) -> bool:
        """True only for evidence this boundary's attester minted, off this deployment's wire."""

        if attested.source_arn != self._source_arn:
            return False
        expected = _attestation(self._key, attested.evidence, attested.source_arn)
        return hmac.compare_digest(expected, attested.attestation)


def ses_event_trust_boundary(
    *,
    transport: str,
    source_arn: str,
    authenticator: SesEventTransportAuthenticator | None,
) -> tuple[SesEventAttester, SesEventEvidenceVerifier]:
    """Build one boundary as two halves over one process-local key.

    The key is generated here and handed to nobody: it exists only inside the pair, so the only
    way to obtain a verifiable attestation is to hold the attester -- which composition gives to
    the event adapter and to nothing else. It is deliberately **not** configuration. A secret
    that lived in an environment variable would be a secret an operator could copy into a
    request, and the point of this value is that it never leaves the process that reconciles.
    """

    key = secrets.token_bytes(32)
    return (
        SesEventAttester(
            key=key, transport=transport, source_arn=source_arn, authenticator=authenticator
        ),
        SesEventEvidenceVerifier(key=key, source_arn=source_arn),
    )


__all__ = [
    "ACCEPTANCE_EVENT_TYPES",
    "CONFIGURATION_SET_TAG",
    "EXECUTION_TAG_NAME",
    "NON_ACCEPTANCE_EVENT_TYPES",
    "AttestedSesEventEvidence",
    "SesEventAttester",
    "SesEventEvidence",
    "SesEventEvidenceVerifier",
    "SesEventRejected",
    "SesEventRejection",
    "SesEventTrustFailure",
    "SesEventUntrusted",
    "decode_configuration_set_event",
    "ses_event_trust_boundary",
]
