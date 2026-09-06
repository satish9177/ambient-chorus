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


class ActionTone(StrEnum):
    """The register a proposal asks the renderer to use (ADR-021 § 11).

    A closed set, promoted from the free ``str`` the proposal used to carry -- which had
    already drifted: a Phase-1 hash fixture constructed ``tone="PROFESSIONAL"``, a value
    outside the frozen set that nothing refused.

    The renderer consumes it only to select frozen template copy. It never reaches the model's
    own text and it grants nothing: no tone widens a scope, names an identity, or changes which
    facts may travel.
    """

    NEUTRAL = "NEUTRAL"
    COLLABORATIVE = "COLLABORATIVE"
    FIRM = "FIRM"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApproverAssurance(StrEnum):
    """How strongly the approver's identity is actually known (ADR-023 SS 4).

    One member, because there is one mechanism: a high-entropy shared access token validated
    against a Secrets Manager hash, after which ``X-Chorus-Demo-Actor`` selects a fixed
    persona. That is **single-presenter demo access control and not authentication of a
    person**. It does not identify who approved; it identifies that somebody holding the demo
    token asserted the approver persona.

    The enum has one member rather than a reserved second one, because an unreachable value is
    an invitation to write code that pretends the stronger case exists. Adding
    ``PRODUCTION_AUTHENTICATED`` requires the authentication ADR that R26 and T26 already say
    V1 does not have, and that ADR is what would decide what the value means.
    """

    DEMO_SHARED_TOKEN = "DEMO_SHARED_TOKEN"  # noqa: S105 - an assurance level, not a credential


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
    authorization_version: int
    """The monotonic epoch of case-owned disclosure authority (ADR-020).

    Deliberately a second counter rather than a second reading of ``version``, and deliberately
    required rather than defaulted. ``version`` is the optimistic-concurrency token and answers
    "has this row moved since I read it"; this answers "has what this case may disclose changed
    since the view was compiled". Before Phase 7 every command gave both questions the same
    answer, so one integer served -- and ``READY_FOR_ACTION -> ACTION_PROPOSED`` is the first
    write where they diverge. Recording that a proposal exists moves the row and changes no
    fact, status, mandate, or count, so a valid proposal must not stale the view that
    authorized it.

    A default of ``1`` would be a guessed-low epoch, which is a view that looks fresher than it
    is. Every construction states the value, and the persistence codec fails closed on a stored
    row that does not carry one.
    """
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
        _positive_version(self.authorization_version)
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


MAX_PROPOSAL_CLAIMS = 12
MAX_PROPOSAL_CAVEATS = 8
MAX_CITATIONS_PER_FIELD = 10
"""The frozen proposal bounds, restated once so the entity and the contract cannot drift."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionCaveat:
    """One qualifying statement with the citations that make it checkable (ADR-021 § 2).

    Structured rather than the bare string the proposal used to store, because the immutable
    artifact a human approves has to contain the caveat-to-fact proof the validator relied on.
    Without it, Phase-8 revalidation could not re-check the binding and the renderer could not
    put the caveat in the References block.

    ``caveat_id`` is model-local within one proposal and deliberately UUID-shaped, exactly as
    ``claim_id`` is: it names nothing outside its own proposal, survives no lookup, and grants
    nothing. The identifier-shape guard that refuses a UUID-shaped Monitor ``client_ref`` does
    not apply to either.

    There is no zero-citation caveat in V1. A caveat nobody can trace is the sentence structured
    claims exist to prevent.
    """

    caveat_id: UUID
    text: str
    export_fact_ids: tuple[UUID, ...]
    caveat_hash: Sha256Digest

    def __post_init__(self) -> None:
        _bounded(self.text, 1, 500, "caveat text")
        if not 1 <= len(self.export_fact_ids) <= MAX_CITATIONS_PER_FIELD:
            raise ValueError("caveat citations must contain 1 to 10 fact IDs")
        if tuple(sorted(self.export_fact_ids, key=str)) != self.export_fact_ids:
            raise ValueError("caveat citations must be sorted")
        _unique(self.export_fact_ids, "caveat citations")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionProposal:
    action_id: ActionId
    case_id: CaseId
    case_version: int
    """The Core OCC version observed at proposal time. **Provenance only** (ADR-020 § 4).

    It answers "which row revision was this artifact built beside" and nothing consults it for
    authorization. Reading it as a requirement that the Core row must never advance again is
    exactly the defect ADR-020 removed -- the proposal's own apply transaction advances it.
    """
    authorization_version: int
    """The disclosure-authority epoch this proposal is valid against. **This is the freshness
    comparison**, performed by the validator now and by the Phase-8 send fence later."""
    view_id: ViewId
    view_hash: Sha256Digest
    subject: str
    claims: tuple[ActionClaim, ...]
    requested_action: str
    requested_deadline: datetime | None
    request_fact_ids: tuple[UUID, ...]
    caveats: tuple[ActionCaveat, ...]
    tone: ActionTone
    agent_invocation_id: UUID
    prompt_version: str
    preview_hash: Sha256Digest
    """The digest of the exact deterministic preview a human is shown and approves.

    Not the execution's ``rendered_message_hash``, which the sender writes later over the bytes
    it prepared for one SES attempt. Two fields, two owners, two moments -- which is what makes
    "the sender sent what the human approved" a comparison rather than a tautology
    (ADR-022 § 2). ``proposal_hash`` covers this field, so an approval binding the proposal hash
    transitively binds the preview.
    """
    proposal_hash: Sha256Digest
    status: ActionProposalStatus
    created_at: datetime
    schema_version: str = "action-proposal/v2"

    def __post_init__(self) -> None:
        _positive_version(self.case_version)
        _positive_version(self.authorization_version)
        _bounded(self.subject, 1, 120, "subject")
        _bounded(self.requested_action, 1, 500, "requested_action")
        if not 1 <= len(self.claims) <= MAX_PROPOSAL_CLAIMS:
            raise ValueError("a proposal carries 1 to 12 claims")
        if len(self.caveats) > MAX_PROPOSAL_CAVEATS:
            raise ValueError("a proposal carries at most 8 caveats")
        _unique(tuple(claim.claim_id for claim in self.claims), "claim IDs")
        _unique(tuple(caveat.caveat_id for caveat in self.caveats), "caveat IDs")
        if not 1 <= len(self.request_fact_ids) <= MAX_CITATIONS_PER_FIELD:
            # Never zero. ADR-021 § 1 removes the factual-premise classifier by making every
            # model-authored substantive field citation-bound, and a request with no reason is
            # a request the recipient cannot evaluate.
            raise ValueError("request citations must contain 1 to 10 fact IDs")
        if tuple(sorted(self.request_fact_ids, key=str)) != self.request_fact_ids:
            raise ValueError("request citations must be sorted")
        _unique(self.request_fact_ids, "request_fact_ids")
        if self.requested_deadline is not None:
            require_utc(self.requested_deadline)
        require_utc(self.created_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class Approval:
    """One human decision about one immutable proposal, written once and never again.

    **Fully immutable** (ADR-023 SS 1). ``consumed_at`` does not exist: the execution reaching
    ``SENDING`` *is* the consumption, it is one-time by compare-and-swap on the execution's row
    version, and it is already durable in the state the sender must write anyway. Recording it
    a second time here bought nothing and cost three things -- a mutable field inside an
    authorization digest, a second place the same fact could disagree, and a write grant on the
    immutable proposal's own partition for whoever performed it (ADR-024 SS 1).

    Because nothing here ever moves, :func:`chorus.privacy.canonical.hash_approval` omits only
    ``{approval_hash, version, created_at, updated_at}`` -- row bookkeeping -- and recomputation
    is meaningful at any later instant. An integrity check that a legitimate write invalidated
    could prove nothing, and would be deleted by the second person who met it (T32).

    **What it binds, and what it deliberately does not.** ``case_id``, ``action_id``,
    ``execution_id``, ``proposal_hash``, ``view_hash``, and ``authorization_version`` are
    stored. ``view_id``, ``preview_hash``, ``template_version``, ``from_identity_id``, and the
    whole destination routing triple are bound **transitively** through ``proposal_hash``,
    which covers ``preview_hash``, which covers all of them. Copying a transitively bound value
    onto this record would create a second copy of a fact the digest already fixes, and two
    copies of one fact can disagree (ADR-023 SS 3). The recipient address, the ``Reply-To``
    address, and the SES configuration set are bound by nothing here, because no artifact a
    human or a model can see may contain them.

    ``execution_id`` is what makes "consumed once by one execution" a statement about two named
    rows rather than about a convention. ``request_key_hash`` replaces a raw caller-supplied
    key, for the reason every other command in this repository already hashes one.
    """

    approval_id: ApprovalId
    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    execution_id: ExecutionId
    proposal_hash: Sha256Digest
    view_hash: Sha256Digest
    authorization_version: int
    """The disclosure-authority epoch current when the human decided. **Provenance only.**

    Send-time authority is re-derived from live Core state by the fence, never read from here
    (ADR-025 SS 4). Storing it records what the human was told, not what the sender may rely on.
    """
    approver_id_hash: Sha256Digest
    approver_assurance: ApproverAssurance
    decision: ApprovalDecision
    approved_at: datetime
    expires_at: datetime
    approval_hash: Sha256Digest
    request_key_hash: Sha256Digest
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "approval/v2"

    def __post_init__(self) -> None:
        require_utc(self.approved_at)
        require_utc(self.expires_at)
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must be after decision")
        _positive_version(self.authorization_version)
        _positive_version(self.version)
        _timestamps(self.created_at, self.updated_at)


class FieldPresence(StrEnum):
    """Whether one ``ActionExecution`` field must, must not, or may be set in a given state."""

    REQUIRED = "REQUIRED"
    ABSENT = "ABSENT"
    OPTIONAL = "OPTIONAL"


_R = FieldPresence.REQUIRED
_A = FieldPresence.ABSENT
_O = FieldPresence.OPTIONAL

EXECUTION_FIELD_PRESENCE: dict[str, dict[ActionExecutionState, FieldPresence]] = {
    "approval_id": {
        ActionExecutionState.DRAFT: _A,
        ActionExecutionState.APPROVED: _R,
        ActionExecutionState.SENDING: _R,
        ActionExecutionState.SENT: _R,
        ActionExecutionState.FAILED: _O,
        ActionExecutionState.SEND_UNKNOWN: _R,
    },
    "idempotency_key": {
        ActionExecutionState.DRAFT: _A,
        ActionExecutionState.APPROVED: _R,
        ActionExecutionState.SENDING: _R,
        ActionExecutionState.SENT: _R,
        ActionExecutionState.FAILED: _O,
        ActionExecutionState.SEND_UNKNOWN: _R,
    },
    "claim_owner_hash": {
        # The per-attempt claim owner (ADR-025 SS 1). It is written in the same conditional
        # write that moves APPROVED -> SENDING, so a durable SENDING row answers not merely
        # "was this execution claimed" but "**which attempt** owns the claim" -- and only the
        # second question authorizes a sender.
        #
        # OPTIONAL at FAILED for the same reason ``started_at`` is: the pre-send failures
        # (a rendered-hash mismatch, an authorization denial, an expired fence) move a row to
        # FAILED from APPROVED, where no claim was ever taken.
        ActionExecutionState.DRAFT: _A,
        ActionExecutionState.APPROVED: _A,
        ActionExecutionState.SENDING: _R,
        ActionExecutionState.SENT: _R,
        ActionExecutionState.FAILED: _O,
        ActionExecutionState.SEND_UNKNOWN: _R,
    },
    "ses_request_token_hash": {
        ActionExecutionState.DRAFT: _A,
        ActionExecutionState.APPROVED: _A,
        ActionExecutionState.SENDING: _R,
        ActionExecutionState.SENT: _R,
        ActionExecutionState.FAILED: _O,
        ActionExecutionState.SEND_UNKNOWN: _R,
    },
    "rendered_message_hash": {
        ActionExecutionState.DRAFT: _A,
        ActionExecutionState.APPROVED: _A,
        ActionExecutionState.SENDING: _R,
        ActionExecutionState.SENT: _R,
        ActionExecutionState.FAILED: _O,
        ActionExecutionState.SEND_UNKNOWN: _R,
    },
    "started_at": {
        ActionExecutionState.DRAFT: _A,
        ActionExecutionState.APPROVED: _A,
        ActionExecutionState.SENDING: _R,
        ActionExecutionState.SENT: _R,
        ActionExecutionState.FAILED: _O,
        ActionExecutionState.SEND_UNKNOWN: _R,
    },
    "ses_message_id": {
        ActionExecutionState.DRAFT: _A,
        ActionExecutionState.APPROVED: _A,
        ActionExecutionState.SENDING: _A,
        ActionExecutionState.SENT: _R,
        ActionExecutionState.FAILED: _A,
        ActionExecutionState.SEND_UNKNOWN: _O,
    },
    "finished_at": {
        ActionExecutionState.DRAFT: _A,
        ActionExecutionState.APPROVED: _A,
        ActionExecutionState.SENDING: _A,
        ActionExecutionState.SENT: _R,
        ActionExecutionState.FAILED: _R,
        ActionExecutionState.SEND_UNKNOWN: _R,
    },
    "failure_code": {
        ActionExecutionState.DRAFT: _A,
        ActionExecutionState.APPROVED: _A,
        ActionExecutionState.SENDING: _A,
        ActionExecutionState.SENT: _A,
        ActionExecutionState.FAILED: _R,
        ActionExecutionState.SEND_UNKNOWN: _A,
    },
    "failure_detail_safe": {
        # ADR-025's closing amendment to the ADR-022 table: the one field the original left
        # ungoverned. OPTIONAL at FAILED, and ABSENT everywhere else -- a detail about a
        # failure has no meaning on a row that has not failed, and leaving a cell unwritten
        # would let a future state default into accepting one.
        #
        # ABSENT at SEND_UNKNOWN, which the first freeze made OPTIONAL. Presence is monotonic
        # and the architecture permits SEND_UNKNOWN -> SENT on positive evidence, where this
        # field is ABSENT -- so a quarantined row that had recorded a detail here could never
        # take the edge that resolves it. An OPTIONAL cell that only one value is reachable
        # from is not an option, it is a trap. The unknown reason is carried by the
        # ``action.send.unknown`` audit event, which is where ADR-025 SS 14 puts it.
        ActionExecutionState.DRAFT: _A,
        ActionExecutionState.APPROVED: _A,
        ActionExecutionState.SENDING: _A,
        ActionExecutionState.SENT: _A,
        ActionExecutionState.FAILED: _O,
        ActionExecutionState.SEND_UNKNOWN: _A,
    },
    "reconciled_at": {
        ActionExecutionState.DRAFT: _A,
        ActionExecutionState.APPROVED: _A,
        ActionExecutionState.SENDING: _A,
        ActionExecutionState.SENT: _O,
        ActionExecutionState.FAILED: _A,
        ActionExecutionState.SEND_UNKNOWN: _O,
    },
}
"""The normative ADR-022 § 1 presence table, expressed as data rather than as prose.

A table rather than nine nullable fields with an implicit rule, for the reason ADR-016 gave for
the handover pair: a rule nobody wrote down is a rule the next state added to the enum quietly
escapes. Adding a state adds a **column**, and a missing cell raises rather than defaulting.

The ``OPTIONAL`` cells under ``FAILED`` are the honest ones and the reason this is not a simple
ladder. ``DRAFT -> FAILED`` reaches a terminal state having never had an approval, and
``APPROVED -> FAILED`` with ``STALE_AUTHORIZATION`` reaches it having never rendered anything
or contacted SES. Requiring a rendered hash on a failure that happened before rendering would
force a fabricated digest onto the record of a message that was never built.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionExecution:
    """One send attempt, whose field presence is a function of its state.

    Four fields an earlier shape made non-optional cannot exist before a human has approved
    anything: there is no ``approval_id`` at ``DRAFT``, the send ``idempotency_key`` is defined
    *over* the approval, the ``rendered_message_hash`` belongs to the sender, and the SES
    request token is minted immediately before the SES call. A ``DRAFT`` was therefore a state
    two documents required and no model could express (ADR-022).

    **Presence is monotonic.** A field that has been set is never unset and never rewritten;
    :func:`chorus.domain.state.transition_action_execution` refuses a transition that would
    clear one.
    """

    execution_id: ExecutionId
    action_id: ActionId
    case_id: CaseId
    approval_id: ApprovalId | None
    proposal_hash: Sha256Digest
    view_hash: Sha256Digest
    idempotency_key: str | None
    state: ActionExecutionState
    claim_owner_hash: Sha256Digest | None
    rendered_message_hash: Sha256Digest | None
    ses_request_token_hash: Sha256Digest | None
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
    schema_version: str = "action-execution/v3"

    def __post_init__(self) -> None:
        if self.attempt_number != 1:
            raise ValueError("V1 permits exactly one send attempt")
        for instant in (self.started_at, self.finished_at, self.reconciled_at):
            if instant is not None:
                require_utc(instant)
        _positive_version(self.version)
        _timestamps(self.created_at, self.updated_at)
        self.require_state_presence()

    def require_state_presence(self) -> None:
        """Refuse a record whose set fields disagree with the frozen presence table."""

        for name, row in EXECUTION_FIELD_PRESENCE.items():
            presence = row[self.state]
            present = getattr(self, name) is not None
            if presence is FieldPresence.REQUIRED and not present:
                raise ValueError(f"{name} is required in state {self.state.value}")
            if presence is FieldPresence.ABSENT and present:
                raise ValueError(f"{name} cannot be set in state {self.state.value}")


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
