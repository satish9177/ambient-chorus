"""The deployed demo clock: read it strongly, move it forward once, or fail closed.

[ADR-029](../../../docs/adr/ADR-029-deployed-demo-clock-authority.md) makes three claims that
are only worth as much as the tests behind them: the read is strongly consistent on every path,
the forward rule is a **condition the store evaluates** rather than a check a process performs,
and every failure is a refusal rather than a fallback. Each is asserted here against the real
adapter -- the in-memory driver evaluates the same closed conditions the DynamoDB renderer
turns into an expression, and a stub driver covers the paths a working store never reaches.

The reset exception is asserted by **absence**: restoring the seed instant is the one legitimate
backward transition, it belongs to the dedicated reset principal behind ``DEMO_RESET_LOCK``, and
:func:`test_the_normal_adapter_exposes_no_reset_or_reseed` fails the moment this class grows a
way to do it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver

from chorus.domain.ids import Namespace
from chorus.domain.time import epoch_micros
from chorus.infrastructure.dynamodb.demo_clock import (
    ATTR_LOGICAL_TIME_MICROS,
    ATTR_RESET_GENERATION,
    ATTR_VERSION,
    DynamoDbDemoClockStore,
    decode_demo_clock,
    demo_clock_key,
    encode_demo_clock,
)
from chorus.ports.demo_clock import (
    DemoClockConflictError,
    DemoClockNotAdvancedError,
    DemoClockRecord,
    DemoClockUnavailableError,
)
from chorus.ports.errors import ExternalDependencyError, PersistenceConflictError
from chorus.ports.storage import (
    ItemKey,
    KeyAbsent,
    PutItem,
    StorageDriver,
    StoredItem,
    TableName,
)

NAMESPACE = Namespace("DEMO")
SEED = datetime(2030, 1, 14, 9, 0, tzinfo=UTC)

pytestmark = pytest.mark.anyio


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Any:
    yield from storage_driver(str(request.param), prefix="clock")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def seed_record(**overrides: Any) -> DemoClockRecord:
    values: dict[str, Any] = {
        "logical_time": SEED,
        "version": 1,
        "reset_generation": 1,
        "seed_instant": SEED,
        "advance_count": 0,
    }
    values.update(overrides)
    return DemoClockRecord(**values)


async def seed(driver: StorageDriver, record: DemoClockRecord | None = None) -> DemoClockRecord:
    """Write the clock row the reset principal would have written."""

    written = record or seed_record()
    await driver.write_item(
        PutItem(
            key=demo_clock_key(NAMESPACE),
            item=encode_demo_clock(NAMESPACE, written),
            condition=KeyAbsent(),
        )
    )
    return written


def store(driver: StorageDriver) -> DynamoDbDemoClockStore:
    return DynamoDbDemoClockStore(driver=driver, namespace=NAMESPACE)


# -- addressing and encoding -------------------------------------------------------------


def test_the_clock_lives_at_the_exact_literal_partition() -> None:
    """One partition, one item, in the **Shareable** table -- never the Core manifest."""

    key = demo_clock_key(NAMESPACE)
    assert key.table is TableName.SHAREABLE
    assert key.partition_key == "NS#DEMO#CLOCK"
    assert key.sort_key == "DEMO_CLOCK"


def test_the_row_carries_the_five_frozen_fields_and_their_condition_twins() -> None:
    """ADR-029 § 1's field set, plus the microsecond twins the forward condition compares."""

    item = encode_demo_clock(NAMESPACE, seed_record())
    assert item[ATTR_LOGICAL_TIME_MICROS] == epoch_micros(SEED)
    assert item[ATTR_VERSION] == 1
    assert item[ATTR_RESET_GENERATION] == 1
    assert item["advance_count"] == 0
    # No case, no community, no content: a timestamp is not private data, which is the whole
    # argument for locating it in the shareable zone.
    assert item["community_id"] is None
    assert item["case_id"] is None


def test_a_row_whose_instant_halves_disagree_is_refused() -> None:
    """The readable value and the value a condition compares are the same instant, or neither."""

    item: dict[str, Any] = dict(encode_demo_clock(NAMESPACE, seed_record()))
    item[ATTR_LOGICAL_TIME_MICROS] = epoch_micros(SEED) + 1
    with pytest.raises(Exception, match="DEMO_CLOCK"):
        decode_demo_clock(NAMESPACE, item)


def test_a_row_with_an_extra_attribute_is_corrupt_not_tolerated() -> None:
    item: dict[str, Any] = dict(encode_demo_clock(NAMESPACE, seed_record()))
    item["injected"] = "anything"
    with pytest.raises(Exception, match="DEMO_CLOCK"):
        decode_demo_clock(NAMESPACE, item)


def test_a_clock_cannot_read_earlier_than_its_own_seed() -> None:
    with pytest.raises(ValueError, match="seed instant"):
        seed_record(logical_time=SEED - timedelta(seconds=1))


# -- reading -----------------------------------------------------------------------------


async def test_a_valid_row_is_read_back_exactly(storage: StorageDriver) -> None:
    written = await seed(storage)
    assert await store(storage).read() == written


async def test_an_absent_clock_fails_closed(storage: StorageDriver) -> None:
    """No row is a refusal, never a default and never a seed this process invents."""

    with pytest.raises(DemoClockUnavailableError):
        await store(storage).read()


async def test_a_corrupt_clock_fails_closed(storage: StorageDriver) -> None:
    item: dict[str, Any] = dict(encode_demo_clock(NAMESPACE, seed_record()))
    item[ATTR_VERSION] = "not a number"
    await storage.write_item(
        PutItem(
            key=demo_clock_key(NAMESPACE),
            item=item,
            condition=KeyAbsent(),
        )
    )
    with pytest.raises(DemoClockUnavailableError):
        await store(storage).read()


async def test_the_read_is_strongly_consistent() -> None:
    """Asserted at the driver call, because "strong" is a flag nobody notices going missing."""

    recorded: list[bool] = []

    class Recording:
        async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
            recorded.append(consistent)
            return encode_demo_clock(NAMESPACE, seed_record())

    await DynamoDbDemoClockStore(namespace=NAMESPACE, driver=Recording()).read()  # type: ignore[arg-type]
    assert recorded == [True]


async def test_a_storage_failure_on_read_fails_closed() -> None:
    class Failing:
        async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
            raise ExternalDependencyError("READ")

    with pytest.raises(DemoClockUnavailableError):
        await DynamoDbDemoClockStore(namespace=NAMESPACE, driver=Failing()).read()  # type: ignore[arg-type]


# -- advancing ---------------------------------------------------------------------------


async def test_a_forward_advance_moves_time_and_bumps_the_version(
    storage: StorageDriver,
) -> None:
    current = await seed(storage)
    moved = await store(storage).advance(current, to=SEED + timedelta(hours=3))

    assert moved.logical_time == SEED + timedelta(hours=3)
    assert moved.version == current.version + 1
    assert moved.advance_count == current.advance_count + 1
    # A normal advance is not the reseed authority: neither of these moves.
    assert moved.reset_generation == current.reset_generation
    assert moved.seed_instant == current.seed_instant
    assert await store(storage).read() == moved


async def test_an_advance_to_the_stored_instant_is_refused(storage: StorageDriver) -> None:
    """ "Advance the clock by nothing" reads as successful and changes nothing. Refused."""

    current = await seed(storage)
    with pytest.raises(DemoClockNotAdvancedError):
        await store(storage).advance(current, to=SEED)


async def test_an_advance_backwards_is_refused(storage: StorageDriver) -> None:
    current = await seed(storage)
    with pytest.raises(DemoClockNotAdvancedError):
        await store(storage).advance(current, to=SEED - timedelta(minutes=1))


async def test_a_stale_version_loses_deterministically(storage: StorageDriver) -> None:
    """Two concurrent advances: one wins, the other is told, and nothing is overwritten."""

    current = await seed(storage)
    winner = await store(storage).advance(current, to=SEED + timedelta(hours=1))

    with pytest.raises(DemoClockConflictError):
        await store(storage).advance(current, to=SEED + timedelta(hours=2))
    assert await store(storage).read() == winner


async def test_a_stale_reset_generation_loses_even_when_the_version_coincides(
    storage: StorageDriver,
) -> None:
    """The ADR-029 § 3 invariant, stated as its own test.

    Reset begins a fresh version sequence, so a command in flight from the previous run can
    carry a version that is live again. The generation is what makes that harmless.
    """

    await seed(storage, seed_record(version=1, reset_generation=2))
    stale = seed_record(version=1, reset_generation=1)

    with pytest.raises(DemoClockConflictError):
        await store(storage).advance(stale, to=SEED + timedelta(hours=1))
    assert (await store(storage).read()).reset_generation == 2


async def test_the_forward_rule_is_enforced_by_the_store_not_by_the_caller(
    storage: StorageDriver,
) -> None:
    """A caller holding a stale reading cannot walk time backwards past a newer one.

    The local guard would accept this -- the target is later than the *stale* reading it holds
    -- so what refuses it is the stored ``logical_time_micros`` condition, which is the point.
    """

    current = await seed(storage)
    await store(storage).advance(current, to=SEED + timedelta(hours=5))
    replayed = seed_record(version=2, advance_count=1, logical_time=SEED)

    with pytest.raises(DemoClockConflictError):
        await store(storage).advance(replayed, to=SEED + timedelta(hours=1))


async def test_a_storage_failure_on_advance_fails_closed() -> None:
    class Failing:
        async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
            return encode_demo_clock(NAMESPACE, seed_record())

        async def write_item(self, operation: PutItem) -> None:
            raise ExternalDependencyError("WRITE")

    with pytest.raises(DemoClockUnavailableError):
        await DynamoDbDemoClockStore(namespace=NAMESPACE, driver=Failing()).advance(  # type: ignore[arg-type]
            seed_record(), to=SEED + timedelta(hours=1)
        )


async def test_a_conditional_failure_becomes_a_conflict_and_never_a_retry() -> None:
    class Conflicting:
        async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
            return encode_demo_clock(NAMESPACE, seed_record())

        async def write_item(self, operation: PutItem) -> None:
            raise PersistenceConflictError("WRITE")

    with pytest.raises(DemoClockConflictError):
        await DynamoDbDemoClockStore(namespace=NAMESPACE, driver=Conflicting()).advance(  # type: ignore[arg-type]
            seed_record(), to=SEED + timedelta(hours=1)
        )


# -- what the normal adapter must not be able to do ---------------------------------------


def test_the_normal_adapter_exposes_no_reset_or_reseed() -> None:
    """Reset is the sole backward transition and it is the reset principal's, not this one's.

    Asserted by absence rather than by a comment, so the day somebody adds a convenience method
    for "put the clock back to the seed" this fails instead of the demo quietly acquiring a way
    to rewrite its own history.
    """

    surface = {name for name in dir(DynamoDbDemoClockStore) if not name.startswith("_")}
    assert surface == {"read", "advance", "driver", "namespace"}
