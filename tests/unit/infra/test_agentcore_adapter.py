"""The live Monitor adapter and its error mapping, exercised without AWS.

The adapter is the one place a botocore exception becomes a CHORUS decision about whether a
second invocation is safe, so every branch of that mapping is tested here. The invoker is
stubbed, which is exactly the seam a deployed smoke test would replace with a real endpoint.
"""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from tests.fixtures.monitor_outputs import build_invocation, build_output, build_result

from chorus.contracts.common import AgentResultEnvelope
from chorus.contracts.monitor import MonitorOutput
from chorus.infrastructure.agentcore.client import Boto3AgentCoreInvoker
from chorus.infrastructure.agentcore.monitor import (
    MAX_PAYLOAD_BYTES,
    SESSION_ID_BYTES,
    AgentCoreMonitorAgent,
)
from chorus.ports.agents import (
    AgentContractViolationError,
    AgentDependencyError,
    AgentRejection,
    AgentTimeoutError,
)

pytestmark = pytest.mark.anyio

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/chorus-monitor"


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


class _RaisingClient:
    """A stand-in AgentCore client that always raises one scripted transport failure."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def invoke_agent_runtime(self, **_: object) -> object:
        raise self.error


def _distinct_uuid(index: int) -> object:
    from uuid import uuid5

    from tests.fixtures.monitor_outputs import SEED

    return uuid5(SEED, f"bulk:{index}")


def _valid_response() -> bytes:
    invocation = build_invocation()
    return build_result(invocation, build_output(invocation)).model_dump_json().encode("utf-8")


async def test_the_adapter_returns_a_parsed_result_envelope() -> None:
    invoker = RecordingInvoker(_valid_response())
    agent = AgentCoreMonitorAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    result = await agent.invoke_monitor(build_invocation())

    assert isinstance(result, AgentResultEnvelope)
    assert isinstance(result.output, MonitorOutput)


async def test_the_adapter_addresses_the_configured_runtime_endpoint() -> None:
    invoker = RecordingInvoker(_valid_response())
    agent = AgentCoreMonitorAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    await agent.invoke_monitor(build_invocation())

    assert invoker.calls[0]["runtime_arn"] == RUNTIME_ARN


async def test_every_invocation_gets_its_own_random_session() -> None:
    """V1 agents are stateless, so no two invocations may share a session."""

    invoker = RecordingInvoker(_valid_response())
    agent = AgentCoreMonitorAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    await agent.invoke_monitor(build_invocation())
    await agent.invoke_monitor(build_invocation())

    first, second = (str(call["session_id"]) for call in invoker.calls)
    assert first != second
    assert len(first) == SESSION_ID_BYTES * 2


async def test_the_payload_is_the_serialized_envelope_and_nothing_else() -> None:
    invoker = RecordingInvoker(_valid_response())
    agent = AgentCoreMonitorAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)
    invocation = build_invocation()

    await agent.invoke_monitor(invocation)

    payload = invoker.calls[0]["payload"]
    assert isinstance(payload, bytes)
    sent = json.loads(payload.decode("utf-8"))
    assert sent == json.loads(invocation.model_dump_json())
    assert "runtime_arn" not in sent


async def test_the_largest_contract_valid_payload_stays_inside_the_frozen_bound() -> None:
    """The batch and message bounds are what keep the transport limit unreachable.

    Constructed at the maximum the contract allows -- fifty messages of ten thousand
    characters each -- and asserted to still fit. If a future bound were widened past this,
    the adapter would start refusing well-formed requests, and this test says so first.
    """

    invocation = build_invocation()
    template = invocation.payload.messages[0]
    largest = invocation.model_copy(
        update={
            "payload": invocation.payload.model_copy(
                update={
                    "messages": tuple(
                        template.model_copy(
                            update={
                                "text": "x" * 10_000,
                                "message_id": _distinct_uuid(index),
                            }
                        )
                        for index in range(50)
                    )
                }
            )
        }
    )

    assert len(largest.model_dump_json().encode("utf-8")) < MAX_PAYLOAD_BYTES
    assert MAX_PAYLOAD_BYTES == 1_048_576


async def test_an_oversized_response_is_refused_without_being_parsed() -> None:
    invoker = RecordingInvoker(b"x" * (MAX_PAYLOAD_BYTES + 1))
    agent = AgentCoreMonitorAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    with pytest.raises(AgentDependencyError) as raised:
        await agent.invoke_monitor(build_invocation())

    assert raised.value.retryable is False
    assert raised.value.reason_codes == ("AGENTCORE_RESPONSE_TOO_LARGE",)


async def test_a_malformed_response_is_a_contract_violation_that_quotes_nothing() -> None:
    invoker = RecordingInvoker(b'{"schema_version": "agent-output/v1"}')
    agent = AgentCoreMonitorAgent(invoker=invoker, runtime_arn=RUNTIME_ARN)

    with pytest.raises(AgentContractViolationError) as raised:
        await agent.invoke_monitor(build_invocation())

    assert raised.value.reason_codes == (AgentRejection.SCHEMA_INVALID.value,)
    assert raised.value.retryable is False
    assert "schema_version" not in str(raised.value)


def test_a_read_timeout_becomes_a_retryable_timeout() -> None:
    """The read timeout is the enforcement point for the frozen agent budget."""

    invoker = Boto3AgentCoreInvoker(
        _RaisingClient(ReadTimeoutError(endpoint_url="https://example.invalid"))
    )

    with pytest.raises(AgentTimeoutError) as raised:
        invoker.invoke(runtime_arn=RUNTIME_ARN, session_id="s" * 40, payload=b"{}")

    assert raised.value.retryable is True


def test_a_dropped_connection_is_retryable_because_nothing_came_back() -> None:
    from botocore.exceptions import EndpointConnectionError

    invoker = Boto3AgentCoreInvoker(
        _RaisingClient(EndpointConnectionError(endpoint_url="https://example.invalid"))
    )

    with pytest.raises(AgentDependencyError) as raised:
        invoker.invoke(runtime_arn=RUNTIME_ARN, session_id="s" * 40, payload=b"{}")

    assert raised.value.retryable is True


def test_a_throttle_is_classified_as_retryable() -> None:
    invoker = Boto3AgentCoreInvoker(_ClientErrorClient("ThrottlingException"))

    with pytest.raises(AgentDependencyError) as raised:
        invoker.invoke(runtime_arn=RUNTIME_ARN, session_id="s" * 40, payload=b"{}")

    assert raised.value.retryable is True
    assert raised.value.reason_codes == ("AGENTCORE_UNAVAILABLE",)


def test_a_definite_rejection_is_never_retried() -> None:
    invoker = Boto3AgentCoreInvoker(_ClientErrorClient("ValidationException"))

    with pytest.raises(AgentDependencyError) as raised:
        invoker.invoke(runtime_arn=RUNTIME_ARN, session_id="s" * 40, payload=b"{}")

    assert raised.value.retryable is False
    assert raised.value.reason_codes == ("AGENTCORE_REJECTED",)


def test_an_access_denied_is_never_retried() -> None:
    """An IAM denial is the boundary working; retrying it would only repeat the denial."""

    invoker = Boto3AgentCoreInvoker(_ClientErrorClient("AccessDeniedException"))

    with pytest.raises(AgentDependencyError) as raised:
        invoker.invoke(runtime_arn=RUNTIME_ARN, session_id="s" * 40, payload=b"{}")

    assert raised.value.retryable is False


def test_a_service_error_message_never_reaches_the_raised_error() -> None:
    invoker = Boto3AgentCoreInvoker(
        _ClientErrorClient("ValidationException", message="resident-b said SECRET")
    )

    with pytest.raises(AgentDependencyError) as raised:
        invoker.invoke(runtime_arn=RUNTIME_ARN, session_id="s" * 40, payload=b"{}")

    assert "SECRET" not in str(raised.value)
    assert "SECRET" not in repr(raised.value)


class _ClientErrorClient:
    """A stand-in AgentCore client that always raises one scripted service error."""

    def __init__(self, code: str, *, message: str = "an error occurred") -> None:
        self.code = code
        self.message = message

    def invoke_agent_runtime(self, **_: object) -> object:
        raise ClientError(
            {"Error": {"Code": self.code, "Message": self.message}}, "InvokeAgentRuntime"
        )
