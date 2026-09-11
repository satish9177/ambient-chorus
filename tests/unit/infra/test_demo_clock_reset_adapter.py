"""The reset principal's fenced clock reseed: bump the generation, never reuse it, fail closed.

[ADR-029](../../../docs/adr/ADR-029-deployed-demo-clock-authority.md) § 3 and deployment
contract §§ 21, 39. Reset is the one legitimate backward transition, and a mechanism that
permits exactly one backward move must guarantee that nothing from before that move can act
afterwards -- that guarantee is ``reset_generation``. Asserted here against the real
:class:`~chorus.infrastructure.dynamodb.demo_clock.DynamoDbDemoClockResetStore` and the normal
:class:`~chorus.infrastructure.dynamodb.demo_clock.DynamoDbDemoClockStore` side by side, over
the in-memory driver that evaluates the same closed conditions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver

from chorus.domain.ids import Namespace
from chorus.infrastructure.dynamodb.demo_clock import (
    ATTR_VERSION,
    DynamoDbDemoClockResetStore,
    DynamoDbDemoClockStore,
    decode_demo_clock,
    demo_clock_key,
    encode_demo_clock,
)
from chorus.ports.demo_clock import (
    DemoClockConflictError,
    DemoClockRecord,
    DemoClockResetConflictError,
    DemoClockUnavailableError,
)
from chorus.ports.errors import ExternalDependencyError, PersistenceConflictError
from chorus.ports.storage import ItemKey, KeyAbsent, PutItem, StorageDriver, StoredItem

NAMESPACE = Namespace("DEMO")
SEED = datetime(2030, 1, 14, 9, 0, tzinfo=UTC)

pytestmark = pytest.mark.anyio


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Any:
    yield from storage_driver(str(request.param), prefix="clock-reset")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def record(**overrides: Any) -> DemoClockRecord:
    values: dict[str, Any] = {
        "logical_time": SEED,
        "version": 1,
        "reset_generation": 1,
        "seed_instant": SEED,
        "advance_count": 0,
    }
    values.update(overrides)
    return DemoClockRecord(**values)


async def seed(driver: StorageDriver, row: DemoClockRecord) -> DemoClockRecord:
    await driver.write_item(
        PutItem(
            key=demo_clock_key(NAMESPACE),
            item=encode_demo_clock(NAMESPACE, row),
            condition=KeyAbsent(),
        )
    )
    return row


def reset_store(driver: StorageDriver) -> DynamoDbDemoClockResetStore:
    return DynamoDbDemoClockResetStore(driver=driver, namespace=NAMESPACE)


def normal_store(driver: StorageDriver) -> DynamoDbDemoClockStore:
    return DynamoDbDemoClockStore(driver=driver, namespace=NAMESPACE)


# -- the generation invariant ----------------------------------------------------------------


async def test_the_reset_bumps_the_generation_and_restores_the_seed(
    storage: StorageDriver,
) -> None:
    current = await seed(
        storage,
        record(
            version=7, reset_generation=3, advance_count=6, logical_time=SEED + timedelta(hours=9)
        ),
    )
    reseeded = await reset_store(storage).reseed(seed_instant=SEED, current=current)

    assert reseeded.reset_generation == 4  # strictly greater, never reused
    assert reseeded.logical_time == SEED
    assert reseeded.seed_instant == SEED
    assert reseeded.advance_count == 0
    assert reseeded.version == 1  # a fresh sequence
    assert await normal_store(storage).read() == reseeded


async def test_a_missing_clock_is_created_at_generation_one(storage: StorageDriver) -> None:
    """ADR-029 § 6: reset may legitimately run before the first advance, or in a fresh deploy."""

    assert await reset_store(storage).read() is None
    reseeded = await reset_store(storage).reseed(seed_instant=SEED, current=None)
    assert reseeded.reset_generation == 1
    assert (await normal_store(storage).read()).reset_generation == 1


async def test_a_corrupt_clock_fails_closed_and_is_not_overwritten(
    storage: StorageDriver,
) -> None:
    item: dict[str, Any] = dict(encode_demo_clock(NAMESPACE, record()))
    item[ATTR_VERSION] = "not a number"
    await storage.write_item(
        PutItem(key=demo_clock_key(NAMESPACE), item=item, condition=KeyAbsent())
    )
    with pytest.raises(DemoClockUnavailableError):
        await reset_store(storage).read()


# -- the stale pre-reset writer, exactly as deployment contract § 21 scripts it -------------


async def test_a_stale_pre_reset_advance_fails_after_the_reset_even_when_the_version_coincides(
    storage: StorageDriver,
) -> None:
    """1. API reads pre-reset: version=N, generation=G1.
    2. reset succeeds: generation=G2, a fresh version sequence.
    3. stale API attempts the old CAS using version=N, generation=G1.
    4. it MUST fail -- the generation fence, not the version, is what rejects it.
    """

    pre_reset = await seed(storage, record(version=1, reset_generation=1))

    reseeded = await reset_store(storage).reseed(seed_instant=SEED, current=pre_reset)
    assert reseeded.reset_generation == 2
    assert reseeded.version == 1  # the numeric version now *coincides* with the stale reading

    with pytest.raises(DemoClockConflictError):
        await normal_store(storage).advance(pre_reset, to=SEED + timedelta(hours=1))
    assert (await normal_store(storage).read()).reset_generation == 2


async def test_a_repeated_reset_is_safe_and_keeps_bumping_the_generation(
    storage: StorageDriver,
) -> None:
    row = await seed(storage, record())
    first = await reset_store(storage).reseed(seed_instant=SEED, current=row)
    second = await reset_store(storage).reseed(seed_instant=SEED, current=first)
    third = await reset_store(storage).reseed(seed_instant=SEED, current=second)

    assert [first.reset_generation, second.reset_generation, third.reset_generation] == [2, 3, 4]
    assert (await normal_store(storage).read()).reset_generation == 4


async def test_a_reseed_against_a_stale_read_loses_deterministically(
    storage: StorageDriver,
) -> None:
    """The lock-acquisition-race window: another reset moved the row first."""

    row = await seed(storage, record())
    await reset_store(storage).reseed(seed_instant=SEED, current=row)  # generation -> 2
    with pytest.raises(DemoClockResetConflictError):
        await reset_store(storage).reseed(seed_instant=SEED, current=row)  # still holds gen 1


# -- typed failures --------------------------------------------------------------------------


async def test_a_storage_failure_on_reseed_is_a_typed_unavailable_error() -> None:
    class Failing:
        async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
            return encode_demo_clock(NAMESPACE, record())

        async def write_item(self, operation: PutItem) -> None:
            raise ExternalDependencyError("WRITE")

    with pytest.raises(DemoClockUnavailableError):
        await DynamoDbDemoClockResetStore(namespace=NAMESPACE, driver=Failing()).reseed(  # type: ignore[arg-type]
            seed_instant=SEED, current=record()
        )


async def test_a_conditional_failure_on_reseed_is_a_reset_conflict_never_a_retry() -> None:
    class Conflicting:
        async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
            return encode_demo_clock(NAMESPACE, record())

        async def write_item(self, operation: PutItem) -> None:
            raise PersistenceConflictError("WRITE")

    with pytest.raises(DemoClockResetConflictError):
        await DynamoDbDemoClockResetStore(namespace=NAMESPACE, driver=Conflicting()).reseed(  # type: ignore[arg-type]
            seed_instant=SEED, current=record()
        )


async def test_the_reseed_read_is_strongly_consistent() -> None:
    recorded: list[bool] = []

    class Recording:
        async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
            recorded.append(consistent)
            return None

    await DynamoDbDemoClockResetStore(namespace=NAMESPACE, driver=Recording()).read()  # type: ignore[arg-type]
    assert recorded == [True]


# -- the normal adapter still exposes no reseed --------------------------------------------


async def test_a_reseeded_row_round_trips_through_the_normal_decoder(
    storage: StorageDriver,
) -> None:
    row = await seed(storage, record())
    reseeded = await reset_store(storage).reseed(seed_instant=SEED, current=row)
    raw = await storage.get_item(demo_clock_key(NAMESPACE), consistent=True)
    assert raw is not None
    assert decode_demo_clock(NAMESPACE, raw) == reseeded


def test_the_normal_store_still_has_no_reset_or_reseed() -> None:
    surface = {name for name in dir(DynamoDbDemoClockStore) if not name.startswith("_")}
    assert surface == {"read", "advance", "driver", "namespace"}
    assert not hasattr(DynamoDbDemoClockStore, "reseed")
