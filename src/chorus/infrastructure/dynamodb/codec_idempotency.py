"""Idempotency record mapping shared by the Core and Shareable tables.

The record lives in the contextual partition that owns the command, so an ingest key sits in
its community partition, a case command in its case partition, and an action command in its
action partition. The command type, actor hash, and key hash are the only sort-key segments;
no client-supplied text is ever written into a key.
"""

from __future__ import annotations

from typing import Final

from chorus.domain.ids import ActionId
from chorus.infrastructure.dynamodb import keys
from chorus.infrastructure.dynamodb.codec import (
    ATTR_EXPIRES_AT_EPOCH,
    DecodedScope,
    EntityType,
    ItemReader,
    build_entity,
    envelope,
    instant,
    read_envelope,
)
from chorus.infrastructure.dynamodb.codec_fence import decode_entity_refs, encode_entity_refs
from chorus.ports.idempotency import (
    REQUEST_HASH_ATTRIBUTE,
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotencyRecord,
    IdempotencyStatus,
    IdempotentCommand,
)
from chorus.ports.storage import ItemKey, StoredItem, StoredValue, TableName

IDEMPOTENCY_SCHEMA_VERSIONS: Final = frozenset({"idempotency-record/v1"})


def _partition_key(partition: IdempotencyPartition) -> str:
    match partition.kind:
        case IdempotencyPartitionKind.NAMESPACE:
            return keys.namespace_partition(partition.namespace)
        case IdempotencyPartitionKind.COMMUNITY:
            if partition.community_id is None:  # pragma: no cover - guarded by the record
                raise ValueError("community partition requires a community")
            return keys.community_partition(partition.namespace, partition.community_id)
        case IdempotencyPartitionKind.CASE:
            if partition.case_id is None:  # pragma: no cover - guarded by the record
                raise ValueError("case partition requires a case")
            return keys.case_partition(partition.namespace, partition.case_id)
        case IdempotencyPartitionKind.VIEW_CURRENT:
            if partition.case_id is None:  # pragma: no cover - guarded by the record
                raise ValueError("view-current partition requires a case")
            return keys.view_current_partition(partition.namespace, partition.case_id)
        case IdempotencyPartitionKind.ACTION:
            if partition.action_id is None:  # pragma: no cover - guarded by the record
                raise ValueError("action partition requires an action")
            return keys.action_partition(partition.namespace, partition.action_id)
        case _:  # pragma: no cover - closed enum
            raise AssertionError("unreachable idempotency partition kind")


def idempotency_item_key(key: IdempotencyKey, *, table: TableName) -> ItemKey:
    return ItemKey(
        table=table,
        partition_key=_partition_key(key.partition),
        sort_key=keys.idempotency_sort_key(key.command.value, key.actor_id_hash, key.key_hash),
    )


def encode_idempotency(record: IdempotencyRecord, *, table: TableName) -> StoredItem:
    key = idempotency_item_key(record.key, table=table)
    partition = record.key.partition
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.IDEMPOTENCY_RECORD,
        schema_version=record.schema_version,
        key=key,
        namespace=partition.namespace,
        community_id=partition.community_id,
        case_id=partition.case_id,
    )
    item.update(
        {
            "partition_kind": partition.kind.value,
            "action_id": None if partition.action_id is None else str(partition.action_id),
            "command": record.key.command.value,
            "actor_id_hash": record.key.actor_id_hash.value,
            "key_hash": record.key.key_hash.value,
            REQUEST_HASH_ATTRIBUTE: record.request_hash.value,
            "status": record.status.value,
            "result_entity_refs": encode_entity_refs(record.result_entity_refs),
            "response_status": record.response_status,
            "created_at": instant(record.created_at),
            "updated_at": instant(record.updated_at),
            ATTR_EXPIRES_AT_EPOCH: record.expires_at_epoch,
            "version": record.version,
        }
    )
    return item


def decode_idempotency(item: StoredItem) -> tuple[DecodedScope, IdempotencyRecord]:
    reader = ItemReader(item, entity_ref="IDEMPOTENCY_RECORD")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.IDEMPOTENCY_RECORD,
        accepted_schema_versions=IDEMPOTENCY_SCHEMA_VERSIONS,
    )
    partition = build_entity(
        reader.entity_ref,
        IdempotencyPartition,
        kind=reader.enum("partition_kind", IdempotencyPartitionKind),
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=scope.case_id,
        action_id=reader.optional_identifier("action_id", ActionId),
    )
    key = build_entity(
        reader.entity_ref,
        IdempotencyKey,
        partition=partition,
        command=reader.enum("command", IdempotentCommand),
        actor_id_hash=reader.digest("actor_id_hash"),
        key_hash=reader.digest("key_hash"),
    )
    record = build_entity(
        reader.entity_ref,
        IdempotencyRecord,
        key=key,
        request_hash=reader.digest(REQUEST_HASH_ATTRIBUTE),
        status=reader.enum("status", IdempotencyStatus),
        result_entity_refs=decode_entity_refs(reader, "result_entity_refs"),
        response_status=reader.optional_number("response_status"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        expires_at_epoch=reader.number(ATTR_EXPIRES_AT_EPOCH),
        version=reader.number("version"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, record
