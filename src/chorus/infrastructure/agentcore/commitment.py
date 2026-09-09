"""The live commitment-extraction adapter: one reply out, one strict envelope back.

The same deployed runtime, the same execution role, and the same application inference profile
as :mod:`chorus.infrastructure.agentcore.investigator` -- and a different declared operation.
That is the whole difference, and it is stated in the payload rather than inferred from it
(Phase 11 deployment contract § 11).

A separate adapter class rather than a second method on the investigator adapter, because the
ports are separate: :class:`~chorus.ports.agents.CommitmentExtractionPort` and
:class:`~chorus.ports.agents.InvestigatorAgentPort` answer different questions under different
reviewed prompts, and one class with two methods would let a composition wire the extraction and
get the investigation.

Session identity is random per invocation, for a reason sharper here than anywhere else: the
payload is one stranger's email, and a reused session would carry it into whatever the runtime
was asked next.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import anyio
from pydantic import ValidationError

from chorus.contracts.agentcore import ExtractCommitmentRequest, InvestigatorOperation
from chorus.contracts.commitment import CommitmentExtractionOutput
from chorus.contracts.common import AgentResultEnvelope
from chorus.infrastructure.agentcore.client import AgentCoreInvoker
from chorus.ports.agents import (
    AgentContractViolationError,
    AgentDependencyError,
    CommitmentExtractionInvocation,
    CommitmentExtractionRejection,
    CommitmentExtractionResult,
)

MAX_PAYLOAD_BYTES = 1_048_576
"""The frozen 1 MiB application payload limit, enforced before the request leaves."""

SESSION_ID_BYTES = 20
"""AgentCore requires a long session identifier; 40 hex characters satisfies it."""


@dataclass(slots=True)
class AgentCoreCommitmentExtractionAgent:
    """Invoke the deployed Investigator runtime once, for ``EXTRACT_COMMITMENT``.

    ``runtime_arn`` is the **Investigator** endpoint. There is no fourth runtime, no fourth role,
    and no fourth profile, which is why the extraction inherits the Investigator's IAM position
    exactly rather than acquiring one of its own.
    """

    invoker: AgentCoreInvoker
    runtime_arn: str

    async def invoke_commitment_extraction(
        self, invocation: CommitmentExtractionInvocation
    ) -> CommitmentExtractionResult:
        request = ExtractCommitmentRequest(
            operation=InvestigatorOperation.EXTRACT_COMMITMENT, invocation=invocation
        )
        payload = request.model_dump_json().encode("utf-8")
        if len(payload) > MAX_PAYLOAD_BYTES:
            # Refused locally rather than at the service, so an oversized reply is a typed
            # contract failure instead of an opaque transport error.
            raise AgentContractViolationError((CommitmentExtractionRejection.SCHEMA_INVALID,))
        session_id = secrets.token_hex(SESSION_ID_BYTES)
        raw = await anyio.to_thread.run_sync(
            lambda: self.invoker.invoke(
                runtime_arn=self.runtime_arn, session_id=session_id, payload=payload
            )
        )
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise AgentDependencyError("AGENTCORE_RESPONSE_TOO_LARGE", retryable=False)
        try:
            return AgentResultEnvelope[CommitmentExtractionOutput].model_validate_json(raw)
        except ValidationError as error:
            # The exception is not chained into the message and never logged: a validation
            # report from Pydantic quotes the offending input, which here is model output
            # derived from a stranger's email.
            raise AgentContractViolationError(
                (CommitmentExtractionRejection.SCHEMA_INVALID,)
            ) from error
