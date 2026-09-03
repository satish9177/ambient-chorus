"""Durable command idempotency and the commit proof used to resolve unknown outcomes.

The same key with the same request hash replays a recorded outcome; the same key with a
different request hash is a conflict, never an overwrite. The record's TTL is cleanup only:
``send_attempt_is_authoritative`` exists so callers reason about an execution's own recorded
state, which stays authoritative after the idempotency record has expired.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from chorus.domain.ids import Sha256Digest
from chorus.infrastructure.dynamodb import codec_idempotency
from chorus.infrastructure.dynamodb.guards import (
    UNCHECKED,
    create_operation,
    replace_operation,
    require_same,
    validate_scope,
)
from chorus.ports.errors import IdempotencyConflictError, PersistenceConflictError
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyFailedFinal,
    IdempotencyInProgress,
    IdempotencyKey,
    IdempotencyOutcome,
    IdempotencyRecord,
    IdempotencyReplay,
    IdempotencyStarted,
    IdempotencyStatus,
    retention_seconds,
)
from chorus.ports.storage import PutItem, StorageDriver, TableName
from chorus.ports.unit_of_work import CommitProof


def _expiry(key: IdempotencyKey, now: datetime) -> int:
    return int(now.timestamp()) + retention_seconds(key.command)


@dataclass(slots=True)
class IdempotencyRepository:
    """Idempotency records for one physical table."""

    driver: StorageDriver
    table: TableName

    async def load(self, key: IdempotencyKey) -> IdempotencyRecord | None:
        item_key = codec_idempotency.idempotency_item_key(key, table=self.table)
        item = await self.driver.get_item(item_key, consistent=True)
        if item is None:
            return None
        decoded, record = codec_idempotency.decode_idempotency(item)
        validate_scope(
            decoded,
            key=item_key,
            entity_ref="IDEMPOTENCY_RECORD",
            namespace=key.partition.namespace,
            community_id=key.partition.community_id,
            case_id=UNCHECKED,
        )
        require_same(decoded.case_id, key.partition.case_id, "IDEMPOTENCY_RECORD")
        require_same(record.key, key, "IDEMPOTENCY_RECORD")
        return record

    def _classify(
        self, record: IdempotencyRecord, request_hash: Sha256Digest
    ) -> IdempotencyOutcome:
        if record.request_hash != request_hash:
            raise IdempotencyConflictError("IDEMPOTENCY_RECORD")
        match record.status:
            case IdempotencyStatus.COMPLETED:
                return IdempotencyReplay(record=record)
            case IdempotencyStatus.IN_PROGRESS:
                return IdempotencyInProgress(record=record)
            case IdempotencyStatus.FAILED_FINAL:
                return IdempotencyFailedFinal(record=record)
            case _:  # pragma: no cover - closed enum
                raise AssertionError("unreachable idempotency status")

    async def begin(
        self, key: IdempotencyKey, *, request_hash: Sha256Digest, now: datetime
    ) -> IdempotencyOutcome:
        """Claim the key, or classify the existing record as replay, in-progress, or conflict."""

        record = IdempotencyRecord(
            key=key,
            request_hash=request_hash,
            status=IdempotencyStatus.IN_PROGRESS,
            result_entity_refs=(),
            response_status=None,
            created_at=now,
            updated_at=now,
            expires_at_epoch=_expiry(key, now),
            version=1,
        )
        operation = create_operation(
            codec_idempotency.idempotency_item_key(key, table=self.table),
            codec_idempotency.encode_idempotency(record, table=self.table),
        )
        try:
            await self.driver.write_item(operation)
        except PersistenceConflictError:
            existing = await self.load(key)
            if existing is None:
                raise
            return self._classify(existing, request_hash)
        return IdempotencyStarted(record=record)

    def stage_create_completed(
        self,
        key: IdempotencyKey,
        *,
        request_hash: Sha256Digest,
        result_entity_refs: tuple[EntityRef, ...],
        response_status: int,
        now: datetime,
    ) -> PutItem:
        """Stage the create-only completed record that doubles as this plan's commit proof."""

        record = IdempotencyRecord(
            key=key,
            request_hash=request_hash,
            status=IdempotencyStatus.COMPLETED,
            result_entity_refs=result_entity_refs,
            response_status=response_status,
            created_at=now,
            updated_at=now,
            expires_at_epoch=_expiry(key, now),
            version=1,
        )
        return create_operation(
            codec_idempotency.idempotency_item_key(key, table=self.table),
            codec_idempotency.encode_idempotency(record, table=self.table),
        )

    def stage_complete(
        self,
        record: IdempotencyRecord,
        *,
        result_entity_refs: tuple[EntityRef, ...],
        response_status: int,
        now: datetime,
    ) -> PutItem:
        return self._stage_transition(
            record,
            status=IdempotencyStatus.COMPLETED,
            result_entity_refs=result_entity_refs,
            response_status=response_status,
            now=now,
        )

    def stage_fail_final(
        self, record: IdempotencyRecord, *, response_status: int, now: datetime
    ) -> PutItem:
        return self._stage_transition(
            record,
            status=IdempotencyStatus.FAILED_FINAL,
            result_entity_refs=(),
            response_status=response_status,
            now=now,
        )

    def _stage_transition(
        self,
        record: IdempotencyRecord,
        *,
        status: IdempotencyStatus,
        result_entity_refs: tuple[EntityRef, ...],
        response_status: int,
        now: datetime,
    ) -> PutItem:
        if record.status is not IdempotencyStatus.IN_PROGRESS:
            raise ValueError("only an in-progress record may transition to a final outcome")
        updated = replace(
            record,
            status=status,
            result_entity_refs=result_entity_refs,
            response_status=response_status,
            updated_at=now,
            version=record.version + 1,
        )
        return replace_operation(
            codec_idempotency.idempotency_item_key(record.key, table=self.table),
            codec_idempotency.encode_idempotency(updated, table=self.table),
            expected_version=record.version,
            new_version=updated.version,
        )

    def commit_proof(self, key: IdempotencyKey, *, request_hash: Sha256Digest) -> CommitProof:
        """Return the item whose presence proves the owning transaction committed."""

        return CommitProof(
            key=codec_idempotency.idempotency_item_key(key, table=self.table),
            request_hash=request_hash,
        )

    def completion_proof(self, record: IdempotencyRecord) -> CommitProof:
        """Return the proof for the transaction that completes this reserved record.

        Deliberately keyed on the record rather than on the key alone: what proves the
        completing transaction committed is the *version* the record reaches, and only the
        record the caller reserved knows which version that is.
        """

        if record.status is not IdempotencyStatus.IN_PROGRESS:
            raise ValueError("only an in-progress record can be completed by a plan")
        return CommitProof(
            key=codec_idempotency.idempotency_item_key(record.key, table=self.table),
            request_hash=record.request_hash,
            completed_version=record.version + 1,
        )
