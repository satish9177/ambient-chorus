"""The frozen corpus is input data, and its integrity is checked before it is used.

Two things are being asserted. First, that the fixture matches the frozen demo description --
24 messages, four residents, six incidents, the private details, the contradiction, the photo,
the injection. Second, and more importantly, that it contains *no* expected outcome: no report
identifier, no fact identifier, no case identifier, and no statement about which messages
belong together. A corpus that carried those would let discovery be copied rather than done.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chorus.domain.errors import IntegrityError
from chorus.infrastructure.fixtures.synthetic_feed import (
    SyntheticAmbientAdapter,
    default_fixture_root,
)

FIXTURE_ROOT = default_fixture_root()


@pytest.fixture
def adapter() -> SyntheticAmbientAdapter:
    return SyntheticAmbientAdapter()


def test_the_corpus_holds_exactly_the_frozen_twenty_four_messages(
    adapter: SyntheticAmbientAdapter,
) -> None:
    messages = adapter.messages()

    assert len(messages) == 24
    assert len({message.channel_message_id for message in messages}) == 24
    assert adapter.seed_version == "elevator/v1"


def test_the_corpus_is_ordered_and_replayed_exactly(
    adapter: SyntheticAmbientAdapter,
) -> None:
    messages = adapter.messages()

    assert list(messages) == sorted(messages, key=lambda item: item.sent_at)
    assert adapter.messages() == SyntheticAmbientAdapter().messages()
    assert adapter.corpus_sha256 == SyntheticAmbientAdapter().corpus_sha256


def test_four_residents_and_one_unverified_sender_are_registered(
    adapter: SyntheticAmbientAdapter,
) -> None:
    residents = [seed for seed in adapter.contributor_seeds if seed.is_resident]
    others = [seed for seed in adapter.contributor_seeds if not seed.is_resident]

    assert len(residents) == 4
    assert len(others) == 1


def test_the_corpus_carries_no_expected_outcome(adapter: SyntheticAmbientAdapter) -> None:
    """Nothing in the file names a report, a fact, a case, or a grouping."""

    raw = (FIXTURE_ROOT / "feed.json").read_text(encoding="utf-8").lower()

    for forbidden in (
        "report_id",
        "fact_id",
        "case_id",
        "issue_type",
        "incident",
        "candidate",
        "expected",
        "elevator_failure",
    ):
        assert forbidden not in raw


def test_every_message_carries_only_channel_facts(adapter: SyntheticAmbientAdapter) -> None:
    payload = json.loads((FIXTURE_ROOT / "feed.json").read_text(encoding="utf-8"))

    for message in payload["messages"]:
        assert set(message) == {
            "channel_message_id",
            "sent_at",
            "contributor_pseudonym",
            "text",
            "attachment_fixture_ids",
        }


def test_the_photo_and_the_injection_are_the_two_ingested_attachments(
    adapter: SyntheticAmbientAdapter,
) -> None:
    ingested = [item for item in adapter.evidence_fixtures if item.ingested_with_feed]
    staged = [item for item in adapter.evidence_fixtures if not item.ingested_with_feed]

    assert {item.media_type for item in ingested} == {"image/jpeg", "text/plain"}
    assert [item.media_type for item in staged] == ["message/rfc822"]


def test_the_management_reply_is_staged_but_never_ingested(
    adapter: SyntheticAmbientAdapter,
) -> None:
    """Phase 9 ingests it live; Phase 3 only proves it is catalogued and checksummed."""

    reply = next(
        item for item in adapter.evidence_fixtures if item.fixture_id == "management-reply"
    )
    attached = {
        attachment.fixture_id
        for message in adapter.messages()
        for attachment in message.attachments
    }

    assert reply.ingested_with_feed is False
    assert reply.fixture_id not in attached


def test_the_injection_message_is_present_and_is_only_text(
    adapter: SyntheticAmbientAdapter,
) -> None:
    injection = next(
        message
        for message in adapter.messages()
        if "ignore all previous instructions" in message.text.lower()
    )

    assert injection.channel_message_id == "feed-018"
    assert injection.contributor_pseudonym == "attacker-fixture"


def test_a_tampered_corpus_fails_closed(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    feed = root / "feed.json"
    feed.write_text(feed.read_text(encoding="utf-8").replace("lift", "elevator"), encoding="utf-8")

    with pytest.raises(IntegrityError):
        SyntheticAmbientAdapter(root)


def test_tampered_evidence_bytes_fail_closed(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    (root / "evidence" / "injection-notice.txt").write_bytes(b"different bytes entirely")

    with pytest.raises(IntegrityError):
        SyntheticAmbientAdapter(root)


def test_an_unknown_manifest_field_fails_closed(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["expected_case_id"] = "00000000-0000-4000-8000-000000000000"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(IntegrityError):
        SyntheticAmbientAdapter(root)


def test_a_message_naming_an_unknown_actor_fails_closed(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    feed_path = root / "feed.json"
    feed = json.loads(feed_path.read_text(encoding="utf-8"))
    feed["messages"][0]["contributor_pseudonym"] = "resident-z"
    _rewrite_feed(root, feed)

    with pytest.raises(IntegrityError):
        SyntheticAmbientAdapter(root)


def test_manifest_identifiers_match_their_documented_derivation(
    adapter: SyntheticAmbientAdapter,
) -> None:
    assert adapter.community.community_id.value == adapter.derived_id("community")
    for seed in adapter.contributor_seeds:
        assert seed.contributor_id.value == adapter.derived_id(f"contributor/{seed.pseudonym}")


def _copy_fixture(tmp_path: Path) -> Path:
    import shutil

    destination = tmp_path / "elevator-v1"
    shutil.copytree(FIXTURE_ROOT, destination)
    return destination


def _rewrite_feed(root: Path, feed: dict[str, object]) -> None:
    """Rewrite the corpus and its declared digest, so only the tested field is wrong."""

    import hashlib

    path = root / "feed.json"
    raw = (json.dumps(feed, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(raw)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["corpus"]["sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
