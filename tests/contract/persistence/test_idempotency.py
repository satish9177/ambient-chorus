"""Idempotency contract: replay, conflict, in-progress, and retention."""

from __future__ import annotations

import pytest
from tests.fixtures.persistence import NOW, OTHER_CASE, PRIMARY, build_repositories, digest

from chorus.domain.entities import ActionExecutionState
from chorus.infrastructure.dynamodb import codec_idempotency
from chorus.ports.errors import IdempotencyConflictError
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyFailedFinal,
    IdempotencyInProgress,
    IdempotencyPartitionKind,
    IdempotencyReplay,
    IdempotencyStarted,
    IdempotencyStatus,
    IdempotentCommand,
    retention_seconds,
    send_attempt_is_authoritative,
)
from chorus.ports.limits import (
    ORDINARY_IDEMPOTENCY_TTL_SECONDS,
    SEND_IDEMPOTENCY_TTL_SECONDS,
)
from chorus.ports.storage import StorageDriver, TableName

pytestmark = pytest.mark.anyio


async def test_a_first_attempt_claims_the_key(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)

    outcome = await repositories.idempotency.begin(
        PRIMARY.idempotency_key(), request_hash=digest("request"), now=NOW
    )

    assert isinstance(outcome, IdempotencyStarted)
    assert outcome.record.status is IdempotencyStatus.IN_PROGRESS


async def test_a_concurrent_attempt_sees_the_record_in_progress(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    key = PRIMARY.idempotency_key()
    await repositories.idempotency.begin(key, request_hash=digest("request"), now=NOW)

    outcome = await repositories.idempotency.begin(key, request_hash=digest("request"), now=NOW)

    assert isinstance(outcome, IdempotencyInProgress)


async def test_the_same_key_with_a_different_request_is_a_conflict(
    storage: StorageDriver,
) -> None:
    """A replayed key must never quietly execute a different command."""

    repositories = build_repositories(storage)
    key = PRIMARY.idempotency_key()
    await repositories.idempotency.begin(key, request_hash=digest("request"), now=NOW)

    with pytest.raises(IdempotencyConflictError):
        await repositories.idempotency.begin(
            key, request_hash=digest("a-different-request"), now=NOW
        )


async def test_a_completed_command_replays_its_recorded_outcome(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    key = PRIMARY.idempotency_key()
    started = await repositories.idempotency.begin(key, request_hash=digest("request"), now=NOW)
    assert isinstance(started, IdempotencyStarted)
    refs = (EntityRef(entity_type="COMMUNITY_CASE", entity_id=PRIMARY.uuid("case"), version=2),)
    await storage.write_item(
        repositories.idempotency.stage_complete(
            started.record, result_entity_refs=refs, response_status=200, now=NOW
        )
    )

    outcome = await repositories.idempotency.begin(key, request_hash=digest("request"), now=NOW)

    assert isinstance(outcome, IdempotencyReplay)
    assert outcome.record.result_entity_refs == refs
    assert outcome.record.response_status == 200


async def test_a_terminally_failed_command_is_not_retryable_under_its_key(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    key = PRIMARY.idempotency_key()
    started = await repositories.idempotency.begin(key, request_hash=digest("request"), now=NOW)
    assert isinstance(started, IdempotencyStarted)
    await storage.write_item(
        repositories.idempotency.stage_fail_final(started.record, response_status=422, now=NOW)
    )

    outcome = await repositories.idempotency.begin(key, request_hash=digest("request"), now=NOW)

    assert isinstance(outcome, IdempotencyFailedFinal)


async def test_only_an_in_progress_record_may_reach_a_final_outcome(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    key = PRIMARY.idempotency_key()
    started = await repositories.idempotency.begin(key, request_hash=digest("request"), now=NOW)
    assert isinstance(started, IdempotencyStarted)
    await storage.write_item(
        repositories.idempotency.stage_complete(
            started.record, result_entity_refs=(), response_status=200, now=NOW
        )
    )
    completed = await repositories.idempotency.load(key)
    assert completed is not None

    with pytest.raises(ValueError, match="in-progress"):
        repositories.idempotency.stage_complete(
            completed, result_entity_refs=(), response_status=200, now=NOW
        )


async def test_keys_in_different_partitions_never_collide(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    primary_key = PRIMARY.idempotency_key()
    other_key = OTHER_CASE.idempotency_key()

    await repositories.idempotency.begin(primary_key, request_hash=digest("a"), now=NOW)
    outcome = await repositories.idempotency.begin(other_key, request_hash=digest("b"), now=NOW)

    assert isinstance(outcome, IdempotencyStarted)


async def test_a_key_is_scoped_to_its_command_and_actor(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    await repositories.idempotency.begin(
        PRIMARY.idempotency_key(command=IdempotentCommand.COMPILE_VIEW),
        request_hash=digest("a"),
        now=NOW,
    )

    outcome = await repositories.idempotency.begin(
        PRIMARY.idempotency_key(command=IdempotentCommand.PROPOSE_ACTION),
        request_hash=digest("a"),
        now=NOW,
    )

    assert isinstance(outcome, IdempotencyStarted)


@pytest.mark.parametrize(
    "kind",
    [
        IdempotencyPartitionKind.NAMESPACE,
        IdempotencyPartitionKind.COMMUNITY,
        IdempotencyPartitionKind.CASE,
        IdempotencyPartitionKind.ACTION,
    ],
)
async def test_every_contextual_partition_round_trips(
    storage: StorageDriver, kind: IdempotencyPartitionKind
) -> None:
    repositories = build_repositories(storage)
    key = PRIMARY.idempotency_key(kind=kind)

    started = await repositories.idempotency.begin(key, request_hash=digest("request"), now=NOW)
    loaded = await repositories.idempotency.load(key)

    assert isinstance(started, IdempotencyStarted)
    assert loaded == started.record


async def test_retention_is_longer_for_a_send(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    ordinary = PRIMARY.idempotency_key(command=IdempotentCommand.COMPILE_VIEW)
    send = PRIMARY.idempotency_key(
        command=IdempotentCommand.SEND_ACTION, kind=IdempotencyPartitionKind.ACTION
    )

    ordinary_outcome = await repositories.idempotency.begin(
        ordinary, request_hash=digest("a"), now=NOW
    )
    send_outcome = await repositories.idempotency.begin(send, request_hash=digest("b"), now=NOW)

    assert isinstance(ordinary_outcome, IdempotencyStarted)
    assert isinstance(send_outcome, IdempotencyStarted)
    assert (
        ordinary_outcome.record.expires_at_epoch
        == int(NOW.timestamp()) + ORDINARY_IDEMPOTENCY_TTL_SECONDS
    )
    assert (
        send_outcome.record.expires_at_epoch == int(NOW.timestamp()) + SEND_IDEMPOTENCY_TTL_SECONDS
    )


async def test_retention_helpers_match_the_frozen_bounds() -> None:
    assert retention_seconds(IdempotentCommand.SEND_ACTION) == SEND_IDEMPOTENCY_TTL_SECONDS
    assert retention_seconds(IdempotentCommand.COMPILE_VIEW) == ORDINARY_IDEMPOTENCY_TTL_SECONDS


async def test_a_recorded_send_attempt_stays_authoritative_after_expiry() -> None:
    """TTL cleans up the record; it does not licence a second send."""

    assert send_attempt_is_authoritative(ActionExecutionState.SENT)
    assert send_attempt_is_authoritative(ActionExecutionState.SEND_UNKNOWN)
    assert not send_attempt_is_authoritative(ActionExecutionState.APPROVED)
    assert not send_attempt_is_authoritative(ActionExecutionState.FAILED)


async def test_an_idempotency_record_is_addressed_inside_its_own_partition() -> None:
    key = PRIMARY.idempotency_key(kind=IdempotencyPartitionKind.CASE)

    item_key = codec_idempotency.idempotency_item_key(key, table=TableName.CORE)

    assert item_key.partition_key.endswith(str(PRIMARY.case_id))
    assert item_key.sort_key.startswith("IDEMPOTENCY#APPLY_INVESTIGATION#")
