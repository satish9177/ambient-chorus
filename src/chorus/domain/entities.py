"""Immutable domain entities and closed lifecycle enums."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    AssessmentId,
    CaseId,
    CommitmentId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    EvidenceRootId,
    ExecutionId,
    FactId,
    MessageId,
    Namespace,
    OperationId,
    ReportId,
    SensitiveStr,
    Sha256Digest,
    ViewId,
)
from chorus.domain.time import require_utc


class DisclosureScope(StrEnum):
    INTERNAL_ONLY = "INTERNAL_ONLY"
    AGGREGATE_ONLY = "AGGREGATE_ONLY"
    ANONYMOUS_CASE = "ANONYMOUS_CASE"
    NAMED_CASE = "NAMED_CASE"
    EXTERNAL_ACTION = "EXTERNAL_ACTION"


class EvidenceStatus(StrEnum):
    REPORTED = "REPORTED"
    CORROBORATED = "CORROBORATED"
    VERIFIED = "VERIFIED"
    CONTRADICTED = "CONTRADICTED"
    UNKNOWN = "UNKNOWN"


class CaseState(StrEnum):
    CANDIDATE = "CANDIDATE"
    AWAITING_MANDATES = "AWAITING_MANDATES"
    INVESTIGATING = "INVESTIGATING"
    READY_FOR_ACTION = "READY_FOR_ACTION"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    ACTIONED = "ACTIONED"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    CLOSED_UNRESOLVED = "CLOSED_UNRESOLVED"


class MandateStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REFUSED = "REFUSED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class ActionExecutionState(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SEND_UNKNOWN = "SEND_UNKNOWN"


class CommitmentStatus(StrEnum):
    PENDING = "PENDING"
    DUE = "DUE"
    FULFILLED = "FULFILLED"
    MISSED = "MISSED"
    CANCELLED = "CANCELLED"


class SensitivityCategory(StrEnum):
    GENERAL = "GENERAL"
    IDENTITY = "IDENTITY"
    CONTACT = "CONTACT"
    UNIT_LOCATION = "UNIT_LOCATION"
    HEALTH = "HEALTH"
    MINOR = "MINOR"
    PRIVATE_QUOTE = "PRIVATE_QUOTE"
    PRIVATE_EVIDENCE_URI = "PRIVATE_EVIDENCE_URI"


class FactType(StrEnum):
    INCIDENT_OCCURRENCE = "INCIDENT_OCCURRENCE"
    SERVICE_IMPACT = "SERVICE_IMPACT"
    LOCATION_AREA = "LOCATION_AREA"
    IDENTITY_ATTRIBUTE = "IDENTITY_ATTRIBUTE"
    UNIT_LOCATION = "UNIT_LOCATION"
    HEALTH_DETAIL = "HEALTH_DETAIL"
    MANAGEMENT_STATEMENT = "MANAGEMENT_STATEMENT"
    CONTRADICTION = "CONTRADICTION"
    COMMITMENT_TERM = "COMMITMENT_TERM"
    EVIDENCE_DESCRIPTION = "EVIDENCE_DESCRIPTION"


class Purpose(StrEnum):
    REQUEST_ELEVATOR_REPAIR_AND_RESPONSE = "REQUEST_ELEVATOR_REPAIR_AND_RESPONSE"


class DestinationKind(StrEnum):
    PROPERTY_MANAGER = "PROPERTY_MANAGER"


class CommunityStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ContributorStatus(StrEnum):
    ACTIVE = "ACTIVE"
    WITHDRAWN = "WITHDRAWN"


class MessageProcessingStatus(StrEnum):
    NEW = "NEW"
    PROCESSED = "PROCESSED"
    REJECTED = "REJECTED"


class DerivationKind(StrEnum):
    ORIGINAL = "ORIGINAL"
    FORWARDED = "FORWARDED"
    TRANSFORMED = "TRANSFORMED"


class MalwareScanStatus(StrEnum):
    PENDING = "PENDING"
    CLEAN = "CLEAN"
    REJECTED = "REJECTED"


class ExtractionStatus(StrEnum):
    NOT_NEEDED = "NOT_NEEDED"
    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class ActionProposalStatus(StrEnum):
    DRAFT = "DRAFT"
    INVALIDATED = "INVALIDATED"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ActorType(StrEnum):
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"
    AGENT = "AGENT"
    AWS_SERVICE = "AWS_SERVICE"


class AuditDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    NONE = "NONE"


class ApplicationOperationKind(StrEnum):
    MONITOR = "MONITOR"
    INVESTIGATE = "INVESTIGATE"
    PROPOSE_ACTION = "PROPOSE_ACTION"
    SEND_ACTION = "SEND_ACTION"
    DEMO_DUE = "DEMO_DUE"


class ApplicationOperationStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


def _bounded(value: str, minimum: int, maximum: int, field_name: str) -> None:
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"{field_name} length is invalid")


def _positive_version(version: int) -> None:
    if version < 1:
        raise ValueError("version must be positive")


def _unique(values: tuple[object, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must be unique")


def _timestamps(created_at: datetime, updated_at: datetime) -> None:
    require_utc(created_at)
    require_utc(updated_at)
    if updated_at < created_at:
        raise ValueError("updated_at precedes created_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class Community:
    community_id: CommunityId
    namespace: Namespace
    name: str
    timezone: str
    status: CommunityStatus
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "community/v1"

    def __post_init__(self) -> None:
        _bounded(self.name, 1, 120, "name")
        _bounded(self.timezone, 1, 64, "timezone")
        _positive_version(self.version)
        _timestamps(self.created_at, self.updated_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class Contributor:
    contributor_id: ContributorId
    community_id: CommunityId
    namespace: Namespace
    pseudonym: str
    display_name: SensitiveStr | None
    email: SensitiveStr | None
    status: ContributorStatus
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "contributor/v1"

    def __post_init__(self) -> None:
        _bounded(self.pseudonym, 1, 40, "pseudonym")
        _positive_version(self.version)
        _timestamps(self.created_at, self.updated_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class CommunityMessage:
    message_id: MessageId
    community_id: CommunityId
    namespace: Namespace
    channel_message_id: str
    contributor_id: ContributorId | None
    sent_at: datetime
    received_at: datetime
    raw_text: SensitiveStr = field(repr=False)
    attachment_ids: tuple[EvidenceItemId, ...]
    content_sha256: Sha256Digest
    ingestion_idempotency_key: str
    processing_status: MessageProcessingStatus
    version: int
    created_at: datetime
    updated_at: datetime
    adapter: str = "SYNTHETIC"
    schema_version: str = "community-message/v1"

    def __post_init__(self) -> None:
        _bounded(self.channel_message_id, 1, 160, "channel_message_id")
        _bounded(self.raw_text.reveal(), 1, 10_000, "raw_text")
        _unique(self.attachment_ids, "attachment_ids")
        require_utc(self.sent_at)
        require_utc(self.received_at)
        _positive_version(self.version)
        _timestamps(self.created_at, self.updated_at)
        if self.adapter != "SYNTHETIC":
            raise ValueError("unsupported adapter")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceRoot:
    root_id: EvidenceRootId
    community_id: CommunityId
    namespace: Namespace
    root_sha256: Sha256Digest
    media_type: str
    first_observed_at: datetime
    derivation_kind: DerivationKind
    parent_root_id: EvidenceRootId | None
    created_at: datetime
    updated_at: datetime
    version: int = 1
    schema_version: str = "evidence-root/v1"

    def __post_init__(self) -> None:
        _bounded(self.media_type, 1, 120, "media_type")
        require_utc(self.first_observed_at)
        _timestamps(self.created_at, self.updated_at)
        if self.version != 1:
            raise ValueError("evidence roots are immutable version 1")
        if self.derivation_kind is DerivationKind.ORIGINAL and self.parent_root_id is not None:
            raise ValueError("original evidence cannot have a parent root")
        if self.derivation_kind is not DerivationKind.ORIGINAL and self.parent_root_id is None:
            raise ValueError("derived evidence requires a parent root")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceItem:
    evidence_id: EvidenceItemId
    root_id: EvidenceRootId
    community_id: CommunityId
    case_id: CaseId
    namespace: Namespace
    submitted_by_contributor_id: ContributorId
    source_message_id: MessageId | None
    private_object_key: SensitiveStr = field(repr=False)
    media_type: str
    byte_length: int
    sha256: Sha256Digest
    captured_at: datetime | None
    uploaded_at: datetime
    derived_from_evidence_id: EvidenceItemId | None
    malware_scan_status: MalwareScanStatus
    extraction_status: ExtractionStatus
    extracted_text: SensitiveStr | None = field(repr=False)
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "evidence-item/v1"

    def __post_init__(self) -> None:
        _bounded(self.media_type, 1, 120, "media_type")
        if self.byte_length < 0:
            raise ValueError("byte_length cannot be negative")
        if self.captured_at is not None:
            require_utc(self.captured_at)
        require_utc(self.uploaded_at)
        _positive_version(self.version)
        _timestamps(self.created_at, self.updated_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class CommunityCase:
    case_id: CaseId
    community_id: CommunityId
    namespace: Namespace
    title: str
    issue_type: str
    state: CaseState
    report_ids: tuple[ReportId, ...]
    fact_ids: tuple[FactId, ...]
    assessment_id: AssessmentId | None
    current_view_id: ViewId | None
    current_action_id: ActionId | None
    corroboration_source_count: int
    state_reason_code: str
    version: int
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    schema_version: str = "community-case/v1"

    def __post_init__(self) -> None:
        _bounded(self.title, 1, 160, "title")
        _bounded(self.issue_type, 1, 80, "issue_type")
        _bounded(self.state_reason_code, 1, 80, "state_reason_code")
        _unique(self.report_ids, "report_ids")
        _unique(self.fact_ids, "fact_ids")
        if self.corroboration_source_count < 0:
            raise ValueError("corroboration_source_count cannot be negative")
        _positive_version(self.version)
        _timestamps(self.created_at, self.updated_at)
        if self.resolved_at is not None:
            require_utc(self.resolved_at)
        if self.closed_at is not None:
            require_utc(self.closed_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceFinding:
    fact_id: FactId
    evidence_status: EvidenceStatus
    reason_code: str


@dataclass(frozen=True, slots=True, kw_only=True)
class InvestigationAssessment:
    assessment_id: AssessmentId
    case_id: CaseId
    based_on_case_version: int
    agent_invocation_id: UUID
    linkage_decision: str
    findings: tuple[EvidenceFinding, ...]
    contradiction_fact_ids: tuple[FactId, ...]
    alternative_explanations: tuple[str, ...]
    independent_source_count: int
    is_corroborated: bool
    recommended_disposition: str
    assessment_hash: Sha256Digest
    created_at: datetime
    schema_version: str = "investigation-assessment/v1"

    def __post_init__(self) -> None:
        _positive_version(self.based_on_case_version)
        _unique(tuple(item.fact_id for item in self.findings), "finding fact IDs")
        _unique(self.contradiction_fact_ids, "contradiction_fact_ids")
        if self.independent_source_count < 0:
            raise ValueError("independent_source_count cannot be negative")
        if self.is_corroborated != (self.independent_source_count >= 2):
            raise ValueError("corroboration flag disagrees with independent source count")
        require_utc(self.created_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionClaim:
    claim_id: UUID
    text: str
    export_fact_ids: tuple[UUID, ...]
    claim_hash: Sha256Digest

    def __post_init__(self) -> None:
        _bounded(self.text, 1, 500, "claim text")
        if not 1 <= len(self.export_fact_ids) <= 10:
            raise ValueError("claim citations must contain 1 to 10 fact IDs")
        if tuple(sorted(self.export_fact_ids, key=str)) != self.export_fact_ids:
            raise ValueError("claim citations must be sorted")
        _unique(self.export_fact_ids, "claim citations")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionProposal:
    action_id: ActionId
    case_id: CaseId
    case_version: int
    view_id: ViewId
    view_hash: Sha256Digest
    subject: str
    claims: tuple[ActionClaim, ...]
    requested_action: str
    requested_deadline: datetime | None
    request_fact_ids: tuple[UUID, ...]
    caveats: tuple[str, ...]
    tone: str
    agent_invocation_id: UUID
    prompt_version: str
    proposal_hash: Sha256Digest
    status: ActionProposalStatus
    created_at: datetime
    schema_version: str = "action-proposal/v1"

    def __post_init__(self) -> None:
        _positive_version(self.case_version)
        _bounded(self.subject, 1, 200, "subject")
        _bounded(self.requested_action, 1, 500, "requested_action")
        if not self.claims:
            raise ValueError("proposal requires at least one claim")
        _unique(tuple(claim.claim_id for claim in self.claims), "claim IDs")
        _unique(self.request_fact_ids, "request_fact_ids")
        if self.requested_deadline is not None:
            require_utc(self.requested_deadline)
        require_utc(self.created_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class Approval:
    approval_id: ApprovalId
    action_id: ActionId
    case_id: CaseId
    proposal_hash: Sha256Digest
    view_hash: Sha256Digest
    approver_id: ContributorId
    decision: ApprovalDecision
    approved_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    approval_hash: Sha256Digest
    idempotency_key: str
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "approval/v1"

    def __post_init__(self) -> None:
        require_utc(self.approved_at)
        require_utc(self.expires_at)
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must be after decision")
        if self.consumed_at is not None:
            require_utc(self.consumed_at)
        _positive_version(self.version)
        _timestamps(self.created_at, self.updated_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionExecution:
    execution_id: ExecutionId
    action_id: ActionId
    case_id: CaseId
    approval_id: ApprovalId
    proposal_hash: Sha256Digest
    view_hash: Sha256Digest
    idempotency_key: str
    state: ActionExecutionState
    rendered_message_hash: Sha256Digest
    ses_request_token_hash: Sha256Digest
    ses_message_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    failure_code: str | None
    failure_detail_safe: str | None
    reconciled_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
    attempt_number: int = 1
    schema_version: str = "action-execution/v1"

    def __post_init__(self) -> None:
        if self.attempt_number != 1:
            raise ValueError("V1 permits exactly one send attempt")
        for instant in (self.started_at, self.finished_at, self.reconciled_at):
            if instant is not None:
                require_utc(instant)
        _positive_version(self.version)
        _timestamps(self.created_at, self.updated_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class Commitment:
    commitment_id: CommitmentId
    case_id: CaseId
    action_id: ActionId | None
    source_evidence_id: EvidenceItemId
    obligor: str
    action_text: str
    due_at: datetime
    verification_method: str
    status: CommitmentStatus
    scheduler_name: str
    schedule_generation: int
    due_event_id: UUID
    verified_by_contributor_id: ContributorId | None
    verification_evidence_id: EvidenceItemId | None
    outcome_note: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "commitment/v1"

    def __post_init__(self) -> None:
        _bounded(self.obligor, 1, 120, "obligor")
        _bounded(self.action_text, 1, 500, "action_text")
        _bounded(self.verification_method, 1, 300, "verification_method")
        require_utc(self.due_at)
        if self.schedule_generation < 1:
            raise ValueError("schedule_generation must be positive")
        _positive_version(self.version)
        _timestamps(self.created_at, self.updated_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEntityRef:
    entity_type: str
    entity_id: UUID
    version: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditDetails:
    """Closed safe detail shape: bounded counts and codes only."""

    count: int | None
    rule_id: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEvent:
    audit_event_id: UUID
    namespace: Namespace
    community_id: CommunityId | None
    case_id: CaseId | None
    actor_type: ActorType
    actor_id_hash: Sha256Digest
    event_type: str
    occurred_at: datetime
    correlation_id: UUID
    causation_id: UUID | None
    idempotency_key_hash: Sha256Digest | None
    entity_refs: tuple[AuditEntityRef, ...]
    decision: AuditDecision
    reason_codes: tuple[str, ...]
    safe_details: AuditDetails
    input_hash: Sha256Digest | None
    output_hash: Sha256Digest | None
    schema_version: str = "audit-event/v1"

    def __post_init__(self) -> None:
        require_utc(self.occurred_at)
        _unique(self.reason_codes, "reason_codes")


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationOperation:
    """Durable status for one asynchronous command, plus the handover it authorizes.

    ``monitor_invocation_id`` and ``monitor_locator_hash`` are the authoritative Monitor
    handover identity. They exist because a worker delivery is data on a queue and data on a
    queue can be wrong: without them the first delivery for an operation that had written
    nothing yet had nothing to disagree with, so *any* invocation identity and *any* subset of
    message locators would have been accepted on trust. They are written when the operation is
    created -- before the job is dispatched and before the first model call -- so the durable
    operation, not the delivery, is what says which invocation and which exact new-message set
    this run is authorized to use.

    They carry identifiers and a digest only, never a locator list and never message content.
    They are immutable for the operation's lifetime: every transition copies them forward, and
    nothing in the system rebinds an operation to a second invocation.
    """

    operation_id: OperationId
    kind: ApplicationOperationKind
    namespace: Namespace
    actor_id_hash: Sha256Digest
    case_id: CaseId | None
    request_hash: Sha256Digest
    status: ApplicationOperationStatus
    result_refs: tuple[UUID, ...]
    error_code: str | None
    expires_at_epoch: int
    version: int
    created_at: datetime
    updated_at: datetime
    monitor_invocation_id: UUID | None = None
    monitor_locator_hash: Sha256Digest | None = None
    schema_version: str = "application-operation/v1"

    def __post_init__(self) -> None:
        _unique(self.result_refs, "result_refs")
        if self.expires_at_epoch < 0:
            raise ValueError("expires_at_epoch cannot be negative")
        _positive_version(self.version)
        _timestamps(self.created_at, self.updated_at)
        bound = (self.monitor_invocation_id is None, self.monitor_locator_hash is None)
        if len(set(bound)) != 1:
            raise ValueError("a Monitor handover binds an invocation and a locator hash together")
        if self.kind is not ApplicationOperationKind.MONITOR and self.monitor_invocation_id:
            raise ValueError("only a MONITOR operation carries a Monitor handover identity")
