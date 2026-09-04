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


UNNAMED_ISSUE_TYPE = "OTHER"
"""The issue type that records the absence of a name rather than a problem.

Spelled here, in the domain, because a stored ``CommunityCase.issue_type`` is a plain string
and the rule below has to read a case row exactly as it reads a fresh proposal. The agent
contract's ``IssueType.OTHER`` is the wire spelling of this same value.
"""


def issue_type_names_a_subject(issue_type: str) -> bool:
    """Whether this vocabulary word identifies *what* went wrong, and may therefore group.

    This is the whole of the candidate-grouping discriminator, and it is deliberately the only
    one (ADR-012). A candidate case is a merge -- the creation guard needs two reports before a
    case exists at all -- so filing two reports under one case is a claim that they describe
    one incident. Deterministic code can only prove that claim from a closed signal the input
    already carries, and the issue type is the only closed signal that says anything about the
    problem. ``LocationAreaCode`` is a four-member *area kind*
    (``LOBBY``/``ELEVATOR_CAB``/``COMMON_AREA``/``BUILDING``), not a place identity, so it
    cannot separate an elevator fault from a water-pressure complaint that share a building;
    the proposed title and the similarity reasons are free text the model wrote itself, so
    agreeing with them proves only that the model was consistent.

    ``OTHER`` therefore does not group. Widening what intake may group is a *vocabulary*
    change -- add a named member to the issue vocabulary -- reviewed once, in the open, rather
    than inferred per answer from prose.

    The comparison is case- and whitespace-insensitive so that no spelling of the unnamed type
    can be the thing that grants grouping. The contract enum admits only the canonical form, so
    this cannot matter for an answer; it matters for a stored ``issue_type``, which is an
    ordinary string, and there the fail-closed reading is the one to take.
    """

    return issue_type.strip().upper() != UNNAMED_ISSUE_TYPE


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


class ContradictionMateriality(StrEnum):
    """How much a validated contradiction is allowed to cost.

    Advisory and **block-only** (ADR-015). ``MEDIUM`` and ``HIGH`` block readiness; ``LOW`` is
    nonfatal and leaves a downstream caveat obligation. No member grants anything: an accepted
    contradiction can lower a fact's status and stop a case becoming ready, and can never make
    a case ready, verify a fact, widen a scope, authorize an identity, or choose a destination.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ApplicationOperationKind(StrEnum):
    MONITOR = "MONITOR"
    INVESTIGATE = "INVESTIGATE"
    PROPOSE_ACTION = "PROPOSE_ACTION"
    SEND_ACTION = "SEND_ACTION"
    DEMO_DUE = "DEMO_DUE"


AGENT_INVOKING_OPERATION_KINDS: frozenset[ApplicationOperationKind] = frozenset(
    {
        ApplicationOperationKind.MONITOR,
        ApplicationOperationKind.INVESTIGATE,
        ApplicationOperationKind.PROPOSE_ACTION,
    }
)
"""The operation kinds that invoke an agent, and therefore carry a handover identity.

Generalized from the ``MONITOR``-only pair by ADR-016. ``SEND_ACTION`` and ``DEMO_DUE`` invoke
no agent, so they carry no handover at all -- and an operation of one of those kinds that
arrives holding one is refused at construction rather than quietly ignored.
"""


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
    """One fact's *resolved* status and the closed code that explains how it got there.

    ``evidence_status`` is never the model's proposal. It is the deterministic recomputation
    resolved against the downgrade-only ladder of ADR-015, so a finding row is a record of what
    application code decided rather than of what the Investigator asked for.
    """

    fact_id: FactId
    evidence_status: EvidenceStatus
    reason_code: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentContradiction:
    """One validated contradiction: which facts conflict, said how, and at what cost.

    Structured rather than flattened onto the assessment, because a single tuple of fact IDs
    loses which facts belong to which contradiction and loses the ``materiality`` the readiness
    guard reads. Both losses were in an earlier shape and both are corrected here.

    The citation bounds are the domain rule, not a schema convenience: fewer than two facts
    names no conflict, and an unbounded list would let one entry sweep a whole case into
    ``CONTRADICTED``.
    """

    statement_fact_ids: tuple[FactId, ...]
    description: str = field(repr=False)
    materiality: ContradictionMateriality

    def __post_init__(self) -> None:
        if not 2 <= len(self.statement_fact_ids) <= 10:
            raise ValueError("a contradiction cites 2 to 10 facts")
        _unique(self.statement_fact_ids, "statement_fact_ids")
        _bounded(self.description, 1, 500, "contradiction description")


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentAlternative:
    """One alternative explanation, with the citations that make it checkable."""

    description: str = field(repr=False)
    cited_report_ids: tuple[ReportId, ...]
    cited_fact_ids: tuple[FactId, ...]
    cited_evidence_ids: tuple[EvidenceItemId, ...]

    def __post_init__(self) -> None:
        _bounded(self.description, 1, 500, "alternative description")
        _unique(self.cited_report_ids, "cited_report_ids")
        _unique(self.cited_fact_ids, "cited_fact_ids")
        _unique(self.cited_evidence_ids, "cited_evidence_ids")


ASSESSMENT_SCHEMA_VERSION_V1 = "investigation-assessment/v1"
ASSESSMENT_SCHEMA_VERSION_V2 = "investigation-assessment/v2"
"""Writers emit v2; readers accept both. The v1 shape flattened contradictions and dropped
their materiality, so a v1 row cannot state that a contradiction was nonfatal -- which is why
the decoder reads one at its most conservative rather than guessing."""


@dataclass(frozen=True, slots=True, kw_only=True)
class InvestigationAssessment:
    """The validated, immutable record of one investigation.

    Immutable and append-only. ``CommunityCase.assessment_id`` is the current-assessment
    pointer and there is no second pointer item, so the pointer and the case version can never
    disagree about which assessment is current.

    ``independent_source_count`` is always the deterministically recomputed case-level value
    and never the number the agent returned; ``recommended_disposition`` is recorded advice and
    is never read by a transition guard.
    """

    assessment_id: AssessmentId
    case_id: CaseId
    based_on_case_version: int
    agent_invocation_id: UUID
    linkage_decision: str
    findings: tuple[EvidenceFinding, ...]
    contradictions: tuple[AssessmentContradiction, ...]
    alternative_explanations: tuple[AssessmentAlternative, ...]
    independent_source_count: int
    is_corroborated: bool
    recommended_disposition: str
    assessment_hash: Sha256Digest
    created_at: datetime
    schema_version: str = ASSESSMENT_SCHEMA_VERSION_V2

    def __post_init__(self) -> None:
        _positive_version(self.based_on_case_version)
        _unique(tuple(item.fact_id for item in self.findings), "finding fact IDs")
        if self.independent_source_count < 0:
            raise ValueError("independent_source_count cannot be negative")
        if self.is_corroborated != (self.independent_source_count >= 2):
            raise ValueError("corroboration flag disagrees with independent source count")
        require_utc(self.created_at)

    @property
    def contradicted_fact_ids(self) -> tuple[FactId, ...]:
        """Every fact a validated contradiction cites, sorted and deduplicated."""

        cited = {
            fact_id
            for contradiction in self.contradictions
            for fact_id in contradiction.statement_fact_ids
        }
        return tuple(sorted(cited, key=str))

    @property
    def blocking_contradiction(self) -> bool:
        """True when any validated contradiction is material enough to block readiness."""

        return any(
            contradiction.materiality
            in {ContradictionMateriality.MEDIUM, ContradictionMateriality.HIGH}
            for contradiction in self.contradictions
        )


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

    ``agent_invocation_id`` and ``agent_binding_hash`` are the agent handover identity. They
    exist because a worker delivery is data on a queue and data on a queue can be wrong:
    without them the first delivery for an operation that had written nothing yet had nothing
    to disagree with, so *any* invocation identity and *any* subset of the delivered work would
    have been accepted on trust. They are written when the operation is created -- before the
    job is dispatched and before the first model call -- so the durable operation, not the
    delivery, is what says which invocation and which exact work this run is authorized to do.

    The pair was originally ``MONITOR``-only. ADR-016 generalized it, because an unbound
    ``INVESTIGATE`` job could present a fresh invocation identity, find no durable invocation
    record, and spend a second model pass over the same private case.

    ``agent_binding_hash`` names the exact work per kind: the sorted locator digest for
    ``MONITOR``, the canonical digest of ``{case_id, expected_case_version, reason}`` for
    ``INVESTIGATE``, and of ``{case_id, view_id, view_hash}`` for ``PROPOSE_ACTION``. It
    carries identifiers and digests only, never a locator list, never message text, and never a
    view body. It is immutable for the operation's lifetime: every transition copies both
    forward, and nothing in the system rebinds an operation to a second invocation.
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
    agent_invocation_id: UUID | None = None
    agent_binding_hash: Sha256Digest | None = None
    schema_version: str = "application-operation/v2"

    def __post_init__(self) -> None:
        _unique(self.result_refs, "result_refs")
        if self.expires_at_epoch < 0:
            raise ValueError("expires_at_epoch cannot be negative")
        _positive_version(self.version)
        _timestamps(self.created_at, self.updated_at)
        bound = (self.agent_invocation_id is None, self.agent_binding_hash is None)
        if len(set(bound)) != 1:
            raise ValueError("an agent handover binds an invocation and a binding hash together")
        if self.kind not in AGENT_INVOKING_OPERATION_KINDS and self.agent_invocation_id:
            raise ValueError("only an agent-invoking operation carries a handover identity")
