"""The ADR-005 synthetic ambient adapter: fixed input data with verified checksums.

The corpus on disk is *input*, not an answer key. It contains messages, actors, timestamps and
attachment provenance, and it deliberately contains no report identifier, no fact identifier,
no case identifier, and no statement about which messages belong together. Discovery has to
happen at runtime through the Monitor contract, so there is nothing here for it to copy.

Every file is checksum-verified against the manifest before a single message is returned. A
corpus that was edited without updating its manifest, an evidence file whose bytes changed, or
a declared byte length that disagrees with the file fails closed with an integrity error
rather than being replayed as if it were the frozen fixture.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid5

from chorus.domain.errors import IntegrityError
from chorus.domain.ids import CommunityId, ContributorId, EvidenceItemId, Sha256Digest
from chorus.domain.time import parse_utc, require_utc
from chorus.ports.ambient import AmbientAttachment, AmbientContributorSeed, AmbientMessage

CORPUS_SCHEMA_VERSION: Final = "ambient-corpus/v1"
MANIFEST_SCHEMA_VERSION: Final = "ambient-manifest/v1"
DEFAULT_SEED_VERSION: Final = "elevator/v1"

_MANIFEST_FILE: Final = "manifest.json"


def default_fixture_root(seed_version: str = DEFAULT_SEED_VERSION) -> Path:
    """Locate the checked-in corpus for a seed version inside a source checkout.

    The corpus is developer and demo seed data, not part of the installed wheel, so a
    deployed composition root passes an explicit path instead of relying on this.
    """

    repository_root = Path(__file__).resolve().parents[4]
    return repository_root / "demo" / "fixtures" / seed_version.replace("/", "-")


def _digest_of(raw: bytes) -> Sha256Digest:
    return Sha256Digest(f"sha256:{sha256(raw).hexdigest()}")


def _fail(detail: str) -> IntegrityError:
    """Build a safe integrity error naming only a fixed code, never fixture content."""

    return IntegrityError(f"AMBIENT_FIXTURE:{detail}")


class _Reader:
    """Exact JSON object reader: every key is consumed, and leftovers fail closed."""

    __slots__ = ("_consumed", "_raw", "_ref")

    def __init__(self, raw: object, *, ref: str) -> None:
        if not isinstance(raw, dict):
            raise _fail(f"type:{ref}")
        self._raw: dict[str, Any] = raw
        self._ref = ref
        self._consumed: set[str] = set()

    def _value(self, name: str) -> object:
        if name not in self._raw:
            raise _fail(f"missing:{self._ref}.{name}")
        self._consumed.add(name)
        return self._raw[name]

    def text(self, name: str) -> str:
        value = self._value(name)
        if not isinstance(value, str):
            raise _fail(f"type:{self._ref}.{name}")
        return value

    def optional_text(self, name: str) -> str | None:
        value = self._value(name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise _fail(f"type:{self._ref}.{name}")
        return value

    def number(self, name: str) -> int:
        value = self._value(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(f"type:{self._ref}.{name}")
        return value

    def flag(self, name: str) -> bool:
        value = self._value(name)
        if not isinstance(value, bool):
            raise _fail(f"type:{self._ref}.{name}")
        return value

    def texts(self, name: str) -> tuple[str, ...]:
        value = self._value(name)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise _fail(f"type:{self._ref}.{name}")
        return tuple(str(item) for item in value)

    def objects(self, name: str) -> tuple[object, ...]:
        value = self._value(name)
        if not isinstance(value, list):
            raise _fail(f"type:{self._ref}.{name}")
        return tuple(value)

    def child(self, name: str) -> _Reader:
        return _Reader(self._value(name), ref=f"{self._ref}.{name}")

    def optional_child(self, name: str, *, ref: str) -> _Reader | None:
        """Consume a required key whose value is either an object or an explicit null."""

        value = self._value(name)
        return None if value is None else _Reader(value, ref=ref)

    def digest(self, name: str) -> Sha256Digest:
        try:
            return Sha256Digest(self.text(name))
        except ValueError as error:
            raise _fail(f"value:{self._ref}.{name}") from error

    def uuid(self, name: str) -> UUID:
        value = self.text(name)
        try:
            parsed = UUID(value)
        except ValueError as error:
            raise _fail(f"value:{self._ref}.{name}") from error
        if str(parsed) != value:
            raise _fail(f"value:{self._ref}.{name}")
        return parsed

    def instant(self, name: str) -> datetime:
        try:
            return parse_utc(self.text(name))
        except ValueError as error:
            raise _fail(f"value:{self._ref}.{name}") from error

    def finish(self) -> None:
        unread = set(self._raw) - self._consumed
        if unread:
            raise _fail(f"unexpected:{self._ref}")


_FORBIDDEN_CAPTION_LOCATOR = re.compile(r"(?i)(?:https?|s3)://")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceReview:
    """The ADR-018 fixed review: curated fixture metadata, and nothing else.

    In policy/v1 this is the *only* producer of a safe-evidence review. It is not an LLM
    judgement, not visual inference, not a moderation service, and not a resident mandate.

    ``reviewed_by`` is fixture-curation provenance. It is deliberately a plain string and not a
    ``ContributorId``: recording a resident identifier here would write into the corpus the
    falsehood that a person authorized an export review, which is the same category of mistake
    ADR-015 refused when it declined to model management as a contributor. It confers no
    authority of any kind, and in particular it is not a verification source.

    ``safe_caption`` is read from the evidence entry that owns this review rather than being
    restated inside it. One string in one place cannot disagree with itself, and the entry is
    where the ingestion path already reads the caption from.
    """

    no_face: bool
    no_unit: bool
    no_name: bool
    no_health: bool
    safe_caption: str
    reviewed_by: str
    reviewed_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.reviewed_at)
        if not 1 <= len(self.reviewed_by) <= 120:
            raise ValueError("reviewer provenance length is invalid")
        if not 1 <= len(self.safe_caption) <= 300:
            raise ValueError("safe caption length is invalid")
        if "@" in self.safe_caption or _FORBIDDEN_CAPTION_LOCATOR.search(self.safe_caption):
            raise ValueError("safe caption contains a forbidden locator")

    @property
    def cleared(self) -> bool:
        """True only when every frozen clearance holds. Any false flag fails closed."""

        return self.no_face and self.no_unit and self.no_name and self.no_health


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceFixture:
    """One checksum-verified evidence file declared by the manifest."""

    fixture_id: str
    evidence_id: EvidenceItemId
    relative_path: str
    media_type: str
    byte_length: int
    sha256: Sha256Digest
    safe_caption: str | None
    ingested_with_feed: bool
    review: EvidenceReview | None = None

    def __post_init__(self) -> None:
        if self.review is not None and self.review.safe_caption != self.safe_caption:
            raise ValueError("a review must carry the caption of the entry that owns it")


@dataclass(frozen=True, slots=True, kw_only=True)
class CommunitySeed:
    """The single demo community the corpus belongs to."""

    community_id: CommunityId
    name: str
    timezone: str
    public_label: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ContributorSeed:
    """A pseudonymous actor plus the durable contributor identity it maps onto."""

    pseudonym: str
    display_name: str
    is_resident: bool
    contributor_id: ContributorId


class SyntheticAmbientAdapter:
    """The frozen 24-message community feed, verified on load and replayed exactly.

    Construction performs every integrity check, so a caller that holds an instance is
    holding a corpus whose bytes matched its manifest. Nothing is re-read afterwards.
    """

    __slots__ = (
        "_community",
        "_contributor_ids_by_pseudonym",
        "_contributors",
        "_corpus_sha256",
        "_evidence",
        "_evidence_by_id",
        "_logical_clock_start",
        "_messages",
        "_seed_version",
        "_uuid5_name_prefix",
        "_uuid5_namespace",
    )

    def __init__(self, root: Path | None = None) -> None:
        fixture_root = default_fixture_root() if root is None else root
        manifest = _Reader(_load_json(fixture_root / _MANIFEST_FILE), ref=MANIFEST_SCHEMA_VERSION)
        if manifest.text("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise _fail("schema_version:manifest")
        self._seed_version = manifest.text("seed_version")
        self._uuid5_namespace = manifest.uuid("uuid5_namespace")
        self._uuid5_name_prefix = manifest.text("uuid5_name_prefix")
        self._logical_clock_start = manifest.instant("logical_clock_start")

        community = manifest.child("community")
        self._community = CommunitySeed(
            community_id=CommunityId(community.uuid("community_id")),
            name=community.text("name"),
            timezone=community.text("timezone"),
            public_label=community.text("public_label"),
        )
        community.finish()

        self._contributors = _read_contributors(manifest)
        self._contributor_ids_by_pseudonym = {
            item.pseudonym: item.contributor_id for item in self._contributors
        }
        self._evidence = _read_evidence(manifest, fixture_root)
        self._evidence_by_id = {item.evidence_id: item for item in self._evidence}

        corpus = manifest.child("corpus")
        corpus_path = _contained_path(fixture_root, corpus.text("file"))
        declared_digest = corpus.digest("sha256")
        declared_count = corpus.number("message_count")
        corpus.finish()
        manifest.finish()

        raw_corpus = _read_bytes(corpus_path)
        actual_digest = _digest_of(raw_corpus)
        if actual_digest != declared_digest:
            raise _fail("checksum:corpus")
        self._corpus_sha256 = actual_digest
        self._messages = _read_messages(
            raw_corpus,
            seed_version=self._seed_version,
            evidence=self._evidence,
            known_pseudonyms=frozenset(item.pseudonym for item in self._contributors),
        )
        if len(self._messages) != declared_count:
            raise _fail("count:corpus")

    # -- AmbientChannelPort ------------------------------------------------------------

    @property
    def seed_version(self) -> str:
        return self._seed_version

    @property
    def corpus_sha256(self) -> Sha256Digest:
        return self._corpus_sha256

    def contributors(self) -> tuple[AmbientContributorSeed, ...]:
        return tuple(
            AmbientContributorSeed(
                pseudonym=item.pseudonym,
                display_name=item.display_name,
                is_resident=item.is_resident,
            )
            for item in self._contributors
        )

    def messages(self) -> tuple[AmbientMessage, ...]:
        return self._messages

    # -- seeding helpers ---------------------------------------------------------------

    @property
    def community(self) -> CommunitySeed:
        return self._community

    @property
    def logical_clock_start(self) -> datetime:
        return self._logical_clock_start

    @property
    def evidence_fixtures(self) -> tuple[EvidenceFixture, ...]:
        return self._evidence

    @property
    def contributor_seeds(self) -> tuple[ContributorSeed, ...]:
        return self._contributors

    @property
    def contributor_ids_by_pseudonym(self) -> dict[str, ContributorId]:
        return dict(self._contributor_ids_by_pseudonym)

    def describe(self, evidence_id: EvidenceItemId) -> AmbientAttachment | None:
        """Resolve one attachment identifier from the verified manifest."""

        fixture = self._evidence_by_id.get(evidence_id)
        if fixture is None:
            return None
        return AmbientAttachment(
            fixture_id=fixture.fixture_id,
            evidence_id=fixture.evidence_id,
            media_type=fixture.media_type,
            byte_length=fixture.byte_length,
            sha256=fixture.sha256,
            safe_caption=fixture.safe_caption,
        )

    def derived_id(self, name: str) -> UUID:
        """Reproduce a manifest identifier from its documented UUIDv5 derivation.

        Only fixture identities are derived this way. Durable identity for anything the
        system discovers at runtime is derived from validated inputs instead, never from a
        fixture name.
        """

        return uuid5(self._uuid5_namespace, f"{self._uuid5_name_prefix}{name}")


def _load_json(path: Path) -> object:
    try:
        return json.loads(_read_bytes(path).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise _fail(f"json:{path.name}") from error


def _contained_path(fixture_root: Path, relative: str) -> Path:
    """Resolve one manifest-declared path, refusing anything outside the fixture root.

    A manifest is checked-in data rather than user input today, which is exactly why this is
    worth writing down: the loader is the only thing standing between "a path in a JSON file"
    and "bytes this process reads and hashes as trusted fixture content", and a future manifest
    could arrive from somewhere less careful.

    Resolution happens *before* containment is checked, so ``..`` segments, an absolute path,
    and a symlink pointing outside the tree are all rejected by the same test rather than by
    three separate string checks that each miss a case.
    """

    if not relative or chr(0) in relative:
        raise _fail("path:empty")
    candidate = Path(relative)
    if candidate.is_absolute() or candidate.drive or candidate.anchor:
        raise _fail("path:absolute")
    root = fixture_root.resolve(strict=False)
    resolved = (root / candidate).resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise _fail("path:escape")
    return resolved


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise _fail(f"missing_file:{path.name}") from error


def _read_contributors(manifest: _Reader) -> tuple[ContributorSeed, ...]:
    seeds: list[ContributorSeed] = []
    for index, raw in enumerate(manifest.objects("contributors")):
        reader = _Reader(raw, ref=f"contributor[{index}]")
        seeds.append(
            ContributorSeed(
                pseudonym=reader.text("pseudonym"),
                display_name=reader.text("display_name"),
                is_resident=reader.flag("is_resident"),
                contributor_id=ContributorId(reader.uuid("contributor_id")),
            )
        )
        reader.finish()
    pseudonyms = tuple(item.pseudonym for item in seeds)
    identifiers = tuple(item.contributor_id for item in seeds)
    if not seeds or len(set(pseudonyms)) != len(pseudonyms):
        raise _fail("contributors")
    if len(set(identifiers)) != len(identifiers):
        raise _fail("contributors")
    return tuple(seeds)


def _read_review(reader: _Reader, index: int, *, safe_caption: str | None) -> EvidenceReview | None:
    """Read one entry's ADR-018 review, failing closed on anything incomplete.

    ``review`` is a required key with an explicit ``null`` for the entries that have none, so
    an omitted review is a manifest error rather than a silently unreviewed file. A review
    without a caption is refused here: the caption is part of the record, and a cleared review
    with nothing to export describes a decision nobody could have made.
    """

    child = reader.optional_child("review", ref=f"evidence[{index}].review")
    if child is None:
        return None
    if safe_caption is None:
        raise _fail(f"missing:evidence[{index}].safe_caption")
    try:
        review = EvidenceReview(
            no_face=child.flag("no_face"),
            no_unit=child.flag("no_unit"),
            no_name=child.flag("no_name"),
            no_health=child.flag("no_health"),
            safe_caption=safe_caption,
            reviewed_by=child.text("reviewed_by"),
            reviewed_at=child.instant("reviewed_at"),
        )
    except ValueError as error:
        raise _fail(f"invariant:evidence[{index}].review") from error
    child.finish()
    return review


def _read_evidence(manifest: _Reader, fixture_root: Path) -> tuple[EvidenceFixture, ...]:
    fixtures: list[EvidenceFixture] = []
    for index, raw in enumerate(manifest.objects("evidence")):
        reader = _Reader(raw, ref=f"evidence[{index}]")
        relative_path = reader.text("file")
        declared_length = reader.number("byte_length")
        declared_digest = reader.digest("sha256")
        safe_caption = reader.optional_text("safe_caption")
        review = _read_review(reader, index, safe_caption=safe_caption)
        try:
            fixture = EvidenceFixture(
                fixture_id=reader.text("fixture_id"),
                evidence_id=EvidenceItemId(reader.uuid("evidence_id")),
                relative_path=relative_path,
                media_type=reader.text("media_type"),
                byte_length=declared_length,
                sha256=declared_digest,
                safe_caption=safe_caption,
                ingested_with_feed=reader.flag("ingested_with_feed"),
                review=review,
            )
        except ValueError as error:
            raise _fail(f"invariant:evidence[{index}]") from error
        reader.finish()
        raw_bytes = _read_bytes(_contained_path(fixture_root, relative_path))
        if len(raw_bytes) != declared_length or _digest_of(raw_bytes) != declared_digest:
            raise _fail("checksum:evidence")
        fixtures.append(fixture)
    fixture_ids = tuple(item.fixture_id for item in fixtures)
    evidence_ids = tuple(item.evidence_id for item in fixtures)
    if len(set(fixture_ids)) != len(fixture_ids) or len(set(evidence_ids)) != len(evidence_ids):
        raise _fail("evidence")
    return tuple(fixtures)


def _read_messages(
    raw_corpus: bytes,
    *,
    seed_version: str,
    evidence: tuple[EvidenceFixture, ...],
    known_pseudonyms: frozenset[str],
) -> tuple[AmbientMessage, ...]:
    corpus = _Reader(
        json.loads(raw_corpus.decode("utf-8")),
        ref=CORPUS_SCHEMA_VERSION,
    )
    if corpus.text("schema_version") != CORPUS_SCHEMA_VERSION:
        raise _fail("schema_version:corpus")
    if corpus.text("seed_version") != seed_version:
        raise _fail("seed_version:corpus")
    adapter = corpus.text("adapter")
    by_fixture_id = {item.fixture_id: item for item in evidence}

    messages: list[AmbientMessage] = []
    for index, raw in enumerate(corpus.objects("messages")):
        reader = _Reader(raw, ref=f"message[{index}]")
        pseudonym = reader.optional_text("contributor_pseudonym")
        if pseudonym is not None and pseudonym not in known_pseudonyms:
            raise _fail("unknown_pseudonym")
        attachments: list[AmbientAttachment] = []
        for fixture_id in reader.texts("attachment_fixture_ids"):
            fixture = by_fixture_id.get(fixture_id)
            if fixture is None or not fixture.ingested_with_feed:
                raise _fail("unknown_attachment")
            attachments.append(
                AmbientAttachment(
                    fixture_id=fixture.fixture_id,
                    evidence_id=fixture.evidence_id,
                    media_type=fixture.media_type,
                    byte_length=fixture.byte_length,
                    sha256=fixture.sha256,
                    safe_caption=fixture.safe_caption,
                )
            )
        try:
            message = AmbientMessage(
                adapter=adapter,
                channel_message_id=reader.text("channel_message_id"),
                contributor_pseudonym=pseudonym,
                sent_at=reader.instant("sent_at"),
                text=reader.text("text"),
                attachments=tuple(attachments),
            )
        except ValueError as error:
            raise _fail("invariant:message") from error
        reader.finish()
        messages.append(message)
    corpus.finish()

    channel_ids = tuple(message.channel_message_id for message in messages)
    if len(set(channel_ids)) != len(channel_ids):
        raise _fail("duplicate_channel_message_id")
    ordered = tuple(
        sorted(messages, key=lambda message: (message.sent_at, message.channel_message_id))
    )
    if ordered != tuple(messages):
        raise _fail("order:corpus")
    return tuple(messages)
