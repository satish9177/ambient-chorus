"""Trust-aligned repository ports.

Three protocols mirror the three physical tables so a caller cannot accidentally reach across
a trust boundary: ``CoreRepositoryPort`` is private-zone only, ``ShareableRepositoryPort`` is
external-safe only, and ``AuditRepositoryPort`` is append-only.

Read intent is expressed by the method name, never by a caller-supplied boolean:

* ``load_*`` performs a strongly consistent read and is the only form permitted to inform an
  authorization or state-changing decision;
* ``read_*`` performs an eventually consistent read and is permitted only for display
  projections that the frozen access-pattern table marks eventual.

Mutations are staged as typed write operations and committed through a ``UnitOfWork`` so every
durable change is an explicit, bounded, atomic transaction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from chorus.domain.entities import (
    ActionExecution,
    ActionProposal,
    ApplicationOperation,
    Approval,
    AuditEvent,
    Commitment,
    Community,
    CommunityCase,
    CommunityMessage,
    Contributor,
    EvidenceItem,
    EvidenceRoot,
    InvestigationAssessment,
)
from chorus.domain.facts import Fact, Report
from chorus.domain.ids import (
    ApprovalId,
    CommitmentId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    ExecutionId,
    FactId,
    MandateId,
    OperationId,
    ReportId,
    Sha256Digest,
    ViewId,
)
from chorus.domain.mandates import DisclosureMandate
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyKey,
    IdempotencyOutcome,
    IdempotencyRecord,
)
from chorus.ports.pagination import Page, PageRequest
from chorus.ports.records import (
    ActionHistoryLocator,
    ActionPointerExpectation,
    AgentInvocationResult,
    ChannelUniquenessLock,
    CurrentActionPointer,
    CurrentViewPointer,
    FactMandateAssociation,
    MandatePointerExpectation,
    MessageFeedEntry,
    SendFence,
    StoredCurrentMandatePointer,
    StoredShareableView,
    ViewHistoryLocator,
    ViewPointerExpectation,
)
from chorus.ports.scopes import ActionScope, CaseScope, CommunityScope, NamespaceScope
from chorus.ports.storage import CheckItem, PutItem
from chorus.ports.unit_of_work import CommitProof


class CoreRepositoryPort(Protocol):
    """Private-zone persistence: community, messages, reports, facts, mandates, fences."""

    async def load_community(self, scope: NamespaceScope, community_id: CommunityId) -> Community:
        """Strongly read one community."""

    async def load_contributor(
        self, scope: CommunityScope, contributor_id: ContributorId
    ) -> Contributor:
        """Strongly read one contributor."""

    async def load_message(
        self, scope: CommunityScope, entry: MessageFeedEntry
    ) -> CommunityMessage:
        """Strongly read one ambient message by its time-ordered locator."""

    async def load_channel_lock(
        self, scope: CommunityScope, *, adapter: str, channel_message_id: str
    ) -> ChannelUniquenessLock | None:
        """Strongly read the uniqueness lock that proves a channel message was ingested."""

    async def load_operation(
        self, scope: NamespaceScope, operation_id: OperationId
    ) -> ApplicationOperation:
        """Strongly read one durable application operation by direct get."""

    async def load_evidence_root(
        self, scope: CommunityScope, root_sha256: Sha256Digest
    ) -> EvidenceRoot | None:
        """Strongly read the content-addressed evidence root for a community."""

    async def load_case(self, scope: CaseScope) -> CommunityCase:
        """Strongly read the case aggregate before any guarded mutation."""

    async def read_case_for_display(self, scope: CaseScope) -> CommunityCase:
        """Eventually read the case aggregate for a display projection only."""

    async def load_report(self, scope: CaseScope, report_id: ReportId) -> Report:
        """Strongly read one report."""

    async def load_facts(self, scope: CaseScope, fact_ids: tuple[FactId, ...]) -> tuple[Fact, ...]:
        """Strongly batch-read facts; a missing or foreign item fails the whole operation."""

    async def load_evidence_items(
        self, scope: CaseScope, evidence_ids: tuple[EvidenceItemId, ...]
    ) -> tuple[EvidenceItem, ...]:
        """Strongly batch-read evidence metadata with whole-operation validation."""

    async def load_mandate_version(
        self, scope: CaseScope, mandate_id: MandateId, version: int
    ) -> DisclosureMandate:
        """Strongly read one immutable mandate version."""

    async def load_current_mandate_pointer(
        self, scope: CaseScope, mandate_id: MandateId
    ) -> StoredCurrentMandatePointer:
        """Strongly read the pointer that decides which mandate version is current."""

    async def load_current_mandate_pointers(
        self, scope: CaseScope, request: PageRequest
    ) -> Page[StoredCurrentMandatePointer]:
        """Strongly page the case's current mandate pointers."""

    async def load_agent_invocation(
        self, scope: CaseScope, invocation_id: UUID
    ) -> AgentInvocationResult | None:
        """Strongly read a durable agent invocation record."""

    async def load_send_fence(self, scope: CaseScope) -> SendFence | None:
        """Strongly read the case send-authorization fence."""

    async def read_message_feed(
        self,
        scope: CommunityScope,
        *,
        start: datetime,
        end: datetime,
        request: PageRequest,
    ) -> Page[CommunityMessage]:
        """Eventually read the ambient feed in canonical time order."""

    async def read_case_facts(self, scope: CaseScope, request: PageRequest) -> Page[Fact]:
        """Eventually page case facts for display."""

    async def read_case_reports(self, scope: CaseScope, request: PageRequest) -> Page[Report]:
        """Eventually page case reports for display."""

    async def read_case_assessments(
        self, scope: CaseScope, request: PageRequest
    ) -> Page[InvestigationAssessment]:
        """Eventually page immutable investigation assessments for display."""

    def stage_create_community(self, scope: NamespaceScope, community: Community) -> PutItem: ...

    def stage_update_community(
        self, scope: NamespaceScope, community: Community, *, expected_version: int
    ) -> PutItem: ...

    def stage_create_contributor(
        self, scope: CommunityScope, contributor: Contributor
    ) -> PutItem: ...

    def stage_update_contributor(
        self, scope: CommunityScope, contributor: Contributor, *, expected_version: int
    ) -> PutItem: ...

    def stage_create_message(self, scope: CommunityScope, message: CommunityMessage) -> PutItem: ...

    def stage_update_message(
        self, scope: CommunityScope, message: CommunityMessage, *, expected_version: int
    ) -> PutItem: ...

    def stage_create_channel_lock(
        self, scope: CommunityScope, lock: ChannelUniquenessLock
    ) -> PutItem: ...

    def stage_create_operation(
        self, scope: NamespaceScope, operation: ApplicationOperation
    ) -> PutItem: ...

    def stage_update_operation(
        self, scope: NamespaceScope, operation: ApplicationOperation, *, expected_version: int
    ) -> PutItem: ...

    def stage_create_evidence_root(self, scope: CommunityScope, root: EvidenceRoot) -> PutItem: ...

    def stage_create_case(self, scope: CaseScope, case: CommunityCase) -> PutItem: ...

    def stage_update_case(
        self, scope: CaseScope, case: CommunityCase, *, expected_version: int
    ) -> PutItem: ...

    def stage_create_report(self, scope: CaseScope, report: Report) -> PutItem: ...

    def stage_update_report(
        self, scope: CaseScope, report: Report, *, expected_version: int
    ) -> PutItem: ...

    def stage_create_fact(self, scope: CaseScope, fact: Fact) -> PutItem: ...

    def stage_update_fact(
        self, scope: CaseScope, fact: Fact, *, expected_version: int
    ) -> PutItem: ...

    def stage_create_evidence_item(self, scope: CaseScope, item: EvidenceItem) -> PutItem: ...

    def stage_update_evidence_item(
        self, scope: CaseScope, item: EvidenceItem, *, expected_version: int
    ) -> PutItem: ...

    def stage_append_assessment(
        self, scope: CaseScope, assessment: InvestigationAssessment
    ) -> PutItem: ...

    def stage_append_mandate_version(
        self, scope: CaseScope, mandate: DisclosureMandate
    ) -> PutItem: ...

    def stage_replace_current_mandate_pointer(
        self,
        scope: CaseScope,
        pointer: StoredCurrentMandatePointer,
        *,
        expected: MandatePointerExpectation | None,
    ) -> PutItem: ...

    def stage_append_fact_mandate_association(
        self, scope: CaseScope, association: FactMandateAssociation
    ) -> PutItem: ...

    def stage_append_agent_invocation(
        self, scope: CaseScope, result: AgentInvocationResult
    ) -> PutItem: ...

    def stage_require_no_live_send_fence(self, scope: CaseScope, *, now: datetime) -> CheckItem:
        """Condition-check participant asserting no unexpired fence blocks this mutation."""

    async def acquire_send_fence(self, scope: CaseScope, fence: SendFence) -> SendFence:
        """Conditionally create the fence, or replay an identical live fence unchanged."""

    async def release_send_fence(self, scope: CaseScope, execution_id: ExecutionId) -> None:
        """Delete only a fence owned by this execution; a foreign fence is left untouched."""


class ShareableRepositoryPort(Protocol):
    """External-safe persistence: views, pointers, proposals, approvals, executions."""

    async def load_view(self, scope: CaseScope, view_id: ViewId) -> StoredShareableView:
        """Strongly read one immutable compiled view."""

    async def load_current_view_pointer(self, scope: CaseScope) -> CurrentViewPointer | None:
        """Strongly read the current view pointer."""

    async def read_view_history(
        self, scope: CaseScope, request: PageRequest
    ) -> Page[ViewHistoryLocator]:
        """Eventually page safe view locators for display."""

    async def load_proposal(self, scope: ActionScope) -> ActionProposal:
        """Strongly read one immutable action proposal."""

    async def load_approval(self, scope: ActionScope, approval_id: ApprovalId) -> Approval:
        """Strongly read one immutable approval decision."""

    async def load_execution(
        self, scope: ActionScope, execution_id: ExecutionId
    ) -> ActionExecution:
        """Strongly read the authoritative send-attempt state."""

    async def load_current_action_pointer(self, scope: CaseScope) -> CurrentActionPointer | None:
        """Strongly read the current action pointer."""

    async def read_action_history(
        self, scope: CaseScope, request: PageRequest
    ) -> Page[ActionHistoryLocator]:
        """Eventually page safe action locators for display."""

    async def load_commitment(self, scope: CaseScope, commitment_id: CommitmentId) -> Commitment:
        """Strongly read one commitment before a guarded mutation."""

    async def read_case_commitments(
        self, scope: CaseScope, request: PageRequest
    ) -> Page[Commitment]:
        """Eventually page case commitments for display."""

    async def assert_view_capacity(self, scope: CaseScope) -> None:
        """Fail closed when another view would exceed the frozen per-case bound."""

    async def assert_action_capacity(self, scope: CaseScope) -> None:
        """Fail closed when another action would exceed the frozen per-case bound."""

    async def assert_commitment_capacity(self, scope: CaseScope) -> None:
        """Fail closed when another commitment would exceed the frozen per-case bound."""

    def stage_append_view(self, scope: CaseScope, view: StoredShareableView) -> PutItem: ...

    def stage_replace_current_view_pointer(
        self,
        scope: CaseScope,
        pointer: CurrentViewPointer,
        *,
        expected: ViewPointerExpectation | None,
    ) -> PutItem: ...

    def stage_append_view_history_locator(
        self, scope: CaseScope, locator: ViewHistoryLocator
    ) -> PutItem: ...

    def stage_append_proposal(self, scope: ActionScope, proposal: ActionProposal) -> PutItem: ...

    def stage_append_approval(self, scope: ActionScope, approval: Approval) -> PutItem: ...

    def stage_consume_approval(
        self, scope: ActionScope, approval: Approval, *, expected_version: int
    ) -> PutItem: ...

    def stage_create_execution(self, scope: ActionScope, execution: ActionExecution) -> PutItem: ...

    def stage_update_execution(
        self, scope: ActionScope, execution: ActionExecution, *, expected_version: int
    ) -> PutItem: ...

    def stage_replace_current_action_pointer(
        self,
        scope: CaseScope,
        pointer: CurrentActionPointer,
        *,
        expected: ActionPointerExpectation | None,
    ) -> PutItem: ...

    def stage_append_action_history_locator(
        self, scope: CaseScope, locator: ActionHistoryLocator
    ) -> PutItem: ...

    def stage_create_commitment(self, scope: CaseScope, commitment: Commitment) -> PutItem: ...

    def stage_update_commitment(
        self, scope: CaseScope, commitment: Commitment, *, expected_version: int
    ) -> PutItem: ...


class AuditRepositoryPort(Protocol):
    """Append-only safe audit records."""

    def stage_append_case_event(self, scope: CaseScope, event: AuditEvent) -> PutItem: ...

    def stage_append_namespace_event(self, scope: NamespaceScope, event: AuditEvent) -> PutItem: ...

    async def read_case_events(self, scope: CaseScope, request: PageRequest) -> Page[AuditEvent]:
        """Eventually page case audit events in occurrence order."""

    async def read_namespace_events(
        self, scope: NamespaceScope, request: PageRequest
    ) -> Page[AuditEvent]:
        """Eventually page namespace audit events in occurrence order."""


class IdempotencyRepositoryPort(Protocol):
    """Durable command idempotency in the Core or Shareable table."""

    async def load(self, key: IdempotencyKey) -> IdempotencyRecord | None:
        """Strongly read the record bound to a command key."""

    async def begin(
        self,
        key: IdempotencyKey,
        *,
        request_hash: Sha256Digest,
        now: datetime,
    ) -> IdempotencyOutcome:
        """Claim the key or classify an existing record as replay, in-progress, or conflict."""

    def stage_create_completed(
        self,
        key: IdempotencyKey,
        *,
        request_hash: Sha256Digest,
        result_entity_refs: tuple[EntityRef, ...],
        response_status: int,
        now: datetime,
    ) -> PutItem:
        """Stage the create-only completed record that also proves the transaction committed."""

    def stage_complete(
        self,
        record: IdempotencyRecord,
        *,
        result_entity_refs: tuple[EntityRef, ...],
        response_status: int,
        now: datetime,
    ) -> PutItem:
        """Stage the guarded transition of an in-progress record to its final outcome."""

    def stage_fail_final(
        self, record: IdempotencyRecord, *, response_status: int, now: datetime
    ) -> PutItem:
        """Stage the guarded transition of an in-progress record to a terminal failure."""

    def commit_proof(self, key: IdempotencyKey, *, request_hash: Sha256Digest) -> CommitProof:
        """Return the proof item a plan writes so an unknown outcome can be resolved."""
