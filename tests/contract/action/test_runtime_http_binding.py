"""The Action runtime's HTTP binding, driven with the envelope the application actually sends.

The other Action isolation tests assert what crosses the *port*. This one carries the same
envelope across the *transport* -- through the ASGI application AgentCore will call -- and
asserts that the answer is the exact result contract and that no second, private-input path
exists on the way in.

No model, no AWS, no socket. The runner is stubbed; everything else is the deployed code.
"""

from __future__ import annotations

import json

import httpx
import pytest
from runtimes.action import entrypoint as action_entrypoint
from runtimes.action import main as action_main
from runtimes.action.prompt import ACTION_PROMPT_VERSION
from runtimes.server import AgentCoreServer
from tests.fixtures.action import ActionHarness, grounded_draft

from chorus.contracts.action import ActionInput, ActionProposalDraft
from chorus.contracts.common import AgentName

pytestmark = pytest.mark.anyio

PROFILE = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/action"


class _StubActionRunner:
    """Answers with a minimal valid draft bound to the view it was given."""

    model_id = PROFILE

    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[ActionInput] = []

    async def run(self, payload: ActionInput, *, fence: str) -> ActionProposalDraft:
        self.calls += 1
        self.seen.append(payload)
        return grounded_draft(payload)


def _client(app: AgentCoreServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime.invalid"
    )


async def _captured(harness: ActionHarness) -> object:
    await harness.prepare()
    await harness.propose_action().execute(await harness.command())
    assert len(harness.agent.invocations) == 1
    return harness.agent.invocations[0]


async def test_the_action_binding_answers_the_exact_result_contract(
    harness: ActionHarness,
) -> None:
    invocation = await _captured(harness)
    runner = _StubActionRunner()

    async def handler(raw: bytes) -> bytes:
        return await action_entrypoint.handle(raw, runner=runner, budget_seconds=5)

    app = AgentCoreServer(
        handler=handler,
        contract_error=action_entrypoint.RuntimeContractError,
        budget_error=action_entrypoint.RuntimeBudgetExceededError,
    )
    raw = invocation.model_dump_json().encode("utf-8")  # type: ignore[attr-defined]

    async with _client(app) as client:
        response = await client.post("/invocations", content=raw)

    assert response.status_code == 200
    envelope = json.loads(response.text)
    assert envelope["schema_version"] == "agent-output/v1"
    assert envelope["agent_name"] == AgentName.ACTION.value
    assert envelope["prompt_version"] == ACTION_PROMPT_VERSION
    assert envelope["model_profile_arn_hash"].startswith("sha256:")
    assert PROFILE not in response.text
    assert runner.calls == 1


async def test_the_runtime_is_handed_only_the_safe_view_it_was_sent(
    harness: ActionHarness,
) -> None:
    """The transport adds nothing and removes nothing: what the port carried is what arrived."""

    invocation = await _captured(harness)
    runner = _StubActionRunner()

    async def handler(raw: bytes) -> bytes:
        return await action_entrypoint.handle(raw, runner=runner, budget_seconds=5)

    app = AgentCoreServer(
        handler=handler,
        contract_error=action_entrypoint.RuntimeContractError,
        budget_error=action_entrypoint.RuntimeBudgetExceededError,
    )
    raw = invocation.model_dump_json().encode("utf-8")  # type: ignore[attr-defined]

    async with _client(app) as client:
        await client.post("/invocations", content=raw)

    assert runner.seen == [invocation.payload]  # type: ignore[attr-defined]


async def test_a_private_field_added_to_the_payload_opens_no_second_input_path(
    harness: ActionHarness,
) -> None:
    """A caller cannot smuggle private evidence in beside the safe view.

    The contract is closed, so an unknown field is a validation failure rather than a field the
    runtime ignores -- and the refusal happens before any model is reached. This is the property
    that keeps "the Action runtime never receives a private projection" true across the
    transport as well as across the port.
    """

    invocation = await _captured(harness)
    body = json.loads(invocation.model_dump_json())  # type: ignore[attr-defined]
    body["payload"]["private_evidence_text"] = "Resident in unit 4B has a heart condition."

    runner = _StubActionRunner()

    async def handler(raw: bytes) -> bytes:
        return await action_entrypoint.handle(raw, runner=runner, budget_seconds=5)

    app = AgentCoreServer(
        handler=handler,
        contract_error=action_entrypoint.RuntimeContractError,
        budget_error=action_entrypoint.RuntimeBudgetExceededError,
    )

    async with _client(app) as client:
        response = await client.post("/invocations", content=json.dumps(body).encode("utf-8"))

    assert response.status_code == 400
    assert json.loads(response.text) == {"error": "INVALID_REQUEST"}
    assert "heart condition" not in response.text
    assert "unit 4B" not in response.text
    assert runner.calls == 0


async def test_the_action_runtime_refuses_an_investigator_request_envelope(
    harness: ActionHarness,
) -> None:
    """The Investigator's two-operation wrapper is Investigator-only.

    The Action runtime accepts one shape and answers one question, so a request built for a
    different runtime is refused rather than unwrapped.
    """

    invocation = await _captured(harness)
    wrapped = {
        "schema_version": "investigator-request/v1",
        "operation": "INVESTIGATE",
        "invocation": json.loads(invocation.model_dump_json()),  # type: ignore[attr-defined]
    }

    async with _client(action_main.app) as client:
        response = await client.post("/invocations", content=json.dumps(wrapped).encode("utf-8"))

    assert response.status_code == 400
