"""The deployed demo reset's Core ``NS#DEMO`` control plane: manifest, lock, receipt, purge.

The adapter half of [chorus.ports.demo_reset](../../ports/demo_reset.py). Every row here lives
in the **Core** table at the literal partition ``NS#DEMO``, alongside the community row
([06-persistence-and-evidence.md](../../../../docs/architecture/06-persistence-and-evidence.md)
§ Core table mapping). The manifest, the lock, and the receipts are the reset's own control
plane -- the bounded purge is built to preserve them (their sort-key prefixes are in
``keys.DEMO_RESET_CONTROL_SORT_PREFIXES``) while it deletes the seed and case-world rows around
them.

There is no scan anywhere in this module. The partition purge :class:`DynamoDbDemoPartitionPurge`
queries **one manifest-listed partition at a time** and deletes its items in bounded batches;
the demo's namespace grammar is what makes that a complete enumeration without a scan.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from chorus.domain.errors import IntegrityError
from chorus.domain.ids import Namespace
from chorus.domain.time import epoch_micros, require_utc
from chorus.infrastructure.dynamodb import keys
from chorus.infrastructure.dynamodb.codec import (
    ATTR_SORT_KEY,
    EntityType,
    ItemReader,
    build_entity,
    envelope,
    instant,
    read_envelope,
)
from chorus.ports.demo_reset import (
    DEMO_MANIFEST_SCHEMA_VERSION,
    DEMO_RESET_LOCK_SCHEMA_VERSION,
    DEMO_RESET_RECEIPT_SCHEMA_VERSION,
    DemoManifest,
    DemoManifestUnavailableError,
    DemoResetLockContendedError,
    DemoResetLockHandle,
    DemoResetLockNotOwnedError,
    DemoResetReceipt,
    require_demo_partition,
)
from chorus.ports.errors import PersistenceConflictError, PersistenceError
from chorus.ports.storage import (
    AnyOf,
    AttributeEqualsNumber,
    AttributeEqualsString,
    AttributeLessThanNumber,
    DeleteItem,
    ItemKey,
    KeyAbsent,
    KeyPresent,
    PutItem,
    QueryRequest,
    SortKeyAll,
    SortKeyBeginsWith,
    StorageDriver,
    StoredItem,
    StoredValue,
    TableName,
)

_MANIFEST_SCHEMA_VERSIONS = frozenset({DEMO_MANIFEST_SCHEMA_VERSION})
_LOCK_SCHEMA_VERSIONS = frozenset({DEMO_RESET_LOCK_SCHEMA_VERSION})
_RECEIPT_SCHEMA_VERSIONS = frozenset({DEMO_RESET_RECEIPT_SCHEMA_VERSION})

_MANIFEST_REF = "DEMO_MANIFEST"
_LOCK_REF = "DEMO_RESET_LOCK"
_RECEIPT_REF = "DEMO_RESET_RECEIPT"

ATTR_MANIFEST_VERSION = "manifest_version"
ATTR_PARTITION_KEYS = "partition_keys"
ATTR_CONTROL_SORT_PREFIXES = "control_sort_prefixes"
ATTR_PRIVATE_PREFIXES = "private_object_prefixes"
ATTR_EXPORT_PREFIXES = "export_object_prefixes"
ATTR_SCHEDULE_NAME_PREFIX = "schedule_name_prefix"
ATTR_SEED_VERSION = "seed_version"
ATTR_CREATED_AT = "created_at"

ATTR_OWNER_TOKEN = "owner_token"  # noqa: S105 -- an attribute name, not a value
ATTR_ACQUIRED_AT = "acquired_at"
ATTR_EXPIRES_AT = "expires_at"
ATTR_EXPIRES_AT_MICROS = "expires_at_micros"

ATTR_IDEMPOTENCY_KEY = "idempotency_key"
ATTR_REQUEST_FINGERPRINT = "request_fingerprint"
ATTR_RECORDED_AT = "recorded_at"
ATTR_RESULT_JSON = "result_json"

MAX_PARTITION_DELETE_PAGES = 200
"""A partition with more pages than this is a corruption signal, not a normal demo partition --
the demo never fills a partition, so an unbounded loop would be a scan by another name."""


def _control_key(namespace: Namespace, sort_key: str) -> ItemKey:
    return ItemKey(
        table=TableName.CORE,
        partition_key=keys.demo_control_partition(namespace),
        sort_key=sort_key,
    )


# ---------------------------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------------------------


def encode_demo_manifest(namespace: Namespace, manifest: DemoManifest) -> StoredItem:
    key = _control_key(namespace, keys.demo_manifest_sort_key(manifest.seed_version))
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.DEMO_MANIFEST,
        schema_version=DEMO_MANIFEST_SCHEMA_VERSION,
        key=key,
        namespace=namespace,
        community_id=None,
        case_id=None,
    )
    item.update(
        {
            ATTR_SEED_VERSION: manifest.seed_version,
            ATTR_CREATED_AT: instant(manifest.created_at),
            ATTR_MANIFEST_VERSION: manifest.version,
            ATTR_PARTITION_KEYS: tuple(manifest.partition_keys),
            ATTR_CONTROL_SORT_PREFIXES: tuple(manifest.control_sort_prefixes),
            ATTR_PRIVATE_PREFIXES: tuple(manifest.private_object_prefixes),
            ATTR_EXPORT_PREFIXES: tuple(manifest.export_object_prefixes),
            ATTR_SCHEDULE_NAME_PREFIX: manifest.schedule_name_prefix,
        }
    )
    return item


def decode_demo_manifest(namespace: Namespace, item: StoredItem) -> DemoManifest:
    reader = ItemReader(item, entity_ref=_MANIFEST_REF)
    scope, _ = read_envelope(
        reader,
        expected_type=EntityType.DEMO_MANIFEST,
        accepted_schema_versions=_MANIFEST_SCHEMA_VERSIONS,
    )
    if scope.namespace != namespace or scope.partition_key != keys.demo_control_partition(
        namespace
    ):
        raise IntegrityError(f"{_MANIFEST_REF}:scope")
    manifest = build_entity(
        _MANIFEST_REF,
        DemoManifest,
        seed_version=reader.text(ATTR_SEED_VERSION),
        created_at=reader.instant(ATTR_CREATED_AT),
        version=reader.number(ATTR_MANIFEST_VERSION),
        partition_keys=reader.texts(ATTR_PARTITION_KEYS),
        control_sort_prefixes=reader.texts(ATTR_CONTROL_SORT_PREFIXES),
        private_object_prefixes=reader.texts(ATTR_PRIVATE_PREFIXES),
        export_object_prefixes=reader.texts(ATTR_EXPORT_PREFIXES),
        schedule_name_prefix=reader.text(ATTR_SCHEDULE_NAME_PREFIX),
    )
    reader.finish()
    return manifest


@dataclass(frozen=True, slots=True)
class DynamoDbDemoManifestStore:
    """The one ``DemoManifest`` row, read strongly and written under optimistic concurrency."""

    driver: StorageDriver
    namespace: Namespace
    seed_version: str

    def _key(self) -> ItemKey:
        return _control_key(self.namespace, keys.demo_manifest_sort_key(self.seed_version))

    async def load(self) -> DemoManifest | None:
        try:
            item = await self.driver.get_item(self._key(), consistent=True)
        except PersistenceError as error:
            raise DemoManifestUnavailableError("the demo manifest could not be read") from error
        if item is None:
            return None
        try:
            return decode_demo_manifest(self.namespace, item)
        except (IntegrityError, ValueError) as error:
            raise DemoManifestUnavailableError("the demo manifest row is not a manifest") from error

    async def put(self, manifest: DemoManifest, *, expected_version: int | None) -> DemoManifest:
        if manifest.seed_version != self.seed_version:
            raise DemoManifestUnavailableError("manifest seed version does not match the store")
        condition: object = (
            KeyAbsent()
            if expected_version is None
            else AttributeEqualsNumber(name=ATTR_MANIFEST_VERSION, value=expected_version)
        )
        operation = PutItem(
            key=self._key(),
            item=encode_demo_manifest(self.namespace, manifest),
            condition=condition,  # type: ignore[arg-type]
        )
        try:
            await self.driver.write_item(operation)
        except PersistenceConflictError as error:
            raise DemoManifestUnavailableError(
                "the demo manifest moved under this write"
            ) from error
        except PersistenceError as error:
            raise DemoManifestUnavailableError("the demo manifest could not be written") from error
        return manifest


ATTR_REGISTERED_PARTITION_KEY = "registered_partition_key"
_REGISTERED_REF = "DEMO_REGISTERED_PARTITION"
_REGISTERED_SCHEMA_VERSION = "demo-registered-partition/v1"
_REGISTERED_SCHEMA_VERSIONS = frozenset({_REGISTERED_SCHEMA_VERSION})


def encode_demo_registered_partition(namespace: Namespace, partition_key: str) -> StoredItem:
    require_demo_partition(partition_key)
    key = _control_key(namespace, keys.demo_registered_partition_sort_key(partition_key))
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.DEMO_REGISTERED_PARTITION,
        schema_version=_REGISTERED_SCHEMA_VERSION,
        key=key,
        namespace=namespace,
        community_id=None,
        case_id=None,
    )
    item[ATTR_REGISTERED_PARTITION_KEY] = partition_key
    return item


def decode_demo_registered_partition(namespace: Namespace, item: StoredItem) -> str:
    reader = ItemReader(item, entity_ref=_REGISTERED_REF)
    read_envelope(
        reader,
        expected_type=EntityType.DEMO_REGISTERED_PARTITION,
        accepted_schema_versions=_REGISTERED_SCHEMA_VERSIONS,
    )
    partition_key = reader.text(ATTR_REGISTERED_PARTITION_KEY)
    reader.finish()
    return require_demo_partition(partition_key)


@dataclass(frozen=True, slots=True)
class DynamoDbDemoManifestRegistrar:
    """Record a dynamically created reset-owned partition, atomically or standalone.

    :meth:`registration_operation` returns the create-only ``PutItem`` an application command
    appends to *its own* transaction, so the marker and the partition's first row commit
    together or not at all -- a crash cannot orphan a durable resource outside the reset
    inventory. :meth:`register_partition` is the standalone form. :meth:`registered_partition_keys`
    reads the whole set back for the reset's enumeration.
    """

    driver: StorageDriver
    namespace: Namespace

    def registration_operation(self, partition_key: str) -> PutItem:
        return self.partition_registration(self.namespace, partition_key)

    @staticmethod
    def partition_registration(namespace: Namespace, partition_key: str) -> PutItem:
        return PutItem(
            key=_control_key(namespace, keys.demo_registered_partition_sort_key(partition_key)),
            item=encode_demo_registered_partition(namespace, partition_key),
            condition=KeyAbsent(),
        )

    async def register_partition(self, partition_key: str) -> None:
        try:
            await self.driver.write_item(self.registration_operation(partition_key))
        except PersistenceConflictError:
            return  # already registered -- the outcome this wanted
        except PersistenceError as error:
            raise DemoManifestUnavailableError(
                "a demo partition registration could not be written"
            ) from error

    async def registered_partition_keys(self) -> tuple[str, ...]:
        found: list[str] = []
        start: str | None = None
        for _page in range(MAX_PARTITION_DELETE_PAGES):
            try:
                result = await self.driver.query(
                    QueryRequest(
                        table=TableName.CORE,
                        partition_key=keys.demo_control_partition(self.namespace),
                        sort_key=SortKeyBeginsWith(keys.DEMO_REGISTERED_PARTITION_SORT_KEY_PREFIX),
                        consistent=True,
                        limit=100,
                        exclusive_start_sort_key=start,
                    )
                )
            except PersistenceError as error:
                raise DemoManifestUnavailableError(
                    "the demo partition markers could not be read"
                ) from error
            for item in result.items:
                found.append(decode_demo_registered_partition(self.namespace, item))
            start = result.last_evaluated_sort_key
            if start is None:
                break
        return tuple(dict.fromkeys(found))


# ---------------------------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------------------------


def encode_demo_reset_lock(namespace: Namespace, handle: DemoResetLockHandle) -> StoredItem:
    key = _control_key(namespace, keys.DEMO_RESET_LOCK_SORT_KEY)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.DEMO_RESET_LOCK,
        schema_version=DEMO_RESET_LOCK_SCHEMA_VERSION,
        key=key,
        namespace=namespace,
        community_id=None,
        case_id=None,
    )
    item.update(
        {
            ATTR_OWNER_TOKEN: handle.owner_token,
            ATTR_ACQUIRED_AT: instant(handle.acquired_at),
            ATTR_EXPIRES_AT: instant(handle.expires_at),
            ATTR_EXPIRES_AT_MICROS: epoch_micros(handle.expires_at),
        }
    )
    return item


def decode_demo_reset_lock(namespace: Namespace, item: StoredItem) -> DemoResetLockHandle:
    reader = ItemReader(item, entity_ref=_LOCK_REF)
    read_envelope(
        reader,
        expected_type=EntityType.DEMO_RESET_LOCK,
        accepted_schema_versions=_LOCK_SCHEMA_VERSIONS,
    )
    owner = reader.text(ATTR_OWNER_TOKEN)
    acquired = reader.instant(ATTR_ACQUIRED_AT)
    expires = reader.instant(ATTR_EXPIRES_AT)
    reader.number(ATTR_EXPIRES_AT_MICROS)
    reader.finish()
    return build_entity(
        _LOCK_REF,
        DemoResetLockHandle,
        owner_token=owner,
        acquired_at=acquired,
        expires_at=expires,
    )


@dataclass(frozen=True, slots=True)
class DynamoDbDemoResetLock:
    """``DEMO_RESET_LOCK`` -- a conditional item at ``NS#DEMO`` / ``DEMO_RESET_LOCK``."""

    driver: StorageDriver
    namespace: Namespace

    def _key(self) -> ItemKey:
        return _control_key(self.namespace, keys.DEMO_RESET_LOCK_SORT_KEY)

    async def acquire(
        self, *, owner_token: str, now: datetime, ttl_seconds: int
    ) -> DemoResetLockHandle:
        require_utc(now)
        if not owner_token:
            raise IntegrityError(f"{_LOCK_REF}:owner_token")
        handle = DemoResetLockHandle(
            owner_token=owner_token,
            acquired_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        # Take the lock iff no row exists OR the existing lock has expired (stale recovery).
        condition = AnyOf(
            (
                KeyAbsent(),
                AttributeLessThanNumber(name=ATTR_EXPIRES_AT_MICROS, value=epoch_micros(now)),
            )
        )
        try:
            await self.driver.write_item(
                PutItem(
                    key=self._key(),
                    item=encode_demo_reset_lock(self.namespace, handle),
                    condition=condition,
                )
            )
        except PersistenceConflictError as error:
            raise DemoResetLockContendedError("another demo reset holds DEMO_RESET_LOCK") from error
        except PersistenceError as error:
            raise DemoResetLockContendedError("DEMO_RESET_LOCK could not be acquired") from error
        return handle

    async def read(self) -> DemoResetLockHandle | None:
        try:
            item = await self.driver.get_item(self._key(), consistent=True)
        except PersistenceError as error:
            raise DemoResetLockContendedError("DEMO_RESET_LOCK could not be read") from error
        if item is None:
            return None
        try:
            return decode_demo_reset_lock(self.namespace, item)
        except (IntegrityError, ValueError) as error:
            raise DemoResetLockContendedError("DEMO_RESET_LOCK row is corrupt") from error

    async def release(self, handle: DemoResetLockHandle) -> None:
        try:
            await self.driver.write_item(
                DeleteItem(
                    key=self._key(),
                    condition=AttributeEqualsString(
                        name=ATTR_OWNER_TOKEN, value=handle.owner_token
                    ),
                )
            )
        except PersistenceConflictError as error:
            raise DemoResetLockNotOwnedError(
                "DEMO_RESET_LOCK is not owned by this reset attempt"
            ) from error
        except PersistenceError as error:
            raise DemoResetLockNotOwnedError("DEMO_RESET_LOCK could not be released") from error


# ---------------------------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------------------------


def encode_demo_reset_receipt(namespace: Namespace, receipt: DemoResetReceipt) -> StoredItem:
    key = _control_key(namespace, keys.demo_reset_receipt_sort_key(receipt.idempotency_key))
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.DEMO_RESET_RECEIPT,
        schema_version=DEMO_RESET_RECEIPT_SCHEMA_VERSION,
        key=key,
        namespace=namespace,
        community_id=None,
        case_id=None,
    )
    item.update(
        {
            ATTR_IDEMPOTENCY_KEY: receipt.idempotency_key,
            ATTR_REQUEST_FINGERPRINT: receipt.request_fingerprint,
            ATTR_RECORDED_AT: instant(receipt.recorded_at),
            ATTR_RESULT_JSON: receipt.result_json,
        }
    )
    return item


def decode_demo_reset_receipt(namespace: Namespace, item: StoredItem) -> DemoResetReceipt:
    reader = ItemReader(item, entity_ref=_RECEIPT_REF)
    read_envelope(
        reader,
        expected_type=EntityType.DEMO_RESET_RECEIPT,
        accepted_schema_versions=_RECEIPT_SCHEMA_VERSIONS,
    )
    receipt = build_entity(
        _RECEIPT_REF,
        DemoResetReceipt,
        idempotency_key=reader.text(ATTR_IDEMPOTENCY_KEY),
        request_fingerprint=reader.text(ATTR_REQUEST_FINGERPRINT),
        recorded_at=reader.instant(ATTR_RECORDED_AT),
        result_json=reader.text(ATTR_RESULT_JSON),
    )
    reader.finish()
    return receipt


@dataclass(frozen=True, slots=True)
class DynamoDbDemoResetReceiptStore:
    """Durable idempotent-replay records at ``NS#DEMO`` / ``DEMO_RESET_RECEIPT#{digest}``."""

    driver: StorageDriver
    namespace: Namespace

    async def load(self, idempotency_key: str) -> DemoResetReceipt | None:
        key = _control_key(self.namespace, keys.demo_reset_receipt_sort_key(idempotency_key))
        try:
            item = await self.driver.get_item(key, consistent=True)
        except PersistenceError as error:
            raise DemoManifestUnavailableError("a demo reset receipt could not be read") from error
        if item is None:
            return None
        try:
            return decode_demo_reset_receipt(self.namespace, item)
        except (IntegrityError, ValueError) as error:
            raise DemoManifestUnavailableError("a demo reset receipt row is corrupt") from error

    async def put(self, receipt: DemoResetReceipt) -> None:
        try:
            await self.driver.write_item(
                PutItem(
                    key=_control_key(
                        self.namespace,
                        keys.demo_reset_receipt_sort_key(receipt.idempotency_key),
                    ),
                    item=encode_demo_reset_receipt(self.namespace, receipt),
                    condition=KeyAbsent(),
                )
            )
        except PersistenceConflictError:
            # Idempotent: the receipt is already recorded, which is the outcome this wanted.
            return
        except PersistenceError as error:
            raise DemoManifestUnavailableError(
                "a demo reset receipt could not be written"
            ) from error


# ---------------------------------------------------------------------------------------------
# Bounded partition purge
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DynamoDbDemoPartitionPurge:
    """Query one manifest-listed partition and delete its items in bounded batches.

    No scan: one ``QueryRequest`` per partition, paged by ``last_evaluated_sort_key``, with a
    hard page cap. Every partition key is re-checked to be in the DEMO namespace before a
    single delete is issued.
    """

    driver: StorageDriver
    page_size: int = 100

    async def partition_item_sort_keys(self, *, table: str, partition_key: str) -> tuple[str, ...]:
        require_demo_partition(partition_key)
        table_name = TableName(table)
        found: list[str] = []
        start: str | None = None
        for _page in range(MAX_PARTITION_DELETE_PAGES):
            result = await self.driver.query(
                QueryRequest(
                    table=table_name,
                    partition_key=partition_key,
                    sort_key=SortKeyAll(),
                    consistent=True,
                    limit=self.page_size,
                    exclusive_start_sort_key=start,
                )
            )
            found.extend(str(item[ATTR_SORT_KEY]) for item in result.items)
            start = result.last_evaluated_sort_key
            if start is None:
                break
        else:
            raise IntegrityError(f"DEMO_RESET_PARTITION_TOO_LARGE:{partition_key}")
        return tuple(found)

    async def delete_partition(
        self,
        *,
        table: str,
        partition_key: str,
        keep_sort_prefixes: Sequence[str] = (),
    ) -> int:
        require_demo_partition(partition_key)
        table_name = TableName(table)
        deleted = 0
        start: str | None = None
        for _page in range(MAX_PARTITION_DELETE_PAGES):
            result = await self.driver.query(
                QueryRequest(
                    table=table_name,
                    partition_key=partition_key,
                    sort_key=SortKeyAll(),
                    consistent=True,
                    limit=self.page_size,
                    exclusive_start_sort_key=start,
                )
            )
            for item in result.items:
                sort_key = str(item[ATTR_SORT_KEY])
                if any(sort_key.startswith(prefix) for prefix in keep_sort_prefixes):
                    continue
                try:
                    await self.driver.write_item(
                        DeleteItem(
                            key=ItemKey(
                                table=table_name,
                                partition_key=partition_key,
                                sort_key=sort_key,
                            ),
                            # Delete iff it is still there. A concurrent delete is the same
                            # outcome, not an error.
                            condition=KeyPresent(),
                        )
                    )
                    deleted += 1
                except PersistenceConflictError:
                    continue
            start = result.last_evaluated_sort_key
            if start is None:
                break
        else:
            raise IntegrityError(f"DEMO_RESET_PARTITION_TOO_LARGE:{partition_key}")
        return deleted


__all__ = [
    "DynamoDbDemoManifestRegistrar",
    "DynamoDbDemoManifestStore",
    "DynamoDbDemoPartitionPurge",
    "DynamoDbDemoResetLock",
    "DynamoDbDemoResetReceiptStore",
    "decode_demo_manifest",
    "decode_demo_registered_partition",
    "decode_demo_reset_lock",
    "decode_demo_reset_receipt",
    "encode_demo_manifest",
    "encode_demo_registered_partition",
    "encode_demo_reset_lock",
    "encode_demo_reset_receipt",
]
