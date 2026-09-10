"""Production-path regressions for the residual reset races; no AWS or token-cache reset."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from tests.smoke.test_local_hero_flow import test_hero_flow as run_hero_flow
from tests.unit.functions.test_deployed_demo_reset import (
    _DIGEST,
    _deployed,
    _driver,
    _operations_with_registrar,
    _run,
    _seed_initial_manifest,
)

from chorus.application.services.demo_side_effect import demo_side_effect
from chorus.composition.demo_reset import DemoResetInFlightSend
from chorus.composition.local import LocalComposition, build_local
from chorus.domain.entities import ApplicationOperationKind
from chorus.domain.ids import CaseId, Namespace, Uuid4Generator, ViewId
from chorus.infrastructure.dynamodb import codec_share, keys
from chorus.infrastructure.dynamodb.demo_mutation import LOCK_KEY
from chorus.infrastructure.dynamodb.idempotency import IdempotencyRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.infrastructure.local.monitor_agent import LexicalFakeMonitorAgent
from chorus.ports.agents import MonitorInvocation, MonitorResult
from chorus.ports.demo_reset import DemoResetReceipt
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.idempotency import (
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotentCommand,
)
from chorus.ports.records import CurrentViewPointer
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import CheckItem, ItemKey, KeyAbsent, PutItem, TableName
from chorus.settings import Environment, Settings

pytestmark = pytest.mark.anyio


def _use_demo_fixture_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.fixtures import action, compile, elevator, reply, send

    for module in (action, compile, elevator, reply, send):
        monkeypatch.setattr(module, "NAMESPACE", Namespace("DEMO"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def composition(tmp_path: Path) -> LocalComposition:
    return build_local(
        Settings(environment=Environment.DEVELOPMENT, local_data_dir=tmp_path),
        storage=InMemoryStorageDriver(),
    )


async def test_real_operation_creation_is_atomic_with_lock_absence_and_resumes(
    composition: LocalComposition,
) -> None:
    d = _deployed(composition)
    operations, registrar = _operations_with_registrar(composition)
    handle = await d.lock.acquire(
        owner_token="reset-owner", now=d.wall_clock.now(), ttl_seconds=300
    )
    with pytest.raises(PersistenceConflictError):
        await operations.create(
            namespace=Namespace("DEMO"),
            kind=ApplicationOperationKind.SEND_ACTION,
            actor_id_hash=_DIGEST,
            request_hash=_DIGEST,
        )
    assert await registrar.registered_partition_keys() == ()
    assert not any(
        r.get("entity_type") == "APPLICATION_OPERATION"
        for r in await _driver(composition).namespace_items("DEMO")
    )
    await d.lock.release(handle)
    operation = await operations.create(
        namespace=Namespace("DEMO"),
        kind=ApplicationOperationKind.SEND_ACTION,
        actor_id_hash=_DIGEST,
        request_hash=_DIGEST,
    )
    partition = keys.operation_partition(Namespace("DEMO"), operation.operation_id)
    assert partition in await registrar.registered_partition_keys()
    wire_plans = [ops for _, ops in _driver(composition)._tokens.values()]
    assert any(
        any(op.key.partition_key == partition for op in plan)
        and CheckItem(key=LOCK_KEY, condition=KeyAbsent()) in plan
        for plan in wire_plans
    )
    other = await replace(operations, partition_registrar=None).create(
        namespace=Namespace("DEMO2"),
        kind=ApplicationOperationKind.SEND_ACTION,
        actor_id_hash=_DIGEST,
        request_hash=_DIGEST,
    )
    other_partition = keys.operation_partition(Namespace("DEMO2"), other.operation_id)
    assert all(
        not any(op.key == LOCK_KEY for op in plan)
        for _, plan in _driver(composition)._tokens.values()
        if any(op.key.partition_key == other_partition for op in plan)
    )


async def test_existing_external_attempt_blocks_reset_and_new_attempt_is_fenced(
    composition: LocalComposition,
) -> None:
    d = _deployed(composition)
    await _seed_initial_manifest(d.manifest_store)
    repository = IdempotencyRepository(driver=_driver(composition), table=TableName.CORE)
    key = IdempotencyKey(
        partition=IdempotencyPartition(
            kind=IdempotencyPartitionKind.NAMESPACE, namespace=Namespace("DEMO")
        ),
        command=IdempotentCommand.COMPILE_VIEW,
        actor_id_hash=_DIGEST,
        key_hash=_DIGEST,
    )
    handle = await d.lock.acquire(owner_token="owner", now=d.wall_clock.now(), ttl_seconds=300)
    entered = False
    with pytest.raises(PersistenceConflictError):
        async with demo_side_effect(
            key=key,
            repository=repository,
            unit_of_work=StorageUnitOfWork(_driver(composition)),
            clock=d.wall_clock,
            ids=Uuid4Generator(),
        ):
            entered = True
    assert not entered
    await d.lock.release(handle)
    async with demo_side_effect(
        key=key,
        repository=repository,
        unit_of_work=StorageUnitOfWork(_driver(composition)),
        clock=d.wall_clock,
        ids=Uuid4Generator(),
    ):
        with pytest.raises(DemoResetInFlightSend, match="RESET_SIDE_EFFECT_IN_FLIGHT"):
            await _run(d, key=None)
        assert await d.clock_reset_store.read() is None
    assert not (await _run(d, key="after-effect")).replayed


async def test_concurrent_receipt_misses_replay_once_and_fresh_reset_reseeds(
    composition: LocalComposition,
) -> None:
    d = _deployed(composition)
    await _seed_initial_manifest(d.manifest_store)
    real = d.receipts
    barrier = asyncio.Event()

    class Receipts:
        misses = 0

        async def load(self, key: str) -> DemoResetReceipt | None:
            result = await real.load(key)
            if result is None and self.misses < 2:
                self.misses += 1
                if self.misses == 2:
                    barrier.set()
                await barrier.wait()
            return result

        async def put(self, receipt: DemoResetReceipt) -> None:
            await real.put(receipt)

    d.receipts = Receipts()
    a, b = await asyncio.gather(_run(d, key="same"), _run(replace(d), key="same"))
    assert a.reset_id == b.reset_id
    assert sorted((a.replayed, b.replayed)) == [False, True]
    clock = await d.clock_reset_store.read()
    assert clock is not None and clock.reset_generation == 1
    d.receipts = real
    fresh = await _run(d, key="fresh")
    assert fresh.counts.messages == 24 and fresh.counts.evidence == 2
    clock = await d.clock_reset_store.read()
    assert clock is not None and clock.reset_generation == 2


async def test_real_monitor_multiple_case_worlds_are_registered_and_purged(
    composition: LocalComposition, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chorus_api.main import build_app

    original = LexicalFakeMonitorAgent.invoke_monitor

    async def split(agent: LexicalFakeMonitorAgent, invocation: MonitorInvocation) -> MonitorResult:
        result = await original(agent, invocation)
        links = tuple(
            link.model_copy(update={"candidate_group_ref": "group-a" if i % 2 else "group-b"})
            for i, link in enumerate(result.output.candidate_links)
        )
        return result.model_copy(
            update={"output": result.output.model_copy(update={"candidate_links": links})}
        )

    monkeypatch.setattr(LexicalFakeMonitorAgent, "invoke_monitor", split)
    # The existing hero helper asserts that all signals name one case. Our real Monitor
    # path has succeeded when it reaches that assertion with two accepted groups instead.
    with TestClient(build_app(composition.container)) as client, pytest.raises(AssertionError):
        await run_hero_flow(client, composition)
    d = _deployed(composition)
    driver = _driver(composition)
    predicted = str(d.seeder.demo_case_id)
    cases = {
        CaseId(UUID(str(row["PK"]).removeprefix("NS#DEMO#CASE#")))
        for row in await driver.namespace_items("DEMO")
        if row.get("entity_type") == "COMMUNITY_CASE" and not str(row["PK"]).endswith(predicted)
    }
    assert len(cases) >= 2
    descendant_keys: list[ItemKey] = []
    now = d.wall_clock.now()
    for case_id in cases:
        view_id = ViewId(uuid4())
        scope = CaseScope(
            namespace=Namespace("DEMO"), community_id=d.seeder.community_id, case_id=case_id
        )
        pointer = CurrentViewPointer(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=case_id,
            view_id=view_id,
            view_hash=_DIGEST,
            case_version=1,
            authorization_version=1,
            expires_at=now + timedelta(hours=1),
            version=1,
            created_at=now,
            updated_at=now,
        )
        pointer_item = codec_share.encode_view_pointer(scope, pointer)
        pointer_key = ItemKey(
            table=TableName.SHAREABLE,
            partition_key=str(pointer_item["PK"]),
            sort_key=str(pointer_item["SK"]),
        )
        view_key = ItemKey(
            table=TableName.SHAREABLE,
            partition_key=keys.view_partition(scope.namespace, view_id),
            sort_key="VIEW",
        )
        # The opaque child is enough to test bounded deletion; its locator uses the actual
        # strict production codec, and the two first rows commit together like compilation.
        await driver.transact_write(
            (
                PutItem(key=pointer_key, item=pointer_item, condition=KeyAbsent()),
                PutItem(
                    key=view_key,
                    item={"PK": view_key.partition_key, "SK": "VIEW"},
                    condition=KeyAbsent(),
                ),
            ),
            client_request_token=str(uuid4()),
        )
        descendant_keys.append(view_key)
    neighbor = ItemKey(
        table=TableName.SHAREABLE, partition_key="NS#DEMO2#CASE#neighbor", sort_key="CASE"
    )
    await driver.write_item(
        PutItem(
            key=neighbor, item={"PK": neighbor.partition_key, "SK": "CASE"}, condition=KeyAbsent()
        )
    )
    await _seed_initial_manifest(
        d.manifest_store, extra_partitions=(f"NS#DEMO#COMM#{d.seeder.community_id}",)
    )
    await _run(d, key="multi-case")
    for key in descendant_keys:
        assert await driver.get_item(key, consistent=True) is None
    remaining = {str(row["PK"]) for row in await driver.namespace_items("DEMO")}
    assert all(keys.case_partition(Namespace("DEMO"), case) not in remaining for case in cases)
    assert await driver.get_item(neighbor, consistent=True) is not None


async def test_real_monitor_cannot_apply_after_reset_acquires_lock(
    composition: LocalComposition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chorus_api.main import build_app

    from chorus.ports.demo_reset import DemoResetLockHandle
    from chorus.ports.storage import StoredItem

    d = _deployed(composition)
    original = LexicalFakeMonitorAgent.invoke_monitor
    held: DemoResetLockHandle | None = None
    before: tuple[StoredItem, ...] = ()

    async def case_world() -> tuple[StoredItem, ...]:
        return tuple(
            row
            for row in await _driver(composition).namespace_items("DEMO")
            if "#CASE#" in str(row["PK"]) or "#FENCE#" in str(row["PK"])
        )

    async def lock_before_apply(
        agent: LexicalFakeMonitorAgent, invocation: MonitorInvocation
    ) -> MonitorResult:
        nonlocal held, before
        result = await original(agent, invocation)
        before = await case_world()
        held = await d.lock.acquire(
            owner_token="reset-during-monitor", now=d.wall_clock.now(), ttl_seconds=300
        )
        return result

    monkeypatch.setattr(LexicalFakeMonitorAgent, "invoke_monitor", lock_before_apply)
    with (
        TestClient(build_app(composition.container)) as client,
        pytest.raises((PersistenceConflictError, AssertionError)),
    ):
        await run_hero_flow(client, composition)
    assert held is not None, "the production Monitor invocation must actually have run"
    assert await case_world() == before
    await d.lock.release(held)


async def test_real_compile_side_effect_is_admitted_before_reset_and_blocks_purge(
    composition: LocalComposition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fixtures.compile import CompileHarness, photo_bytes

    from chorus.domain.ids import CommunityId, Sha256Digest
    from chorus.infrastructure.local.objects import InMemoryObjectStore

    _use_demo_fixture_namespace(monkeypatch)
    harness = CompileHarness(driver=_driver(composition))
    photo = photo_bytes()
    await harness.seed(evidence_items=harness.align_photo_digest(photo), photo=photo)
    assert harness.scope.namespace == Namespace("DEMO")
    compile_view = harness.compile_view()
    d = _deployed(composition)
    await _seed_initial_manifest(d.manifest_store)
    held = await d.lock.acquire(owner_token="owner", now=d.wall_clock.now(), ttl_seconds=300)
    with pytest.raises(PersistenceConflictError):
        await compile_view.execute(harness.command())
    assert harness.objects.put_calls == 0
    await d.lock.release(held)
    original = InMemoryObjectStore.put_export_evidence
    attempted = False

    async def during_put(
        store: InMemoryObjectStore,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        derivative_sha256: Sha256Digest,
        content: bytes,
        media_type: str,
    ) -> None:
        nonlocal attempted
        attempted = True
        with pytest.raises(DemoResetInFlightSend, match="RESET_SIDE_EFFECT_IN_FLIGHT"):
            await _run(d, key=None)
        assert await d.clock_reset_store.read() is None
        await original(
            store,
            namespace=namespace,
            community_id=community_id,
            case_id=case_id,
            derivative_sha256=derivative_sha256,
            content=content,
            media_type=media_type,
        )

    monkeypatch.setattr(InMemoryObjectStore, "put_export_evidence", during_put)
    result = await compile_view.execute(harness.command())
    assert result.view is not None and attempted
    assert harness.objects.put_calls == 1


async def test_real_schedule_intent_blocks_reset_and_projection_covers_lagging_list(
    composition: LocalComposition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from tests.contract.reply.test_scheduler_wall_clock_and_envelope import (
        _commitment,
        _schedule_command,
    )
    from tests.fixtures.action import ActionHarness
    from tests.fixtures.reply import ReplyHarness
    from tests.fixtures.send import SendHarness

    from chorus.application.services.commitment_schedule import (
        due_schedule_request,
        schedule_name_prefix,
    )
    from chorus.infrastructure.local.demo_reset import InMemoryDemoSchedulePurge
    from chorus.infrastructure.local.scheduler import InMemoryDeadlineScheduler
    from chorus.ports.scheduler import DueScheduleRequest, ScheduleOutcome

    _use_demo_fixture_namespace(monkeypatch)
    harness = ReplyHarness(send=SendHarness(action=ActionHarness(driver=_driver(composition))))
    await harness.prepare_sent()
    commitment = await _commitment(harness)
    schedule = harness.create_schedule()
    d = _deployed(composition)
    prefix = schedule_name_prefix(
        environment=schedule.scheduler_environment, namespace=Namespace("DEMO")
    )
    d.schedule_name_prefix = prefix
    await _seed_initial_manifest(d.manifest_store, schedule_prefix=prefix)
    command = _schedule_command(harness, commitment, logical_now=schedule.clock.now())
    held = await d.lock.acquire(owner_token="owner", now=d.wall_clock.now(), ttl_seconds=300)
    with pytest.raises(PersistenceConflictError):
        await schedule.execute(command)
    assert not harness.scheduler.created
    await d.lock.release(held)
    original = InMemoryDeadlineScheduler.create_due_schedule

    async def during_create(
        adapter: InMemoryDeadlineScheduler, request: DueScheduleRequest
    ) -> ScheduleOutcome:
        with pytest.raises(DemoResetInFlightSend, match="RESET_SIDE_EFFECT_IN_FLIGHT"):
            await _run(d, key=None)
        return await original(adapter, request)

    monkeypatch.setattr(InMemoryDeadlineScheduler, "create_due_schedule", during_create)
    await schedule.execute(command)
    request = harness.scheduler.created[-1]
    neighbor = due_schedule_request(
        environment=schedule.scheduler_environment,
        namespace=Namespace("DEMO2"),
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
        commitment_id=commitment.commitment_id,
        generation=commitment.schedule_generation,
        due_at=commitment.due_at,
        at_utc=request.at_utc,
    )
    similar = replace(
        request, schedule_name=request.schedule_name.replace(prefix, prefix[:-1] + "0-")
    )
    harness.scheduler.schedules[neighbor.schedule_name] = neighbor
    harness.scheduler.schedules[similar.schedule_name] = similar
    purge = InMemoryDemoSchedulePurge(harness.scheduler, prefix)

    class LaggingList:
        async def list_demo_schedules(self, *, name_prefix: str) -> tuple[str, ...]:
            assert name_prefix == prefix
            return ()

        async def delete_schedules(self, *, names: Sequence[str]) -> int:
            return await purge.delete_schedules(names=names)

    d.schedule_purge = LaggingList()
    await _run(d, key="after-schedule")
    assert request.schedule_name not in harness.scheduler.schedules
    assert neighbor.schedule_name in harness.scheduler.schedules
    assert similar.schedule_name in harness.scheduler.schedules


async def test_an_ambiguous_object_attempt_remains_a_reset_barrier_after_head_recovery(
    composition: LocalComposition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fixtures.compile import CompileHarness, photo_bytes

    _use_demo_fixture_namespace(monkeypatch)
    harness = CompileHarness(driver=_driver(composition))
    photo = photo_bytes()
    await harness.seed(evidence_items=harness.align_photo_digest(photo), photo=photo)
    harness.objects.ambiguous_next_put = True
    result = await harness.compile_view().execute(harness.command())
    assert result.view is not None  # existing content-addressed HEAD recovery still works
    d = _deployed(composition)
    await _seed_initial_manifest(d.manifest_store)
    with pytest.raises(DemoResetInFlightSend, match="RESET_SIDE_EFFECT_IN_FLIGHT"):
        await _run(d, key=None)
    assert await d.clock_reset_store.read() is None


async def test_real_dynamodb_wire_transaction_contains_operation_marker_and_lock_check(
    composition: LocalComposition,
) -> None:
    from tests.unit.persistence.test_driver import TABLE_NAMES, StubClient

    from chorus.infrastructure.dynamodb.core import CoreRepository
    from chorus.infrastructure.dynamodb.demo_reset_store import DynamoDbDemoManifestRegistrar
    from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver

    client = StubClient()
    driver = DynamoDbStorageDriver(client=client, table_names=TABLE_NAMES)
    operations, _ = _operations_with_registrar(composition)
    assert isinstance(operations.core, CoreRepository)
    wired = replace(
        operations,
        core=replace(operations.core, driver=driver),
        unit_of_work=StorageUnitOfWork(driver),
        partition_registrar=DynamoDbDemoManifestRegistrar(driver, Namespace("DEMO")),
    )
    operation = await wired.create(
        namespace=Namespace("DEMO"),
        kind=ApplicationOperationKind.SEND_ACTION,
        actor_id_hash=_DIGEST,
        request_hash=_DIGEST,
    )
    assert len(client.calls) == 1
    method, request = client.calls[0]
    assert method == "transact_write_items"
    items = request["TransactItems"]
    assert len(items) == 3
    puts = [item["Put"]["Item"] for item in items if "Put" in item]
    assert {item["entity_type"]["S"] for item in puts} == {
        "APPLICATION_OPERATION",
        "DEMO_REGISTERED_PARTITION",
    }
    assert any(
        item["PK"]["S"] == keys.operation_partition(Namespace("DEMO"), operation.operation_id)
        for item in puts
    )
    condition = next(item["ConditionCheck"] for item in items if "ConditionCheck" in item)
    assert condition["TableName"] == TABLE_NAMES[TableName.CORE]
    assert condition["Key"] == {"PK": {"S": "NS#DEMO"}, "SK": {"S": "DEMO_RESET_LOCK"}}
    assert "attribute_not_exists" in condition["ConditionExpression"]


async def test_independent_conditional_calls_are_not_deduplicated_but_stage_retries_are() -> None:
    from tests.unit.persistence.test_driver import TABLE_NAMES, StubClient

    from chorus.domain.ids import Uuid5Generator
    from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver

    client = StubClient()
    driver = DynamoDbStorageDriver(
        client=client,
        table_names=TABLE_NAMES,
        write_ids=Uuid5Generator(namespace=UUID(int=99), prefix="write"),
    )
    key = ItemKey(table=TableName.SHAREABLE, partition_key="NS#DEMO#VIEW#x", sort_key="VIEW")
    operation = PutItem(
        key=key, item={"PK": key.partition_key, "SK": key.sort_key}, condition=KeyAbsent()
    )
    await driver.write_item(operation)
    await driver.write_item(operation)
    assert client.calls[-1][1]["ClientRequestToken"] != client.calls[-2][1]["ClientRequestToken"]
    await driver.transact_write((operation,), client_request_token="same-logical-stage")
    await driver.transact_write((operation,), client_request_token="same-logical-stage")
    assert client.calls[-1][1]["ClientRequestToken"] == client.calls[-2][1]["ClientRequestToken"]


async def test_interrupted_purge_retains_registration_until_its_case_world_is_removed(
    composition: LocalComposition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fixtures.compile import CompileHarness

    from chorus.ports.demo_reset import DemoResetInfrastructureError

    _use_demo_fixture_namespace(monkeypatch)
    harness = CompileHarness(driver=_driver(composition))
    await harness.seed()
    d = _deployed(composition)
    case_partition = keys.case_partition(Namespace("DEMO"), harness.scope.case_id)
    await _seed_initial_manifest(
        d.manifest_store,
        extra_partitions=(keys.community_partition(Namespace("DEMO"), harness.scope.community_id),),
    )
    real = d.partition_purge

    class InterruptedPurge:
        fail_next = True

        async def delete_partition(
            self, *, table: str, partition_key: str, keep_sort_prefixes: Sequence[str] = ()
        ) -> int:
            if self.fail_next and table == "CORE" and partition_key == case_partition:
                self.fail_next = False
                raise DemoResetInfrastructureError("injected purge interruption")
            return await real.delete_partition(
                table=table, partition_key=partition_key, keep_sort_prefixes=keep_sort_prefixes
            )

        async def partition_item_sort_keys(
            self, *, table: str, partition_key: str
        ) -> tuple[str, ...]:
            return await real.partition_item_sort_keys(table=table, partition_key=partition_key)

    d.partition_purge = InterruptedPurge()
    with pytest.raises(DemoResetInfrastructureError):
        await _run(d, key="interrupted")
    assert case_partition in await d.registrar.registered_partition_keys()
    assert await d.receipts.load("interrupted") is None
    await _run(d, key="complete-after-interruption")
    assert await d.registrar.registered_partition_keys() == ()
    assert not any(
        str(row["PK"]) == case_partition
        for row in await _driver(composition).namespace_items("DEMO")
    )


async def test_a_manifest_cannot_make_reset_delete_and_recreate_the_clock(
    composition: LocalComposition,
) -> None:
    from tests.unit.infra.test_demo_clock_reset_adapter import record, seed

    d = _deployed(composition)
    await _seed_initial_manifest(
        d.manifest_store, extra_partitions=(keys.demo_clock_partition(Namespace("DEMO")),)
    )
    await seed(_driver(composition), record(reset_generation=7))
    await _run(d, key="clock-preservation")
    clock = await d.clock_reset_store.read()
    assert clock is not None and clock.reset_generation == 8


def test_an_existing_wrong_lock_condition_cannot_disable_the_storage_guard() -> None:
    from chorus.infrastructure.dynamodb.demo_mutation import fence_operations
    from chorus.ports.storage import KeyPresent

    key = ItemKey(table=TableName.CORE, partition_key="NS#DEMO#OPERATION#x", sort_key="OPERATION")
    operation = PutItem(
        key=key, item={"PK": key.partition_key, "SK": key.sort_key}, condition=KeyAbsent()
    )
    with pytest.raises(ValueError, match="reset lock to be absent"):
        fence_operations((operation, CheckItem(key=LOCK_KEY, condition=KeyPresent())))
