"""The deployed demo reset: the frozen manifest-driven sequence, against production adapters.

Review R2. The local :class:`~chorus.composition.demo_reset.DemoResetService` and this class are
**one implementation of one contract**
([08-api-design.md](../../../docs/architecture/08-api-design.md) § Ownership): the refusal
rules, the frozen ``DemoResetResult`` receipt, and the seed step are
literally the local service's -- ``DemoResetService.seed_only`` and ``.build_result`` are called
here verbatim. What this class adds is the deployed machinery the local path fakes with an
in-process dict and a namespace sweep:

* a durable **idempotent replay** -- an identical request under the same key returns the
  recorded :class:`~chorus.ports.demo_reset.DemoResetReceipt` and performs **no** second
  destructive reset (no second generation bump, no second clock rewind);
* the **``DEMO_RESET_LOCK``** -- acquired before any destructive step, released only if this
  attempt still owns it, so concurrent demo mutation cannot escape the verified inventory;
* the persisted **``DemoManifest``** -- the bounded purge enumerates *its* partitions, object
  prefixes, and schedule-name grammar, one query at a time, **never a scan**; missing or
  corrupt fails the reset closed;
* the ADR-029 § 3 **generation-fenced clock reseed** -- :class:`DynamoDbDemoClockResetStore`
  bumps ``reset_generation`` to a value never used before and restores the seed instant. The
  authoritative clock row is **never** deleted-and-recreated, so no stale pre-reset advance can
  win against it.

The sequence order is [11-frontend-and-demo.md](../../../docs/architecture/11-frontend-and-demo.md)
§ One-command reset and deployment contract § 12; a stage that only partly completes raises a
:class:`~chorus.ports.demo_reset.DemoResetInfrastructureError` rather than reporting
``COMPLETED``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from chorus.composition.demo_reset import (
    DEMO_CONFIRMATION,
    DEMO_NAMESPACE,
    RESET_IN_FLIGHT_STATES,
    DemoResetInFlightSend,
    DemoResetRefused,
    DemoResetResult,
    DemoResetService,
    ResetContributor,
    ResetCounts,
    ResetEvidence,
)
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    Namespace,
    Sha256Digest,
)
from chorus.domain.time import Clock
from chorus.infrastructure.dynamodb import codec_share
from chorus.infrastructure.dynamodb import keys as dbkeys
from chorus.infrastructure.dynamodb.codec import ATTR_ENTITY_TYPE, EntityType
from chorus.ports.demo_clock import DemoClockResetStorePort
from chorus.ports.demo_reset import (
    DemoManifest,
    DemoManifestRegistrarPort,
    DemoManifestStorePort,
    DemoManifestUnavailableError,
    DemoObjectPrefixPurgePort,
    DemoPartitionPurgePort,
    DemoResetLockPort,
    DemoResetPurgeCounts,
    DemoResetReceipt,
    DemoResetReceiptStorePort,
    DemoResetVerificationError,
    DemoSchedulePurgePort,
)
from chorus.ports.errors import IdempotencyConflictError
from chorus.ports.storage import (
    DeleteItem,
    ItemKey,
    KeyPresent,
    QueryRequest,
    SortKeyAll,
    StorageDriver,
    StoredItem,
    TableName,
)

_EXECUTION_PREFIX = "NS#DEMO#EXECUTION#"
_CLOCK_SORT_KEY = "DEMO_CLOCK"
_ALL_TABLES = ("CORE", "SHAREABLE", "AUDIT")
LOCK_TTL_SECONDS = 300


@dataclass(slots=True, kw_only=True)
class DeployedDemoReset:
    """Run one deployed demo reset to its frozen receipt, or fail loudly."""

    seeder: DemoResetService
    driver: StorageDriver
    manifest_store: DemoManifestStorePort
    registrar: DemoManifestRegistrarPort
    lock: DemoResetLockPort
    receipts: DemoResetReceiptStorePort
    partition_purge: DemoPartitionPurgePort
    private_object_purge: DemoObjectPrefixPurgePort
    export_object_purge: DemoObjectPrefixPurgePort
    schedule_purge: DemoSchedulePurgePort
    clock_reset_store: DemoClockResetStorePort
    wall_clock: Clock
    namespace: Namespace
    seed_version: str
    settings_environment: str
    seed_instant: datetime
    private_bucket: str
    export_bucket: str
    schedule_name_prefix: str

    async def reset(
        self,
        *,
        namespace: str,
        confirm: str,
        seed_version: str,
        idempotency_key: str | None,
    ) -> DemoResetResult:
        fingerprint = f"{namespace}\x1f{confirm}\x1f{seed_version}"

        # 1. durable idempotent replay -- FIRST, exactly as the local service does: an exact
        #    replay returns the recorded receipt and performs no second destructive reset; the
        #    same key under a materially different request is a conflict, never a silent reset.
        if idempotency_key is not None:
            recorded = await self.receipts.load(idempotency_key)
            if recorded is not None:
                if recorded.request_fingerprint != fingerprint:
                    raise IdempotencyConflictError("DEMO_RESET")
                return replace(_result_from_json(recorded.result_json), replayed=True)

        # 2. validate request / environment / seed / confirmation (before anything durable).
        if namespace != DEMO_NAMESPACE:
            raise DemoResetRefused("RESET_NAMESPACE")
        if confirm != DEMO_CONFIRMATION:
            raise DemoResetRefused("RESET_CONFIRMATION")
        if seed_version != self.seed_version:
            raise DemoResetRefused("RESET_SEED_VERSION")
        if self.settings_environment != "demo":
            raise DemoResetRefused("RESET_ENVIRONMENT")
        # 2a. the frozen fixture snapshot must still be exact (shared with the local service).
        self.seeder._validate_frozen_fixture_snapshot()

        # 3. acquire DEMO_RESET_LOCK -- serialises resets, bounds the inventory window.
        owner_token = uuid4().hex
        now = self.wall_clock.now()
        import asyncio

        from chorus.ports.demo_reset import DemoResetLockContendedError

        for attempt in range(2_300):
            try:
                handle = await self.lock.acquire(
                    owner_token=owner_token, now=self.wall_clock.now(), ttl_seconds=LOCK_TTL_SECONDS
                )
                break
            except DemoResetLockContendedError:
                if idempotency_key is None or attempt == 2_299:
                    raise
                # Let an identical invocation complete its durable receipt. Acquisition,
                # rather than a receipt read alone, orders the eventual replay decision.
                await asyncio.sleep(0.05)
        from chorus.infrastructure.dynamodb.demo_mutation import (
            reset_authority,
            reset_transaction_identity,
        )

        authority_token = reset_authority.set(owner_token)
        try:
            # A competing request may have completed after our optimistic receipt read.
            # Ownership serializes the second read with every destructive reset stage.
            if idempotency_key is not None:
                recorded = await self.receipts.load(idempotency_key)
                if recorded is not None:
                    if recorded.request_fingerprint != fingerprint:
                        raise IdempotencyConflictError("DEMO_RESET")
                    return replace(_result_from_json(recorded.result_json), replayed=True)

            # 4. load + validate the persisted manifest (no scan fallback).
            manifest = await self.manifest_store.load()
            if manifest is None or manifest.seed_version != seed_version:
                raise DemoManifestUnavailableError(
                    "no demo manifest for this seed version -- reset cannot enumerate its targets"
                )
            if manifest.schedule_name_prefix != self.schedule_name_prefix:
                raise DemoManifestUnavailableError("the manifest schedule namespace is not DEMO")

            # 5. resolve the complete target set: deterministic manifest roots + atomically
            #    registered OPERATION markers + pointer-chain-reconciled case-world partitions.
            targets = await self._all_target_partitions(manifest)

            # 6. refuse if any target EXECUTION partition holds a SENDING / SEND_UNKNOWN row.
            await self._guard_no_side_effects(targets)
            await self._guard_no_in_flight_send(targets)

            # 7. bounded purge of every target partition, object prefix, and schedule.
            counts = await self._purge(manifest, targets)

            # 8. verify cleanup -- no target partition still holds a purgeable item.
            await self._verify_purged(manifest, targets)

            # 9. clock reseed: a NEW, never-reused generation; the row is reseeded, never deleted.
            current = await self.clock_reset_store.read()
            reseeded = await self.clock_reset_store.reseed(
                seed_instant=self.seed_instant, current=current
            )

            # 10. seed the fixed community / contributors / corpus / evidence -- the local
            #     service's own implementation, verbatim.
            # The persisted generation identifies this reset, independent of process/retry.
            # Every retry of a seed transaction keeps its token; a fresh reset changes it.
            reset_id = uuid5(
                NAMESPACE_URL, f"chorus/demo-reset/{seed_version}/{reseeded.reset_generation}"
            )
            identity_token = reset_transaction_identity.set(str(reset_id))
            try:
                await self.seeder.seed_only()
            finally:
                reset_transaction_identity.reset(identity_token)

            # 11. write the fresh manifest, conditioned on the version this reset read.
            fresh = self._fresh_manifest(manifest)
            await self.manifest_store.put(fresh, expected_version=manifest.version)

            # 12. verify the seed landed -- the manifest is readable and names the seed roots.
            written = await self.manifest_store.load()
            if written is None or written.version != fresh.version:
                raise DemoResetVerificationError("the fresh demo manifest did not persist")

            # 13. build the frozen receipt (one implementation) and record it durably.
            registrations_removed = await self._clear_completed_registrations()
            result = self.seeder.build_result(
                namespace=namespace,
                seed_version=seed_version,
                deleted=counts.items + registrations_removed,
            )
            result = replace(result, reset_id=reset_id)
            if idempotency_key is not None:
                await self.receipts.put(
                    DemoResetReceipt(
                        idempotency_key=idempotency_key,
                        request_fingerprint=fingerprint,
                        recorded_at=now,
                        result_json=_result_to_json(result),
                    )
                )
            _ = reseeded  # generation digest is derived by the handler from the receipt
            return result
        finally:
            reset_authority.reset(authority_token)
            # 14. release the lock -- only if this attempt still owns it.
            await self.lock.release(handle)

    # -- target enumeration: manifest roots + registered markers + pointer-chain -----------

    async def _query_partition_items(
        self, table: TableName, partition_key: str
    ) -> list[StoredItem]:
        items: list[StoredItem] = []
        start: str | None = None
        while True:
            page = await self.driver.query(
                QueryRequest(
                    table=table,
                    partition_key=partition_key,
                    sort_key=SortKeyAll(),
                    consistent=True,
                    limit=100,
                    exclusive_start_sort_key=start,
                )
            )
            items.extend(page.items)
            start = page.last_evaluated_sort_key
            if start is None:
                return items

    async def _resolve_case_world_partitions(self, case_id: CaseId) -> set[str]:
        """The dynamic case-world partitions, **reconciled from the deterministic roots** --
        never a scan.

        The demo's case ID is a pure function of the frozen corpus (``predict_demo_case_id``),
        so ``VIEW_CURRENT#{case}`` and ``ACTION_CURRENT#{case}`` are deterministic partitions.
        Each holds the current pointer **and the history locators**, so every view / action
        the demo ever compiled or proposed is reachable by a bounded query from a known key --
        and each is written in the *same transaction* as its own partition's first row, so a
        crash cannot leave a child partition the pointer does not name. From an action we also
        derive its ``EXECUTION#{action}`` partition, and from a ``SENT`` execution the
        ``OUTBOUND_MESSAGE#{sha(ses_message_id)}`` correlation partition.
        """

        namespace = self.namespace
        found: set[str] = set()

        for item in await self._query_partition_items(
            TableName.SHAREABLE, dbkeys.view_current_partition(namespace, case_id)
        ):
            entity_type = item.get(ATTR_ENTITY_TYPE)
            if entity_type == EntityType.CURRENT_VIEW_POINTER.value:
                _s, pointer = codec_share.decode_view_pointer(item)
                found.add(dbkeys.view_partition(namespace, pointer.view_id))
            elif entity_type == EntityType.VIEW_HISTORY_LOCATOR.value:
                _s, locator = codec_share.decode_view_history(item)
                found.add(dbkeys.view_partition(namespace, locator.view_id))

        for item in await self._query_partition_items(
            TableName.SHAREABLE, dbkeys.action_current_partition(namespace, case_id)
        ):
            entity_type = item.get(ATTR_ENTITY_TYPE)
            action_id = None
            if entity_type == EntityType.CURRENT_ACTION_POINTER.value:
                _s, action_pointer = codec_share.decode_action_pointer(item)
                action_id = action_pointer.action_id
            elif entity_type == EntityType.ACTION_HISTORY_LOCATOR.value:
                _s, action_locator = codec_share.decode_action_history(item)
                action_id = action_locator.action_id
            if action_id is not None:
                found.add(dbkeys.action_partition(namespace, action_id))
                found.add(dbkeys.execution_partition(namespace, action_id))

        for execution_partition in [p for p in found if p.startswith(_EXECUTION_PREFIX)]:
            for item in await self._query_partition_items(TableName.SHAREABLE, execution_partition):
                if item.get(ATTR_ENTITY_TYPE) != EntityType.ACTION_EXECUTION.value:
                    continue
                _s, execution = codec_share.decode_execution(item)
                if execution.ses_message_id:
                    found.add(
                        dbkeys.outbound_message_partition(namespace, execution.ses_message_id)
                    )
        return found

    async def _all_target_partitions(self, manifest: DemoManifest) -> tuple[str, ...]:
        """Every reset-owned partition: the manifest's deterministic roots, the dynamically
        registered markers (``OPERATION`` roots, written atomically at creation), and the
        pointer-chain-reconciled case-world partitions."""

        targets: set[str] = set(manifest.partition_keys)
        targets |= set(await self.registrar.registered_partition_keys())
        cases = {
            CaseId(UUID(partition.removeprefix("NS#DEMO#CASE#")))
            for partition in targets
            if partition.startswith("NS#DEMO#CASE#")
            and _is_uuid(partition.removeprefix("NS#DEMO#CASE#"))
        }
        cases.add(self.seeder.demo_case_id)
        for case_id in cases:
            targets |= await self._resolve_case_world_partitions(case_id)
        return tuple(sorted(targets))

    # -- guard -----------------------------------------------------------------------------

    async def _guard_no_side_effects(self, targets: tuple[str, ...]) -> None:
        from chorus.application.services.demo_side_effect import SIDE_EFFECT_ACTOR
        from chorus.infrastructure.dynamodb.codec_idempotency import decode_idempotency
        from chorus.ports.idempotency import IdempotencyStatus

        for partition in targets:
            for table in _ALL_TABLES:
                for item in await self._query_partition_items(TableName(table), partition):
                    if item.get(ATTR_ENTITY_TYPE) != EntityType.IDEMPOTENCY_RECORD.value:
                        continue
                    _, record = decode_idempotency(item)
                    if (
                        record.key.actor_id_hash == SIDE_EFFECT_ACTOR
                        and record.status == IdempotencyStatus.IN_PROGRESS
                    ):
                        raise DemoResetInFlightSend("RESET_SIDE_EFFECT_IN_FLIGHT")

    async def _guard_no_in_flight_send(self, targets: tuple[str, ...]) -> None:
        """Fail closed if any EXECUTION partition holds a SENDING / SEND_UNKNOWN row -- an
        external message is in flight or its outcome is unknown."""

        for partition_key in targets:
            if not partition_key.startswith(_EXECUTION_PREFIX):
                continue
            for item in await self._query_partition_items(TableName.SHAREABLE, partition_key):
                if item.get(ATTR_ENTITY_TYPE) != EntityType.ACTION_EXECUTION.value:
                    continue
                _scope, execution = codec_share.decode_execution(item)
                if execution.state in RESET_IN_FLIGHT_STATES:
                    raise DemoResetInFlightSend("RESET_EXECUTION_IN_FLIGHT")

    # -- bounded purge -------------------------------------------------------------------

    def _keep_prefixes(self, manifest: DemoManifest, partition_key: str) -> tuple[str, ...]:
        if partition_key == "NS#DEMO":
            # Inventory is recovery state. Removing markers before their targets have been
            # verified would make a later reset blind after an interrupted purge.
            return (
                *dbkeys.DEMO_RESET_CONTROL_SORT_PREFIXES,
                *manifest.control_sort_prefixes,
                dbkeys.DEMO_REGISTERED_PARTITION_SORT_KEY_PREFIX,
                _CLOCK_SORT_KEY,
            )
        if partition_key == dbkeys.demo_clock_partition(self.namespace):
            return (_CLOCK_SORT_KEY,)
        return ()

    async def _clear_completed_registrations(self) -> int:
        registered = await self.registrar.registered_partition_keys()
        for partition in registered:
            await self.driver.write_item(
                DeleteItem(
                    key=ItemKey(
                        table=TableName.CORE,
                        partition_key=dbkeys.demo_control_partition(self.namespace),
                        sort_key=dbkeys.demo_registered_partition_sort_key(partition),
                    ),
                    condition=KeyPresent(),
                )
            )
        return len(registered)

    async def _purge(
        self, manifest: DemoManifest, targets: tuple[str, ...]
    ) -> DemoResetPurgeCounts:
        # Scheduler listing can lag a successful create. The durable projection already
        # names that schedule, even if recording CREATED lost the race to our lock.
        projected_schedules: set[str] = set()
        for partition in targets:
            for item in await self._query_partition_items(TableName.SHAREABLE, partition):
                if item.get(ATTR_ENTITY_TYPE) == EntityType.COMMITMENT_SCHEDULE.value:
                    _, projection = codec_share.decode_commitment_schedule(item)
                    if not projection.schedule_name.startswith(manifest.schedule_name_prefix):
                        raise DemoResetVerificationError("DEMO_RESET_SCHEDULE_NAMESPACE")
                    projected_schedules.add(projection.schedule_name)
        counts = DemoResetPurgeCounts()
        for partition_key in targets:
            keep = self._keep_prefixes(manifest, partition_key)
            for table in _ALL_TABLES:
                deleted = await self.partition_purge.delete_partition(
                    table=table, partition_key=partition_key, keep_sort_prefixes=keep
                )
                if deleted:
                    counts = counts.merged(DemoResetPurgeCounts(partitions=1, items=deleted))

        for prefix in manifest.private_object_prefixes:
            object_keys = await self.private_object_purge.list_prefix(
                bucket=self.private_bucket, prefix=prefix
            )
            removed = await self.private_object_purge.delete_objects(
                bucket=self.private_bucket, keys=object_keys
            )
            counts = counts.merged(DemoResetPurgeCounts(private_objects=removed))
        for prefix in manifest.export_object_prefixes:
            object_keys = await self.export_object_purge.list_prefix(
                bucket=self.export_bucket, prefix=prefix
            )
            removed = await self.export_object_purge.delete_objects(
                bucket=self.export_bucket, keys=object_keys
            )
            counts = counts.merged(DemoResetPurgeCounts(export_objects=removed))

        names = await self.schedule_purge.list_demo_schedules(
            name_prefix=manifest.schedule_name_prefix
        )
        removed_schedules = await self.schedule_purge.delete_schedules(
            names=tuple(sorted(set(names) | projected_schedules))
        )
        counts = counts.merged(DemoResetPurgeCounts(schedules=removed_schedules))
        return counts

    async def _verify_purged(self, manifest: DemoManifest, targets: tuple[str, ...]) -> None:
        for partition_key in targets:
            keep = self._keep_prefixes(manifest, partition_key)
            for table in _ALL_TABLES:
                remaining = await self.partition_purge.partition_item_sort_keys(
                    table=table, partition_key=partition_key
                )
                stale = [sk for sk in remaining if not any(sk.startswith(p) for p in keep)]
                if stale:
                    raise DemoResetVerificationError(
                        f"DEMO_RESET_STALE_PARTITION:{table}:{partition_key}"
                    )

    # -- fresh manifest --------------------------------------------------------------------

    def _fresh_manifest(self, previous: DemoManifest) -> DemoManifest:
        """The manifest for the newly seeded demo: the deterministic reset-owned roots.

        The demo's community and case IDs are pure functions of the frozen corpus, so every
        partition rooted at either is nameable now -- ``NS#DEMO`` (community + control plane),
        ``NS#DEMO#COMM#{community}`` (contributors, messages, evidence roots), and the
        case-keyed roots ``CASE#{case}`` / ``FENCE#{case}`` / ``VIEW_CURRENT#{case}`` /
        ``ACTION_CURRENT#{case}``. The genuinely dynamic ``OPERATION`` / ``VIEW`` / ``ACTION`` /
        ``EXECUTION`` / ``OUTBOUND_MESSAGE`` partitions are re-registered (``OPERATION``,
        atomically at creation) or re-reconciled from the pointer chain (the rest) as the new
        run creates them.
        """

        community = str(self.seeder.community_id)
        case = str(self.seeder.demo_case_id)
        return DemoManifest(
            seed_version=self.seed_version,
            created_at=self.seeder.clock.now(),
            version=previous.version + 1,
            partition_keys=(
                "NS#DEMO",
                f"NS#DEMO#COMM#{community}",
                f"NS#DEMO#CASE#{case}",
                f"NS#DEMO#FENCE#{case}",
                f"NS#DEMO#VIEW_CURRENT#{case}",
                f"NS#DEMO#ACTION_CURRENT#{case}",
            ),
            control_sort_prefixes=previous.control_sort_prefixes,
            private_object_prefixes=previous.private_object_prefixes,
            export_object_prefixes=previous.export_object_prefixes,
            schedule_name_prefix=previous.schedule_name_prefix,
        )


# -- the frozen receipt as JSON, for the durable replay record ---------------------------


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except ValueError:
        return False


def _result_to_json(result: DemoResetResult) -> str:
    return json.dumps(
        {
            "reset_id": str(result.reset_id),
            "namespace": result.namespace,
            "seed_version": result.seed_version,
            "corpus_sha256": result.corpus_sha256.value,
            "logical_now": result.logical_now.isoformat(),
            "community_id": str(result.community_id.value),
            "destination_id": result.destination_id,
            "contributors": [
                {
                    "actor": c.actor,
                    "pseudonym": c.pseudonym,
                    "contributor_id": str(c.contributor_id.value),
                }
                for c in result.contributors
            ],
            "evidence": [
                {
                    "evidence_id": str(e.evidence_id.value),
                    "media_type": e.media_type,
                    "sha256": e.sha256.value,
                }
                for e in result.evidence
            ],
            "counts": {
                "deleted": result.counts.deleted,
                "messages": result.counts.messages,
                "contributors": result.counts.contributors,
                "evidence": result.counts.evidence,
            },
            "audit_event_id": str(result.audit_event_id),
        }
    )


def _result_from_json(raw: str) -> DemoResetResult:
    data = json.loads(raw)
    return DemoResetResult(
        reset_id=UUID(data["reset_id"]),
        namespace=data["namespace"],
        seed_version=data["seed_version"],
        corpus_sha256=Sha256Digest(data["corpus_sha256"]),
        logical_now=datetime.fromisoformat(data["logical_now"]),
        community_id=CommunityId(UUID(data["community_id"])),
        destination_id=data["destination_id"],
        contributors=tuple(
            ResetContributor(
                actor=c["actor"],
                pseudonym=c["pseudonym"],
                contributor_id=ContributorId(UUID(c["contributor_id"])),
            )
            for c in data["contributors"]
        ),
        evidence=tuple(
            ResetEvidence(
                evidence_id=EvidenceItemId(UUID(e["evidence_id"])),
                media_type=e["media_type"],
                sha256=Sha256Digest(e["sha256"]),
            )
            for e in data["evidence"]
        ),
        counts=ResetCounts(
            deleted=data["counts"]["deleted"],
            messages=data["counts"]["messages"],
            contributors=data["counts"]["contributors"],
            evidence=data["counts"]["evidence"],
        ),
        replayed=True,
        audit_event_id=UUID(data["audit_event_id"]),
    )


__all__ = ["LOCK_TTL_SECONDS", "DeployedDemoReset"]
