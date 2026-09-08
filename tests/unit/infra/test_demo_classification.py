"""P2-5: the demo signal classification and predicted case identity cannot drift.

The fake Monitor (``build_lexical_output``) and demo reset (``predict_demo_case_id``) classify
the frozen corpus through one shared module, and reset derives its prediction from the message
identifiers ``IngestMessages`` actually assigns -- not from a positional or hard-coded
channel-id list. A reordered corpus therefore either reorders both derivations together, or is
rejected at adapter construction before any seed is written.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import cast

import pytest

from chorus.composition import demo_reset
from chorus.composition.demo_reset import predict_demo_case_id
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import Namespace
from chorus.infrastructure.fixtures.synthetic_feed import (
    SyntheticAmbientAdapter,
    default_fixture_root,
)
from chorus.infrastructure.local import demo_classification, monitor_agent
from chorus.infrastructure.local.demo_classification import (
    is_reportable_message,
    is_signal_message,
    sensitive_detail_kind,
)

NAMESPACE = Namespace("DEMO")
FROZEN_SIGNAL_CHANNEL_IDS = frozenset(
    {"feed-002", "feed-005", "feed-008", "feed-011", "feed-012", "feed-014", "feed-016"}
)
FROZEN_SENSITIVE_DETAIL_CHANNEL_IDS = frozenset({"feed-004", "feed-006", "feed-007"})
"""P2-8: the corpus's family/health/unit messages in the same exchange as the incident report."""


# -- D: one classification module, no second copy to drift --------------------------------


def test_the_fake_monitor_uses_the_shared_classification_predicates() -> None:
    # The fake Monitor holds no copy of the predicates -- it binds the exact function objects
    # the shared module defines, so the two cannot answer differently.
    monitor_globals = vars(monitor_agent)
    assert monitor_globals["is_signal_message"] is demo_classification.is_signal_message
    assert (
        monitor_globals["is_policy_like_instruction"]
        is demo_classification.is_policy_like_instruction
    )
    assert monitor_globals["sensitive_detail_kind"] is demo_classification.sensitive_detail_kind
    # And reset's case-identity prediction binds the exact union predicate the fake Monitor's
    # report-creation decision is built from, not a restated equivalent.
    assert vars(demo_reset)["is_reportable_message"] is demo_classification.is_reportable_message


def test_the_shared_signal_predicate_matches_the_frozen_corpus_set() -> None:
    adapter = SyntheticAmbientAdapter()
    classified = {
        message.channel_message_id
        for message in adapter.messages()
        if is_signal_message(message.text)
    }
    assert classified == FROZEN_SIGNAL_CHANNEL_IDS


# -- P2-8: private-detail messages are recognized, and never double-counted as signals -----


def test_sensitive_detail_kind_matches_the_frozen_corpus_set() -> None:
    adapter = SyntheticAmbientAdapter()
    by_channel = {
        message.channel_message_id: sensitive_detail_kind(message.text)
        for message in adapter.messages()
    }
    detected = {channel_id for channel_id, kind in by_channel.items() if kind is not None}
    assert detected == FROZEN_SENSITIVE_DETAIL_CHANNEL_IDS
    assert by_channel["feed-004"] == "IDENTITY_ATTRIBUTE"  # "My mother Leela..."
    assert by_channel["feed-006"] == "HEALTH_DETAIL"  # "...has asthma..."
    assert by_channel["feed-007"] == "UNIT_LOCATION"  # "...apartment 4B..."


def test_signal_and_sensitive_detail_are_mutually_exclusive_on_the_frozen_corpus() -> None:
    adapter = SyntheticAmbientAdapter()
    for message in adapter.messages():
        if is_signal_message(message.text):
            assert sensitive_detail_kind(message.text) is None, message.channel_message_id


def test_is_reportable_message_is_the_union_on_the_frozen_corpus() -> None:
    adapter = SyntheticAmbientAdapter()
    reportable = {
        message.channel_message_id
        for message in adapter.messages()
        if is_reportable_message(message.text)
    }
    assert reportable == FROZEN_SIGNAL_CHANNEL_IDS | FROZEN_SENSITIVE_DETAIL_CHANNEL_IDS


def test_the_injected_instruction_is_neither_a_signal_nor_a_sensitive_detail() -> None:
    injected = next(
        message
        for message in SyntheticAmbientAdapter().messages()
        if message.channel_message_id == "feed-018"
    )
    assert not is_signal_message(injected.text)
    assert sensitive_detail_kind(injected.text) is None
    assert not is_reportable_message(injected.text)


# -- A / C: prediction is a pure function of the seeded message identifiers ---------------


def test_prediction_is_deterministic_across_calls() -> None:
    adapter = SyntheticAmbientAdapter()
    first = predict_demo_case_id(
        adapter, namespace=NAMESPACE, community_id=adapter.community.community_id
    )
    second = predict_demo_case_id(
        adapter, namespace=NAMESPACE, community_id=adapter.community.community_id
    )
    assert first == second


def test_prediction_changes_with_message_order_because_it_reads_assigned_ids() -> None:
    """A reordered corpus reorders the identifiers ``IngestMessages`` assigns, so the
    prediction reorders with it -- the live Monitor run over that same order derives the same
    value, which is the invariant. It is emphatically *not* a positional constant."""

    adapter = SyntheticAmbientAdapter()
    forward = predict_demo_case_id(
        adapter, namespace=NAMESPACE, community_id=adapter.community.community_id
    )

    class _Reversed:
        def __init__(self, inner: SyntheticAmbientAdapter) -> None:
            self._inner = inner

        def messages(self) -> object:
            return tuple(reversed(self._inner.messages()))

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    reversed_prediction = predict_demo_case_id(
        cast(SyntheticAmbientAdapter, _Reversed(adapter)),
        namespace=NAMESPACE,
        community_id=adapter.community.community_id,
    )
    assert reversed_prediction != forward


# -- B: an accidental reorder is rejected before any seed is written ---------------------


def test_a_corpus_that_does_not_match_its_manifest_digest_is_refused(tmp_path: Path) -> None:
    fixture_root = tmp_path / "elevator-v1"
    shutil.copytree(default_fixture_root(), fixture_root)
    feed_path = fixture_root / "feed.json"
    feed = json.loads(feed_path.read_text(encoding="utf-8"))
    feed["messages"] = list(reversed(feed["messages"]))
    feed_path.write_text(json.dumps(feed), encoding="utf-8")

    with pytest.raises(IntegrityError):
        SyntheticAmbientAdapter(root=fixture_root)
