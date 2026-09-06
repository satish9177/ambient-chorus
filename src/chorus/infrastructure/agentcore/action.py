"""The live Action adapter: one compiled safe view out, one strict envelope back.

Deliberately thin, and identical in shape to the Monitor's and the Investigator's. It
serializes the invocation envelope, sends it to the named runtime endpoint, and parses the
response with the same strict model the fake adapters satisfy. It does not interpret the
answer, does not repair it, does not retry it, and does not log any part of it.

The payload here is the one payload in the system that is *already external-safe* -- it is the
compiled view and nothing else -- but that changes nothing about the adapter's discipline. The
answer coming back is the text an external recipient would read, so an adapter that patched a
malformed proposal would be editing a message nobody reviewed.

Session identity is random per invocation because V1 agents are stateless. Reusing a session
would give one proposal access to another case's compiled view, which is exactly the implicit
shared state the frozen orchestration decision rules out.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import anyio
from pydantic import ValidationError

from chorus.contracts.action import ActionProposalDraft
from chorus.contracts.common import AgentResultEnvelope
from chorus.infrastructure.agentcore.client import AgentCoreInvoker
from chorus.ports.agents import (
    ActionInvocation,
    ActionRejection,
    ActionResult,
    AgentContractViolationError,
    AgentDependencyError,
)

MAX_PAYLOAD_BYTES = 1_048_576
"""The frozen 1 MiB application payload limit, enforced before the request leaves."""

SESSION_ID_BYTES = 20
"""AgentCore requires a long session identifier; 40 hex characters satisfies it."""


@dataclass(slots=True)
class AgentCoreActionAgent:
    """Invoke the deployed Action runtime once."""

    invoker: AgentCoreInvoker
    runtime_arn: str

    async def invoke_action(self, invocation: ActionInvocation) -> ActionResult:
        payload = invocation.model_dump_json().encode("utf-8")
        if len(payload) > MAX_PAYLOAD_BYTES:
            # Refused locally rather than at the service, so an oversized view is a typed
            # contract failure instead of an opaque transport error.
            raise AgentContractViolationError((ActionRejection.OUTPUT_EXCEEDS_BOUNDS,))
        session_id = secrets.token_hex(SESSION_ID_BYTES)
        raw = await anyio.to_thread.run_sync(
            lambda: self.invoker.invoke(
                runtime_arn=self.runtime_arn, session_id=session_id, payload=payload
            )
        )
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise AgentDependencyError("AGENTCORE_RESPONSE_TOO_LARGE", retryable=False)
        try:
            return AgentResultEnvelope[ActionProposalDraft].model_validate_json(raw)
        except ValidationError as error:
            # The exception is not chained into the message and never logged: a validation
            # report from Pydantic quotes the offending input, which here is model-authored
            # prose intended for an external recipient.
            raise AgentContractViolationError((ActionRejection.SCHEMA_INVALID,)) from error


__all__ = ["MAX_PAYLOAD_BYTES", "AgentCoreActionAgent"]
