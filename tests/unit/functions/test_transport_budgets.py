"""Caller-specific synchronous-invocation transport budgets (review P2-9).

Proves the inequality chains hold against the actual ``lambda.toml`` timeouts, that
``create_lambda_client`` applies the explicit deadlines, and that a downstream that exceeds the
client read timeout surfaces as the typed dependency failure -- never as a completed response --
without a second SDK attempt.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ReadTimeoutError
from tools.build_lambda_artifacts import load_lambda_manifest

from chorus.infrastructure.compiler.invoker import SINGLE_ATTEMPT_RETRIES, create_lambda_client
from chorus.infrastructure.lambdas import transport_budgets as tb
from chorus.infrastructure.lambdas.invoker import (
    AsynchronousLambdaInvoker,
    SynchronousLambdaInvoker,
)
from chorus.ports.errors import ExternalDependencyError, PersistenceErrorCode

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_the_budget_constants_match_the_lambda_manifest_timeouts() -> None:
    assert load_lambda_manifest("api").timeout_seconds == tb.API_LAMBDA_TIMEOUT_SECONDS
    assert load_lambda_manifest("worker").timeout_seconds == tb.WORKER_LAMBDA_TIMEOUT_SECONDS
    assert load_lambda_manifest("sender").timeout_seconds == tb.SENDER_LAMBDA_TIMEOUT_SECONDS
    # the compiler and watcher self-terminate before the API's client read timeout for them
    assert load_lambda_manifest("compiler").timeout_seconds < tb.API_DOWNSTREAM_READ_TIMEOUT_SECONDS
    assert (
        load_lambda_manifest("commitment_watcher").timeout_seconds
        < tb.API_DOWNSTREAM_READ_TIMEOUT_SECONDS
    )


def test_the_request_path_inequality_chain_holds() -> None:
    assert (
        tb.API_DOWNSTREAM_READ_TIMEOUT_SECONDS
        < tb.API_LAMBDA_TIMEOUT_SECONDS
        < tb.HTTP_API_INTEGRATION_CEILING_SECONDS
    )


def test_the_worker_out_waits_the_sender_but_leaves_projection_budget() -> None:
    assert (
        tb.SENDER_LAMBDA_TIMEOUT_SECONDS
        < tb.WORKER_SENDER_READ_TIMEOUT_SECONDS
        < tb.WORKER_LAMBDA_TIMEOUT_SECONDS
    )
    # some worker budget remains after the sender's own maximum runtime
    assert tb.WORKER_LAMBDA_TIMEOUT_SECONDS - tb.WORKER_SENDER_READ_TIMEOUT_SECONDS >= 10


def test_create_lambda_client_applies_the_explicit_transport_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeBoto3:
        @staticmethod
        def client(service: str, **kwargs: Any) -> str:
            captured["service"] = service
            captured["config"] = kwargs["config"]
            return "client"

    import chorus.infrastructure.compiler.invoker as invoker_mod

    monkeypatch.setitem(__import__("sys").modules, "boto3", _FakeBoto3)
    _ = invoker_mod
    create_lambda_client(
        region_name="us-east-1",
        connect_timeout=tb.API_DOWNSTREAM_CONNECT_TIMEOUT_SECONDS,
        read_timeout=tb.API_DOWNSTREAM_READ_TIMEOUT_SECONDS,
    )
    config = captured["config"]
    assert config.connect_timeout == tb.API_DOWNSTREAM_CONNECT_TIMEOUT_SECONDS
    assert config.read_timeout == tb.API_DOWNSTREAM_READ_TIMEOUT_SECONDS
    assert config.retries == SINGLE_ATTEMPT_RETRIES  # no SDK retries slipped in


class _TimingOutClient:
    """A boto3 Lambda client whose ``invoke`` always read-times-out (a slow-but-not-refused
    downstream). One call only -- proving nothing retries underneath the caller."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        raise ReadTimeoutError(endpoint_url="https://lambda.us-east-1.amazonaws.com")


async def test_a_synchronous_downstream_timeout_is_a_typed_dependency_failure() -> None:
    client = _TimingOutClient()
    invoker = SynchronousLambdaInvoker(client=client, function_name="arn:...:compiler")

    with pytest.raises(ExternalDependencyError) as raised:
        await invoker.invoke(operation="CompileView", payload={})

    assert raised.value.code == PersistenceErrorCode.DEPENDENCY_REJECTED
    assert raised.value.retryable is False  # ambiguous: never auto-report as completed
    assert client.calls == 1


async def test_an_async_dispatch_timeout_is_retryable_unreachable() -> None:
    client = _TimingOutClient()
    invoker = AsynchronousLambdaInvoker(client=client, function_name="arn:...:worker")

    with pytest.raises(ExternalDependencyError) as raised:
        await invoker.dispatch(operation="RUN_MONITOR", payload={})

    assert raised.value.code == PersistenceErrorCode.DEPENDENCY_UNAVAILABLE
    assert raised.value.retryable is True  # the handover definitely did not happen
    assert client.calls == 1
