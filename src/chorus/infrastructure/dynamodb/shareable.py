"""Shareable-table repository: views, pointers, proposals, approvals, executions.

This repository never returns a private entity and never accepts one. Its reads follow the
same naming contract as the Core repository: ``load_*`` is strongly consistent because the
result decides whether an export may proceed; ``read_*`` is eventual and display-only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from chorus.domain.entities import (
    ActionExecution,
    ActionProposal,
    Approval,
    Commitment,
)
from chorus.domain.ids import ApprovalId, CommitmentId, ExecutionId, ViewId
from chorus.infrastructure.dynamodb import codec_share, keys
from chorus.infrastructure.dynamodb.codec import ATTR_VERSION, DecodedScope
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.guards import (
    EntityIdentity,
    create_operation,
    replace_operation,
    require_same,
    validate_page_scope,
    validate_scope,
)
from chorus.ports.errors import (
    ModelLimitExceededError,
    NotFoundError,
)
from chorus.ports.limits import (
    MAX_ACTIONS_PER_CASE,
    MAX_COMMITMENTS_PER_CASE,
    MAX_VIEWS_PER_CASE,
)
from chorus.ports.pagination import Page, PageCursor, PageRequest, QueryBinding
from chorus.ports.records import (
    ActionHistoryLocator,
    ActionPointerExpectation,
    CurrentActionPointer,
    CurrentViewPointer,
    StoredShareableView,
    ViewHistoryLocator,
    ViewPointerExpectation,
)
from chorus.ports.scopes import ActionScope, CaseScope
from chorus.ports.storage import (
    AllOf,
    AttributeEqualsNumber,
    AttributeEqualsString,
    ItemKey,
    KeyAbsent,
    PutItem,
    QueryRequest,
    SortKeyBeginsWith,
    StorageDriver,
    StoredItem,
    TableName,
)

ATTR_VIEW_HASH = "view_hash"
ATTR_PROPOSAL_HASH = "proposal_hash"
ATTR_APPROVAL_HASH = "approval_hash"


@dataclass(slots=True)
class ShareableRepository:
    """External-safe repository for the Shareable table."""

    driver: StorageDriver
    cursors: SignedCursorCodec

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

    async def load_view(self, scope: CaseScope, view_id: ViewId) -> StoredShareableView:
        key = codec_share.view_key(scope, view_id)
        loaded = await self._get(key, codec_share.decode_view, consistent=True)
        if loaded is None:
            raise NotFoundError("SHAREABLE_VIEW")
        decoded, view = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="SHAREABLE_VIEW",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(view.view_id, view_id, "SHAREABLE_VIEW")
        require_same(view.case_id, scope.case_id, "SHAREABLE_VIEW")
        return view

    async def load_current_view_pointer(self, scope: CaseScope) -> CurrentViewPointer | None:
        key = codec_share.view_pointer_key(scope)
        loaded = await self._get(key, codec_share.decode_view_pointer, consistent=True)
        if loaded is None:
            return None
        decoded, pointer = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="CURRENT_VIEW_POINTER",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(pointer.case_id, scope.case_id, "CURRENT_VIEW_POINTER")
        return pointer

    async def load_proposal(self, scope: ActionScope) -> ActionProposal:
        key = codec_share.proposal_key(scope)
        loaded = await self._get(key, codec_share.decode_proposal, consistent=True)
        if loaded is None:
            raise NotFoundError("ACTION_PROPOSAL")
        decoded, proposal = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="ACTION_PROPOSAL",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(proposal.action_id, scope.action_id, "ACTION_PROPOSAL")
        require_same(proposal.case_id, scope.case_id, "ACTION_PROPOSAL")
        return proposal

    async def load_approval(self, scope: ActionScope, approval_id: ApprovalId) -> Approval:
        key = codec_share.approval_key(scope, approval_id)
        loaded = await self._get(key, codec_share.decode_approval, consistent=True)
        if loaded is None:
            raise NotFoundError("APPROVAL")
        decoded, approval = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="APPROVAL",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(approval.approval_id, approval_id, "APPROVAL")
        require_same(approval.action_id, scope.action_id, "APPROVAL")
        require_same(approval.case_id, scope.case_id, "APPROVAL")
        return approval

    async def load_execution(
        self, scope: ActionScope, execution_id: ExecutionId
    ) -> ActionExecution:
        key = codec_share.execution_key(scope, execution_id)
        loaded = await self._get(key, codec_share.decode_execution, consistent=True)
        if loaded is None:
            raise NotFoundError("ACTION_EXECUTION")
        decoded, execution = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="ACTION_EXECUTION",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(execution.execution_id, execution_id, "ACTION_EXECUTION")
        require_same(execution.action_id, scope.action_id, "ACTION_EXECUTION")
        require_same(execution.case_id, scope.case_id, "ACTION_EXECUTION")
        return execution

    async def load_current_action_pointer(self, scope: CaseScope) -> CurrentActionPointer | None:
        key = codec_share.action_pointer_key(scope)
        loaded = await self._get(key, codec_share.decode_action_pointer, consistent=True)
        if loaded is None:
            return None
        decoded, pointer = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="CURRENT_ACTION_POINTER",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(pointer.case_id, scope.case_id, "CURRENT_ACTION_POINTER")
        return pointer

    async def load_commitment(self, scope: CaseScope, commitment_id: CommitmentId) -> Commitment:
        key = codec_share.commitment_key(scope, commitment_id)
        loaded = await self._get(key, codec_share.decode_commitment, consistent=True)
        if loaded is None:
            raise NotFoundError("COMMITMENT")
        decoded, commitment = loaded
        validate_scope(
            decoded,
            key=key,
            entity_ref="COMMITMENT",
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
        )
        require_same(commitment.commitment_id, commitment_id, "COMMITMENT")
        require_same(commitment.case_id, scope.case_id, "COMMITMENT")
        return commitment

    # -- paged reads -------------------------------------------------------------------

    async def _page[EntityT](
        self,
        *,
        scope: CaseScope,
        binding: QueryBinding,
        partition_key: str,
        sort_key_prefix: str,
        request: PageRequest,
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
                binding=binding,
                partition_key=partition_key,
            )
        result = await self.driver.query(
            QueryRequest(
                table=TableName.SHAREABLE,
                partition_key=partition_key,
                sort_key=SortKeyBeginsWith(sort_key_prefix),
                consistent=False,
                limit=request.limit,
                exclusive_start_sort_key=start,
            )
        )
        entities: list[EntityT] = []
        for item in result.items:
            decoded, entity = decode(item)
            validate_page_scope(
                decoded,
                identity(entity),
                expected_key=address(entity),
                entity_ref=entity_ref,
                namespace=scope.namespace,
                community_id=scope.community_id,
                case_id=scope.case_id,
            )
            entities.append(entity)
        next_cursor: PageCursor | None = None
        if result.last_evaluated_sort_key is not None:
            next_cursor = self.cursors.issue(
                namespace=scope.namespace,
                binding=binding,
                partition_key=partition_key,
                sort_key=result.last_evaluated_sort_key,
            )
        return Page(items=tuple(entities), next_cursor=next_cursor)

    async def read_view_history(
        self, scope: CaseScope, request: PageRequest
    ) -> Page[ViewHistoryLocator]:
        return await self._page(
            scope=scope,
            binding=QueryBinding.SHAREABLE_VIEW_HISTORY,
            partition_key=keys.view_current_partition(scope.namespace, scope.case_id),
            sort_key_prefix=keys.HISTORY_SORT_KEY_PREFIX,
            request=request,
            decode=codec_share.decode_view_history,
            identity=lambda locator: EntityIdentity(
                namespace=locator.namespace,
                community_id=locator.community_id,
                case_id=locator.case_id,
            ),
            address=lambda locator: codec_share.view_history_key(scope, locator),
            entity_ref="VIEW_HISTORY_LOCATOR",
        )

    async def read_action_history(
        self, scope: CaseScope, request: PageRequest
    ) -> Page[ActionHistoryLocator]:
        return await self._page(
            scope=scope,
            binding=QueryBinding.SHAREABLE_ACTION_HISTORY,
            partition_key=keys.action_current_partition(scope.namespace, scope.case_id),
            sort_key_prefix=keys.HISTORY_SORT_KEY_PREFIX,
            request=request,
            decode=codec_share.decode_action_history,
            identity=lambda locator: EntityIdentity(
                namespace=locator.namespace,
                community_id=locator.community_id,
                case_id=locator.case_id,
            ),
            address=lambda locator: codec_share.action_history_key(scope, locator),
            entity_ref="ACTION_HISTORY_LOCATOR",
        )

    async def read_case_commitments(
        self, scope: CaseScope, request: PageRequest
    ) -> Page[Commitment]:
        return await self._page(
            scope=scope,
            binding=QueryBinding.SHAREABLE_CASE_COMMITMENTS,
            partition_key=keys.case_partition(scope.namespace, scope.case_id),
            sort_key_prefix=keys.COMMITMENT_SORT_KEY_PREFIX,
            request=request,
            decode=codec_share.decode_commitment,
            # A commitment carries only its case; namespace and community are checked on the
            # stored envelope, which is all the entity itself claims.
            identity=lambda commitment: EntityIdentity(case_id=commitment.case_id),
            address=lambda commitment: codec_share.commitment_key(scope, commitment.commitment_id),
            entity_ref="COMMITMENT",
        )

    # -- capacity ----------------------------------------------------------------------

    async def _count_at_most(
        self, *, partition_key: str, sort_key_prefix: str, maximum: int
    ) -> int:
        result = await self.driver.query(
            QueryRequest(
                table=TableName.SHAREABLE,
                partition_key=partition_key,
                sort_key=SortKeyBeginsWith(sort_key_prefix),
                consistent=True,
                limit=maximum + 1,
            )
        )
        return len(result.items)

    async def assert_view_capacity(self, scope: CaseScope) -> None:
        count = await self._count_at_most(
            partition_key=keys.view_current_partition(scope.namespace, scope.case_id),
            sort_key_prefix=keys.HISTORY_SORT_KEY_PREFIX,
            maximum=MAX_VIEWS_PER_CASE,
        )
        if count >= MAX_VIEWS_PER_CASE:
            raise ModelLimitExceededError("CASE_VIEWS")

    async def assert_action_capacity(self, scope: CaseScope) -> None:
        count = await self._count_at_most(
            partition_key=keys.action_current_partition(scope.namespace, scope.case_id),
            sort_key_prefix=keys.HISTORY_SORT_KEY_PREFIX,
            maximum=MAX_ACTIONS_PER_CASE,
        )
        if count >= MAX_ACTIONS_PER_CASE:
            raise ModelLimitExceededError("CASE_ACTIONS")

    async def assert_commitment_capacity(self, scope: CaseScope) -> None:
        count = await self._count_at_most(
            partition_key=keys.case_partition(scope.namespace, scope.case_id),
            sort_key_prefix=keys.COMMITMENT_SORT_KEY_PREFIX,
            maximum=MAX_COMMITMENTS_PER_CASE,
        )
        if count >= MAX_COMMITMENTS_PER_CASE:
            raise ModelLimitExceededError("CASE_COMMITMENTS")

    # -- staged writes -----------------------------------------------------------------

    def stage_append_view(self, scope: CaseScope, view: StoredShareableView) -> PutItem:
        return create_operation(
            codec_share.view_key(scope, view.view_id), codec_share.encode_view(scope, view)
        )

    def stage_replace_current_view_pointer(
        self,
        scope: CaseScope,
        pointer: CurrentViewPointer,
        *,
        expected: ViewPointerExpectation | None,
    ) -> PutItem:
        key = codec_share.view_pointer_key(scope)
        item = codec_share.encode_view_pointer(scope, pointer)
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
                    AttributeEqualsString(name=ATTR_VIEW_HASH, value=expected.view_hash.value),
                )
            ),
        )

    def stage_append_view_history_locator(
        self, scope: CaseScope, locator: ViewHistoryLocator
    ) -> PutItem:
        return create_operation(
            codec_share.view_history_key(scope, locator),
            codec_share.encode_view_history(scope, locator),
        )

    def stage_append_proposal(self, scope: ActionScope, proposal: ActionProposal) -> PutItem:
        return create_operation(
            codec_share.proposal_key(scope), codec_share.encode_proposal(scope, proposal)
        )

    def stage_append_approval(self, scope: ActionScope, approval: Approval) -> PutItem:
        return create_operation(
            codec_share.approval_key(scope, approval.approval_id),
            codec_share.encode_approval(scope, approval),
        )

    def stage_consume_approval(
        self, scope: ActionScope, approval: Approval, *, expected: Approval
    ) -> PutItem:
        """Record the one-time consumption of an approval without rewriting the decision.

        An approval is an immutable human authorization; only ``consumed_at`` and its version
        projection may ever change. A whole-item put could silently carry a different
        proposal hash, view hash, decision, approver, or expiry alongside the consumption, so
        the caller supplies the record it loaded and every other field is compared against
        it. ``approval_hash`` is bound in the condition as well, but the field-by-field
        comparison is what makes the guarantee independent of whatever that hash covers.
        """

        if approval.consumed_at is None:
            raise ValueError("consuming an approval must record consumed_at")
        if expected.consumed_at is not None:
            raise ValueError("an approval is consumed exactly once")
        if approval.version != expected.version + 1:
            raise ValueError("consuming an approval must increment the version by one")
        rewound = replace(
            approval,
            consumed_at=expected.consumed_at,
            version=expected.version,
            updated_at=expected.updated_at,
        )
        if rewound != expected:
            raise ValueError("consuming an approval must not rewrite the decision")
        return PutItem(
            key=codec_share.approval_key(scope, approval.approval_id),
            item=codec_share.encode_approval(scope, approval),
            condition=AllOf(
                (
                    AttributeEqualsNumber(name=ATTR_VERSION, value=expected.version),
                    AttributeEqualsString(
                        name=ATTR_APPROVAL_HASH, value=expected.approval_hash.value
                    ),
                )
            ),
        )

    def stage_create_execution(self, scope: ActionScope, execution: ActionExecution) -> PutItem:
        return create_operation(
            codec_share.execution_key(scope, execution.execution_id),
            codec_share.encode_execution(scope, execution),
        )

    def stage_update_execution(
        self, scope: ActionScope, execution: ActionExecution, *, expected_version: int
    ) -> PutItem:
        return replace_operation(
            codec_share.execution_key(scope, execution.execution_id),
            codec_share.encode_execution(scope, execution),
            expected_version=expected_version,
            new_version=execution.version,
        )

    def stage_replace_current_action_pointer(
        self,
        scope: CaseScope,
        pointer: CurrentActionPointer,
        *,
        expected: ActionPointerExpectation | None,
    ) -> PutItem:
        key = codec_share.action_pointer_key(scope)
        item = codec_share.encode_action_pointer(scope, pointer)
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
                    AttributeEqualsString(
                        name=ATTR_PROPOSAL_HASH, value=expected.proposal_hash.value
                    ),
                )
            ),
        )

    def stage_append_action_history_locator(
        self, scope: CaseScope, locator: ActionHistoryLocator
    ) -> PutItem:
        return create_operation(
            codec_share.action_history_key(scope, locator),
            codec_share.encode_action_history(scope, locator),
        )

    def stage_create_commitment(self, scope: CaseScope, commitment: Commitment) -> PutItem:
        return create_operation(
            codec_share.commitment_key(scope, commitment.commitment_id),
            codec_share.encode_commitment(scope, commitment),
        )

    def stage_update_commitment(
        self, scope: CaseScope, commitment: Commitment, *, expected_version: int
    ) -> PutItem:
        return replace_operation(
            codec_share.commitment_key(scope, commitment.commitment_id),
            codec_share.encode_commitment(scope, commitment),
            expected_version=expected_version,
            new_version=commitment.version,
        )
