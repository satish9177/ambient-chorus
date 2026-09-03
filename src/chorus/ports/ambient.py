"""The ambient channel port and the untrusted records an adapter yields.

ADR-005 freezes exactly one V1 adapter: a synthetic deterministic community feed. The port is
shaped so a future Slack, email, or ticket adapter would have to preserve the same four
properties -- a stable channel message identity, a content hash, a contributor mapping, and
attachment provenance -- rather than inventing its own replay semantics.

Everything crossing this port is untrusted input. An adapter reports what a channel said; it
never decides what is relevant, never names a case, and never carries authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from chorus.domain.ids import EvidenceItemId, Sha256Digest
from chorus.domain.time import require_utc


@dataclass(frozen=True, slots=True, kw_only=True)
class AmbientAttachment:
    """Provenance for one attachment, without its bytes.

    ``sha256`` is the content address the evidence root is keyed on, so two channels carrying
    the same underlying file collapse to one origin and cannot manufacture corroboration.
    """

    fixture_id: str
    evidence_id: EvidenceItemId
    media_type: str
    byte_length: int
    sha256: Sha256Digest
    safe_caption: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.fixture_id) <= 64:
            raise ValueError("attachment fixture identifier length is invalid")
        if not 1 <= len(self.media_type) <= 120:
            raise ValueError("attachment media type length is invalid")
        if self.byte_length < 1:
            raise ValueError("attachment byte length must be positive")
        if self.safe_caption is not None and not 1 <= len(self.safe_caption) <= 300:
            raise ValueError("attachment caption length is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class AmbientMessage:
    """One message exactly as the channel reported it."""

    adapter: str
    channel_message_id: str
    contributor_pseudonym: str | None
    sent_at: datetime
    text: str
    attachments: tuple[AmbientAttachment, ...] = ()

    def __post_init__(self) -> None:
        if self.adapter != "SYNTHETIC":
            raise ValueError("unsupported adapter")
        if not 1 <= len(self.channel_message_id) <= 160:
            raise ValueError("channel message identifier length is invalid")
        if not 1 <= len(self.text) <= 10_000:
            raise ValueError("message text length is invalid")
        if (
            self.contributor_pseudonym is not None
            and not 1 <= len(self.contributor_pseudonym) <= 40
        ):
            raise ValueError("contributor pseudonym length is invalid")
        require_utc(self.sent_at)
        fixture_ids = tuple(attachment.fixture_id for attachment in self.attachments)
        if len(set(fixture_ids)) != len(fixture_ids):
            raise ValueError("message attachments must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class AmbientContributorSeed:
    """The pseudonymous identity a channel actor maps onto."""

    pseudonym: str
    display_name: str
    is_resident: bool

    def __post_init__(self) -> None:
        if not 1 <= len(self.pseudonym) <= 40:
            raise ValueError("contributor pseudonym length is invalid")
        if not 1 <= len(self.display_name) <= 120:
            raise ValueError("contributor display name length is invalid")


class AmbientChannelPort(Protocol):
    """Read the frozen ambient corpus for one channel."""

    @property
    def seed_version(self) -> str:
        """The immutable seed identity, for example ``elevator/v1``."""

    @property
    def corpus_sha256(self) -> Sha256Digest:
        """Digest of the exact corpus bytes this adapter loaded and verified."""

    def contributors(self) -> tuple[AmbientContributorSeed, ...]:
        """Return the pseudonymous actor registry the corpus references."""

    def messages(self) -> tuple[AmbientMessage, ...]:
        """Return the whole corpus in channel order; replay is exact."""


class AttachmentCatalogPort(Protocol):
    """Resolve one attachment identifier into the safe descriptor an agent may see.

    V1 accepts only fixed synthetic evidence fixtures, so the catalog is the manifest. It
    exists as a port because the Monitor projection must never guess an attachment description:
    an identifier the catalog cannot describe fails the projection instead of reaching the
    agent as a bare identifier it might then cite.
    """

    def describe(self, evidence_id: EvidenceItemId) -> AmbientAttachment | None:
        """Return the attachment, or ``None`` when this deployment does not know it."""
