"""The ambient signal feed: raw messages in time order, with discovered patterns marked.

This is the presenter's private surface. It deliberately shows the community's own messages,
which is the point of the demo -- the audience has to see the ordinary noise before the
discovery means anything. It is never an Action payload, never an agent input, and never a
source of external disclosure.

The signal join avoids the index V1 refuses to build, and it joins by *exact message ID*. A
feed page already carries at most a hundred message IDs, so the signals for that page are
fetched by direct key in one bounded batch get. Paginating the signal prefix independently and
hoping the two pages overlapped was the earlier approach and it was wrong twice over: signal
sort keys order by message UUID rather than by time, so the first hundred signals bear no
relation to the current page of messages, and the second cursor was discarded, silently
truncating the join for any community with more signals than fit in one page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from chorus.domain.entities import CaseState, CommunityMessage
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    MessageId,
    Namespace,
)
from chorus.ports.ambient import AttachmentCatalogPort
from chorus.ports.pagination import Page, PageCursor, PageRequest
from chorus.ports.repositories import CoreRepositoryPort
from chorus.ports.scopes import CommunityScope


@dataclass(frozen=True, slots=True, kw_only=True)
class ChorusSignal:
    """What the feed says about a message that a validated proposal linked."""

    candidate_case_id: CaseId
    label: str
    related_count: int
    status: CaseState


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedAttachmentThumbnail:
    """A fixture-safe presenter preview reference; never a bucket key or a URL."""

    evidence_id: str
    media_type: str
    caption: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedItem:
    """One ambient feed row."""

    message_id: MessageId
    sent_at: datetime
    pseudonym: str | None
    text: str
    attachment_thumbnails: tuple[FeedAttachmentThumbnail, ...]
    chorus_signal: ChorusSignal | None


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedPage:
    """One bounded page of the ambient feed."""

    items: tuple[FeedItem, ...]
    next_cursor: PageCursor | None


@dataclass(slots=True)
class ReadAmbientFeed:
    """Read one bounded page of the ambient feed with its discovered-pattern signals."""

    core: CoreRepositoryPort
    attachments: AttachmentCatalogPort

    async def execute(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        start: datetime,
        end: datetime,
        request: PageRequest,
    ) -> FeedPage:
        scope = CommunityScope(namespace=namespace, community_id=community_id)
        # One strong direct get before anything is read. Without it a request naming a
        # community that does not exist -- or one in another namespace -- gets an empty page,
        # which is indistinguishable from a real community that has said nothing yet. That is
        # a slow enumeration oracle, and it costs one addressed read to close.
        await self.core.load_community(scope.namespace_scope, community_id)
        messages: Page[CommunityMessage] = await self.core.read_message_feed(
            scope, start=start, end=end, request=request
        )
        signal_by_message = await self.core.read_feed_signals_for_messages(
            scope, tuple(message.message_id for message in messages.items)
        )

        pseudonyms: dict[ContributorId, str] = {}
        items: list[FeedItem] = []
        for message in messages.items:
            contributor_id = message.contributor_id
            pseudonym: str | None = None
            if contributor_id is not None:
                if contributor_id not in pseudonyms:
                    contributor = await self.core.load_contributor(scope, contributor_id)
                    pseudonyms[contributor_id] = contributor.pseudonym
                pseudonym = pseudonyms[contributor_id]

            signal = signal_by_message.get(message.message_id)
            items.append(
                FeedItem(
                    message_id=message.message_id,
                    sent_at=message.sent_at,
                    pseudonym=pseudonym,
                    text=message.raw_text.reveal(),
                    attachment_thumbnails=tuple(
                        self._thumbnail(evidence_id) for evidence_id in message.attachment_ids
                    ),
                    chorus_signal=(
                        None
                        if signal is None
                        else ChorusSignal(
                            candidate_case_id=signal.case_id,
                            label=signal.label,
                            related_count=signal.related_message_count,
                            status=signal.case_state,
                        )
                    ),
                )
            )
        return FeedPage(items=tuple(items), next_cursor=messages.next_cursor)

    def _thumbnail(self, evidence_id: EvidenceItemId) -> FeedAttachmentThumbnail:
        attachment = self.attachments.describe(evidence_id)
        if attachment is None:
            return FeedAttachmentThumbnail(
                evidence_id=str(evidence_id), media_type="application/octet-stream", caption=None
            )
        return FeedAttachmentThumbnail(
            evidence_id=str(evidence_id),
            media_type=attachment.media_type,
            caption=attachment.safe_caption,
        )
