"""The feed joins signals by exact message ID, at every page and at every scale.

The earlier join paginated the ``MESSAGE_SIGNAL#`` prefix independently of the message page and
hoped the two overlapped. They do not overlap in general and cannot be made to: message sort
keys order by time, signal sort keys order by message UUID, and the second page's cursor was
discarded, so a community with more signals than fit in one page silently lost the join for
every row whose signal happened to sort past the first hundred.

These tests build exactly that situation -- more signals than a page, and a page of messages
whose signals sort late -- and assert the join is complete anyway. They also cover the ordinary
cases that must keep working: no signals at all, some rows signalled and others not, and pages
deep into a long feed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.fixtures.monitor import MonitorHarness

from chorus.domain.entities import CaseState
from chorus.domain.ids import CaseId, MessageId
from chorus.ports.ambient import AmbientMessage
from chorus.ports.pagination import PageRequest
from chorus.ports.records import FeedSignalProjection, MessageFeedEntry
from chorus.ports.scopes import CommunityScope
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio

FEED_START = datetime(2000, 1, 1, tzinfo=UTC)
FEED_END = datetime(2100, 1, 1, tzinfo=UTC)
SIGNAL_COUNT = 130
"""More than one page of signals, and more than one page of messages.

A hundred is the frozen page maximum, so a hundred and thirty is the smallest number that
makes "the first page of signals" and "the signals of the first page" genuinely different sets.
"""


async def _ingest_many(harness: MonitorHarness, count: int) -> tuple[MessageFeedEntry, ...]:
    """Ingest ``count`` synthetic messages spread one minute apart."""

    await harness.seed()
    base = datetime(2030, 1, 2, 8, 0, tzinfo=UTC)
    entries: list[MessageFeedEntry] = []
    for start in range(0, count, 25):
        batch = tuple(
            AmbientMessage(
                adapter="SYNTHETIC",
                channel_message_id=f"bulk-{index:04d}",
                contributor_pseudonym="resident-a",
                sent_at=base + timedelta(minutes=index),
                text=f"An ordinary message number {index}.",
            )
            for index in range(start, min(start + 25, count))
        )
        result = await harness.ingest_messages(batch, idempotency_key=f"bulk-key-{start:04d}")
        sent_at = {message.channel_message_id: message.sent_at for message in batch}
        entries.extend(
            MessageFeedEntry(message_id=item.message_id, sent_at=sent_at[item.channel_message_id])
            for item in result.messages
        )
    return tuple(entries)


async def _write_signals(
    harness: MonitorHarness, entries: tuple[MessageFeedEntry, ...], case_id: CaseId
) -> None:
    """Write one signal per message directly, in batches the transaction bound allows."""

    scope = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    now = harness.clock.now()
    for start in range(0, len(entries), 50):
        chunk = entries[start : start + 50]
        await harness.unit_of_work.commit(
            TransactionPlan(
                name="seed-feed-signals",
                operations=tuple(
                    harness.core.stage_create_feed_signal(
                        scope,
                        FeedSignalProjection(
                            namespace=harness.namespace,
                            community_id=harness.community_id,
                            message_id=entry.message_id,
                            case_id=case_id,
                            case_version=1,
                            label="Recurring lift failures",
                            related_message_count=len(entries),
                            case_state=CaseState.CANDIDATE,
                            detected_at=now,
                        ),
                    )
                    for entry in chunk
                ),
                audit_required=False,
            )
        )


async def _page(harness: MonitorHarness, *, limit: int, cursor: object = None) -> object:
    return await harness.read_feed.execute(
        namespace=harness.namespace,
        community_id=harness.community_id,
        start=FEED_START,
        end=FEED_END,
        request=PageRequest(limit=limit, cursor=cursor),  # type: ignore[arg-type]
    )


async def test_every_row_of_every_page_carries_its_own_signal(
    harness: MonitorHarness,
) -> None:
    """The exact regression: more signals than a page, and no row loses its own."""

    entries = await _ingest_many(harness, SIGNAL_COUNT)
    case_id = CaseId(entries[0].message_id.value)
    await _write_signals(harness, entries, case_id)

    seen: set[MessageId] = set()
    cursor = None
    pages = 0
    while True:
        page = await _page(harness, limit=25, cursor=cursor)
        pages += 1
        for item in page.items:  # type: ignore[attr-defined]
            assert item.chorus_signal is not None, f"row {item.message_id} lost its signal"
            assert item.chorus_signal.candidate_case_id == case_id
            seen.add(item.message_id)
        cursor = page.next_cursor  # type: ignore[attr-defined]
        if cursor is None:
            break

    assert pages > 1
    assert seen == {entry.message_id for entry in entries}


async def test_a_late_page_whose_signals_sort_past_the_first_hundred_still_joins(
    harness: MonitorHarness,
) -> None:
    """Signal sort keys are message UUIDs, so "the first hundred" is an arbitrary set.

    Reaching the final page and finding every row signalled is precisely what the old join
    could not do, because the rows on that page are the *last* by time and their signals sit
    wherever their identifiers happen to fall in UUID order.
    """

    entries = await _ingest_many(harness, SIGNAL_COUNT)
    case_id = CaseId(entries[0].message_id.value)
    await _write_signals(harness, entries, case_id)

    cursor = None
    last_page = None
    while True:
        page = await _page(harness, limit=100, cursor=cursor)
        last_page = page
        cursor = page.next_cursor  # type: ignore[attr-defined]
        if cursor is None:
            break

    assert last_page is not None
    assert last_page.items  # type: ignore[attr-defined]
    assert all(item.chorus_signal is not None for item in last_page.items)  # type: ignore[attr-defined]


async def test_a_sparsely_signalled_feed_marks_only_the_rows_that_have_signals(
    harness: MonitorHarness,
) -> None:
    entries = await _ingest_many(harness, 40)
    case_id = CaseId(entries[0].message_id.value)
    signalled = entries[::4]
    await _write_signals(harness, signalled, case_id)

    page = await _page(harness, limit=100)

    marked = {
        item.message_id
        for item in page.items  # type: ignore[attr-defined]
        if item.chorus_signal is not None
    }
    assert marked == {entry.message_id for entry in signalled}


async def test_a_feed_with_no_signals_at_all_returns_every_row_unmarked(
    harness: MonitorHarness,
) -> None:
    await _ingest_many(harness, 12)

    page = await _page(harness, limit=100)

    assert page.items  # type: ignore[attr-defined]
    assert all(item.chorus_signal is None for item in page.items)  # type: ignore[attr-defined]


async def test_a_signal_for_a_message_outside_the_page_is_not_fetched(
    harness: MonitorHarness,
) -> None:
    """The join asks for exactly the page's keys, so an unrelated signal is never read."""

    entries = await _ingest_many(harness, 60)
    case_id = CaseId(entries[0].message_id.value)
    await _write_signals(harness, entries, case_id)

    first = await _page(harness, limit=10)

    assert len(first.items) == 10  # type: ignore[attr-defined]
    assert all(item.chorus_signal is not None for item in first.items)  # type: ignore[attr-defined]
    scope = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    fetched = await harness.core.read_feed_signals_for_messages(
        scope,
        tuple(item.message_id for item in first.items),  # type: ignore[attr-defined]
    )
    assert len(fetched) == 10


async def test_a_signal_from_another_community_never_reaches_this_feed(
    harness: MonitorHarness,
) -> None:
    """Containment is asserted on the join itself, not only on the query that feeds it."""

    from tests.fixtures.monitor import OTHER_NAMESPACE

    entries = await _ingest_many(harness, 5)
    case_id = CaseId(entries[0].message_id.value)
    await _write_signals(harness, entries, case_id)

    foreign = MonitorHarness(driver=harness.driver, namespace=OTHER_NAMESPACE)
    scope = CommunityScope(namespace=OTHER_NAMESPACE, community_id=foreign.community_id)

    fetched = await foreign.core.read_feed_signals_for_messages(
        scope, tuple(entry.message_id for entry in entries)
    )

    assert fetched == {}
