"""The deployed demo reset: one contract, manifest-driven, lock-fenced, generation-safe.

Review R2. The deployed :class:`~chorus.composition.deployed_demo_reset.DeployedDemoReset` runs
the frozen sequence against the same ``DemoResetService`` the local path uses -- here wired over
the in-memory storage driver, object store, and deadline scheduler, so the whole orchestration
runs with no AWS. Asserts the properties the reviewer reproduced as broken:

* an invalid ``seed_version`` is refused;
* an identical request under the same key replays the durable receipt and does **not** bump the
  reset generation again or rewind the clock again;
* ``DEMO_RESET_LOCK`` serialises resets and a non-owner cannot release it;
* a missing or corrupt manifest fails the reset closed -- no scan fallback;
* the bounded purge deletes only manifest-listed DEMO partitions/objects/schedules and keeps
  the control rows and the authoritative clock row;
* the clock reseed is generation-fenced and never a delete-and-recreate.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from tests.smoke.test_local_hero_flow import test_hero_flow as run_hero_flow

from chorus.application.operations import ApplicationOperations, StartReservation
from chorus.application.services.commitment_schedule import (
    due_schedule_request,
    schedule_name_prefix,
)
from chorus.composition.demo_reset import DemoResetRefused, DemoResetResult, predict_demo_case_id
from chorus.composition.deployed_demo_reset import DeployedDemoReset
from chorus.composition.local import LocalComposition, build_local
from chorus.domain.entities import ApplicationOperationKind
from chorus.domain.ids import CommitmentId, Namespace, Sha256Digest
from chorus.infrastructure.dynamodb import keys
from chorus.infrastructure.dynamodb.demo_clock import (
    DynamoDbDemoClockResetStore,
    DynamoDbDemoClockStore,
)
from chorus.infrastructure.dynamodb.demo_reset_store import (
    DynamoDbDemoManifestRegistrar,
    DynamoDbDemoManifestStore,
    DynamoDbDemoPartitionPurge,
    DynamoDbDemoResetLock,
    DynamoDbDemoResetReceiptStore,
)
from chorus.infrastructure.local.demo_reset import (
    InMemoryDemoObjectPrefixPurge,
    InMemoryDemoSchedulePurge,
)
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.infrastructure.local.scheduler import InMemoryDeadlineScheduler
from chorus.ports.demo_reset import (
    DemoManifest,
    DemoManifestStorePort,
    DemoManifestUnavailableError,
    DemoResetLockContendedError,
    DemoResetLockHandle,
    DemoResetLockNotOwnedError,
)
from chorus.ports.errors import IdempotencyConflictError, PersistenceConflictError
from chorus.ports.scheduler import DueScheduleRequest
from chorus.ports.storage import ItemKey, KeyAbsent, KeyPresent, PutItem, TableName
from chorus.ports.unit_of_work import TransactionOutcome, TransactionPlan, UnitOfWork
from chorus.settings import Environment, Settings

pytestmark = pytest.mark.anyio

NAMESPACE = Namespace("DEMO")

SCHEDULE_PREFIX = schedule_name_prefix(environment="dev", namespace=NAMESPACE)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def composition(tmp_path: Path) -> LocalComposition:
    return build_local(
        Settings(environment=Environment.DEVELOPMENT, local_data_dir=tmp_path),
        storage=InMemoryStorageDriver(),
    )


def _driver(composition: LocalComposition) -> InMemoryStorageDriver:
    driver = composition.driver
    assert isinstance(driver, InMemoryStorageDriver)
    return driver


async def _seed_initial_manifest(
    store: DemoManifestStorePort,
    *,
    extra_partitions: tuple[str, ...] = (),
    schedule_prefix: str = SCHEDULE_PREFIX,
) -> DemoManifest:
    manifest = DemoManifest(
        seed_version="elevator/v1",
        created_at=datetime(2029, 1, 1, tzinfo=UTC),
        version=1,
        partition_keys=("NS#DEMO", *extra_partitions),
        control_sort_prefixes=keys.DEMO_RESET_CONTROL_SORT_PREFIXES,
        private_object_prefixes=("ns/DEMO/",),
        export_object_prefixes=("ns/DEMO/",),
        schedule_name_prefix=schedule_prefix,
    )
    return await store.put(manifest, expected_version=None)


def _deployed(
    composition: LocalComposition, *, settings_environment: str = "demo"
) -> DeployedDemoReset:
    driver = _driver(composition)
    seeder = composition.container.reset_demo
    assert seeder is not None
    return DeployedDemoReset(
        seeder=seeder,
        driver=driver,
        manifest_store=DynamoDbDemoManifestStore(
            driver=driver, namespace=NAMESPACE, seed_version="elevator/v1"
        ),
        registrar=DynamoDbDemoManifestRegistrar(driver=driver, namespace=NAMESPACE),
        lock=DynamoDbDemoResetLock(driver=driver, namespace=NAMESPACE),
        receipts=DynamoDbDemoResetReceiptStore(driver=driver, namespace=NAMESPACE),
        partition_purge=DynamoDbDemoPartitionPurge(driver=driver),
        private_object_purge=InMemoryDemoObjectPrefixPurge(seeder.objects, "private"),  # type: ignore[arg-type]
        export_object_purge=InMemoryDemoObjectPrefixPurge(seeder.objects, "export"),  # type: ignore[arg-type]
        schedule_purge=InMemoryDemoSchedulePurge(seeder.scheduler, SCHEDULE_PREFIX),  # type: ignore[arg-type]
        clock_reset_store=DynamoDbDemoClockResetStore(driver=driver, namespace=NAMESPACE),
        wall_clock=seeder.clock,
        namespace=NAMESPACE,
        seed_version="elevator/v1",
        settings_environment=settings_environment,
        seed_instant=seeder.adapter.logical_clock_start,
        private_bucket="chorus-private-evidence-demo",
        export_bucket="chorus-export-evidence-demo",
        schedule_name_prefix=SCHEDULE_PREFIX,
    )


async def _run(
    deployed: DeployedDemoReset, *, key: str | None, seed_version: str = "elevator/v1"
) -> DemoResetResult:
    return await deployed.reset(
        namespace="DEMO", confirm="RESET DEMO", seed_version=seed_version, idempotency_key=key
    )


# -- validation -------------------------------------------------------------------------------


async def test_an_invalid_seed_version_is_refused(composition: LocalComposition) -> None:
    deployed = _deployed(composition)
    store = deployed.manifest_store
    assert isinstance(store, DynamoDbDemoManifestStore)
    await _seed_initial_manifest(store)
    with pytest.raises(DemoResetRefused, match="RESET_SEED_VERSION"):
        await _run(deployed, key=None, seed_version="elevator/v2")


async def test_a_wrong_environment_is_refused(composition: LocalComposition) -> None:
    deployed = _deployed(composition, settings_environment="development")
    with pytest.raises(DemoResetRefused, match="RESET_ENVIRONMENT"):
        await _run(deployed, key=None)


# -- durable replay: no second generation bump, no second rewind ---------------------------


async def test_an_identical_request_replays_the_receipt_without_a_second_reset(
    composition: LocalComposition,
) -> None:
    deployed = _deployed(composition)
    store = deployed.manifest_store
    assert isinstance(store, DynamoDbDemoManifestStore)
    await _seed_initial_manifest(store)
    clock = DynamoDbDemoClockStore(driver=_driver(composition), namespace=NAMESPACE)

    first = await _run(deployed, key="op-0001")
    assert first.replayed is False
    generation_after_first = (await clock.read()).reset_generation

    replay = await _run(deployed, key="op-0001")
    assert replay.replayed is True
    assert replay.reset_id == first.reset_id  # the recorded receipt, verbatim
    # the generation did NOT move, and the clock was NOT rewound a second time
    assert (await clock.read()).reset_generation == generation_after_first


async def test_a_different_request_under_the_same_key_is_an_idempotency_conflict(
    composition: LocalComposition,
) -> None:
    deployed = _deployed(composition)
    store = deployed.manifest_store
    assert isinstance(store, DynamoDbDemoManifestStore)
    await _seed_initial_manifest(store)
    await _run(deployed, key="op-0002")
    with pytest.raises(IdempotencyConflictError):
        await deployed.reset(
            namespace="DEMO",
            confirm="RESET DEMO",
            seed_version="elevator/v2",
            idempotency_key="op-0002",
        )


# -- the lock ------------------------------------------------------------------------------


async def test_a_second_reset_is_refused_while_the_lock_is_held(
    composition: LocalComposition,
) -> None:
    deployed = _deployed(composition)
    lock = deployed.lock
    assert isinstance(lock, DynamoDbDemoResetLock)
    await lock.acquire(
        owner_token="someone-else", now=datetime(2030, 1, 14, 9, tzinfo=UTC), ttl_seconds=300
    )
    store = deployed.manifest_store
    assert isinstance(store, DynamoDbDemoManifestStore)
    await _seed_initial_manifest(store)
    with pytest.raises(DemoResetLockContendedError):
        await _run(deployed, key=None)


async def test_a_non_owner_cannot_release_the_lock(composition: LocalComposition) -> None:
    lock = DynamoDbDemoResetLock(driver=_driver(composition), namespace=NAMESPACE)
    now = datetime(2030, 1, 14, 9, tzinfo=UTC)
    await lock.acquire(owner_token="owner-a", now=now, ttl_seconds=300)
    stranger = DemoResetLockHandle(
        owner_token="owner-b", acquired_at=now, expires_at=now.replace(hour=10)
    )
    with pytest.raises(DemoResetLockNotOwnedError):
        await lock.release(stranger)
    held = await lock.read()
    assert held is not None and held.owner_token == "owner-a"


# -- manifest fail-closed ---------------------------------------------------------------


async def test_a_missing_manifest_fails_the_reset_closed(
    composition: LocalComposition,
) -> None:
    deployed = _deployed(composition)
    with pytest.raises(DemoManifestUnavailableError):
        await _run(deployed, key=None)


async def test_a_corrupt_manifest_row_fails_the_reset_closed(
    composition: LocalComposition,
) -> None:
    driver = _driver(composition)
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
                "unexpected_attribute": "boom",
            },
            condition=KeyAbsent(),
        )
    )
    deployed = _deployed(composition)
    with pytest.raises(DemoManifestUnavailableError):
        await _run(deployed, key=None)


# -- bounded purge: DEMO gone, neighbours and control rows survive ----------------------


async def test_the_purge_removes_a_manifest_listed_partition_and_keeps_control_rows(
    composition: LocalComposition,
) -> None:
    driver = _driver(composition)
    deployed = _deployed(composition)
    store = deployed.manifest_store
    assert isinstance(store, DynamoDbDemoManifestStore)
    # a stale case partition the demo created, plus a neighbouring-namespace row that must survive
    stale_case = "NS#DEMO#CASE#stale"
    await _seed_initial_manifest(store, extra_partitions=(stale_case,))
    await driver.write_item(
        PutItem(
            key=ItemKey(table=TableName.SHAREABLE, partition_key=stale_case, sort_key="CASE"),
            item={"PK": stale_case, "SK": "CASE", "entity_type": "x", "schema_version": "x"},
            condition=KeyAbsent(),
        )
    )
    neighbour = "NS#DEMO2#CASE#other"
    await driver.write_item(
        PutItem(
            key=ItemKey(table=TableName.SHAREABLE, partition_key=neighbour, sort_key="CASE"),
            item={"PK": neighbour, "SK": "CASE", "entity_type": "x", "schema_version": "x"},
            condition=KeyAbsent(),
        )
    )
    # a schedule inside and one outside the DEMO name grammar
    seeder = composition.container.reset_demo
    assert seeder is not None
    assert isinstance(seeder.scheduler, InMemoryDeadlineScheduler)

    def request_for(namespace: Namespace) -> DueScheduleRequest:
        return due_schedule_request(
            environment="dev",
            namespace=namespace,
            community_id=seeder.community_id,
            case_id=seeder.demo_case_id,
            commitment_id=CommitmentId(UUID(int=1)),
            generation=1,
            due_at=seeder.clock.now(),
            at_utc=seeder.clock.now(),
        )

    positive, negative = request_for(NAMESPACE), request_for(Namespace("DEMO2"))
    seeder.scheduler.schedules[positive.schedule_name] = positive
    seeder.scheduler.schedules[negative.schedule_name] = negative

    clock = DynamoDbDemoClockStore(driver=driver, namespace=NAMESPACE)
    await _run(deployed, key=None)

    # the stale case partition is gone; the neighbour namespace is untouched
    assert (
        await driver.get_item(
            ItemKey(table=TableName.SHAREABLE, partition_key=stale_case, sort_key="CASE"),
            consistent=True,
        )
        is None
    )
    assert (
        await driver.get_item(
            ItemKey(table=TableName.SHAREABLE, partition_key=neighbour, sort_key="CASE"),
            consistent=True,
        )
        is not None
    )
    # the authoritative clock row survived (reseeded, not deleted) and the manifest was rewritten
    assert (await clock.read()).logical_time == seeder.adapter.logical_clock_start
    stored = await store.load()
    assert stored is not None and stored.version >= 2
    # only the DEMO-grammar schedule was deleted
    assert positive.schedule_name not in seeder.scheduler.schedules
    assert negative.schedule_name in seeder.scheduler.schedules


async def test_the_reset_bumps_a_new_generation_that_the_normal_store_never_reuses(
    composition: LocalComposition,
) -> None:
    driver = _driver(composition)
    deployed = _deployed(composition)
    store = deployed.manifest_store
    assert isinstance(store, DynamoDbDemoManifestStore)
    await _seed_initial_manifest(store)
    clock = DynamoDbDemoClockStore(driver=driver, namespace=NAMESPACE)

    await _run(deployed, key="gen-1")
    first_generation = (await clock.read()).reset_generation
    await _run(deployed, key="gen-2")
    second_generation = (await clock.read()).reset_generation
    assert second_generation > first_generation


# -- A4: the REAL creation call-sites invoke the registration mechanism ---------------------

_DIGEST = Sha256Digest("sha256:" + "0" * 64)


def _operations_with_registrar(
    composition: LocalComposition,
) -> tuple[ApplicationOperations, DynamoDbDemoManifestRegistrar]:
    """The container's own ``ApplicationOperations`` with the OPERATION-partition registrar
    wired exactly as ``functions/worker/composition.py`` wires it for the deployed DEMO path."""

    driver = _driver(composition)
    registrar = DynamoDbDemoManifestRegistrar(driver=driver, namespace=NAMESPACE)
    operations = composition.container.operations
    assert isinstance(operations, ApplicationOperations)
    operations.partition_registrar = registrar
    return operations, registrar


class _CapturingUnitOfWork:
    """Wraps the real unit of work and records every plan it is asked to commit."""

    def __init__(self, inner: UnitOfWork) -> None:
        self._inner = inner
        self.plans: list[TransactionPlan] = []

    async def commit(self, plan: TransactionPlan) -> None:
        self.plans.append(plan)
        return await self._inner.commit(plan)

    async def resolve_outcome(self, plan: TransactionPlan) -> TransactionOutcome:
        return await self._inner.resolve_outcome(plan)


async def test_a_real_operation_create_registers_its_partition_in_one_transaction(
    composition: LocalComposition,
) -> None:
    driver = _driver(composition)
    operations, registrar = _operations_with_registrar(composition)
    captured = _CapturingUnitOfWork(operations.unit_of_work)
    operations.unit_of_work = captured

    operation = await operations.create(
        namespace=Namespace("DEMO"),
        kind=ApplicationOperationKind.SEND_ACTION,
        actor_id_hash=_DIGEST,
        request_hash=_DIGEST,
    )
    operation_partition = keys.operation_partition(Namespace("DEMO"), operation.operation_id)
    control_partition = keys.demo_control_partition(NAMESPACE)

    # white box: the operation row and its marker are ONE plan, not a best-effort second write.
    plans = [p for p in captured.plans if p.name == "create-operation"]
    assert len(plans) == 1
    put_partitions = {op.key.partition_key for op in plans[0].operations}
    assert operation_partition in put_partitions
    marker_sorts = [
        op.key.sort_key for op in plans[0].operations if op.key.partition_key == control_partition
    ]
    assert marker_sorts == [keys.demo_registered_partition_sort_key(operation_partition)]

    # black box: the operation partition has a durable row AND the reset can enumerate it.
    purge = DynamoDbDemoPartitionPurge(driver=driver)
    assert await purge.partition_item_sort_keys(table="CORE", partition_key=operation_partition)
    assert operation_partition in await registrar.registered_partition_keys()


async def test_the_reservation_completion_call_site_also_registers_its_partition(
    composition: LocalComposition,
) -> None:
    from chorus.ports.idempotency import IdempotentCommand

    operations, registrar = _operations_with_registrar(composition)

    reserved = await operations.reserve_start(
        namespace=Namespace("DEMO"),
        command=IdempotentCommand.SEND_ACTION,
        actor_id_hash=_DIGEST,
        key_hash=_DIGEST,
        request_hash=_DIGEST,
    )
    assert isinstance(reserved, StartReservation)
    started = await operations.complete_start(
        reserved,
        namespace=Namespace("DEMO"),
        kind=ApplicationOperationKind.SEND_ACTION,
        actor_id_hash=_DIGEST,
    )
    operation_partition = keys.operation_partition(
        Namespace("DEMO"), started.operation.operation_id
    )
    assert operation_partition in await registrar.registered_partition_keys()


async def test_a_failing_marker_write_rolls_back_the_whole_creation(
    composition: LocalComposition,
) -> None:
    driver = _driver(composition)
    operations, registrar = _operations_with_registrar(composition)
    real = registrar.registration_operation

    def poisoned(partition_key: str) -> PutItem:
        good = real(partition_key)
        # A create-only marker whose condition can never hold: the transaction must abort,
        # and DynamoDB's all-or-nothing semantics must take the operation row down with it.
        return PutItem(key=good.key, item=good.item, condition=KeyPresent())

    operations.partition_registrar = SimpleNamespace(registration_operation=poisoned)

    with pytest.raises(PersistenceConflictError):
        await operations.create(
            namespace=Namespace("DEMO"),
            kind=ApplicationOperationKind.SEND_ACTION,
            actor_id_hash=_DIGEST,
            request_hash=_DIGEST,
        )

    items = await driver.namespace_items("DEMO")
    assert not [i for i in items if i.get("entity_type") == "APPLICATION_OPERATION"]
    assert not [i for i in items if i.get("entity_type") == "DEMO_REGISTERED_PARTITION"]


async def test_a_manifest_version_bump_does_not_lose_a_registration(
    composition: LocalComposition,
) -> None:
    driver = _driver(composition)
    store = DynamoDbDemoManifestStore(
        driver=driver, namespace=NAMESPACE, seed_version="elevator/v1"
    )
    manifest_v1 = await _seed_initial_manifest(store)
    registrar = DynamoDbDemoManifestRegistrar(driver=driver, namespace=NAMESPACE)

    await registrar.register_partition("NS#DEMO#OPERATION#aaaa")
    # a concurrent path discovers another dynamic root and rewrites the manifest row.
    await store.put(replace(manifest_v1, version=2), expected_version=1)
    await registrar.register_partition("NS#DEMO#OPERATION#bbbb")

    # markers are their own rows, not manifest fields, so the version bump lost neither.
    assert set(await registrar.registered_partition_keys()) == {
        "NS#DEMO#OPERATION#aaaa",
        "NS#DEMO#OPERATION#bbbb",
    }


async def test_the_deployed_reset_removes_a_full_hero_flow_case_world(
    composition: LocalComposition,
) -> None:
    from chorus_api.main import build_app

    driver = _driver(composition)
    # Wire the OPERATION-partition registrar into the request path exactly as the deployed
    # worker composition does, so every operation the hero flow creates leaves a marker.
    registrar = DynamoDbDemoManifestRegistrar(driver=driver, namespace=NAMESPACE)
    composition.container.operations.partition_registrar = registrar

    app = build_app(composition.container)
    with TestClient(app) as client:
        await run_hero_flow(client, composition)

    community_id = composition.adapter.community.community_id
    case_id = predict_demo_case_id(
        composition.adapter, namespace=Namespace("DEMO"), community_id=community_id
    )

    def _partitions(prefix: str) -> set[str]:
        return {pk for (_t, pk, _s) in driver._current if pk.startswith(prefix)}

    view_p = _partitions("NS#DEMO#VIEW#")
    action_p = _partitions("NS#DEMO#ACTION#")
    exec_p = _partitions("NS#DEMO#EXECUTION#")
    op_p = _partitions("NS#DEMO#OPERATION#")
    outbound_p = _partitions("NS#DEMO#OUTBOUND_MESSAGE#")
    assert view_p and action_p and exec_p and op_p, (view_p, action_p, exec_p, op_p)
    # every OPERATION partition the real request path created is in the reset inventory.
    assert op_p <= set(await registrar.registered_partition_keys())

    # a row in a look-alike namespace a reset must never touch.
    await driver.write_item(
        PutItem(
            key=ItemKey(
                table=TableName.SHAREABLE, partition_key="NS#DEMO2#CASE#x", sort_key="CASE"
            ),
            item={
                "PK": "NS#DEMO2#CASE#x",
                "SK": "CASE",
                "entity_type": "x",
                "schema_version": "x",
            },
            condition=KeyAbsent(),
        )
    )

    deployed = _deployed(composition)
    store = deployed.manifest_store
    assert isinstance(store, DynamoDbDemoManifestStore)
    await _seed_initial_manifest(
        store,
        extra_partitions=(
            f"NS#DEMO#COMM#{community_id}",
            f"NS#DEMO#CASE#{case_id}",
            f"NS#DEMO#FENCE#{case_id}",
            f"NS#DEMO#VIEW_CURRENT#{case_id}",
            f"NS#DEMO#ACTION_CURRENT#{case_id}",
        ),
    )

    result = await _run(deployed, key="hero-e2e-0001")
    assert result.replayed is False

    purge = DynamoDbDemoPartitionPurge(driver=driver)
    for partition_key in view_p | action_p | exec_p | op_p | outbound_p:
        for table in ("CORE", "SHAREABLE", "AUDIT"):
            assert (
                await purge.partition_item_sort_keys(table=table, partition_key=partition_key) == ()
            ), (table, partition_key)
    # every dynamic-partition marker was cleared with the partition it named.
    assert await registrar.registered_partition_keys() == ()
    # the look-alike namespace survived untouched, and a fresh demo has been seeded.
    assert (
        await driver.get_item(
            ItemKey(table=TableName.SHAREABLE, partition_key="NS#DEMO2#CASE#x", sort_key="CASE"),
            consistent=True,
        )
        is not None
    )
    stored = await store.load()
    assert stored is not None and stored.version >= 2
