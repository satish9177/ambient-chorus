"""The deployed demo reset's Core ``NS#DEMO`` control plane: manifest, lock, receipt, purge.

Review R2. Asserts the adapter behaviours the orchestration depends on: the manifest round-trips
and fails closed on an unexpected attribute; the lock is conditional, recovers a stale holder,
and refuses a non-owner release; the receipt is create-only and idempotent; the partition purge
is bounded, keeps the control rows, and refuses a partition outside the DEMO namespace.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from chorus.domain.errors import IntegrityError
from chorus.domain.ids import Namespace
from chorus.infrastructure.dynamodb import keys
from chorus.infrastructure.dynamodb.demo_reset_store import (
    DynamoDbDemoManifestRegistrar,
    DynamoDbDemoManifestStore,
    DynamoDbDemoPartitionPurge,
    DynamoDbDemoResetLock,
    DynamoDbDemoResetReceiptStore,
    decode_demo_manifest,
    encode_demo_manifest,
)
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.ports.demo_reset import (
    DemoManifest,
    DemoManifestUnavailableError,
    DemoResetLockContendedError,
    DemoResetLockHandle,
    DemoResetLockNotOwnedError,
    DemoResetReceipt,
)
from chorus.ports.storage import ItemKey, KeyAbsent, PutItem, TableName

pytestmark = pytest.mark.anyio

NS = Namespace("DEMO")
T0 = datetime(2030, 1, 14, 9, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def driver() -> InMemoryStorageDriver:
    return InMemoryStorageDriver()


def _manifest(**over: Any) -> DemoManifest:
    values: dict[str, Any] = {
        "seed_version": "elevator/v1",
        "created_at": T0,
        "version": 1,
        "partition_keys": ("NS#DEMO", "NS#DEMO#COMM#c"),
        "control_sort_prefixes": keys.DEMO_RESET_CONTROL_SORT_PREFIXES,
        "private_object_prefixes": ("ns/DEMO/",),
        "export_object_prefixes": ("ns/DEMO/",),
        "schedule_name_prefix": "chorus-demo-",
    }
    values.update(over)
    return DemoManifest(**values)


# -- manifest ------------------------------------------------------------------------------


def test_a_manifest_round_trips_through_its_codec() -> None:
    manifest = _manifest()
    assert decode_demo_manifest(NS, encode_demo_manifest(NS, manifest)) == manifest


def test_a_manifest_row_with_an_unexpected_attribute_is_corrupt() -> None:
    item: dict[str, Any] = dict(encode_demo_manifest(NS, _manifest()))
    item["surprise"] = "x"
    with pytest.raises((IntegrityError, ValueError)):
        decode_demo_manifest(NS, item)


def test_a_manifest_partition_key_outside_the_demo_namespace_is_refused() -> None:
    with pytest.raises(ValueError, match="DEMO namespace"):
        _manifest(partition_keys=("NS#DEMO", "NS#OTHER#CASE#x"))


async def test_the_store_writes_and_reads_under_optimistic_concurrency(
    driver: InMemoryStorageDriver,
) -> None:
    store = DynamoDbDemoManifestStore(driver=driver, namespace=NS, seed_version="elevator/v1")
    assert await store.load() is None
    await store.put(_manifest(version=1), expected_version=None)
    stored = await store.load()
    assert stored is not None and stored.version == 1
    await store.put(_manifest(version=2), expected_version=1)
    with pytest.raises(DemoManifestUnavailableError):
        await store.put(_manifest(version=9), expected_version=1)  # stale expected version


async def test_a_corrupt_manifest_row_makes_load_fail_closed(
    driver: InMemoryStorageDriver,
) -> None:
    await driver.write_item(
        PutItem(
            key=ItemKey(
                table=TableName.CORE,
                partition_key="NS#DEMO",
                sort_key=keys.demo_manifest_sort_key("elevator/v1"),
            ),
            item={
                "PK": "NS#DEMO",
                "SK": keys.demo_manifest_sort_key("elevator/v1"),
                "entity_type": "DEMO_MANIFEST",
                "schema_version": "demo-manifest/v1",
                "namespace": "DEMO",
                "community_id": None,
                "case_id": None,
                "junk": 1,
            },
            condition=KeyAbsent(),
        )
    )
    store = DynamoDbDemoManifestStore(driver=driver, namespace=NS, seed_version="elevator/v1")
    with pytest.raises(DemoManifestUnavailableError):
        await store.load()


async def test_the_registrar_records_a_partition_marker_idempotently(
    driver: InMemoryStorageDriver,
) -> None:
    registrar = DynamoDbDemoManifestRegistrar(driver=driver, namespace=NS)

    await registrar.register_partition("NS#DEMO#OPERATION#abc")
    await registrar.register_partition("NS#DEMO#OPERATION#abc")  # idempotent, not an overwrite
    await registrar.register_partition("NS#DEMO#OPERATION#def")

    assert set(await registrar.registered_partition_keys()) == {
        "NS#DEMO#OPERATION#abc",
        "NS#DEMO#OPERATION#def",
    }

    with pytest.raises(IntegrityError):
        await registrar.register_partition("NS#OTHER#OPERATION#x")


async def test_the_registration_operation_is_a_create_only_put_for_the_marker(
    driver: InMemoryStorageDriver,
) -> None:
    registrar = DynamoDbDemoManifestRegistrar(driver=driver, namespace=NS)
    operation = registrar.registration_operation("NS#DEMO#OPERATION#abc")

    assert isinstance(operation, PutItem)
    assert isinstance(operation.condition, KeyAbsent)
    assert operation.key.table == TableName.CORE
    assert operation.key.partition_key == keys.demo_control_partition(NS)
    assert operation.key.sort_key.startswith(keys.DEMO_REGISTERED_PARTITION_SORT_KEY_PREFIX)

    # applying it makes the marker visible to the enumeration the reset uses
    await driver.write_item(operation)
    assert await registrar.registered_partition_keys() == ("NS#DEMO#OPERATION#abc",)


# -- lock ---------------------------------------------------------------------------------


async def test_the_lock_is_conditional_and_a_second_owner_is_contended(
    driver: InMemoryStorageDriver,
) -> None:
    lock = DynamoDbDemoResetLock(driver=driver, namespace=NS)
    await lock.acquire(owner_token="a", now=T0, ttl_seconds=300)
    held = await lock.read()
    assert held is not None and held.owner_token == "a"
    with pytest.raises(DemoResetLockContendedError):
        await lock.acquire(owner_token="b", now=T0 + timedelta(seconds=1), ttl_seconds=300)


async def test_a_stale_lock_can_be_recovered_by_a_new_owner(
    driver: InMemoryStorageDriver,
) -> None:
    lock = DynamoDbDemoResetLock(driver=driver, namespace=NS)
    await lock.acquire(owner_token="a", now=T0, ttl_seconds=60)
    # well past expiry
    handle = await lock.acquire(owner_token="b", now=T0 + timedelta(seconds=120), ttl_seconds=300)
    assert handle.owner_token == "b"
    held = await lock.read()
    assert held is not None and held.owner_token == "b"


async def test_only_the_owner_can_release_the_lock(driver: InMemoryStorageDriver) -> None:
    lock = DynamoDbDemoResetLock(driver=driver, namespace=NS)
    handle = await lock.acquire(owner_token="a", now=T0, ttl_seconds=300)
    stranger = DemoResetLockHandle(
        owner_token="b", acquired_at=T0, expires_at=T0 + timedelta(seconds=300)
    )
    with pytest.raises(DemoResetLockNotOwnedError):
        await lock.release(stranger)
    await lock.release(handle)
    assert await lock.read() is None


# -- receipt ----------------------------------------------------------------------------


async def test_the_receipt_store_is_create_only_and_idempotent(
    driver: InMemoryStorageDriver,
) -> None:
    store = DynamoDbDemoResetReceiptStore(driver=driver, namespace=NS)
    receipt = DemoResetReceipt(
        idempotency_key="op-1",
        request_fingerprint="DEMO\x1fRESET DEMO\x1felevator/v1",
        recorded_at=T0,
        result_json='{"reset_id": "x"}',
    )
    await store.put(receipt)
    await store.put(receipt)  # idempotent no-op, not an overwrite error
    loaded = await store.load("op-1")
    assert loaded is not None
    assert loaded.request_fingerprint == receipt.request_fingerprint
    assert await store.load("op-unknown") is None


# -- bounded partition purge ------------------------------------------------------------


async def _put(driver: InMemoryStorageDriver, table: TableName, pk: str, sk: str) -> None:
    await driver.write_item(
        PutItem(
            key=ItemKey(table=table, partition_key=pk, sort_key=sk),
            item={"PK": pk, "SK": sk, "entity_type": "x", "schema_version": "x"},
            condition=KeyAbsent(),
        )
    )


async def test_the_purge_deletes_a_partition_but_keeps_the_control_rows(
    driver: InMemoryStorageDriver,
) -> None:
    await _put(driver, TableName.CORE, "NS#DEMO", "COMMUNITY#c")
    await _put(driver, TableName.CORE, "NS#DEMO", keys.demo_manifest_sort_key("elevator/v1"))
    await _put(driver, TableName.CORE, "NS#DEMO", keys.DEMO_RESET_LOCK_SORT_KEY)

    purge = DynamoDbDemoPartitionPurge(driver=driver)
    from chorus.infrastructure.dynamodb.demo_mutation import reset_authority

    owner = reset_authority.set("test-reset-owner")
    try:
        deleted = await purge.delete_partition(
            table="CORE",
            partition_key="NS#DEMO",
            keep_sort_prefixes=keys.DEMO_RESET_CONTROL_SORT_PREFIXES,
        )
    finally:
        reset_authority.reset(owner)
    assert deleted == 1  # only COMMUNITY#c
    remaining = await purge.partition_item_sort_keys(table="CORE", partition_key="NS#DEMO")
    assert set(remaining) == {
        keys.demo_manifest_sort_key("elevator/v1"),
        keys.DEMO_RESET_LOCK_SORT_KEY,
    }


async def test_the_purge_refuses_a_partition_outside_the_demo_namespace(
    driver: InMemoryStorageDriver,
) -> None:
    purge = DynamoDbDemoPartitionPurge(driver=driver)
    with pytest.raises(IntegrityError):
        await purge.delete_partition(table="CORE", partition_key="NS#DEMO2#CASE#x")
    with pytest.raises(IntegrityError):
        await purge.partition_item_sort_keys(table="CORE", partition_key="NS#OTHER")
