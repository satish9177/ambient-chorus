"""Investigator answers, from the cooperative one to the hostile ones.

Every builder starts from the *invocation*, so an answer cites what the model was actually
given rather than what a test happens to know. That matters for the adversarial cases too: an
answer is only interesting as an attack when the citations it invents sit beside citations that
are genuine, because an answer that got everything wrong would be refused for the wrong reason.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

from chorus.contracts.investigation import (
    AlternativeExplanation,
    CitationSet,
    ContradictionDraft,
    DuplicateEvidenceGroup,
    EvidenceFindingDraft,
    InvestigationAssessmentDraft,
    LinkageDecision,
    LinkageReason,
    ProposedCommitment,
    RecommendedCaseDisposition,
    SufficiencyDraft,
)
from chorus.domain.entities import ContradictionMateriality, EvidenceStatus
from chorus.ports.agents import InvestigationInvocation

type AnswerBuilder = Callable[[InvestigationInvocation], InvestigationAssessmentDraft]


def cooperative(
    *,
    linkage: LinkageDecision = LinkageDecision.SAME_ISSUE,
    proposed: dict[UUID, EvidenceStatus] | None = None,
    contradictions: tuple[ContradictionDraft, ...] = (),
    duplicate_groups: tuple[DuplicateEvidenceGroup, ...] = (),
    commitments: tuple[ProposedCommitment, ...] = (),
    disposition: RecommendedCaseDisposition = RecommendedCaseDisposition.READY_FOR_ACTION,
    model_source_count: int | None = None,
    restate_current_status: bool = False,
) -> AnswerBuilder:
    """A well-formed answer that cites only what it was given.

    ``proposed`` names the facts this answer has an opinion about, and by default it is the
    *only* thing the answer has findings for. Emitting a finding for every fact at its stored
    ``current_status`` would look neutral and is not: whenever the recomputation raises a fact
    from ``REPORTED`` to ``CORROBORATED``, restating the stored value is a **downgrade** the
    ladder honours. ``restate_current_status`` turns that on deliberately, for the tests that
    are about exactly that.
    """

    def build(invocation: InvestigationInvocation) -> InvestigationAssessmentDraft:
        payload = invocation.payload
        overrides = proposed or {}
        facts = (
            payload.facts
            if restate_current_status
            else tuple(fact for fact in payload.facts if fact.fact_id in overrides)
        )
        citations = CitationSet(
            cited_report_ids=tuple(report.report_id for report in payload.reports[:10]),
        )
        reporters = {report.contributor_pseudonym_id for report in payload.reports}
        count = len(reporters) if model_source_count is None else model_source_count
        return InvestigationAssessmentDraft(
            case_id=payload.case.case_id,
            based_on_case_version=payload.case.version,
            linkage_decision=linkage,
            linkage_reasons=(
                LinkageReason(
                    reason="Every report describes the same lift failing.", citations=citations
                ),
            ),
            alternative_explanations=(
                AlternativeExplanation(
                    description="A scheduled maintenance window could explain some of these.",
                    citations=citations,
                ),
            ),
            evidence_findings=tuple(
                EvidenceFindingDraft(
                    fact_id=fact.fact_id,
                    proposed_status=overrides.get(fact.fact_id, fact.current_status),
                    supporting_evidence_ids=fact.evidence_ids,
                    rationale="Assessed against the reports and evidence in this case.",
                )
                for fact in facts
            ),
            contradictions=contradictions,
            duplicate_evidence_groups=duplicate_groups,
            proposed_commitments=commitments,
            sufficiency=SufficiencyDraft(
                independent_source_count=count,
                is_corroborated=count >= payload.corroboration_min,
                gaps=(),
            ),
            recommended_case_disposition=disposition,
        )

    return build


def contradiction_over(
    fact_ids: tuple[UUID, ...],
    *,
    materiality: ContradictionMateriality,
    description: str = "Two accounts of the same morning cannot both be right.",
) -> ContradictionDraft:
    return ContradictionDraft(
        statement_fact_ids=fact_ids,
        description=description,
        materiality=materiality,
    )


def with_foreign_fact_citation() -> AnswerBuilder:
    """An answer whose alternative explanation cites a fact from no case at all."""

    base = cooperative()

    def build(invocation: InvestigationInvocation) -> InvestigationAssessmentDraft:
        answer = base(invocation)
        return answer.model_copy(
            update={
                "alternative_explanations": (
                    AlternativeExplanation(
                        description="A fact this model was never shown explains everything.",
                        citations=CitationSet(cited_fact_ids=(uuid4(),)),
                    ),
                )
            }
        )

    return build


def with_invented_finding() -> AnswerBuilder:
    """An answer with a finding about a fact identifier that was never in the input."""

    base = cooperative()

    def build(invocation: InvestigationInvocation) -> InvestigationAssessmentDraft:
        answer = base(invocation)
        return answer.model_copy(
            update={
                "evidence_findings": (
                    *answer.evidence_findings,
                    EvidenceFindingDraft(
                        fact_id=uuid4(),
                        proposed_status=EvidenceStatus.REPORTED,
                        rationale="A fact that does not exist.",
                    ),
                )
            }
        )

    return build
