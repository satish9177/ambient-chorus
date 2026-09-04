"""Build the exact bounded payload the Investigator is authorized to reason over.

Projection is a privacy decision, not a serialization convenience, and the Investigator's is
the widest projection in the system: it is the one agent that legitimately sees private facts,
health details, unit labels, and extracted evidence text, because assessing an incident is
what it is for. That makes what is *absent* load-bearing.

Absent, always: contributor display names, email addresses, S3 object keys, presigned URLs,
raw message bodies, mandate records, disclosure scopes, destination metadata, and any case but
this one. Contributors appear as pseudonyms. Evidence appears as metadata plus permitted text,
never as bytes and never as a locator.

Two refusals rather than two trims
----------------------------------
A payload that cannot be built *correctly* is refused, never quietly shortened. An evidence
item whose extracted text exceeds the frozen bound is not truncated -- a claim cut in half is
a claim the skeptic would evaluate against text the case does not contain -- and a case whose
active facts exceed the frozen input bound is refused rather than sampled, because a sampled
case is a case the model was asked about without being shown.

Extracted text is included only from evidence whose malware scan is ``CLEAN``. That is not a
verification claim -- a clean scan says nothing about the world, and the allowed verification
source set stays empty -- it is a statement about which bytes storage accepted. Text decoded
from bytes that were never accepted has no business being handed to anything.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.application.services.investigation_values import (
    UnprojectableFactError,
    to_contract_value,
)
from chorus.contracts.investigation import (
    MAX_INVESTIGATION_EVIDENCE,
    MAX_INVESTIGATION_FACTS,
    MAX_INVESTIGATION_REPORTS,
    MAX_PRIOR_FINDINGS,
    InvestigationCase,
    InvestigationEvidence,
    InvestigationFact,
    InvestigationInput,
    InvestigationReport,
    PriorAssessment,
    PriorFinding,
)
from chorus.contracts.monitor import IssueType, MonitorFactValue
from chorus.domain.entities import (
    CommunityCase,
    EvidenceItem,
    InvestigationAssessment,
    MalwareScanStatus,
)
from chorus.domain.facts import Fact, FactStatus, Report, ReportStatus
from chorus.domain.ids import ContributorId, EvidenceItemId, FactId, ReportId

MAX_EXTRACTED_TEXT = 4_000
"""The frozen bound on one evidence item's extracted text, mirrored from the contract.

Restated here so the projection can refuse an oversized item with a typed application error
rather than letting Pydantic reject the whole payload with a report that quotes the private
text it rejected.
"""


class InvestigationProjectionError(ValueError):
    """The case could not be projected safely, so it is refused rather than trimmed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class InvestigationProjection:
    """The payload plus the lookups deterministic validation will need afterwards.

    The validator is handed the same projection the runtime was handed, so "the model cited
    something that was in its input" is checked against the input that actually existed rather
    than against a reconstruction of it -- which would drift the moment the case did.
    """

    payload: InvestigationInput
    projected_fact_ids: frozenset[FactId]
    projected_report_ids: frozenset[ReportId]
    projected_evidence_ids: frozenset[EvidenceItemId]


def _pseudonym(pseudonyms: dict[ContributorId, str], contributor_id: ContributorId) -> str:
    pseudonym = pseudonyms.get(contributor_id)
    if pseudonym is None:
        # An owner the application cannot name pseudonymously would have to be named some
        # other way, and every other way is either a private identity or a placeholder that
        # makes two people look like one.
        raise InvestigationProjectionError("contributor pseudonym is unavailable")
    return pseudonym


def _issue_type(case: CommunityCase) -> IssueType:
    try:
        return IssueType(case.issue_type.strip().upper())
    except ValueError as error:
        raise InvestigationProjectionError(
            "case issue type is outside the V1 vocabulary"
        ) from error


def _extracted_text(item: EvidenceItem) -> str | None:
    """Permitted extracted text, or nothing. Never a truncation of the former."""

    if item.extracted_text is None:
        return None
    if item.malware_scan_status is not MalwareScanStatus.CLEAN:
        return None
    revealed = item.extracted_text.reveal()
    if len(revealed) > MAX_EXTRACTED_TEXT:
        raise InvestigationProjectionError("extracted evidence text exceeds the frozen bound")
    return revealed


def project_investigation_input(
    *,
    case: CommunityCase,
    reports: tuple[Report, ...],
    facts: tuple[Fact, ...],
    evidence_items: tuple[EvidenceItem, ...],
    pseudonyms: dict[ContributorId, str],
    prior_assessment: InvestigationAssessment | None = None,
) -> InvestigationProjection:
    """Project one strongly loaded case into the bounded Investigator payload.

    Only ``ACTIVE`` reports and the ``ACTIVE`` facts belonging to them are shown. A duplicate
    or retracted report has no field in the contract that could say so, and presenting withdrawn
    material with no way to mark it withdrawn would invite the skeptic to weigh it. Facts under
    a non-active report are still *classified* deterministically -- the independence function
    already discounts them -- they are simply not put to the model, which means no finding, and
    no finding means the computed status stands unchanged.
    """

    active_reports = tuple(report for report in reports if report.status is ReportStatus.ACTIVE)
    if not active_reports:
        raise InvestigationProjectionError("a case under investigation has no active report")
    if len(active_reports) > MAX_INVESTIGATION_REPORTS:
        raise InvestigationProjectionError("case reports exceed the frozen investigation bound")
    report_ids = {report.report_id for report in active_reports}

    active_facts = tuple(
        fact for fact in facts if fact.status is FactStatus.ACTIVE and fact.report_id in report_ids
    )
    if len(active_facts) > MAX_INVESTIGATION_FACTS:
        raise InvestigationProjectionError("case facts exceed the frozen investigation bound")

    cited_evidence_ids = {evidence_id for fact in active_facts for evidence_id in fact.evidence_ids}
    items_by_id = {item.evidence_id: item for item in evidence_items}
    projected_items: list[EvidenceItem] = []
    for evidence_id in sorted(cited_evidence_ids, key=str):
        item = items_by_id.get(evidence_id)
        if item is None:
            # A fact citing evidence the application could not load is a lineage failure, and
            # answering about the fact anyway would be answering about a case nobody has seen
            # whole.
            raise InvestigationProjectionError("cited evidence is unavailable")
        projected_items.append(item)
    if len(projected_items) > MAX_INVESTIGATION_EVIDENCE:
        raise InvestigationProjectionError("case evidence exceeds the frozen investigation bound")

    payload = InvestigationInput(
        case=InvestigationCase(
            case_id=case.case_id.value,
            version=case.version,
            title=case.title,
            issue_type=_issue_type(case),
            current_state=case.state,
        ),
        reports=tuple(
            InvestigationReport(
                report_id=report.report_id.value,
                contributor_pseudonym_id=_pseudonym(pseudonyms, report.contributor_id),
                summary=report.private_summary.reveal(),
                occurred_at=report.occurred_at,
                source_message_ids=tuple(
                    message_id.value for message_id in report.source_message_ids
                ),
            )
            for report in active_reports
        ),
        facts=tuple(
            InvestigationFact(
                fact_id=fact.fact_id.value,
                report_id=fact.report_id.value,
                contributor_pseudonym_id=_pseudonym(pseudonyms, fact.contributor_id),
                typed_value=_fact_value(fact),
                sensitivity=fact.sensitivity,
                evidence_ids=tuple(evidence_id.value for evidence_id in fact.evidence_ids),
                current_status=fact.evidence_status,
            )
            for fact in active_facts
        ),
        evidence=tuple(
            InvestigationEvidence(
                evidence_id=item.evidence_id.value,
                root_id=item.root_id.value,
                submitted_by_pseudonym_id=_pseudonym(pseudonyms, item.submitted_by_contributor_id),
                media_type=item.media_type,
                sha256=item.sha256.value,
                derived_from_evidence_id=(
                    None
                    if item.derived_from_evidence_id is None
                    else item.derived_from_evidence_id.value
                ),
                extracted_text=_extracted_text(item),
                safe_machine_caption=None,
            )
            for item in projected_items
        ),
        prior_assessment=_prior(prior_assessment),
    )
    return InvestigationProjection(
        payload=payload,
        projected_fact_ids=frozenset(fact.fact_id for fact in active_facts),
        projected_report_ids=frozenset(report_ids),
        projected_evidence_ids=frozenset(item.evidence_id for item in projected_items),
    )


def _prior(assessment: InvestigationAssessment | None) -> PriorAssessment | None:
    """The previous conclusion, so this run is a revision rather than a restart.

    Findings only. The prior contradictions, alternatives, and disposition are deliberately
    withheld: repeating a model's own earlier free text back to it is how a first guess becomes
    a settled belief, and the statuses are the part deterministic code is going to recompute
    anyway.
    """

    if assessment is None:
        return None
    findings = assessment.findings[:MAX_PRIOR_FINDINGS]
    return PriorAssessment(
        assessment_id=assessment.assessment_id.value,
        based_on_case_version=assessment.based_on_case_version,
        findings=tuple(
            PriorFinding(fact_id=finding.fact_id.value, evidence_status=finding.evidence_status)
            for finding in findings
        ),
    )


def _fact_value(fact: Fact) -> MonitorFactValue:
    """Map one stored typed value onto its wire shape, refusing what has no shape."""

    try:
        return to_contract_value(fact)
    except UnprojectableFactError as error:
        raise InvestigationProjectionError("stored fact has no agent-contract shape") from error
