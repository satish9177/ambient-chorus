"""The AgentCore entry point: parse a declared operation, answer with a strict envelope.

Runtime-level validation happens here and is separate from the application's semantic
validation. This layer proves the request is well formed, addressed to *this* agent, names an
operation this runtime serves, and is within the payload bound. The application then proves the
answer is about the case that was sent. Neither layer trusts the other to have done its half.

Two operations, one runtime
---------------------------
The Investigator serves ``INVESTIGATE`` and ``EXTRACT_COMMITMENT`` (Phase 11 deployment contract
§ 11, [ADR-027](../../../docs/adr/ADR-027-commitment-extraction-grounding-and-authority.md) § 1).
The operation is **declared by the caller and dispatched on**, never inferred from the shape of
the payload: :data:`~chorus.contracts.agentcore.INVESTIGATOR_REQUEST_ADAPTER` reads the tag
first, selects exactly one member, and refuses an unrecognised tag without parsing either
payload or reaching a model. There is one branch, it is on the tag, and each arm selects a
reviewed prompt and an output schema that were fixed when the artifact was built.

The request does not name a prompt version and this runtime does not accept one. It runs the
reviewed prompt its own artifact ships for the declared operation and says which one that was
in its answer; the application refuses any result that names a different one.

Nothing is logged. Not the payload, not the answer, not a truncated preview of either: the
payload is a whole private case or a stranger's email by construction, and a runtime log group
is not a private evidence store.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Final, Protocol

from pydantic import ValidationError

from chorus.contracts.agentcore import (
    INVESTIGATOR_REQUEST_ADAPTER,
    ExtractCommitmentRequest,
    InvestigateRequest,
    InvestigatorRequest,
)
from chorus.contracts.commitment import (
    COMMITMENT_EXTRACTION_PROMPT_VERSION,
    CommitmentExtractionInput,
    CommitmentExtractionOutput,
)
from chorus.contracts.common import (
    AGENT_OUTPUT_SCHEMA_VERSION,
    AgentInputEnvelope,
    AgentName,
    AgentResultEnvelope,
    StrictModel,
)
from chorus.contracts.investigation import (
    InvestigationAssessmentDraft,
    InvestigationInput,
)
from runtimes.investigator.agent import (
    MODEL_READ_TIMEOUT_SECONDS,
    InvestigatorAgentRunner,
)
from runtimes.investigator.commitment_prompt import derive_commitment_fence
from runtimes.investigator.prompt import INVESTIGATOR_PROMPT_VERSION, derive_fence

MAX_PAYLOAD_BYTES: Final = 1_048_576

RUNTIME_BUDGET_SECONDS: Final = 75
"""How long this runtime may take before its caller should stop waiting for it.

The middle rung of the timeout hierarchy: strictly greater than the model read timeout, so a
slow model produces a typed failure from *inside* the runtime, and strictly less than the
application's AgentCore read timeout, so the application never gives up on a runtime that is
still working and launches a second one beside it. A test asserts the ordering rather than
trusting three separately chosen numbers to stay consistent with each other.

It is **enforced**, not merely declared. A number that only appears in a docstring bounds
nothing, and the caller giving up is exactly the event that licenses a second invocation over
the same private case. So the runner is wrapped in :func:`asyncio.timeout`, which cancels the
in-flight coroutine at the budget rather than abandoning it to keep running beside its
replacement. Both operations share the rung, because both are one model pass on this runtime.
"""

MODEL_ID_VARIABLE: Final = "CHORUS_INVESTIGATOR_MODEL_PROFILE_ARN"
REGION_VARIABLE: Final = "AWS_REGION"


class InvestigationRunner(Protocol):
    """The only thing the ``INVESTIGATE`` arm needs from whatever answers an invocation."""

    @property
    def model_id(self) -> str:
        """The inference profile identifier, hashed into the answer and never carried whole."""

    async def run(self, payload: InvestigationInput, *, fence: str) -> InvestigationAssessmentDraft:
        """Return the structured answer for one bounded case payload."""


class CommitmentExtractionRunner(Protocol):
    """The only thing the ``EXTRACT_COMMITMENT`` arm needs.

    A second protocol rather than a second method on the first, so a test -- and a future
    composition -- can supply one arm without silently satisfying the other. The deployed runner
    satisfies both, which is the point: one runtime, one role, one profile.
    """

    @property
    def model_id(self) -> str:
        """The inference profile identifier, hashed into the answer and never carried whole."""

    async def extract(
        self, payload: CommitmentExtractionInput, *, fence: str
    ) -> CommitmentExtractionOutput:
        """Return the structured commitment candidates for one inbound reply."""


class RuntimeContractError(ValueError):
    """The request is not a valid Investigator invocation; refused before any model call."""


class RuntimeBudgetExceededError(RuntimeContractError):
    """The runner did not finish inside the runtime budget and was cancelled.

    Raised only after the in-flight work has actually been cancelled, so there is no orphan
    model call still running beside whatever the caller does next. It carries no detail about
    what the invocation contained, for the same reason nothing else here does.
    """

    def __init__(self) -> None:
        super().__init__("the Investigator runtime exceeded its budget")


def parse_request(raw: bytes) -> InvestigatorRequest:
    """Validate one request by its declared operation, or refuse it quoting nothing.

    The tag is read before either payload model is tried, so an unknown operation is refused
    without the runtime having attempted to interpret the bytes as anything.

    **The envelope guard is per-operation, because the two operations bind to different things.**
    An investigation is about one version of one case, so its envelope must name both. An
    extraction is bound to one immutable inbound artifact, and the application deliberately
    sends ``case_id=None`` / ``case_version=None`` for it: a case *version* on that envelope
    would be a value nothing checks and everything could drift from
    ([ADR-027](../../../docs/adr/ADR-027-commitment-extraction-grounding-and-authority.md) § 1).
    The case identifier the extraction is about lives in its payload, where the output has to
    echo it back and the application's own envelope guard compares the two.

    So the shared guard is only the one both operations share -- the agent this runtime is --
    and each arm adds its own. A single stricter guard would have refused the accepted
    application envelope, which is a runtime rejecting a request the system is designed to send.
    """

    if len(raw) > MAX_PAYLOAD_BYTES:
        raise RuntimeContractError("payload exceeds the frozen bound")
    try:
        request = INVESTIGATOR_REQUEST_ADAPTER.validate_json(raw)
    except ValidationError as error:
        raise RuntimeContractError("payload is not a valid Investigator request") from error
    if request.invocation.agent_name is not AgentName.INVESTIGATOR:
        raise RuntimeContractError("invocation is addressed to a different agent")
    if isinstance(request, InvestigateRequest) and request.invocation.case_id is None:
        # An investigation is always about exactly one case. An envelope without one could only
        # produce an assessment nothing could be applied to.
        raise RuntimeContractError("an investigation invocation names exactly one case")
    # ``EXTRACT_COMMITMENT`` needs no envelope case, and its payload's own ``case_id`` and
    # ``source_evidence_id`` are non-optional on ``CommitmentExtractionInput``, so a request
    # missing either was already refused by the tagged-union parse above.
    return request


def parse_invocation(raw: bytes) -> AgentInputEnvelope[InvestigationInput]:
    """Validate one ``INVESTIGATE`` request and return its inner envelope.

    Kept because the investigation envelope is what every existing boundary test is written
    against, and because narrowing the request to the arm a caller asked for is exactly what a
    reader wants when they are checking the investigation path.
    """

    request = parse_request(raw)
    if not isinstance(request, InvestigateRequest):
        raise RuntimeContractError("request declares a different operation")
    return request.invocation


async def handle(
    raw: bytes,
    *,
    runner: InvestigationRunner | None = None,
    extraction_runner: CommitmentExtractionRunner | None = None,
    budget_seconds: float = RUNTIME_BUDGET_SECONDS,
) -> bytes:
    """Answer one invocation with a serialized result envelope, inside the runtime budget.

    ``budget_seconds`` is a parameter rather than a constant read at the call site so a test can
    prove the enforcement without waiting a minute for it. The default is the frozen budget, and
    nothing in the deployed path passes anything else.

    The two runner parameters exist for the same reason: a test supplies the arm it is about,
    and the deployed path supplies neither, so each invocation builds its own runner from the
    environment and discards it. Nothing is cached between calls, here or below.
    """

    request = parse_request(raw)
    if isinstance(request, ExtractCommitmentRequest):
        return await _extract(request, extraction_runner, budget_seconds=budget_seconds)
    return await _investigate(request, runner, budget_seconds=budget_seconds)


async def _investigate(
    request: InvestigateRequest,
    runner: InvestigationRunner | None,
    *,
    budget_seconds: float,
) -> bytes:
    """The ``INVESTIGATE`` arm, unchanged in every semantic from Phase 5."""

    active = runner if runner is not None else _runner_from_environment()
    payload = request.invocation.payload
    # Derived here rather than inside the runner: the fence must come from the server-generated
    # invocation identity, and only this layer holds the envelope that carries it.
    fence = derive_fence(payload, request.invocation.invocation_id)
    started = datetime.now(UTC)
    output = await _within_budget(active.run(payload, fence=fence), budget_seconds=budget_seconds)
    invocation = request.invocation
    envelope = AgentResultEnvelope[InvestigationAssessmentDraft](
        schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
        invocation_id=invocation.invocation_id,
        namespace=invocation.namespace,
        agent_name=AgentName.INVESTIGATOR,
        case_id=invocation.case_id,
        case_version=invocation.case_version,
        model_profile_arn_hash=model_profile_hash(active.model_id),
        prompt_version=INVESTIGATOR_PROMPT_VERSION,
        started_at=started,
        completed_at=datetime.now(UTC),
        output=output,
    )
    return envelope.model_dump_json().encode("utf-8")


async def _extract(
    request: ExtractCommitmentRequest,
    runner: CommitmentExtractionRunner | None,
    *,
    budget_seconds: float,
) -> bytes:
    """The ``EXTRACT_COMMITMENT`` arm: one reply in, spans out, nothing decided."""

    active = runner if runner is not None else _runner_from_environment()
    payload = request.invocation.payload
    fence = derive_commitment_fence(payload, request.invocation.invocation_id)
    started = datetime.now(UTC)
    output = await _within_budget(
        active.extract(payload, fence=fence), budget_seconds=budget_seconds
    )
    invocation = request.invocation
    # Written out rather than shared with the other arm: the envelope is generic in its output,
    # so a helper returning a partially built one would have to be parameterised by the shared
    # base -- and a ``StrictModel`` parameter validates away every field the output actually has.
    envelope = AgentResultEnvelope[CommitmentExtractionOutput](
        schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
        invocation_id=invocation.invocation_id,
        namespace=invocation.namespace,
        agent_name=AgentName.INVESTIGATOR,
        case_id=invocation.case_id,
        case_version=invocation.case_version,
        model_profile_arn_hash=model_profile_hash(active.model_id),
        prompt_version=COMMITMENT_EXTRACTION_PROMPT_VERSION,
        started_at=started,
        completed_at=datetime.now(UTC),
        output=output,
    )
    return envelope.model_dump_json().encode("utf-8")


async def _within_budget[OutputT: StrictModel](
    work: Awaitable[OutputT], *, budget_seconds: float
) -> OutputT:
    """Run one model pass under the frozen budget, cancelling it rather than abandoning it."""

    try:
        async with asyncio.timeout(budget_seconds):
            return await work
    except TimeoutError as error:
        # ``asyncio.timeout`` cancels the awaited coroutine before it re-raises, so the model
        # call is genuinely over rather than merely no longer awaited.
        raise RuntimeBudgetExceededError() from error


def model_profile_hash(model_id: str) -> str:
    """Hash the inference profile so a result can name it without carrying the ARN."""

    return f"sha256:{sha256(model_id.encode('utf-8')).hexdigest()}"


def timeout_hierarchy() -> tuple[int, int]:
    """The two budgets this artifact owns, innermost first.

    Exposed so a test can assert the ordering against the application's own timeout without
    importing the runtime's internals or restating either number.
    """

    return MODEL_READ_TIMEOUT_SECONDS, RUNTIME_BUDGET_SECONDS


def _runner_from_environment() -> InvestigatorAgentRunner:
    """Build this invocation's runner from deployment configuration, and fail closed without it.

    Two variables, both deployment-owned, neither reachable from a request: the application
    inference profile this runtime is allowed to invoke, and the region its client speaks to.
    A missing value is refused before any model call rather than defaulted, because a default
    here would be a silent choice of which model read a private case.
    """

    model_id = os.environ.get(MODEL_ID_VARIABLE, "")
    region_name = os.environ.get(REGION_VARIABLE, "")
    if not model_id or not region_name:
        raise RuntimeContractError("runtime configuration is incomplete")
    return InvestigatorAgentRunner(model_id=model_id, region_name=region_name)
