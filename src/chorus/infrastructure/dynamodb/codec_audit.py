"""Append-only audit item mapping.

Audit items hold identifiers, hashes, decisions, and bounded reason codes. They never hold raw
message, evidence, prompt, completion, contact, unit, or health values, so the audit trail
cannot become a second private corpus.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from chorus.domain.entities import (
    ActorType,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    DisclosureScope,
    Purpose,
)
from chorus.domain.ids import (
    DestinationId,
    EvidenceItemId,
    ExportFactId,
    FactId,
    SafeEvidenceRefId,
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
    instant,
    optional_identifier,
    read_envelope,
)
from chorus.infrastructure.dynamodb.codec_core import build_entity_error
from chorus.ports.records import (
    CompileDecisionOutcome,
    CompiledEvidenceRecord,
    CompiledFactRecord,
    CompileItemOutcome,
    CompilerAuditProjection,
    CompilerGateRecord,
)
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


COMPILE_PROJECTION_SCHEMA_VERSIONS: Final = frozenset({"compiler-audit-projection/v1"})


def compile_projection_key(scope: CaseScope, compile_id: UUID) -> ItemKey:
    return ItemKey(
        table=_AUDIT,
        partition_key=keys.case_partition(scope.namespace, scope.case_id),
        sort_key=keys.compile_audit_sort_key(compile_id),
    )


def encode_compile_projection(
    scope: CaseScope, projection: CompilerAuditProjection, *, retention: AuditRetention
) -> StoredItem:
    """Render the private compile lineage as identifiers, codes, versions, and digests.

    Every string written here is either a closed code the record's own constructor already
    validated or an identifier. There is no free-text field, which is what keeps the row from
    becoming a place a rationale eventually lands.
    """

    key = compile_projection_key(scope, projection.compile_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.COMPILER_AUDIT_PROJECTION,
        schema_version=projection.schema_version,
        key=key,
        namespace=projection.namespace,
        community_id=projection.community_id,
        case_id=projection.case_id,
    )
    item.update(
        {
            "compile_id": str(projection.compile_id),
            "audit_event_id": str(projection.audit_event_id),
            "requested_at": instant(projection.requested_at),
            "created_at": instant(projection.created_at),
            "based_on_case_version": projection.based_on_case_version,
            "compiler_version": projection.compiler_version,
            "policy_version": projection.policy_version,
            "destination_id": projection.destination_id.value,
            "destination_registry_version": projection.destination_registry_version,
            "destination_routing_token": str(projection.destination_routing_token),
            "purpose": projection.purpose.value,
            "decision": projection.decision.value,
            "reason_codes": tuple(projection.reason_codes),
            "gates": tuple(
                {
                    "gate": record.gate,
                    "gate_name": record.gate_name,
                    "outcome": record.outcome,
                    "reason_codes": tuple(record.reason_codes),
                }
                for record in projection.gates
            ),
            "facts": tuple(
                {
                    "fact_id": str(record.fact_id),
                    "necessity": record.necessity,
                    "intended_usage": record.intended_usage,
                    "granted_scope": (
                        None if record.granted_scope is None else record.granted_scope.value
                    ),
                    "outcome": record.outcome.value,
                    "reason_codes": tuple(record.reason_codes),
                    "export_fact_ids": tuple(str(value) for value in record.export_fact_ids),
                    "transformation_rule_id": record.transformation_rule_id,
                }
                for record in projection.facts
            ),
            "evidence": tuple(
                {
                    "source_evidence_id": str(record.source_evidence_id),
                    "outcome": record.outcome.value,
                    "reason_codes": tuple(record.reason_codes),
                    "safe_evidence_ref_id": optional_identifier(record.safe_evidence_ref_id),
                    "export_handle_id": optional_identifier(record.export_handle_id),
                    "derivative_sha256": (
                        None if record.derivative_sha256 is None else record.derivative_sha256.value
                    ),
                }
                for record in projection.evidence
            ),
            "view_id": optional_identifier(projection.view_id),
            "view_hash": None if projection.view_hash is None else projection.view_hash.value,
        }
    )
    expires_at_epoch = retention.expires_at_epoch(projection.created_at)
    if expires_at_epoch is not None:
        item[ATTR_EXPIRES_AT_EPOCH] = expires_at_epoch
    return item


def decode_compile_projection(item: StoredItem) -> tuple[DecodedScope, CompilerAuditProjection]:
    reader = ItemReader(item, entity_ref="COMPILER_AUDIT_PROJECTION")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.COMPILER_AUDIT_PROJECTION,
        accepted_schema_versions=COMPILE_PROJECTION_SCHEMA_VERSIONS,
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    gates: list[CompilerGateRecord] = []
    for index, raw in enumerate(reader.mappings("gates")):
        child = reader.child(raw, f"gates[{index}]")
        gates.append(
            build_entity(
                child.entity_ref,
                CompilerGateRecord,
                gate=child.number("gate"),
                gate_name=child.text("gate_name"),
                outcome=child.text("outcome"),
                reason_codes=child.texts("reason_codes"),
            )
        )
        child.finish()
    facts: list[CompiledFactRecord] = []
    for index, raw in enumerate(reader.mappings("facts")):
        child = reader.child(raw, f"facts[{index}]")
        facts.append(
            build_entity(
                child.entity_ref,
                CompiledFactRecord,
                fact_id=child.identifier("fact_id", FactId),
                necessity=child.text("necessity"),
                intended_usage=child.text("intended_usage"),
                granted_scope=child.optional_enum("granted_scope", DisclosureScope),
                outcome=child.enum("outcome", CompileItemOutcome),
                reason_codes=child.texts("reason_codes"),
                export_fact_ids=child.identifiers("export_fact_ids", ExportFactId),
                transformation_rule_id=child.optional_text("transformation_rule_id"),
            )
        )
        child.finish()
    evidence: list[CompiledEvidenceRecord] = []
    for index, raw in enumerate(reader.mappings("evidence")):
        child = reader.child(raw, f"evidence[{index}]")
        evidence.append(
            build_entity(
                child.entity_ref,
                CompiledEvidenceRecord,
                source_evidence_id=child.identifier("source_evidence_id", EvidenceItemId),
                outcome=child.enum("outcome", CompileItemOutcome),
                reason_codes=child.texts("reason_codes"),
                safe_evidence_ref_id=child.optional_identifier(
                    "safe_evidence_ref_id", SafeEvidenceRefId
                ),
                export_handle_id=child.optional_uuid("export_handle_id"),
                derivative_sha256=child.optional_digest("derivative_sha256"),
            )
        )
        child.finish()
    projection = build_entity(
        reader.entity_ref,
        CompilerAuditProjection,
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=scope.case_id,
        compile_id=reader.uuid("compile_id"),
        audit_event_id=reader.uuid("audit_event_id"),
        requested_at=reader.instant("requested_at"),
        created_at=reader.instant("created_at"),
        based_on_case_version=reader.number("based_on_case_version"),
        compiler_version=reader.text("compiler_version"),
        policy_version=reader.text("policy_version"),
        destination_id=DestinationId(reader.text("destination_id")),
        destination_registry_version=reader.number("destination_registry_version"),
        destination_routing_token=reader.uuid("destination_routing_token"),
        purpose=reader.enum("purpose", Purpose),
        decision=reader.enum("decision", CompileDecisionOutcome),
        reason_codes=reader.texts("reason_codes"),
        gates=tuple(gates),
        facts=tuple(facts),
        evidence=tuple(evidence),
        view_id=reader.optional_identifier("view_id", ViewId),
        view_hash=reader.optional_digest("view_hash"),
        schema_version=schema_version,
    )
    stored_expiry: int | None = None
    if reader.contains(ATTR_EXPIRES_AT_EPOCH):
        stored_expiry = reader.number(ATTR_EXPIRES_AT_EPOCH)
    reader.finish()
    if stored_expiry is not None and stored_expiry != AuditRetention.demo().expires_at_epoch(
        projection.created_at
    ):
        raise build_entity_error(reader, ATTR_EXPIRES_AT_EPOCH)
    return scope, projection
