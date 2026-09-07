"""Astra P2-3: an over-limit reference set must refuse closed, never truncate.

``DecodedReceipt.thread_references`` used to silently cut the deduplicated set down to
``MAX_MESSAGE_IDS`` entries before correlation ever ran, which let an attacker pad a delivery
with unknown message identifiers to push a *second* genuine match past the cutoff -- turning an
ambiguous correlation into an apparently unique, successful one. These tests pin the repaired
behavior directly against the pure property, with no envelope or storage plumbing required.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from chorus.application.services.inbound_mail import (
    MAX_MESSAGE_IDS,
    DecodedReceipt,
    InboundReplyRejected,
    InboundReplyRejection,
    ReceiptVerdicts,
)

RECEIVED = datetime(2030, 1, 2, 9, 0, tzinfo=UTC)
PASS = ReceiptVerdicts(spf="PASS", dkim="PASS", dmarc="PASS", spam="PASS", virus="PASS")


def _receipt(*, in_reply_to: tuple[str, ...], references: tuple[str, ...]) -> DecodedReceipt:
    return DecodedReceipt(
        message_id="<reply@manager.invalid>",
        in_reply_to=in_reply_to,
        references=references,
        subject_length=10,
        source="manager@chorus.invalid",
        destination="chorus-replies@chorus.invalid",
        bucket_name="bucket",
        object_key="key",
        headers_truncated=False,
        received_at=RECEIVED,
        verdicts=PASS,
    )


def _ids(prefix: str, count: int) -> tuple[str, ...]:
    return tuple(f"<{prefix}-{index}@us-east-1.amazonses.com>" for index in range(count))


def test_a_reference_set_at_the_bound_is_returned_whole() -> None:
    receipt = _receipt(in_reply_to=(), references=_ids("ref", MAX_MESSAGE_IDS))

    assert len(receipt.thread_references) == MAX_MESSAGE_IDS


def test_a_reference_set_one_over_the_bound_refuses_closed_rather_than_truncating() -> None:
    receipt = _receipt(in_reply_to=(), references=_ids("ref", MAX_MESSAGE_IDS + 1))

    with pytest.raises(InboundReplyRejected) as raised:
        _ = receipt.thread_references
    assert raised.value.rejection is InboundReplyRejection.REPLY_TOO_MANY_REFERENCES


def test_padding_with_unknown_references_cannot_rescue_a_would_be_truncated_match() -> None:
    """The genuine match named at position 0 must not become reachable by drowning it in padding.

    Before this repair, a truncating implementation kept exactly the first ``MAX_MESSAGE_IDS``
    entries -- so a genuine second match placed *after* that cutoff was silently dropped, and
    correlation proceeded as if only the first match existed. The repaired property refuses the
    whole delivery instead of ever answering with a silently shortened set.
    """

    genuine = "<real-match@us-east-1.amazonses.com>"
    padding = _ids("unknown", MAX_MESSAGE_IDS + 5)
    receipt = _receipt(in_reply_to=(genuine,), references=padding)

    with pytest.raises(InboundReplyRejected) as raised:
        _ = receipt.thread_references
    assert raised.value.rejection is InboundReplyRejection.REPLY_TOO_MANY_REFERENCES


def test_deduplication_happens_before_the_bound_is_checked() -> None:
    """``In-Reply-To`` repeated inside ``References`` is one reference, not two."""

    shared = _ids("shared", MAX_MESSAGE_IDS)
    receipt = _receipt(in_reply_to=(shared[0],), references=shared)

    assert receipt.thread_references == shared


def test_in_reply_to_is_ordered_first() -> None:
    receipt = _receipt(
        in_reply_to=("<first@us-east-1.amazonses.com>",),
        references=("<second@us-east-1.amazonses.com>",),
    )

    assert receipt.thread_references == (
        "<first@us-east-1.amazonses.com>",
        "<second@us-east-1.amazonses.com>",
    )
