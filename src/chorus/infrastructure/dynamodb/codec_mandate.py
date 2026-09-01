"""Explicit Core-table item mappings for immutable mandate versions and their pointers."""

from __future__ import annotations

from typing import Final

from chorus.domain.entities import DisclosureScope, MandateStatus, Purpose
from chorus.domain.ids import (
    ContributorId,
    DestinationId,
    FactId,
    MandateId,
)
from chorus.domain.mandates import (
    CurrentMandatePointer,
    DisclosureMandate,
    FactGrant,
    IdentityGrant,
)
from chorus.infrastructure.dynamodb import keys
from chorus.infrastructure.dynamodb.codec import (
    DecodedScope,
    EntityType,
    ItemReader,
    build_entity,
    envelope,
    identifier,
    instant,
    optional_identifier,
    optional_instant,
    read_envelope,
)
from chorus.infrastructure.dynamodb.codec_core import build_entity_error
from chorus.ports.records import FactMandateAssociation, StoredCurrentMandatePointer
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import ItemKey, StoredItem, StoredValue, TableName

_CORE: Final = TableName.CORE

MANDATE_SCHEMA_VERSIONS: Final = frozenset({"disclosure-mandate/v1"})
MANDATE_POINTER_SCHEMA_VERSIONS: Final = frozenset({"current-mandate-pointer/v1"})
FACT_MANDATE_SCHEMA_VERSIONS: Final = frozenset({"fact-mandate-association/v1"})


def mandate_version_key(scope: CaseScope, mandate_id: MandateId, version: int) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.case_partition(scope.namespace, scope.case_id),
        sort_key=keys.mandate_version_sort_key(mandate_id, version),
    )


def encode_mandate(scope: CaseScope, mandate: DisclosureMandate) -> StoredItem:
    key = mandate_version_key(scope, mandate.mandate_id, mandate.version)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.DISCLOSURE_MANDATE,
        schema_version=mandate.schema_version,
        key=key,
        namespace=mandate.namespace,
        community_id=mandate.community_id,
        case_id=mandate.case_id,
    )
    item.update(
        {
            "mandate_id": identifier(mandate.mandate_id),
            "version": mandate.version,
            "contributor_id": identifier(mandate.contributor_id),
            "status": mandate.status.value,
            "fact_grants": tuple(
                {
                    "fact_id": identifier(grant.fact_id),
                    "max_scope": grant.max_scope.value,
                    "allow_safe_transformation": grant.allow_safe_transformation,
                }
                for grant in mandate.fact_grants
            ),
            "identity_grant": {
                "externally_shareable": mandate.identity_grant.externally_shareable,
                "max_scope": mandate.identity_grant.max_scope.value,
            },
            "allowed_destination_ids": tuple(
                destination.value for destination in mandate.allowed_destination_ids
            ),
            "allowed_purposes": tuple(purpose.value for purpose in mandate.allowed_purposes),
            "valid_from": instant(mandate.valid_from),
            "expires_at": optional_instant(mandate.expires_at),
            "proposed_at": instant(mandate.proposed_at),
            "decided_at": optional_instant(mandate.decided_at),
            "revoked_at": optional_instant(mandate.revoked_at),
            "decision_actor_id": optional_identifier(mandate.decision_actor_id),
            "supersedes_version": mandate.supersedes_version,
            "terms_hash": mandate.terms_hash.value,
            "created_at": instant(mandate.created_at),
            "updated_at": instant(mandate.updated_at),
        }
    )
    return item


def decode_mandate(item: StoredItem) -> tuple[DecodedScope, DisclosureMandate]:
    reader = ItemReader(item, entity_ref="DISCLOSURE_MANDATE")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.DISCLOSURE_MANDATE,
        accepted_schema_versions=MANDATE_SCHEMA_VERSIONS,
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    grants: list[FactGrant] = []
    for index, raw in enumerate(reader.mappings("fact_grants")):
        grant_reader = reader.child(raw, f"fact_grants[{index}]")
        grant = build_entity(
            grant_reader.entity_ref,
            FactGrant,
            fact_id=grant_reader.identifier("fact_id", FactId),
            max_scope=grant_reader.enum("max_scope", DisclosureScope),
            allow_safe_transformation=grant_reader.flag("allow_safe_transformation"),
        )
        grant_reader.finish()
        grants.append(grant)
    identity_reader = reader.child(reader.mapping("identity_grant"), "identity_grant")
    identity_grant = build_entity(
        identity_reader.entity_ref,
        IdentityGrant,
        externally_shareable=identity_reader.flag("externally_shareable"),
        max_scope=identity_reader.enum("max_scope", DisclosureScope),
    )
    identity_reader.finish()
    try:
        destinations = tuple(
            DestinationId(value) for value in reader.texts("allowed_destination_ids")
        )
    except ValueError as error:
        raise build_entity_error(reader, "allowed_destination_ids") from error
    try:
        purposes = tuple(Purpose(value) for value in reader.texts("allowed_purposes"))
    except ValueError as error:
        raise build_entity_error(reader, "allowed_purposes") from error
    mandate = build_entity(
        reader.entity_ref,
        DisclosureMandate,
        mandate_id=reader.identifier("mandate_id", MandateId),
        version=reader.number("version"),
        case_id=scope.case_id,
        community_id=scope.community_id,
        contributor_id=reader.identifier("contributor_id", ContributorId),
        namespace=scope.namespace,
        status=reader.enum("status", MandateStatus),
        fact_grants=tuple(grants),
        identity_grant=identity_grant,
        allowed_destination_ids=destinations,
        allowed_purposes=purposes,
        valid_from=reader.instant("valid_from"),
        expires_at=reader.optional_instant("expires_at"),
        proposed_at=reader.instant("proposed_at"),
        decided_at=reader.optional_instant("decided_at"),
        revoked_at=reader.optional_instant("revoked_at"),
        decision_actor_id=reader.optional_identifier("decision_actor_id", ContributorId),
        supersedes_version=reader.optional_number("supersedes_version"),
        terms_hash=reader.digest("terms_hash"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, mandate


def mandate_pointer_key(scope: CaseScope, mandate_id: MandateId) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.case_partition(scope.namespace, scope.case_id),
        sort_key=keys.mandate_current_sort_key(mandate_id),
    )


def encode_mandate_pointer(scope: CaseScope, stored: StoredCurrentMandatePointer) -> StoredItem:
    key = mandate_pointer_key(scope, stored.pointer.mandate_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.CURRENT_MANDATE_POINTER,
        schema_version=stored.schema_version,
        key=key,
        namespace=stored.namespace,
        community_id=stored.community_id,
        case_id=stored.pointer.case_id,
    )
    item.update(
        {
            "mandate_id": identifier(stored.pointer.mandate_id),
            "mandate_version": stored.pointer.version,
            "contributor_id": identifier(stored.pointer.contributor_id),
            "terms_hash": stored.pointer.terms_hash.value,
            "status": stored.status.value,
            "version": stored.version,
            "created_at": instant(stored.created_at),
            "updated_at": instant(stored.updated_at),
        }
    )
    return item


def decode_mandate_pointer(
    item: StoredItem,
) -> tuple[DecodedScope, StoredCurrentMandatePointer]:
    reader = ItemReader(item, entity_ref="CURRENT_MANDATE_POINTER")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.CURRENT_MANDATE_POINTER,
        accepted_schema_versions=MANDATE_POINTER_SCHEMA_VERSIONS,
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    pointer = build_entity(
        reader.entity_ref,
        CurrentMandatePointer,
        mandate_id=reader.identifier("mandate_id", MandateId),
        version=reader.number("mandate_version"),
        case_id=scope.case_id,
        contributor_id=reader.identifier("contributor_id", ContributorId),
        terms_hash=reader.digest("terms_hash"),
    )
    stored = build_entity(
        reader.entity_ref,
        StoredCurrentMandatePointer,
        namespace=scope.namespace,
        community_id=scope.community_id,
        pointer=pointer,
        status=reader.enum("status", MandateStatus),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, stored


def fact_mandate_key(scope: CaseScope, fact_id: FactId, mandate_id: MandateId) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.case_partition(scope.namespace, scope.case_id),
        sort_key=keys.fact_mandate_sort_key(fact_id, mandate_id),
    )


def encode_fact_mandate(scope: CaseScope, association: FactMandateAssociation) -> StoredItem:
    key = fact_mandate_key(scope, association.fact_id, association.mandate_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.FACT_MANDATE_ASSOCIATION,
        schema_version=association.schema_version,
        key=key,
        namespace=association.namespace,
        community_id=association.community_id,
        case_id=association.case_id,
    )
    item.update(
        {
            "fact_id": identifier(association.fact_id),
            "mandate_id": identifier(association.mandate_id),
            "mandate_version": association.mandate_version,
            "terms_hash": association.terms_hash.value,
            "contributor_id": identifier(association.contributor_id),
            "created_at": instant(association.created_at),
        }
    )
    return item


def decode_fact_mandate(item: StoredItem) -> tuple[DecodedScope, FactMandateAssociation]:
    reader = ItemReader(item, entity_ref="FACT_MANDATE_ASSOCIATION")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.FACT_MANDATE_ASSOCIATION,
        accepted_schema_versions=FACT_MANDATE_SCHEMA_VERSIONS,
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    association = build_entity(
        reader.entity_ref,
        FactMandateAssociation,
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=scope.case_id,
        fact_id=reader.identifier("fact_id", FactId),
        mandate_id=reader.identifier("mandate_id", MandateId),
        mandate_version=reader.number("mandate_version"),
        terms_hash=reader.digest("terms_hash"),
        contributor_id=reader.identifier("contributor_id", ContributorId),
        created_at=reader.instant("created_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, association
