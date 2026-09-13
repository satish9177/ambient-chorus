"""The two adapters that address one Investigator runtime, and the bytes they actually send.

Both go to the same deployed runtime under the same execution role and the same application
inference profile. What separates them is a declared operation in the payload, so these tests
assert the payload -- and then hand it to the runtime's own parser, which is the only way to
prove the two halves of the contract agree rather than merely resemble each other.

No AWS: the invoker is a recording stand-in, and the response is a serialized envelope built
from the same contracts the deployed runtime returns.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid5

import pytest
from runtimes.investigator import entrypoint

from chorus.contracts.agentcore import ExtractCommitmentRequest, InvestigateRequest
from chorus.contracts.commitment import (
    COMMITMENT_EXTRACTION_PROMPT_VERSION,
    CommitmentExtractionInput,
    CommitmentExtractionOutput,
)
from chorus.contracts.common import (
    AGENT_OUTPUT_SCHEMA_VERSION,
    INVESTIGATOR_PROMPT_VERSION,
    AgentInputEnvelope,
    AgentName,
    AgentResultEnvelope,
)
from chorus.contracts.investigation import (
    InvestigationAssessmentDraft,
    InvestigationCase,
    InvestigationInput,
    InvestigationReport,
    LinkageDecision,
    RecommendedCaseDisposition,
    SufficiencyDraft,
)
from chorus.contracts.monitor import IssueType
from chorus.domain.entities import CaseState
from chorus.infrastructure.agentcore.commitment import AgentCoreCommitmentExtractionAgent
from chorus.infrastructure.agentcore.investigator import AgentCoreInvestigatorAgent
from chorus.ports.agents import (
    AgentContractViolationError,
    CommitmentExtractionRejection,
)

pytestmark = pytest.mark.anyio

SEED = UUID("6f7a8b9c-0d1e-52f3-a4b5-c6d7e8f90a1b")
NOW = datetime(2030, 8, 1, 9, 0, 0, tzinfo=UTC)
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/chorus_investigator"
PROFILE_HASH = "sha256:" + "ab" * 32


def uuid(name: str) -> UUID:
    return uuid5(SEED, name)


class RecordingInvoker:
    """Return a scripted response and remember exactly how it was called."""

    def __init__(self, response: bytes) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def invoke(self, *, runtime_arn: str, session_id: str, payload: bytes) -> bytes:
        self.calls.append(
            {"runtime_arn": runtime_arn, "session_id": session_id, "payload": payload}
        )
        return self.response


def investigation_invocation() -> AgentInputEnvelope[InvestigationInput]:
    return AgentInputEnvelope[InvestigationInput](
        invocation_id=uuid("invocation"),
        namespace="DEMO",
        agent_name=AgentName.INVESTIGATOR,
        case_id=uuid("case"),
        case_version=3,
        requested_at=NOW,
        policy_version="policy/v1",
        payload=InvestigationInput(
            case=InvestigationCase(
                case_id=uuid("case"),
                version=3,
                title="Recurring elevator failure",
                issue_type=IssueType.ELEVATOR_FAILURE,
                current_state=CaseState.INVESTIGATING,
            ),
            reports=(
                InvestigationReport(
                    report_id=uuid("report"),
                    contributor_pseudonym_id="resident-a",
                    summary="The lift stopped again.",
                    source_message_ids=(uuid("message"),),
                ),
            ),
        ),
    )


def extraction_invocation() -> AgentInputEnvelope[CommitmentExtractionInput]:
    """The envelope the application sends: no case on it, by design (ADR-027 § 1)."""

    return AgentInputEnvelope[CommitmentExtractionInput](
        invocation_id=uuid("invocation"),
        namespace="DEMO",
        agent_name=AgentName.INVESTIGATOR,
        case_id=None,
        case_version=None,
        requested_at=NOW,
        policy_version="policy/v1",
        payload=CommitmentExtractionInput(
            case_id=uuid("case"),
            source_evidence_id=uuid("evidence"),
            destination_display_label="Property Management",
            reply_text="We will repair elevator B by 2030-09-10.",
        ),
    )


def _investigation_response() -> bytes:
    invocation = investigation_invocation()
    envelope = AgentResultEnvelope[InvestigationAssessmentDraft](
        schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
        invocation_id=invocation.invocation_id,
        namespace=invocation.namespace,
        agent_name=AgentName.INVESTIGATOR,
        case_id=invocation.case_id,
        case_version=invocation.case_version,
        model_profile_arn_hash=PROFILE_HASH,
        prompt_version=INVESTIGATOR_PROMPT_VERSION,
        started_at=NOW,
        completed_at=NOW,
        output=InvestigationAssessmentDraft(
            case_id=uuid("case"),
            based_on_case_version=3,
            linkage_decision=LinkageDecision.UNCERTAIN,
            sufficiency=SufficiencyDraft(independent_source_count=1, is_corroborated=False),
            recommended_case_disposition=RecommendedCaseDisposition.CONTINUE_INVESTIGATION,
        ),
    )
    return envelope.model_dump_json().encode("utf-8")


def _extraction_response() -> bytes:
    invocation = extraction_invocation()
    envelope = AgentResultEnvelope[CommitmentExtractionOutput](
        schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
        invocation_id=invocation.invocation_id,
        namespace=invocation.namespace,
        agent_name=AgentName.INVESTIGATOR,
        case_id=invocation.case_id,
        case_version=invocation.case_version,
        model_profile_arn_hash=PROFILE_HASH,
        prompt_version=COMMITMENT_EXTRACTION_PROMPT_VERSION,
        started_at=NOW,
        completed_at=NOW,
        output=CommitmentExtractionOutput(
            case_id=uuid("case"), source_evidence_id=uuid("evidence"), commitments=()
        ),
    )
    return envelope.model_dump_json().encode("utf-8")


# -- what goes on the wire ---------------------------------------------------------------------


async def test_the_investigation_adapter_declares_its_operation() -> None:
    invoker = RecordingInvoker(_investigation_response())
    agent = AgentCoreInvestigatorAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)
    invocation = investigation_invocation()

    await agent.invoke_investigator(invocation)

    payload = invoker.calls[0]["payload"]
    assert isinstance(payload, bytes)
    sent = json.loads(payload.decode("utf-8"))
    assert sent["schema_version"] == "investigator-request/v1"
    assert sent["operation"] == "INVESTIGATE"
    assert sent["invocation"] == json.loads(invocation.model_dump_json())


async def test_the_extraction_adapter_declares_its_operation() -> None:
    invoker = RecordingInvoker(_extraction_response())
    agent = AgentCoreCommitmentExtractionAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)
    invocation = extraction_invocation()

    await agent.invoke_commitment_extraction(invocation)

    payload = invoker.calls[0]["payload"]
    assert isinstance(payload, bytes)
    sent = json.loads(payload.decode("utf-8"))
    assert sent["operation"] == "EXTRACT_COMMITMENT"
    assert sent["invocation"] == json.loads(invocation.model_dump_json())


async def test_neither_adapter_puts_deployment_configuration_in_the_payload() -> None:
    """The runtime ARN, the region, and the profile are the deployment's, not the request's."""

    invoker = RecordingInvoker(_extraction_response())
    agent = AgentCoreCommitmentExtractionAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    await agent.invoke_commitment_extraction(extraction_invocation())

    payload = invoker.calls[0]["payload"]
    assert isinstance(payload, bytes)
    body = payload.decode("utf-8")
    assert RUNTIME_ARN not in body
    assert "arn:aws" not in body
    assert invoker.calls[0]["runtime_arn"] == RUNTIME_ARN


async def test_both_adapters_address_the_same_investigator_runtime() -> None:
    """One runtime, one role, one profile. There is no fourth agent to address."""

    investigation = RecordingInvoker(_investigation_response())
    extraction = RecordingInvoker(_extraction_response())

    await AgentCoreInvestigatorAgent(
        invoker=investigation, runtime_arn=RUNTIME_ARN
    ).invoke_investigator(investigation_invocation())
    await AgentCoreCommitmentExtractionAgent(
        invoker=extraction, runtime_arn=RUNTIME_ARN
    ).invoke_commitment_extraction(extraction_invocation())

    assert investigation.calls[0]["runtime_arn"] == extraction.calls[0]["runtime_arn"]


async def test_every_extraction_gets_its_own_session() -> None:
    """A reused session would carry one stranger's email into the next request."""

    invoker = RecordingInvoker(_extraction_response())
    agent = AgentCoreCommitmentExtractionAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    await agent.invoke_commitment_extraction(extraction_invocation())
    await agent.invoke_commitment_extraction(extraction_invocation())

    first, second = (str(call["session_id"]) for call in invoker.calls)
    assert first != second
    assert len(first) == 40


# -- the two halves of the contract meet --------------------------------------------------------


async def test_the_runtime_parses_exactly_what_the_investigation_adapter_sends() -> None:
    invoker = RecordingInvoker(_investigation_response())
    agent = AgentCoreInvestigatorAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)
    invocation = investigation_invocation()

    await agent.invoke_investigator(invocation)

    raw = invoker.calls[0]["payload"]
    assert isinstance(raw, bytes)
    parsed = entrypoint.parse_request(raw)
    assert isinstance(parsed, InvestigateRequest)
    assert parsed.invocation.payload == invocation.payload


async def test_the_runtime_parses_exactly_what_the_extraction_adapter_sends() -> None:
    invoker = RecordingInvoker(_extraction_response())
    agent = AgentCoreCommitmentExtractionAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)
    invocation = extraction_invocation()

    await agent.invoke_commitment_extraction(invocation)

    raw = invoker.calls[0]["payload"]
    assert isinstance(raw, bytes)
    parsed = entrypoint.parse_request(raw)
    assert isinstance(parsed, ExtractCommitmentRequest)
    assert parsed.invocation.payload.reply_text == invocation.payload.reply_text


async def test_a_malformed_extraction_response_is_a_contract_violation_quoting_nothing() -> None:
    invoker = RecordingInvoker(b'{"schema_version":"agent-output/v1"}')
    agent = AgentCoreCommitmentExtractionAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    with pytest.raises(AgentContractViolationError) as raised:
        await agent.invoke_commitment_extraction(extraction_invocation())

    assert raised.value.reason_codes == (CommitmentExtractionRejection.SCHEMA_INVALID,)
    assert "reply_text" not in repr(raised.value)
    assert "validation error" not in repr(raised.value)
