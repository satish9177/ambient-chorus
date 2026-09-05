"""Core-table repository: private community, case, fact, mandate, and fence persistence.

Read intent is fixed by the method name. ``load_*`` hard-codes ``ConsistentRead=True`` because
those results inform authorization and state-changing decisions; ``read_*`` is eventually
consistent and exists only for the display projections the frozen access-pattern table marks
eventual. No method accepts a caller-supplied consistency flag.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from chorus.domain.entities import (
    ApplicationOperation,
    Community,
    CommunityCase,
    CommunityMessage,
    Contributor,
    EvidenceItem,
    EvidenceRoot,
    InvestigationAssessment,
)
from chorus.domain.errors import IntegrityError
from chorus.domain.facts import Fact, Report
from chorus.domain.ids import (
    AssessmentId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    EvidenceRootId,
    ExecutionId,
    FactId,
    MandateId,
    MessageId,
    OperationId,
    ReportId,
    Sha256Digest,
)
from chorus.domain.mandates import DisclosureMandate
from chorus.domain.time import epoch_micros
from chorus.infrastructure.dynamodb import codec_case, codec_core, codec_fence, codec_mandate, keys
from chorus.infrastructure.dynamodb.codec import (
    ATTR_VERSION,
    DecodedScope,
)
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.guards import (
    UNCHECKED,
    EntityIdentity,
    create_operation,
    replace_operation,
    require_same,
    validate_page_scope,
    validate_scope,
)
from chorus.ports.errors import (
    CrossCaseViolationError,
    ModelLimitExceededError,
    NotFoundError,
    PersistenceConflictError,
)
from chorus.ports.limits import BATCH_GET_MAX_KEYS, MAX_ACTIVE_FACTS_PER_CASE, MAX_PAGE_SIZE
from chorus.ports.pagination import Page, PageCursor, PageRequest, QueryBinding
from chorus.ports.records import (
    AgentInvocationResult,
    ChannelUniquenessLock,
    EvidenceRootLocator,
    FactMandateAssociation,
    FeedSignalProjection,
    MandatePointerExpectation,
    MessageFeedEntry,
    MonitorApplyProgress,
    MonitorSnapshotChunk,
    MonitorSnapshotKind,
    MonitorSnapshotManifest,
    SendFence,
    StoredCurrentMandatePointer,
)
from chorus.ports.scopes import CaseScope, CommunityScope, NamespaceScope, OperationScope
from chorus.ports.storage import (
    AllOf,
    AnyOf,
    AttributeAtMostNumber,
    AttributeEqualsNumber,
    AttributeEqualsString,
    CheckItem,
    DeleteItem,
    ItemKey,
    KeyAbsent,
    PutItem,
    QueryRequest,
    SortKeyBeginsWith,
    SortKeyBetween,
    StorageDriver,
    StoredItem,
    TableName,
)

ATTR_MANDATE_VERSION = "mandate_version"


def _expired_or_absent(now: datetime) -> AnyOf:
    """No fence holds this case at ``now``: either none exists, or the stored one expired.

    ``AttributeAtMostNumber`` is ``stored <= now``, so a fence is live for every instant
    strictly before its deadline and expired from the deadline onwards.
    """

    return AnyOf(
        (
            KeyAbsent(),
            AttributeAtMostNumber(
                name=codec_fence.ATTR_FENCE_EXPIRES_AT_MICROS, value=epoch_micros(now)
            ),
        )
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _PageContext:
    binding: QueryBinding
    partition_key: str


@dataclass(slots=True)
class CoreRepository:
    """Private-zone repository for the Core table."""

    driver: StorageDriver
    cursors: SignedCursorCodec

    # -- reads -------------------------------------------------------------------------

    async def _get[EntityT](
        self,
        key: ItemKey,
        decode: Callable[[StoredItem], tuple[DecodedScope, EntityT]],
        *,
        consistent: bool,
    ) -> tuple[DecodedScope, EntityT] | None:
        item = await self.driver.get_item(key, consistent=consistent)
        if item is None:
            return None
        return decode(item)

    async def load_community(self, scope: NamespaceScope, community_id: CommunityId) -> Community:
        key = codec_core.community_key(scope, community_id)
        loaded = await self._get(key, codec_core.decode_community, consistent=True)
        if loaded is None:
            raise NotFoundError("COMMUNITY")
        decoded, community = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="COMMUNITY",
            namespace=scope.namespace,
            community_id=community_id,
            case_id=None,
        )
        require_same(community.community_id, community_id, "COMMUNITY")
        require_same(community.namespace, scope.namespace, "COMMUNITY")
        return community

    async def load_contributor(
        self, scope: CommunityScope, contributor_id: ContributorId
    ) -> Contributor:
        key = codec_core.contributor_key(scope, contributor_id)
        loaded = await self._get(key, codec_core.decode_contributor, consistent=True)
        if loaded is None:
            raise NotFoundError("CONTRIBUTOR")
        decoded, contributor = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="CONTRIBUTOR",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=None,
        )
        require_same(contributor.contributor_id, contributor_id, "CONTRIBUTOR")
        require_same(contributor.community_id, scope.community_id, "CONTRIBUTOR")
        return contributor

    async def load_message(
        self, scope: CommunityScope, entry: MessageFeedEntry
    ) -> CommunityMessage:
        key = codec_core.message_key(scope, sent_at=entry.sent_at, message_id=entry.message_id)
        loaded = await self._get(key, codec_core.decode_message, consistent=True)
        if loaded is None:
            raise NotFoundError("COMMUNITY_MESSAGE")
        decoded, message = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="COMMUNITY_MESSAGE",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=None,
        )
        require_same(message.message_id, entry.message_id, "COMMUNITY_MESSAGE")
        require_same(message.community_id, scope.community_id, "COMMUNITY_MESSAGE")
        return message

    async def load_channel_lock(
        self, scope: CommunityScope, *, adapter: str, channel_message_id_sha256: Sha256Digest
    ) -> ChannelUniquenessLock | None:
        key = codec_core.channel_lock_key(
            scope, adapter=adapter, channel_message_id_sha256=channel_message_id_sha256
        )
        loaded = await self._get(key, codec_core.decode_channel_lock, consistent=True)
        if loaded is None:
            return None
        decoded, lock = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="CHANNEL_UNIQUENESS_LOCK",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=None,
        )
        require_same(lock.community_id, scope.community_id, "CHANNEL_UNIQUENESS_LOCK")
        return lock

    async def load_operation(
        self, scope: NamespaceScope, operation_id: OperationId
    ) -> ApplicationOperation:
        key = codec_core.operation_key(scope, operation_id)
        loaded = await self._get(key, codec_core.decode_operation, consistent=True)
        if loaded is None:
            raise NotFoundError("APPLICATION_OPERATION")
        decoded, operation = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="APPLICATION_OPERATION",
            namespace=scope.namespace,
            community_id=None,
            case_id=UNCHECKED,
        )
        require_same(operation.operation_id, operation_id, "APPLICATION_OPERATION")
        return operation

    async def load_evidence_root(
        self, scope: CommunityScope, root_sha256: Sha256Digest
    ) -> EvidenceRoot | None:
        key = codec_core.evidence_root_key(scope, root_sha256)
        loaded = await self._get(key, codec_core.decode_evidence_root, consistent=True)
        if loaded is None:
            return None
        decoded, root = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="EVIDENCE_ROOT",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=None,
        )
        require_same(root.community_id, scope.community_id, "EVIDENCE_ROOT")
        require_same(root.root_sha256, root_sha256, "EVIDENCE_ROOT")
        return root

    async def load_evidence_roots_by_id(
        self, scope: CommunityScope, root_ids: tuple[EvidenceRootId, ...]
    ) -> tuple[EvidenceRoot, ...]:
        """Resolve roots by identifier with two direct-key batch gets and nothing else.

        The first batch reads exactly the ID locators asked for; the second reads exactly the
        canonical rows those locators name. Both are ``BatchGetItem`` on keys this method
        constructed, so the no-scan and no-GSI guarantees hold by construction rather than by
        review. Anything the pair cannot resolve -- an absent locator, a locator whose root is
        gone, a row that decodes into another community -- is an ``IntegrityError`` that quotes
        nothing, because an ancestry chain that cannot be completed must never be answered with
        a shorter one.
        """

        if len(set(root_ids)) != len(root_ids):
            raise ValueError("requested evidence root IDs must be unique")
        if len(root_ids) > BATCH_GET_MAX_KEYS:
            raise ModelLimitExceededError("EVIDENCE_ROOT_BATCH")
        if not root_ids:
            return ()

        locator_keys = {
            codec_core.evidence_root_locator_key(scope, root_id).sort_key: root_id
            for root_id in root_ids
        }
        locator_items = await self.driver.batch_get_items(
            tuple(codec_core.evidence_root_locator_key(scope, root_id) for root_id in root_ids),
            consistent=True,
        )
        by_root_id: dict[EvidenceRootId, Sha256Digest] = {}
        for item in locator_items:
            decoded, locator = codec_core.decode_evidence_root_locator(item)
            key = codec_core.evidence_root_locator_key(scope, locator.root_id)
            validate_scope(
                decoded,
                key=key,
                entity_ref="EVIDENCE_ROOT_LOCATOR",
                namespace=scope.namespace,
                community_id=scope.community_id,
                case_id=None,
            )
            require_same(locator.community_id, scope.community_id, "EVIDENCE_ROOT_LOCATOR")
            if key.sort_key not in locator_keys:
                raise CrossCaseViolationError("EVIDENCE_ROOT_LOCATOR")
            by_root_id[locator.root_id] = locator.root_sha256
        if any(root_id not in by_root_id for root_id in root_ids):
            # A root whose locator is absent cannot be addressed at all, and an ancestry
            # answer built without it would be an under-count wearing the shape of a result.
            raise IntegrityError("EVIDENCE_ROOT_LOCATOR")

        digests = {by_root_id[root_id] for root_id in root_ids}
        root_keys = {codec_core.evidence_root_key(scope, digest).sort_key for digest in digests}
        root_items = await self.driver.batch_get_items(
            tuple(
                codec_core.evidence_root_key(scope, digest) for digest in sorted(digests, key=str)
            ),
            consistent=True,
        )
        roots: dict[EvidenceRootId, EvidenceRoot] = {}
        for item in root_items:
            decoded, root = codec_core.decode_evidence_root(item)
            key = codec_core.evidence_root_key(scope, root.root_sha256)
            validate_scope(
                decoded,
                key=key,
                entity_ref="EVIDENCE_ROOT",
                namespace=scope.namespace,
                community_id=scope.community_id,
                case_id=None,
            )
            require_same(root.community_id, scope.community_id, "EVIDENCE_ROOT")
            if key.sort_key not in root_keys:
                raise CrossCaseViolationError("EVIDENCE_ROOT")
            roots[root.root_id] = root
        ordered: list[EvidenceRoot] = []
        for root_id in root_ids:
            resolved = roots.get(root_id)
            if resolved is None or resolved.root_sha256 != by_root_id[root_id]:
                # The locator named a row that is missing, or one that turned out to be a
                # different root's content. Either way the pair disagrees, and a disagreement
                # between two immutable create-only items is an integrity failure.
                raise IntegrityError("EVIDENCE_ROOT")
            ordered.append(resolved)
        return tuple(ordered)

    async def _case(self, scope: CaseScope, *, consistent: bool) -> CommunityCase:
        key = codec_core.case_key(scope)
        loaded = await self._get(key, codec_core.decode_case, consistent=consistent)
        if loaded is None:
            raise NotFoundError("COMMUNITY_CASE")
        decoded, case = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="COMMUNITY_CASE",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(case.case_id, scope.case_id, "COMMUNITY_CASE")
        require_same(case.community_id, scope.community_id, "COMMUNITY_CASE")
        return case

    async def load_case(self, scope: CaseScope) -> CommunityCase:
        return await self._case(scope, consistent=True)

    async def read_case_for_display(self, scope: CaseScope) -> CommunityCase:
        return await self._case(scope, consistent=False)

    async def load_report(self, scope: CaseScope, report_id: ReportId) -> Report:
        key = codec_case.report_key(scope, report_id)
        loaded = await self._get(key, codec_case.decode_report, consistent=True)
        if loaded is None:
            raise NotFoundError("REPORT")
        decoded, report = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="REPORT",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(report.report_id, report_id, "REPORT")
        require_same(report.case_id, scope.case_id, "REPORT")
        require_same(report.community_id, scope.community_id, "REPORT")
        return report

    async def load_facts(self, scope: CaseScope, fact_ids: tuple[FactId, ...]) -> tuple[Fact, ...]:
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("requested fact IDs must be unique")
        if len(fact_ids) > BATCH_GET_MAX_KEYS:
            raise ModelLimitExceededError("FACT_BATCH")
        if not fact_ids:
            return ()
        requested = {codec_case.fact_key(scope, fact_id).sort_key for fact_id in fact_ids}
        items = await self.driver.batch_get_items(
            tuple(codec_case.fact_key(scope, fact_id) for fact_id in fact_ids), consistent=True
        )
        decoded_facts: dict[FactId, Fact] = {}
        for item in items:
            decoded, fact = codec_case.decode_fact(item)
            key = codec_case.fact_key(scope, fact.fact_id)
            validate_scope(
                decoded,
                key=key,
                entity_ref="FACT",
                namespace=scope.namespace,
                community_id=scope.community_id,
                case_id=scope.case_id,
            )
            require_same(fact.case_id, scope.case_id, "FACT")
            require_same(fact.community_id, scope.community_id, "FACT")
            if key.sort_key not in requested:
                raise CrossCaseViolationError("FACT")
            decoded_facts[fact.fact_id] = fact
        missing = tuple(fact_id for fact_id in fact_ids if fact_id not in decoded_facts)
        if missing:
            raise NotFoundError("FACT")
        return tuple(decoded_facts[fact_id] for fact_id in fact_ids)

    async def load_evidence_items(
        self, scope: CaseScope, evidence_ids: tuple[EvidenceItemId, ...]
    ) -> tuple[EvidenceItem, ...]:
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("requested evidence IDs must be unique")
        if len(evidence_ids) > BATCH_GET_MAX_KEYS:
            raise ModelLimitExceededError("EVIDENCE_BATCH")
        if not evidence_ids:
            return ()
        requested = {
            codec_case.evidence_item_key(scope, evidence_id).sort_key
            for evidence_id in evidence_ids
        }
        items = await self.driver.batch_get_items(
            tuple(codec_case.evidence_item_key(scope, evidence_id) for evidence_id in evidence_ids),
            consistent=True,
        )
        decoded_items: dict[EvidenceItemId, EvidenceItem] = {}
        for item in items:
            decoded, evidence = codec_case.decode_evidence_item(item)
            key = codec_case.evidence_item_key(scope, evidence.evidence_id)
            validate_scope(
                decoded,
                key=key,
                entity_ref="EVIDENCE_ITEM",
                namespace=scope.namespace,
                community_id=scope.community_id,
                case_id=scope.case_id,
            )
            require_same(evidence.case_id, scope.case_id, "EVIDENCE_ITEM")
            require_same(evidence.community_id, scope.community_id, "EVIDENCE_ITEM")
            if key.sort_key not in requested:
                raise CrossCaseViolationError("EVIDENCE_ITEM")
            decoded_items[evidence.evidence_id] = evidence
        if any(evidence_id not in decoded_items for evidence_id in evidence_ids):
            raise NotFoundError("EVIDENCE_ITEM")
        return tuple(decoded_items[evidence_id] for evidence_id in evidence_ids)

    async def load_mandate_version(
        self, scope: CaseScope, mandate_id: MandateId, version: int
    ) -> DisclosureMandate:
        key = codec_mandate.mandate_version_key(scope, mandate_id, version)
        loaded = await self._get(key, codec_mandate.decode_mandate, consistent=True)
        if loaded is None:
            raise NotFoundError("DISCLOSURE_MANDATE")
        decoded, mandate = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="DISCLOSURE_MANDATE",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(mandate.mandate_id, mandate_id, "DISCLOSURE_MANDATE")
        require_same(mandate.version, version, "DISCLOSURE_MANDATE")
        require_same(mandate.case_id, scope.case_id, "DISCLOSURE_MANDATE")
        require_same(mandate.community_id, scope.community_id, "DISCLOSURE_MANDATE")
        return mandate

    async def load_current_mandate_pointer(
        self, scope: CaseScope, mandate_id: MandateId
    ) -> StoredCurrentMandatePointer:
        key = codec_mandate.mandate_pointer_key(scope, mandate_id)
        loaded = await self._get(key, codec_mandate.decode_mandate_pointer, consistent=True)
        if loaded is None:
            raise NotFoundError("CURRENT_MANDATE_POINTER")
        decoded, stored = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="CURRENT_MANDATE_POINTER",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(stored.pointer.mandate_id, mandate_id, "CURRENT_MANDATE_POINTER")
        require_same(stored.pointer.case_id, scope.case_id, "CURRENT_MANDATE_POINTER")
        return stored

    async def load_current_mandate_pointers(
        self, scope: CaseScope, request: PageRequest
    ) -> Page[StoredCurrentMandatePointer]:
        context = _PageContext(
            binding=QueryBinding.CORE_CASE_MANDATE_POINTERS,
            partition_key=keys.case_partition(scope.namespace, scope.case_id),
        )
        return await self._page(
            scope=scope,
            context=context,
            table=TableName.CORE,
            sort_key=SortKeyBeginsWith(keys.MANDATE_CURRENT_SORT_KEY_PREFIX),
            request=request,
            consistent=True,
            decode=codec_mandate.decode_mandate_pointer,
            identity=lambda stored: EntityIdentity(
                namespace=stored.namespace,
                community_id=stored.community_id,
                case_id=stored.pointer.case_id,
            ),
            address=lambda stored: codec_mandate.mandate_pointer_key(
                scope, stored.pointer.mandate_id
            ),
            entity_ref="CURRENT_MANDATE_POINTER",
        )

    async def load_agent_invocation(
        self, scope: CaseScope, invocation_id: UUID
    ) -> AgentInvocationResult | None:
        key = codec_fence.agent_invocation_key(scope, invocation_id)
        loaded = await self._get(key, codec_fence.decode_agent_invocation, consistent=True)
        if loaded is None:
            return None
        decoded, result = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="AGENT_INVOCATION_RESULT",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(result.invocation_id, invocation_id, "AGENT_INVOCATION_RESULT")
        require_same(result.case_id, scope.case_id, "AGENT_INVOCATION_RESULT")
        return result

    async def _signals_by_message_id(
        self,
        scope: CommunityScope,
        message_ids: tuple[MessageId, ...],
        *,
        consistent: bool,
    ) -> dict[MessageId, FeedSignalProjection]:
        """Fetch exactly the signals for these messages, by direct key.

        Paginating the ``MESSAGE_SIGNAL#`` prefix and hoping the page overlaps the feed page
        was the earlier approach, and it was wrong twice over: signal sort keys are ordered by
        message UUID rather than by time, so the first hundred signals bear no relation to the
        current page of messages, and the discarded second cursor silently truncated the join
        for any community with more than a page of signals. A feed page already carries at
        most a hundred message IDs, so the exact keys are known and a bounded ``BatchGetItem``
        answers precisely.
        """

        unique = tuple(dict.fromkeys(message_ids))
        if not unique:
            return {}
        if len(unique) > BATCH_GET_MAX_KEYS:
            raise ModelLimitExceededError("FEED_SIGNAL_BATCH")
        keys_by_sort = {
            codec_core.feed_signal_key(scope, message_id).sort_key: message_id
            for message_id in unique
        }
        items = await self.driver.batch_get_items(
            tuple(codec_core.feed_signal_key(scope, message_id) for message_id in unique),
            consistent=consistent,
        )
        found: dict[MessageId, FeedSignalProjection] = {}
        for item in items:
            decoded, signal = codec_core.decode_feed_signal(item)
            key = codec_core.feed_signal_key(scope, signal.message_id)
            validate_scope(
                decoded,
                key=key,
                entity_ref="FEED_SIGNAL_PROJECTION",
                namespace=scope.namespace,
                community_id=scope.community_id,
                case_id=None,
            )
            require_same(signal.community_id, scope.community_id, "FEED_SIGNAL_PROJECTION")
            if key.sort_key not in keys_by_sort:
                raise CrossCaseViolationError("FEED_SIGNAL_PROJECTION")
            found[signal.message_id] = signal
        return found

    async def load_feed_signals(
        self, scope: CommunityScope, message_ids: tuple[MessageId, ...]
    ) -> dict[MessageId, FeedSignalProjection]:
        """Strongly read the signals for exactly these messages.

        Strong because the Monitor apply gates read it: whether a message is already linked to
        another case decides whether a whole answer is refused, and an authorization-shaped
        decision never rests on an eventually consistent read.
        """

        return await self._signals_by_message_id(scope, message_ids, consistent=True)

    async def read_feed_signals_for_messages(
        self, scope: CommunityScope, message_ids: tuple[MessageId, ...]
    ) -> dict[MessageId, FeedSignalProjection]:
        """Eventually read the signals for exactly these messages, for display only."""

        return await self._signals_by_message_id(scope, message_ids, consistent=False)

    async def load_monitor_progress(
        self, scope: OperationScope, invocation_id: UUID
    ) -> MonitorApplyProgress | None:
        key = codec_core.monitor_progress_key(scope, invocation_id)
        loaded = await self._get(key, codec_core.decode_monitor_progress, consistent=True)
        if loaded is None:
            return None
        decoded, progress = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="MONITOR_APPLY_PROGRESS",
            namespace=scope.namespace,
            community_id=None,
            case_id=None,
        )
        require_same(progress.invocation_id, invocation_id, "MONITOR_APPLY_PROGRESS")
        require_same(progress.operation_id, scope.operation_id, "MONITOR_APPLY_PROGRESS")
        return progress

    async def load_monitor_snapshot_manifest(
        self, scope: OperationScope, *, kind: MonitorSnapshotKind, invocation_id: UUID
    ) -> MonitorSnapshotManifest | None:
        key = codec_core.monitor_snapshot_manifest_key(scope, kind, invocation_id)
        loaded = await self._get(key, codec_core.decode_monitor_snapshot_manifest, consistent=True)
        if loaded is None:
            return None
        decoded, manifest = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="MONITOR_SNAPSHOT_MANIFEST",
            namespace=scope.namespace,
            community_id=None,
            case_id=None,
        )
        require_same(manifest.invocation_id, invocation_id, "MONITOR_SNAPSHOT_MANIFEST")
        require_same(manifest.operation_id, scope.operation_id, "MONITOR_SNAPSHOT_MANIFEST")
        require_same(manifest.kind, kind, "MONITOR_SNAPSHOT_MANIFEST")
        return manifest

    async def load_monitor_snapshot_chunks(
        self, scope: OperationScope, manifest: MonitorSnapshotManifest
    ) -> tuple[MonitorSnapshotChunk, ...]:
        """Batch-get exactly the chunks the manifest names, in the order it names them.

        Addressed by direct key rather than by walking the chunk prefix, so a partially
        written or over-written snapshot cannot present itself as a complete one: the
        manifest states the count, and a chunk it names that is not there is an integrity
        failure rather than a shorter payload.
        """

        keys = tuple(
            codec_core.monitor_snapshot_chunk_key(
                scope, manifest.kind, manifest.invocation_id, index
            )
            for index in range(manifest.chunk_count)
        )
        items = await self.driver.batch_get_items(keys, consistent=True)
        by_index: dict[int, MonitorSnapshotChunk] = {}
        addressed = {key.sort_key for key in keys}
        for item in items:
            decoded, chunk = codec_core.decode_monitor_snapshot_chunk(item)
            validate_scope(
                decoded,
                key=codec_core.monitor_snapshot_chunk_key(
                    scope, chunk.kind, chunk.invocation_id, chunk.index
                ),
                entity_ref="MONITOR_SNAPSHOT_CHUNK",
                namespace=scope.namespace,
                community_id=None,
                case_id=None,
            )
            require_same(chunk.invocation_id, manifest.invocation_id, "MONITOR_SNAPSHOT_CHUNK")
            require_same(chunk.operation_id, scope.operation_id, "MONITOR_SNAPSHOT_CHUNK")
            require_same(chunk.community_id, manifest.community_id, "MONITOR_SNAPSHOT_CHUNK")
            require_same(chunk.kind, manifest.kind, "MONITOR_SNAPSHOT_CHUNK")
            if decoded.sort_key not in addressed or chunk.index in by_index:
                raise CrossCaseViolationError("MONITOR_SNAPSHOT_CHUNK")
            by_index[chunk.index] = chunk
        if len(by_index) != manifest.chunk_count:
            raise IntegrityError("MONITOR_SNAPSHOT_CHUNK:missing")
        return tuple(by_index[index] for index in range(manifest.chunk_count))

    def stage_create_monitor_snapshot_manifest(
        self, scope: OperationScope, manifest: MonitorSnapshotManifest
    ) -> PutItem:
        return create_operation(
            codec_core.monitor_snapshot_manifest_key(scope, manifest.kind, manifest.invocation_id),
            codec_core.encode_monitor_snapshot_manifest(scope, manifest),
        )

    def stage_create_monitor_snapshot_chunk(
        self, scope: OperationScope, chunk: MonitorSnapshotChunk
    ) -> PutItem:
        return create_operation(
            codec_core.monitor_snapshot_chunk_key(
                scope, chunk.kind, chunk.invocation_id, chunk.index
            ),
            codec_core.encode_monitor_snapshot_chunk(scope, chunk),
        )

    async def load_operation_agent_invocation(
        self, scope: OperationScope, invocation_id: UUID
    ) -> AgentInvocationResult | None:
        key = codec_fence.operation_agent_invocation_key(scope, invocation_id)
        loaded = await self._get(
            key, codec_fence.decode_operation_agent_invocation, consistent=True
        )
        if loaded is None:
            return None
        decoded, result = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="AGENT_INVOCATION_RESULT",
            namespace=scope.namespace,
            community_id=None,
            case_id=None,
        )
        require_same(result.invocation_id, invocation_id, "AGENT_INVOCATION_RESULT")
        require_same(result.operation_id, scope.operation_id, "AGENT_INVOCATION_RESULT")
        return result

    async def load_operation_invocation_ids(self, scope: OperationScope) -> tuple[UUID, ...]:
        """Strongly read which agent invocations this operation partition already records.

        One bounded query on the partition the caller already holds the key for -- not a scan,
        not an index, and not a walk of anything unbounded. Both the invocation result and the
        apply-progress row live under the ``AGENT_`` prefix and both name their invocation, so
        a single range condition covers everything that can answer "which invocation owns this
        operation".

        It exists for the worker's job binding: a job naming a different invocation than the
        one this operation's own durable records already name is a misrouted delivery, and the
        worker has to be able to tell before it claims anything.
        """

        result = await self.driver.query(
            QueryRequest(
                table=TableName.CORE,
                partition_key=keys.operation_partition(scope.namespace, scope.operation_id),
                sort_key=SortKeyBeginsWith(keys.OPERATION_INVOCATION_SORT_KEY_PREFIX),
                consistent=True,
                limit=MAX_PAGE_SIZE,
            )
        )
        found: list[UUID] = []
        for item in result.items:
            raw = item.get("invocation_id")
            if not isinstance(raw, str):
                raise IntegrityError("APPLICATION_OPERATION:invocation")
            try:
                invocation_id = UUID(raw)
            except ValueError as error:
                raise IntegrityError("APPLICATION_OPERATION:invocation") from error
            if invocation_id not in found:
                found.append(invocation_id)
        return tuple(found)

    async def load_send_fence(self, scope: CaseScope) -> SendFence | None:
        key = codec_fence.send_fence_key(scope)
        loaded = await self._get(key, codec_fence.decode_send_fence, consistent=True)
        if loaded is None:
            return None
        decoded, fence = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="SEND_FENCE",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(fence.case_id, scope.case_id, "SEND_FENCE")
        require_same(fence.community_id, scope.community_id, "SEND_FENCE")
        return fence

    # -- paged reads -------------------------------------------------------------------

    async def _page[EntityT](
        self,
        *,
        scope: CaseScope | CommunityScope,
        context: _PageContext,
        table: TableName,
        sort_key: SortKeyBeginsWith | SortKeyBetween,
        request: PageRequest,
        consistent: bool,
        decode: Callable[[StoredItem], tuple[DecodedScope, EntityT]],
        identity: Callable[[EntityT], EntityIdentity],
        address: Callable[[EntityT], ItemKey],
        entity_ref: str,
    ) -> Page[EntityT]:
        start: str | None = None
        if request.cursor is not None:
            start = self.cursors.verify(
                request.cursor,
                namespace=scope.namespace,
                binding=context.binding,
                partition_key=context.partition_key,
            )
        result = await self.driver.query(
            QueryRequest(
                table=table,
                partition_key=context.partition_key,
                sort_key=sort_key,
                consistent=consistent,
                limit=request.limit,
                exclusive_start_sort_key=start,
            )
        )
        entities: list[EntityT] = []
        case_id = scope.case_id if isinstance(scope, CaseScope) else UNCHECKED
        for item in result.items:
            decoded, entity = decode(item)
            validate_page_scope(
                decoded,
                identity(entity),
                expected_key=address(entity),
                entity_ref=entity_ref,
                namespace=scope.namespace,
                community_id=scope.community_id,
                case_id=case_id,
            )
            entities.append(entity)
        next_cursor: PageCursor | None = None
        if result.last_evaluated_sort_key is not None:
            next_cursor = self.cursors.issue(
                namespace=scope.namespace,
                binding=context.binding,
                partition_key=context.partition_key,
                sort_key=result.last_evaluated_sort_key,
            )
        return Page(items=tuple(entities), next_cursor=next_cursor)

    async def read_message_feed(
        self, scope: CommunityScope, *, start: datetime, end: datetime, request: PageRequest
    ) -> Page[CommunityMessage]:
        context = _PageContext(
            binding=QueryBinding.CORE_COMMUNITY_FEED,
            partition_key=keys.community_partition(scope.namespace, scope.community_id),
        )
        return await self._page(
            scope=scope,
            context=context,
            table=TableName.CORE,
            sort_key=SortKeyBetween(
                low=keys.message_sort_key_lower_bound(start),
                high=keys.message_sort_key_upper_bound(end),
            ),
            request=request,
            consistent=False,
            decode=codec_core.decode_message,
            identity=lambda message: EntityIdentity(
                namespace=message.namespace, community_id=message.community_id
            ),
            address=lambda message: codec_core.message_key(
                scope, sent_at=message.sent_at, message_id=message.message_id
            ),
            entity_ref="COMMUNITY_MESSAGE",
        )

    async def read_recent_messages(
        self, scope: CommunityScope, *, before: datetime, limit: int
    ) -> tuple[CommunityMessage, ...]:
        """Read the newest messages at or before an instant, newest first.

        Deliberately not a paginated surface: this is how a bounded Monitor context window is
        assembled, and a context window is a fixed-size look-back rather than something a
        caller walks. It is the same time-ordered partition query the feed uses, run in
        descending order so a limit takes the *most recent* prior messages rather than the
        oldest ones inside the window -- which is what an ascending limit would have given.
        """

        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError("recent message limit must be between 1 and the frozen maximum")
        result = await self.driver.query(
            QueryRequest(
                table=TableName.CORE,
                partition_key=keys.community_partition(scope.namespace, scope.community_id),
                sort_key=SortKeyBetween(
                    low=keys.MESSAGE_SORT_KEY_PREFIX,
                    high=keys.message_sort_key_upper_bound(before),
                ),
                consistent=False,
                limit=limit,
                ascending=False,
            )
        )
        messages: list[CommunityMessage] = []
        for item in result.items:
            decoded, message = codec_core.decode_message(item)
            validate_page_scope(
                decoded,
                EntityIdentity(namespace=message.namespace, community_id=message.community_id),
                expected_key=codec_core.message_key(
                    scope, sent_at=message.sent_at, message_id=message.message_id
                ),
                entity_ref="COMMUNITY_MESSAGE",
                namespace=scope.namespace,
                community_id=scope.community_id,
                case_id=UNCHECKED,
            )
            messages.append(message)
        return tuple(messages)

    async def read_feed_signals(
        self, scope: CommunityScope, request: PageRequest
    ) -> Page[FeedSignalProjection]:
        """Eventually page the discovered-pattern signals for one community feed."""

        context = _PageContext(
            binding=QueryBinding.CORE_COMMUNITY_FEED_SIGNALS,
            partition_key=keys.community_partition(scope.namespace, scope.community_id),
        )
        return await self._page(
            scope=scope,
            context=context,
            table=TableName.CORE,
            sort_key=SortKeyBeginsWith(keys.FEED_SIGNAL_SORT_KEY_PREFIX),
            request=request,
            consistent=False,
            decode=codec_core.decode_feed_signal,
            # A signal claims a namespace and a community. Its case is data the row points
            # at, not the scope it was stored under, so it is checked by the codec rather
            # than treated as this page's case boundary.
            identity=lambda signal: EntityIdentity(
                namespace=signal.namespace, community_id=signal.community_id
            ),
            address=lambda signal: codec_core.feed_signal_key(scope, signal.message_id),
            entity_ref="FEED_SIGNAL_PROJECTION",
        )

    async def read_case_facts(self, scope: CaseScope, request: PageRequest) -> Page[Fact]:
        context = _PageContext(
            binding=QueryBinding.CORE_CASE_FACTS,
            partition_key=keys.case_partition(scope.namespace, scope.case_id),
        )
        return await self._page(
            scope=scope,
            context=context,
            table=TableName.CORE,
            sort_key=SortKeyBeginsWith(keys.FACT_SORT_KEY_PREFIX),
            request=request,
            consistent=False,
            decode=codec_case.decode_fact,
            identity=lambda fact: EntityIdentity(
                namespace=fact.namespace,
                community_id=fact.community_id,
                case_id=fact.case_id,
            ),
            address=lambda fact: codec_case.fact_key(scope, fact.fact_id),
            entity_ref="FACT",
        )

    async def read_case_reports(self, scope: CaseScope, request: PageRequest) -> Page[Report]:
        context = _PageContext(
            binding=QueryBinding.CORE_CASE_REPORTS,
            partition_key=keys.case_partition(scope.namespace, scope.case_id),
        )
        return await self._page(
            scope=scope,
            context=context,
            table=TableName.CORE,
            sort_key=SortKeyBeginsWith(keys.REPORT_SORT_KEY_PREFIX),
            request=request,
            consistent=False,
            decode=codec_case.decode_report,
            identity=lambda report: EntityIdentity(
                namespace=report.namespace,
                community_id=report.community_id,
                case_id=report.case_id,
            ),
            address=lambda report: codec_case.report_key(scope, report.report_id),
            entity_ref="REPORT",
        )

    async def load_current_assessment(
        self, scope: CaseScope, assessment_id: AssessmentId
    ) -> InvestigationAssessment | None:
        """Strongly read the newest assessment and prove the case actually points at it.

        A bounded descending query with ``limit=1`` on the case's own ``ASSESSMENT#`` prefix.
        The sort key leads with the creation instant, so the newest row is the last one --
        which is why this reads descending rather than paging the prefix forward.

        The identity check is the point of the method. Assessments are append-only and the
        case's ``assessment_id`` is the only current pointer, so a newest row that the case
        does not name means an append committed beside the case update this caller read. That
        is a stale read, not a current assessment, and it answers ``None``.
        """

        result = await self.driver.query(
            QueryRequest(
                table=TableName.CORE,
                partition_key=keys.case_partition(scope.namespace, scope.case_id),
                sort_key=SortKeyBeginsWith(keys.ASSESSMENT_SORT_KEY_PREFIX),
                consistent=True,
                limit=1,
                ascending=False,
            )
        )
        if not result.items:
            return None
        decoded, assessment = codec_case.decode_assessment(result.items[0])
        key = codec_case.assessment_key(scope, assessment)
        validate_scope(
            decoded,
            key=key,
            entity_ref="INVESTIGATION_ASSESSMENT",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(assessment.case_id, scope.case_id, "INVESTIGATION_ASSESSMENT")
        if assessment.assessment_id != assessment_id:
            return None
        return assessment

    async def read_case_assessments(
        self, scope: CaseScope, request: PageRequest
    ) -> Page[InvestigationAssessment]:
        context = _PageContext(
            binding=QueryBinding.CORE_CASE_ASSESSMENTS,
            partition_key=keys.case_partition(scope.namespace, scope.case_id),
        )
        return await self._page(
            scope=scope,
            context=context,
            table=TableName.CORE,
            sort_key=SortKeyBeginsWith(keys.ASSESSMENT_SORT_KEY_PREFIX),
            request=request,
            consistent=False,
            decode=codec_case.decode_assessment,
            # An assessment carries only its case; namespace and community are checked on
            # the stored envelope, which is all the entity itself claims.
            identity=lambda assessment: EntityIdentity(case_id=assessment.case_id),
            address=lambda assessment: codec_case.assessment_key(scope, assessment),
            entity_ref="INVESTIGATION_ASSESSMENT",
        )

    # -- staged writes -----------------------------------------------------------------

    def stage_create_community(self, scope: NamespaceScope, community: Community) -> PutItem:
        return create_operation(
            codec_core.community_key(scope, community.community_id),
            codec_core.encode_community(scope, community),
        )

    def stage_update_community(
        self, scope: NamespaceScope, community: Community, *, expected_version: int
    ) -> PutItem:
        return replace_operation(
            codec_core.community_key(scope, community.community_id),
            codec_core.encode_community(scope, community),
            expected_version=expected_version,
            new_version=community.version,
        )

    def stage_create_contributor(self, scope: CommunityScope, contributor: Contributor) -> PutItem:
        return create_operation(
            codec_core.contributor_key(scope, contributor.contributor_id),
            codec_core.encode_contributor(scope, contributor),
        )

    def stage_update_contributor(
        self, scope: CommunityScope, contributor: Contributor, *, expected_version: int
    ) -> PutItem:
        return replace_operation(
            codec_core.contributor_key(scope, contributor.contributor_id),
            codec_core.encode_contributor(scope, contributor),
            expected_version=expected_version,
            new_version=contributor.version,
        )

    def stage_create_message(self, scope: CommunityScope, message: CommunityMessage) -> PutItem:
        return create_operation(
            codec_core.message_key(scope, sent_at=message.sent_at, message_id=message.message_id),
            codec_core.encode_message(scope, message),
        )

    def stage_update_message(
        self, scope: CommunityScope, message: CommunityMessage, *, expected_version: int
    ) -> PutItem:
        return replace_operation(
            codec_core.message_key(scope, sent_at=message.sent_at, message_id=message.message_id),
            codec_core.encode_message(scope, message),
            expected_version=expected_version,
            new_version=message.version,
        )

    def stage_create_channel_lock(
        self, scope: CommunityScope, lock: ChannelUniquenessLock
    ) -> PutItem:
        return create_operation(
            codec_core.channel_lock_key(
                scope,
                adapter=lock.adapter,
                channel_message_id_sha256=lock.channel_message_id_sha256,
            ),
            codec_core.encode_channel_lock(scope, lock),
        )

    def stage_create_feed_signal(
        self, scope: CommunityScope, signal: FeedSignalProjection
    ) -> PutItem:
        """Stage the create-only feed signal that accompanies a committed discovery."""

        return create_operation(
            codec_core.feed_signal_key(scope, signal.message_id),
            codec_core.encode_feed_signal(scope, signal),
        )

    def stage_update_feed_signal(
        self, scope: CommunityScope, signal: FeedSignalProjection, *, expected_version: int
    ) -> PutItem:
        """Stage a guarded replace of an existing feed signal.

        The projection is display data, so a refreshed label or case state has to be able to
        land. It is still a compare-and-set: two concurrent applies cannot both believe they
        wrote the current row, and the loser reloads rather than overwriting a change it never
        saw. What may *not* be decided here is whether the linkage itself is allowed -- that is
        a domain rule the apply service settles before any of this is staged.
        """

        return replace_operation(
            codec_core.feed_signal_key(scope, signal.message_id),
            codec_core.encode_feed_signal(scope, signal),
            expected_version=expected_version,
            new_version=signal.version,
        )

    def stage_create_monitor_progress(
        self, scope: OperationScope, progress: MonitorApplyProgress
    ) -> PutItem:
        return create_operation(
            codec_core.monitor_progress_key(scope, progress.invocation_id),
            codec_core.encode_monitor_progress(scope, progress),
        )

    def stage_update_monitor_progress(
        self, scope: OperationScope, progress: MonitorApplyProgress, *, expected_version: int
    ) -> PutItem:
        return replace_operation(
            codec_core.monitor_progress_key(scope, progress.invocation_id),
            codec_core.encode_monitor_progress(scope, progress),
            expected_version=expected_version,
            new_version=progress.version,
        )

    def stage_append_operation_agent_invocation(
        self, scope: OperationScope, result: AgentInvocationResult
    ) -> PutItem:
        return create_operation(
            codec_fence.operation_agent_invocation_key(scope, result.invocation_id),
            codec_fence.encode_operation_agent_invocation(scope, result),
        )

    async def record_operation_agent_invocation(
        self, scope: OperationScope, result: AgentInvocationResult
    ) -> None:
        """Write the run's invocation record, or accept that it is already written.

        A single create-only conditional write rather than a transaction, for two reasons.
        It is one item, so a transaction would buy nothing but a commit proof it would then
        need somewhere to put; and this record must be writable on the failure path, where
        the domain was left untouched and the point is to leave a durable trace of *why*
        without the tracing itself becoming another way to fail.

        A conditional failure means the record is already there. Both writers describe the
        same invocation identity with the same hashes, so the loser has nothing to correct.
        """

        try:
            await self.driver.write_item(
                self.stage_append_operation_agent_invocation(scope, result)
            )
        except PersistenceConflictError:
            return

    def stage_create_operation(
        self, scope: NamespaceScope, operation: ApplicationOperation
    ) -> PutItem:
        return create_operation(
            codec_core.operation_key(scope, operation.operation_id),
            codec_core.encode_operation(scope, operation),
        )

    async def apply_operation_transition(
        self,
        scope: NamespaceScope,
        operation: ApplicationOperation,
        *,
        expected_version: int,
    ) -> None:
        """Apply one guarded operation transition as a single conditional write.

        Deliberately not routed through a transaction. A ``TransactWriteItems`` call carries a
        client request token derived from the plan, and DynamoDB treats a repeat of that token
        inside a ten-minute window as an idempotent replay -- so two workers composing the
        byte-identical claim at one injected instant would both be told they had won it. A
        bare conditional write has no token, so the version condition is evaluated for every
        caller and exactly one of them succeeds.
        """

        await self.driver.write_item(
            self.stage_update_operation(scope, operation, expected_version=expected_version)
        )

    def stage_update_operation(
        self, scope: NamespaceScope, operation: ApplicationOperation, *, expected_version: int
    ) -> PutItem:
        return replace_operation(
            codec_core.operation_key(scope, operation.operation_id),
            codec_core.encode_operation(scope, operation),
            expected_version=expected_version,
            new_version=operation.version,
        )

    def stage_create_evidence_root_locator(
        self, scope: CommunityScope, locator: EvidenceRootLocator
    ) -> PutItem:
        return create_operation(
            codec_core.evidence_root_locator_key(scope, locator.root_id),
            codec_core.encode_evidence_root_locator(scope, locator),
        )

    def stage_create_evidence_root(self, scope: CommunityScope, root: EvidenceRoot) -> PutItem:
        return create_operation(
            codec_core.evidence_root_key(scope, root.root_sha256),
            codec_core.encode_evidence_root(scope, root),
        )

    def stage_create_case(self, scope: CaseScope, case: CommunityCase) -> PutItem:
        self._require_case_capacity(case)
        return create_operation(codec_core.case_key(scope), codec_core.encode_case(scope, case))

    def stage_update_case(
        self, scope: CaseScope, case: CommunityCase, *, expected_version: int
    ) -> PutItem:
        self._require_case_capacity(case)
        return replace_operation(
            codec_core.case_key(scope),
            codec_core.encode_case(scope, case),
            expected_version=expected_version,
            new_version=case.version,
        )

    def stage_require_case_version(self, scope: CaseScope, *, expected_version: int) -> CheckItem:
        """Condition on the case standing at exactly this version, writing nothing.

        The compile transaction's authorization guard. It is a ``CheckItem`` rather than a
        guarded update for two independent reasons, and either alone would be enough: the
        compiler's only Core write is the send fence, and a compile that bumped the case
        version would stale its own freshly written view against the exact-version check the
        proposal validator performs.
        """

        if expected_version < 1:
            raise ValueError("an expected case version must be positive")
        return CheckItem(
            key=codec_core.case_key(scope),
            condition=AttributeEqualsNumber(name=ATTR_VERSION, value=expected_version),
        )

    @staticmethod
    def _require_case_capacity(case: CommunityCase) -> None:
        if len(case.fact_ids) > MAX_ACTIVE_FACTS_PER_CASE:
            raise ModelLimitExceededError("CASE_FACTS")

    def stage_create_report(self, scope: CaseScope, report: Report) -> PutItem:
        return create_operation(
            codec_case.report_key(scope, report.report_id),
            codec_case.encode_report(scope, report),
        )

    def stage_update_report(
        self, scope: CaseScope, report: Report, *, expected_version: int
    ) -> PutItem:
        return replace_operation(
            codec_case.report_key(scope, report.report_id),
            codec_case.encode_report(scope, report),
            expected_version=expected_version,
            new_version=report.version,
        )

    def stage_create_fact(self, scope: CaseScope, fact: Fact) -> PutItem:
        return create_operation(
            codec_case.fact_key(scope, fact.fact_id), codec_case.encode_fact(scope, fact)
        )

    def stage_update_fact(self, scope: CaseScope, fact: Fact, *, expected_version: int) -> PutItem:
        return replace_operation(
            codec_case.fact_key(scope, fact.fact_id),
            codec_case.encode_fact(scope, fact),
            expected_version=expected_version,
            new_version=fact.version,
        )

    def stage_create_evidence_item(self, scope: CaseScope, item: EvidenceItem) -> PutItem:
        return create_operation(
            codec_case.evidence_item_key(scope, item.evidence_id),
            codec_case.encode_evidence_item(scope, item),
        )

    def stage_update_evidence_item(
        self, scope: CaseScope, item: EvidenceItem, *, expected_version: int
    ) -> PutItem:
        return replace_operation(
            codec_case.evidence_item_key(scope, item.evidence_id),
            codec_case.encode_evidence_item(scope, item),
            expected_version=expected_version,
            new_version=item.version,
        )

    def stage_append_assessment(
        self, scope: CaseScope, assessment: InvestigationAssessment
    ) -> PutItem:
        return create_operation(
            codec_case.assessment_key(scope, assessment),
            codec_case.encode_assessment(scope, assessment),
        )

    def stage_append_mandate_version(self, scope: CaseScope, mandate: DisclosureMandate) -> PutItem:
        return create_operation(
            codec_mandate.mandate_version_key(scope, mandate.mandate_id, mandate.version),
            codec_mandate.encode_mandate(scope, mandate),
        )

    def stage_replace_current_mandate_pointer(
        self,
        scope: CaseScope,
        pointer: StoredCurrentMandatePointer,
        *,
        expected: MandatePointerExpectation | None,
    ) -> PutItem:
        key = codec_mandate.mandate_pointer_key(scope, pointer.pointer.mandate_id)
        item = codec_mandate.encode_mandate_pointer(scope, pointer)
        if expected is None:
            if pointer.version != 1:
                raise ValueError("a first pointer write must be version 1")
            return PutItem(key=key, item=item, condition=KeyAbsent())
        if pointer.version != expected.row_version + 1:
            raise ValueError("a pointer replace must increment the row version by one")
        return PutItem(
            key=key,
            item=item,
            condition=AllOf(
                (
                    AttributeEqualsNumber(name=ATTR_VERSION, value=expected.row_version),
                    AttributeEqualsNumber(
                        name=ATTR_MANDATE_VERSION, value=expected.mandate_version
                    ),
                )
            ),
        )

    def stage_append_fact_mandate_association(
        self, scope: CaseScope, association: FactMandateAssociation
    ) -> PutItem:
        return create_operation(
            codec_mandate.fact_mandate_key(scope, association.fact_id, association.mandate_id),
            codec_mandate.encode_fact_mandate(scope, association),
        )

    def stage_append_agent_invocation(
        self, scope: CaseScope, result: AgentInvocationResult
    ) -> PutItem:
        return create_operation(
            codec_fence.agent_invocation_key(scope, result.invocation_id),
            codec_fence.encode_agent_invocation(scope, result),
        )

    def stage_require_no_live_send_fence(self, scope: CaseScope, *, now: datetime) -> CheckItem:
        """Assert no unexpired fence blocks an authorization-sensitive mutation.

        The comparison is exact microseconds, so a revocation cannot slip in ahead of a fence
        that is still live for a fraction of a second. Equality is expired, matching the
        frozen ``now < expires_at`` semantics.
        """

        return CheckItem(
            key=codec_fence.send_fence_key(scope),
            condition=_expired_or_absent(now),
        )

    # -- send fence --------------------------------------------------------------------

    async def acquire_send_fence(self, scope: CaseScope, fence: SendFence) -> SendFence:
        """Create the fence, take over an expired one, or replay an identical live fence."""

        key = codec_fence.send_fence_key(scope)
        item = codec_fence.encode_send_fence(scope, fence)
        condition = _expired_or_absent(fence.acquired_at)
        try:
            await self.driver.write_item(PutItem(key=key, item=item, condition=condition))
        except PersistenceConflictError:
            existing = await self.load_send_fence(scope)
            if existing is None:
                raise
            if existing.execution_id != fence.execution_id:
                raise
            # The same execution already holds a live fence; its original expiry stands so a
            # replay cannot silently extend the authorization window.
            return existing
        return fence

    async def release_send_fence(self, scope: CaseScope, execution_id: ExecutionId) -> None:
        """Delete only this execution's fence; another execution's fence is left intact."""

        await self.driver.write_item(
            DeleteItem(
                key=codec_fence.send_fence_key(scope),
                condition=AttributeEqualsString(
                    name=codec_fence.ATTR_FENCE_EXECUTION_ID, value=str(execution_id)
                ),
            )
        )
