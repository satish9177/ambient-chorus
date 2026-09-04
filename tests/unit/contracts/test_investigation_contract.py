"""The Investigator wire contract: closed, bounded, and unable to express an authority.

The most important assertions here are negative. There is no field in this schema through which
a model can name a case state, a case split, a disclosure scope, a purpose, a destination, an
identity permission, a mandate, or a fact it would like created -- and an agent cannot propose
what it has no field to propose in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from chorus.contracts.investigation import (
    CORROBORATION_MIN,
    MAX_INVESTIGATION_FACTS,
    AlternativeExplanation,
    CitationSet,
    ContradictionDraft,
    DuplicateEvidenceGroup,
    EvidenceFindingDraft,
    InvestigationAssessmentDraft,
    InvestigationCase,
    InvestigationFact,
    InvestigationInput,
    InvestigationReport,
    LinkageDecision,
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
from chorus.ports.limits import MAX_ACTIVE_FACTS_PER_CASE

NOW = datetime(2030, 6, 1, 9, 0, 0, tzinfo=UTC)


def case() -> InvestigationCase:
    return InvestigationCase(
        case_id=uuid4(),
        version=1,
        title="Recurring elevator failure",
        issue_type=IssueType.ELEVATOR_FAILURE,
        current_state=CaseState.INVESTIGATING,
    )


def report(report_id: UUID | None = None) -> InvestigationReport:
    return InvestigationReport(
        report_id=report_id or uuid4(),
        contributor_pseudonym_id="resident-a",
        summary="The lift stopped.",
        source_message_ids=(uuid4(),),
    )


def draft(**overrides: object) -> InvestigationAssessmentDraft:
    base: dict[str, object] = dict(  # noqa: C408 - kwargs mirror the model signature
        case_id=uuid4(),
        based_on_case_version=1,
        linkage_decision=LinkageDecision.SAME_ISSUE,
        sufficiency=SufficiencyDraft(independent_source_count=2, is_corroborated=True),
        recommended_case_disposition=RecommendedCaseDisposition.CONTINUE_INVESTIGATION,
    )
    base.update(overrides)
    return InvestigationAssessmentDraft(**base)  # type: ignore[arg-type]


# -- what the schema cannot express -----------------------------------------------------------


def test_the_answer_has_no_field_for_a_state_scope_destination_or_identity() -> None:
    fields = set(InvestigationAssessmentDraft.model_fields)
    forbidden = {
        "case_state",
        "next_state",
        "transition",
        "disclosure_scope",
        "max_scope",
        "purpose",
        "destination",
        "destination_id",
        "identity_grant",
        "mandate_id",
        "proposed_facts",
        "new_facts",
        "split_case_ids",
    }
    assert fields & forbidden == set()


def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        draft(grant_external_action=True)


def test_the_input_has_no_field_for_a_mandate_or_a_destination() -> None:
    fields = set(InvestigationInput.model_fields)
    assert "mandates" not in fields
    assert "destination" not in fields
    assert "policy" not in fields


def test_evidence_carries_no_uri_and_no_object_key() -> None:
    from chorus.contracts.investigation import InvestigationEvidence

    fields = set(InvestigationEvidence.model_fields)
    assert "private_object_key" not in fields
    assert "url" not in fields
    assert "presigned_url" not in fields


# -- bounds ---------------------------------------------------------------------------------------


def test_the_fact_bound_matches_the_frozen_per_case_ceiling() -> None:
    """The input is one case's active facts, so the two bounds must be the same number."""

    assert MAX_INVESTIGATION_FACTS == MAX_ACTIVE_FACTS_PER_CASE


def test_the_corroboration_minimum_is_a_literal_the_model_cannot_change() -> None:
    assert CORROBORATION_MIN == 2
    with pytest.raises(ValidationError):
        # The literal is what makes this unchangeable: a model or a caller naming any
        # other minimum is refused by the schema rather than by a later check.
        InvestigationInput.model_validate(
            {
                "case": case().model_dump(mode="json"),
                "reports": [report().model_dump(mode="json")],
                "corroboration_min": 1,
            }
        )


def test_an_input_requires_at_least_one_report() -> None:
    with pytest.raises(ValidationError):
        InvestigationInput(case=case(), reports=())


def test_an_input_fact_must_name_a_report_in_the_same_input() -> None:
    with pytest.raises(ValidationError):
        InvestigationInput(
            case=case(),
            reports=(report(),),
            facts=(
                InvestigationFact(
                    fact_id=uuid4(),
                    report_id=uuid4(),
                    contributor_pseudonym_id="resident-a",
                    typed_value=LocationAreaValue(
                        fact_type=FactType.LOCATION_AREA, area=LocationAreaCode.LOBBY
                    ),
                    sensitivity=SensitivityCategory.GENERAL,
                    current_status=EvidenceStatus.REPORTED,
                ),
            ),
        )


def test_an_input_fact_must_cite_evidence_in_the_same_input() -> None:
    owner = report()
    with pytest.raises(ValidationError):
        InvestigationInput(
            case=case(),
            reports=(owner,),
            facts=(
                InvestigationFact(
                    fact_id=uuid4(),
                    report_id=owner.report_id,
                    contributor_pseudonym_id="resident-a",
                    typed_value=LocationAreaValue(
                        fact_type=FactType.LOCATION_AREA, area=LocationAreaCode.LOBBY
                    ),
                    sensitivity=SensitivityCategory.GENERAL,
                    evidence_ids=(uuid4(),),
                    current_status=EvidenceStatus.REPORTED,
                ),
            ),
        )


# -- the contradiction shape -----------------------------------------------------------------------


def test_a_contradiction_needs_at_least_two_cited_facts() -> None:
    """One fact names no conflict, so there is nothing for a validator to check."""

    with pytest.raises(ValidationError):
        ContradictionDraft(
            statement_fact_ids=(uuid4(),),
            description="A conflict with nothing.",
            materiality=ContradictionMateriality.LOW,
        )


def test_a_contradiction_may_not_sweep_a_whole_case() -> None:
    with pytest.raises(ValidationError):
        ContradictionDraft(
            statement_fact_ids=tuple(uuid4() for _ in range(11)),
            description="Everything conflicts with everything.",
            materiality=ContradictionMateriality.HIGH,
        )


def test_a_contradiction_cites_each_fact_once() -> None:
    duplicated = uuid4()
    with pytest.raises(ValidationError):
        ContradictionDraft(
            statement_fact_ids=(duplicated, duplicated),
            description="A fact contradicting itself.",
            materiality=ContradictionMateriality.LOW,
        )


def test_materiality_is_a_closed_enum() -> None:
    with pytest.raises(ValidationError):
        ContradictionDraft(
            statement_fact_ids=(uuid4(), uuid4()),
            description="A conflict.",
            materiality="CATASTROPHIC",  # type: ignore[arg-type]
        )


# -- citations -------------------------------------------------------------------------------------


def test_a_cited_claim_must_cite_something() -> None:
    """An explanation nobody can check against the input is one nothing can be done with."""

    with pytest.raises(ValidationError):
        AlternativeExplanation(description="Trust me.", citations=CitationSet())


def test_an_alternative_keeps_its_citations_structured() -> None:
    alternative = AlternativeExplanation(
        description="Scheduled maintenance.",
        citations=CitationSet(cited_report_ids=(uuid4(),), cited_fact_ids=(uuid4(),)),
    )
    assert alternative.citations.cited_report_ids
    assert alternative.citations.cited_fact_ids


def test_one_evidence_item_cannot_both_support_and_oppose_a_fact() -> None:
    shared = uuid4()
    with pytest.raises(ValidationError):
        EvidenceFindingDraft(
            fact_id=uuid4(),
            proposed_status=EvidenceStatus.REPORTED,
            supporting_evidence_ids=(shared,),
            opposing_evidence_ids=(shared,),
            rationale="Both at once.",
        )


def test_findings_name_each_fact_once() -> None:
    fact_id = uuid4()
    with pytest.raises(ValidationError):
        draft(
            evidence_findings=(
                EvidenceFindingDraft(
                    fact_id=fact_id,
                    proposed_status=EvidenceStatus.REPORTED,
                    rationale="One.",
                ),
                EvidenceFindingDraft(
                    fact_id=fact_id,
                    proposed_status=EvidenceStatus.UNKNOWN,
                    rationale="Two.",
                ),
            )
        )


def test_duplicate_groups_name_each_root_once() -> None:
    root_id = uuid4()
    with pytest.raises(ValidationError):
        draft(
            duplicate_evidence_groups=(
                DuplicateEvidenceGroup(root_id=root_id, evidence_ids=(uuid4(),), reason="One."),
                DuplicateEvidenceGroup(root_id=root_id, evidence_ids=(uuid4(),), reason="Two."),
            )
        )


def test_contradicted_stays_in_the_proposed_status_vocabulary() -> None:
    """Removing it would push the model's reading into free text no validator can see."""

    finding = EvidenceFindingDraft(
        fact_id=uuid4(),
        proposed_status=EvidenceStatus.CONTRADICTED,
        rationale="I think these conflict.",
    )
    assert finding.proposed_status is EvidenceStatus.CONTRADICTED


def test_a_proposed_commitment_requires_a_utc_instant() -> None:
    from chorus.contracts.investigation import ProposedCommitment

    with pytest.raises(ValidationError):
        ProposedCommitment(
            source_evidence_id=uuid4(),
            obligor="Property Management",
            action_text="Repair the lift.",
            due_at=datetime(2030, 6, 1, 9, 0, 0),
            verification_method="A resident confirms.",
        )


def test_a_utc_due_instant_is_accepted() -> None:
    from chorus.contracts.investigation import ProposedCommitment

    commitment = ProposedCommitment(
        source_evidence_id=uuid4(),
        obligor="Property Management",
        action_text="Repair the lift.",
        due_at=NOW + timedelta(days=3),
        verification_method="A resident confirms.",
    )
    assert commitment.due_at.tzinfo is not None
