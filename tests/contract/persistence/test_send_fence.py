"""Send-fence contract: one live send per case, and revocation ordered against it.

The fence is the primitive that makes the frozen ordering rule enforceable: while a send
attempt holds the fence, an authorization-sensitive mutation that carries the fence check
cannot commit. The fence itself never sends anything.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from tests.fixtures.persistence import NOW, OTHER_CASE, PRIMARY, build_repositories

from chorus.domain.ids import ExecutionId
from chorus.domain.time import epoch_micros, epoch_seconds_ceiling
from chorus.infrastructure.dynamodb import codec_fence, keys
from chorus.infrastructure.dynamodb.codec import ATTR_EXPIRES_AT_EPOCH
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.pagination import PageRequest
from chorus.ports.records import SendFence
from chorus.ports.storage import (
    CheckItem,
    DeleteItem,
    ItemKey,
    KeyPresent,
    QueryRequest,
    StorageDriver,
    TableName,
)
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio

OTHER_EXECUTION = ExecutionId(PRIMARY.uuid("execution:other"))


async def test_a_fence_is_acquired_and_read_back(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    fence = PRIMARY.send_fence()

    acquired = await repositories.core.acquire_send_fence(PRIMARY.case_scope, fence)

    assert acquired == fence
    assert await repositories.core.load_send_fence(PRIMARY.case_scope) == fence


async def test_a_second_execution_cannot_take_a_live_fence(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    await repositories.core.acquire_send_fence(PRIMARY.case_scope, PRIMARY.send_fence())

    with pytest.raises(PersistenceConflictError):
        await repositories.core.acquire_send_fence(
            PRIMARY.case_scope,
            PRIMARY.send_fence(
                execution_id=OTHER_EXECUTION, acquired_at=NOW + timedelta(seconds=1)
            ),
        )


async def test_the_same_execution_replays_its_own_fence_without_extending_it(
    storage: StorageDriver,
) -> None:
    """A retry inside one execution must not silently widen its authorization window."""

    repositories = build_repositories(storage)
    original = PRIMARY.send_fence()
    await repositories.core.acquire_send_fence(PRIMARY.case_scope, original)

    replayed = await repositories.core.acquire_send_fence(
        PRIMARY.case_scope,
        PRIMARY.send_fence(acquired_at=NOW + timedelta(minutes=1)),
    )

    assert replayed == original
    assert replayed.expires_at == original.expires_at


async def test_an_expired_fence_can_be_taken_over(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    expired = PRIMARY.send_fence(
        acquired_at=NOW - timedelta(hours=1), lifetime=timedelta(minutes=5)
    )
    await repositories.core.acquire_send_fence(PRIMARY.case_scope, expired)

    successor = PRIMARY.send_fence(execution_id=OTHER_EXECUTION, acquired_at=NOW)
    acquired = await repositories.core.acquire_send_fence(PRIMARY.case_scope, successor)

    assert acquired == successor
    assert (await repositories.core.load_send_fence(PRIMARY.case_scope)) == successor


async def test_only_the_holder_may_release_a_fence(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    fence = PRIMARY.send_fence()
    await repositories.core.acquire_send_fence(PRIMARY.case_scope, fence)

    with pytest.raises(PersistenceConflictError):
        await repositories.core.release_send_fence(PRIMARY.case_scope, OTHER_EXECUTION)
    assert await repositories.core.load_send_fence(PRIMARY.case_scope) == fence

    await repositories.core.release_send_fence(PRIMARY.case_scope, fence.execution_id)
    assert await repositories.core.load_send_fence(PRIMARY.case_scope) is None


async def test_a_live_fence_blocks_an_authorization_sensitive_mutation(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    await repositories.core.acquire_send_fence(PRIMARY.case_scope, PRIMARY.send_fence())
    revocation = TransactionPlan(
        name="revoke-mandate",
        operations=(
            repositories.core.stage_require_no_live_send_fence(PRIMARY.case_scope, now=NOW),
            repositories.core.stage_append_mandate_version(
                PRIMARY.case_scope, PRIMARY.mandate(version=2)
            ),
            repositories.audit.stage_append_case_event(PRIMARY.case_scope, PRIMARY.audit_event()),
        ),
        audit_required=True,
    )

    with pytest.raises(PersistenceConflictError):
        await repositories.unit_of_work.commit(revocation)


async def test_no_fence_permits_an_authorization_sensitive_mutation(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    revocation = TransactionPlan(
        name="revoke-mandate",
        operations=(
            repositories.core.stage_require_no_live_send_fence(PRIMARY.case_scope, now=NOW),
            repositories.core.stage_append_mandate_version(
                PRIMARY.case_scope, PRIMARY.mandate(version=2)
            ),
            repositories.audit.stage_append_case_event(PRIMARY.case_scope, PRIMARY.audit_event()),
        ),
        audit_required=True,
    )

    await repositories.unit_of_work.commit(revocation)

    assert (
        await repositories.core.load_mandate_version(PRIMARY.case_scope, PRIMARY.mandate_id, 2)
    ).version == 2


async def test_an_expired_fence_no_longer_blocks_a_mutation(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    await repositories.core.acquire_send_fence(
        PRIMARY.case_scope,
        PRIMARY.send_fence(acquired_at=NOW - timedelta(hours=2), lifetime=timedelta(minutes=5)),
    )
    revocation = TransactionPlan(
        name="revoke-mandate",
        operations=(
            repositories.core.stage_require_no_live_send_fence(PRIMARY.case_scope, now=NOW),
            repositories.core.stage_append_mandate_version(
                PRIMARY.case_scope, PRIMARY.mandate(version=2)
            ),
            repositories.audit.stage_append_case_event(PRIMARY.case_scope, PRIMARY.audit_event()),
        ),
        audit_required=True,
    )

    await repositories.unit_of_work.commit(revocation)


async def test_a_fence_is_scoped_to_one_case(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    await repositories.core.acquire_send_fence(PRIMARY.case_scope, PRIMARY.send_fence())

    assert await repositories.core.load_send_fence(OTHER_CASE.case_scope) is None
    other = await repositories.core.acquire_send_fence(
        OTHER_CASE.case_scope, OTHER_CASE.send_fence()
    )
    assert other.case_id == OTHER_CASE.case_id


MICROSECOND = timedelta(microseconds=1)


def fence_expiring_at(
    expires_at: datetime, *, execution_id: ExecutionId | None = None
) -> SendFence:
    """A fence held by one execution, expiring at an exact instant."""

    fence = PRIMARY.send_fence()
    return replace(
        fence,
        expires_at=expires_at,
        execution_id=fence.execution_id if execution_id is None else execution_id,
    )


async def test_a_fence_is_live_one_microsecond_before_it_expires(
    storage: StorageDriver,
) -> None:
    """Second-granularity comparison would round the deadline down and lose this."""

    repositories = build_repositories(storage)
    expiry = NOW + timedelta(seconds=60, microseconds=500_000)
    await repositories.core.acquire_send_fence(PRIMARY.case_scope, fence_expiring_at(expiry))

    with pytest.raises(PersistenceConflictError):
        await repositories.core.acquire_send_fence(
            PRIMARY.case_scope,
            replace(
                fence_expiring_at(expiry + timedelta(seconds=60), execution_id=OTHER_EXECUTION),
                acquired_at=expiry - MICROSECOND,
            ),
        )


async def test_a_fence_is_expired_exactly_at_its_deadline(storage: StorageDriver) -> None:
    """Equality is expired, matching the frozen ``now < expires_at`` rule."""

    repositories = build_repositories(storage)
    expiry = NOW + timedelta(seconds=60, microseconds=500_000)
    await repositories.core.acquire_send_fence(PRIMARY.case_scope, fence_expiring_at(expiry))

    taken = await repositories.core.acquire_send_fence(
        PRIMARY.case_scope,
        replace(
            fence_expiring_at(expiry + timedelta(seconds=60), execution_id=OTHER_EXECUTION),
            acquired_at=expiry,
        ),
    )

    assert taken.execution_id == OTHER_EXECUTION


async def test_a_fence_is_expired_one_microsecond_after_its_deadline(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    expiry = NOW + timedelta(seconds=60, microseconds=500_000)
    await repositories.core.acquire_send_fence(PRIMARY.case_scope, fence_expiring_at(expiry))

    taken = await repositories.core.acquire_send_fence(
        PRIMARY.case_scope,
        replace(
            fence_expiring_at(expiry + timedelta(seconds=60), execution_id=OTHER_EXECUTION),
            acquired_at=expiry + MICROSECOND,
        ),
    )

    assert taken.execution_id == OTHER_EXECUTION


async def test_a_revocation_cannot_win_a_sub_second_race_against_a_live_fence(
    storage: StorageDriver,
) -> None:
    """The ordering guarantee has to hold inside the second, not only across seconds."""

    repositories = build_repositories(storage)
    expiry = NOW + timedelta(seconds=60, microseconds=500_000)
    await repositories.core.acquire_send_fence(PRIMARY.case_scope, fence_expiring_at(expiry))
    plan = TransactionPlan(
        name="revoke",
        operations=(
            repositories.core.stage_require_no_live_send_fence(
                PRIMARY.case_scope, now=expiry - MICROSECOND
            ),
            repositories.audit.stage_append_case_event(PRIMARY.case_scope, PRIMARY.audit_event()),
        ),
        audit_required=True,
    )

    with pytest.raises(PersistenceConflictError):
        await repositories.unit_of_work.commit(plan)


async def test_a_revocation_proceeds_from_the_exact_expiry_instant(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    expiry = NOW + timedelta(seconds=60, microseconds=500_000)
    await repositories.core.acquire_send_fence(PRIMARY.case_scope, fence_expiring_at(expiry))
    plan = TransactionPlan(
        name="revoke",
        operations=(
            repositories.core.stage_require_no_live_send_fence(PRIMARY.case_scope, now=expiry),
            repositories.audit.stage_append_case_event(PRIMARY.case_scope, PRIMARY.audit_event()),
        ),
        audit_required=True,
    )

    await repositories.unit_of_work.commit(plan)

    page = await repositories.audit.read_case_events(PRIMARY.case_scope, PageRequest(limit=10))
    assert len(page.items) == 1


async def test_the_ttl_field_is_never_what_authorization_compares(
    storage: StorageDriver,
) -> None:
    """TTL cleanup and the authorization deadline are separate fields with separate units."""

    repositories = build_repositories(storage)
    expiry = NOW + timedelta(seconds=60, microseconds=500_000)
    fence = fence_expiring_at(expiry)
    await repositories.core.acquire_send_fence(PRIMARY.case_scope, fence)

    stored = await storage.get_item(codec_fence.send_fence_key(PRIMARY.case_scope), consistent=True)
    assert stored is not None
    assert stored[codec_fence.ATTR_FENCE_EXPIRES_AT_MICROS] == epoch_micros(expiry)
    # The TTL value rounds up, so it never names an instant before the real deadline.
    assert stored[ATTR_EXPIRES_AT_EPOCH] == epoch_seconds_ceiling(expiry)
    assert stored[ATTR_EXPIRES_AT_EPOCH] != epoch_micros(expiry)


# -- ADR-019: the fence has its own partition ---------------------------------------------


async def test_the_fence_is_stored_outside_the_case_partition(storage: StorageDriver) -> None:
    """The separation, proved against the item that actually lands in storage.

    This is the security property ADR-019 exists for: ``dynamodb:LeadingKeys`` constrains the
    partition key alone, so a fence sharing the case partition cannot be granted to the compiler
    without granting the case row, its facts, its reports and its mandates with it.
    """

    repository = build_repositories(storage).core
    scope = PRIMARY.case_scope
    await repository.acquire_send_fence(scope, PRIMARY.send_fence())

    key = codec_fence.send_fence_key(scope)
    assert key.partition_key == keys.fence_partition(scope.namespace, scope.case_id)
    assert key.partition_key != keys.case_partition(scope.namespace, scope.case_id)
    assert key.sort_key == "SEND_FENCE"

    stored = await storage.get_item(key, consistent=True)
    assert stored is not None
    assert stored["PK"] == f"NS#{scope.namespace.value}#FENCE#{scope.case_id}"


async def test_a_fence_write_leaves_the_case_partition_untouched(
    storage: StorageDriver,
) -> None:
    """Acquiring a fence must not add a row to the partition it used to live in."""

    repository = build_repositories(storage).core
    scope = PRIMARY.case_scope
    case_partition = keys.case_partition(scope.namespace, scope.case_id)

    await repository.acquire_send_fence(scope, PRIMARY.send_fence())

    page = await storage.query(
        QueryRequest(table=TableName.CORE, partition_key=case_partition, consistent=True)
    )
    assert [item["SK"] for item in page.items] == []


async def test_a_transaction_can_check_the_case_and_the_fence_together(
    storage: StorageDriver,
) -> None:
    """The two guards now live in different partitions, and that changes nothing.

    ``TransactWriteItems`` spans partitions and tables within one account and Region, so the
    compile transaction stages its case-version check and its no-live-fence check exactly as
    before. The move is physical, not semantic.
    """

    repository = build_repositories(storage).core
    unit_of_work = StorageUnitOfWork(driver=storage)
    scope = PRIMARY.case_scope
    case = PRIMARY.case()
    await unit_of_work.commit(
        TransactionPlan(
            name="seed-case",
            operations=(repository.stage_create_case(scope, case),),
            audit_required=False,
        )
    )

    plan = TransactionPlan(
        name="two-partition-guards",
        operations=(
            repository.stage_require_case_version(scope, expected_version=case.version),
            repository.stage_require_no_live_send_fence(scope, now=NOW),
            repository.stage_create_report(scope, PRIMARY.report()),
        ),
        audit_required=False,
    )
    await unit_of_work.commit(plan)

    checks = [operation for operation in plan.operations if isinstance(operation, CheckItem)]
    assert {check.key.partition_key for check in checks} == {
        keys.case_partition(scope.namespace, scope.case_id),
        keys.fence_partition(scope.namespace, scope.case_id),
    }


async def test_clearing_the_fence_partition_leaves_the_case_and_permits_a_new_acquire(
    storage: StorageDriver,
) -> None:
    """The reset regression, expressed against the partition a reset would target.

    ``chorus-demo reset`` itself is Phase-11 work and does not exist yet, so what is proved here
    is the property that command will rely on: the fence has its own partition root, deleting it
    removes the fence and nothing else, and a later execution can acquire normally afterwards.
    ADR-019 records that the reset manifest must include this root.
    """

    repository = build_repositories(storage).core
    unit_of_work = StorageUnitOfWork(driver=storage)
    scope = PRIMARY.case_scope
    case = PRIMARY.case()
    await unit_of_work.commit(
        TransactionPlan(
            name="seed-case",
            operations=(repository.stage_create_case(scope, case),),
            audit_required=False,
        )
    )
    await repository.acquire_send_fence(scope, PRIMARY.send_fence())
    assert await repository.load_send_fence(scope) is not None

    # What a reset does to this root: delete every item under the fence partition.
    fence_partition = keys.fence_partition(scope.namespace, scope.case_id)
    page = await storage.query(
        QueryRequest(table=TableName.CORE, partition_key=fence_partition, consistent=True)
    )
    for item in page.items:
        await storage.write_item(
            DeleteItem(
                key=ItemKey(
                    table=TableName.CORE,
                    partition_key=fence_partition,
                    sort_key=str(item["SK"]),
                ),
                condition=KeyPresent(),
            )
        )

    assert await repository.load_send_fence(scope) is None
    # The case survived the reset of the fence root.
    assert (await repository.load_case(scope)).version == case.version
    # And a different execution can acquire cleanly afterwards.
    successor = replace(
        PRIMARY.send_fence(), execution_id=ExecutionId(PRIMARY.uuid("execution:successor"))
    )
    acquired = await repository.acquire_send_fence(scope, successor)
    assert acquired.execution_id == successor.execution_id
