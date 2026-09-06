"""The one transport a sender may use to reach the compiler: a synchronous Lambda invoke.

Separated from the adapter so the boundary type can be imported, implemented, and asserted
without boto3 -- which is what lets a test construct the deployed composition with no AWS
anything and still be testing the real object graph.

Nothing is retried here. ``total_max_attempts=1`` is pinned for the same reason it is pinned on
the SES client: the send order makes exactly one deliberate external attempt, and an SDK retry
underneath it is an attempt nothing records. A fence acquisition is safe to repeat in principle
-- the compiler's acquire is idempotent for the same ``execution_id`` -- but the *caller* has to
be the one deciding to repeat it, because only the caller knows how much of its authorization
window it has already spent.

**Phase 8 deploys nothing.** This artifact exists, is composed, and is asserted; the deployed
function and the live invocation belong to Phase 11.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from chorus.ports.errors import ExternalDependencyError, PersistenceErrorCode

LAMBDA_SERVICE_NAME = "lambda"

SINGLE_ATTEMPT_RETRIES: dict[str, Any] = {"mode": "standard", "total_max_attempts": 1}
"""One attempt, counting the initial request. Nothing under the caller retries."""


def _unusable(operation: str) -> ExternalDependencyError:
    """The compiler could not be reached or could not be understood. Never a grant."""

    return ExternalDependencyError(
        operation, code=PersistenceErrorCode.DEPENDENCY_REJECTED, retryable=False
    )


@dataclass(slots=True)
class LambdaCompilerInvoker:
    """Invoke the compiler function synchronously and decode its JSON body.

    ``function_name`` is the ARN from deployment configuration, and it is the only resource the
    sender's ``lambda:InvokeFunction`` grant names. The client is injected rather than
    constructed so this class holds no credential-acquisition behaviour of its own.
    """

    client: Any
    function_name: str

    async def invoke(self, *, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.invoke(
            FunctionName=self.function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps({"operation": operation, "payload": payload}).encode("utf-8"),
        )
        if response.get("FunctionError"):
            # The compiler raised. That is not a denial -- it is the authority failing to
            # answer -- and the two must never collapse into one value.
            raise _unusable(operation)
        try:
            body = json.loads(response["Payload"].read().decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError) as error:
            raise _unusable(operation) from error
        if not isinstance(body, dict):
            raise _unusable(operation)
        return body


def create_lambda_client(*, region_name: str, endpoint_url: str | None = None) -> Any:
    """The pinned single-attempt Lambda client, constructed only where one is really needed."""

    import boto3
    from botocore.config import Config

    return boto3.client(
        LAMBDA_SERVICE_NAME,
        region_name=region_name,
        endpoint_url=endpoint_url,
        config=Config(retries=SINGLE_ATTEMPT_RETRIES),
    )


__all__ = [
    "LAMBDA_SERVICE_NAME",
    "SINGLE_ATTEMPT_RETRIES",
    "LambdaCompilerInvoker",
    "create_lambda_client",
]
