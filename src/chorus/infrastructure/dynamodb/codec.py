"""Strict item envelope, typed attribute readers, and fail-closed decoding primitives.

Every persisted item carries an explicit ``entity_type`` discriminator, a ``schema_version``,
and the namespace/community/case it belongs to. Decoding is exact: a reader records every
attribute it consumes and ``finish`` rejects any unread attribute, so foreign, stale, or
injected data cannot ride along inside an authorization artifact.

Malformed, unknown, or out-of-domain stored data always raises ``IntegrityError``. There is no
best-effort path, no ``pickle``, no ``eval``, no dynamic class lookup, and no reflective
"serialize anything" behaviour.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeVar
from uuid import UUID

from chorus.domain.errors import IntegrityError
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    Namespace,
    SensitiveStr,
    Sha256Digest,
    UUIDIdentifier,
)
from chorus.domain.time import format_utc, parse_utc
from chorus.ports.storage import ItemKey, StoredItem, StoredValue

ATTR_PARTITION_KEY = "PK"
ATTR_SORT_KEY = "SK"
ATTR_ENTITY_TYPE = "entity_type"
ATTR_SCHEMA_VERSION = "schema_version"
ATTR_NAMESPACE = "namespace"
ATTR_COMMUNITY_ID = "community_id"
ATTR_CASE_ID = "case_id"
ATTR_VERSION = "version"
ATTR_EXPIRES_AT_EPOCH = "expires_at_epoch"


class EntityType(StrEnum):
    """Closed discriminator set; an unknown value fails closed."""

    COMMUNITY = "COMMUNITY"
    CONTRIBUTOR = "CONTRIBUTOR"
    COMMUNITY_MESSAGE = "COMMUNITY_MESSAGE"
    CHANNEL_UNIQUENESS_LOCK = "CHANNEL_UNIQUENESS_LOCK"
    APPLICATION_OPERATION = "APPLICATION_OPERATION"
    EVIDENCE_ROOT = "EVIDENCE_ROOT"
    EVIDENCE_ROOT_LOCATOR = "EVIDENCE_ROOT_LOCATOR"
    FEED_SIGNAL_PROJECTION = "FEED_SIGNAL_PROJECTION"
    MONITOR_APPLY_PROGRESS = "MONITOR_APPLY_PROGRESS"
    MONITOR_SNAPSHOT_MANIFEST = "MONITOR_SNAPSHOT_MANIFEST"
    MONITOR_SNAPSHOT_CHUNK = "MONITOR_SNAPSHOT_CHUNK"
    COMMUNITY_CASE = "COMMUNITY_CASE"
    REPORT = "REPORT"
    FACT = "FACT"
    EVIDENCE_ITEM = "EVIDENCE_ITEM"
    INVESTIGATION_ASSESSMENT = "INVESTIGATION_ASSESSMENT"
    DISCLOSURE_MANDATE = "DISCLOSURE_MANDATE"
    CURRENT_MANDATE_POINTER = "CURRENT_MANDATE_POINTER"
    FACT_MANDATE_ASSOCIATION = "FACT_MANDATE_ASSOCIATION"
    AGENT_INVOCATION_RESULT = "AGENT_INVOCATION_RESULT"
    SEND_FENCE = "SEND_FENCE"
    IDEMPOTENCY_RECORD = "IDEMPOTENCY_RECORD"
    SHAREABLE_VIEW = "SHAREABLE_VIEW"
    CURRENT_VIEW_POINTER = "CURRENT_VIEW_POINTER"
    VIEW_HISTORY_LOCATOR = "VIEW_HISTORY_LOCATOR"
    ACTION_PROPOSAL = "ACTION_PROPOSAL"
    APPROVAL = "APPROVAL"
    ACTION_EXECUTION = "ACTION_EXECUTION"
    CURRENT_ACTION_POINTER = "CURRENT_ACTION_POINTER"
    ACTION_HISTORY_LOCATOR = "ACTION_HISTORY_LOCATOR"
    COMMITMENT = "COMMITMENT"
    AUDIT_EVENT = "AUDIT_EVENT"
    COMPILER_AUDIT_PROJECTION = "COMPILER_AUDIT_PROJECTION"


@dataclass(frozen=True, slots=True, kw_only=True)
class DecodedScope:
    """Scope attributes recovered from a stored item before its entity is trusted."""

    partition_key: str
    sort_key: str
    namespace: Namespace
    community_id: CommunityId | None
    case_id: CaseId | None


IdentifierT = TypeVar("IdentifierT", bound=UUIDIdentifier)
EnumT = TypeVar("EnumT", bound=StrEnum)


def _fail(entity_ref: str, detail: str) -> IntegrityError:
    """Build a safe integrity error; ``detail`` is a fixed code, never stored content."""

    return IntegrityError(f"{entity_ref}:{detail}")


class ItemReader:
    """Exact attribute reader; unread attributes are an integrity failure."""

    __slots__ = ("_consumed", "_item", "_ref")

    def __init__(self, item: StoredItem, *, entity_ref: str) -> None:
        self._item = item
        self._ref = entity_ref
        self._consumed: set[str] = set()

    def _raw(self, name: str) -> StoredValue:
        if name not in self._item:
            raise _fail(self._ref, f"missing:{name}")
        self._consumed.add(name)
        return self._item[name]

    def text(self, name: str) -> str:
        value = self._raw(name)
        if not isinstance(value, str):
            raise _fail(self._ref, f"type:{name}")
        return value

    def optional_text(self, name: str) -> str | None:
        value = self._raw(name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise _fail(self._ref, f"type:{name}")
        return value

    def sensitive(self, name: str) -> SensitiveStr:
        return self._wrap(name, self.text(name))

    def optional_sensitive(self, name: str) -> SensitiveStr | None:
        value = self.optional_text(name)
        return None if value is None else self._wrap(name, value)

    def _wrap(self, name: str, value: str) -> SensitiveStr:
        try:
            return SensitiveStr(value)
        except ValueError as error:
            raise _fail(self._ref, f"value:{name}") from error

    def number(self, name: str) -> int:
        value = self._raw(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(self._ref, f"type:{name}")
        return value

    def optional_number(self, name: str) -> int | None:
        value = self._raw(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(self._ref, f"type:{name}")
        return value

    def flag(self, name: str) -> bool:
        value = self._raw(name)
        if not isinstance(value, bool):
            raise _fail(self._ref, f"type:{name}")
        return value

    def digest(self, name: str) -> Sha256Digest:
        try:
            return Sha256Digest(self.text(name))
        except ValueError as error:
            raise _fail(self._ref, f"value:{name}") from error

    def optional_digest(self, name: str) -> Sha256Digest | None:
        value = self.optional_text(name)
        if value is None:
            return None
        try:
            return Sha256Digest(value)
        except ValueError as error:
            raise _fail(self._ref, f"value:{name}") from error

    def _parse_uuid(self, name: str, value: str) -> UUID:
        try:
            parsed = UUID(value)
        except ValueError as error:
            raise _fail(self._ref, f"value:{name}") from error
        if str(parsed) != value:
            raise _fail(self._ref, f"value:{name}")
        return parsed

    def identifier(self, name: str, identifier_type: type[IdentifierT]) -> IdentifierT:
        return identifier_type(self._parse_uuid(name, self.text(name)))

    def optional_identifier(
        self, name: str, identifier_type: type[IdentifierT]
    ) -> IdentifierT | None:
        value = self.optional_text(name)
        if value is None:
            return None
        return identifier_type(self._parse_uuid(name, value))

    def uuid(self, name: str) -> UUID:
        return self._parse_uuid(name, self.text(name))

    def optional_uuid(self, name: str) -> UUID | None:
        value = self.optional_text(name)
        if value is None:
            return None
        return self._parse_uuid(name, value)

    def namespace(self, name: str) -> Namespace:
        try:
            return Namespace(self.text(name))
        except ValueError as error:
            raise _fail(self._ref, f"value:{name}") from error

    def instant(self, name: str) -> datetime:
        try:
            return parse_utc(self.text(name))
        except ValueError as error:
            raise _fail(self._ref, f"value:{name}") from error

    def optional_instant(self, name: str) -> datetime | None:
        value = self.optional_text(name)
        if value is None:
            return None
        try:
            return parse_utc(value)
        except ValueError as error:
            raise _fail(self._ref, f"value:{name}") from error

    def enum(self, name: str, enum_type: type[EnumT]) -> EnumT:
        value = self.text(name)
        try:
            return enum_type(value)
        except ValueError as error:
            raise _fail(self._ref, f"enum:{name}") from error

    def optional_enum(self, name: str, enum_type: type[EnumT]) -> EnumT | None:
        value = self.optional_text(name)
        if value is None:
            return None
        try:
            return enum_type(value)
        except ValueError as error:
            raise _fail(self._ref, f"enum:{name}") from error

    def sequence(self, name: str) -> tuple[StoredValue, ...]:
        value = self._raw(name)
        if not isinstance(value, tuple):
            raise _fail(self._ref, f"type:{name}")
        return value

    def texts(self, name: str) -> tuple[str, ...]:
        values: list[str] = []
        for item in self.sequence(name):
            if not isinstance(item, str):
                raise _fail(self._ref, f"type:{name}")
            values.append(item)
        return tuple(values)

    def identifiers(self, name: str, identifier_type: type[IdentifierT]) -> tuple[IdentifierT, ...]:
        return tuple(identifier_type(self._parse_uuid(name, value)) for value in self.texts(name))

    def uuids(self, name: str) -> tuple[UUID, ...]:
        return tuple(self._parse_uuid(name, value) for value in self.texts(name))

    def mapping(self, name: str) -> StoredItem:
        value = self._raw(name)
        if isinstance(value, tuple) or not isinstance(value, Mapping):
            raise _fail(self._ref, f"type:{name}")
        return value

    def mappings(self, name: str) -> tuple[StoredItem, ...]:
        items = self.sequence(name)
        result: list[StoredItem] = []
        for item in items:
            if isinstance(item, tuple) or not isinstance(item, Mapping):
                raise _fail(self._ref, f"type:{name}")
            result.append(item)
        return tuple(result)

    def child(self, item: StoredItem, suffix: str) -> ItemReader:
        return ItemReader(item, entity_ref=f"{self._ref}.{suffix}")

    def contains(self, name: str) -> bool:
        """Whether the item carries an attribute at all.

        Used only where a *policy* decides whether an attribute is written, never to make a
        required attribute optional: the caller must still read and validate it when present.
        """

        return name in self._item

    def finish(self) -> None:
        """Reject any attribute the decoder did not explicitly consume."""

        unread = set(self._item) - self._consumed
        if unread:
            raise _fail(self._ref, "unexpected_attributes")

    @property
    def entity_ref(self) -> str:
        return self._ref


def build_entity[EntityT](
    entity_ref: str, factory: Callable[..., EntityT], /, **values: object
) -> EntityT:
    """Construct a frozen domain object, mapping constructor rejection to IntegrityError."""

    try:
        return factory(**values)
    except (ValueError, TypeError) as error:
        raise _fail(entity_ref, "invariant") from error


def require_addressed_item(key: ItemKey, item: StoredItem) -> None:
    """Assert a written item carries the key attributes it is being written to.

    DynamoDB derives an item's address from its own ``PK``/``SK``, so a staged key that
    disagreed with the body would silently land somewhere else -- and the write's condition
    would be evaluated at that other address. Both adapters enforce this, so the mistake
    fails identically instead of only in the emulator.
    """

    if item.get(ATTR_PARTITION_KEY) != key.partition_key or item.get(ATTR_SORT_KEY) != key.sort_key:
        raise ValueError("a stored item must carry the key attributes it is written to")


def envelope(
    *,
    entity_type: EntityType,
    schema_version: str,
    key: ItemKey,
    namespace: Namespace,
    community_id: CommunityId | None,
    case_id: CaseId | None,
) -> dict[str, StoredValue]:
    """Build the mandatory attributes present on every persisted item."""

    return {
        ATTR_PARTITION_KEY: key.partition_key,
        ATTR_SORT_KEY: key.sort_key,
        ATTR_ENTITY_TYPE: entity_type.value,
        ATTR_SCHEMA_VERSION: schema_version,
        ATTR_NAMESPACE: namespace.value,
        ATTR_COMMUNITY_ID: None if community_id is None else str(community_id),
        ATTR_CASE_ID: None if case_id is None else str(case_id),
    }


def read_envelope(
    reader: ItemReader,
    *,
    expected_type: EntityType,
    accepted_schema_versions: frozenset[str],
) -> tuple[DecodedScope, str]:
    """Consume and validate the envelope, returning the scope and schema version."""

    partition_key = reader.text(ATTR_PARTITION_KEY)
    sort_key = reader.text(ATTR_SORT_KEY)
    entity_type = reader.enum(ATTR_ENTITY_TYPE, EntityType)
    if entity_type is not expected_type:
        raise _fail(reader.entity_ref, "discriminator")
    schema_version = reader.text(ATTR_SCHEMA_VERSION)
    if schema_version not in accepted_schema_versions:
        raise _fail(reader.entity_ref, "schema_version")
    namespace = reader.namespace(ATTR_NAMESPACE)
    community_id = reader.optional_identifier(ATTR_COMMUNITY_ID, CommunityId)
    case_id = reader.optional_identifier(ATTR_CASE_ID, CaseId)
    scope = DecodedScope(
        partition_key=partition_key,
        sort_key=sort_key,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
    )
    return scope, schema_version


def instant(value: datetime) -> str:
    """Serialize an instant using the frozen canonical representation."""

    return format_utc(value)


def optional_instant(value: datetime | None) -> StoredValue:
    return None if value is None else format_utc(value)


def identifier(value: UUIDIdentifier | UUID) -> str:
    return str(value)


def optional_identifier(value: UUIDIdentifier | UUID | None) -> StoredValue:
    return None if value is None else str(value)


def identifiers(values: tuple[UUIDIdentifier, ...] | tuple[UUID, ...]) -> tuple[StoredValue, ...]:
    return tuple(str(value) for value in values)


def sensitive(value: SensitiveStr) -> str:
    return value.reveal()


def optional_sensitive(value: SensitiveStr | None) -> StoredValue:
    return None if value is None else value.reveal()
