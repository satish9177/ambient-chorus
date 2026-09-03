"""The AgentCore entry point: parse a strict envelope, answer with a strict envelope.

Runtime-level validation happens here and is separate from the application's semantic
validation. This layer proves the request is well formed, addressed to *this* agent, and within
the payload bound. The application then proves the answer is about the input that was sent.
Neither layer trusts the other to have done its half.

The request does not name a prompt version and this runtime does not accept one. It runs the
single reviewed prompt that ships inside its own artifact and says which one that was in its
answer; the application refuses any result that names a different one. A caller-supplied prompt
version could only be ignored or -- far worse -- honoured.

Nothing is logged. Not the payload, not the answer, not a truncated preview of either: the
payload is private community text by construction, and a runtime log group is not a private
evidence store.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from hashlib import sha256
from typing import Final, Protocol

from pydantic import ValidationError

from chorus.contracts.common import (
    AGENT_OUTPUT_SCHEMA_VERSION,
    AgentInputEnvelope,
    AgentName,
    AgentResultEnvelope,
)
from chorus.contracts.monitor import MonitorInput, MonitorOutput
from runtimes.monitor.agent import MODEL_READ_TIMEOUT_SECONDS, MonitorAgentRunner
from runtimes.monitor.prompt import MONITOR_PROMPT_VERSION, derive_fence

MAX_PAYLOAD_BYTES: Final = 1_048_576

RUNTIME_BUDGET_SECONDS: Final = 60
"""How long this runtime may take before its caller should stop waiting for it.

The middle rung of the timeout hierarchy: strictly greater than the model read timeout, so a
slow model produces a typed failure from *inside* the runtime, and strictly less than the
application's AgentCore read timeout, so the application never gives up on a runtime that is
still working and launches a second one beside it. A test asserts the ordering rather than
trusting three separately chosen numbers to stay consistent with each other.

It is **enforced**, not merely declared. A number that only appears in a docstring bounds
nothing: a model call that hangs past its own read timeout, or a runner that stalls between
one, would run until the caller's transport gave up -- and the caller giving up is exactly the
event that licenses a second invocation over the same private payload. So the runner is
wrapped in :func:`asyncio.timeout`, which cancels the in-flight coroutine at the budget rather
than abandoning it to keep running beside its replacement.
"""

MODEL_ID_VARIABLE: Final = "CHORUS_MONITOR_MODEL_PROFILE_ARN"
REGION_VARIABLE: Final = "AWS_REGION"


class MonitorRunner(Protocol):
    """The only thing this entry point needs from whatever answers an invocation.

    A structural protocol rather than the concrete runner class, because the budget wrapper
    and the envelope assembly are the parts under test and neither depends on Strands being
    installed. Declaring the shape here also keeps the dependency one-directional: the runner
    satisfies the entry point, not the other way round.
    """

    @property
    def model_id(self) -> str:
        """The inference profile identifier, hashed into the answer and never carried whole."""

    async def run(self, payload: MonitorInput, *, fence: str) -> MonitorOutput:
        """Return the structured answer for one bounded payload."""


class RuntimeContractError(ValueError):
    """The request is not a valid Monitor invocation; it is refused before any model call."""


class RuntimeBudgetExceededError(RuntimeContractError):
    """The runner did not finish inside the runtime budget and was cancelled.

    Raised only after the in-flight work has actually been cancelled, so there is no orphan
    model call still running beside whatever the caller does next. It carries no detail about
    what the invocation contained, for the same reason nothing else here does.
    """

    def __init__(self) -> None:
        super().__init__("the Monitor runtime exceeded its budget")


def parse_invocation(raw: bytes) -> AgentInputEnvelope[MonitorInput]:
    """Validate one request envelope, or refuse it with a message that quotes nothing."""

    if len(raw) > MAX_PAYLOAD_BYTES:
        raise RuntimeContractError("payload exceeds the frozen bound")
    try:
        invocation = AgentInputEnvelope[MonitorInput].model_validate_json(raw)
    except ValidationError as error:
        raise RuntimeContractError("payload is not a valid Monitor invocation") from error
    if invocation.agent_name is not AgentName.MONITOR:
        raise RuntimeContractError("invocation is addressed to a different agent")
    return invocation


async def handle(
    raw: bytes,
    *,
    runner: MonitorRunner | None = None,
    budget_seconds: float = RUNTIME_BUDGET_SECONDS,
) -> bytes:
    """Answer one invocation with a serialized result envelope, inside the runtime budget.

    ``budget_seconds`` is a parameter rather than a constant read at the call site so a test
    can prove the enforcement without waiting a minute for it. The default is the frozen
    budget, and nothing in the deployed path passes anything else.
    """

    invocation = parse_invocation(raw)
    active = runner if runner is not None else _runner_from_environment()
    # Derived here rather than inside the runner: the fence must come from the server-generated
    # invocation identity, and only this layer holds the envelope that carries it.
    fence = derive_fence(invocation.payload, invocation.invocation_id)
    started = datetime.now(UTC)
    try:
        async with asyncio.timeout(budget_seconds):
            output: MonitorOutput = await active.run(invocation.payload, fence=fence)
    except TimeoutError as error:
        # ``asyncio.timeout`` cancels the awaited coroutine before it re-raises, so the model
        # call is genuinely over rather than merely no longer awaited.
        raise RuntimeBudgetExceededError() from error
    completed = datetime.now(UTC)
    envelope = AgentResultEnvelope[MonitorOutput](
        schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
        invocation_id=invocation.invocation_id,
        namespace=invocation.namespace,
        agent_name=AgentName.MONITOR,
        case_id=invocation.case_id,
        case_version=invocation.case_version,
        model_profile_arn_hash=model_profile_hash(active.model_id),
        prompt_version=MONITOR_PROMPT_VERSION,
        started_at=started,
        completed_at=completed,
        output=output,
    )
    return envelope.model_dump_json().encode("utf-8")


def model_profile_hash(model_id: str) -> str:
    """Hash the inference profile so a result can name it without carrying the ARN."""

    return f"sha256:{sha256(model_id.encode('utf-8')).hexdigest()}"


def timeout_hierarchy() -> tuple[int, int]:
    """The two budgets this artifact owns, innermost first.

    Exposed so a test can assert the ordering against the application's own timeout without
    importing the runtime's internals or restating either number.
    """

    return MODEL_READ_TIMEOUT_SECONDS, RUNTIME_BUDGET_SECONDS


def _runner_from_environment() -> MonitorAgentRunner:
    model_id = os.environ.get(MODEL_ID_VARIABLE, "")
    region_name = os.environ.get(REGION_VARIABLE, "")
    if not model_id or not region_name:
        raise RuntimeContractError("runtime configuration is incomplete")
    return MonitorAgentRunner(model_id=model_id, region_name=region_name)
