"""The AgentCore HTTP binding, driven over a real ASGI transport.

Every assertion here goes through :class:`httpx.ASGITransport`, so the code under test is the
application AgentCore will actually call -- routing, body framing, status codes and all -- with
no socket, no uvicorn, and no AWS. What is stubbed is the *runner*, because a model is the one
thing a transport test must not need.

The three runtimes share this binding, so the transport properties are asserted once, against a
recording handler, and each runtime's own end-to-end shape is asserted separately with its real
``handle``.
"""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
from runtimes import server
from runtimes.monitor import entrypoint as monitor_entrypoint
from runtimes.monitor import main as monitor_main
from runtimes.server import AgentCoreServer
from tests.fixtures.monitor_outputs import build_invocation, build_output

from chorus.contracts.common import MONITOR_PROMPT_VERSION, AgentName
from chorus.contracts.monitor import MonitorInput, MonitorOutput

pytestmark = pytest.mark.anyio

SECOND_INVOCATION = UUID("2b1c4d6e-8f90-4a12-b345-6789abcdef01")
"""A second server-generated invocation identity, so two requests are genuinely distinct."""

SECRET = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abcdef123456"
"""A model profile ARN, used as a sentinel. It is deployment configuration, so no response and
no error body may ever contain it."""


class _ContractError(ValueError):
    """Stands in for a runtime's own contract error, which is passed in rather than imported."""


class _BudgetError(_ContractError):
    """Stands in for a runtime's own budget error."""


class _Recorder:
    """A handler that answers, refuses, or fails, and remembers what it was given."""

    def __init__(self, *, answer: bytes = b'{"ok":true}') -> None:
        self.answer = answer
        self.seen: list[bytes] = []
        self.raises: Exception | None = None

    async def __call__(self, raw: bytes) -> bytes:
        self.seen.append(raw)
        if self.raises is not None:
            raise self.raises
        return self.answer


def _app(handler: object) -> AgentCoreServer:
    assert callable(handler)
    return AgentCoreServer(
        handler=handler,
        contract_error=_ContractError,
        budget_error=_BudgetError,
    )


def _client(app: AgentCoreServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime.invalid"
    )


# -- the frozen address and routes ---------------------------------------------------------


def test_the_binding_declares_the_address_agentcore_reaches() -> None:
    """Not configurable, and asserted here so a change is a deliberate one."""

    assert server.HOST == "0.0.0.0"  # noqa: S104 - the contract is the value
    assert server.PORT == 8080
    assert server.PING_PATH == "/ping"
    assert server.INVOCATIONS_PATH == "/invocations"


async def test_ping_answers_two_hundred_with_a_supported_status() -> None:
    async with _client(_app(_Recorder())) as client:
        response = await client.get("/ping")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert json.loads(response.text) == {"status": "Healthy"}


async def test_ping_touches_no_model_and_carries_no_configuration() -> None:
    """A health check that reached the runner would fail closed on a cold start, and a health
    body that named the profile would publish deployment configuration to anyone who could
    reach the port."""

    recorder = _Recorder()
    async with _client(_app(recorder)) as client:
        response = await client.get("/ping")

    assert recorder.seen == []
    body = json.loads(response.text)
    assert set(body) == {"status"}
    assert body["status"] in {"Healthy", "HealthyBusy"}
    assert SECRET not in response.text


async def test_an_unknown_path_and_a_wrong_method_are_refused_without_reaching_the_handler() -> (
    None
):
    recorder = _Recorder()
    async with _client(_app(recorder)) as client:
        unknown = await client.get("/")
        wrong_method = await client.post("/ping", content=b"{}")
        get_invocations = await client.get("/invocations")

    assert unknown.status_code == 404
    assert wrong_method.status_code == 405
    assert get_invocations.status_code == 405
    assert recorder.seen == []


# -- the invocation route ------------------------------------------------------------------


async def test_a_valid_request_is_handed_to_the_handler_verbatim_and_answered_verbatim() -> None:
    recorder = _Recorder(answer=b'{"answer":1}')
    async with _client(_app(recorder)) as client:
        response = await client.post("/invocations", content=b'{"request":1}')

    assert response.status_code == 200
    assert response.content == b'{"answer":1}'
    assert recorder.seen == [b'{"request":1}']


async def test_a_refused_request_is_a_four_hundred_that_quotes_nothing() -> None:
    recorder = _Recorder()
    recorder.raises = _ContractError(f"payload named {SECRET} and was rejected")
    async with _client(_app(recorder)) as client:
        response = await client.post("/invocations", content=b'{"malformed":')

    assert response.status_code == 400
    assert json.loads(response.text) == {"error": "INVALID_REQUEST"}
    assert SECRET not in response.text
    assert "Traceback" not in response.text


async def test_a_schema_invalid_payload_is_refused_by_the_real_handler_before_any_model() -> None:
    """The handler is the Monitor's own, with no runner supplied: a request that fails
    validation must be refused before the runtime ever tries to build one from the
    environment, which is what proves no model was reached."""

    async with _client(monitor_main.app) as client:
        response = await client.post("/invocations", content=b'{"schema_version":"agent-input/v1"}')

    assert response.status_code == 400
    assert json.loads(response.text) == {"error": "INVALID_REQUEST"}


async def test_an_exhausted_budget_is_a_gateway_timeout_and_not_a_client_error() -> None:
    """The distinction matters to the caller: a 4xx says "do not send this again"."""

    recorder = _Recorder()
    recorder.raises = _BudgetError("the runtime exceeded its budget")
    async with _client(_app(recorder)) as client:
        response = await client.post("/invocations", content=b"{}")

    assert response.status_code == 504
    assert json.loads(response.text) == {"error": "RUNTIME_BUDGET_EXCEEDED"}


async def test_an_unexpected_failure_is_a_five_hundred_carrying_no_detail() -> None:
    recorder = _Recorder()
    recorder.raises = RuntimeError(f"boto3 could not reach {SECRET}")
    async with _client(_app(recorder)) as client:
        response = await client.post("/invocations", content=b"{}")

    assert response.status_code == 500
    assert json.loads(response.text) == {"error": "RUNTIME_FAILURE"}
    assert SECRET not in response.text
    assert "boto3" not in response.text
    assert "RuntimeError" not in response.text


async def test_a_body_past_the_transport_bound_is_refused_while_it_is_still_arriving() -> None:
    """``handle`` cannot refuse bytes it has already been made to buffer."""

    recorder = _Recorder()
    oversize = b"x" * (server.MAX_REQUEST_BYTES + 1)
    async with _client(_app(recorder)) as client:
        response = await client.post("/invocations", content=oversize)

    assert response.status_code == 413
    assert json.loads(response.text) == {"error": "PAYLOAD_TOO_LARGE"}
    assert recorder.seen == []


# -- the Monitor binding, end to end without a model ----------------------------------------


class _StubMonitorRunner:
    """One runner, recording how many times it was asked and by whom."""

    model_id = SECRET

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, payload: MonitorInput, *, fence: str) -> MonitorOutput:
        self.calls += 1
        return build_output(build_invocation(messages=payload.messages))

    @staticmethod
    def second_request() -> bytes:
        """The same payload under a different server-generated invocation identity."""

        other = build_invocation().model_copy(update={"invocation_id": SECOND_INVOCATION})
        return other.model_dump_json().encode("utf-8")


async def test_the_monitor_binding_returns_the_exact_result_contract() -> None:
    runner = _StubMonitorRunner()

    async def handler(raw: bytes) -> bytes:
        return await monitor_entrypoint.handle(raw, runner=runner, budget_seconds=5)

    invocation = build_invocation()
    async with _client(_app(handler)) as client:
        response = await client.post(
            "/invocations", content=invocation.model_dump_json().encode("utf-8")
        )

    assert response.status_code == 200
    envelope = json.loads(response.text)
    assert envelope["schema_version"] == "agent-output/v1"
    assert envelope["agent_name"] == AgentName.MONITOR.value
    assert envelope["prompt_version"] == MONITOR_PROMPT_VERSION
    assert envelope["invocation_id"] == str(invocation.invocation_id)
    assert envelope["model_profile_arn_hash"].startswith("sha256:")
    assert SECRET not in response.text, "the profile ARN is named by digest and never carried"
    assert runner.calls == 1


async def test_two_invocations_share_no_state_through_the_application_object() -> None:
    """The application is frozen and slotted, so it *cannot* hold a request; this proves the
    consequence -- each response is about its own request and nothing of the first survives."""

    runner = _StubMonitorRunner()

    async def handler(raw: bytes) -> bytes:
        return await monitor_entrypoint.handle(raw, runner=runner, budget_seconds=5)

    app = _app(handler)
    first = build_invocation()
    async with _client(app) as client:
        one = await client.post("/invocations", content=first.model_dump_json().encode("utf-8"))
        two = await client.post("/invocations", content=runner.second_request())

    assert one.status_code == 200
    assert two.status_code == 200
    assert runner.calls == 2
    # Each answer names its own request and only its own.
    assert json.loads(one.text)["invocation_id"] == str(first.invocation_id)
    assert json.loads(two.text)["invocation_id"] == str(SECOND_INVOCATION)
    with pytest.raises(AttributeError):
        object.__setattr__(app, "last_request", b"anything")


def test_the_application_cannot_be_given_a_second_handler() -> None:
    """Frozen for a reason: a runtime that could be rebound at runtime would be a runtime whose
    binding is not decided by its artifact."""

    app = _app(_Recorder())
    with pytest.raises((AttributeError, TypeError)):
        app.handler = _Recorder()  # type: ignore[misc]


def test_each_runtime_binds_only_its_own_handler() -> None:
    """Three artifacts, three ``main.py`` files, three handlers -- and no runtime holds another's.

    Read off the constructed application objects rather than off the source, because the claim
    is about what is bound, not about what the file says.
    """

    from runtimes.action import main as action_main
    from runtimes.investigator import main as investigator_main

    bindings = {
        "monitor": monitor_main.app,
        "investigator": investigator_main.app,
        "action": action_main.app,
    }
    for agent, app in bindings.items():
        assert app.handler.__module__ == f"runtimes.{agent}.entrypoint"
        assert app.contract_error.__module__ == f"runtimes.{agent}.entrypoint"
        assert app.budget_error.__module__ == f"runtimes.{agent}.entrypoint"
    assert len({id(app.handler) for app in bindings.values()}) == 3
