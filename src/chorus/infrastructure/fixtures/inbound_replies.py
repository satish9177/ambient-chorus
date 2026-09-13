"""The reviewed RFC 822 replies the demo route may select, and nothing a caller may compose.

``POST /v1/demo/external-replies`` takes a **fixture selector** and nothing else. The route reads
no case, action, destination, sender, subject, or body from the caller, because a caller-supplied
reply is not a reply ([ADR-026](../../../../docs/adr/ADR-026-inbound-reply-trust-and-correlation.md)
§ Alternatives). What the selector names is one of the messages below -- reviewed, checked in,
and diffable -- and the fixture is fed through the *same* attester a deployed delivery uses, over
the local authenticator. The demo exercises the trust boundary instead of bypassing it.

Two things about a fixture are still not the fixture's to choose. Its ``In-Reply-To`` is filled
in from the **actual** ``ses_message_id`` of the execution the demo just sent, because that is
the correlation channel and a fixture that carried its own would correlate to nothing; and its
addresses come from the deployment's local configuration, because the digests they have to match
are the deployment's. Everything else -- the body, the subject, the structure -- is exactly what
was reviewed.

The staged demo reply states an explicit ISO date. ADR-021 § 6 rejects weekday and relative date
constructs outright and ADR-027 § 4 will not let a model turn "Wednesday" into an instant, so
"Technician scheduled Wednesday 10-12" would produce no commitment at all. The demo continues to
show a real schedule; it stops showing a date the system invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from chorus.domain.time import format_utc

INBOUND_FIXTURE_BUCKET: Final = "chorus-local-inbound-fixtures"
"""The bucket name the local receipt envelope names. No such bucket exists; Phase 11 builds one."""

MANAGER_PROMISE = "manager-promise"
MANAGER_HEDGE = "manager-hedge"
MANAGER_WEEKDAY = "manager-weekday"
MANAGER_QUOTE_ONLY = "manager-quote-only"
MANAGER_HTML_ONLY = "manager-html-only"
MANAGER_ATTACHMENT = "manager-attachment"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedInboundReply:
    """One reviewed message: a subject, a plain-text body, and how it is structured.

    ``html_body`` and ``attachment`` exist so the refusal branches have a reviewed input too. A
    fixture that could only produce acceptable messages would leave four of the thirteen closed
    refusal codes provable only by a hand-built envelope, which is the thing this file exists to
    avoid.
    """

    fixture_id: str
    subject: str
    text_body: str | None
    html_body: str | None = None
    attachment: bool = False
    description: str = ""


REVIEWED_REPLIES: Final[dict[str, ReviewedInboundReply]] = {
    MANAGER_PROMISE: ReviewedInboundReply(
        fixture_id=MANAGER_PROMISE,
        subject="Re: Elevator service request",
        text_body=(
            "Thank you for the report.\n"
            "We will restore elevator B to service by 2030-01-14.\n"
            "Please contact the office with any further concerns.\n"
        ),
        description="The staged demo reply: unconditional, single-obligor, explicit ISO date.",
    ),
    MANAGER_HEDGE: ReviewedInboundReply(
        fixture_id=MANAGER_HEDGE,
        subject="Re: Elevator service request",
        text_body=(
            "Thank you for the report.\n"
            "We will look into elevator B and may have an update by 2030-01-14.\n"
        ),
        description="Hedged: fails the unconditionality check on 'look' and on 'may'.",
    ),
    MANAGER_WEEKDAY: ReviewedInboundReply(
        fixture_id=MANAGER_WEEKDAY,
        subject="Re: Elevator service request",
        text_body="Thank you for the report.\nTechnician scheduled Wednesday 10-12.\n",
        description="The old demo text: a weekday is not a deadline, so no commitment follows.",
    ),
    MANAGER_QUOTE_ONLY: ReviewedInboundReply(
        fixture_id=MANAGER_QUOTE_ONLY,
        subject="Re: Elevator service request",
        text_body=None,
        description="Nothing but our own quoted message: every line is removed before extraction.",
    ),
    MANAGER_HTML_ONLY: ReviewedInboundReply(
        fixture_id=MANAGER_HTML_ONLY,
        subject="Re: Elevator service request",
        text_body=None,
        html_body="<p>We will restore elevator B by 2030-01-14.</p>",
        description="HTML only: never parsed, so REPLY_NO_PLAIN_TEXT.",
    ),
    MANAGER_ATTACHMENT: ReviewedInboundReply(
        fixture_id=MANAGER_ATTACHMENT,
        subject="Re: Elevator service request",
        text_body="We will restore elevator B to service by 2030-01-14.\n",
        attachment=True,
        description="Carries an attachment: refused whole, and no metadata about it is kept.",
    ),
}


def reviewed_reply(fixture_id: str) -> ReviewedInboundReply:
    """Resolve one selector, or refuse. A selector is a key, never a path and never content."""

    reply = REVIEWED_REPLIES.get(fixture_id)
    if reply is None:
        raise KeyError("unknown inbound reply fixture")
    return reply


def build_raw_message(
    reply: ReviewedInboundReply,
    *,
    message_id: str,
    in_reply_to: str,
    from_address: str,
    to_address: str,
    quoted_outbound: str | None = None,
) -> bytes:
    """Assemble the raw MIME for one reviewed reply.

    Written by hand rather than through ``email.message.EmailMessage`` so the bytes a test
    reasons about are the bytes in this file. The parser under test is the one in the attester;
    a builder that used the same library to compose would be testing that library against
    itself.
    """

    headers = [
        f"Message-ID: {message_id}",
        f"In-Reply-To: {in_reply_to}",
        f"References: {in_reply_to}",
        f"From: {from_address}",
        f"To: {to_address}",
        f"Subject: {reply.subject}",
        "MIME-Version: 1.0",
    ]
    text = reply.text_body or ""
    if quoted_outbound:
        quoted = "\n".join(f"> {line}" for line in quoted_outbound.splitlines())
        text = f"{text}\n{quoted}\n" if text else f"{quoted}\n"

    if reply.attachment:
        boundary = "chorus-fixture-mixed"
        headers.append(f'Content-Type: multipart/mixed; boundary="{boundary}"')
        body = (
            f"--{boundary}\n"
            "Content-Type: text/plain; charset=utf-8\n\n"
            f"{text}\n"
            f"--{boundary}\n"
            'Content-Type: application/pdf; name="schedule.pdf"\n'
            'Content-Disposition: attachment; filename="schedule.pdf"\n\n'
            "%PDF-1.4 reviewed placeholder\n"
            f"--{boundary}--\n"
        )
    elif reply.html_body is not None and reply.text_body is None:
        headers.append("Content-Type: text/html; charset=utf-8")
        body = f"{reply.html_body}\n"
    else:
        headers.append("Content-Type: text/plain; charset=utf-8")
        body = text
    return ("\n".join(headers) + "\n\n" + body).encode("utf-8")


def build_receipt_envelope(
    *,
    message_id: str,
    in_reply_to: str,
    subject: str,
    source: str,
    destination: str,
    received_at: datetime,
    object_key: str,
    bucket_name: str = INBOUND_FIXTURE_BUCKET,
    spf: str = "PASS",
    dkim: str = "PASS",
    dmarc: str = "PASS",
    spam: str = "PASS",
    virus: str = "PASS",
    headers_truncated: bool = False,
    recipients: list[str] | None = None,
    from_header: list[str] | None = None,
) -> dict[str, Any]:
    """The shape SES publishes for a received message, verdicts and receipt action included.

    Every verdict is a parameter so the gate can be exercised in both directions. A fixture that
    could only produce ``PASS`` would leave ``INBOUND_VERDICT_FAILED`` untested, and that gate is
    the one that turns "who wrote this" from a header into a fact.
    """

    effective_from = from_header or [source]
    effective_recipients = recipients or [destination]

    return {
        "notificationType": "Received",
        "mail": {
            "timestamp": format_utc(received_at),
            "source": source,
            "messageId": message_id.strip("<>"),
            "destination": [destination],
            "headersTruncated": headers_truncated,
            "headers": [
                {"name": "Message-ID", "value": message_id},
                {"name": "In-Reply-To", "value": in_reply_to},
                {"name": "References", "value": in_reply_to},
                {"name": "From", "value": effective_from[0]},
                {"name": "To", "value": destination},
                {"name": "Subject", "value": subject},
            ],
            "commonHeaders": {
                "messageId": message_id,
                "inReplyTo": in_reply_to,
                "references": [in_reply_to],
                "subject": subject,
                "from": effective_from,
                "to": [destination],
            },
        },
        "receipt": {
            "recipients": effective_recipients,
            "spfVerdict": {"status": spf},
            "dkimVerdict": {"status": dkim},
            "dmarcVerdict": {"status": dmarc},
            "spamVerdict": {"status": spam},
            "virusVerdict": {"status": virus},
            "action": {
                "type": "S3",
                "bucketName": bucket_name,
                "objectKey": object_key,
            },
        },
    }


def outbound_message_id(ses_message_id: str, *, domain: str = "us-east-1.amazonses.com") -> str:
    """The ``Message-ID`` SES gives an outbound send: ``<{messageId}@{region}.amazonses.com>``.

    This is the whole correlation channel. SES composes the header itself -- ``Content.Simple``
    means the sender never builds one -- so the local part of a correspondent's ``In-Reply-To``
    is exactly the identifier the send recorded, and that is what the locator is addressed by.
    """

    return f"<{ses_message_id}@{domain}>"


__all__ = [
    "INBOUND_FIXTURE_BUCKET",
    "MANAGER_ATTACHMENT",
    "MANAGER_HEDGE",
    "MANAGER_HTML_ONLY",
    "MANAGER_PROMISE",
    "MANAGER_QUOTE_ONLY",
    "MANAGER_WEEKDAY",
    "REVIEWED_REPLIES",
    "ReviewedInboundReply",
    "build_raw_message",
    "build_receipt_envelope",
    "outbound_message_id",
    "reviewed_reply",
]
