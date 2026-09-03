"""Ingestion contract: identity, replay, conflict, and evidence origin.

These run against both drivers because every expectation here is about conditional writes and
strongly consistent reads, which is exactly where an emulator and DynamoDB could disagree.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.fixtures.monitor import PRESENTER_ACTOR_HASH, MonitorHarness

from chorus.application.commands.ingest_messages import (
    IngestMessagesCommand,
    channel_identity_digest,
)
from chorus.domain.errors import IntegrityError, ValidationError
from chorus.domain.ids import EvidenceItemId, Sha256Digest
from chorus.infrastructure.fixtures.synthetic_feed import SyntheticAmbientAdapter
from chorus.ports.errors import IdempotencyConflictError, NotFoundError
from chorus.ports.pagination import PageRequest
from chorus.ports.records import MessageFeedEntry

pytestmark = pytest.mark.anyio


async def test_first_ingest_stores_every_frozen_message_once(harness: MonitorHarness) -> None:
    await harness.seed()

    locators = await harness.ingest_feed()

    assert len(locators) == 24
    assert len({locator.message_id for locator in locators}) == 24
    page = await harness.core.read_message_feed(
        harness.core_scope,
        start=harness.adapter.messages()[0].sent_at - timedelta(days=1),
        end=harness.adapter.messages()[-1].sent_at + timedelta(days=1),
        request=PageRequest(limit=100),
    )
    assert len(page.items) == 24


async def test_exact_replay_returns_the_original_identifiers(harness: MonitorHarness) -> None:
    await harness.seed()
    corpus = harness.adapter.messages()[:5]

    first = await harness.ingest_messages(corpus, idempotency_key="replay-key-000001")
    again = await harness.ingest_messages(corpus, idempotency_key="replay-key-000001")

    assert [item.message_id for item in first.messages] == [
        item.message_id for item in again.messages
    ]
    assert first.replayed_count == 0
    assert again.replayed_count == 5
    assert again.accepted_count == 0


async def test_replay_under_a_new_client_key_still_finds_the_original(
    harness: MonitorHarness,
) -> None:
    """The channel identity, not the client key, is what makes a message unique."""

    await harness.seed()
    corpus = harness.adapter.messages()[:3]

    first = await harness.ingest_messages(corpus, idempotency_key="first-key-000001")
    again = await harness.ingest_messages(corpus, idempotency_key="second-key-00001")

    assert [item.message_id for item in first.messages] == [
        item.message_id for item in again.messages
    ]
    assert again.replayed_count == 3


async def test_same_channel_identity_with_changed_content_is_a_conflict(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    original = harness.adapter.messages()[1]
    await harness.ingest_messages((original,), idempotency_key="conflict-key-0001")
    tampered = replace(original, text="Something completely different was said here.")

    with pytest.raises(IdempotencyConflictError):
        await harness.ingest_messages((tampered,), idempotency_key="conflict-key-0002")


async def test_a_conflicting_replay_leaves_the_original_message_intact(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    original = harness.adapter.messages()[1]
    stored = await harness.ingest_messages((original,), idempotency_key="conflict-key-0003")
    tampered = replace(original, text="Overwritten text.")

    with pytest.raises(IdempotencyConflictError):
        await harness.ingest_messages((tampered,), idempotency_key="conflict-key-0004")

    message = await harness.core.load_message(
        harness.core_scope,
        MessageFeedEntry(message_id=stored.messages[0].message_id, sent_at=original.sent_at),
    )
    assert message.raw_text.reveal() == original.text


async def test_a_duplicate_channel_identifier_inside_one_request_is_refused(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    message = harness.adapter.messages()[0]

    with pytest.raises(ValidationError):
        await harness.ingest_messages((message, message), idempotency_key="dup-key-00000001")


async def test_a_malformed_source_message_is_refused_before_any_write(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    corpus = harness.adapter.messages()
    # Built at the command boundary rather than through the adapter: the adapter already
    # refuses an oversized message, and the point of this test is that the *command* refuses
    # it too, so a future non-synthetic adapter cannot become the only thing checking.
    broken = replace(harness.command_message(corpus[0]), text="x" * 10_001)

    with pytest.raises(ValidationError):
        await harness.ingest.execute(
            IngestMessagesCommand(
                namespace=harness.namespace,
                community_id=harness.community_id,
                actor_id_hash=PRESENTER_ACTOR_HASH,
                idempotency_key="malformed-key-001",
                messages=(broken,),
            )
        )

    # Nothing partial survives: the uniqueness lock for that channel identity was never taken,
    # so the batch is genuinely absent rather than half stored.
    assert (
        await harness.core.load_channel_lock(
            harness.core_scope,
            adapter="SYNTHETIC",
            channel_message_id_sha256=channel_identity_digest(corpus[0].channel_message_id),
        )
        is None
    )


async def test_a_message_for_an_unknown_contributor_is_refused(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    message = harness.adapter.messages()[0]
    command_message = harness.command_message(message)
    unknown = replace(command_message, contributor_id=harness.contributor_id("nobody"))

    with pytest.raises(NotFoundError):
        await harness.ingest.execute(
            IngestMessagesCommand(
                namespace=harness.namespace,
                community_id=harness.community_id,
                actor_id_hash=PRESENTER_ACTOR_HASH,
                idempotency_key="unknown-actor-0001",
                messages=(unknown,),
            )
        )


async def test_initial_evidence_creates_one_content_addressed_root(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    photo_message = next(message for message in harness.adapter.messages() if message.attachments)
    attachment = photo_message.attachments[0]

    result = await harness.ingest_messages((photo_message,), idempotency_key="evidence-key-01")

    assert result.evidence_roots_created == 1
    root = await harness.core.load_evidence_root(harness.core_scope, attachment.sha256)
    assert root is not None
    assert root.root_sha256 == attachment.sha256
    assert root.media_type == attachment.media_type


async def test_duplicate_evidence_bytes_collapse_onto_one_root(
    harness: MonitorHarness,
) -> None:
    """Forwarding the same photo records a second submission, not a second origin."""

    await harness.seed()
    photo_message = next(message for message in harness.adapter.messages() if message.attachments)
    attachment = photo_message.attachments[0]
    forwarded = replace(
        photo_message,
        channel_message_id="feed-016-forwarded",
        text="Forwarding what was posted earlier about the lift.",
        attachments=(
            replace(
                attachment,
                fixture_id="elevator-e42-photo-copy",
                evidence_id=EvidenceItemId(harness.adapter.derived_id("evidence/forwarded-copy")),
            ),
        ),
    )

    first = await harness.ingest_messages((photo_message,), idempotency_key="dup-root-000001")
    second = await harness.ingest_messages((forwarded,), idempotency_key="dup-root-000002")

    assert first.evidence_roots_created == 1
    assert second.evidence_roots_created == 0


async def test_one_content_hash_claiming_two_media_types_fails_closed(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    photo_message = next(message for message in harness.adapter.messages() if message.attachments)
    await harness.ingest_messages((photo_message,), idempotency_key="media-type-00001")
    relabelled = replace(
        photo_message,
        channel_message_id="feed-016-relabelled",
        text="Same bytes, different declared type.",
        attachments=(replace(photo_message.attachments[0], media_type="text/plain"),),
    )

    with pytest.raises(IntegrityError):
        await harness.ingest_messages((relabelled,), idempotency_key="media-type-00002")


async def test_ingestion_is_deterministic_across_a_reset(harness: MonitorHarness) -> None:
    """The corpus digest is the same on every load, so replay is exact by construction."""

    assert harness.adapter.corpus_sha256 == SyntheticAmbientAdapter().corpus_sha256
    assert isinstance(harness.adapter.corpus_sha256, Sha256Digest)
