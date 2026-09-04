"""Semantic validation of one Investigator answer: about this input, or refused whole.

The rules are whole-output. Every test here builds a *mostly* valid answer and breaks one
thing, because an answer that got everything wrong would be refused for the wrong reason and
would prove nothing about the check under test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

import pytest

from chorus.application.services.investigation_validation import (
    validate_investigation_result,
)
from chorus.contracts.common import (
    AGENT_INPUT_SCHEMA_VERSION,
    AGENT_OUTPUT_SCHEMA_VERSION,
    INVESTIGATOR_PROMPT_VERSION,
    AgentInputEnvelope,
    AgentName,
    AgentResultEnvelope,
)
from chorus.contracts.investigation import (
    AlternativeExplanation,
    CitationSet,
    ContradictionDraft,
    DuplicateEvidenceGroup,
    EvidenceFindingDraft,
    InvestigationAssessmentDraft,
    InvestigationCase,
    InvestigationEvidence,
    InvestigationFact,
    InvestigationInput,
    InvestigationReport,
    LinkageDecision,
    ProposedCommitment,
    RecommendedCaseDisposition,
    SufficiencyDraft,
)
from chorus.contracts.monitor import IssueType, LocationAreaValue
from chorus.domain.entities import (
    CaseState,
    ContradictionMateriality,
    EvidenceStatus,
    FactType,
    SensitivityCategory,
)
from chorus.domain.facts import LocationAreaCode
from chorus.domain.ids import Namespace
from chorus.ports.agents import AgentContractViolationError, InvestigationRejection

NAMESPACE = Namespace("TEST_INVESTIGATION")
SEED = UUID("7d0c1b2a-3e4f-5a6b-8c9d-0e1f2a3b4c5d")
NOW = datetime(2030, 3, 1, 9, 0, 0, tzinfo=UTC)


def uuid(name: str) -> UUID:
    return uuid5(SEED, name)


CASE_ID = uuid("case")
FACT_A = uuid("fact:a")
FACT_B = uuid("fact:b")
REPORT_A = uuid("report:a")
REPORT_B = uuid("report:b")
EVIDENCE = uuid("evidence:photo")
ROOT = uuid("root:photo")


def payload(*, version: int = 3) -> InvestigationInput:
    return InvestigationInput(
        case=InvestigationCase(
            case_id=CASE_ID,
            version=version,
            title="Recurring elevator failure",
            issue_type=IssueType.ELEVATOR_FAILURE,
            current_state=CaseState.INVESTIGATING,
        ),
        reports=(
            InvestigationReport(
                report_id=REPORT_A,
                contributor_pseudonym_id="resident-a",
                summary="The lift stopped again.",
                source_message_ids=(uuid("message:a"),),
            ),
            InvestigationReport(
                report_id=REPORT_B,
                contributor_pseudonym_id="resident-b",
                summary="It was stuck between floors.",
                source_message_ids=(uuid("message:b"),),
            ),
        ),
        facts=(
            InvestigationFact(
                fact_id=FACT_A,
                report_id=REPORT_A,
                contributor_pseudonym_id="resident-a",
                typed_value=LocationAreaValue(
                    fact_type=FactType.LOCATION_AREA, area=LocationAreaCode.ELEVATOR_CAB
                ),
                sensitivity=SensitivityCategory.GENERAL,
                evidence_ids=(EVIDENCE,),
                current_status=EvidenceStatus.REPORTED,
            ),
            InvestigationFact(
                fact_id=FACT_B,
                report_id=REPORT_B,
                contributor_pseudonym_id="resident-b",
                typed_value=LocationAreaValue(
                    fact_type=FactType.LOCATION_AREA, area=LocationAreaCode.LOBBY
                ),
                sensitivity=SensitivityCategory.GENERAL,
                current_status=EvidenceStatus.REPORTED,
            ),
        ),
        evidence=(
            InvestigationEvidence(
                evidence_id=EVIDENCE,
                root_id=ROOT,
                submitted_by_pseudonym_id="resident-a",
                media_type="image/jpeg",
                sha256="sha256:" + "ab" * 32,
            ),
        ),
    )


def invocation(*, version: int = 3) -> AgentInputEnvelope[InvestigationInput]:
    return AgentInputEnvelope[InvestigationInput](
        schema_version=AGENT_INPUT_SCHEMA_VERSION,
        invocation_id=uuid("invocation"),
        namespace=NAMESPACE.value,
        agent_name=AgentName.INVESTIGATOR,
        case_id=CASE_ID,
        case_version=version,
        requested_at=NOW,
        policy_version="policy/v1",
        payload=payload(version=version),
    )


def answer(**overrides: object) -> InvestigationAssessmentDraft:
    base: dict[str, object] = dict(  # noqa: C408 - kwargs mirror the model signature
        case_id=CASE_ID,
        based_on_case_version=3,
        linkage_decision=LinkageDecision.SAME_ISSUE,
        alternative_explanations=(
            AlternativeExplanation(
                description="Maintenance could explain it.",
                citations=CitationSet(cited_report_ids=(REPORT_A,)),
            ),
        ),
        evidence_findings=(
            EvidenceFindingDraft(
                fact_id=FACT_A,
                proposed_status=EvidenceStatus.REPORTED,
                rationale="One person's account.",
            ),
        ),
        sufficiency=SufficiencyDraft(independent_source_count=2, is_corroborated=True),
        recommended_case_disposition=RecommendedCaseDisposition.CONTINUE_INVESTIGATION,
    )
    base.update(overrides)
    return InvestigationAssessmentDraft(**base)  # type: ignore[arg-type]


def result(
    draft: InvestigationAssessmentDraft,
    *,
    prompt_version: str = INVESTIGATOR_PROMPT_VERSION,
    invocation_id: UUID | None = None,
    namespace: str = NAMESPACE.value,
    case_version: int = 3,
) -> AgentResultEnvelope[InvestigationAssessmentDraft]:
    return AgentResultEnvelope[InvestigationAssessmentDraft](
        schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
        invocation_id=invocation_id or uuid("invocation"),
        namespace=namespace,
        agent_name=AgentName.INVESTIGATOR,
        case_id=CASE_ID,
        case_version=case_version,
        model_profile_arn_hash="sha256:" + "cd" * 32,
        prompt_version=prompt_version,
        started_at=NOW,
        completed_at=NOW,
        output=draft,
    )


def reasons(error: AgentContractViolationError) -> set[str]:
    return set(error.reason_codes)


def validate(draft: InvestigationAssessmentDraft, **kwargs: object) -> object:
    return validate_investigation_result(
        invocation=invocation(),
        result=result(draft, **kwargs),  # type: ignore[arg-type]
        namespace=NAMESPACE,
    )


# -- the answer belongs to this invocation -------------------------------------------------


def test_a_valid_answer_is_accepted_whole() -> None:
    validated = validate(answer())
    assert validated.linkage_decision is LinkageDecision.SAME_ISSUE  # type: ignore[attr-defined]


def test_a_foreign_invocation_identity_is_refused() -> None:
    with pytest.raises(AgentContractViolationError) as caught:
        validate(answer(), invocation_id=uuid4())
    assert InvestigationRejection.ENVELOPE_MISMATCH.value in reasons(caught.value)


def test_an_unreviewed_prompt_version_is_refused() -> None:
    """Case T. The request never named a version, so this compares against the reviewed one."""

    with pytest.raises(AgentContractViolationError) as caught:
        validate(answer(), prompt_version="investigator/v2")
    assert InvestigationRejection.PROMPT_VERSION_MISMATCH.value in reasons(caught.value)


def test_an_answer_about_another_case_is_refused() -> None:
    with pytest.raises(AgentContractViolationError) as caught:
        validate(answer(case_id=uuid4()))
    assert InvestigationRejection.CASE_MISMATCH.value in reasons(caught.value)


def test_an_answer_bound_to_another_case_version_is_refused() -> None:
    """Case S. An assessment of an older world must never be applied to the current one."""

    with pytest.raises(AgentContractViolationError) as caught:
        validate(answer(based_on_case_version=2))
    assert InvestigationRejection.CASE_VERSION_MISMATCH.value in reasons(caught.value)


# -- invented and foreign identifiers ------------------------------------------------------


def test_an_invented_fact_in_a_finding_rejects_the_whole_answer() -> None:
    """Cases C and F."""

    with pytest.raises(AgentContractViolationError) as caught:
        validate(
            answer(
                evidence_findings=(
                    EvidenceFindingDraft(
                        fact_id=uuid4(),
                        proposed_status=EvidenceStatus.UNKNOWN,
                        rationale="A fact nobody has.",
                    ),
                )
            )
        )
    assert InvestigationRejection.UNKNOWN_FACT_ID.value in reasons(caught.value)


def test_a_foreign_citation_in_an_alternative_rejects_the_whole_answer() -> None:
    """Case G. The input was one case, so 'not in the input' and 'not in this case' agree."""

    with pytest.raises(AgentContractViolationError) as caught:
        validate(
            answer(
                alternative_explanations=(
                    AlternativeExplanation(
                        description="Another case explains it.",
                        citations=CitationSet(cited_fact_ids=(uuid4(),)),
                    ),
                )
            )
        )
    assert InvestigationRejection.UNKNOWN_FACT_ID.value in reasons(caught.value)


def test_an_invented_evidence_citation_on_a_finding_is_refused() -> None:
    with pytest.raises(AgentContractViolationError) as caught:
        validate(
            answer(
                evidence_findings=(
                    EvidenceFindingDraft(
                        fact_id=FACT_A,
                        proposed_status=EvidenceStatus.REPORTED,
                        supporting_evidence_ids=(uuid4(),),
                        rationale="Supported by evidence nobody has.",
                    ),
                )
            )
        )
    assert InvestigationRejection.UNKNOWN_EVIDENCE_ID.value in reasons(caught.value)


def test_an_invented_root_in_a_duplicate_group_is_refused() -> None:
    with pytest.raises(AgentContractViolationError) as caught:
        validate(
            answer(
                duplicate_evidence_groups=(
                    DuplicateEvidenceGroup(
                        root_id=uuid4(),
                        evidence_ids=(EVIDENCE,),
                        reason="These look like the same photograph.",
                    ),
                )
            )
        )
    assert InvestigationRejection.UNKNOWN_ROOT_ID.value in reasons(caught.value)


def test_a_commitment_citing_evidence_from_another_case_is_refused() -> None:
    """Case AH."""

    with pytest.raises(AgentContractViolationError) as caught:
        validate(
            answer(
                proposed_commitments=(
                    ProposedCommitment(
                        source_evidence_id=uuid4(),
                        obligor="Property Management",
                        action_text="Repair the lift.",
                        due_at=NOW,
                        verification_method="A resident confirms the lift runs.",
                    ),
                )
            )
        )
    assert InvestigationRejection.COMMITMENT_CITATION_INVALID.value in reasons(caught.value)


# -- contradictions -------------------------------------------------------------------------


def test_a_contradiction_citing_a_fact_of_this_case_is_accepted() -> None:
    validated = validate(
        answer(
            contradictions=(
                ContradictionDraft(
                    statement_fact_ids=(FACT_A, FACT_B),
                    description="Two accounts of one morning.",
                    materiality=ContradictionMateriality.LOW,
                ),
            )
        )
    )
    assert validated.contradicted_fact_ids  # type: ignore[attr-defined]


def test_a_contradiction_citing_a_foreign_fact_rejects_the_whole_answer() -> None:
    """Case AJ's precondition: a fabricated contradiction must at least be about real facts."""

    with pytest.raises(AgentContractViolationError) as caught:
        validate(
            answer(
                contradictions=(
                    ContradictionDraft(
                        statement_fact_ids=(FACT_A, uuid4()),
                        description="A conflict with somebody else's case.",
                        materiality=ContradictionMateriality.HIGH,
                    ),
                )
            )
        )
    assert InvestigationRejection.UNKNOWN_FACT_ID.value in reasons(caught.value)


def test_the_model_count_and_disposition_are_recorded_and_never_used() -> None:
    """Cases I, J, and O in one answer: wrong count, wrong flag, ambitious disposition."""

    validated = validate(
        answer(
            sufficiency=SufficiencyDraft(independent_source_count=99, is_corroborated=True),
            recommended_case_disposition=RecommendedCaseDisposition.READY_FOR_ACTION,
        )
    )
    assert validated.model_independent_source_count == 99  # type: ignore[attr-defined]
    assert validated.recommended_disposition is RecommendedCaseDisposition.READY_FOR_ACTION  # type: ignore[attr-defined]
