"""Audit-table contract: append-only, scoped, retained, and paginated."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from tests.fixtures.persistence import (
    DEMO_RETENTION,
    OTHER_CASE,
    OTHER_NAMESPACE_WORLD,
    PRIMARY,
    build_repositories,
    relocated,
)

from chorus.infrastructure.dynamodb import codec_audit
from chorus.ports.errors import CrossCaseViolationError, PersistenceConflictError
from chorus.ports.limits import AUDIT_TTL_SECONDS
from chorus.ports.pagination import PageRequest
from chorus.ports.storage import StorageDriver

pytestmark = pytest.mark.anyio


async def test_a_case_event_round_trips(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    event = PRIMARY.audit_event()

    await storage.write_item(repositories.audit.stage_append_case_event(PRIMARY.case_scope, event))

    page = await repositories.audit.read_case_events(PRIMARY.case_scope, PageRequest(limit=10))
    assert page.items == (event,)


async def test_an_audit_event_is_never_overwritten(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    operation = repositories.audit.stage_append_case_event(
        PRIMARY.case_scope, PRIMARY.audit_event()
    )
    await storage.write_item(operation)

    with pytest.raises(PersistenceConflictError):
        await storage.write_item(operation)


async def test_an_event_from_another_case_cannot_be_staged(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)

    with pytest.raises(CrossCaseViolationError):
        repositories.audit.stage_append_case_event(PRIMARY.case_scope, OTHER_CASE.audit_event())


async def test_an_event_from_another_namespace_cannot_be_staged(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)

    with pytest.raises(CrossCaseViolationError):
        repositories.audit.stage_append_namespace_event(
            OTHER_NAMESPACE_WORLD.namespace_scope, PRIMARY.audit_event(case_scoped=False)
        )


async def test_a_foreign_event_body_at_a_valid_address_is_denied(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    event = OTHER_CASE.audit_event()
    foreign = codec_audit.encode_case_event(OTHER_CASE.case_scope, event, retention=DEMO_RETENTION)
    key = codec_audit.case_event_key(PRIMARY.case_scope, event)

    await storage.write_item(relocated(foreign, key))

    with pytest.raises(CrossCaseViolationError):
        await repositories.audit.read_case_events(PRIMARY.case_scope, PageRequest(limit=10))


async def test_namespace_events_are_separate_from_case_events(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    case_event = PRIMARY.audit_event(index=0)
    namespace_event = PRIMARY.audit_event(index=1, case_scoped=False)

    await storage.write_item(
        repositories.audit.stage_append_case_event(PRIMARY.case_scope, case_event)
    )
    await storage.write_item(
        repositories.audit.stage_append_namespace_event(PRIMARY.namespace_scope, namespace_event)
    )

    case_page = await repositories.audit.read_case_events(PRIMARY.case_scope, PageRequest(limit=10))
    namespace_page = await repositories.audit.read_namespace_events(
        PRIMARY.namespace_scope, PageRequest(limit=10)
    )

    assert case_page.items == (case_event,)
    assert namespace_page.items == (namespace_event,)


# -- the namespace partition holds only namespace-level events -----------------------------
#
# The frozen audit mapping gives the namespace partition one purpose: "namespace events
# without case (reset/config)". There is no community partition in the audit table, so an
# event owning a community or a case has no shape there. Both the writer and the reader
# enforce that, because a case-owned row reachable through a namespace-wide read is case
# history surfacing outside its case.


async def test_a_case_event_cannot_be_staged_as_a_namespace_event(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)

    with pytest.raises(CrossCaseViolationError):
        repositories.audit.stage_append_namespace_event(
            PRIMARY.namespace_scope, PRIMARY.audit_event()
        )


async def test_a_community_owning_event_cannot_be_staged_as_a_namespace_event(
    storage: StorageDriver,
) -> None:
    """A community is still narrower ownership than the partition represents."""

    repositories = build_repositories(storage)
    community_event = replace(
        PRIMARY.audit_event(case_scoped=False), community_id=PRIMARY.community_id
    )

    with pytest.raises(CrossCaseViolationError):
        repositories.audit.stage_append_namespace_event(PRIMARY.namespace_scope, community_event)


async def test_a_persisted_namespace_row_owning_a_case_fails_the_page(
    storage: StorageDriver,
) -> None:
    """The reader fails closed even when the row bypassed the writer entirely."""

    repositories = build_repositories(storage)
    event = PRIMARY.audit_event()
    await storage.write_item(
        relocated(
            codec_audit.encode_case_event(PRIMARY.case_scope, event, retention=DEMO_RETENTION),
            codec_audit.namespace_event_key(PRIMARY.namespace_scope, event),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.audit.read_namespace_events(
            PRIMARY.namespace_scope, PageRequest(limit=10)
        )


async def test_a_persisted_namespace_row_owning_a_community_fails_the_page(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    event = replace(PRIMARY.audit_event(case_scoped=False), community_id=PRIMARY.community_id)
    await storage.write_item(
        relocated(
            codec_audit.encode_namespace_event(
                PRIMARY.namespace_scope, event, retention=DEMO_RETENTION
            ),
            codec_audit.namespace_event_key(PRIMARY.namespace_scope, event),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.audit.read_namespace_events(
            PRIMARY.namespace_scope, PageRequest(limit=10)
        )


async def test_a_valid_namespace_event_still_round_trips(storage: StorageDriver) -> None:
    """The rule narrows the partition; it does not close it."""

    repositories = build_repositories(storage)
    event = PRIMARY.audit_event(case_scoped=False)

    await storage.write_item(
        repositories.audit.stage_append_namespace_event(PRIMARY.namespace_scope, event)
    )

    page = await repositories.audit.read_namespace_events(
        PRIMARY.namespace_scope, PageRequest(limit=10)
    )
    assert page.items == (event,)


async def test_a_valid_case_event_still_round_trips_through_its_own_scope(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    event = PRIMARY.audit_event()

    await storage.write_item(repositories.audit.stage_append_case_event(PRIMARY.case_scope, event))

    page = await repositories.audit.read_case_events(PRIMARY.case_scope, PageRequest(limit=10))
    assert page.items == (event,)


async def test_case_events_page_in_occurrence_order(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    events = [PRIMARY.audit_event(index=index) for index in range(6)]
    for event in events:
        await storage.write_item(
            repositories.audit.stage_append_case_event(PRIMARY.case_scope, event)
        )

    seen: list[UUID] = []
    request = PageRequest(limit=2)
    for _ in range(10):
        page = await repositories.audit.read_case_events(PRIMARY.case_scope, request)
        seen.extend(item.audit_event_id for item in page.items)
        if page.next_cursor is None:
            break
        request = PageRequest(limit=2, cursor=page.next_cursor)

    assert seen == [
        event.audit_event_id for event in sorted(events, key=lambda item: item.occurred_at)
    ]


async def test_every_audit_item_carries_the_frozen_retention() -> None:
    event = PRIMARY.audit_event()
    item = codec_audit.encode_case_event(PRIMARY.case_scope, event, retention=DEMO_RETENTION)

    assert item["expires_at_epoch"] == int(event.occurred_at.timestamp()) + AUDIT_TTL_SECONDS
