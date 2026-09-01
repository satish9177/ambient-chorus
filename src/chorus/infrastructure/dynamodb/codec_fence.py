"""Explicit Core-table mappings for the send-authorization fence and agent invocations."""

from __future__ import annotations

from typing import Final
from uuid import UUID

from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    ExecutionId,
    ViewId,
)
from chorus.infrastructure.dynamodb import keys
from chorus.infrastructure.dynamodb.codec import (
    ATTR_EXPIRES_AT_EPOCH,
    DecodedScope,
    EntityType,
    ItemReader,
    build_entity,
    envelope,
    identifier,
    instant,
    read_envelope,
)
from chorus.infrastructure.dynamodb.codec_core import build_entity_error
from chorus.ports.idempotency import EntityRef
from chorus.ports.records import (
    AgentInvocationOutcome,
    AgentInvocationResult,
    AgentName,
    SendFence,
)
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import ItemKey, StoredItem, StoredValue, TableName

_CORE: Final = TableName.CORE

AGENT_INVOCATION_SCHEMA_VERSIONS: Final = frozenset({"agent-invocation-result/v1"})
SEND_FENCE_SCHEMA_VERSIONS: Final = frozenset({"send-fence/v1"})

ATTR_FENCE_EXECUTION_ID: Final = "execution_id"
ATTR_FENCE_EXPIRES_AT_MICROS: Final = "expires_at_micros"
"""Exact fence deadline every authorization condition compares against.

``expires_at_epoch`` remains on the item purely so DynamoDB TTL can sweep abandoned fences;
it is deliberately never read by a condition.
"""


def encode_entity_refs(refs: tuple[EntityRef, ...]) -> tuple[StoredValue, ...]:
    return tuple(
        {
            "entity_type": ref.entity_type,
            "entity_id": str(ref.entity_id),
            "version": ref.version,
        }
        for ref in refs
    )


def decode_entity_refs(reader: ItemReader, name: str) -> tuple[EntityRef, ...]:
    refs: list[EntityRef] = []
    for index, raw in enumerate(reader.mappings(name)):
        ref_reader = reader.child(raw, f"{name}[{index}]")
        ref = build_entity(
            ref_reader.entity_ref,
            EntityRef,
            entity_type=ref_reader.text("entity_type"),
            entity_id=ref_reader.uuid("entity_id"),
            version=ref_reader.optional_number("version"),
        )
        ref_reader.finish()
        refs.append(ref)
    return tuple(refs)


def agent_invocation_key(scope: CaseScope, invocation_id: UUID) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.case_partition(scope.namespace, scope.case_id),
        sort_key=keys.agent_invocation_sort_key(invocation_id),
    )


def encode_agent_invocation(scope: CaseScope, result: AgentInvocationResult) -> StoredItem:
    key = agent_invocation_key(scope, result.invocation_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.AGENT_INVOCATION_RESULT,
        schema_version=result.schema_version,
        key=key,
        namespace=result.namespace,
        community_id=result.community_id,
        case_id=result.case_id,
    )
    item.update(
        {
            "invocation_id": str(result.invocation_id),
            "agent_name": result.agent_name.value,
            "prompt_version": result.prompt_version,
            "input_hash": result.input_hash.value,
            "output_hash": None if result.output_hash is None else result.output_hash.value,
            "outcome": result.outcome.value,
            "result_refs": encode_entity_refs(result.result_refs),
            "created_at": instant(result.created_at),
        }
    )
    return item


def decode_agent_invocation(item: StoredItem) -> tuple[DecodedScope, AgentInvocationResult]:
    reader = ItemReader(item, entity_ref="AGENT_INVOCATION_RESULT")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.AGENT_INVOCATION_RESULT,
        accepted_schema_versions=AGENT_INVOCATION_SCHEMA_VERSIONS,
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    result = build_entity(
        reader.entity_ref,
        AgentInvocationResult,
        invocation_id=reader.uuid("invocation_id"),
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=scope.case_id,
        agent_name=reader.enum("agent_name", AgentName),
        prompt_version=reader.text("prompt_version"),
        input_hash=reader.digest("input_hash"),
        output_hash=reader.optional_digest("output_hash"),
        outcome=reader.enum("outcome", AgentInvocationOutcome),
        result_refs=decode_entity_refs(reader, "result_refs"),
        created_at=reader.instant("created_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, result


def send_fence_key(scope: CaseScope) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.case_partition(scope.namespace, scope.case_id),
        sort_key=keys.send_fence_sort_key(),
    )


def encode_send_fence(scope: CaseScope, fence: SendFence) -> StoredItem:
    key = send_fence_key(scope)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.SEND_FENCE,
        schema_version=fence.schema_version,
        key=key,
        namespace=fence.namespace,
        community_id=fence.community_id,
        case_id=fence.case_id,
    )
    item.update(
        {
            ATTR_FENCE_EXECUTION_ID: identifier(fence.execution_id),
            "action_id": identifier(fence.action_id),
            "approval_id": identifier(fence.approval_id),
            "view_id": identifier(fence.view_id),
            "authorization_snapshot_hash": fence.authorization_snapshot_hash.value,
            "acquired_at": instant(fence.acquired_at),
            "expires_at": instant(fence.expires_at),
            ATTR_FENCE_EXPIRES_AT_MICROS: fence.expires_at_micros,
            ATTR_EXPIRES_AT_EPOCH: fence.expires_at_epoch,
        }
    )
    return item


def decode_send_fence(item: StoredItem) -> tuple[DecodedScope, SendFence]:
    reader = ItemReader(item, entity_ref="SEND_FENCE")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.SEND_FENCE,
        accepted_schema_versions=SEND_FENCE_SCHEMA_VERSIONS,
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    fence = build_entity(
        reader.entity_ref,
        SendFence,
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=scope.case_id,
        execution_id=reader.identifier(ATTR_FENCE_EXECUTION_ID, ExecutionId),
        action_id=reader.identifier("action_id", ActionId),
        approval_id=reader.identifier("approval_id", ApprovalId),
        view_id=reader.identifier("view_id", ViewId),
        authorization_snapshot_hash=reader.digest("authorization_snapshot_hash"),
        acquired_at=reader.instant("acquired_at"),
        expires_at=reader.instant("expires_at"),
        schema_version=schema_version,
    )
    stored_micros = reader.number(ATTR_FENCE_EXPIRES_AT_MICROS)
    stored_epoch = reader.number(ATTR_EXPIRES_AT_EPOCH)
    reader.finish()
    # Both derived fields must still agree with the instant they were derived from, so a
    # tampered deadline cannot outlive the read that loads it.
    if stored_micros != fence.expires_at_micros:
        raise build_entity_error(reader, ATTR_FENCE_EXPIRES_AT_MICROS)
    if stored_epoch != fence.expires_at_epoch:
        raise build_entity_error(reader, ATTR_EXPIRES_AT_EPOCH)
    return scope, fence
