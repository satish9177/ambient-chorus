"""Append-only audit item mapping.

Audit items hold identifiers, hashes, decisions, and bounded reason codes. They never hold raw
message, evidence, prompt, completion, contact, unit, or health values, so the audit trail
cannot become a second private corpus.
"""

from __future__ import annotations

from typing import Final

from chorus.domain.entities import (
    ActorType,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
)
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
from chorus.infrastructure.dynamodb.codec_core import build_entity_error
from chorus.ports.retention import AuditRetention
from chorus.ports.scopes import CaseScope, NamespaceScope
from chorus.ports.storage import ItemKey, StoredItem, StoredValue, TableName

_AUDIT: Final = TableName.AUDIT

AUDIT_SCHEMA_VERSIONS: Final = frozenset({"audit-event/v1"})


def case_event_key(scope: CaseScope, event: AuditEvent) -> ItemKey:
    return ItemKey(
        table=_AUDIT,
        partition_key=keys.case_partition(scope.namespace, scope.case_id),
        sort_key=keys.audit_event_sort_key(event.occurred_at, event.audit_event_id),
    )


def namespace_event_key(scope: NamespaceScope, event: AuditEvent) -> ItemKey:
    return ItemKey(
        table=_AUDIT,
        partition_key=keys.namespace_partition(scope.namespace),
        sort_key=keys.audit_event_sort_key(event.occurred_at, event.audit_event_id),
    )


def _encode(key: ItemKey, event: AuditEvent, retention: AuditRetention) -> StoredItem:
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.AUDIT_EVENT,
        schema_version=event.schema_version,
        key=key,
        namespace=event.namespace,
        community_id=event.community_id,
        case_id=event.case_id,
    )
    item.update(
        {
            "audit_event_id": str(event.audit_event_id),
            "actor_type": event.actor_type.value,
            "actor_id_hash": event.actor_id_hash.value,
            "event_type": event.event_type,
            "occurred_at": instant(event.occurred_at),
            "correlation_id": str(event.correlation_id),
            "causation_id": None if event.causation_id is None else str(event.causation_id),
            "idempotency_key_hash": (
                None if event.idempotency_key_hash is None else event.idempotency_key_hash.value
            ),
            "entity_refs": tuple(
                {
                    "entity_type": ref.entity_type,
                    "entity_id": str(ref.entity_id),
                    "version": ref.version,
                }
                for ref in event.entity_refs
            ),
            "decision": event.decision.value,
            "reason_codes": tuple(event.reason_codes),
            "safe_details": {
                "count": event.safe_details.count,
                "rule_id": event.safe_details.rule_id,
            },
            "input_hash": None if event.input_hash is None else event.input_hash.value,
            "output_hash": None if event.output_hash is None else event.output_hash.value,
        }
    )
    expires_at_epoch = retention.expires_at_epoch(event.occurred_at)
    if expires_at_epoch is not None:
        item[ATTR_EXPIRES_AT_EPOCH] = expires_at_epoch
    return item


def encode_case_event(
    scope: CaseScope, event: AuditEvent, *, retention: AuditRetention
) -> StoredItem:
    return _encode(case_event_key(scope, event), event, retention)


def encode_namespace_event(
    scope: NamespaceScope, event: AuditEvent, *, retention: AuditRetention
) -> StoredItem:
    return _encode(namespace_event_key(scope, event), event, retention)


def decode_audit_event(item: StoredItem) -> tuple[DecodedScope, AuditEvent]:
    reader = ItemReader(item, entity_ref="AUDIT_EVENT")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.AUDIT_EVENT,
        accepted_schema_versions=AUDIT_SCHEMA_VERSIONS,
    )
    refs: list[AuditEntityRef] = []
    for index, raw in enumerate(reader.mappings("entity_refs")):
        ref_reader = reader.child(raw, f"entity_refs[{index}]")
        ref = build_entity(
            ref_reader.entity_ref,
            AuditEntityRef,
            entity_type=ref_reader.text("entity_type"),
            entity_id=ref_reader.uuid("entity_id"),
            version=ref_reader.optional_number("version"),
        )
        ref_reader.finish()
        refs.append(ref)
    details_reader = reader.child(reader.mapping("safe_details"), "safe_details")
    safe_details = build_entity(
        details_reader.entity_ref,
        AuditDetails,
        count=details_reader.optional_number("count"),
        rule_id=details_reader.optional_text("rule_id"),
    )
    details_reader.finish()
    event = build_entity(
        reader.entity_ref,
        AuditEvent,
        audit_event_id=reader.uuid("audit_event_id"),
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=scope.case_id,
        actor_type=reader.enum("actor_type", ActorType),
        actor_id_hash=reader.digest("actor_id_hash"),
        event_type=reader.text("event_type"),
        occurred_at=reader.instant("occurred_at"),
        correlation_id=reader.uuid("correlation_id"),
        causation_id=reader.optional_uuid("causation_id"),
        idempotency_key_hash=reader.optional_digest("idempotency_key_hash"),
        entity_refs=tuple(refs),
        decision=reader.enum("decision", AuditDecision),
        reason_codes=reader.texts("reason_codes"),
        safe_details=safe_details,
        input_hash=reader.optional_digest("input_hash"),
        output_hash=reader.optional_digest("output_hash"),
        schema_version=schema_version,
    )
    # Whether the attribute exists at all is a deployment retention choice, so its absence is
    # valid. When it is present it must still be a TTL this codec could have written, which
    # keeps an arbitrary injected expiry from riding along inside an audit item.
    stored_expiry: int | None = None
    if reader.contains(ATTR_EXPIRES_AT_EPOCH):
        stored_expiry = reader.number(ATTR_EXPIRES_AT_EPOCH)
    reader.finish()
    if stored_expiry is not None and stored_expiry != AuditRetention.demo().expires_at_epoch(
        event.occurred_at
    ):
        raise build_entity_error(reader, ATTR_EXPIRES_AT_EPOCH)
    return scope, event
