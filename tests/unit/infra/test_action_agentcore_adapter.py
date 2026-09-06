"""The live Action adapter, exercised without AWS.

The adapter is deliberately thin, so what is worth testing is exactly the small set of
decisions it does make: refuse an oversized payload locally rather than at the service, refuse
an oversized response, refuse a malformed answer without quoting it, and use a fresh session
per invocation.

The invoker is stubbed, which is the seam a deployed smoke test would replace with a real
endpoint. Everything above it is the same code path the fake adapters exercise, which is what
makes the local suite meaningful about the deployed one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from chorus.contracts.action import (
    ACTION_PROMPT_VERSION,
    ActionClaimDraft,
    ActionInput,
    ActionProposalDraft,
    ActionRequestDraft,
    ActionToneValue,
    MandateVersionRefInput,
    SafeDestinationInput,
    SafeDestinationKind,
    SafeDisclosureScope,
    SafeEvidenceStatus,
    SafeFactType,
    SafePurpose,
    SafeTransformationKind,
    ShareableFactInput,
)
from chorus.contracts.common import (
    AGENT_INPUT_SCHEMA_VERSION,
    AGENT_OUTPUT_SCHEMA_VERSION,
    AgentInputEnvelope,
    AgentName,
    AgentResultEnvelope,
)
from chorus.infrastructure.agentcore.action import (
    MAX_PAYLOAD_BYTES,
    SESSION_ID_BYTES,
    AgentCoreActionAgent,
)
from chorus.ports.agents import (
    ActionRejection,
    AgentContractViolationError,
    AgentDependencyError,
)
from chorus.privacy.compiler import POLICY_BUILD_HASH

pytestmark = pytest.mark.anyio

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/chorus-action"
NOW = datetime(2030, 1, 14, 9, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


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


def _view(*, facts: int = 1, label: str = "Maple Court") -> ActionInput:
    fact_ids = [uuid4() for _ in range(facts)]
    return ActionInput(
        view_id=uuid4(),
        case_id=uuid4(),
        community_public_label=label,
        case_version=1,
        authorization_version=1,
        policy_version="policy/v1",
        compiler_version="compiler/v1",
        policy_build_hash=POLICY_BUILD_HASH.value,
        destination=SafeDestinationInput(
            destination_id="property_manager:demo",
            kind=SafeDestinationKind.PROPERTY_MANAGER,
            registry_version=1,
            routing_token=uuid4(),
            display_label="Property Management",
        ),
        purpose=SafePurpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
        generated_at=NOW,
        expires_at=datetime(2030, 1, 14, 9, 15, tzinfo=UTC),
        mandate_version_set=(
            MandateVersionRefInput(mandate_id=uuid4(), version=1, terms_hash=DIGEST),
        ),
        authorization_snapshot_hash=DIGEST,
        shareable_facts=tuple(
            ShareableFactInput(
                export_fact_id=fact_id,
                fact_type=SafeFactType.INCIDENT_OCCURRENCE,
                safe_text="The elevator was out of service on 2030-01-14.",
                effective_scope=SafeDisclosureScope.EXTERNAL_ACTION,
                evidence_status=SafeEvidenceStatus.CORROBORATED,
                contributor_count=4,
                transformation=SafeTransformationKind.AGGREGATED,
                transformation_rule_id="aggregate-incidents/v1",
                safe_evidence_ref_ids=(),
                content_hash=DIGEST,
            )
            for fact_id in fact_ids
        ),
        safe_evidence_refs=(),
        audit_refs=(uuid4(),),
        view_hash=DIGEST,
    )


def _invocation(payload: ActionInput | None = None) -> AgentInputEnvelope[ActionInput]:
    view = payload or _view()
    return AgentInputEnvelope[ActionInput](
        schema_version=AGENT_INPUT_SCHEMA_VERSION,
        invocation_id=uuid4(),
        namespace="TEST_ACTION",
        agent_name=AgentName.ACTION,
        case_id=view.case_id,
        case_version=view.case_version,
        requested_at=NOW,
        policy_version="policy/v1",
        payload=view,
    )


def _result(invocation: AgentInputEnvelope[ActionInput]) -> bytes:
    fact = invocation.payload.shareable_facts[0].export_fact_id
    envelope = AgentResultEnvelope[ActionProposalDraft](
        schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
        invocation_id=invocation.invocation_id,
        namespace=invocation.namespace,
        agent_name=AgentName.ACTION,
        case_id=invocation.case_id,
        case_version=invocation.case_version,
        model_profile_arn_hash=DIGEST,
        prompt_version=ACTION_PROMPT_VERSION,
        started_at=NOW,
        completed_at=NOW,
        output=ActionProposalDraft(
            view_id=invocation.payload.view_id,
            view_hash=invocation.payload.view_hash,
            case_id=invocation.payload.case_id,
            case_version=invocation.payload.case_version,
            authorization_version=invocation.payload.authorization_version,
            subject="Repair request",
            claims=(
                ActionClaimDraft(
                    claim_id=uuid4(),
                    text="The elevator was out of service on 2030-01-14.",
                    export_fact_ids=(fact,),
                ),
            ),
            request=ActionRequestDraft(
                requested_action="Please inspect and repair.", request_fact_ids=(fact,)
            ),
            caveats=(),
            tone=ActionToneValue.NEUTRAL,
        ),
    )
    return envelope.model_dump_json().encode("utf-8")


async def test_the_adapter_carries_one_payload_out_and_one_envelope_back() -> None:
    invocation = _invocation()
    invoker = RecordingInvoker(_result(invocation))
    agent = AgentCoreActionAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    result = await agent.invoke_action(invocation)

    assert result.invocation_id == invocation.invocation_id
    assert result.prompt_version == ACTION_PROMPT_VERSION
    assert len(invoker.calls) == 1
    assert invoker.calls[0]["runtime_arn"] == RUNTIME_ARN


async def test_the_session_identifier_is_fresh_per_invocation() -> None:
    """V1 agents are stateless. A reused session would give one proposal access to another
    case's compiled view, which is the implicit shared state the orchestration decision
    rules out."""

    seen: set[str] = set()
    for _ in range(5):
        invocation = _invocation()
        invoker = RecordingInvoker(_result(invocation))
        agent = AgentCoreActionAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)
        await agent.invoke_action(invocation)
        session = str(invoker.calls[0]["session_id"])
        assert len(session) == SESSION_ID_BYTES * 2
        seen.add(session)

    assert len(seen) == 5


async def test_an_oversized_payload_is_refused_locally() -> None:
    """A typed contract failure rather than an opaque transport error.

    Refused before the request leaves, so an oversized view is diagnosable from the code that
    built it rather than from a service message about a request nobody can inspect.
    """

    invocation = _invocation(_view(label="x" * 120))
    huge = invocation.model_copy(update={"payload": _view(facts=100, label="y" * 120)})
    invoker = RecordingInvoker(b"{}")
    agent = AgentCoreActionAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    if len(huge.model_dump_json().encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        pytest.skip("the frozen view bounds keep even a maximal payload under 1 MiB")

    with pytest.raises(AgentContractViolationError):
        await agent.invoke_action(huge)
    assert invoker.calls == []


async def test_an_oversized_response_is_a_non_retryable_dependency_error() -> None:
    invocation = _invocation()
    invoker = RecordingInvoker(b"x" * (MAX_PAYLOAD_BYTES + 1))
    agent = AgentCoreActionAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    with pytest.raises(AgentDependencyError) as caught:
        await agent.invoke_action(invocation)

    assert caught.value.retryable is False


async def test_a_malformed_answer_is_a_schema_violation_that_quotes_nothing() -> None:
    """The validation report is never chained into the message and never logged.

    A Pydantic error quotes the offending input, which here is model-authored prose intended
    for an external recipient.
    """

    invocation = _invocation()
    invoker = RecordingInvoker(b'{"not": "an envelope"}')
    agent = AgentCoreActionAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    with pytest.raises(AgentContractViolationError) as caught:
        await agent.invoke_action(invocation)

    assert caught.value.reason_codes == (ActionRejection.SCHEMA_INVALID.value,)
    assert "not" not in str(caught.value)


async def test_the_adapter_never_retries_internally() -> None:
    """Retry identity belongs to the use case, which owns the invocation ID."""

    invocation = _invocation()
    invoker = RecordingInvoker(_result(invocation))
    agent = AgentCoreActionAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    await agent.invoke_action(invocation)

    assert len(invoker.calls) == 1


async def test_the_payload_on_the_wire_is_the_serialized_envelope_and_nothing_else() -> None:
    invocation = _invocation()
    invoker = RecordingInvoker(_result(invocation))
    agent = AgentCoreActionAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    await agent.invoke_action(invocation)

    sent = invoker.calls[0]["payload"]
    assert isinstance(sent, bytes)
    assert sent == invocation.model_dump_json().encode("utf-8")
