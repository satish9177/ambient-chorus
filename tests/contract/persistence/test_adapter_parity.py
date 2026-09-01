"""Expectations that must hold identically for both adapters.

The in-memory driver exists so the rest of the system can be tested without Docker. That is
only legitimate while it reports the same things the real adapter reports; anywhere the two
could diverge silently is checked here against both.
"""

from __future__ import annotations

import pytest
from tests.fixtures.persistence import NOW, PRIMARY, MovableClock, build_repositories

from chorus.domain.ids import FactId
from chorus.infrastructure.dynamodb import codec_case, codec_share
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.ports.errors import (
    PersistenceConflictError,
    PersistenceErrorCode,
    TransactionLimitExceededError,
)
from chorus.ports.limits import TRANSACTION_MAX_OPERATIONS, TRANSACTION_TOKEN_WINDOW_SECONDS
from chorus.ports.pagination import PageRequest
from chorus.ports.storage import KeyAbsent, PutItem, StorageDriver
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio


async def test_a_conflicting_write_reports_the_operation_not_the_item(
    storage: StorageDriver,
) -> None:
    """An error must not disclose which item lost a race."""

    repositories = build_repositories(storage)
    operation = repositories.core.stage_create_fact(PRIMARY.case_scope, PRIMARY.fact())
    await storage.write_item(operation)

    with pytest.raises(PersistenceConflictError) as raised:
        await storage.write_item(operation)

    assert raised.value.code is PersistenceErrorCode.PERSISTENCE_CONFLICT
    assert raised.value.entity_ref == "WRITE"
    assert str(PRIMARY.fact_id) not in str(raised.value)


async def test_an_identical_transaction_replay_is_deduplicated(
    storage: StorageDriver,
) -> None:
    """Resubmitting one transaction is a replay, not a conflict, under both adapters."""

    repositories = build_repositories(storage)
    plan = TransactionPlan(
        name="create-case",
        operations=(
            repositories.core.stage_create_case(PRIMARY.case_scope, PRIMARY.case()),
            repositories.audit.stage_append_case_event(PRIMARY.case_scope, PRIMARY.audit_event()),
        ),
        audit_required=True,
    )

    await repositories.unit_of_work.commit(plan)
    await repositories.unit_of_work.commit(plan)

    page = await repositories.audit.read_case_events(PRIMARY.case_scope, PageRequest(limit=10))
    assert len(page.items) == 1


async def test_a_conflicting_transaction_reports_the_transaction(
    storage: StorageDriver,
) -> None:
    """A distinct transaction that loses a condition names the transaction, not the item."""

    repositories = build_repositories(storage)
    await storage.write_item(
        repositories.core.stage_create_case(PRIMARY.case_scope, PRIMARY.case())
    )
    plan = TransactionPlan(
        name="create-case",
        operations=(
            repositories.core.stage_create_case(PRIMARY.case_scope, PRIMARY.case()),
            repositories.audit.stage_append_case_event(PRIMARY.case_scope, PRIMARY.audit_event()),
        ),
        audit_required=True,
    )

    with pytest.raises(PersistenceConflictError) as raised:
        await repositories.unit_of_work.commit(plan)

    assert raised.value.entity_ref == "TRANSACTION"
    assert str(PRIMARY.case_id) not in str(raised.value)


async def test_an_oversized_transaction_is_rejected_the_same_way(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    operations = tuple(
        repositories.core.stage_create_fact(
            PRIMARY.case_scope, PRIMARY.fact(fact_id=FactId(PRIMARY.uuid(f"over:{index}")))
        )
        for index in range(TRANSACTION_MAX_OPERATIONS + 1)
    )

    with pytest.raises(TransactionLimitExceededError) as raised:
        await storage.transact_write(operations, client_request_token="token")

    assert raised.value.entity_ref == "TRANSACTION"


async def test_a_stored_item_must_carry_the_key_it_is_written_to(
    storage: StorageDriver,
) -> None:
    """DynamoDB derives an item's address from its own key attributes."""

    key = PRIMARY.case_scope
    operation = build_repositories(storage).core.stage_create_fact(key, PRIMARY.fact())
    misaddressed = PutItem(
        key=operation.key,
        item={**operation.item, "PK": "NS#TEST_PERSISTENCE#CASE#somewhere-else"},
        condition=KeyAbsent(),
    )

    with pytest.raises(ValueError, match="key attributes"):
        await storage.write_item(misaddressed)


async def test_a_full_page_always_offers_a_continuation(storage: StorageDriver) -> None:
    """A cursor means "the read stopped at the limit", never "more items certainly exist"."""

    repositories = build_repositories(storage)
    for index in range(2):
        await storage.write_item(
            repositories.core.stage_create_fact(
                PRIMARY.case_scope, PRIMARY.fact(fact_id=FactId(PRIMARY.uuid(f"page:{index}")))
            )
        )

    first = await repositories.core.read_case_facts(PRIMARY.case_scope, PageRequest(limit=2))

    assert len(first.items) == 2
    assert first.next_cursor is not None
    second = await repositories.core.read_case_facts(
        PRIMARY.case_scope, PageRequest(limit=2, cursor=first.next_cursor)
    )
    assert second.items == ()
    assert second.next_cursor is None


async def test_a_partial_page_ends_the_read(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    await storage.write_item(
        repositories.core.stage_create_fact(PRIMARY.case_scope, PRIMARY.fact())
    )

    page = await repositories.core.read_case_facts(PRIMARY.case_scope, PageRequest(limit=10))

    assert len(page.items) == 1
    assert page.next_cursor is None


async def test_a_batch_read_addresses_exactly_one_table(storage: StorageDriver) -> None:
    """One BatchGetItem entry names one table, so a mixed batch is a caller error.

    The DynamoDB adapter has always rejected this locally. The emulator used to accept it,
    which meant a repository that mixed tables would have passed every offline test and failed
    only against the real service.
    """

    core_key = codec_case.fact_key(PRIMARY.case_scope, PRIMARY.fact_id)
    share_key = codec_share.commitment_key(PRIMARY.case_scope, PRIMARY.commitment_id)

    with pytest.raises(ValueError, match="exactly one table"):
        await storage.batch_get_items((core_key, share_key), consistent=True)


async def test_an_empty_batch_reads_nothing_in_both_adapters(storage: StorageDriver) -> None:
    assert await storage.batch_get_items((), consistent=True) == ()


def _plan() -> TransactionPlan:
    repositories = build_repositories(InMemoryStorageDriver())
    return TransactionPlan(
        name="token-window",
        operations=(repositories.core.stage_create_case(PRIMARY.case_scope, PRIMARY.case()),),
        audit_required=False,
    )


async def test_a_transaction_token_still_deduplicates_inside_its_window() -> None:
    """Inside the window a repeated identical request is absorbed, as DynamoDB absorbs it."""

    clock = MovableClock(NOW)
    driver = InMemoryStorageDriver(clock=clock)
    plan = _plan()

    await driver.transact_write(plan.operations, client_request_token=plan.client_request_token)
    clock.advance(seconds=TRANSACTION_TOKEN_WINDOW_SECONDS - 1)
    await driver.transact_write(plan.operations, client_request_token=plan.client_request_token)


async def test_a_transaction_token_stops_deduplicating_at_exactly_its_window() -> None:
    """The boundary itself is outside the window, matching every other deadline in CHORUS.

    ``now < deadline`` is live and ``now >= deadline`` has passed, the same rule the send
    fence uses. Pinning the exact second stops the emulator drifting a second either way from
    the service it stands in for.
    """

    clock = MovableClock(NOW)
    driver = InMemoryStorageDriver(clock=clock)
    plan = _plan()

    await driver.transact_write(plan.operations, client_request_token=plan.client_request_token)
    clock.advance(seconds=TRANSACTION_TOKEN_WINDOW_SECONDS)

    with pytest.raises(PersistenceConflictError):
        await driver.transact_write(plan.operations, client_request_token=plan.client_request_token)


async def test_a_transaction_token_stops_deduplicating_after_its_window() -> None:
    """The emulator must forget a token exactly when the real service forgets it.

    Real time cannot be advanced against DynamoDB Local, so this pins the emulator to the
    documented ten-minute window rather than running there. What matters is that no test can
    come to rely on deduplication the real service would no longer provide: once the token
    lapses, only the plan's own create-only condition stops the request applying twice, and
    that is what the caller sees here.
    """

    clock = MovableClock(NOW)
    driver = InMemoryStorageDriver(clock=clock)
    plan = _plan()

    await driver.transact_write(plan.operations, client_request_token=plan.client_request_token)
    clock.advance(seconds=TRANSACTION_TOKEN_WINDOW_SECONDS + 1)

    with pytest.raises(PersistenceConflictError):
        await driver.transact_write(plan.operations, client_request_token=plan.client_request_token)
