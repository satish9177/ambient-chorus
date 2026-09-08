"""Local Monitor adapters: one scripted, one lexical. Neither is ever the demo path.

Both implement :class:`~chorus.ports.agents.MonitorAgentPort`, so the application code under
test is byte-for-byte the code that runs against Bedrock. What changes is only who answers.

``ScriptedMonitorAgent`` answers with whatever a test hands it, including deliberately
malformed answers. It is how the adversarial suite exercises the validator without needing a
model that can be persuaded to hallucinate on demand.

``LexicalFakeMonitorAgent`` is a crude keyword stand-in for local development. It is a *fake
model*, not a fallback detector: the deployed demo rejects ``CHORUS_AGENT_MODE=fake`` at
startup, and nothing in the application consults these keywords. Its purpose is to let a
developer exercise ingestion, validation, persistence, and the feed without Bedrock
credentials -- and to make it obvious in review that the real detection lives in the model,
because this stand-in reads nothing like it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Final
from uuid import UUID

from chorus.contracts.common import (
    AGENT_OUTPUT_SCHEMA_VERSION,
    MONITOR_PROMPT_VERSION,
    AgentName,
    AgentResultEnvelope,
)
from chorus.contracts.monitor import (
    CandidateLink,
    HealthDetailValue,
    IdentityAttributeValue,
    IncidentOccurrenceValue,
    IssueType,
    MessageClassification,
    MonitorMessage,
    MonitorMessageResult,
    MonitorOutput,
    MonitorSourceSpan,
    ProposedFact,
    ProposedReport,
    UnitLocationValue,
)
from chorus.domain.entities import FactType, SensitivityCategory
from chorus.domain.facts import FailureMode, LocationAreaCode, SubjectRelation
from chorus.infrastructure.local.demo_classification import (
    is_policy_like_instruction,
    is_signal_message,
    sensitive_detail_kind,
)
from chorus.ports.agents import AgentError, MonitorInvocation, MonitorResult

FAKE_MODEL_PROFILE_HASH: Final = f"sha256:{sha256(b'fake-monitor-runtime').hexdigest()}"
MAX_QUOTE = 500

_OUT_OF_SERVICE_TERMS: Final = ("out of service", "unavailable", "e42")
_STUCK_TERMS: Final = ("stuck", "stalled", "will not move", "kept opening")
_ERRATIC_TERMS: Final = ("cycling", "doors keep")

LEXICAL_GROUP_REF: Final = "lift-group"
"""The one new-case group this stand-in ever proposes.

A real model distinguishes unrelated problems and gives each its own group reference. This
one only knows how to spot lift language, so everything it finds belongs to a single group --
which is honest about how little it is doing.
"""


@dataclass(slots=True)
class ScriptedMonitorAgent:
    """Answer with an exact, test-supplied output or failure.

    ``responder`` receives the invocation so a test can assert what the application actually
    projected -- which is how "the payload contained no contact detail" becomes a test rather
    than a claim.
    """

    responder: Callable[[MonitorInvocation], MonitorOutput]
    failures: list[AgentError] = field(default_factory=list)
    invocations: list[MonitorInvocation] = field(default_factory=list)
    prompt_version: str = MONITOR_PROMPT_VERSION
    envelope_override: Callable[[MonitorResult], MonitorResult] | None = None

    async def invoke_monitor(self, invocation: MonitorInvocation) -> MonitorResult:
        self.invocations.append(invocation)
        if self.failures:
            raise self.failures.pop(0)
        started = datetime.now(UTC)
        envelope = AgentResultEnvelope[MonitorOutput](
            schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
            invocation_id=invocation.invocation_id,
            namespace=invocation.namespace,
            agent_name=AgentName.MONITOR,
            case_id=invocation.case_id,
            case_version=invocation.case_version,
            model_profile_arn_hash=FAKE_MODEL_PROFILE_HASH,
            prompt_version=self.prompt_version,
            started_at=started,
            completed_at=started,
            output=self.responder(invocation),
        )
        if self.envelope_override is not None:
            return self.envelope_override(envelope)
        return envelope


@dataclass(slots=True)
class LexicalFakeMonitorAgent:
    """A deterministic keyword stand-in for the model, for local development only."""

    invocations: list[MonitorInvocation] = field(default_factory=list)

    async def invoke_monitor(self, invocation: MonitorInvocation) -> MonitorResult:
        self.invocations.append(invocation)
        started = datetime.now(UTC)
        return AgentResultEnvelope[MonitorOutput](
            schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
            invocation_id=invocation.invocation_id,
            namespace=invocation.namespace,
            agent_name=AgentName.MONITOR,
            case_id=invocation.case_id,
            case_version=invocation.case_version,
            model_profile_arn_hash=FAKE_MODEL_PROFILE_HASH,
            prompt_version=MONITOR_PROMPT_VERSION,
            started_at=started,
            completed_at=started,
            output=build_lexical_output(invocation),
        )


def build_lexical_output(invocation: MonitorInvocation) -> MonitorOutput:
    """Build one structurally complete answer from simple keyword matching."""

    payload = invocation.payload
    existing_case_id = (
        payload.candidate_case_summaries[0].case_id if payload.candidate_case_summaries else None
    )

    results: list[MonitorMessageResult] = []
    reports: list[ProposedReport] = []
    facts: list[ProposedFact] = []
    links: list[CandidateLink] = []

    for index, message in enumerate(payload.messages, start=1):
        lowered = message.text.lower()
        if is_policy_like_instruction(message.text):
            results.append(
                MonitorMessageResult(
                    message_id=message.message_id,
                    classification=MessageClassification.POLICY_LIKE_INSTRUCTION,
                    reason="message is addressed to a system rather than to neighbours",
                )
            )
            continue
        detail_kind = sensitive_detail_kind(message.text)
        if detail_kind is not None:
            results.append(
                MonitorMessageResult(
                    message_id=message.message_id,
                    classification=MessageClassification.POSSIBLE_ISSUE_SIGNAL,
                    reason="message names a private detail tied to the reported incident",
                )
            )
            report_ref = f"report-{index:03d}"
            fact_ref = f"fact-{index:03d}"
            reports.append(_detail_report(report_ref, message))
            facts.append(_detail_fact(fact_ref, report_ref, detail_kind, message))
            links.append(_incident_link(report_ref, existing_case_id))
            continue

        if not is_signal_message(message.text):
            results.append(
                MonitorMessageResult(
                    message_id=message.message_id,
                    classification=MessageClassification.NOISE,
                    reason="no equipment failure language present",
                )
            )
            continue

        results.append(
            MonitorMessageResult(
                message_id=message.message_id,
                classification=MessageClassification.POSSIBLE_ISSUE_SIGNAL,
                reason="message describes an equipment failure",
            )
        )
        report_ref = f"report-{index:03d}"
        fact_ref = f"fact-{index:03d}"
        reports.append(
            ProposedReport(
                client_ref=report_ref,
                message_ids=(message.message_id,),
                contributor_pseudonym_id=message.contributor_pseudonym_id,
                issue_type=IssueType.ELEVATOR_FAILURE,
                summary=message.text[:1_000],
                occurred_at=message.sent_at,
                location_area=LocationAreaCode.ELEVATOR_CAB,
                confidence_basis=("keyword stand-in for a model reading",),
            )
        )
        facts.append(
            ProposedFact(
                client_ref=fact_ref,
                report_client_ref=report_ref,
                fact_type=FactType.INCIDENT_OCCURRENCE,
                typed_value=IncidentOccurrenceValue(
                    fact_type=FactType.INCIDENT_OCCURRENCE,
                    occurred_at=message.sent_at,
                    failure_mode=_failure_mode(lowered),
                ),
                sensitivity=SensitivityCategory.GENERAL,
                evidence_ids=tuple(
                    descriptor.evidence_id for descriptor in message.attachment_descriptors
                ),
                source_spans=(_whole_message_span(message),),
            )
        )
        links.append(_incident_link(report_ref, existing_case_id))

    return MonitorOutput(
        message_results=tuple(results),
        proposed_reports=tuple(reports),
        proposed_facts=tuple(facts),
        candidate_links=tuple(links),
    )


def _whole_message_span(message: MonitorMessage) -> MonitorSourceSpan:
    quote = message.text[:MAX_QUOTE]
    return MonitorSourceSpan(message_id=message.message_id, start=0, end=len(quote), quote=quote)


def _failure_mode(lowered: str) -> FailureMode:
    if any(term in lowered for term in _OUT_OF_SERVICE_TERMS):
        return FailureMode.OUT_OF_SERVICE
    if any(term in lowered for term in _STUCK_TERMS):
        return FailureMode.STUCK
    if any(term in lowered for term in _ERRATIC_TERMS):
        return FailureMode.ERRATIC
    return FailureMode.UNKNOWN


def _incident_link(report_ref: str, existing_case_id: UUID | None) -> CandidateLink:
    """The one grouping decision this stand-in makes, shared by every report it proposes.

    Every report this stand-in emits -- an equipment signal or a private detail (P2-8) -- is
    read as part of the same reported incident, so all of them link into the one candidate
    group (or the one existing case, once there is one).
    """

    return CandidateLink(
        report_client_ref=report_ref,
        existing_case_id=existing_case_id,
        # Exactly one of the two is set. A stand-in that got this wrong would be exercising
        # the contract's own validator rather than the application's.
        candidate_group_ref=None if existing_case_id is not None else LEXICAL_GROUP_REF,
        proposed_case_title="Recurring lift failures",
        similarity_reasons=("repeated equipment failure in the same building",),
        confidence="0.6",
    )


def _detail_report(report_ref: str, message: MonitorMessage) -> ProposedReport:
    """A report for a private detail is still about the same incident conversation.

    ``issue_type``/``location_area`` are the same as the equipment-signal report: the detail
    was mentioned in the course of reporting the elevator problem, not as an unrelated topic.
    """

    return ProposedReport(
        client_ref=report_ref,
        message_ids=(message.message_id,),
        contributor_pseudonym_id=message.contributor_pseudonym_id,
        issue_type=IssueType.ELEVATOR_FAILURE,
        summary=message.text[:1_000],
        occurred_at=message.sent_at,
        location_area=LocationAreaCode.ELEVATOR_CAB,
        confidence_basis=("keyword stand-in for a private-detail reading",),
    )


_REQUIRED_DETAIL_SENSITIVITY: Final = {
    "HEALTH_DETAIL": SensitivityCategory.HEALTH,
    "IDENTITY_ATTRIBUTE": SensitivityCategory.IDENTITY,
    "UNIT_LOCATION": SensitivityCategory.UNIT_LOCATION,
}
"""Mirrors ``chorus.domain.facts.REQUIRED_SENSITIVITY`` for the three kinds this stand-in
proposes -- restated rather than imported, because this file must never import the deterministic
compiler/policy layer, only the contract and domain-entity types every Monitor adapter uses."""


def _extract_after(text: str, marker: str) -> str | None:
    """The first word following ``marker`` in ``text``, stripped of trailing punctuation."""

    lowered = text.lower()
    position = lowered.find(marker)
    if position == -1:
        return None
    remainder = text[position + len(marker) :].strip()
    if not remainder:
        return None
    word = remainder.split(maxsplit=1)[0]
    return word.strip(".,!?;:")


def _detail_fact(
    fact_ref: str, report_ref: str, kind: str, message: MonitorMessage
) -> ProposedFact:
    """Build the one typed private-detail fact this stand-in recognizes.

    Extraction is as crude as the rest of this stand-in -- the first word after a fixed marker
    -- because it exists to exercise the mandate/compile boundary against a real typed fact,
    not to demonstrate name or unit extraction.
    """

    if kind == "HEALTH_DETAIL":
        typed_value: HealthDetailValue | IdentityAttributeValue | UnitLocationValue = (
            HealthDetailValue(
                fact_type=FactType.HEALTH_DETAIL,
                subject_relation=SubjectRelation.FAMILY,
                detail=message.text[:500],
            )
        )
    elif kind == "IDENTITY_ATTRIBUTE":
        typed_value = IdentityAttributeValue(
            fact_type=FactType.IDENTITY_ATTRIBUTE,
            display_name=_extract_after(message.text, "mother") or "a named family member",
        )
    else:
        typed_value = UnitLocationValue(
            fact_type=FactType.UNIT_LOCATION,
            unit_label=_extract_after(message.text, "apartment") or "an unlabeled unit",
        )

    return ProposedFact(
        client_ref=fact_ref,
        report_client_ref=report_ref,
        fact_type=FactType(kind),
        typed_value=typed_value,
        sensitivity=_REQUIRED_DETAIL_SENSITIVITY[kind],
        evidence_ids=(),
        source_spans=(_whole_message_span(message),),
    )
