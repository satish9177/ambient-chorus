"""The deployed demo reset's persistence boundary: the manifest, the lock, and the receipt.

The frozen manifest-driven reset
([11-frontend-and-demo.md](../../../docs/architecture/11-frontend-and-demo.md) § One-command
reset; [08-api-design.md](../../../docs/architecture/08-api-design.md) § Reset;
[06-persistence-and-evidence.md](../../../docs/architecture/06-persistence-and-evidence.md)
§ Core table mapping). Reset is an **operator action for deterministic demo restoration**, and
the deployed path must do exactly what the local one does -- the difference is which adapters it
holds, not what "reset" means.

Three durable concepts live here, all in the Core ``NS#DEMO`` partition:

* the **``DemoManifest``** -- the mutable runtime inventory of every reset-owned partition,
  object prefix, and schedule-name grammar. It is **not** the checked-in fixture input manifest
  (:class:`~chorus.infrastructure.fixtures.synthetic_feed.SyntheticAmbientAdapter`); that one
  declares the seed corpus, this one tracks what the running demo has created so the bounded
  purge can enumerate it **without a scan**. Missing or corrupt fails the reset closed;
* the **``DEMO_RESET_LOCK``** -- a conditional item that serialises resets and bounds the
  inventory window, so concurrent demo mutation cannot escape a reset's verified target set.
  It carries an owner token; a release only removes the lock this attempt owns;
* the **``DemoResetReceipt``** -- the durable idempotent-replay record. An identical request
  under the same key replays the recorded receipt and performs **no** second destructive
  reset -- no second generation bump, no second clock rewind.

The bounded purge, the object-prefix deletion, and the schedule cleanup are expressed as narrow
ports too, so an in-memory adapter can drive the same orchestration a real reset does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from chorus.domain.errors import IntegrityError
from chorus.domain.time import require_utc
from chorus.ports.storage import PutItem

DEMO_MANIFEST_SCHEMA_VERSION = "demo-manifest/v1"
DEMO_RESET_RECEIPT_SCHEMA_VERSION = "demo-reset-receipt/v1"
DEMO_RESET_LOCK_SCHEMA_VERSION = "demo-reset-lock/v1"


class DemoResetInfrastructureError(Exception):
    """A reset step could not be carried out. Its own family, and never a fallback.

    A reset that cannot prove it completed a stage must fail loudly rather than report
    ``COMPLETED`` over a partial result -- that is the defect this family exists to make
    unreachable.
    """


class DemoManifestUnavailableError(DemoResetInfrastructureError):
    """The ``DemoManifest`` row is missing, malformed, or the store could not be reached.

    One type for all three because the caller's response is identical and must be: refuse. A
    reset never falls back to a table scan to reconstruct the inventory
    ([11-frontend-and-demo.md](../../../docs/architecture/11-frontend-and-demo.md) § 4).
    """


class DemoResetLockContendedError(DemoResetInfrastructureError):
    """Another reset holds ``DEMO_RESET_LOCK``; this attempt acquired nothing.

    Deterministic and definite: the conditional acquire is evaluated by the store, so a losing
    attempt knows it lost and knows nothing was locked or mutated.
    """


class DemoResetLockNotOwnedError(DemoResetInfrastructureError):
    """A release named a lock this attempt does not own.

    Refused rather than forced: a non-owner cannot release, and a stale lock is recovered only
    through the recorded ``expires_at`` / owner check, never by a blind delete.
    """


class DemoResetPartialError(DemoResetInfrastructureError):
    """A destructive or seeding stage completed only in part.

    The reset is left recoverable and **never** reports success. A retried reset re-runs the
    bounded steps against the same manifest inventory and never broadens deletion.
    """


class DemoResetVerificationError(DemoResetInfrastructureError):
    """A post-purge or post-seed verification found a manifest-listed resource in the wrong
    state -- a stale target that survived cleanup, or a seed row that is not the intended one.
    """


# ---------------------------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoManifest:
    """The mutable runtime inventory of every reset-owned resource.

    ``partition_keys`` holds every Core / Shareable / Audit partition key the demo owns -- the
    static seed roots (``NS#DEMO``, ``NS#DEMO#COMM#{community}``) plus every dynamically created
    one registered as the demo runs (``NS#DEMO#CASE#…``, ``NS#DEMO#FENCE#…``,
    ``NS#DEMO#VIEW#…``, ``NS#DEMO#ACTION#…``, ``NS#DEMO#EXECUTION#…``, ``NS#DEMO#OPERATION#…``).
    ``control_sort_prefixes`` are the sort-key prefixes **within** ``NS#DEMO`` the purge must
    **not** delete -- the manifest itself, the reset lock, the receipts, and the demo clock live
    there and are managed by the reset, not erased by it.

    ``version`` is optimistic-concurrency for the manifest row; a registration or a rewrite
    conditions on it.
    """

    seed_version: str
    created_at: datetime
    version: int
    partition_keys: tuple[str, ...]
    control_sort_prefixes: tuple[str, ...]
    private_object_prefixes: tuple[str, ...]
    export_object_prefixes: tuple[str, ...]
    schedule_name_prefix: str

    def __post_init__(self) -> None:
        require_utc(self.created_at)
        if self.version < 1:
            raise ValueError("a demo manifest carries a positive version")
        if not self.seed_version:
            raise ValueError("a demo manifest names a seed version")
        for key in self.partition_keys:
            if not (key == "NS#DEMO" or key.startswith("NS#DEMO#")):
                raise ValueError(f"manifest partition key {key!r} is not in the DEMO namespace")
        for prefix in (*self.private_object_prefixes, *self.export_object_prefixes):
            if not prefix.startswith("ns/DEMO/"):
                raise ValueError(f"manifest object prefix {prefix!r} is not under ns/DEMO/")
        if not self.schedule_name_prefix:
            raise ValueError("a demo manifest names a schedule-name grammar prefix")

    def with_partitions(self, keys: Sequence[str]) -> DemoManifest:
        """Return a copy whose ``partition_keys`` also contains ``keys`` (deduped, ordered)."""

        merged = list(self.partition_keys)
        for key in keys:
            if key not in merged:
                merged.append(key)
        return DemoManifest(
            seed_version=self.seed_version,
            created_at=self.created_at,
            version=self.version + 1,
            partition_keys=tuple(merged),
            control_sort_prefixes=self.control_sort_prefixes,
            private_object_prefixes=self.private_object_prefixes,
            export_object_prefixes=self.export_object_prefixes,
            schedule_name_prefix=self.schedule_name_prefix,
        )

    def is_reset_owned_partition(self, partition_key: str) -> bool:
        """Whether ``partition_key`` is one the manifest authorises this reset to purge."""

        return partition_key in self.partition_keys

    def is_control_sort_key(self, sort_key: str) -> bool:
        """Whether ``sort_key`` (in ``NS#DEMO``) is a reset control row the purge must keep."""

        return any(sort_key.startswith(prefix) for prefix in self.control_sort_prefixes)


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoResetLockHandle:
    """Proof that this reset attempt holds ``DEMO_RESET_LOCK``."""

    owner_token: str
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.acquired_at)
        require_utc(self.expires_at)
        if not self.owner_token:
            raise ValueError("a reset lock handle carries an owner token")
        if self.expires_at <= self.acquired_at:
            raise ValueError("a reset lock expiry is after its acquisition")


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoResetReceipt:
    """A durable idempotent-replay record: the request fingerprint and the recorded result.

    ``result_json`` is the frozen ``DemoResetResult`` payload, serialised. The reset service
    owns that shape; this port only stores and returns it verbatim so a replay is
    byte-identical to the original answer.
    """

    idempotency_key: str
    request_fingerprint: str
    recorded_at: datetime
    result_json: str

    def __post_init__(self) -> None:
        require_utc(self.recorded_at)
        if not self.idempotency_key or not self.request_fingerprint:
            raise ValueError("a reset receipt carries a key and a request fingerprint")


# ---------------------------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------------------------


class DemoManifestStorePort(Protocol):
    """Read the one ``DemoManifest`` row, and write it back under optimistic concurrency."""

    async def load(self) -> DemoManifest | None:
        """Strongly read the manifest, or ``None`` when no row exists (a fresh deployment).

        A row that is present but unparseable raises :class:`DemoManifestUnavailableError` --
        reset never blind-overwrites an inventory it could not read.
        """

    async def put(self, manifest: DemoManifest, *, expected_version: int | None) -> DemoManifest:
        """Write ``manifest``, conditioned on the stored ``version`` being ``expected_version``
        (or on the row's absence when ``None``). A concurrent writer loses deterministically
        with :class:`DemoManifestUnavailableError`.
        """


class PartitionRegistrationPort(Protocol):
    """Emit the write that records a dynamically created reset-owned partition.

    The slim surface an application command needs to make registration **atomic** with the
    resource's own first durable write ([11-frontend-and-demo.md] § 3): the command appends
    :meth:`registration_operation` to its existing transaction, so a crash can never leave the
    resource durable and the marker absent. ``None`` outside the deployed demo, where nothing
    is appended and behaviour is unchanged.
    """

    def registration_operation(self, partition_key: str) -> PutItem:
        """A create-only ``PutItem`` recording ``partition_key`` in the reset inventory.

        Idempotent by construction (create-only), so re-running the enclosing transaction is
        safe. A partition key outside the DEMO namespace raises before any write is composed.
        """


class DemoManifestRegistrarPort(PartitionRegistrationPort, Protocol):
    """Register a newly created reset-owned partition, and read the registered set back.

    Called from the composition roots that create dynamic demo entities so a durable resource
    can never exist outside the reset inventory ([11-frontend-and-demo.md] § 3). Registration is
    made durable **transactionally** with the resource's own write via
    :meth:`registration_operation`; :meth:`register_partition` is the standalone form for a
    caller that is not already in a transaction.
    """

    async def register_partition(self, partition_key: str) -> None:
        """Add ``partition_key`` to the reset inventory if it is not already there.

        Idempotent. A partition key outside the DEMO namespace is refused.
        """

    async def registered_partition_keys(self) -> tuple[str, ...]:
        """Every dynamically registered reset-owned partition key, from bounded queries."""


class DemoResetLockPort(Protocol):
    """Acquire, read, and release ``DEMO_RESET_LOCK`` -- the reset-serialising conditional item."""

    async def acquire(
        self, *, owner_token: str, now: datetime, ttl_seconds: int
    ) -> DemoResetLockHandle:
        """Take the lock for ``owner_token``. Raises :class:`DemoResetLockContendedError` if a
        live lock is held by another owner; **may** take a lock whose ``expires_at`` has passed
        (stale recovery), recording the takeover.
        """

    async def read(self) -> DemoResetLockHandle | None:
        """The current lock holder, or ``None``."""

    async def release(self, handle: DemoResetLockHandle) -> None:
        """Remove the lock **iff** it still names ``handle.owner_token``. A release of a lock
        owned by someone else raises :class:`DemoResetLockNotOwnedError` and removes nothing.
        """


class DemoResetReceiptStorePort(Protocol):
    """Durable idempotent-replay records for completed resets."""

    async def load(self, idempotency_key: str) -> DemoResetReceipt | None:
        """The recorded receipt for ``idempotency_key``, or ``None``."""

    async def put(self, receipt: DemoResetReceipt) -> None:
        """Persist ``receipt``, create-only. A second write under the same key is a no-op reply
        of the first (idempotent), never an overwrite.
        """


class DemoPartitionPurgePort(Protocol):
    """Enumerate and bounded-delete the items of one manifest-listed partition.

    Query-by-partition then delete in bounded batches -- **never** a scan
    ([11-frontend-and-demo.md] § 4-5). ``keep_sort_prefixes`` protects the reset control rows
    inside ``NS#DEMO``.
    """

    async def partition_item_sort_keys(self, *, table: str, partition_key: str) -> tuple[str, ...]:
        """Every sort key in the partition, from bounded pages. Used to verify emptiness."""

    async def delete_partition(
        self,
        *,
        table: str,
        partition_key: str,
        keep_sort_prefixes: Sequence[str] = (),
    ) -> int:
        """Delete every item in the partition except those whose sort key starts with a
        ``keep_sort_prefixes`` entry. Returns the count deleted.
        """


class DemoObjectPrefixPurgePort(Protocol):
    """List and bounded-delete objects under an ``ns/DEMO/`` prefix, in one evidence bucket."""

    async def list_prefix(self, *, bucket: str, prefix: str) -> tuple[str, ...]:
        """Every object key under ``prefix``. Refuses a prefix that is not under ``ns/DEMO/``."""

    async def delete_objects(self, *, bucket: str, keys: Sequence[str]) -> int:
        """Delete the given keys, each re-checked to be under ``ns/DEMO/`` first. Returns the
        count deleted.
        """


class DemoSchedulePurgePort(Protocol):
    """List and delete the demo's one-time schedules in the ``chorus-{env}`` group."""

    async def list_demo_schedules(self, *, name_prefix: str) -> tuple[str, ...]:
        """Schedule names in the group whose name begins with ``name_prefix`` (the frozen
        ``chorus-{env}-{namespace_hash8}`` grammar). Not a wildcard delete of the group.
        """

    async def delete_schedules(self, *, names: Sequence[str]) -> int:
        """Delete the named schedules, each re-checked against ``name_prefix`` grammar. Returns
        the count deleted.
        """


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoResetPurgeCounts:
    """What one bounded purge removed -- safe operational metadata only."""

    partitions: int = 0
    items: int = 0
    private_objects: int = 0
    export_objects: int = 0
    schedules: int = 0

    def merged(self, other: DemoResetPurgeCounts) -> DemoResetPurgeCounts:
        return DemoResetPurgeCounts(
            partitions=self.partitions + other.partitions,
            items=self.items + other.items,
            private_objects=self.private_objects + other.private_objects,
            export_objects=self.export_objects + other.export_objects,
            schedules=self.schedules + other.schedules,
        )


def require_demo_partition(partition_key: str) -> str:
    """Refuse a partition key that is not in the DEMO namespace, before any mutation."""

    if partition_key == "NS#DEMO" or partition_key.startswith("NS#DEMO#"):
        return partition_key
    raise IntegrityError(f"DEMO_RESET_TARGET:{partition_key}")


def require_demo_object_prefix(prefix: str) -> str:
    """Refuse an object prefix that is not under ``ns/DEMO/``, before any deletion."""

    if prefix.startswith("ns/DEMO/"):
        return prefix
    raise IntegrityError(f"DEMO_RESET_OBJECT_PREFIX:{prefix}")


__all__ = [
    "DEMO_MANIFEST_SCHEMA_VERSION",
    "DEMO_RESET_LOCK_SCHEMA_VERSION",
    "DEMO_RESET_RECEIPT_SCHEMA_VERSION",
    "DemoManifest",
    "DemoManifestRegistrarPort",
    "DemoManifestStorePort",
    "DemoManifestUnavailableError",
    "DemoObjectPrefixPurgePort",
    "DemoPartitionPurgePort",
    "DemoResetInfrastructureError",
    "DemoResetLockContendedError",
    "DemoResetLockHandle",
    "DemoResetLockNotOwnedError",
    "DemoResetLockPort",
    "DemoResetPartialError",
    "DemoResetPurgeCounts",
    "DemoResetReceipt",
    "DemoResetReceiptStorePort",
    "DemoResetVerificationError",
    "DemoSchedulePurgePort",
    "PartitionRegistrationPort",
    "require_demo_object_prefix",
    "require_demo_partition",
]
