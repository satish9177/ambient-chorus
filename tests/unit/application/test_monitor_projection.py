"""What the Monitor is given, and everything it is not.

The projection is a privacy decision, so most of these tests are about absence: a contributor's
name, their email, their durable identifier, a private object key. A test that only checked the
messages were present would pass just as happily on a payload that also carried all of that.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from chorus.application.services.monitor_projection import (
    DECLARED_SENSITIVE_CATEGORIES,
    ProjectionError,
    project_monitor_input,
)
from chorus.contracts.monitor import MonitorAttachmentDescriptor
from chorus.domain.entities import (
    CommunityMessage,
    MessageProcessingStatus,
    SensitivityCategory,
)
from chorus.domain.ids import (
    CommunityId,
    ContributorId,
    EvidenceItemId,
    MessageId,
    Namespace,
    SensitiveStr,
    Sha256Digest,
)

NAMESPACE = Namespace("TEST_PROJECTION")
COMMUNITY = CommunityId(uuid4())
NOW = datetime(2030, 1, 8, 7, 45, tzinfo=UTC)
DIGEST = Sha256Digest("sha256:" + "d" * 64)


def _message(
    *,
    text: str = "The lift stopped again.",
    contributor_id: ContributorId | None = None,
    attachments: tuple[EvidenceItemId, ...] = (),
) -> CommunityMessage:
    return CommunityMessage(
        message_id=MessageId(uuid4()),
        community_id=COMMUNITY,
        namespace=NAMESPACE,
        channel_message_id=f"feed-{uuid4().hex[:8]}",
        contributor_id=contributor_id,
        sent_at=NOW,
        received_at=NOW,
        raw_text=SensitiveStr(text),
        attachment_ids=attachments,
        content_sha256=DIGEST,
        ingestion_idempotency_key="key-0001",
        processing_status=MessageProcessingStatus.NEW,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def test_a_projected_message_carries_a_pseudonym_and_never_a_contributor_identifier() -> None:
    contributor = ContributorId(uuid4())
    message = _message(contributor_id=contributor)

    projection = project_monitor_input(
        messages=(message,), pseudonyms={contributor: "resident-a"}, attachments={}
    )

    payload = projection.payload.model_dump_json()
    assert "resident-a" in payload
    assert str(contributor) not in payload
    assert projection.contributor_by_pseudonym == {"resident-a": contributor}


def test_the_payload_carries_no_channel_or_storage_secret() -> None:
    contributor = ContributorId(uuid4())
    message = _message(contributor_id=contributor)

    payload = project_monitor_input(
        messages=(message,), pseudonyms={contributor: "resident-a"}, attachments={}
    ).payload.model_dump_json()

    assert message.content_sha256.value not in payload
    assert message.ingestion_idempotency_key not in payload
    assert NAMESPACE.value not in payload
    assert str(COMMUNITY) not in payload


def test_an_unattributed_message_is_left_out_rather_than_given_a_placeholder_owner() -> None:
    contributor = ContributorId(uuid4())
    attributed = _message(contributor_id=contributor)
    anonymous = _message(contributor_id=None)

    projection = project_monitor_input(
        messages=(attributed, anonymous),
        pseudonyms={contributor: "resident-a"},
        attachments={},
    )

    assert len(projection.payload.messages) == 1
    assert projection.skipped_unattributed_message_ids == (anonymous.message_id.value,)


def test_a_message_whose_contributor_has_no_pseudonym_is_left_out() -> None:
    known = ContributorId(uuid4())
    unknown = ContributorId(uuid4())

    projection = project_monitor_input(
        messages=(_message(contributor_id=known), _message(contributor_id=unknown)),
        pseudonyms={known: "resident-a"},
        attachments={},
    )

    assert len(projection.payload.messages) == 1


def test_a_batch_with_nothing_attributable_is_refused() -> None:
    with pytest.raises(ProjectionError):
        project_monitor_input(messages=(_message(),), pseudonyms={}, attachments={})


def test_one_pseudonym_covering_two_contributors_is_refused() -> None:
    """Ownership must be provable, and a shared pseudonym makes it ambiguous."""

    first = ContributorId(uuid4())
    second = ContributorId(uuid4())

    with pytest.raises(ProjectionError):
        project_monitor_input(
            messages=(_message(contributor_id=first), _message(contributor_id=second)),
            pseudonyms={first: "resident-a", second: "resident-a"},
            attachments={},
        )


def test_an_attachment_the_application_cannot_describe_fails_the_batch() -> None:
    contributor = ContributorId(uuid4())
    message = _message(contributor_id=contributor, attachments=(EvidenceItemId(uuid4()),))

    with pytest.raises(ProjectionError):
        project_monitor_input(
            messages=(message,), pseudonyms={contributor: "resident-a"}, attachments={}
        )


def test_an_attachment_is_described_by_type_and_caption_only() -> None:
    contributor = ContributorId(uuid4())
    evidence_id = EvidenceItemId(uuid4())
    message = _message(contributor_id=contributor, attachments=(evidence_id,))

    projection = project_monitor_input(
        messages=(message,),
        pseudonyms={contributor: "resident-a"},
        attachments={
            evidence_id: MonitorAttachmentDescriptor(
                evidence_id=evidence_id.value,
                media_type="image/jpeg",
                safe_caption="A photograph of a control panel.",
            )
        },
    )

    descriptor = projection.payload.messages[0].attachment_descriptors[0]
    assert set(type(descriptor).model_fields) == {"evidence_id", "media_type", "safe_caption"}
    assert "sha256" not in projection.payload.model_dump_json()


def test_an_empty_batch_is_refused() -> None:
    with pytest.raises(ProjectionError):
        project_monitor_input(messages=(), pseudonyms={}, attachments={})


def test_a_batch_beyond_the_frozen_bound_is_refused() -> None:
    contributor = ContributorId(uuid4())
    messages = tuple(_message(contributor_id=contributor) for _ in range(51))

    with pytest.raises(ProjectionError):
        project_monitor_input(
            messages=messages, pseudonyms={contributor: "resident-a"}, attachments={}
        )


def test_the_declared_sensitive_categories_never_include_a_general_one() -> None:
    """Naming a category asks the model to flag it; it is not permission to disclose it."""

    assert SensitivityCategory.GENERAL not in DECLARED_SENSITIVE_CATEGORIES
    assert SensitivityCategory.HEALTH in DECLARED_SENSITIVE_CATEGORIES
    assert SensitivityCategory.UNIT_LOCATION in DECLARED_SENSITIVE_CATEGORIES
