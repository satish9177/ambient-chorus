"""Category A: the frozen key grammar is exact and user text never reaches a key."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from tests.fixtures.persistence import PRIMARY, digest

from chorus.domain.ids import (
    ActionId,
    CaseId,
    CommunityId,
    MandateId,
    MessageId,
    Namespace,
    OperationId,
    Sha256Digest,
    ViewId,
)
from chorus.infrastructure.dynamodb import keys

NAMESPACE = Namespace("TEST_KEYS")
INSTANT = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def test_partition_grammar_matches_the_frozen_table() -> None:
    community_id = CommunityId(UUID("11111111-1111-4111-8111-111111111111"))
    case_id = CaseId(UUID("22222222-2222-4222-8222-222222222222"))
    operation_id = OperationId(UUID("33333333-3333-4333-8333-333333333333"))
    view_id = ViewId(UUID("44444444-4444-4444-8444-444444444444"))
    action_id = ActionId(UUID("55555555-5555-4555-8555-555555555555"))

    assert keys.namespace_partition(NAMESPACE) == "NS#TEST_KEYS"
    assert keys.community_partition(NAMESPACE, community_id) == f"NS#TEST_KEYS#COMM#{community_id}"
    assert keys.case_partition(NAMESPACE, case_id) == f"NS#TEST_KEYS#CASE#{case_id}"
    assert (
        keys.operation_partition(NAMESPACE, operation_id)
        == f"NS#TEST_KEYS#OPERATION#{operation_id}"
    )
    assert keys.view_partition(NAMESPACE, view_id) == f"NS#TEST_KEYS#VIEW#{view_id}"
    assert keys.view_current_partition(NAMESPACE, case_id) == f"NS#TEST_KEYS#VIEW_CURRENT#{case_id}"
    assert keys.action_partition(NAMESPACE, action_id) == f"NS#TEST_KEYS#ACTION#{action_id}"
    assert (
        keys.action_current_partition(NAMESPACE, case_id)
        == f"NS#TEST_KEYS#ACTION_CURRENT#{case_id}"
    )


def test_sort_keys_are_time_ordered_and_zero_padded() -> None:
    message_id = MessageId(UUID("66666666-6666-4666-8666-666666666666"))
    mandate_id = MandateId(UUID("77777777-7777-4777-8777-777777777777"))

    assert (
        keys.message_sort_key(INSTANT, message_id)
        == f"MESSAGE#2026-08-31T12:00:00.000000Z#{message_id}"
    )
    assert (
        keys.mandate_version_sort_key(mandate_id, 7) == f"MANDATE#{mandate_id}#VERSION#0000000007"
    )
    # Zero padding is what makes lexicographic order equal numeric order.
    assert keys.mandate_version_sort_key(mandate_id, 2) < keys.mandate_version_sort_key(
        mandate_id, 10
    )


def test_message_feed_bounds_enclose_every_identifier_at_the_boundary_instant() -> None:
    message_id = MessageId(UUID("66666666-6666-4666-8666-666666666666"))
    sort_key = keys.message_sort_key(INSTANT, message_id)

    assert keys.message_sort_key_lower_bound(INSTANT) <= sort_key
    assert sort_key <= keys.message_sort_key_upper_bound(INSTANT)


def test_channel_message_identity_enters_a_key_only_as_a_digest() -> None:
    channel_message_id = "synthetic-message#with#separators"
    hashed = digest(channel_message_id)

    sort_key = keys.channel_lock_sort_key("SYNTHETIC", hashed)

    assert sort_key == f"MESSAGE_KEY#SYNTHETIC#{hashed.value}"
    assert channel_message_id not in sort_key


@pytest.mark.parametrize(
    "segment",
    ["", "with#separator", "control\x00character", "delete\x7f", "x" * 257],
)
def test_invalid_key_segments_are_rejected(segment: str) -> None:
    with pytest.raises(ValueError, match="key segment"):
        keys.channel_lock_sort_key(segment, digest("value"))


def test_mandate_version_must_be_positive_and_bounded() -> None:
    mandate_id = PRIMARY.mandate_id
    with pytest.raises(ValueError, match="positive"):
        keys.mandate_version_sort_key(mandate_id, 0)
    with pytest.raises(ValueError, match="fixed key width"):
        keys.mandate_version_sort_key(mandate_id, 10**10)


def test_digest_segments_are_canonical() -> None:
    root = Sha256Digest("sha256:" + "a" * 64)

    assert keys.evidence_root_sort_key(root) == f"EVIDENCE_ROOT#{root.value}"


def test_prefixes_match_the_sort_keys_they_scan() -> None:
    world = PRIMARY
    assert keys.fact_sort_key(world.fact_id).startswith(keys.FACT_SORT_KEY_PREFIX)
    assert keys.report_sort_key(world.report_id).startswith(keys.REPORT_SORT_KEY_PREFIX)
    assert keys.commitment_sort_key(world.commitment_id).startswith(keys.COMMITMENT_SORT_KEY_PREFIX)
    assert keys.assessment_sort_key(INSTANT, world.assessment_id).startswith(
        keys.ASSESSMENT_SORT_KEY_PREFIX
    )
    assert keys.view_history_sort_key(INSTANT, world.view_id).startswith(
        keys.HISTORY_SORT_KEY_PREFIX
    )
    assert keys.action_history_sort_key(INSTANT, world.action_id).startswith(
        keys.HISTORY_SORT_KEY_PREFIX
    )
    assert keys.audit_event_sort_key(INSTANT, world.uuid("audit")).startswith(
        keys.EVENT_SORT_KEY_PREFIX
    )
    assert keys.mandate_current_sort_key(world.mandate_id).startswith(
        keys.MANDATE_CURRENT_SORT_KEY_PREFIX
    )
    assert keys.message_sort_key(INSTANT, world.message_id).startswith(keys.MESSAGE_SORT_KEY_PREFIX)
