"""Small in-memory builders for Monitor invocations and answers.

The validator suite needs to vary one thing at a time -- a citation, a span, an owner, an enum
-- against an otherwise valid answer. These builders make that possible without storage, and
without hand-writing a whole output for every negative case.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from chorus.contracts.common import (
    AGENT_INPUT_SCHEMA_VERSION,
    AGENT_OUTPUT_SCHEMA_VERSION,
    MONITOR_PROMPT_VERSION,
    AgentInputEnvelope,
    AgentName,
    AgentResultEnvelope,
)
from chorus.contracts.monitor import (
    CandidateLink,
    IncidentOccurrenceValue,
    IssueType,
    MessageClassification,
    MonitorAttachmentDescriptor,
    MonitorCandidateSummary,
    MonitorInput,
    MonitorMessage,
    MonitorMessageResult,
    MonitorOutput,
    MonitorSourceSpan,
    ProposedFact,
    ProposedReport,
)
from chorus.domain.entities import FactType, SensitivityCategory
from chorus.domain.facts import FailureMode, LocationAreaCode
from chorus.domain.ids import ContributorId, Namespace
from chorus.ports.agents import MonitorInvocation, MonitorResult

NAMESPACE = Namespace("TEST_VALIDATOR")
SEED = UUID("f0a1a4c3-52f9-5a1d-9b2e-6c7d8e9f0a1b")
MODEL_PROFILE_HASH = "sha256:" + "a" * 64

CONTRIBUTORS: dict[str, ContributorId] = {
    "resident-a": ContributorId(uuid5(SEED, "contributor:resident-a")),
    "resident-b": ContributorId(uuid5(SEED, "contributor:resident-b")),
}

MESSAGE_TEXTS = {
    "feed-100": "The lift stopped between floors again this morning.",
    "feed-101": "It was out of service when I got home this evening.",
    "feed-102": "Reminder that the bin collection moves to Thursday.",
}
MESSAGE_OWNERS = {
    "feed-100": "resident-a",
    "feed-101": "resident-b",
    "feed-102": "resident-a",
}


def message_id(channel_message_id: str) -> UUID:
    return uuid5(SEED, f"message:{channel_message_id}")


def evidence_id(fixture: str) -> UUID:
    return uuid5(SEED, f"evidence:{fixture}")


def case_id(name: str) -> UUID:
    return uuid5(SEED, f"case:{name}")


def build_messages(*, with_attachment: bool = False) -> tuple[MonitorMessage, ...]:
    messages: list[MonitorMessage] = []
    for index, (channel_message_id, text) in enumerate(MESSAGE_TEXTS.items()):
        descriptors: tuple[MonitorAttachmentDescriptor, ...] = ()
        if with_attachment and channel_message_id == "feed-101":
            descriptors = (
                MonitorAttachmentDescriptor(
                    evidence_id=evidence_id("photo"),
                    media_type="image/jpeg",
                    safe_caption="A photograph of a control panel.",
                ),
            )
        messages.append(
            MonitorMessage(
                message_id=message_id(channel_message_id),
                channel_message_id=channel_message_id,
                contributor_pseudonym_id=MESSAGE_OWNERS[channel_message_id],
                sent_at=datetime(2030, 1, 8 + index, 9, 0, 0, tzinfo=UTC),
                text=text,
                attachment_descriptors=descriptors,
            )
        )
    return tuple(messages)


def build_invocation(
    *,
    messages: tuple[MonitorMessage, ...] | None = None,
    summaries: tuple[MonitorCandidateSummary, ...] = (),
) -> MonitorInvocation:
    """Build one request envelope.

    There is no ``prompt_version`` to supply: the request does not name one. Which prompt ran
    is something the *runtime* states in its answer, and a test that wants a wrong one sets it
    on the result through :func:`build_result`.
    """

    return AgentInputEnvelope[MonitorInput](
        schema_version=AGENT_INPUT_SCHEMA_VERSION,
        invocation_id=uuid5(SEED, "invocation:1"),
        namespace=NAMESPACE.value,
        agent_name=AgentName.MONITOR,
        case_id=None,
        case_version=None,
        requested_at=datetime(2030, 1, 14, 9, 0, 0, tzinfo=UTC),
        policy_version="policy/v1",
        payload=MonitorInput(
            messages=messages if messages is not None else build_messages(),
            candidate_case_summaries=summaries,
        ),
    )


LIFT_GROUP_REF = "lift-group"
"""The model-local label naming the one new case these fixture answers propose."""


def span_for(
    message: MonitorMessage, *, start: int = 0, length: int | None = None
) -> MonitorSourceSpan:
    end = len(message.text) if length is None else start + length
    return MonitorSourceSpan(
        message_id=message.message_id,
        start=start,
        end=end,
        quote=message.text[start:end],
    )


def build_output(invocation: MonitorInvocation) -> MonitorOutput:
    """A valid answer: two linked reports drawn from the two failure messages."""

    messages = {message.channel_message_id: message for message in invocation.payload.messages}
    signal_ids = ("feed-100", "feed-101")
    results = tuple(
        MonitorMessageResult(
            message_id=message.message_id,
            classification=(
                MessageClassification.POSSIBLE_ISSUE_SIGNAL
                if message.channel_message_id in signal_ids
                else MessageClassification.NOISE
            ),
            reason="fixture classification",
        )
        for message in invocation.payload.messages
    )
    reports: list[ProposedReport] = []
    facts: list[ProposedFact] = []
    links: list[CandidateLink] = []
    for index, channel_message_id in enumerate(signal_ids, start=1):
        message = messages[channel_message_id]
        report_ref = f"report-{index}"
        reports.append(
            ProposedReport(
                client_ref=report_ref,
                message_ids=(message.message_id,),
                contributor_pseudonym_id=message.contributor_pseudonym_id,
                issue_type=IssueType.ELEVATOR_FAILURE,
                summary=message.text,
                occurred_at=message.sent_at,
                location_area=LocationAreaCode.ELEVATOR_CAB,
            )
        )
        facts.append(
            ProposedFact(
                client_ref=f"fact-{index}",
                report_client_ref=report_ref,
                fact_type=FactType.INCIDENT_OCCURRENCE,
                typed_value=IncidentOccurrenceValue(
                    fact_type=FactType.INCIDENT_OCCURRENCE,
                    occurred_at=message.sent_at,
                    failure_mode=FailureMode.OUT_OF_SERVICE,
                ),
                sensitivity=SensitivityCategory.GENERAL,
                source_spans=(span_for(message),),
            )
        )
        links.append(
            CandidateLink(
                report_client_ref=report_ref,
                candidate_group_ref=LIFT_GROUP_REF,
                proposed_case_title="Recurring lift failures",
                similarity_reasons=("same equipment, repeated over days",),
                confidence="0.7",
            )
        )
    return MonitorOutput(
        message_results=results,
        proposed_reports=tuple(reports),
        proposed_facts=tuple(facts),
        candidate_links=tuple(links),
    )


def build_result(
    invocation: MonitorInvocation,
    output: MonitorOutput,
    *,
    prompt_version: str = MONITOR_PROMPT_VERSION,
) -> MonitorResult:
    started = datetime(2030, 1, 14, 9, 0, 1, tzinfo=UTC)
    return AgentResultEnvelope[MonitorOutput](
        schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
        invocation_id=invocation.invocation_id,
        namespace=invocation.namespace,
        agent_name=AgentName.MONITOR,
        case_id=invocation.case_id,
        case_version=invocation.case_version,
        model_profile_arn_hash=MODEL_PROFILE_HASH,
        prompt_version=prompt_version,
        started_at=started,
        completed_at=started,
        output=output,
    )


@dataclass(frozen=True, slots=True)
class ValidatorCase:
    """One invocation and answer pair, ready for the validator."""

    invocation: MonitorInvocation
    output: MonitorOutput

    @property
    def result(self) -> MonitorResult:
        return build_result(self.invocation, self.output)


def valid_case(**kwargs: object) -> ValidatorCase:
    invocation = build_invocation(**kwargs)  # type: ignore[arg-type]
    return ValidatorCase(invocation=invocation, output=build_output(invocation))
