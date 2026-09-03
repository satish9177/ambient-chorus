"""Explicit Core-table item mappings.

One boring function pair per entity. There is no ORM, no attribute reflection, and no generic
"persist any dataclass" helper: every attribute is named twice on purpose so a schema change
cannot happen silently.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from chorus.domain.entities import (
    ApplicationOperation,
    ApplicationOperationKind,
    ApplicationOperationStatus,
    CaseState,
    Community,
    CommunityCase,
    CommunityMessage,
    CommunityStatus,
    Contributor,
    ContributorStatus,
    DerivationKind,
    EvidenceRoot,
    MessageProcessingStatus,
)
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import (
    ActionId,
    AssessmentId,
    CaseId,
    CommunityId,
    ContributorId,
    DestinationId,
    EvidenceItemId,
    EvidenceRootId,
    FactId,
    MessageId,
    OperationId,
    ReportId,
    SensitiveStr,
    Sha256Digest,
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
    identifiers,
    instant,
    optional_identifier,
    optional_instant,
    optional_sensitive,
    read_envelope,
    sensitive,
)
from chorus.ports.records import (
    ChannelUniquenessLock,
    FeedSignalProjection,
    MonitorApplyProgress,
    MonitorSnapshotChunk,
    MonitorSnapshotKind,
    MonitorSnapshotManifest,
)
from chorus.ports.scopes import CaseScope, CommunityScope, NamespaceScope, OperationScope
from chorus.ports.storage import ItemKey, StoredItem, StoredValue, TableName

_CORE: Final = TableName.CORE

COMMUNITY_SCHEMA_VERSIONS: Final = frozenset({"community/v1"})
CONTRIBUTOR_SCHEMA_VERSIONS: Final = frozenset({"contributor/v1"})
MESSAGE_SCHEMA_VERSIONS: Final = frozenset({"community-message/v1"})
CHANNEL_LOCK_SCHEMA_VERSIONS: Final = frozenset({"channel-uniqueness-lock/v1"})
OPERATION_SCHEMA_VERSIONS: Final = frozenset({"application-operation/v1"})
EVIDENCE_ROOT_SCHEMA_VERSIONS: Final = frozenset({"evidence-root/v1"})
CASE_SCHEMA_VERSIONS: Final = frozenset({"community-case/v1"})
FEED_SIGNAL_SCHEMA_VERSIONS: Final = frozenset({"feed-signal-projection/v2"})
MONITOR_PROGRESS_SCHEMA_VERSIONS: Final = frozenset({"monitor-apply-progress/v1"})
MONITOR_SNAPSHOT_MANIFEST_SCHEMA_VERSIONS: Final = frozenset({"monitor-snapshot-manifest/v1"})
MONITOR_SNAPSHOT_CHUNK_SCHEMA_VERSIONS: Final = frozenset({"monitor-snapshot-chunk/v1"})


def build_entity_error(reader: ItemReader, name: str) -> IntegrityError:
    """Safe integrity failure naming only the attribute, never its stored value."""

    return IntegrityError(f"{reader.entity_ref}:value:{name}")


def _destinations(reader: ItemReader, name: str) -> tuple[DestinationId, ...]:
    values = reader.texts(name)
    try:
        return tuple(DestinationId(value) for value in values)
    except ValueError as error:
        raise build_entity_error(reader, name) from error


# --------------------------------------------------------------------------------------
# Community
# --------------------------------------------------------------------------------------


def community_key(scope: NamespaceScope, community_id: CommunityId) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.namespace_partition(scope.namespace),
        sort_key=keys.community_sort_key(community_id),
    )


def encode_community(scope: NamespaceScope, community: Community) -> StoredItem:
    key = community_key(scope, community.community_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.COMMUNITY,
        schema_version=community.schema_version,
        key=key,
        namespace=community.namespace,
        community_id=community.community_id,
        case_id=None,
    )
    item.update(
        {
            "community_id": identifier(community.community_id),
            "name": community.name,
            "timezone": community.timezone,
            "status": community.status.value,
            "version": community.version,
            "created_at": instant(community.created_at),
            "updated_at": instant(community.updated_at),
        }
    )
    return item


def decode_community(item: StoredItem) -> tuple[DecodedScope, Community]:
    reader = ItemReader(item, entity_ref="COMMUNITY")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.COMMUNITY,
        accepted_schema_versions=COMMUNITY_SCHEMA_VERSIONS,
    )
    community = build_entity(
        reader.entity_ref,
        Community,
        community_id=reader.identifier("community_id", CommunityId),
        namespace=scope.namespace,
        name=reader.text("name"),
        timezone=reader.text("timezone"),
        status=reader.enum("status", CommunityStatus),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, community


# --------------------------------------------------------------------------------------
# Contributor
# --------------------------------------------------------------------------------------


def contributor_key(scope: CommunityScope, contributor_id: ContributorId) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.community_partition(scope.namespace, scope.community_id),
        sort_key=keys.contributor_sort_key(contributor_id),
    )


def encode_contributor(scope: CommunityScope, contributor: Contributor) -> StoredItem:
    key = contributor_key(scope, contributor.contributor_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.CONTRIBUTOR,
        schema_version=contributor.schema_version,
        key=key,
        namespace=contributor.namespace,
        community_id=contributor.community_id,
        case_id=None,
    )
    item.update(
        {
            "contributor_id": identifier(contributor.contributor_id),
            "pseudonym": contributor.pseudonym,
            "display_name": optional_sensitive(contributor.display_name),
            "email": optional_sensitive(contributor.email),
            "status": contributor.status.value,
            "version": contributor.version,
            "created_at": instant(contributor.created_at),
            "updated_at": instant(contributor.updated_at),
        }
    )
    return item


def decode_contributor(item: StoredItem) -> tuple[DecodedScope, Contributor]:
    reader = ItemReader(item, entity_ref="CONTRIBUTOR")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.CONTRIBUTOR,
        accepted_schema_versions=CONTRIBUTOR_SCHEMA_VERSIONS,
    )
    if scope.community_id is None:
        raise build_entity_error(reader, "community_id")
    contributor = build_entity(
        reader.entity_ref,
        Contributor,
        contributor_id=reader.identifier("contributor_id", ContributorId),
        community_id=scope.community_id,
        namespace=scope.namespace,
        pseudonym=reader.text("pseudonym"),
        display_name=reader.optional_sensitive("display_name"),
        email=reader.optional_sensitive("email"),
        status=reader.enum("status", ContributorStatus),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, contributor


# --------------------------------------------------------------------------------------
# Community message and channel uniqueness lock
# --------------------------------------------------------------------------------------


def message_key(scope: CommunityScope, *, sent_at: datetime, message_id: MessageId) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.community_partition(scope.namespace, scope.community_id),
        sort_key=keys.message_sort_key(sent_at, message_id),
    )


def encode_message(scope: CommunityScope, message: CommunityMessage) -> StoredItem:
    key = message_key(scope, sent_at=message.sent_at, message_id=message.message_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.COMMUNITY_MESSAGE,
        schema_version=message.schema_version,
        key=key,
        namespace=message.namespace,
        community_id=message.community_id,
        case_id=None,
    )
    item.update(
        {
            "message_id": identifier(message.message_id),
            "adapter": message.adapter,
            "channel_message_id": message.channel_message_id,
            "contributor_id": optional_identifier(message.contributor_id),
            "sent_at": instant(message.sent_at),
            "received_at": instant(message.received_at),
            "raw_text": sensitive(message.raw_text),
            "attachment_ids": identifiers(message.attachment_ids),
            "content_sha256": message.content_sha256.value,
            "ingestion_idempotency_key": message.ingestion_idempotency_key,
            "processing_status": message.processing_status.value,
            "version": message.version,
            "created_at": instant(message.created_at),
            "updated_at": instant(message.updated_at),
        }
    )
    return item


def decode_message(item: StoredItem) -> tuple[DecodedScope, CommunityMessage]:
    reader = ItemReader(item, entity_ref="COMMUNITY_MESSAGE")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.COMMUNITY_MESSAGE,
        accepted_schema_versions=MESSAGE_SCHEMA_VERSIONS,
    )
    if scope.community_id is None:
        raise build_entity_error(reader, "community_id")
    message = build_entity(
        reader.entity_ref,
        CommunityMessage,
        message_id=reader.identifier("message_id", MessageId),
        community_id=scope.community_id,
        namespace=scope.namespace,
        adapter=reader.text("adapter"),
        channel_message_id=reader.text("channel_message_id"),
        contributor_id=reader.optional_identifier("contributor_id", ContributorId),
        sent_at=reader.instant("sent_at"),
        received_at=reader.instant("received_at"),
        raw_text=reader.sensitive("raw_text"),
        attachment_ids=reader.identifiers("attachment_ids", EvidenceItemId),
        content_sha256=reader.digest("content_sha256"),
        ingestion_idempotency_key=reader.text("ingestion_idempotency_key"),
        processing_status=reader.enum("processing_status", MessageProcessingStatus),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, message


def feed_signal_key(scope: CommunityScope, message_id: MessageId) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.community_partition(scope.namespace, scope.community_id),
        sort_key=keys.feed_signal_sort_key(message_id),
    )


def encode_feed_signal(scope: CommunityScope, signal: FeedSignalProjection) -> StoredItem:
    key = feed_signal_key(scope, signal.message_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.FEED_SIGNAL_PROJECTION,
        schema_version=signal.schema_version,
        key=key,
        namespace=signal.namespace,
        community_id=signal.community_id,
        # The signal names its case in a body attribute rather than the scope envelope. The
        # row lives in the community partition, and putting a case ID in the envelope would
        # make a community-scoped page look like case-scoped data to the scope validator.
        case_id=None,
    )
    item.update(
        {
            "message_id": identifier(signal.message_id),
            "signal_case_id": identifier(signal.case_id),
            "case_version": signal.case_version,
            "label": signal.label,
            "related_message_count": signal.related_message_count,
            "case_state": signal.case_state.value,
            "detected_at": instant(signal.detected_at),
            "version": signal.version,
        }
    )
    return item


def decode_feed_signal(item: StoredItem) -> tuple[DecodedScope, FeedSignalProjection]:
    reader = ItemReader(item, entity_ref="FEED_SIGNAL_PROJECTION")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.FEED_SIGNAL_PROJECTION,
        accepted_schema_versions=FEED_SIGNAL_SCHEMA_VERSIONS,
    )
    if scope.community_id is None:
        raise build_entity_error(reader, "community_id")
    if scope.case_id is not None:
        raise build_entity_error(reader, "case_id")
    signal = build_entity(
        reader.entity_ref,
        FeedSignalProjection,
        namespace=scope.namespace,
        community_id=scope.community_id,
        message_id=reader.identifier("message_id", MessageId),
        case_id=reader.identifier("signal_case_id", CaseId),
        case_version=reader.number("case_version"),
        label=reader.text("label"),
        related_message_count=reader.number("related_message_count"),
        case_state=reader.enum("case_state", CaseState),
        detected_at=reader.instant("detected_at"),
        version=reader.number("version"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, signal


# --------------------------------------------------------------------------------------
# Monitor apply progress
# --------------------------------------------------------------------------------------


def monitor_progress_key(scope: OperationScope, invocation_id: UUID) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.operation_partition(scope.namespace, scope.operation_id),
        sort_key=keys.monitor_progress_sort_key(invocation_id),
    )


def encode_monitor_progress(scope: OperationScope, progress: MonitorApplyProgress) -> StoredItem:
    key = monitor_progress_key(scope, progress.invocation_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.MONITOR_APPLY_PROGRESS,
        schema_version=progress.schema_version,
        key=key,
        namespace=progress.namespace,
        # The row lives in the operation partition, so the scope envelope names neither a
        # community nor a case. The community it belongs to is a body attribute, checked
        # against the caller's scope after decoding like every other cross-scope field.
        community_id=None,
        case_id=None,
    )
    item.update(
        {
            "invocation_id": str(progress.invocation_id),
            "operation_id": identifier(progress.operation_id),
            "progress_community_id": identifier(progress.community_id),
            "input_hash": progress.input_hash.value,
            "output_hash": progress.output_hash.value,
            "plan_hash": progress.plan_hash.value,
            "completed_steps": progress.completed_steps,
            "total_steps": progress.total_steps,
            "version": progress.version,
            "created_at": instant(progress.created_at),
            "updated_at": instant(progress.updated_at),
        }
    )
    return item


def decode_monitor_progress(item: StoredItem) -> tuple[DecodedScope, MonitorApplyProgress]:
    reader = ItemReader(item, entity_ref="MONITOR_APPLY_PROGRESS")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.MONITOR_APPLY_PROGRESS,
        accepted_schema_versions=MONITOR_PROGRESS_SCHEMA_VERSIONS,
    )
    if scope.community_id is not None or scope.case_id is not None:
        raise build_entity_error(reader, "scope")
    progress = build_entity(
        reader.entity_ref,
        MonitorApplyProgress,
        invocation_id=reader.uuid("invocation_id"),
        operation_id=reader.identifier("operation_id", OperationId),
        namespace=scope.namespace,
        community_id=reader.identifier("progress_community_id", CommunityId),
        input_hash=reader.digest("input_hash"),
        output_hash=reader.digest("output_hash"),
        plan_hash=reader.digest("plan_hash"),
        completed_steps=reader.number("completed_steps"),
        total_steps=reader.number("total_steps"),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, progress


# --------------------------------------------------------------------------------------
# Monitor invocation snapshots
# --------------------------------------------------------------------------------------


def monitor_snapshot_manifest_key(
    scope: OperationScope, kind: MonitorSnapshotKind, invocation_id: UUID
) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.operation_partition(scope.namespace, scope.operation_id),
        sort_key=keys.monitor_snapshot_manifest_sort_key(kind.value, invocation_id),
    )


def monitor_snapshot_chunk_key(
    scope: OperationScope, kind: MonitorSnapshotKind, invocation_id: UUID, index: int
) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.operation_partition(scope.namespace, scope.operation_id),
        sort_key=keys.monitor_snapshot_chunk_sort_key(kind.value, invocation_id, index),
    )


def encode_monitor_snapshot_manifest(
    scope: OperationScope, manifest: MonitorSnapshotManifest
) -> StoredItem:
    key = monitor_snapshot_manifest_key(scope, manifest.kind, manifest.invocation_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.MONITOR_SNAPSHOT_MANIFEST,
        schema_version=manifest.schema_version,
        key=key,
        namespace=manifest.namespace,
        community_id=None,
        case_id=None,
    )
    item.update(
        {
            "invocation_id": str(manifest.invocation_id),
            "operation_id": identifier(manifest.operation_id),
            "snapshot_community_id": identifier(manifest.community_id),
            "kind": manifest.kind.value,
            "content_sha256": manifest.content_sha256.value,
            "byte_length": manifest.byte_length,
            "chunk_count": manifest.chunk_count,
            "input_hash": manifest.input_hash.value,
            "output_hash": None if manifest.output_hash is None else manifest.output_hash.value,
            "plan_hash": None if manifest.plan_hash is None else manifest.plan_hash.value,
            "model_profile_hash": (
                None if manifest.model_profile_hash is None else manifest.model_profile_hash.value
            ),
            "provenance_hash": (
                None if manifest.provenance_hash is None else manifest.provenance_hash.value
            ),
            "prompt_version": manifest.prompt_version,
            "created_at": instant(manifest.created_at),
            ATTR_EXPIRES_AT_EPOCH: manifest.expires_at_epoch,
        }
    )
    return item


def decode_monitor_snapshot_manifest(
    item: StoredItem,
) -> tuple[DecodedScope, MonitorSnapshotManifest]:
    reader = ItemReader(item, entity_ref="MONITOR_SNAPSHOT_MANIFEST")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.MONITOR_SNAPSHOT_MANIFEST,
        accepted_schema_versions=MONITOR_SNAPSHOT_MANIFEST_SCHEMA_VERSIONS,
    )
    if scope.community_id is not None or scope.case_id is not None:
        raise build_entity_error(reader, "scope")
    manifest = build_entity(
        reader.entity_ref,
        MonitorSnapshotManifest,
        invocation_id=reader.uuid("invocation_id"),
        operation_id=reader.identifier("operation_id", OperationId),
        namespace=scope.namespace,
        community_id=reader.identifier("snapshot_community_id", CommunityId),
        kind=reader.enum("kind", MonitorSnapshotKind),
        content_sha256=reader.digest("content_sha256"),
        byte_length=reader.number("byte_length"),
        chunk_count=reader.number("chunk_count"),
        input_hash=reader.digest("input_hash"),
        output_hash=reader.optional_digest("output_hash"),
        plan_hash=reader.optional_digest("plan_hash"),
        model_profile_hash=reader.optional_digest("model_profile_hash"),
        provenance_hash=reader.optional_digest("provenance_hash"),
        prompt_version=reader.text("prompt_version"),
        created_at=reader.instant("created_at"),
        expires_at_epoch=reader.number(ATTR_EXPIRES_AT_EPOCH),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, manifest


def encode_monitor_snapshot_chunk(scope: OperationScope, chunk: MonitorSnapshotChunk) -> StoredItem:
    key = monitor_snapshot_chunk_key(scope, chunk.kind, chunk.invocation_id, chunk.index)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.MONITOR_SNAPSHOT_CHUNK,
        schema_version=chunk.schema_version,
        key=key,
        namespace=chunk.namespace,
        community_id=None,
        case_id=None,
    )
    item.update(
        {
            "invocation_id": str(chunk.invocation_id),
            "operation_id": identifier(chunk.operation_id),
            "snapshot_community_id": identifier(chunk.community_id),
            "kind": chunk.kind.value,
            "chunk_index": chunk.index,
            # Canonical UTF-8 text, stored as a string attribute. DynamoDB stores strings as
            # UTF-8, so there is nothing to base64-expand and nothing gained by doing so.
            "content": sensitive(chunk.content),
            ATTR_EXPIRES_AT_EPOCH: chunk.expires_at_epoch,
        }
    )
    return item


def decode_monitor_snapshot_chunk(item: StoredItem) -> tuple[DecodedScope, MonitorSnapshotChunk]:
    reader = ItemReader(item, entity_ref="MONITOR_SNAPSHOT_CHUNK")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.MONITOR_SNAPSHOT_CHUNK,
        accepted_schema_versions=MONITOR_SNAPSHOT_CHUNK_SCHEMA_VERSIONS,
    )
    if scope.community_id is not None or scope.case_id is not None:
        raise build_entity_error(reader, "scope")
    chunk = build_entity(
        reader.entity_ref,
        MonitorSnapshotChunk,
        invocation_id=reader.uuid("invocation_id"),
        operation_id=reader.identifier("operation_id", OperationId),
        namespace=scope.namespace,
        community_id=reader.identifier("snapshot_community_id", CommunityId),
        kind=reader.enum("kind", MonitorSnapshotKind),
        index=reader.number("chunk_index"),
        content=SensitiveStr(reader.text("content")),
        expires_at_epoch=reader.number(ATTR_EXPIRES_AT_EPOCH),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, chunk


def channel_lock_key(
    scope: CommunityScope, *, adapter: str, channel_message_id_sha256: Sha256Digest
) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.community_partition(scope.namespace, scope.community_id),
        sort_key=keys.channel_lock_sort_key(adapter, channel_message_id_sha256),
    )


def encode_channel_lock(scope: CommunityScope, lock: ChannelUniquenessLock) -> StoredItem:
    key = channel_lock_key(
        scope,
        adapter=lock.adapter,
        channel_message_id_sha256=lock.channel_message_id_sha256,
    )
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.CHANNEL_UNIQUENESS_LOCK,
        schema_version=lock.schema_version,
        key=key,
        namespace=lock.namespace,
        community_id=lock.community_id,
        case_id=None,
    )
    item.update(
        {
            "adapter": lock.adapter,
            "channel_message_id_sha256": lock.channel_message_id_sha256.value,
            "message_id": identifier(lock.message_id),
            "content_sha256": lock.content_sha256.value,
            "created_at": instant(lock.created_at),
        }
    )
    return item


def decode_channel_lock(item: StoredItem) -> tuple[DecodedScope, ChannelUniquenessLock]:
    reader = ItemReader(item, entity_ref="CHANNEL_UNIQUENESS_LOCK")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.CHANNEL_UNIQUENESS_LOCK,
        accepted_schema_versions=CHANNEL_LOCK_SCHEMA_VERSIONS,
    )
    if scope.community_id is None:
        raise build_entity_error(reader, "community_id")
    lock = build_entity(
        reader.entity_ref,
        ChannelUniquenessLock,
        namespace=scope.namespace,
        community_id=scope.community_id,
        adapter=reader.text("adapter"),
        channel_message_id_sha256=reader.digest("channel_message_id_sha256"),
        message_id=reader.identifier("message_id", MessageId),
        content_sha256=reader.digest("content_sha256"),
        created_at=reader.instant("created_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, lock


# --------------------------------------------------------------------------------------
# Application operation
# --------------------------------------------------------------------------------------


def operation_key(scope: NamespaceScope, operation_id: OperationId) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.operation_partition(scope.namespace, operation_id),
        sort_key=keys.operation_sort_key(),
    )


def encode_operation(scope: NamespaceScope, operation: ApplicationOperation) -> StoredItem:
    key = operation_key(scope, operation.operation_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.APPLICATION_OPERATION,
        schema_version=operation.schema_version,
        key=key,
        namespace=operation.namespace,
        community_id=None,
        case_id=operation.case_id,
    )
    item.update(
        {
            "operation_id": identifier(operation.operation_id),
            "kind": operation.kind.value,
            "actor_id_hash": operation.actor_id_hash.value,
            "request_hash": operation.request_hash.value,
            "status": operation.status.value,
            "result_refs": identifiers(operation.result_refs),
            "error_code": operation.error_code,
            # The Monitor handover identity: which agent invocation this operation authorizes
            # and the digest of the exact new-message set it may be run over. Identifiers and
            # a hash only -- never the locators themselves, and never message content.
            "monitor_invocation_id": (
                None
                if operation.monitor_invocation_id is None
                else str(operation.monitor_invocation_id)
            ),
            "monitor_locator_hash": (
                None
                if operation.monitor_locator_hash is None
                else operation.monitor_locator_hash.value
            ),
            ATTR_EXPIRES_AT_EPOCH: operation.expires_at_epoch,
            "version": operation.version,
            "created_at": instant(operation.created_at),
            "updated_at": instant(operation.updated_at),
        }
    )
    return item


def decode_operation(item: StoredItem) -> tuple[DecodedScope, ApplicationOperation]:
    reader = ItemReader(item, entity_ref="APPLICATION_OPERATION")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.APPLICATION_OPERATION,
        accepted_schema_versions=OPERATION_SCHEMA_VERSIONS,
    )
    operation = build_entity(
        reader.entity_ref,
        ApplicationOperation,
        operation_id=reader.identifier("operation_id", OperationId),
        kind=reader.enum("kind", ApplicationOperationKind),
        namespace=scope.namespace,
        actor_id_hash=reader.digest("actor_id_hash"),
        case_id=scope.case_id,
        request_hash=reader.digest("request_hash"),
        status=reader.enum("status", ApplicationOperationStatus),
        result_refs=reader.uuids("result_refs"),
        error_code=reader.optional_text("error_code"),
        monitor_invocation_id=reader.optional_uuid("monitor_invocation_id"),
        monitor_locator_hash=reader.optional_digest("monitor_locator_hash"),
        expires_at_epoch=reader.number(ATTR_EXPIRES_AT_EPOCH),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, operation


# --------------------------------------------------------------------------------------
# Evidence root
# --------------------------------------------------------------------------------------


def evidence_root_key(scope: CommunityScope, root_sha256: Sha256Digest) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.community_partition(scope.namespace, scope.community_id),
        sort_key=keys.evidence_root_sort_key(root_sha256),
    )


def encode_evidence_root(scope: CommunityScope, root: EvidenceRoot) -> StoredItem:
    key = evidence_root_key(scope, root.root_sha256)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.EVIDENCE_ROOT,
        schema_version=root.schema_version,
        key=key,
        namespace=root.namespace,
        community_id=root.community_id,
        case_id=None,
    )
    item.update(
        {
            "root_id": identifier(root.root_id),
            "root_sha256": root.root_sha256.value,
            "media_type": root.media_type,
            "first_observed_at": instant(root.first_observed_at),
            "derivation_kind": root.derivation_kind.value,
            "parent_root_id": optional_identifier(root.parent_root_id),
            "version": root.version,
            "created_at": instant(root.created_at),
            "updated_at": instant(root.updated_at),
        }
    )
    return item


def decode_evidence_root(item: StoredItem) -> tuple[DecodedScope, EvidenceRoot]:
    reader = ItemReader(item, entity_ref="EVIDENCE_ROOT")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.EVIDENCE_ROOT,
        accepted_schema_versions=EVIDENCE_ROOT_SCHEMA_VERSIONS,
    )
    if scope.community_id is None:
        raise build_entity_error(reader, "community_id")
    root = build_entity(
        reader.entity_ref,
        EvidenceRoot,
        root_id=reader.identifier("root_id", EvidenceRootId),
        community_id=scope.community_id,
        namespace=scope.namespace,
        root_sha256=reader.digest("root_sha256"),
        media_type=reader.text("media_type"),
        first_observed_at=reader.instant("first_observed_at"),
        derivation_kind=reader.enum("derivation_kind", DerivationKind),
        parent_root_id=reader.optional_identifier("parent_root_id", EvidenceRootId),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, root


# --------------------------------------------------------------------------------------
# Case aggregate
# --------------------------------------------------------------------------------------


def case_key(scope: CaseScope) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.case_partition(scope.namespace, scope.case_id),
        sort_key=keys.case_sort_key(),
    )


def encode_case(scope: CaseScope, case: CommunityCase) -> StoredItem:
    key = case_key(scope)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.COMMUNITY_CASE,
        schema_version=case.schema_version,
        key=key,
        namespace=case.namespace,
        community_id=case.community_id,
        case_id=case.case_id,
    )
    item.update(
        {
            "title": case.title,
            "issue_type": case.issue_type,
            "state": case.state.value,
            "report_ids": identifiers(case.report_ids),
            "fact_ids": identifiers(case.fact_ids),
            "assessment_id": optional_identifier(case.assessment_id),
            "current_view_id": optional_identifier(case.current_view_id),
            "current_action_id": optional_identifier(case.current_action_id),
            "corroboration_source_count": case.corroboration_source_count,
            "state_reason_code": case.state_reason_code,
            "version": case.version,
            "created_at": instant(case.created_at),
            "updated_at": instant(case.updated_at),
            "resolved_at": optional_instant(case.resolved_at),
            "closed_at": optional_instant(case.closed_at),
        }
    )
    return item


def decode_case(item: StoredItem) -> tuple[DecodedScope, CommunityCase]:
    reader = ItemReader(item, entity_ref="COMMUNITY_CASE")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.COMMUNITY_CASE,
        accepted_schema_versions=CASE_SCHEMA_VERSIONS,
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    case = build_entity(
        reader.entity_ref,
        CommunityCase,
        case_id=scope.case_id,
        community_id=scope.community_id,
        namespace=scope.namespace,
        title=reader.text("title"),
        issue_type=reader.text("issue_type"),
        state=reader.enum("state", CaseState),
        report_ids=reader.identifiers("report_ids", ReportId),
        fact_ids=reader.identifiers("fact_ids", FactId),
        assessment_id=reader.optional_identifier("assessment_id", AssessmentId),
        current_view_id=reader.optional_identifier("current_view_id", ViewId),
        current_action_id=reader.optional_identifier("current_action_id", ActionId),
        corroboration_source_count=reader.number("corroboration_source_count"),
        state_reason_code=reader.text("state_reason_code"),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        resolved_at=reader.optional_instant("resolved_at"),
        closed_at=reader.optional_instant("closed_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, case
