"""Scripted Monitor answers, shared by every suite that needs one.

These build *valid* output: every message classified exactly once, every report owned by the
contributor who sent the messages it cites, every fact anchored to a real span. That is
deliberate. A scripted answer whose only job is to be rejected proves the validator works; an
answer that is accepted is what lets a test say something about apply, resume, capacity, and
concurrency -- which is where the interesting failures live.

They live in a fixture module rather than in whichever suite happened to need them first, so
two suites cannot drift into two slightly different notions of "a well-formed answer".
"""

from __future__ import annotations

from uuid import UUID

from chorus.contracts.monitor import (
    CandidateLink,
    EvidenceDescriptionValue,
    IncidentOccurrenceValue,
    IssueType,
    LocationAreaValue,
    ManagementStatementValue,
    MessageClassification,
    MonitorFactValue,
    MonitorInput,
    MonitorMessage,
    MonitorMessageResult,
    MonitorOutput,
    MonitorSourceSpan,
    ProposedFact,
    ProposedReport,
    ServiceImpactValue,
)
from chorus.domain.entities import FactType, SensitivityCategory
from chorus.domain.facts import (
    EvidenceMediaKind,
    FailureMode,
    ImpactCode,
    LocationAreaCode,
)

type GroupSpec = tuple[str, IssueType, str, tuple[int, ...]]

MAX_QUOTE = 500


def whole_span(message: MonitorMessage) -> MonitorSourceSpan:
    quote = message.text[:MAX_QUOTE]
    return MonitorSourceSpan(message_id=message.message_id, start=0, end=len(quote), quote=quote)


def classify_all(payload: MonitorInput, signals: set[UUID]) -> tuple[MonitorMessageResult, ...]:
    return tuple(
        MonitorMessageResult(
            message_id=message.message_id,
            classification=(
                MessageClassification.POSSIBLE_ISSUE_SIGNAL
                if message.message_id in signals
                else MessageClassification.NOISE
            ),
            reason="scripted classification",
        )
        for message in payload.messages
    )


def report_for(message: MonitorMessage, ref: str, issue: IssueType) -> ProposedReport:
    return ProposedReport(
        client_ref=ref,
        message_ids=(message.message_id,),
        contributor_pseudonym_id=message.contributor_pseudonym_id,
        issue_type=issue,
        summary=message.text[:1_000],
        occurred_at=message.sent_at,
        location_area=LocationAreaCode.ELEVATOR_CAB,
    )


def fact_for(
    message: MonitorMessage,
    ref: str,
    report_ref: str,
    *,
    failure_mode: FailureMode = FailureMode.STUCK,
) -> ProposedFact:
    return ProposedFact(
        client_ref=ref,
        report_client_ref=report_ref,
        fact_type=FactType.INCIDENT_OCCURRENCE,
        typed_value=IncidentOccurrenceValue(
            fact_type=FactType.INCIDENT_OCCURRENCE,
            occurred_at=message.sent_at,
            failure_mode=failure_mode,
        ),
        sensitivity=SensitivityCategory.GENERAL,
        source_spans=(whole_span(message),),
    )


def grouped_answer(
    payload: MonitorInput,
    groups: tuple[GroupSpec, ...],
    *,
    failure_mode: FailureMode = FailureMode.STUCK,
) -> MonitorOutput:
    """Build one answer that files the given message indices into the given groups."""

    reports: list[ProposedReport] = []
    facts: list[ProposedFact] = []
    links: list[CandidateLink] = []
    signals: set[UUID] = set()
    for group_ref, issue, title, indices in groups:
        for index in indices:
            message = payload.messages[index]
            signals.add(message.message_id)
            report_ref = f"report-{index:03d}"
            reports.append(report_for(message, report_ref, issue))
            facts.append(
                fact_for(message, f"fact-{index:03d}", report_ref, failure_mode=failure_mode)
            )
            links.append(
                CandidateLink(
                    report_client_ref=report_ref,
                    candidate_group_ref=group_ref,
                    proposed_case_title=title,
                    similarity_reasons=("scripted grouping",),
                    confidence="0.8",
                )
            )
    return MonitorOutput(
        message_results=classify_all(payload, signals),
        proposed_reports=tuple(reports),
        proposed_facts=tuple(facts),
        candidate_links=tuple(links),
    )


THREE_GROUPS: tuple[GroupSpec, ...] = (
    ("lift-group", IssueType.ELEVATOR_FAILURE, "Recurring lift failures", (1, 4)),
    ("north-lift-group", IssueType.ELEVATOR_FAILURE, "North tower lift out of service", (0, 2)),
    ("service-lift-group", IssueType.ELEVATOR_FAILURE, "Service lift door faults", (6, 8)),
)
"""Three separate problems that share one issue type, kept apart only by their group refs.

Every member is ``ELEVATOR_FAILURE`` on purpose. Grouping by issue type would collapse all
three into one case, so an answer that still yields three cases is proving that the group
reference -- and nothing else -- is what separates them (B-1).

They are not ``OTHER``, also on purpose. Under ADR-012 two reports reach one case only under an
issue type that names a subject, so an ``OTHER`` group can never have a second member and can
never become a case. A fixture that produced three cases from ``OTHER`` groups would be
asserting the defect this suite exists to prevent; :data:`UNPROVABLE_OTHER_GROUP` is the
fixture for the refusal itself.
"""

UNPROVABLE_OTHER_GROUP: tuple[GroupSpec, ...] = (
    ("vague-group", IssueType.OTHER, "General building issue", (0, 2)),
)
"""Two unrelated reports the vocabulary can only call ``OTHER``, filed under one vague title.

Exactly the merge ADR-012 refuses: issue type agrees, title agrees, and neither agreement is
evidence of anything, because the model chose both.
"""


# ---------------------------------------------------------------------------------------
# Multi-fact answers, for capacity, resume, and concurrency scenarios
# ---------------------------------------------------------------------------------------

UNRESTRICTED_FACT_TYPES: tuple[FactType, ...] = (
    FactType.INCIDENT_OCCURRENCE,
    FactType.SERVICE_IMPACT,
    FactType.LOCATION_AREA,
    FactType.MANAGEMENT_STATEMENT,
    FactType.EVIDENCE_DESCRIPTION,
)
"""The fact types intake may assert without a protected sensitivity category.

Five of them, which is what makes a hundred-fact answer expressible at all: a fact's slot is
its report, its type, and its lineage, so distinct facts need distinct types or distinct
reports rather than distinct wording.
"""


def typed_value(fact_type: FactType, message: MonitorMessage) -> MonitorFactValue:
    """One valid contract value per unrestricted fact type, drawn from the cited message."""

    match fact_type:
        case FactType.INCIDENT_OCCURRENCE:
            return IncidentOccurrenceValue(
                fact_type=FactType.INCIDENT_OCCURRENCE,
                occurred_at=message.sent_at,
                failure_mode=FailureMode.STUCK,
            )
        case FactType.SERVICE_IMPACT:
            return ServiceImpactValue(
                fact_type=FactType.SERVICE_IMPACT,
                impact_code=ImpactCode.ACCESS_BLOCKED,
                summary="stairs only while the lift is out",
            )
        case FactType.LOCATION_AREA:
            return LocationAreaValue(
                fact_type=FactType.LOCATION_AREA, area=LocationAreaCode.ELEVATOR_CAB
            )
        case FactType.MANAGEMENT_STATEMENT:
            return ManagementStatementValue(
                fact_type=FactType.MANAGEMENT_STATEMENT,
                statement="the contractor was called",
                speaker_org="building management",
                stated_at=message.sent_at,
            )
        case FactType.EVIDENCE_DESCRIPTION:
            return EvidenceDescriptionValue(
                fact_type=FactType.EVIDENCE_DESCRIPTION,
                description="a photograph of the lift door",
                media_kind=EvidenceMediaKind.IMAGE,
            )
        case _:  # pragma: no cover - the tuple above is the closed set
            raise AssertionError("unsupported fact type for a scripted answer")


def typed_fact(message: MonitorMessage, report_ref: str, fact_type: FactType) -> ProposedFact:
    """One proposed fact occupying the slot (report, type, this message, no evidence)."""

    return ProposedFact(
        client_ref=f"{report_ref}-{fact_type.value.lower()}",
        report_client_ref=report_ref,
        fact_type=fact_type,
        typed_value=typed_value(fact_type, message),
        sensitivity=SensitivityCategory.GENERAL,
        source_spans=(whole_span(message),),
    )


def _reports_over(
    payload: MonitorInput, message_ids: tuple[UUID, ...], issue: IssueType
) -> tuple[tuple[MonitorMessage, ProposedReport], ...]:
    by_id = {message.message_id: message for message in payload.messages}
    pairs: list[tuple[MonitorMessage, ProposedReport]] = []
    for index, message_id in enumerate(message_ids):
        message = by_id[message_id]
        pairs.append((message, report_for(message, f"report-{index:03d}", issue)))
    return tuple(pairs)


def _facts_for(
    pairs: tuple[tuple[MonitorMessage, ProposedReport], ...], count: int
) -> tuple[ProposedFact, ...]:
    """Fill ``count`` distinct slots, walking fact types across reports.

    Types vary fastest so a two-report answer can still reach ten facts, and reports vary
    next so a twenty-five-report answer reaches a hundred. Anything beyond that would need
    lineage the input does not contain, which is the honest bound.
    """

    facts: list[ProposedFact] = []
    for fact_type in UNRESTRICTED_FACT_TYPES:
        for message, report in pairs:
            if len(facts) == count:
                return tuple(facts)
            facts.append(typed_fact(message, report.client_ref, fact_type))
    if len(facts) < count:
        raise AssertionError("the scripted answer cannot express that many distinct fact slots")
    return tuple(facts)


def new_case_answer(
    payload: MonitorInput,
    *,
    message_ids: tuple[UUID, ...],
    fact_count: int,
    group_ref: str,
    title: str,
    issue: IssueType = IssueType.ELEVATOR_FAILURE,
) -> MonitorOutput:
    """Propose one new case over exactly ``message_ids``, carrying ``fact_count`` facts."""

    pairs = _reports_over(payload, message_ids, issue)
    return MonitorOutput(
        message_results=classify_all(payload, set(message_ids)),
        proposed_reports=tuple(report for _, report in pairs),
        proposed_facts=_facts_for(pairs, fact_count),
        candidate_links=tuple(
            CandidateLink(
                report_client_ref=report.client_ref,
                candidate_group_ref=group_ref,
                proposed_case_title=title,
                similarity_reasons=("scripted grouping",),
                confidence="0.8",
            )
            for _, report in pairs
        ),
    )


def extension_answer(
    payload: MonitorInput,
    *,
    case_id: UUID,
    message_ids: tuple[UUID, ...],
    fact_count: int,
) -> MonitorOutput:
    """Extend one existing case with reports over ``message_ids`` and ``fact_count`` facts.

    The case version is read out of the invocation own candidate summaries rather than passed
    in, because that is the only version the apply gate will accept: the answer has to
    describe the case as the agent was actually shown it.
    """

    summary = next(item for item in payload.candidate_case_summaries if item.case_id == case_id)
    pairs = _reports_over(payload, message_ids, summary.issue_type)
    return MonitorOutput(
        message_results=classify_all(payload, set(message_ids)),
        proposed_reports=tuple(report for _, report in pairs),
        proposed_facts=_facts_for(pairs, fact_count),
        candidate_links=tuple(
            CandidateLink(
                report_client_ref=report.client_ref,
                existing_case_id=case_id,
                proposed_case_title=summary.title,
                similarity_reasons=("scripted extension",),
                confidence="0.8",
            )
            for _, report in pairs
        ),
    )


def two_case_extension_answer(
    payload: MonitorInput, *, links: tuple[tuple[UUID, UUID], ...]
) -> MonitorOutput:
    """Extend two existing cases in one answer, one message and one fact each.

    Two cases means two apply steps, which is what makes "the case the *second* step expects
    moved underneath it" a scenario that can be written at all.
    """

    by_id = {message.message_id: message for message in payload.messages}
    summaries = {item.case_id: item for item in payload.candidate_case_summaries}
    reports: list[ProposedReport] = []
    facts: list[ProposedFact] = []
    candidate_links: list[CandidateLink] = []
    for index, (case_id, message_id) in enumerate(links):
        message = by_id[message_id]
        ref = f"report-{index:03d}"
        reports.append(report_for(message, ref, summaries[case_id].issue_type))
        facts.append(typed_fact(message, ref, FactType.INCIDENT_OCCURRENCE))
        candidate_links.append(
            CandidateLink(
                report_client_ref=ref,
                existing_case_id=case_id,
                proposed_case_title=summaries[case_id].title,
                similarity_reasons=("scripted extension",),
                confidence="0.8",
            )
        )
    return MonitorOutput(
        message_results=classify_all(payload, {message_id for _, message_id in links}),
        proposed_reports=tuple(reports),
        proposed_facts=tuple(facts),
        candidate_links=tuple(candidate_links),
    )
