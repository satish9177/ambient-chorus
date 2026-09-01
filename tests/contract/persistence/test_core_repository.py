"""Core-table repository contract: scope denial, concurrency, paging, and read intent."""

from __future__ import annotations

import pytest
from tests.fixtures.persistence import (
    OTHER_CASE,
    OTHER_NAMESPACE_WORLD,
    PRIMARY,
    build_repositories,
    relocated,
)

from chorus.domain.ids import FactId
from chorus.infrastructure.dynamodb import codec_case, codec_core
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.ports.errors import (
    CrossCaseViolationError,
    InvalidCursorError,
    ModelLimitExceededError,
    NotFoundError,
    PersistenceConflictError,
)
from chorus.ports.limits import BATCH_GET_MAX_KEYS, MAX_ACTIVE_FACTS_PER_CASE
from chorus.ports.pagination import PageRequest
from chorus.ports.records import MandatePointerExpectation, MessageFeedEntry
from chorus.ports.storage import StorageDriver

pytestmark = pytest.mark.anyio


async def test_a_community_round_trips_through_the_driver(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    community = PRIMARY.community()

    await storage.write_item(
        repositories.core.stage_create_community(PRIMARY.namespace_scope, community)
    )

    assert (
        await repositories.core.load_community(PRIMARY.namespace_scope, PRIMARY.community_id)
        == community
    )


async def test_an_absent_record_is_not_found(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)

    with pytest.raises(NotFoundError):
        await repositories.core.load_community(PRIMARY.namespace_scope, PRIMARY.community_id)


async def test_an_absent_optional_record_is_none_rather_than_an_error(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)

    assert await repositories.core.load_send_fence(PRIMARY.case_scope) is None


async def test_a_foreign_namespace_body_at_a_valid_address_is_denied(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    foreign = codec_core.encode_community(
        OTHER_NAMESPACE_WORLD.namespace_scope, OTHER_NAMESPACE_WORLD.community()
    )
    key = codec_core.community_key(PRIMARY.namespace_scope, PRIMARY.community_id)

    await storage.write_item(relocated(foreign, key))

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.load_community(PRIMARY.namespace_scope, PRIMARY.community_id)


async def test_a_foreign_case_body_at_a_valid_address_is_denied(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    foreign = codec_case.encode_fact(OTHER_CASE.case_scope, OTHER_CASE.fact())
    key = codec_case.fact_key(PRIMARY.case_scope, PRIMARY.fact_id)

    await storage.write_item(relocated(foreign, key))

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.load_facts(PRIMARY.case_scope, (PRIMARY.fact_id,))


async def test_one_foreign_row_fails_the_whole_batch(storage: StorageDriver) -> None:
    """Partial results would let a caller learn which rows exist in another case."""

    repositories = build_repositories(storage)
    second_id = FactId(PRIMARY.uuid("fact:second"))
    await storage.write_item(
        repositories.core.stage_create_fact(PRIMARY.case_scope, PRIMARY.fact())
    )
    foreign = codec_case.encode_fact(OTHER_CASE.case_scope, OTHER_CASE.fact())
    await storage.write_item(relocated(foreign, codec_case.fact_key(PRIMARY.case_scope, second_id)))

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.load_facts(PRIMARY.case_scope, (PRIMARY.fact_id, second_id))


async def test_a_batch_read_requires_every_requested_record(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    await storage.write_item(
        repositories.core.stage_create_fact(PRIMARY.case_scope, PRIMARY.fact())
    )

    with pytest.raises(NotFoundError):
        await repositories.core.load_facts(
            PRIMARY.case_scope, (PRIMARY.fact_id, FactId(PRIMARY.uuid("fact:absent")))
        )


async def test_a_batch_read_returns_results_in_the_requested_order(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    ids = tuple(FactId(PRIMARY.uuid(f"fact:{index}")) for index in range(5))
    for fact_id in ids:
        await storage.write_item(
            repositories.core.stage_create_fact(PRIMARY.case_scope, PRIMARY.fact(fact_id=fact_id))
        )

    facts = await repositories.core.load_facts(PRIMARY.case_scope, tuple(reversed(ids)))

    assert tuple(fact.fact_id for fact in facts) == tuple(reversed(ids))


async def test_a_batch_read_rejects_duplicate_and_oversized_requests(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)

    with pytest.raises(ValueError, match="unique"):
        await repositories.core.load_facts(PRIMARY.case_scope, (PRIMARY.fact_id, PRIMARY.fact_id))
    with pytest.raises(ModelLimitExceededError):
        await repositories.core.load_facts(
            PRIMARY.case_scope,
            tuple(FactId(PRIMARY.uuid(f"fact:{index}")) for index in range(BATCH_GET_MAX_KEYS + 1)),
        )


async def test_an_empty_batch_reads_nothing(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)

    assert await repositories.core.load_facts(PRIMARY.case_scope, ()) == ()


async def test_a_create_never_overwrites(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    operation = repositories.core.stage_create_fact(PRIMARY.case_scope, PRIMARY.fact())
    await storage.write_item(operation)

    with pytest.raises(PersistenceConflictError):
        await storage.write_item(operation)


async def test_an_update_requires_the_exact_expected_version(storage: StorageDriver) -> None:
    """Two writers who both read version 1 cannot both win."""

    repositories = build_repositories(storage)
    await storage.write_item(
        repositories.core.stage_create_fact(PRIMARY.case_scope, PRIMARY.fact())
    )
    first = repositories.core.stage_update_fact(
        PRIMARY.case_scope, PRIMARY.fact(version=2), expected_version=1
    )
    second = repositories.core.stage_update_fact(
        PRIMARY.case_scope, PRIMARY.fact(version=2), expected_version=1
    )

    await storage.write_item(first)
    with pytest.raises(PersistenceConflictError):
        await storage.write_item(second)

    stored = await repositories.core.load_facts(PRIMARY.case_scope, (PRIMARY.fact_id,))
    assert stored[0].version == 2


async def test_an_update_must_increment_the_version_by_one(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)

    with pytest.raises(ValueError, match="increment"):
        repositories.core.stage_update_fact(
            PRIMARY.case_scope, PRIMARY.fact(version=5), expected_version=1
        )


async def test_case_facts_paginate_without_duplication_or_omission(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    ids = {FactId(PRIMARY.uuid(f"fact:{index}")) for index in range(7)}
    for fact_id in ids:
        await storage.write_item(
            repositories.core.stage_create_fact(PRIMARY.case_scope, PRIMARY.fact(fact_id=fact_id))
        )

    seen: list[FactId] = []
    request = PageRequest(limit=3)
    for _ in range(10):
        page = await repositories.core.read_case_facts(PRIMARY.case_scope, request)
        seen.extend(fact.fact_id for fact in page.items)
        if page.next_cursor is None:
            break
        request = PageRequest(limit=3, cursor=page.next_cursor)

    assert len(seen) == len(ids)
    assert set(seen) == ids


async def test_a_cursor_cannot_be_replayed_against_another_case(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    for index in range(3):
        await storage.write_item(
            repositories.core.stage_create_fact(
                PRIMARY.case_scope, PRIMARY.fact(fact_id=FactId(PRIMARY.uuid(f"fact:{index}")))
            )
        )
    page = await repositories.core.read_case_facts(PRIMARY.case_scope, PageRequest(limit=1))
    assert page.next_cursor is not None

    with pytest.raises(InvalidCursorError):
        await repositories.core.read_case_facts(
            OTHER_CASE.case_scope, PageRequest(limit=1, cursor=page.next_cursor)
        )


async def test_the_message_feed_is_bounded_by_the_requested_window(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    messages = [PRIMARY.message(index=index) for index in range(5)]
    for message in messages:
        await storage.write_item(
            repositories.core.stage_create_message(PRIMARY.community_scope, message)
        )
    window = messages[1:4]

    page = await repositories.core.read_message_feed(
        PRIMARY.community_scope,
        start=window[0].sent_at,
        end=window[-1].sent_at,
        request=PageRequest(limit=10),
    )

    assert {item.message_id for item in page.items} == {item.message_id for item in window}


async def test_a_feed_entry_loads_the_exact_message(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    message = PRIMARY.message(index=2)
    await storage.write_item(
        repositories.core.stage_create_message(PRIMARY.community_scope, message)
    )

    loaded = await repositories.core.load_message(
        PRIMARY.community_scope,
        MessageFeedEntry(message_id=message.message_id, sent_at=message.sent_at),
    )

    assert loaded == message


async def test_the_per_case_fact_bound_is_enforced_before_any_write(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    oversized = PRIMARY.case(
        fact_ids=tuple(
            FactId(PRIMARY.uuid(f"fact:{index}")) for index in range(MAX_ACTIVE_FACTS_PER_CASE + 1)
        )
    )

    with pytest.raises(ModelLimitExceededError):
        repositories.core.stage_create_case(PRIMARY.case_scope, oversized)


async def test_a_case_at_the_bound_is_accepted(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    at_limit = PRIMARY.case(
        fact_ids=tuple(
            FactId(PRIMARY.uuid(f"fact:{index}")) for index in range(MAX_ACTIVE_FACTS_PER_CASE)
        )
    )

    await storage.write_item(repositories.core.stage_create_case(PRIMARY.case_scope, at_limit))

    assert await repositories.core.load_case(PRIMARY.case_scope) == at_limit


async def test_the_current_mandate_pointer_is_created_once_and_replaced_conditionally(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    first = PRIMARY.mandate_pointer(mandate_version=1, row_version=1)
    await storage.write_item(
        repositories.core.stage_replace_current_mandate_pointer(
            PRIMARY.case_scope, first, expected=None
        )
    )

    with pytest.raises(PersistenceConflictError):
        await storage.write_item(
            repositories.core.stage_replace_current_mandate_pointer(
                PRIMARY.case_scope, first, expected=None
            )
        )

    loaded = await repositories.core.load_current_mandate_pointer(
        PRIMARY.case_scope, PRIMARY.mandate_id
    )
    assert loaded == first


async def test_a_pointer_replace_with_a_stale_expectation_is_rejected(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    await storage.write_item(
        repositories.core.stage_replace_current_mandate_pointer(
            PRIMARY.case_scope,
            PRIMARY.mandate_pointer(mandate_version=1, row_version=1),
            expected=None,
        )
    )
    second = PRIMARY.mandate_pointer(mandate_version=2, row_version=2)
    await storage.write_item(
        repositories.core.stage_replace_current_mandate_pointer(
            PRIMARY.case_scope,
            second,
            expected=MandatePointerExpectation(row_version=1, mandate_version=1),
        )
    )

    third = PRIMARY.mandate_pointer(mandate_version=3, row_version=2)
    with pytest.raises(PersistenceConflictError):
        await storage.write_item(
            repositories.core.stage_replace_current_mandate_pointer(
                PRIMARY.case_scope,
                third,
                expected=MandatePointerExpectation(row_version=1, mandate_version=1),
            )
        )

    loaded = await repositories.core.load_current_mandate_pointer(
        PRIMARY.case_scope, PRIMARY.mandate_id
    )
    assert loaded == second


async def test_a_pointer_replace_detects_a_changed_mandate_version(
    storage: StorageDriver,
) -> None:
    """The row version alone is not enough; the pointed-at version is part of the guard."""

    repositories = build_repositories(storage)
    await storage.write_item(
        repositories.core.stage_replace_current_mandate_pointer(
            PRIMARY.case_scope,
            PRIMARY.mandate_pointer(mandate_version=2, row_version=1),
            expected=None,
        )
    )

    with pytest.raises(PersistenceConflictError):
        await storage.write_item(
            repositories.core.stage_replace_current_mandate_pointer(
                PRIMARY.case_scope,
                PRIMARY.mandate_pointer(mandate_version=3, row_version=2),
                expected=MandatePointerExpectation(row_version=1, mandate_version=1),
            )
        )


async def test_mandate_versions_are_immutable_and_ordered(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    for version in (1, 2, 3):
        await storage.write_item(
            repositories.core.stage_append_mandate_version(
                PRIMARY.case_scope, PRIMARY.mandate(version=version)
            )
        )

    with pytest.raises(PersistenceConflictError):
        await storage.write_item(
            repositories.core.stage_append_mandate_version(
                PRIMARY.case_scope, PRIMARY.mandate(version=2)
            )
        )

    loaded = await repositories.core.load_mandate_version(PRIMARY.case_scope, PRIMARY.mandate_id, 2)
    assert loaded.version == 2


async def test_an_evidence_root_is_addressed_by_its_content_hash(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    root = PRIMARY.evidence_root()
    await storage.write_item(
        repositories.core.stage_create_evidence_root(PRIMARY.community_scope, root)
    )

    assert (
        await repositories.core.load_evidence_root(PRIMARY.community_scope, root.root_sha256)
        == root
    )
    assert (
        await repositories.core.load_evidence_root(OTHER_CASE.community_scope, root.root_sha256)
        is None
    )


async def test_an_authorization_read_is_strongly_consistent() -> None:
    """A display read may lag; a read that informs a decision may not."""

    driver = InMemoryStorageDriver(stale_eventual_reads=True)
    repositories = build_repositories(driver)
    await driver.write_item(
        repositories.core.stage_create_case(PRIMARY.case_scope, PRIMARY.case(version=1))
    )
    await driver.write_item(
        repositories.core.stage_update_case(
            PRIMARY.case_scope, PRIMARY.case(version=2), expected_version=1
        )
    )

    assert (await repositories.core.load_case(PRIMARY.case_scope)).version == 2
    assert (await repositories.core.read_case_for_display(PRIMARY.case_scope)).version == 1


async def test_agent_invocation_results_are_scoped_to_their_case(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    result = PRIMARY.agent_invocation()
    await storage.write_item(
        repositories.core.stage_append_agent_invocation(PRIMARY.case_scope, result)
    )

    assert (
        await repositories.core.load_agent_invocation(PRIMARY.case_scope, result.invocation_id)
        == result
    )
    assert (
        await repositories.core.load_agent_invocation(OTHER_CASE.case_scope, result.invocation_id)
        is None
    )


async def test_a_channel_lock_makes_ingestion_idempotent(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    lock = PRIMARY.channel_lock()
    operation = repositories.core.stage_create_channel_lock(PRIMARY.community_scope, lock)

    await storage.write_item(operation)
    with pytest.raises(PersistenceConflictError):
        await storage.write_item(operation)

    assert (
        await repositories.core.load_channel_lock(
            PRIMARY.community_scope,
            adapter=lock.adapter,
            channel_message_id_sha256=lock.channel_message_id_sha256,
        )
        == lock
    )


async def test_assessments_read_in_canonical_time_order(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    assessments = [PRIMARY.assessment(index=index) for index in range(4)]
    for assessment in assessments:
        await storage.write_item(
            repositories.core.stage_append_assessment(PRIMARY.case_scope, assessment)
        )

    page = await repositories.core.read_case_assessments(PRIMARY.case_scope, PageRequest(limit=10))

    assert [item.created_at for item in page.items] == sorted(
        assessment.created_at for assessment in assessments
    )


async def test_records_of_two_worlds_never_collide(storage: StorageDriver) -> None:
    """Identical structures in two namespaces occupy disjoint keys."""

    repositories = build_repositories(storage)
    for world in (PRIMARY, OTHER_NAMESPACE_WORLD):
        await storage.write_item(
            repositories.core.stage_create_case(world.case_scope, world.case())
        )

    primary = await repositories.core.load_case(PRIMARY.case_scope)
    other = await repositories.core.load_case(OTHER_NAMESPACE_WORLD.case_scope)

    assert primary.namespace != other.namespace


async def test_the_three_worlds_are_genuinely_distinct() -> None:
    """The denial tests would be vacuous if the fixture worlds shared identifiers."""

    assert PRIMARY.case_id != OTHER_CASE.case_id
    assert PRIMARY.community_id != OTHER_CASE.community_id
    assert PRIMARY.case_id != OTHER_NAMESPACE_WORLD.case_id
    assert PRIMARY.namespace != OTHER_NAMESPACE_WORLD.namespace
