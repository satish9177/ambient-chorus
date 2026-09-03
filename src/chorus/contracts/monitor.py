"""The private Monitor/Intake runtime contract.

The Monitor reads an explicit, bounded batch of ambient messages plus bounded summaries of
cases it might extend, and answers with *proposals*. It has no tool, no repository, no
credential, and no way to write anything. Everything in :class:`MonitorOutput` is an untrusted
suggestion until deterministic application code validates it and decides what, if anything,
becomes durable state.

Two rules shape the whole file:

* the model never names durable identity -- proposals are wired together by ``client_ref``
  labels that are discarded at the validation boundary, and the only identifier the model may
  echo is an ``existing_case_id`` that was present in its own input;
* every quoted claim is anchored -- a :class:`MonitorSourceSpan` carries offsets *and* the
  quoted text, so deterministic code can prove the quotation exists in the message it cites
  rather than trusting that it does.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from chorus.contracts.common import (
    ClientRefStr,
    ConfidenceStr,
    ReasonStr,
    ShortTextStr,
    StrictModel,
    reject_identifier_shaped,
    require_utc_datetime,
)
from chorus.domain.entities import (
    UNNAMED_ISSUE_TYPE,
    DisclosureScope,
    FactType,
    Purpose,
    SensitivityCategory,
)
from chorus.domain.facts import (
    EvidenceMediaKind,
    FailureMode,
    ImpactCode,
    LocationAreaCode,
    SubjectRelation,
)

MONITOR_INPUT_SCHEMA_VERSION: Final[Literal["monitor-input/v1"]] = "monitor-input/v1"
MONITOR_OUTPUT_SCHEMA_VERSION: Final[Literal["monitor-output/v1"]] = "monitor-output/v1"

MAX_MESSAGES_PER_BATCH = 50
MAX_CANDIDATE_SUMMARIES = 20
MAX_PROPOSED_REPORTS = 25
MAX_PROPOSED_FACTS = 100
MAX_MESSAGE_TEXT = 10_000


class IssueType(StrEnum):
    """The frozen V1 issue vocabulary the Monitor may choose from.

    Every member except ``OTHER`` *names a subject*: the word itself says what went wrong, so
    two reports carrying it are describing the same named thing. ``OTHER`` is the opposite --
    it records that the vocabulary had no word, which is a statement about the vocabulary and
    not about the incident, and it is the wire spelling of
    :data:`~chorus.domain.entities.UNNAMED_ISSUE_TYPE`. What follows from that is
    :func:`~chorus.domain.entities.issue_type_names_a_subject` and ADR-012: only a member that
    names a subject may put two reports into one case.
    """

    ELEVATOR_FAILURE = "ELEVATOR_FAILURE"
    OTHER = UNNAMED_ISSUE_TYPE


class MessageClassification(StrEnum):
    """How the Monitor reads one message; diagnostic only, never a durable decision.

    POLICY_LIKE_INSTRUCTION is an observation that a message is addressed to a system
    rather than to neighbours. It exists so an injection attempt can be counted and audited
    as an event without copying the attempt into a log. It changes nothing: a message so
    classified is still ordinary untrusted data, may still be cited, and confers no authority
    either way.
    """

    POSSIBLE_ISSUE_SIGNAL = "POSSIBLE_ISSUE_SIGNAL"
    NOISE = "NOISE"
    UNCERTAIN = "UNCERTAIN"
    POLICY_LIKE_INSTRUCTION = "POLICY_LIKE_INSTRUCTION"


PseudonymStr = Annotated[str, StringConstraints(min_length=1, max_length=40)]
MessageTextStr = Annotated[str, StringConstraints(min_length=1, max_length=MAX_MESSAGE_TEXT)]
ChannelMessageIdStr = Annotated[str, StringConstraints(min_length=1, max_length=160)]
SummaryStr = Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
TitleStr = Annotated[str, StringConstraints(min_length=1, max_length=160)]
CaptionStr = Annotated[str, StringConstraints(min_length=1, max_length=300)]
MediaTypeStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]


# ---------------------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------------------


class MonitorAttachmentDescriptor(StrictModel):
    """What the Monitor learns about an attachment: an opaque ID, a type, maybe a caption.

    No bytes, no S3 key, no presigned URL, and no private object reference ever reach an
    agent runtime.
    """

    evidence_id: UUID
    media_type: MediaTypeStr
    safe_caption: CaptionStr | None = None


class MonitorMessage(StrictModel):
    """One ambient message, presented to the Monitor strictly as untrusted data."""

    message_id: UUID
    channel_message_id: ChannelMessageIdStr
    contributor_pseudonym_id: PseudonymStr
    sent_at: datetime
    text: MessageTextStr
    attachment_descriptors: Annotated[
        tuple[MonitorAttachmentDescriptor, ...], Field(max_length=8)
    ] = ()

    @model_validator(mode="after")
    def validate_message(self) -> Self:
        require_utc_datetime(self.sent_at)
        evidence_ids = tuple(item.evidence_id for item in self.attachment_descriptors)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("attachment descriptors must be unique")
        return self


class MonitorCandidateSummary(StrictModel):
    """A bounded summary of one case the Monitor may propose extending.

    Summaries are the only cross-message memory the Monitor has. They are built by the
    application from state it already loaded; the agent cannot ask for more.
    """

    case_id: UUID
    case_version: Annotated[int, Field(ge=1)]
    title: TitleStr
    issue_type: IssueType
    location_area: LocationAreaCode | None
    fact_summaries: Annotated[tuple[SummaryStr, ...], Field(max_length=20)] = ()


class MonitorInput(StrictModel):
    """The complete, bounded Monitor payload."""

    schema_version: Literal["monitor-input/v1"] = MONITOR_INPUT_SCHEMA_VERSION
    messages: Annotated[
        tuple[MonitorMessage, ...], Field(min_length=1, max_length=MAX_MESSAGES_PER_BATCH)
    ]
    candidate_case_summaries: Annotated[
        tuple[MonitorCandidateSummary, ...], Field(max_length=MAX_CANDIDATE_SUMMARIES)
    ] = ()
    known_sensitive_categories: Annotated[
        tuple[SensitivityCategory, ...], Field(max_length=16)
    ] = ()
    allowed_issue_types: Annotated[tuple[IssueType, ...], Field(min_length=1, max_length=8)] = (
        IssueType.ELEVATOR_FAILURE,
        IssueType.OTHER,
    )

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        message_ids = tuple(message.message_id for message in self.messages)
        if len(set(message_ids)) != len(message_ids):
            raise ValueError("input message IDs must be unique")
        case_ids = tuple(summary.case_id for summary in self.candidate_case_summaries)
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("candidate case summaries must be unique")
        if len(set(self.known_sensitive_categories)) != len(self.known_sensitive_categories):
            raise ValueError("sensitive categories must be unique")
        if len(set(self.allowed_issue_types)) != len(self.allowed_issue_types):
            raise ValueError("allowed issue types must be unique")
        return self


# ---------------------------------------------------------------------------------------
# Typed fact values
# ---------------------------------------------------------------------------------------


class IncidentOccurrenceValue(StrictModel):
    fact_type: Literal[FactType.INCIDENT_OCCURRENCE]
    occurred_at: datetime
    equipment: Literal["ELEVATOR"] = "ELEVATOR"
    failure_mode: FailureMode

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        require_utc_datetime(self.occurred_at)
        return self


class ServiceImpactValue(StrictModel):
    fact_type: Literal[FactType.SERVICE_IMPACT]
    impact_code: ImpactCode
    summary: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class LocationAreaValue(StrictModel):
    fact_type: Literal[FactType.LOCATION_AREA]
    area: LocationAreaCode


class IdentityAttributeValue(StrictModel):
    fact_type: Literal[FactType.IDENTITY_ATTRIBUTE]
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=120)]


class UnitLocationValue(StrictModel):
    fact_type: Literal[FactType.UNIT_LOCATION]
    unit_label: Annotated[str, StringConstraints(min_length=1, max_length=40)]


class HealthDetailValue(StrictModel):
    fact_type: Literal[FactType.HEALTH_DETAIL]
    subject_relation: SubjectRelation
    detail: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class ManagementStatementValue(StrictModel):
    fact_type: Literal[FactType.MANAGEMENT_STATEMENT]
    statement: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    speaker_org: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    stated_at: datetime

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        require_utc_datetime(self.stated_at)
        return self


class EvidenceDescriptionValue(StrictModel):
    fact_type: Literal[FactType.EVIDENCE_DESCRIPTION]
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    media_kind: EvidenceMediaKind


type MonitorFactValue = (
    IncidentOccurrenceValue
    | ServiceImpactValue
    | LocationAreaValue
    | IdentityAttributeValue
    | UnitLocationValue
    | HealthDetailValue
    | ManagementStatementValue
    | EvidenceDescriptionValue
)
"""The fact shapes intake may propose.

``CONTRADICTION`` and ``COMMITMENT_TERM`` are deliberately absent. A contradiction is a
skeptical finding the Investigator produces from a whole case, and a commitment term is
created only by the deterministic commitment validator from a cited external reply. Neither
can be manufactured by an intake proposal, so neither has a shape here to manufacture it
with.
"""

MonitorFactValueField = Annotated[MonitorFactValue, Field(discriminator="fact_type")]


# ---------------------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------------------


class MonitorSourceSpan(StrictModel):
    """An anchored quotation: where it is, and what it says.

    Both halves are required so deterministic code can verify the span rather than believe
    it. Offsets alone would let a model cite a real range and describe it as something else;
    a quotation alone would let it cite text that never appeared.
    """

    message_id: UUID
    start: Annotated[int, Field(ge=0, le=MAX_MESSAGE_TEXT)]
    end: Annotated[int, Field(ge=1, le=MAX_MESSAGE_TEXT)]
    quote: Annotated[str, StringConstraints(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        if self.end <= self.start:
            raise ValueError("source span end must be after start")
        if self.end - self.start != len(self.quote):
            raise ValueError("source span length does not match its quotation")
        return self


class MonitorMessageResult(StrictModel):
    """The Monitor's reading of exactly one input message."""

    message_id: UUID
    classification: MessageClassification
    reason: ReasonStr


class ProposedReport(StrictModel):
    """A proposed contributor-owned report drawn from one or more cited messages."""

    client_ref: ClientRefStr
    message_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=10)]
    contributor_pseudonym_id: PseudonymStr
    issue_type: IssueType
    summary: SummaryStr
    occurred_at: datetime | None = None
    location_area: LocationAreaCode | None = None
    confidence_basis: Annotated[tuple[ReasonStr, ...], Field(max_length=8)] = ()

    @field_validator("client_ref")
    @classmethod
    def validate_client_ref(cls, value: str) -> str:
        return reject_identifier_shaped(value, "report client_ref")

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if len(set(self.message_ids)) != len(self.message_ids):
            raise ValueError("report message citations must be unique")
        if self.occurred_at is not None:
            require_utc_datetime(self.occurred_at)
        return self


class ProposedFact(StrictModel):
    """A proposed smallest policy-addressable assertion, anchored in cited text."""

    client_ref: ClientRefStr
    report_client_ref: ClientRefStr
    fact_type: FactType
    typed_value: MonitorFactValueField
    sensitivity: SensitivityCategory
    evidence_ids: Annotated[tuple[UUID, ...], Field(max_length=8)] = ()
    source_spans: Annotated[tuple[MonitorSourceSpan, ...], Field(min_length=1, max_length=8)]

    @field_validator("client_ref", "report_client_ref")
    @classmethod
    def validate_client_ref(cls, value: str) -> str:
        return reject_identifier_shaped(value, "fact client_ref")

    @model_validator(mode="after")
    def validate_fact(self) -> Self:
        if self.fact_type is not self.typed_value.fact_type:
            raise ValueError("fact discriminator disagrees with its typed value")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("fact evidence citations must be unique")
        return self


class CandidateLink(StrictModel):
    """A proposal that one report belongs with an existing case, or starts a new one.

    Exactly one of ``existing_case_id`` and ``candidate_group_ref`` is present. Extending a
    known case is naming a durable identifier the input already contained; starting a new one
    is naming a *group*, which is a model-local label with no durable meaning. A link that
    supplied both would be claiming two different destinations for one report, and a link that
    supplied neither would leave the application to guess which new case it meant.
    """

    report_client_ref: ClientRefStr
    existing_case_id: UUID | None = None
    candidate_group_ref: ClientRefStr | None = None
    proposed_case_title: TitleStr
    similarity_reasons: Annotated[tuple[ReasonStr, ...], Field(min_length=1, max_length=8)]
    dissimilarity_reasons: Annotated[tuple[ReasonStr, ...], Field(max_length=8)] = ()
    confidence: ConfidenceStr

    @field_validator("report_client_ref")
    @classmethod
    def validate_client_ref(cls, value: str) -> str:
        return reject_identifier_shaped(value, "candidate link report_client_ref")

    @field_validator("candidate_group_ref")
    @classmethod
    def validate_group_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return reject_identifier_shaped(value, "candidate link candidate_group_ref")

    @model_validator(mode="after")
    def validate_link(self) -> Self:
        if (self.existing_case_id is None) == (self.candidate_group_ref is None):
            raise ValueError(
                "a candidate link names exactly one of existing_case_id, candidate_group_ref"
            )
        return self


class SensitiveSignal(StrictModel):
    """An observation that a message appears to contain a sensitive category."""

    message_id: UUID
    category: SensitivityCategory
    source_span: MonitorSourceSpan


class MissingInformationRequest(StrictModel):
    """A question the intake step would like a contributor to answer."""

    contributor_pseudonym_id: PseudonymStr
    report_client_ref: ClientRefStr
    requested_fields: Annotated[tuple[ShortTextStr, ...], Field(min_length=1, max_length=8)]
    reason: ReasonStr

    @field_validator("report_client_ref")
    @classmethod
    def validate_client_ref(cls, value: str) -> str:
        return reject_identifier_shaped(value, "missing information report_client_ref")


class MandateSuggestion(StrictModel):
    """A suggested starting point for a contributor's disclosure decision.

    A suggestion can only ever produce a ``PROPOSED`` mandate. It is never an approval, never
    widens a policy maximum, and is not authorization of any kind.
    """

    report_client_ref: ClientRefStr
    fact_client_refs: Annotated[tuple[ClientRefStr, ...], Field(min_length=1, max_length=20)]
    suggested_max_scope: DisclosureScope
    suggested_purpose: Purpose

    @field_validator("report_client_ref")
    @classmethod
    def validate_client_ref(cls, value: str) -> str:
        return reject_identifier_shaped(value, "mandate suggestion report_client_ref")

    @model_validator(mode="after")
    def validate_suggestion(self) -> Self:
        if len(set(self.fact_client_refs)) != len(self.fact_client_refs):
            raise ValueError("mandate suggestion fact references must be unique")
        for value in self.fact_client_refs:
            reject_identifier_shaped(value, "mandate suggestion fact_client_ref")
        return self


class MonitorOutput(StrictModel):
    """Everything one Monitor invocation proposes. None of it is durable state."""

    schema_version: Literal["monitor-output/v1"] = MONITOR_OUTPUT_SCHEMA_VERSION
    message_results: Annotated[
        tuple[MonitorMessageResult, ...], Field(min_length=1, max_length=MAX_MESSAGES_PER_BATCH)
    ]
    proposed_reports: Annotated[
        tuple[ProposedReport, ...], Field(max_length=MAX_PROPOSED_REPORTS)
    ] = ()
    proposed_facts: Annotated[tuple[ProposedFact, ...], Field(max_length=MAX_PROPOSED_FACTS)] = ()
    candidate_links: Annotated[
        tuple[CandidateLink, ...], Field(max_length=MAX_PROPOSED_REPORTS)
    ] = ()
    sensitive_signals: Annotated[tuple[SensitiveSignal, ...], Field(max_length=50)] = ()
    missing_information_requests: Annotated[
        tuple[MissingInformationRequest, ...], Field(max_length=25)
    ] = ()
    mandate_suggestions: Annotated[
        tuple[MandateSuggestion, ...], Field(max_length=MAX_PROPOSED_REPORTS)
    ] = ()

    @model_validator(mode="after")
    def validate_output(self) -> Self:
        result_ids = tuple(result.message_id for result in self.message_results)
        if len(set(result_ids)) != len(result_ids):
            raise ValueError("message results must be unique per message")
        report_refs = tuple(report.client_ref for report in self.proposed_reports)
        if len(set(report_refs)) != len(report_refs):
            raise ValueError("report client references must be unique")
        fact_refs = tuple(fact.client_ref for fact in self.proposed_facts)
        if len(set(fact_refs)) != len(fact_refs):
            raise ValueError("fact client references must be unique")
        if set(fact_refs) & set(report_refs):
            raise ValueError("a client reference cannot name both a report and a fact")
        link_refs = tuple(link.report_client_ref for link in self.candidate_links)
        if len(set(link_refs)) != len(link_refs):
            raise ValueError("a report may appear in at most one candidate link")
        suggestion_refs = tuple(item.report_client_ref for item in self.mandate_suggestions)
        if len(set(suggestion_refs)) != len(suggestion_refs):
            raise ValueError("a report may carry at most one mandate suggestion")
        return self
