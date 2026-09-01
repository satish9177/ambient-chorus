"""Persistence-boundary records that are not domain aggregates.

These records are the durable shapes named by the frozen persistence mapping: uniqueness
locks, current pointers, history locators, fences, agent invocation results, and the stored
external-safe view. They are immutable, closed, and built only from domain-safe primitives.

``StoredShareableView`` mirrors the frozen ``ShareableCaseView`` schema field for field. It is
restated here rather than imported because infrastructure and ports must not depend on the
privacy package; a parity test asserts the two field sets never drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from chorus.domain.entities import (
    ActionProposalStatus,
    DestinationKind,
    DisclosureScope,
    EvidenceStatus,
    FactType,
    MandateStatus,
    Purpose,
)
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CaseId,
    CommunityId,
    ContributorId,
    DestinationId,
    ExecutionId,
    ExportFactId,
    FactId,
    MandateId,
    MessageId,
    Namespace,
    SafeEvidenceRefId,
    Sha256Digest,
    ViewId,
)
from chorus.domain.mandates import CurrentMandatePointer
from chorus.domain.time import epoch_micros, epoch_seconds_ceiling, require_utc
from chorus.ports.idempotency import EntityRef


class AgentName(StrEnum):
    MONITOR = "MONITOR"
    INVESTIGATOR = "INVESTIGATOR"
    ACTION = "ACTION"


class AgentInvocationOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class TransformationKind(StrEnum):
    """Mirror of the compiler transformation kinds carried in a stored safe fact."""

    DIRECT = "DIRECT"
    ANONYMIZED = "ANONYMIZED"
    AGGREGATED = "AGGREGATED"
    GENERALIZED = "GENERALIZED"


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelUniquenessLock:
    """Conditional-create lock proving one adapter channel message is ingested once."""

    namespace: Namespace
    community_id: CommunityId
    adapter: str
    channel_message_id_sha256: Sha256Digest
    message_id: MessageId
    content_sha256: Sha256Digest
    created_at: datetime
    schema_version: str = "channel-uniqueness-lock/v1"

    def __post_init__(self) -> None:
        if self.adapter != "SYNTHETIC":
            raise ValueError("unsupported adapter")
        require_utc(self.created_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentInvocationResult:
    """Durable agent invocation record holding hashes and result refs, never content."""

    invocation_id: UUID
    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    agent_name: AgentName
    prompt_version: str
    input_hash: Sha256Digest
    output_hash: Sha256Digest | None
    outcome: AgentInvocationOutcome
    result_refs: tuple[EntityRef, ...]
    created_at: datetime
    schema_version: str = "agent-invocation-result/v1"

    def __post_init__(self) -> None:
        if not 1 <= len(self.prompt_version) <= 64:
            raise ValueError("prompt_version length is invalid")
        require_utc(self.created_at)
        if self.outcome is AgentInvocationOutcome.SUCCEEDED and self.output_hash is None:
            raise ValueError("a succeeded invocation must record an output hash")
        refs = tuple((ref.entity_type, ref.entity_id) for ref in self.result_refs)
        if len(set(refs)) != len(refs):
            raise ValueError("result references must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class SendFence:
    """Short-lived Core fence that orders mandate revocation against one send attempt."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    execution_id: ExecutionId
    action_id: ActionId
    approval_id: ApprovalId
    view_id: ViewId
    authorization_snapshot_hash: Sha256Digest
    acquired_at: datetime
    expires_at: datetime
    schema_version: str = "send-fence/v1"

    def __post_init__(self) -> None:
        require_utc(self.acquired_at)
        require_utc(self.expires_at)
        if self.expires_at <= self.acquired_at:
            raise ValueError("fence expiry must be after acquisition")

    @property
    def expires_at_micros(self) -> int:
        """Exact expiry used by every fence authorization condition.

        A fence is live while ``now < expires_at`` and expired from ``expires_at`` onwards.
        Comparing whole seconds would round the deadline down and let a takeover -- or a
        mandate revocation -- win up to a second before the fence actually expired, so the
        comparison is made in exact microseconds.
        """

        return epoch_micros(self.expires_at)

    @property
    def expires_at_epoch(self) -> int:
        """DynamoDB TTL cleanup field, deliberately separate from authorization.

        Table TTL removes abandoned fences long after they stop mattering. It is never
        consulted by a condition, so its second granularity cannot affect authorization.
        """

        return epoch_seconds_ceiling(self.expires_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredCurrentMandatePointer:
    """Current mandate pointer plus the persisted status and row version."""

    namespace: Namespace
    community_id: CommunityId
    pointer: CurrentMandatePointer
    status: MandateStatus
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "current-mandate-pointer/v1"

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("version must be positive")
        require_utc(self.created_at)
        require_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class FactMandateAssociation:
    """Immutable proof that one fact was authorized by one exact mandate version."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    fact_id: FactId
    mandate_id: MandateId
    mandate_version: int
    terms_hash: Sha256Digest
    contributor_id: ContributorId
    created_at: datetime
    schema_version: str = "fact-mandate-association/v1"

    def __post_init__(self) -> None:
        if self.mandate_version < 1:
            raise ValueError("mandate version must be positive")
        require_utc(self.created_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredSafeDestination:
    """Address-free destination metadata carried inside a stored safe view."""

    destination_id: DestinationId
    kind: DestinationKind
    registry_version: int
    routing_token: UUID
    display_label: str

    def __post_init__(self) -> None:
        if self.registry_version < 1:
            raise ValueError("destination registry version must be positive")
        if not 1 <= len(self.display_label) <= 120:
            raise ValueError("destination display label length is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredMandateVersionRef:
    mandate_id: UUID
    version: int
    terms_hash: Sha256Digest

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("mandate version must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredShareableFact:
    export_fact_id: ExportFactId
    fact_type: FactType
    safe_text: str
    effective_scope: DisclosureScope
    evidence_status: EvidenceStatus
    contributor_count: int
    transformation: TransformationKind
    transformation_rule_id: str
    safe_evidence_ref_ids: tuple[SafeEvidenceRefId, ...]
    content_hash: Sha256Digest

    def __post_init__(self) -> None:
        if not 1 <= len(self.safe_text) <= 500:
            raise ValueError("safe fact text length is invalid")
        if self.contributor_count < 1:
            raise ValueError("shareable fact requires a contributor")
        if self.effective_scope is DisclosureScope.INTERNAL_ONLY:
            raise ValueError("an internal-only fact can never be stored in the safe zone")
        if len(set(self.safe_evidence_ref_ids)) != len(self.safe_evidence_ref_ids):
            raise ValueError("safe evidence references must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredSafeEvidenceRef:
    safe_evidence_ref_id: SafeEvidenceRefId
    media_type: str
    export_handle_id: UUID
    sha256: Sha256Digest
    caption: str
    created_by_rule_id: str
    content_hash: Sha256Digest

    def __post_init__(self) -> None:
        if not 1 <= len(self.media_type) <= 120:
            raise ValueError("safe evidence media type length is invalid")
        if not 1 <= len(self.caption) <= 300:
            raise ValueError("safe evidence caption length is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredShareableView:
    """Immutable compiled view exactly as persisted in the shareable table."""

    schema_version: str
    view_id: ViewId
    case_id: CaseId
    community_public_label: str
    case_version: int
    policy_version: str
    compiler_version: str
    destination: StoredSafeDestination
    purpose: Purpose
    generated_at: datetime
    expires_at: datetime
    mandate_version_set: tuple[StoredMandateVersionRef, ...]
    authorization_snapshot_hash: Sha256Digest
    shareable_facts: tuple[StoredShareableFact, ...]
    safe_evidence_refs: tuple[StoredSafeEvidenceRef, ...]
    audit_refs: tuple[UUID, ...]
    view_hash: Sha256Digest

    def __post_init__(self) -> None:
        require_utc(self.generated_at)
        require_utc(self.expires_at)
        if self.expires_at <= self.generated_at:
            raise ValueError("view expiry must be after generation")
        if self.case_version < 1:
            raise ValueError("case version must be positive")
        if not self.shareable_facts:
            raise ValueError("a stored view always contains at least one safe fact")
        export_ids = tuple(fact.export_fact_id for fact in self.shareable_facts)
        if len(set(export_ids)) != len(export_ids):
            raise ValueError("export fact IDs must be unique")
        ref_ids = tuple(ref.safe_evidence_ref_id for ref in self.safe_evidence_refs)
        if len(set(ref_ids)) != len(ref_ids):
            raise ValueError("safe evidence reference IDs must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentViewPointer:
    """Strongly read pointer naming the only view that may authorize a new action."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    view_id: ViewId
    view_hash: Sha256Digest
    case_version: int
    expires_at: datetime
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "current-view-pointer/v1"

    def __post_init__(self) -> None:
        if self.case_version < 1 or self.version < 1:
            raise ValueError("versions must be positive")
        require_utc(self.expires_at)
        require_utc(self.created_at)
        require_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ViewHistoryLocator:
    """Immutable safe locator for a compiled view."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    view_id: ViewId
    view_hash: Sha256Digest
    case_version: int
    generated_at: datetime
    schema_version: str = "view-history-locator/v1"

    def __post_init__(self) -> None:
        if self.case_version < 1:
            raise ValueError("case version must be positive")
        require_utc(self.generated_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentActionPointer:
    """Strongly read pointer naming the current proposal bound to a case."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    proposal_hash: Sha256Digest
    view_id: ViewId
    view_hash: Sha256Digest
    case_version: int
    status: ActionProposalStatus
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "current-action-pointer/v1"

    def __post_init__(self) -> None:
        if self.case_version < 1 or self.version < 1:
            raise ValueError("versions must be positive")
        require_utc(self.created_at)
        require_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionHistoryLocator:
    """Immutable safe locator for an action proposal."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    proposal_hash: Sha256Digest
    created_at: datetime
    schema_version: str = "action-history-locator/v1"

    def __post_init__(self) -> None:
        require_utc(self.created_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class MandatePointerExpectation:
    """Exact current-mandate-pointer state a conditional replace requires."""

    row_version: int
    mandate_version: int

    def __post_init__(self) -> None:
        if self.row_version < 1 or self.mandate_version < 1:
            raise ValueError("expected pointer versions must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class ViewPointerExpectation:
    """Exact current-view-pointer state a conditional replace requires."""

    row_version: int
    view_hash: Sha256Digest

    def __post_init__(self) -> None:
        if self.row_version < 1:
            raise ValueError("expected pointer row version must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionPointerExpectation:
    """Exact current-action-pointer state a conditional replace requires."""

    row_version: int
    proposal_hash: Sha256Digest

    def __post_init__(self) -> None:
        if self.row_version < 1:
            raise ValueError("expected pointer row version must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageFeedEntry:
    """One ambient feed row returned in canonical time order."""

    message_id: MessageId
    sent_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.sent_at)
