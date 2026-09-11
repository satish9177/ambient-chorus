"""Unit tests for ADR-030 SES receipt decoding against synthetic AWS documentation fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chorus.application.services.inbound_mail import (
    DecodedReceipt,
    InboundReplyRejected,
    InboundReplyRejection,
    decode_receipt_envelope,
)

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "ses_receipt"


def _load_fixture(filename: str) -> dict[str, Any]:
    path = FIXTURE_DIR / filename
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def test_decode_valid_receipt_fixture() -> None:
    fixture = _load_fixture("valid_receipt.json")
    receipt = decode_receipt_envelope(fixture)

    assert isinstance(receipt, DecodedReceipt)
    assert receipt.message_id == "<reply-0001@manager.invalid>"
    assert receipt.bucket_name == "chorus-private-evidence-demo"
    assert receipt.object_key == "ns/DEMO/inbound/ses-msg-0001-synthetic-valid"
    assert receipt.source == "property-manager@chorus.invalid"
    assert receipt.correspondent_address == "property-manager@chorus.invalid"
    assert receipt.destination == "inbound-replies@chorus.invalid"
    assert receipt.recipients == ("inbound-replies@chorus.invalid",)
    assert receipt.headers_truncated is False
    assert receipt.verdicts.accepted is True
    assert len(receipt.thread_references) == 1
    assert "ses-outbound-msg-0001@us-east-1.amazonses.com" in receipt.thread_references[0]


def test_decode_malformed_action_type_fixture() -> None:
    fixture = _load_fixture("malformed_action_type.json")
    with pytest.raises(InboundReplyRejected) as exc:
        decode_receipt_envelope(fixture)
    assert exc.value.rejection is InboundReplyRejection.MALFORMED_ENVELOPE


def test_decode_failed_verdicts_fixture() -> None:
    fixture = _load_fixture("failed_verdicts.json")
    receipt = decode_receipt_envelope(fixture)

    assert receipt.verdicts.accepted is False
    assert receipt.verdicts.spf == "FAIL"
    assert receipt.verdicts.dmarc == "FAIL"
    assert receipt.verdicts.virus == "FAIL"


def test_decode_wrong_recipient_fixture() -> None:
    fixture = _load_fixture("wrong_recipient.json")
    receipt = decode_receipt_envelope(fixture)

    assert receipt.recipients == ("other-mailbox@chorus.invalid",)


def test_decode_wrong_correspondent_fixture() -> None:
    fixture = _load_fixture("wrong_correspondent.json")
    receipt = decode_receipt_envelope(fixture)

    assert receipt.correspondent_address == "stranger@external.invalid"


def test_decode_truncated_headers_fixture() -> None:
    fixture = _load_fixture("truncated_headers.json")
    receipt = decode_receipt_envelope(fixture)

    assert receipt.headers_truncated is True


def test_envelope_sender_and_parsed_from_are_decoded_independently() -> None:
    """ADR-030 § 5: ``mail.source`` (envelope MAIL FROM) is transport provenance only; the
    correspondent identity is the single addr-spec in ``mail.commonHeaders.from``. A delivery
    whose ``mail.source`` is an SRS/bounce return path but whose ``From`` is the real
    correspondent must decode the two as distinct values -- the digest agreement compares the
    ``From`` mailbox, never ``mail.source``."""

    fixture = _load_fixture("valid_receipt.json")
    fixture["mail"]["source"] = "bounces+srs=abcd=manager.invalid@bounce.provider.invalid"
    fixture["mail"]["commonHeaders"]["from"] = [
        "Property Manager <property-manager@chorus.invalid>"
    ]

    receipt = decode_receipt_envelope(fixture)

    assert receipt.source == "bounces+srs=abcd=manager.invalid@bounce.provider.invalid"
    assert receipt.correspondent_address == "property-manager@chorus.invalid"
    assert receipt.source != receipt.correspondent_address


def test_multiple_mail_destination_entries_are_not_refused_at_decode() -> None:
    """ADR-030 § 6: the single-recipient refusal is removed from the decode step. A reply that
    CCs other mailboxes still decodes; the authoritative receive address is
    ``receipt.recipients`` (the set the rule matched), decoded here for the agreement check."""

    fixture = _load_fixture("valid_receipt.json")
    fixture["mail"]["destination"] = [
        "inbound-replies@chorus.invalid",
        "someone-else@external.invalid",
    ]

    receipt = decode_receipt_envelope(fixture)

    assert receipt.recipients == ("inbound-replies@chorus.invalid",)


def test_absent_or_multiple_from_is_malformed() -> None:
    """ADR-030 § 5: absent, empty, or more than one ``commonHeaders.from`` entry is
    ``MALFORMED_ENVELOPE`` -- a message claiming two authors is not a message with an author."""

    for bad_from in ([], ["a@x.invalid", "b@y.invalid"], "not-a-list"):
        fixture = _load_fixture("valid_receipt.json")
        fixture["mail"]["commonHeaders"]["from"] = bad_from
        with pytest.raises(InboundReplyRejected) as exc:
            decode_receipt_envelope(fixture)
        assert exc.value.rejection is InboundReplyRejection.MALFORMED_ENVELOPE
