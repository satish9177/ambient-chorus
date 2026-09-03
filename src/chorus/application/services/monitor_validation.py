"""Deterministic semantic validation of one Monitor answer.

Schema validity is not truth and not authorization. By the time an answer reaches this module
Pydantic has already proved it is *well formed*; everything here proves it is *about the input
that was actually sent*, and refuses the whole answer when it is not.

The rules are whole-output, never per-item. There is no path that keeps the acceptable half of
a malformed proposal, because a model that invented one identifier has demonstrated that the
rest of its answer is unverified too, and because salvaging is exactly how a cross-case
reference gets quietly accepted.

What is checked, and why each check exists:

* **envelope and prompt identity** -- the answer must belong to this invocation and to the
  reviewed prompt version, so a replayed or foreign result cannot be applied here;
* **message coverage** -- every input message is accounted for exactly once, so "the model
  never mentioned message 18" cannot pass as "message 18 is noise";
* **citation membership** -- every message, evidence, and case identifier already existed in
  this invocation's own input, which is what makes a hallucinated or foreign ID impossible
  rather than merely unlikely;
* **ownership** -- a report belongs to the contributor who actually sent every message it
  cites, so nobody can be made the owner of someone else's words or mandate;
* **anchored quotation** -- each source span must reproduce the exact substring at the exact
  offsets of the message it cites;
* **linkage completeness** -- every proposed report carries a candidate link and every proposed
  fact belongs to a linked report, so nothing is silently dropped on the way to persistence;
* **provable grouping** -- two reports share a case only under an issue type that names a
  subject, because that is the only closed signal in the input from which relatedness can be
  proved rather than believed (ADR-012).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from chorus.contracts.common import MONITOR_PROMPT_VERSION, AgentName
from chorus.contracts.monitor import (
    EvidenceDescriptionValue,
    HealthDetailValue,
    IdentityAttributeValue,
    IncidentOccurrenceValue,
    LocationAreaValue,
    ManagementStatementValue,
    MessageClassification,
    MonitorCandidateSummary,
    MonitorFactValue,
    MonitorMessage,
    MonitorOutput,
    MonitorSourceSpan,
    ProposedFact,
    ServiceImpactValue,
    UnitLocationValue,
)
from chorus.domain.entities import FactType, SensitivityCategory, issue_type_names_a_subject
from chorus.domain.facts import (
    REQUIRED_SENSITIVITY,
    EvidenceDescription,
    FactValue,
    HealthDetail,
    IdentityAttribute,
    IncidentOccurrence,
    LocationArea,
    LocationAreaCode,
    ManagementStatement,
    ServiceImpact,
    UnitLocation,
)
from chorus.domain.ids import CaseId, ContributorId, EvidenceItemId, MessageId, Namespace
from chorus.ports.agents import (
    AgentContractViolationError,
    AgentRejection,
    MonitorInvocation,
    MonitorResult,
)
from chorus.ports.limits import MAX_ACTIVE_FACTS_PER_CASE

OCCURRENCE_BACKDATE_WINDOW = timedelta(days=7)
"""How far before its earliest cited message an incident may be said to have occurred.

Residents often report late, so some backdating is legitimate. An unbounded window is not: it
would let a proposal place an incident outside the period the batch actually covers, where no
cited message could ever support or contradict it.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedReport:
    """A report proposal that survived validation, resolved onto durable domain types."""

    client_ref: str
    contributor_id: ContributorId
    issue_type: str
    summary: str
    occurred_at: datetime | None
    location_area: LocationAreaCode | None
    source_message_ids: tuple[MessageId, ...]
    evidence_ids: tuple[EvidenceItemId, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedFact:
    """A fact proposal that survived validation, resolved onto a closed typed value."""

    client_ref: str
    report_client_ref: str
    fact_type: FactType
    value: FactValue
    sensitivity: SensitivityCategory
    evidence_ids: tuple[EvidenceItemId, ...]
    source_message_ids: tuple[MessageId, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedCandidateGroup:
    """One case the validated proposal would create or extend.

    A *new* candidate is grouped by the model's ``candidate_group_ref`` -- an ephemeral label
    that names one proposed case within one answer -- and never by issue type. Grouping by
    issue type was wrong in a way that only shows up on real input: two unrelated problems
    the vocabulary can only call ``OTHER``, a plumbing complaint and a garage gate, collapsed
    into a single case whose title described one of them and whose reports described both.

    The label separates groups; it does not licence one. A shared label is the model asserting
    relatedness, and an assertion is not a proof, so a group only ever reaches two members
    under an issue type that names what went wrong (ADR-012). A group whose issue type is
    ``OTHER`` may therefore exist here with exactly one member, and one member never reaches a
    case: :data:`~chorus.application.services.monitor_apply.MIN_REPORTS_FOR_NEW_CANDIDATE` is
    two.

    The label itself is discarded here. It survives only as far as this dataclass, which
    exists so the planner can tell one proposed group from another; durable case identity is
    still derived from the validated reports, so nothing the model wrote reaches an address.

    ``expected_case_version`` is the version of an existing case *as the agent saw it in its
    own input*. It is retained rather than refreshed at apply time, because the whole question
    at apply time is whether the case still looks the way the reasoning assumed.
    """

    existing_case_id: CaseId | None
    expected_case_version: int | None
    group_ref: str | None
    issue_type: str
    title: str
    report_client_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if (self.existing_case_id is None) == (self.group_ref is None):
            raise ValueError("a validated group names exactly one of case ID and group ref")
        if (self.existing_case_id is None) != (self.expected_case_version is None):
            raise ValueError("an existing-case group carries exactly one expected version")


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedMonitorOutput:
    """Everything deterministic code is prepared to act on from one Monitor answer."""

    reports: tuple[ValidatedReport, ...]
    facts: tuple[ValidatedFact, ...]
    groups: tuple[ValidatedCandidateGroup, ...]
    noise_message_ids: tuple[MessageId, ...]
    policy_like_message_ids: tuple[MessageId, ...]


class _Rejections:
    """Collects reasons so one pass reports every distinct failure it found."""

    __slots__ = ("_reasons",)

    def __init__(self) -> None:
        self._reasons: list[AgentRejection] = []

    def add(self, reason: AgentRejection) -> None:
        self._reasons.append(reason)

    def raise_if_any(self) -> None:
        if self._reasons:
            raise AgentContractViolationError(tuple(self._reasons))


def validate_monitor_result(
    *,
    invocation: MonitorInvocation,
    result: MonitorResult,
    namespace: Namespace,
    contributor_by_pseudonym: dict[str, ContributorId],
) -> ValidatedMonitorOutput:
    """Validate one Monitor answer end to end, or refuse all of it."""

    rejections = _Rejections()
    _validate_envelope(invocation, result, namespace, rejections)
    # The envelope decides whether this answer belongs to this invocation at all, so a
    # mismatch stops the pass before any citation is interpreted against the wrong input.
    rejections.raise_if_any()

    payload = invocation.payload
    output = result.output
    messages_by_id = {message.message_id: message for message in payload.messages}
    summaries_by_case = {summary.case_id: summary for summary in payload.candidate_case_summaries}

    _validate_message_coverage(output, messages_by_id, rejections)

    reports = _validate_reports(
        output=output,
        messages_by_id=messages_by_id,
        allowed_issue_types={issue.value for issue in payload.allowed_issue_types},
        contributor_by_pseudonym=contributor_by_pseudonym,
        rejections=rejections,
    )
    reports_by_ref = {report.client_ref: report for report in reports}

    facts = _validate_facts(
        output=output,
        messages_by_id=messages_by_id,
        reports_by_ref=reports_by_ref,
        rejections=rejections,
    )
    _validate_sensitive_signals(output, messages_by_id, rejections)
    _validate_auxiliary_references(output, set(reports_by_ref), facts, rejections)

    groups = _validate_candidate_links(
        output=output,
        reports_by_ref=reports_by_ref,
        summaries_by_case=summaries_by_case,
        rejections=rejections,
    )
    _validate_bounds(reports, facts, rejections)
    rejections.raise_if_any()

    return ValidatedMonitorOutput(
        reports=reports,
        facts=facts,
        groups=groups,
        noise_message_ids=_classified(output, MessageClassification.NOISE),
        policy_like_message_ids=_classified(output, MessageClassification.POLICY_LIKE_INSTRUCTION),
    )


def _classified(
    output: MonitorOutput, classification: MessageClassification
) -> tuple[MessageId, ...]:
    return tuple(
        MessageId(item.message_id)
        for item in output.message_results
        if item.classification is classification
    )


def _validate_envelope(
    invocation: MonitorInvocation,
    result: MonitorResult,
    namespace: Namespace,
    rejections: _Rejections,
) -> None:
    if (
        result.invocation_id != invocation.invocation_id
        or result.namespace != namespace.value
        or result.namespace != invocation.namespace
        or result.agent_name is not AgentName.MONITOR
        or invocation.agent_name is not AgentName.MONITOR
        or result.case_id != invocation.case_id
        or result.case_version != invocation.case_version
    ):
        rejections.add(AgentRejection.ENVELOPE_MISMATCH)
    if result.prompt_version != MONITOR_PROMPT_VERSION:
        # The request never named a prompt version, so this compares the runtime's answer
        # against the one prompt this application reviewed -- not against something the
        # caller asked for and the runtime could simply have echoed back.
        rejections.add(AgentRejection.PROMPT_VERSION_MISMATCH)


def _validate_message_coverage(
    output: MonitorOutput,
    messages_by_id: dict[UUID, MonitorMessage],
    rejections: _Rejections,
) -> None:
    """Every input message is classified exactly once, and nothing else is."""

    seen: set[UUID] = set()
    for item in output.message_results:
        if item.message_id not in messages_by_id:
            rejections.add(AgentRejection.UNKNOWN_MESSAGE_ID)
            continue
        seen.add(item.message_id)
    if seen != set(messages_by_id):
        rejections.add(AgentRejection.MESSAGE_RESULT_COVERAGE)


def _validate_reports(
    *,
    output: MonitorOutput,
    messages_by_id: dict[UUID, MonitorMessage],
    allowed_issue_types: set[str],
    contributor_by_pseudonym: dict[str, ContributorId],
    rejections: _Rejections,
) -> tuple[ValidatedReport, ...]:
    validated: list[ValidatedReport] = []
    claimed_messages: set[UUID] = set()
    for report in output.proposed_reports:
        cited: list[MonitorMessage] = []
        unknown = False
        for message_id in report.message_ids:
            message = messages_by_id.get(message_id)
            if message is None:
                unknown = True
                continue
            if message_id in claimed_messages:
                # One message supporting two reports would count one person saying one thing
                # twice, which is how corroboration gets inflated further downstream.
                rejections.add(AgentRejection.DUPLICATE_CITATION)
            claimed_messages.add(message_id)
            cited.append(message)
        if unknown or not cited:
            rejections.add(AgentRejection.UNKNOWN_MESSAGE_ID)
            continue

        owners = {message.contributor_pseudonym_id for message in cited}
        contributor_id = contributor_by_pseudonym.get(report.contributor_pseudonym_id)
        if owners != {report.contributor_pseudonym_id} or contributor_id is None:
            rejections.add(AgentRejection.SOURCE_OWNERSHIP_INVALID)
            continue
        if report.issue_type.value not in allowed_issue_types:
            rejections.add(AgentRejection.UNSUPPORTED_ISSUE_TYPE)
            continue
        if report.occurred_at is not None and not _within_observation_window(
            report.occurred_at, cited
        ):
            rejections.add(AgentRejection.TIMESTAMP_OUT_OF_RANGE)
            continue

        ordered = sorted(cited, key=lambda item: (item.sent_at, str(item.message_id)))
        validated.append(
            ValidatedReport(
                client_ref=report.client_ref,
                contributor_id=contributor_id,
                issue_type=report.issue_type.value,
                summary=report.summary,
                occurred_at=report.occurred_at,
                location_area=report.location_area,
                source_message_ids=tuple(MessageId(item.message_id) for item in ordered),
                evidence_ids=tuple(
                    EvidenceItemId(descriptor.evidence_id)
                    for item in ordered
                    for descriptor in item.attachment_descriptors
                ),
            )
        )
    return tuple(validated)


def _within_observation_window(occurred_at: datetime, cited: list[MonitorMessage]) -> bool:
    earliest = min(message.sent_at for message in cited)
    latest = max(message.sent_at for message in cited)
    return earliest - OCCURRENCE_BACKDATE_WINDOW <= occurred_at <= latest


def _validate_facts(
    *,
    output: MonitorOutput,
    messages_by_id: dict[UUID, MonitorMessage],
    reports_by_ref: dict[str, ValidatedReport],
    rejections: _Rejections,
) -> tuple[ValidatedFact, ...]:
    validated: list[ValidatedFact] = []
    for fact in output.proposed_facts:
        report = reports_by_ref.get(fact.report_client_ref)
        if report is None:
            rejections.add(AgentRejection.UNKNOWN_CLIENT_REF)
            continue
        owned_message_ids = {identifier.value for identifier in report.source_message_ids}
        if not _validate_spans(fact.source_spans, messages_by_id, owned_message_ids, rejections):
            continue
        allowed_evidence = {
            descriptor.evidence_id
            for message_id in owned_message_ids
            for descriptor in messages_by_id[message_id].attachment_descriptors
        }
        if not set(fact.evidence_ids) <= allowed_evidence:
            rejections.add(AgentRejection.UNKNOWN_EVIDENCE_ID)
            continue
        required = REQUIRED_SENSITIVITY.get(fact.fact_type)
        if required is not None and fact.sensitivity is not required:
            rejections.add(AgentRejection.SENSITIVITY_MISMATCH)
            continue
        try:
            value = to_domain_value(fact)
        except ValueError:
            rejections.add(AgentRejection.UNSUPPORTED_FACT_TYPE)
            continue
        validated.append(
            ValidatedFact(
                client_ref=fact.client_ref,
                report_client_ref=fact.report_client_ref,
                fact_type=fact.fact_type,
                value=value,
                sensitivity=fact.sensitivity,
                evidence_ids=tuple(
                    EvidenceItemId(item) for item in sorted(fact.evidence_ids, key=str)
                ),
                source_message_ids=tuple(
                    MessageId(message_id)
                    for message_id in sorted(
                        {span.message_id for span in fact.source_spans}, key=str
                    )
                ),
            )
        )
    return tuple(validated)


def _validate_spans(
    spans: tuple[MonitorSourceSpan, ...],
    messages_by_id: dict[UUID, MonitorMessage],
    owned_message_ids: set[UUID],
    rejections: _Rejections,
) -> bool:
    """Prove each quotation exists, at those offsets, in a message the report owns."""

    for span in spans:
        message = messages_by_id.get(span.message_id)
        if message is None:
            rejections.add(AgentRejection.UNKNOWN_MESSAGE_ID)
            return False
        if span.message_id not in owned_message_ids:
            rejections.add(AgentRejection.SOURCE_OWNERSHIP_INVALID)
            return False
        if span.end > len(message.text) or message.text[span.start : span.end] != span.quote:
            rejections.add(AgentRejection.SOURCE_SPAN_INVALID)
            return False
    return True


def _validate_sensitive_signals(
    output: MonitorOutput,
    messages_by_id: dict[UUID, MonitorMessage],
    rejections: _Rejections,
) -> None:
    for signal in output.sensitive_signals:
        message = messages_by_id.get(signal.message_id)
        span = signal.source_span
        if message is None or span.message_id != signal.message_id:
            rejections.add(AgentRejection.UNKNOWN_MESSAGE_ID)
            continue
        if span.end > len(message.text) or message.text[span.start : span.end] != span.quote:
            rejections.add(AgentRejection.SOURCE_SPAN_INVALID)


def _validate_auxiliary_references(
    output: MonitorOutput,
    report_refs: set[str],
    facts: tuple[ValidatedFact, ...],
    rejections: _Rejections,
) -> None:
    fact_refs = {fact.client_ref for fact in facts}
    for request in output.missing_information_requests:
        if request.report_client_ref not in report_refs:
            rejections.add(AgentRejection.UNKNOWN_CLIENT_REF)
    for suggestion in output.mandate_suggestions:
        if suggestion.report_client_ref not in report_refs:
            rejections.add(AgentRejection.UNKNOWN_CLIENT_REF)
            continue
        if not set(suggestion.fact_client_refs) <= fact_refs:
            rejections.add(AgentRejection.UNKNOWN_CLIENT_REF)


@dataclass(slots=True)
class _FreshGroup:
    """Accumulator for one proposed new case, keyed by its model-local group reference."""

    issue_type: str
    title: str
    report_client_refs: list[str]


def _validate_candidate_links(
    *,
    output: MonitorOutput,
    reports_by_ref: dict[str, ValidatedReport],
    summaries_by_case: dict[UUID, MonitorCandidateSummary],
    rejections: _Rejections,
) -> tuple[ValidatedCandidateGroup, ...]:
    """Resolve every link into exactly one group, or refuse the whole answer.

    Three invariants make grouping trustworthy rather than merely plausible.

    A link naming an existing case must name one that was in this invocation's own input, so a
    case identifier can never be invented or borrowed from another community.

    A link naming a new group must agree with every other link in that group about what the
    group *is* -- same issue type, same proposed title -- because a group whose members
    disagree describes no single case, and quietly picking one member's answer would file the
    others under a title that does not describe them.

    And a case may hold two reports only under an issue type that names a subject (ADR-012).
    Agreement is not relatedness: two links agreeing on ``OTHER`` and on one vague title are
    two links the model wrote to agree, and nothing in the input contradicts them. The rule is
    the same whether the second report starts a case or joins one that already exists --
    the harm is identical, and a rule that governed only creation would be a rule an extending
    answer could simply wait one batch to escape.
    """

    linked_refs: set[str] = set()
    existing: dict[UUID, list[str]] = {}
    fresh: dict[str, _FreshGroup] = {}

    for link in output.candidate_links:
        report = reports_by_ref.get(link.report_client_ref)
        if report is None:
            rejections.add(AgentRejection.UNKNOWN_CLIENT_REF)
            continue
        linked_refs.add(link.report_client_ref)
        if link.existing_case_id is not None:
            if link.candidate_group_ref is not None:  # pragma: no cover - contract enforces
                rejections.add(AgentRejection.CANDIDATE_GROUP_INVALID)
                continue
            summary = summaries_by_case.get(link.existing_case_id)
            if summary is None:
                rejections.add(AgentRejection.FOREIGN_CASE_ID)
                continue
            if summary.issue_type.value != report.issue_type:
                rejections.add(AgentRejection.UNSUPPORTED_CANDIDATE_TRANSITION)
                continue
            if not issue_type_names_a_subject(summary.issue_type.value):
                # Extending is merging: the case already holds a report, so this makes two.
                # Under an issue type that names nothing there is no way to prove the two
                # describe one incident, and the case being offered as a candidate proves
                # only that one of its messages was in the recent window.
                rejections.add(AgentRejection.CANDIDATE_GROUP_UNPROVABLE)
                continue
            existing.setdefault(link.existing_case_id, []).append(link.report_client_ref)
            continue

        group_ref = link.candidate_group_ref
        if group_ref is None:  # pragma: no cover - contract enforces
            rejections.add(AgentRejection.CANDIDATE_GROUP_INVALID)
            continue
        group = fresh.get(group_ref)
        if group is None:
            # A group of one is not a merge, so a lone ``OTHER`` report is allowed to name a
            # group here. It simply never reaches a case: the creation guard needs two.
            fresh[group_ref] = _FreshGroup(
                issue_type=report.issue_type,
                title=link.proposed_case_title,
                report_client_refs=[link.report_client_ref],
            )
            continue
        if group.issue_type != report.issue_type or group.title != link.proposed_case_title:
            rejections.add(AgentRejection.CANDIDATE_GROUP_INCONSISTENT)
            continue
        if not issue_type_names_a_subject(group.issue_type):
            # This link would make the group a merge. Refusing here, before anything is
            # derived from it, is what keeps two genuinely different incidents from ever
            # sharing a candidate case.
            rejections.add(AgentRejection.CANDIDATE_GROUP_UNPROVABLE)
            continue
        group.report_client_refs.append(link.report_client_ref)

    if set(reports_by_ref) - linked_refs:
        rejections.add(AgentRejection.UNLINKED_REPORT)

    groups: list[ValidatedCandidateGroup] = []
    for case_id, refs in sorted(existing.items(), key=lambda item: str(item[0])):
        summary = summaries_by_case[case_id]
        groups.append(
            ValidatedCandidateGroup(
                existing_case_id=CaseId(case_id),
                # The version the agent was shown, carried forward untouched. Refreshing it
                # here would let the apply step compare the case against itself and call that
                # agreement.
                expected_case_version=summary.case_version,
                group_ref=None,
                issue_type=summary.issue_type.value,
                title=summary.title,
                report_client_refs=tuple(sorted(refs)),
            )
        )
    for group_ref, group in sorted(fresh.items()):
        groups.append(
            ValidatedCandidateGroup(
                existing_case_id=None,
                expected_case_version=None,
                group_ref=group_ref,
                issue_type=group.issue_type,
                title=group.title,
                report_client_refs=tuple(sorted(group.report_client_refs)),
            )
        )
    return tuple(groups)


def _validate_bounds(
    reports: tuple[ValidatedReport, ...],
    facts: tuple[ValidatedFact, ...],
    rejections: _Rejections,
) -> None:
    if len(facts) > MAX_ACTIVE_FACTS_PER_CASE or len(reports) > MAX_ACTIVE_FACTS_PER_CASE:
        rejections.add(AgentRejection.OUTPUT_EXCEEDS_BOUNDS)


def to_domain_value(fact: ProposedFact) -> FactValue:
    """Map one contract value onto its closed domain variant.

    The mapping is exhaustive over the contract union. A new contract variant that nobody
    mapped raises here instead of quietly persisting as something else.
    """

    value: MonitorFactValue = fact.typed_value
    match value:
        case IncidentOccurrenceValue():
            return IncidentOccurrence(
                occurred_at=value.occurred_at, failure_mode=value.failure_mode
            )
        case ServiceImpactValue():
            return ServiceImpact(impact_code=value.impact_code, summary=value.summary)
        case LocationAreaValue():
            return LocationArea(area=value.area)
        case IdentityAttributeValue():
            return IdentityAttribute(display_name=value.display_name)
        case UnitLocationValue():
            return UnitLocation(unit_label=value.unit_label)
        case HealthDetailValue():
            return HealthDetail(subject_relation=value.subject_relation, detail=value.detail)
        case ManagementStatementValue():
            return ManagementStatement(
                statement=value.statement,
                speaker_org=value.speaker_org,
                stated_at=value.stated_at,
            )
        case EvidenceDescriptionValue():
            return EvidenceDescription(description=value.description, media_kind=value.media_kind)
        case _:
            raise ValueError("unmapped Monitor fact value")
