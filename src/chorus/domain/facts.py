"""Closed fact union, reports, and evidence-independence calculation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from chorus.domain.entities import (
    EvidenceItem,
    EvidenceRoot,
    EvidenceStatus,
    FactType,
    SensitivityCategory,
)
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    EvidenceRootId,
    FactId,
    MessageId,
    Namespace,
    ReportId,
    SensitiveStr,
)
from chorus.domain.time import require_utc


class FailureMode(StrEnum):
    STUCK = "STUCK"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"
    ERRATIC = "ERRATIC"
    UNKNOWN = "UNKNOWN"


class ImpactCode(StrEnum):
    DELAY = "DELAY"
    TRAPPED = "TRAPPED"
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    OTHER = "OTHER"


class LocationAreaCode(StrEnum):
    LOBBY = "LOBBY"
    ELEVATOR_CAB = "ELEVATOR_CAB"
    COMMON_AREA = "COMMON_AREA"
    BUILDING = "BUILDING"


class SubjectRelation(StrEnum):
    SELF = "SELF"
    FAMILY = "FAMILY"
    OTHER = "OTHER"


class EvidenceMediaKind(StrEnum):
    IMAGE = "IMAGE"
    EMAIL = "EMAIL"
    TEXT = "TEXT"


class ReportStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DUPLICATE = "DUPLICATE"
    RETRACTED = "RETRACTED"


class FactStatus(StrEnum):
    ACTIVE = "ACTIVE"
    WITHDRAWN = "WITHDRAWN"


def _bounded(value: str, maximum: int, field_name: str) -> None:
    if not 1 <= len(value) <= maximum:
        raise ValueError(f"{field_name} length is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class IncidentOccurrence:
    occurred_at: datetime
    failure_mode: FailureMode
    equipment: str = "ELEVATOR"

    def __post_init__(self) -> None:
        require_utc(self.occurred_at)
        if self.equipment != "ELEVATOR":
            raise ValueError("V1 supports elevator incidents only")


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceImpact:
    impact_code: ImpactCode
    summary: str = field(repr=False)

    def __post_init__(self) -> None:
        _bounded(self.summary, 500, "summary")


@dataclass(frozen=True, slots=True, kw_only=True)
class LocationArea:
    area: LocationAreaCode


@dataclass(frozen=True, slots=True, kw_only=True)
class IdentityAttribute:
    display_name: str = field(repr=False)

    def __post_init__(self) -> None:
        _bounded(self.display_name, 120, "display_name")


@dataclass(frozen=True, slots=True, kw_only=True)
class UnitLocation:
    unit_label: str = field(repr=False)

    def __post_init__(self) -> None:
        _bounded(self.unit_label, 40, "unit_label")


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthDetail:
    subject_relation: SubjectRelation
    detail: str = field(repr=False)

    def __post_init__(self) -> None:
        _bounded(self.detail, 500, "detail")


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagementStatement:
    statement: str = field(repr=False)
    speaker_org: str
    stated_at: datetime

    def __post_init__(self) -> None:
        _bounded(self.statement, 1_000, "statement")
        _bounded(self.speaker_org, 120, "speaker_org")
        require_utc(self.stated_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class Contradiction:
    statement_fact_ids: tuple[FactId, ...]
    summary: str = field(repr=False)

    def __post_init__(self) -> None:
        if not 2 <= len(self.statement_fact_ids) <= 10:
            raise ValueError("contradiction requires 2 to 10 cited facts")
        if len(set(self.statement_fact_ids)) != len(self.statement_fact_ids):
            raise ValueError("contradiction fact IDs must be unique")
        _bounded(self.summary, 500, "summary")


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitmentTerm:
    obligor: str
    action_text: str = field(repr=False)
    due_at: datetime
    verification_method: str

    def __post_init__(self) -> None:
        _bounded(self.obligor, 120, "obligor")
        _bounded(self.action_text, 500, "action_text")
        _bounded(self.verification_method, 300, "verification_method")
        require_utc(self.due_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceDescription:
    description: str = field(repr=False)
    media_kind: EvidenceMediaKind

    def __post_init__(self) -> None:
        _bounded(self.description, 500, "description")


type FactValue = (
    IncidentOccurrence
    | ServiceImpact
    | LocationArea
    | IdentityAttribute
    | UnitLocation
    | HealthDetail
    | ManagementStatement
    | Contradiction
    | CommitmentTerm
    | EvidenceDescription
)

FACT_VALUE_TYPES: dict[type[object], FactType] = {
    IncidentOccurrence: FactType.INCIDENT_OCCURRENCE,
    ServiceImpact: FactType.SERVICE_IMPACT,
    LocationArea: FactType.LOCATION_AREA,
    IdentityAttribute: FactType.IDENTITY_ATTRIBUTE,
    UnitLocation: FactType.UNIT_LOCATION,
    HealthDetail: FactType.HEALTH_DETAIL,
    ManagementStatement: FactType.MANAGEMENT_STATEMENT,
    Contradiction: FactType.CONTRADICTION,
    CommitmentTerm: FactType.COMMITMENT_TERM,
    EvidenceDescription: FactType.EVIDENCE_DESCRIPTION,
}

REQUIRED_SENSITIVITY: dict[FactType, SensitivityCategory] = {
    FactType.IDENTITY_ATTRIBUTE: SensitivityCategory.IDENTITY,
    FactType.UNIT_LOCATION: SensitivityCategory.UNIT_LOCATION,
    FactType.HEALTH_DETAIL: SensitivityCategory.HEALTH,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class Report:
    report_id: ReportId
    case_id: CaseId | None
    community_id: CommunityId
    contributor_id: ContributorId
    namespace: Namespace
    source_message_ids: tuple[MessageId, ...]
    issue_type: str
    private_summary: SensitiveStr = field(repr=False)
    occurred_at: datetime | None
    location_area: LocationAreaCode | None
    evidence_ids: tuple[EvidenceItemId, ...]
    status: ReportStatus
    duplicate_of_report_id: ReportId | None
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "report/v1"

    def __post_init__(self) -> None:
        if not self.source_message_ids:
            raise ValueError("report requires source messages")
        if len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("source_message_ids must be unique")
        _bounded(self.issue_type, 80, "issue_type")
        _bounded(self.private_summary.reveal(), 1_000, "private_summary")
        if self.occurred_at is not None:
            require_utc(self.occurred_at)
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids must be unique")
        if self.status is ReportStatus.DUPLICATE and self.duplicate_of_report_id is None:
            raise ValueError("duplicate reports require duplicate_of_report_id")
        if self.status is not ReportStatus.DUPLICATE and self.duplicate_of_report_id is not None:
            raise ValueError("only duplicate reports may point at an original")
        if self.version < 1:
            raise ValueError("version must be positive")
        require_utc(self.created_at)
        require_utc(self.updated_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class Fact:
    fact_id: FactId
    case_id: CaseId
    report_id: ReportId
    community_id: CommunityId
    contributor_id: ContributorId
    namespace: Namespace
    fact_type: FactType
    value: FactValue = field(repr=False)
    sensitivity: SensitivityCategory
    evidence_ids: tuple[EvidenceItemId, ...]
    evidence_status: EvidenceStatus
    source_message_ids: tuple[MessageId, ...]
    supersedes_fact_id: FactId | None
    status: FactStatus
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "fact/v1"

    def __post_init__(self) -> None:
        expected_type = FACT_VALUE_TYPES.get(type(self.value))
        if expected_type is not self.fact_type:
            raise ValueError("fact discriminator does not match value type")
        required_sensitivity = REQUIRED_SENSITIVITY.get(self.fact_type)
        if required_sensitivity is not None and self.sensitivity is not required_sensitivity:
            raise ValueError("fact sensitivity does not match protected fact type")
        if not self.source_message_ids:
            raise ValueError("fact requires source message lineage")
        if len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("source message IDs must be unique")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence IDs must be unique")
        if self.version < 1:
            raise ValueError("version must be positive")
        require_utc(self.created_at)
        require_utc(self.updated_at)


def collapse_evidence_root(
    root_id: EvidenceRootId, roots: tuple[EvidenceRoot, ...]
) -> EvidenceRootId:
    """Collapse a forwarded/transformed chain and reject missing roots or cycles."""

    by_id = {root.root_id: root for root in roots}
    if len(by_id) != len(roots):
        raise ValueError("evidence root IDs must be unique")
    starting_root = by_id.get(root_id)
    if starting_root is None:
        raise ValueError("evidence root is missing")
    expected_community = starting_root.community_id
    expected_namespace = starting_root.namespace
    current = root_id
    visited: set[EvidenceRootId] = set()
    while True:
        if current in visited:
            raise ValueError("evidence root cycle")
        visited.add(current)
        root = by_id.get(current)
        if root is None:
            raise ValueError("evidence root is missing")
        if root.community_id != expected_community or root.namespace != expected_namespace:
            raise ValueError("evidence root ancestry crosses community or namespace")
        if root.parent_root_id is None:
            return current
        current = root.parent_root_id


@dataclass(frozen=True, slots=True)
class ReporterSource:
    """One active reporter-origin source without attached evidence."""

    contributor_id: ContributorId


@dataclass(frozen=True, slots=True)
class EvidenceRootSource:
    """One earliest-known evidence origin shared across copies and forwards."""

    root_id: EvidenceRootId


type IndependentSource = ReporterSource | EvidenceRootSource


def _source_sort_key(source: IndependentSource) -> tuple[str, str]:
    if isinstance(source, ReporterSource):
        return ("reporter", str(source.contributor_id))
    return ("evidence-root", str(source.root_id))


@dataclass(frozen=True, slots=True, kw_only=True)
class IndependenceResult:
    """One independence calculation, with the working that produced its count.

    ``count`` is the authoritative value and the only thing an authorization decision reads.
    ``sources_by_contributor`` is the same calculation's intermediate grouping, exposed so the
    private investigation surface and the ``evidence.independence.computed`` event can show
    *why* a count is what it is -- which contributor contributed which origin, and where two
    contributors collapsed onto one forwarded root. It carries identifiers only.

    The mapping is deliberately not an input to anything. Recomputing a count from it would be
    a second implementation of the matching below, and two implementations of an authorization
    quantity is exactly one too many.
    """

    count: int
    sources_by_contributor: dict[ContributorId, tuple[IndependentSource, ...]]


def independent_sources(
    facts: tuple[Fact, ...],
    reports: tuple[Report, ...],
    evidence_items: tuple[EvidenceItem, ...],
    roots: tuple[EvidenceRoot, ...],
) -> IndependenceResult:
    """Compute distinct contributor/origin independence over exactly the facts supplied.

    Additive: :func:`independent_source_count` is a thin wrapper over ``count`` and neither the
    algorithm nor any existing result changes. The function is called with two different fact
    sets and the results mean two different things (ADR-015): the whole case's ``ACTIVE`` facts
    give the **case-level** count that drives readiness and compiler gate 17, while one exact
    canonical claim group gives that *fact's* corroboration. Neither may ever be substituted
    for the other.
    """

    reports_by_id = {report.report_id: report for report in reports}
    evidence_by_id = {item.evidence_id: item for item in evidence_items}
    roots_by_id = {root.root_id: root for root in roots}
    if len(reports_by_id) != len(reports):
        raise ValueError("report IDs must be unique")
    if len(evidence_by_id) != len(evidence_items):
        raise ValueError("evidence item IDs must be unique")
    if len(roots_by_id) != len(roots):
        raise ValueError("evidence root IDs must be unique")
    root_content_keys = {(root.community_id, root.root_sha256) for root in roots}
    if len(root_content_keys) != len(roots):
        raise ValueError("evidence roots must be content-address unique within a community")

    sources: dict[ContributorId, set[IndependentSource]] = {}
    for fact in facts:
        if fact.status is not FactStatus.ACTIVE:
            continue
        report = reports_by_id.get(fact.report_id)
        if (
            report is None
            or report.case_id != fact.case_id
            or report.community_id != fact.community_id
            or report.contributor_id != fact.contributor_id
            or report.namespace != fact.namespace
        ):
            raise ValueError("fact report lineage is invalid")

        evidence_sources: list[EvidenceRootSource] = []
        for evidence_id in fact.evidence_ids:
            item = evidence_by_id.get(evidence_id)
            if item is None:
                raise ValueError("fact evidence item is missing")
            if (
                item.case_id != fact.case_id
                or item.community_id != fact.community_id
                or item.namespace != fact.namespace
            ):
                raise ValueError("fact evidence crosses case, community, or namespace")
            root = roots_by_id.get(item.root_id)
            if (
                root is None
                or root.community_id != fact.community_id
                or root.namespace != fact.namespace
            ):
                raise ValueError("fact evidence root crosses community or namespace")
            evidence_sources.append(EvidenceRootSource(collapse_evidence_root(item.root_id, roots)))

        if report.status is ReportStatus.DUPLICATE:
            duplicate_of_report_id = report.duplicate_of_report_id
            if duplicate_of_report_id is None:
                raise ValueError("duplicate report origin is missing")
            original = reports_by_id.get(duplicate_of_report_id)
            if (
                original is None
                or original.case_id != report.case_id
                or original.community_id != report.community_id
                or original.namespace != report.namespace
            ):
                raise ValueError("duplicate report origin is invalid")
            continue
        if report.status is ReportStatus.RETRACTED:
            continue
        contributor_sources = sources.setdefault(fact.contributor_id, set())
        if not evidence_sources:
            contributor_sources.add(ReporterSource(fact.contributor_id))
            continue
        contributor_sources.update(evidence_sources)

    matched_source_to_contributor: dict[IndependentSource, ContributorId] = {}

    def assign(contributor: ContributorId, seen: set[IndependentSource]) -> bool:
        for source in sorted(sources[contributor], key=_source_sort_key):
            if source in seen:
                continue
            seen.add(source)
            current = matched_source_to_contributor.get(source)
            if current is None or assign(current, seen):
                matched_source_to_contributor[source] = contributor
                return True
        return False

    matched = 0
    for contributor in sorted(sources, key=str):
        if assign(contributor, set()):
            matched += 1
    return IndependenceResult(
        count=matched,
        sources_by_contributor={
            contributor: tuple(sorted(found, key=_source_sort_key))
            for contributor, found in sources.items()
        },
    )


def independent_source_count(
    facts: tuple[Fact, ...],
    reports: tuple[Report, ...],
    evidence_items: tuple[EvidenceItem, ...],
    roots: tuple[EvidenceRoot, ...],
) -> int:
    """Count active, non-duplicate reports with distinct contributors and origins."""

    return independent_sources(facts, reports, evidence_items, roots).count
