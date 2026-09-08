"""``GET /v1/cases/{case_id}/investigation``: the private investigation surface.

[08-api-design.md § Private investigation
projection](../../../../docs/architecture/08-api-design.md) freezes this as a read over
``read_case_facts``, ``read_case_reports``, ``load_current_assessment``, and
``ReadCompileExplanation`` -- the same four methods and the same compile-explanation query the
case surface's ``privacy_counts`` section already uses. It creates nothing, and it is the private
half of ``PrivacyBoundaryCompare``.

``facts[].value_preview`` is the one genuinely private payload on this surface: the fact's own
free text, exactly as a presenter needs to point at it beside the compiled view that excluded it.
Nothing else on this projection carries the private value, and nothing on the safe case surface
carries this field at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from chorus.application.queries._compile_lookup import find_compile_id_for_view
from chorus.application.queries.compile_audit import CompileExplanation, ReadCompileExplanation
from chorus.domain.entities import CaseState, EvidenceStatus, FactType, SensitivityCategory
from chorus.domain.facts import (
    Contradiction,
    EvidenceDescription,
    Fact,
    FactStatus,
    HealthDetail,
    IdentityAttribute,
    IncidentOccurrence,
    LocationArea,
    ManagementStatement,
    Report,
    ReportStatus,
    ServiceImpact,
    UnitLocation,
)
from chorus.ports.pagination import PageRequest
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    ShareableRepositoryPort,
)
from chorus.ports.scopes import CaseScope


@dataclass(frozen=True, slots=True, kw_only=True)
class CaseHeaderView:
    case_id: UUID
    title: str
    state: CaseState
    version: int
    authorization_version: int
    corroboration_source_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportView:
    report_id: UUID
    contributor_id: UUID
    status: ReportStatus
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class FactView:
    fact_id: UUID
    fact_type: FactType
    sensitivity: SensitivityCategory
    value_preview: str
    evidence_status: EvidenceStatus
    status: FactStatus
    contributor_id: UUID
    evidence_ids: tuple[UUID, ...]
    source_message_ids: tuple[UUID, ...]
    version: int


@dataclass(frozen=True, slots=True, kw_only=True)
class FindingView:
    fact_id: UUID
    evidence_status: EvidenceStatus
    reason_code: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ContradictionView:
    statement_fact_ids: tuple[UUID, ...]
    description: str
    materiality: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AlternativeExplanationView:
    description: str
    cited_report_ids: tuple[UUID, ...]
    cited_fact_ids: tuple[UUID, ...]
    cited_evidence_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentView:
    assessment_id: UUID
    based_on_case_version: int
    linkage_decision: str
    independent_source_count: int
    is_corroborated: bool
    recommended_disposition: str
    assessment_hash: str
    created_at: datetime
    findings: tuple[FindingView, ...]
    contradictions: tuple[ContradictionView, ...]
    alternative_explanations: tuple[AlternativeExplanationView, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class InvestigationProjectionResponse:
    case: CaseHeaderView
    reports: tuple[ReportView, ...]
    facts: tuple[FactView, ...]
    assessment: AssessmentView | None
    compile: CompileExplanation | None


@dataclass(slots=True)
class ReadInvestigation:
    """Presenter-only read. Creates nothing; the private half of ``PrivacyBoundaryCompare``."""

    core: CoreRepositoryPort
    audit: AuditRepositoryPort
    shareable: ShareableRepositoryPort

    async def execute(self, scope: CaseScope) -> InvestigationProjectionResponse:
        case = await self.core.read_case_for_display(scope)
        reports = await self._all_reports(scope)
        facts = await self._all_facts(scope)
        assessment = None
        if case.assessment_id is not None:
            loaded = await self.core.load_current_assessment(scope, case.assessment_id)
            if loaded is not None:
                assessment = AssessmentView(
                    assessment_id=loaded.assessment_id.value,
                    based_on_case_version=loaded.based_on_case_version,
                    linkage_decision=loaded.linkage_decision,
                    independent_source_count=loaded.independent_source_count,
                    is_corroborated=loaded.is_corroborated,
                    recommended_disposition=loaded.recommended_disposition,
                    assessment_hash=loaded.assessment_hash.value,
                    created_at=loaded.created_at,
                    findings=tuple(
                        FindingView(
                            fact_id=finding.fact_id.value,
                            evidence_status=finding.evidence_status,
                            reason_code=finding.reason_code,
                        )
                        for finding in loaded.findings
                    ),
                    contradictions=tuple(
                        ContradictionView(
                            statement_fact_ids=tuple(f.value for f in c.statement_fact_ids),
                            description=c.description,
                            materiality=c.materiality.value,
                        )
                        for c in loaded.contradictions
                    ),
                    alternative_explanations=tuple(
                        AlternativeExplanationView(
                            description=alt.description,
                            cited_report_ids=tuple(r.value for r in alt.cited_report_ids),
                            cited_fact_ids=tuple(f.value for f in alt.cited_fact_ids),
                            cited_evidence_ids=tuple(e.value for e in alt.cited_evidence_ids),
                        )
                        for alt in loaded.alternative_explanations
                    ),
                )

        # The authoritative "current safe view" is the strongly read view pointer the compile
        # transaction writes -- never ``case.current_view_id``, which compilation does not
        # populate. Resolving the pointer, then loading that exact view, is what lets the
        # case-scoped audit projection lookup reject a sibling or stale compile: it is asked
        # only ever for the view id the pointer names.
        compile_explanation = None
        pointer = await self.shareable.load_current_view_pointer(scope)
        if pointer is not None:
            current_view = await self.shareable.load_view(scope, pointer.view_id)
            compile_id = await find_compile_id_for_view(
                self.audit, scope, current_view.view_id.value
            )
            if compile_id is not None:
                compile_explanation = await ReadCompileExplanation(audit=self.audit).read(
                    scope, compile_id
                )

        return InvestigationProjectionResponse(
            case=CaseHeaderView(
                case_id=case.case_id.value,
                title=case.title,
                state=case.state,
                version=case.version,
                authorization_version=case.authorization_version,
                corroboration_source_count=case.corroboration_source_count,
            ),
            reports=reports,
            facts=facts,
            assessment=assessment,
            compile=compile_explanation,
        )

    async def _all_reports(self, scope: CaseScope) -> tuple[ReportView, ...]:
        views: list[ReportView] = []
        request = PageRequest()
        while True:
            page = await self.core.read_case_reports(scope, request)
            views.extend(_report_view(item) for item in page.items)
            if page.next_cursor is None:
                return tuple(views)
            request = PageRequest(cursor=page.next_cursor)

    async def _all_facts(self, scope: CaseScope) -> tuple[FactView, ...]:
        views: list[FactView] = []
        request = PageRequest()
        while True:
            page = await self.core.read_case_facts(scope, request)
            views.extend(_fact_view(item) for item in page.items)
            if page.next_cursor is None:
                return tuple(views)
            request = PageRequest(cursor=page.next_cursor)


def _report_view(report: Report) -> ReportView:
    return ReportView(
        report_id=report.report_id.value,
        contributor_id=report.contributor_id.value,
        status=report.status,
        created_at=report.created_at,
    )


def _fact_view(fact: Fact) -> FactView:
    return FactView(
        fact_id=fact.fact_id.value,
        fact_type=fact.fact_type,
        sensitivity=fact.sensitivity,
        value_preview=_value_preview(fact),
        evidence_status=fact.evidence_status,
        status=fact.status,
        contributor_id=fact.contributor_id.value,
        evidence_ids=tuple(item.value for item in fact.evidence_ids),
        source_message_ids=tuple(item.value for item in fact.source_message_ids),
        version=fact.version,
    )


def _value_preview(fact: Fact) -> str:
    """The presenter's private-panel text: the fact's own free content, or its closed shape.

    This is deliberately the mirror image of
    :func:`chorus.application.services.mandate_terms.contributor_wording`: that function is
    careful to say only a safe category sentence because it can be read by anyone who can reach
    the mandate thread over that fact. This one is presenter-only and exists specifically to
    show the private text next to its absence from the compiled view -- so it reads the fact's
    own free-text field wherever it has one, and only falls back to a closed description when it
    does not.
    """

    value = fact.value
    if isinstance(value, IncidentOccurrence):
        mode = value.failure_mode.value.lower().replace("_", " ")
        return f"Elevator {mode} on {value.occurred_at.date().isoformat()}."
    if isinstance(value, ServiceImpact):
        return f"Impact: {value.impact_code.value}. {value.summary}"
    if isinstance(value, LocationArea):
        return f"Location: {value.area.value}."
    if isinstance(value, IdentityAttribute):
        return value.display_name
    if isinstance(value, UnitLocation):
        return value.unit_label
    if isinstance(value, HealthDetail):
        return f"{value.subject_relation.value}: {value.detail}"
    if isinstance(value, ManagementStatement):
        return f'"{value.statement}" -- {value.speaker_org}'
    if isinstance(value, Contradiction):
        return value.summary
    if isinstance(value, EvidenceDescription):
        return f"{value.media_kind.value}: {value.description}"
    return f"{fact.fact_type.value} (no preview available)"


__all__ = [
    "AlternativeExplanationView",
    "AssessmentView",
    "CaseHeaderView",
    "ContradictionView",
    "FactView",
    "FindingView",
    "InvestigationProjectionResponse",
    "ReadInvestigation",
    "ReportView",
]
