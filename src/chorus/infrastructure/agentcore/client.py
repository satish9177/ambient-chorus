"""The narrow AgentCore invocation surface and its single boto3 construction point.

The protocol names exactly one call. An adapter that could also list runtimes, read logs, or
describe endpoints would be a broader capability than invoking an agent needs, and the IAM
role backing it grants nothing else either.

SDK retrying is switched off for the same reason it is switched off for DynamoDB: CHORUS
decides whether a second attempt is safe, and it decides using the invocation identity it
owns. A retry hidden inside botocore would spend a second pass over a private payload and
report only the last attempt's outcome.
"""

from __future__ import annotations

from typing import Any, Final, Protocol, cast

from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
    ResponseStreamingError,
)

from chorus.ports.agents import AgentDependencyError, AgentTimeoutError

SINGLE_ATTEMPT_RETRIES: Final = {"mode": "standard", "total_max_attempts": 1}

TRANSIENT_SERVICE_CODES: Final = frozenset(
    {
        "ThrottlingException",
        "ServiceQuotaExceededException",
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelNotReadyException",
        "RuntimeClientError",
    }
)
"""Codes that mean "nothing was produced; asking again may work".

Anything not listed here is treated as a definite rejection. Failing towards *not* retrying is
the safe direction: a missed retry costs one operation, while a retry of a request that did
run costs a second pass over private text.
"""


class AgentCoreInvoker(Protocol):
    """Carry one payload to one runtime endpoint and return the raw response bytes."""

    def invoke(self, *, runtime_arn: str, session_id: str, payload: bytes) -> bytes:
        """Invoke synchronously, raising a closed agent error on failure."""


class Boto3AgentCoreInvoker:
    """The deployed invoker, mapping botocore failures onto the closed agent taxonomy."""

    __slots__ = ("_client", "_qualifier")

    def __init__(self, client: object, *, qualifier: str | None = None) -> None:
        self._client = client
        self._qualifier = qualifier

    def invoke(self, *, runtime_arn: str, session_id: str, payload: bytes) -> bytes:
        request: dict[str, object] = {
            "agentRuntimeArn": runtime_arn,
            "runtimeSessionId": session_id,
            "payload": payload,
        }
        if self._qualifier is not None:
            request["qualifier"] = self._qualifier
        try:
            response: Any = self._client.invoke_agent_runtime(**request)  # type: ignore[attr-defined]
            body: Any = response["response"]
            raw = body.read() if hasattr(body, "read") else body
        except (ReadTimeoutError, ConnectTimeoutError) as error:
            raise AgentTimeoutError() from error
        except (
            ConnectionClosedError,
            EndpointConnectionError,
            ResponseStreamingError,
        ) as error:
            # The connection dropped part-way. Nothing usable came back, and the runtime holds
            # no state we would have to reconcile, so another attempt is safe.
            raise AgentDependencyError("AGENTCORE_UNAVAILABLE", retryable=True) from error
        except ClientError as error:
            raise _classify(error) from error
        if isinstance(raw, bytes):
            return raw
        if isinstance(raw, str):
            return raw.encode("utf-8")
        raise AgentDependencyError("AGENTCORE_RESPONSE_UNREADABLE", retryable=False)


def _classify(error: ClientError) -> AgentDependencyError:
    """Map one service error code, without carrying the message into the raised error."""

    code = str(error.response.get("Error", {}).get("Code", ""))
    if code in TRANSIENT_SERVICE_CODES:
        return AgentDependencyError("AGENTCORE_UNAVAILABLE", retryable=True)
    return AgentDependencyError("AGENTCORE_REJECTED", retryable=False)


def create_agentcore_invoker(
    *, region_name: str, timeout_seconds: int, qualifier: str | None = None
) -> AgentCoreInvoker:
    """Build the boto3 AgentCore client with retrying off and an explicit read timeout.

    The read timeout is the enforcement point for the frozen agent budget: a runtime that has
    not answered by then produces :class:`AgentTimeoutError`, which is the one failure the
    application is allowed to retry.
    """

    import boto3

    client: Any = boto3.client(
        "bedrock-agentcore",
        region_name=region_name,
        config=Config(
            retries=dict(SINGLE_ATTEMPT_RETRIES),
            read_timeout=timeout_seconds,
            connect_timeout=min(timeout_seconds, 10),
        ),
    )
    return cast(AgentCoreInvoker, Boto3AgentCoreInvoker(client, qualifier=qualifier))
