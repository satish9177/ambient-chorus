"""The private Investigator/Skeptic runtime contract.

The Investigator reads exactly one case -- its reports, its facts, its evidence metadata, and
the assessment that preceded this one -- and answers with a *skeptical reading* of it. It has
no tool, no repository, no credential, and no write path. Everything in
:class:`InvestigationAssessmentDraft` is an untrusted suggestion until deterministic
application code validates it and decides what, if anything, becomes durable state.

Three rules shape the whole file, and each is a boundary rather than a preference:

* **the model names no durable identity.** Every identifier the answer may carry is one that
  appeared in its own input. There is no ``client_ref`` here as there is in the Monitor
  contract, because an investigation creates nothing: it reads facts that already exist and
  says what it thinks of them;

* **every field is either advisory or block-only.** ``proposed_status`` may only *lower* a
  fact's deterministically computed status; ``materiality`` may only *block* readiness;
  ``linkage_decision`` may only fail closed; ``sufficiency`` and
  ``recommended_case_disposition`` are recorded and never read by a guard. There is no field
  in this schema through which the model can grant anything (ADR-015);

* **one authority path per outcome.** ``contradictions[]`` is the only field whose validated
  content can resolve a fact to ``CONTRADICTED``. ``EvidenceFindingStatus.CONTRADICTED`` stays
  in the status vocabulary so the model can state its reading, and it is inert: it names no
  cited facts, so there is nothing for a validator to check and nothing a reader could audit
  the claim against.

What is deliberately absent matters as much as what is here. There is no field for a case
state, a case split, a disclosure scope, a purpose, a destination, an identity permission, a
mandate, a commitment identifier, or a fact the model would like created. An agent cannot
propose what it has no field to propose in.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from chorus.contracts.common import (
    ReasonStr,
    ShortTextStr,
    StrictModel,
    require_utc_datetime,
)
from chorus.contracts.monitor import (
    IssueType,
    MonitorFactValueField,
    PseudonymStr,
)
from chorus.domain.entities import (
    CaseState,
    ContradictionMateriality,
    EvidenceStatus,
    SensitivityCategory,
)

INVESTIGATION_INPUT_SCHEMA_VERSION: Final[Literal["investigation-input/v1"]] = (
    "investigation-input/v1"
)
INVESTIGATION_OUTPUT_SCHEMA_VERSION: Final[Literal["investigation-output/v1"]] = (
    "investigation-output/v1"
)

CORROBORATION_MIN: Final = 2
"""The frozen independence minimum, restated to the agent as context and never as authority.

It is the same constant ``chorus.privacy.policy`` holds; the contract carries it so the model
can say whether it *believes* a case reaches it. Deterministic code recomputes the real number
from stored state and never reads the one that comes back.
"""

MAX_INVESTIGATION_REPORTS = 25
MAX_INVESTIGATION_FACTS = 100
MAX_INVESTIGATION_EVIDENCE = 60
MAX_PRIOR_FINDINGS = 100
MAX_LINKAGE_REASONS = 8
MAX_ALTERNATIVES = 8
MAX_CONTRADICTIONS = 10
MAX_DUPLICATE_GROUPS = 10
MAX_PROPOSED_COMMITMENTS = 5
MAX_GAPS = 10
MAX_CITATIONS = 10
MAX_SOURCE_GROUPS = 10
"""Bounded collection sizes.

``MAX_INVESTIGATION_FACTS`` matches the frozen ``MAX_ACTIVE_FACTS_PER_CASE`` because the input
is one case's active facts and nothing else. The output bounds are smaller than the input
bounds on purpose: an answer that needs a hundred contradictions is not a skeptical reading of
a case, and the transaction that applies it is bounded too.
"""

RationaleStr = Annotated[str, StringConstraints(min_length=1, max_length=500)]
DescriptionStr = Annotated[str, StringConstraints(min_length=1, max_length=500)]
MediaTypeStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]
Sha256Str = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
ExtractedTextStr = Annotated[str, StringConstraints(min_length=1, max_length=4_000)]
CaptionStr = Annotated[str, StringConstraints(min_length=1, max_length=300)]
ObligorStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]
ActionTextStr = Annotated[str, StringConstraints(min_length=1, max_length=500)]
VerificationMethodStr = Annotated[str, StringConstraints(min_length=1, max_length=300)]
SummaryStr = Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
TitleStr = Annotated[str, StringConstraints(min_length=1, max_length=160)]


class LinkageDecision(StrEnum):
    """Whether the case's reports describe one issue.

    Consumed through a fixed table and able only to block: ``SAME_ISSUE`` may satisfy the
    readiness linkage term, ``DIFFERENT_ISSUES`` blocks, and ``UNCERTAIN`` blocks as well --
    an ambiguous linkage is a missing authorization, and the fail-closed rule applies.
    """

    SAME_ISSUE = "SAME_ISSUE"
    DIFFERENT_ISSUES = "DIFFERENT_ISSUES"
    UNCERTAIN = "UNCERTAIN"


class RecommendedCaseDisposition(StrEnum):
    """What the Investigator would do next, recorded and never acted on.

    Deliberately expressive -- including ``READY_FOR_ACTION`` and ``SPLIT_CANDIDATE``, neither
    of which any code reads. A model that has an opinion should be able to state it where a
    human can see it; a vocabulary that could not express the opinion would push it into free
    text, and a guard that read it would make the model the thing that decides.
    """

    CONTINUE_INVESTIGATION = "CONTINUE_INVESTIGATION"
    READY_FOR_ACTION = "READY_FOR_ACTION"
    SPLIT_CANDIDATE = "SPLIT_CANDIDATE"
    CLOSE_UNRESOLVED = "CLOSE_UNRESOLVED"


# ---------------------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------------------


class InvestigationCase(StrictModel):
    """The case under investigation, in the five fields the agent is allowed to see."""

    case_id: UUID
    version: Annotated[int, Field(ge=1)]
    title: TitleStr
    issue_type: IssueType
    current_state: CaseState


class InvestigationReport(StrictModel):
    """One contributor-owned report, pseudonymous and without contact data."""

    report_id: UUID
    contributor_pseudonym_id: PseudonymStr
    summary: SummaryStr
    occurred_at: datetime | None = None
    source_message_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=10)]

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.occurred_at is not None:
            require_utc_datetime(self.occurred_at)
        if len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("report message citations must be unique")
        return self


class InvestigationFact(StrictModel):
    """One stored fact, carrying its *current* status rather than a blank to be filled in.

    ``current_status`` is shown so the model can resolve against what the system already
    believes instead of restating it. It is context, not a starting point the model may raise
    from: the status that ends up stored is recomputed deterministically and then resolved
    against the model's proposal on the downgrade-only ladder.
    """

    fact_id: UUID
    report_id: UUID
    contributor_pseudonym_id: PseudonymStr
    typed_value: MonitorFactValueField
    sensitivity: SensitivityCategory
    evidence_ids: Annotated[tuple[UUID, ...], Field(max_length=MAX_CITATIONS)] = ()
    current_status: EvidenceStatus

    @model_validator(mode="after")
    def validate_fact(self) -> Self:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("fact evidence citations must be unique")
        return self


class InvestigationEvidence(StrictModel):
    """Evidence *metadata* and permitted extracted text. Never bytes, never a URI.

    ``extracted_text`` may carry hostile content -- that is the point of a skeptic reading
    private evidence -- and it is untrusted data in exactly the way message text is. The
    runtime fences it, no validated field can be satisfied by something found inside it, and
    the agent has no capability it could invoke.
    """

    evidence_id: UUID
    root_id: UUID
    submitted_by_pseudonym_id: PseudonymStr
    media_type: MediaTypeStr
    sha256: Sha256Str
    derived_from_evidence_id: UUID | None = None
    extracted_text: ExtractedTextStr | None = None
    safe_machine_caption: CaptionStr | None = None


class PriorFinding(StrictModel):
    """One fact's resolved status from the previous assessment."""

    fact_id: UUID
    evidence_status: EvidenceStatus


class PriorAssessment(StrictModel):
    """What the last investigation concluded, so this one is a revision and not a restart."""

    assessment_id: UUID
    based_on_case_version: Annotated[int, Field(ge=1)]
    findings: Annotated[tuple[PriorFinding, ...], Field(max_length=MAX_PRIOR_FINDINGS)] = ()

    @model_validator(mode="after")
    def validate_prior(self) -> Self:
        fact_ids = tuple(finding.fact_id for finding in self.findings)
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("prior findings must name each fact once")
        return self


class InvestigationInput(StrictModel):
    """The complete, bounded Investigator payload: exactly one case."""

    schema_version: Literal["investigation-input/v1"] = INVESTIGATION_INPUT_SCHEMA_VERSION
    case: InvestigationCase
    reports: Annotated[
        tuple[InvestigationReport, ...], Field(min_length=1, max_length=MAX_INVESTIGATION_REPORTS)
    ]
    facts: Annotated[tuple[InvestigationFact, ...], Field(max_length=MAX_INVESTIGATION_FACTS)] = ()
    evidence: Annotated[
        tuple[InvestigationEvidence, ...], Field(max_length=MAX_INVESTIGATION_EVIDENCE)
    ] = ()
    prior_assessment: PriorAssessment | None = None
    corroboration_min: Literal[2] = CORROBORATION_MIN

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        report_ids = tuple(report.report_id for report in self.reports)
        if len(set(report_ids)) != len(report_ids):
            raise ValueError("input report IDs must be unique")
        fact_ids = tuple(fact.fact_id for fact in self.facts)
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("input fact IDs must be unique")
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("input evidence IDs must be unique")
        known_reports = set(report_ids)
        if any(fact.report_id not in known_reports for fact in self.facts):
            raise ValueError("every input fact names a report in this input")
        known_evidence = set(evidence_ids)
        for fact in self.facts:
            if any(evidence_id not in known_evidence for evidence_id in fact.evidence_ids):
                raise ValueError("every input fact cites evidence in this input")
        return self


# ---------------------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------------------


class CitationSet(StrictModel):
    """The IDs one free-text claim rests on.

    Every reason and every alternative carries one, because an explanation nobody can check
    against the input is an explanation nothing can be done with. Deterministic validation
    proves each identifier was in this invocation's own input.
    """

    cited_report_ids: Annotated[tuple[UUID, ...], Field(max_length=MAX_CITATIONS)] = ()
    cited_fact_ids: Annotated[tuple[UUID, ...], Field(max_length=MAX_CITATIONS)] = ()
    cited_evidence_ids: Annotated[tuple[UUID, ...], Field(max_length=MAX_CITATIONS)] = ()

    @model_validator(mode="after")
    def validate_citations(self) -> Self:
        for name, values in (
            ("report", self.cited_report_ids),
            ("fact", self.cited_fact_ids),
            ("evidence", self.cited_evidence_ids),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{name} citations must be unique")
        if not (self.cited_report_ids or self.cited_fact_ids or self.cited_evidence_ids):
            raise ValueError("a cited claim must cite at least one identifier")
        return self


class LinkageReason(StrictModel):
    """One cited reason for the linkage decision."""

    reason: ReasonStr
    citations: CitationSet


class AlternativeExplanation(StrictModel):
    """One cited alternative reading of the same evidence.

    Preserved structurally rather than flattened to a string, because an alternative whose
    citations were dropped cannot be evaluated by the human the private investigation surface
    shows it to.
    """

    description: DescriptionStr
    citations: CitationSet


class EvidenceFindingDraft(StrictModel):
    """The model's reading of one fact.

    ``proposed_status`` is **advisory and downgrade-only**. It is honoured only when it is
    weaker than the deterministically computed status on the ladder
    ``VERIFIED > CORROBORATED > REPORTED > UNKNOWN``; a proposed ``VERIFIED`` is always
    downgraded, because policy/v1's allowed verification source set is empty. A proposed
    ``CONTRADICTED`` carries no authority at all: ``contradictions[]`` is the only path to that
    status, and it is the only one that names the facts the conflict is between.

    ``independent_source_groups`` is the model's *belief* about which sources are independent.
    It is recorded in the answer, validated for citation membership, and never used to count:
    independence is recomputed from stored contributors and collapsed evidence roots.
    """

    fact_id: UUID
    proposed_status: EvidenceStatus
    supporting_evidence_ids: Annotated[tuple[UUID, ...], Field(max_length=MAX_CITATIONS)] = ()
    opposing_evidence_ids: Annotated[tuple[UUID, ...], Field(max_length=MAX_CITATIONS)] = ()
    independent_source_groups: Annotated[
        tuple[ShortTextStr, ...], Field(max_length=MAX_SOURCE_GROUPS)
    ] = ()
    rationale: RationaleStr

    @model_validator(mode="after")
    def validate_finding(self) -> Self:
        for name, values in (
            ("supporting", self.supporting_evidence_ids),
            ("opposing", self.opposing_evidence_ids),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{name} evidence citations must be unique")
        overlap = set(self.supporting_evidence_ids) & set(self.opposing_evidence_ids)
        if overlap:
            raise ValueError("one evidence item cannot both support and oppose a fact")
        return self


class ContradictionDraft(StrictModel):
    """A proposed contradiction between two or more facts of this case.

    This is the one field of validated model judgement with a deterministic consequence:
    once the cited facts are proved to exist, to belong to this case, and to satisfy the
    cardinality rules, application code resolves every cited fact to ``CONTRADICTED``.

    The direction of effect is the invariant. An accepted contradiction can only make the
    system more conservative -- it lowers statuses and can block readiness -- and can never
    grant readiness, ``VERIFIED``, a scope, an identity, a destination, or any other authority.
    """

    statement_fact_ids: Annotated[tuple[UUID, ...], Field(min_length=2, max_length=10)]
    description: DescriptionStr
    materiality: ContradictionMateriality

    @model_validator(mode="after")
    def validate_contradiction(self) -> Self:
        if len(set(self.statement_fact_ids)) != len(self.statement_fact_ids):
            raise ValueError("a contradiction cites each fact once")
        return self


class DuplicateEvidenceGroup(StrictModel):
    """Evidence the model believes shares one origin.

    Recorded and shown; never counted. Root collapse is deterministic, resolved from the
    stored ``parent_root_id`` chain through the root-ID locator, and a model's opinion about
    which copies are the same file cannot add or remove an independent source.
    """

    root_id: UUID
    evidence_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=MAX_CITATIONS)]
    reason: ReasonStr

    @model_validator(mode="after")
    def validate_group(self) -> Self:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("a duplicate group names each evidence item once")
        return self


class ProposedCommitment(StrictModel):
    """A promise the model believes an external reply contains.

    Phase 5 validates the shape and the citation and then **discards it**. No commitment, no
    schedule, no ``COMMITMENT_TERM`` fact, and no case transition follows. The requirement that
    ``source_evidence_id`` be an authenticated external reply is checked where the commitment
    is actually created, alongside the destination binding that makes it checkable.
    """

    source_evidence_id: UUID
    obligor: ObligorStr
    action_text: ActionTextStr
    due_at: datetime
    verification_method: VerificationMethodStr

    @model_validator(mode="after")
    def validate_commitment(self) -> Self:
        require_utc_datetime(self.due_at)
        return self


class SufficiencyDraft(StrictModel):
    """The model's own count and its gaps.

    ``independent_source_count`` and ``is_corroborated`` are **never** read. They are here so
    an evaluation can measure how well the model reasons about independence, and so a wrong
    belief is visible in the answer rather than invisible in the prompt. The authoritative
    count is recomputed from stored contributors and collapsed roots.
    """

    independent_source_count: Annotated[int, Field(ge=0, le=1_000)]
    is_corroborated: bool
    gaps: Annotated[tuple[ReasonStr, ...], Field(max_length=MAX_GAPS)] = ()


class InvestigationAssessmentDraft(StrictModel):
    """The complete, bounded Investigator answer. Untrusted until validated."""

    schema_version: Literal["investigation-output/v1"] = INVESTIGATION_OUTPUT_SCHEMA_VERSION
    case_id: UUID
    based_on_case_version: Annotated[int, Field(ge=1)]
    linkage_decision: LinkageDecision
    linkage_reasons: Annotated[
        tuple[LinkageReason, ...], Field(max_length=MAX_LINKAGE_REASONS)
    ] = ()
    alternative_explanations: Annotated[
        tuple[AlternativeExplanation, ...], Field(max_length=MAX_ALTERNATIVES)
    ] = ()
    evidence_findings: Annotated[
        tuple[EvidenceFindingDraft, ...], Field(max_length=MAX_INVESTIGATION_FACTS)
    ] = ()
    contradictions: Annotated[
        tuple[ContradictionDraft, ...], Field(max_length=MAX_CONTRADICTIONS)
    ] = ()
    duplicate_evidence_groups: Annotated[
        tuple[DuplicateEvidenceGroup, ...], Field(max_length=MAX_DUPLICATE_GROUPS)
    ] = ()
    proposed_commitments: Annotated[
        tuple[ProposedCommitment, ...], Field(max_length=MAX_PROPOSED_COMMITMENTS)
    ] = ()
    sufficiency: SufficiencyDraft
    recommended_case_disposition: RecommendedCaseDisposition

    @model_validator(mode="after")
    def validate_output(self) -> Self:
        fact_ids = tuple(finding.fact_id for finding in self.evidence_findings)
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("evidence findings must name each fact once")
        group_roots = tuple(group.root_id for group in self.duplicate_evidence_groups)
        if len(set(group_roots)) != len(group_roots):
            raise ValueError("duplicate evidence groups must name each root once")
        return self
