"""Deterministic semantic validation of one Investigator answer.

Schema validity is not truth and not authorization. By the time an answer reaches this module
Pydantic has proved it is *well formed*; everything here proves it is *about the input that was
actually sent*, and refuses the whole answer when it is not.

The rules are whole-output, never per-item. There is no path that keeps the acceptable half of
a malformed assessment: a model that cited one identifier it was never given has demonstrated
that the rest of its reading is unverified too, and salvaging is exactly how a cross-case
reference gets quietly accepted.

What is checked, and why each check exists:

* **envelope and prompt identity** -- the answer belongs to this invocation, this case, this
  case version, and the reviewed prompt version, so a replayed or foreign result cannot be
  applied here;
* **citation membership** -- every fact, report, evidence, and root identifier the answer
  carries appeared in this invocation's own input. That is what makes a hallucinated or
  cross-case identifier impossible rather than merely unlikely, and the input was itself built
  from one case, so "in the input" and "in this case" are the same statement;
* **contradiction shape** -- two to ten unique cited facts, all of them facts of this case, a
  bounded description, and a materiality inside the closed enum. Once those hold, the
  contradiction's *consequence* is applied deterministically and the model gets no further say;
* **proposed-commitment citation** -- the cited source evidence exists in this case. Nothing
  else about a proposed commitment is checked, because nothing about it is persisted.

What is deliberately **not** checked, because it is not the model's to assert: the independence
count, the corroboration flag, the duplicate-evidence grouping, and the recommended
disposition. Those are validated for citation membership so an invented identifier is still
caught, and then they are recorded or discarded. None of them is read by a guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from chorus.contracts.common import INVESTIGATOR_PROMPT_VERSION, AgentName
from chorus.contracts.investigation import (
    CitationSet,
    InvestigationAssessmentDraft,
    InvestigationInput,
    LinkageDecision,
    RecommendedCaseDisposition,
)
from chorus.domain.entities import (
    AssessmentAlternative,
    AssessmentContradiction,
    EvidenceStatus,
)
from chorus.domain.ids import EvidenceItemId, FactId, Namespace, ReportId
from chorus.ports.agents import (
    AgentContractViolationError,
    InvestigationInvocation,
    InvestigationRejection,
    InvestigationResult,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedInvestigation:
    """Everything deterministic code is prepared to act on from one Investigator answer.

    ``proposed_statuses`` is a *proposal map*, not a result. It is consumed by the ladder in
    ``evidence_status.resolve_status``, which may honour a weaker value and never a stronger
    one; a fact with no entry simply keeps its computed status.

    ``duplicate_group_count`` and ``proposed_commitment_count`` are counts and nothing more.
    Both were validated for citation membership and then discarded, because neither has any
    durable consequence in Phase 5 -- root collapse is deterministic, and a commitment is
    created by the commitment validator alongside the destination binding that makes its
    authenticity checkable.
    """

    linkage_decision: LinkageDecision
    linkage_reason_count: int
    proposed_statuses: dict[FactId, EvidenceStatus]
    contradictions: tuple[AssessmentContradiction, ...]
    alternative_explanations: tuple[AssessmentAlternative, ...]
    recommended_disposition: RecommendedCaseDisposition
    model_independent_source_count: int
    model_is_corroborated: bool
    gap_count: int
    duplicate_group_count: int
    proposed_commitment_count: int

    @property
    def contradicted_fact_ids(self) -> frozenset[FactId]:
        """Every fact a validated contradiction cites: the only path to ``CONTRADICTED``."""

        return frozenset(
            fact_id
            for contradiction in self.contradictions
            for fact_id in contradiction.statement_fact_ids
        )


class _Rejections:
    """Collects reasons so one pass reports every distinct failure it found."""

    __slots__ = ("_reasons",)

    def __init__(self) -> None:
        self._reasons: list[InvestigationRejection] = []

    def add(self, reason: InvestigationRejection) -> None:
        self._reasons.append(reason)

    def raise_if_any(self) -> None:
        if self._reasons:
            raise AgentContractViolationError(tuple(self._reasons))


def validate_investigation_result(
    *,
    invocation: InvestigationInvocation,
    result: InvestigationResult,
    namespace: Namespace,
) -> ValidatedInvestigation:
    """Validate one Investigator answer end to end, or refuse all of it."""

    rejections = _Rejections()
    _validate_envelope(invocation, result, namespace, rejections)
    # The envelope decides whether this answer belongs to this invocation at all, so a
    # mismatch stops the pass before any citation is interpreted against the wrong case.
    rejections.raise_if_any()

    payload = invocation.payload
    output = result.output
    known_facts = {fact.fact_id for fact in payload.facts}
    known_reports = {report.report_id for report in payload.reports}
    known_evidence = {item.evidence_id for item in payload.evidence}
    known_roots = {item.root_id for item in payload.evidence}

    _validate_case_identity(payload, output, rejections)
    _validate_citation_sets(output, known_reports, known_facts, known_evidence, rejections)
    proposed = _validate_findings(output, known_facts, known_evidence, rejections)
    contradictions = _validate_contradictions(output, known_facts, rejections)
    _validate_duplicate_groups(output, known_roots, known_evidence, rejections)
    _validate_proposed_commitments(output, known_evidence, rejections)
    rejections.raise_if_any()

    return ValidatedInvestigation(
        linkage_decision=output.linkage_decision,
        linkage_reason_count=len(output.linkage_reasons),
        proposed_statuses=proposed,
        contradictions=contradictions,
        alternative_explanations=tuple(
            AssessmentAlternative(
                description=alternative.description,
                cited_report_ids=tuple(
                    ReportId(value) for value in alternative.citations.cited_report_ids
                ),
                cited_fact_ids=tuple(
                    FactId(value) for value in alternative.citations.cited_fact_ids
                ),
                cited_evidence_ids=tuple(
                    EvidenceItemId(value) for value in alternative.citations.cited_evidence_ids
                ),
            )
            for alternative in output.alternative_explanations
        ),
        recommended_disposition=output.recommended_case_disposition,
        model_independent_source_count=output.sufficiency.independent_source_count,
        model_is_corroborated=output.sufficiency.is_corroborated,
        gap_count=len(output.sufficiency.gaps),
        duplicate_group_count=len(output.duplicate_evidence_groups),
        proposed_commitment_count=len(output.proposed_commitments),
    )


def _validate_envelope(
    invocation: InvestigationInvocation,
    result: InvestigationResult,
    namespace: Namespace,
    rejections: _Rejections,
) -> None:
    if (
        result.invocation_id != invocation.invocation_id
        or result.namespace != namespace.value
        or result.namespace != invocation.namespace
        or result.agent_name is not AgentName.INVESTIGATOR
        or invocation.agent_name is not AgentName.INVESTIGATOR
        or result.case_id != invocation.case_id
        or result.case_version != invocation.case_version
    ):
        rejections.add(InvestigationRejection.ENVELOPE_MISMATCH)
    if result.prompt_version != INVESTIGATOR_PROMPT_VERSION:
        # The request never named a prompt version, so this compares the runtime's answer
        # against the one prompt this application reviewed -- not against something the caller
        # asked for and the runtime could simply have echoed back.
        rejections.add(InvestigationRejection.PROMPT_VERSION_MISMATCH)


def _validate_case_identity(
    payload: InvestigationInput,
    output: InvestigationAssessmentDraft,
    rejections: _Rejections,
) -> None:
    """The answer names the case it was given, at the version it was shown.

    Both halves matter. A wrong case identifier is a foreign answer; a wrong version is an
    answer about a case that has since moved, and applying it would attach a reading of an
    older world to the current one.
    """

    case = payload.case
    if output.case_id != case.case_id:
        rejections.add(InvestigationRejection.CASE_MISMATCH)
    if output.based_on_case_version != case.version:
        rejections.add(InvestigationRejection.CASE_VERSION_MISMATCH)


def _check_citations(
    citations: CitationSet,
    known_reports: set[UUID],
    known_facts: set[UUID],
    known_evidence: set[UUID],
    rejections: _Rejections,
) -> None:
    if any(value not in known_reports for value in citations.cited_report_ids):
        rejections.add(InvestigationRejection.UNKNOWN_REPORT_ID)
    if any(value not in known_facts for value in citations.cited_fact_ids):
        rejections.add(InvestigationRejection.UNKNOWN_FACT_ID)
    if any(value not in known_evidence for value in citations.cited_evidence_ids):
        rejections.add(InvestigationRejection.UNKNOWN_EVIDENCE_ID)


def _validate_citation_sets(
    output: InvestigationAssessmentDraft,
    known_reports: set[UUID],
    known_facts: set[UUID],
    known_evidence: set[UUID],
    rejections: _Rejections,
) -> None:
    for reason in output.linkage_reasons:
        _check_citations(reason.citations, known_reports, known_facts, known_evidence, rejections)
    for alternative in output.alternative_explanations:
        _check_citations(
            alternative.citations, known_reports, known_facts, known_evidence, rejections
        )


def _validate_findings(
    output: InvestigationAssessmentDraft,
    known_facts: set[UUID],
    known_evidence: set[UUID],
    rejections: _Rejections,
) -> dict[FactId, EvidenceStatus]:
    """Prove every finding is about a fact of this case, then keep only its proposal.

    The supporting and opposing evidence citations are validated and then dropped. They are the
    model's account of *why*, and the rationale that carries it is free text the private
    surface shows a human; neither is an input to the status, which is recomputed.
    """

    proposed: dict[FactId, EvidenceStatus] = {}
    for finding in output.evidence_findings:
        if finding.fact_id not in known_facts:
            rejections.add(InvestigationRejection.UNKNOWN_FACT_ID)
            continue
        cited = (*finding.supporting_evidence_ids, *finding.opposing_evidence_ids)
        if any(value not in known_evidence for value in cited):
            rejections.add(InvestigationRejection.UNKNOWN_EVIDENCE_ID)
        proposed[FactId(finding.fact_id)] = finding.proposed_status
    return proposed


def _validate_contradictions(
    output: InvestigationAssessmentDraft,
    known_facts: set[UUID],
    rejections: _Rejections,
) -> tuple[AssessmentContradiction, ...]:
    """Prove exactly what deterministic code can prove about a proposed contradiction.

    Existence, case membership, entity type, cardinality, uniqueness, schema, and a materiality
    inside the closed enum. Deterministic code does **not** decide whether two statements
    actually conflict -- nothing in this system does semantic entailment -- so the model's
    judgement is accepted once those checks pass, and its consequence is then fixed and
    conservative: every cited fact resolves to ``CONTRADICTED``.

    That is validated model judgement with a fail-safe consequence. It can lower a status and
    block readiness; it can never grant readiness, ``VERIFIED``, a scope, an identity, a
    destination, or any other authority. A model that invents contradictions costs the system
    availability and cannot cost it safety.
    """

    contradictions: list[AssessmentContradiction] = []
    for entry in output.contradictions:
        if any(value not in known_facts for value in entry.statement_fact_ids):
            # A contradiction citing a fact this case does not contain is either a
            # hallucination or a cross-case reference, and both refuse the whole answer.
            rejections.add(InvestigationRejection.UNKNOWN_FACT_ID)
            continue
        try:
            contradictions.append(
                AssessmentContradiction(
                    statement_fact_ids=tuple(FactId(value) for value in entry.statement_fact_ids),
                    description=entry.description,
                    materiality=entry.materiality,
                )
            )
        except ValueError:
            # The contract already bounds cardinality and uniqueness; reaching here means the
            # domain rule and the wire schema disagree, which is refused rather than resolved
            # in favour of whichever happens to be looser.
            rejections.add(InvestigationRejection.CONTRADICTION_INVALID)
    return tuple(contradictions)


def _validate_duplicate_groups(
    output: InvestigationAssessmentDraft,
    known_roots: set[UUID],
    known_evidence: set[UUID],
    rejections: _Rejections,
) -> None:
    """Prove the group names real inputs, then let it change nothing.

    Root collapse is deterministic and resolved from stored ``parent_root_id`` chains through
    the root-ID locator. A model's belief about which copies share an origin is recorded in the
    answer and shown to a human; it can neither add nor remove an independent source.
    """

    for group in output.duplicate_evidence_groups:
        if group.root_id not in known_roots:
            rejections.add(InvestigationRejection.UNKNOWN_ROOT_ID)
        if any(value not in known_evidence for value in group.evidence_ids):
            rejections.add(InvestigationRejection.UNKNOWN_EVIDENCE_ID)


def _validate_proposed_commitments(
    output: InvestigationAssessmentDraft,
    known_evidence: set[UUID],
    rejections: _Rejections,
) -> None:
    """Existence and same-case membership of the cited source evidence. Nothing else.

    The requirement that the source be an *authenticated external reply* is checked where the
    commitment is actually created, alongside the destination binding that makes it checkable.
    Phase 5 has no such binding to check against, and inventing one here would be inventing a
    later phase's persistence semantics to satisfy a validation nobody asked for yet.
    """

    for commitment in output.proposed_commitments:
        if commitment.source_evidence_id not in known_evidence:
            rejections.add(InvestigationRejection.COMMITMENT_CITATION_INVALID)
