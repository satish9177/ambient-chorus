"""Append-only audit repository.

There is no update and no delete. Every write is a create-only put, which is exactly what the
unit of work requires before it will accept a plan as audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from chorus.domain.entities import AuditEvent
from chorus.domain.ids import CaseId, CommunityId
from chorus.infrastructure.dynamodb import codec_audit, keys
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.guards import (
    EntityIdentity,
    create_operation,
    require_same,
    validate_page_scope,
    validate_scope,
)
from chorus.ports.errors import CrossCaseViolationError
from chorus.ports.pagination import Page, PageCursor, PageRequest, QueryBinding
from chorus.ports.records import CompilerAuditProjection
from chorus.ports.retention import AuditRetention
from chorus.ports.scopes import CaseScope, NamespaceScope
from chorus.ports.storage import (
    PutItem,
    QueryRequest,
    SortKeyBeginsWith,
    StorageDriver,
    TableName,
)


@dataclass(slots=True)
class AuditRepository:
    """Append-only repository for the Audit table."""

    driver: StorageDriver
    cursors: SignedCursorCodec
    retention: AuditRetention

    def stage_append_case_event(self, scope: CaseScope, event: AuditEvent) -> PutItem:
        if event.namespace != scope.namespace or event.case_id != scope.case_id:
            raise CrossCaseViolationError("AUDIT_EVENT")
        if event.community_id != scope.community_id:
            raise CrossCaseViolationError("AUDIT_EVENT")
        return create_operation(
            codec_audit.case_event_key(scope, event),
            codec_audit.encode_case_event(scope, event, retention=self.retention),
        )

    def stage_append_compile_projection(
        self, scope: CaseScope, projection: CompilerAuditProjection
    ) -> PutItem:
        """Append the immutable private lineage of one compile, allowed or denied.

        Create-only, like every other write to this table, so a redelivered compile that got
        as far as the transaction cannot append a second record of one decision -- the
        condition refuses it and the idempotency record answers the caller instead.
        """

        if (
            projection.namespace != scope.namespace
            or projection.case_id != scope.case_id
            or projection.community_id != scope.community_id
        ):
            raise CrossCaseViolationError("COMPILER_AUDIT_PROJECTION")
        return create_operation(
            codec_audit.compile_projection_key(scope, projection.compile_id),
            codec_audit.encode_compile_projection(scope, projection, retention=self.retention),
        )

    async def load_compile_projection(
        self, scope: CaseScope, compile_id: UUID
    ) -> CompilerAuditProjection | None:
        """Strongly read one compile's private lineage for the authorized case surface."""

        key = codec_audit.compile_projection_key(scope, compile_id)
        item = await self.driver.get_item(key, consistent=True)
        if item is None:
            return None
        decoded, projection = codec_audit.decode_compile_projection(item)
        validate_scope(
            decoded,
            key=key,
            entity_ref="COMPILER_AUDIT_PROJECTION",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(projection.case_id, scope.case_id, "COMPILER_AUDIT_PROJECTION")
        require_same(projection.community_id, scope.community_id, "COMPILER_AUDIT_PROJECTION")
        return projection

    def stage_append_namespace_event(self, scope: NamespaceScope, event: AuditEvent) -> PutItem:
        """Append a namespace-level event, which owns no community and no case.

        The frozen audit mapping gives this partition exactly one purpose: "namespace events
        without case (reset/config)". There is no community partition in the audit table, so
        an event that names a community or a case has no shape here at all -- writing one
        would put case-owned history where a namespace-wide read returns it.
        """

        if event.namespace != scope.namespace:
            raise CrossCaseViolationError("AUDIT_EVENT")
        if event.case_id is not None or event.community_id is not None:
            raise CrossCaseViolationError("AUDIT_EVENT")
        return create_operation(
            codec_audit.namespace_event_key(scope, event),
            codec_audit.encode_namespace_event(scope, event, retention=self.retention),
        )

    async def _page(
        self,
        *,
        namespace_scope: NamespaceScope,
        binding: QueryBinding,
        partition_key: str,
        request: PageRequest,
        expected_case_scope: CaseScope | None,
    ) -> Page[AuditEvent]:
        start: str | None = None
        if request.cursor is not None:
            start = self.cursors.verify(
                request.cursor,
                namespace=namespace_scope.namespace,
                binding=binding,
                partition_key=partition_key,
            )
        result = await self.driver.query(
            QueryRequest(
                table=TableName.AUDIT,
                partition_key=partition_key,
                sort_key=SortKeyBeginsWith(keys.EVENT_SORT_KEY_PREFIX),
                consistent=False,
                limit=request.limit,
                exclusive_start_sort_key=start,
            )
        )
        events: list[AuditEvent] = []
        # A namespace page expects ``None`` rather than "unchecked": the partition holds only
        # events that own no community and no case, so a persisted row carrying either is a
        # corrupted address and fails the page instead of being surfaced as namespace history.
        community_id: CommunityId | None = None
        case_id: CaseId | None = None
        if expected_case_scope is not None:
            community_id = expected_case_scope.community_id
            case_id = expected_case_scope.case_id
        for item in result.items:
            decoded, event = codec_audit.decode_audit_event(item)
            validate_page_scope(
                decoded,
                EntityIdentity(
                    namespace=event.namespace,
                    community_id=event.community_id,
                    case_id=event.case_id,
                ),
                expected_key=(
                    codec_audit.case_event_key(expected_case_scope, event)
                    if expected_case_scope is not None
                    else codec_audit.namespace_event_key(namespace_scope, event)
                ),
                entity_ref="AUDIT_EVENT",
                namespace=namespace_scope.namespace,
                community_id=community_id,
                case_id=case_id,
            )
            events.append(event)
        next_cursor: PageCursor | None = None
        if result.last_evaluated_sort_key is not None:
            next_cursor = self.cursors.issue(
                namespace=namespace_scope.namespace,
                binding=binding,
                partition_key=partition_key,
                sort_key=result.last_evaluated_sort_key,
            )
        return Page(items=tuple(events), next_cursor=next_cursor)

    async def read_case_events(self, scope: CaseScope, request: PageRequest) -> Page[AuditEvent]:
        return await self._page(
            namespace_scope=scope.namespace_scope,
            binding=QueryBinding.AUDIT_CASE_EVENTS,
            partition_key=keys.case_partition(scope.namespace, scope.case_id),
            request=request,
            expected_case_scope=scope,
        )

    async def read_namespace_events(
        self, scope: NamespaceScope, request: PageRequest
    ) -> Page[AuditEvent]:
        return await self._page(
            namespace_scope=scope,
            binding=QueryBinding.AUDIT_NAMESPACE_EVENTS,
            partition_key=keys.namespace_partition(scope.namespace),
            request=request,
            expected_case_scope=None,
        )
