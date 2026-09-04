"""Agent invocation ports and the closed agent-failure taxonomy.

An agent adapter's only job is to carry one strict input envelope to a runtime and bring one
strict output envelope back. It never persists, never authorizes, and never repairs a
malformed answer. Every failure it can produce is one of the closed types below, so the
application can decide *once* whether a failure is safely retryable rather than inferring it
from an SDK exception.

The retry rule is deliberately narrow: only a timeout, a throttle, or a transient runtime
error may be retried, and only while no output has been persisted. A schema violation, an
invented identifier, or a cross-case citation is never retried automatically, because
repeating the same request would only produce the same unusable answer while spending another
invocation against a private payload.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from chorus.contracts.common import AgentInputEnvelope, AgentResultEnvelope
from chorus.contracts.investigation import (
    InvestigationAssessmentDraft,
    InvestigationInput,
)
from chorus.contracts.monitor import MonitorInput, MonitorOutput

type MonitorInvocation = AgentInputEnvelope[MonitorInput]
type MonitorResult = AgentResultEnvelope[MonitorOutput]

type InvestigationInvocation = AgentInputEnvelope[InvestigationInput]
type InvestigationResult = AgentResultEnvelope[InvestigationAssessmentDraft]


class AgentErrorCode(StrEnum):
    """Safe codes the API maps onto the frozen error table."""

    AGENT_CONTRACT_VIOLATION = "AGENT_CONTRACT_VIOLATION"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_DEPENDENCY_ERROR = "AGENT_DEPENDENCY_ERROR"


class AgentRejection(StrEnum):
    """Every deterministic reason a Monitor answer can be refused.

    These are bounded enum values precisely so a rejection can be logged, audited, and
    counted without ever carrying the offending text, identifier, or quotation with it.
    """

    SCHEMA_INVALID = "SCHEMA_INVALID"
    ENVELOPE_MISMATCH = "ENVELOPE_MISMATCH"
    PROMPT_VERSION_MISMATCH = "PROMPT_VERSION_MISMATCH"
    UNKNOWN_MESSAGE_ID = "UNKNOWN_MESSAGE_ID"
    MESSAGE_RESULT_COVERAGE = "MESSAGE_RESULT_COVERAGE"
    DUPLICATE_CITATION = "DUPLICATE_CITATION"
    UNKNOWN_CLIENT_REF = "UNKNOWN_CLIENT_REF"
    UNLINKED_FACT = "UNLINKED_FACT"
    UNLINKED_REPORT = "UNLINKED_REPORT"
    FOREIGN_CASE_ID = "FOREIGN_CASE_ID"
    SOURCE_SPAN_INVALID = "SOURCE_SPAN_INVALID"
    SOURCE_OWNERSHIP_INVALID = "SOURCE_OWNERSHIP_INVALID"
    UNKNOWN_EVIDENCE_ID = "UNKNOWN_EVIDENCE_ID"
    UNSUPPORTED_ISSUE_TYPE = "UNSUPPORTED_ISSUE_TYPE"
    UNSUPPORTED_FACT_TYPE = "UNSUPPORTED_FACT_TYPE"
    SENSITIVITY_MISMATCH = "SENSITIVITY_MISMATCH"
    TIMESTAMP_OUT_OF_RANGE = "TIMESTAMP_OUT_OF_RANGE"
    MODEL_SUPPLIED_IDENTIFIER = "MODEL_SUPPLIED_IDENTIFIER"
    UNSUPPORTED_CANDIDATE_TRANSITION = "UNSUPPORTED_CANDIDATE_TRANSITION"
    OUTPUT_EXCEEDS_BOUNDS = "OUTPUT_EXCEEDS_BOUNDS"
    CANDIDATE_GROUP_INVALID = "CANDIDATE_GROUP_INVALID"
    CANDIDATE_GROUP_INCONSISTENT = "CANDIDATE_GROUP_INCONSISTENT"
    CANDIDATE_GROUP_UNPROVABLE = "CANDIDATE_GROUP_UNPROVABLE"
    AMBIGUOUS_FACT_SLOT = "AMBIGUOUS_FACT_SLOT"


class InvestigationRejection(StrEnum):
    """Every deterministic reason an Investigator answer can be refused.

    A separate closed set from :class:`AgentRejection` because the two agents are refused for
    different things: the Monitor invents reports, the Investigator invents citations. Sharing
    one enum would have produced a vocabulary where half the members can never apply to either
    caller, which is how a reason code stops meaning anything.

    Every member here refuses the **whole** answer. There is no per-item salvage: a model that
    cited one identifier it was never given has demonstrated that the rest of its reading is
    unverified too, and keeping the acceptable half is exactly how a cross-case reference gets
    quietly accepted.
    """

    SCHEMA_INVALID = "SCHEMA_INVALID"
    ENVELOPE_MISMATCH = "ENVELOPE_MISMATCH"
    PROMPT_VERSION_MISMATCH = "PROMPT_VERSION_MISMATCH"
    CASE_MISMATCH = "CASE_MISMATCH"
    CASE_VERSION_MISMATCH = "CASE_VERSION_MISMATCH"
    UNKNOWN_FACT_ID = "UNKNOWN_FACT_ID"
    UNKNOWN_REPORT_ID = "UNKNOWN_REPORT_ID"
    UNKNOWN_EVIDENCE_ID = "UNKNOWN_EVIDENCE_ID"
    UNKNOWN_ROOT_ID = "UNKNOWN_ROOT_ID"
    DUPLICATE_CITATION = "DUPLICATE_CITATION"
    DUPLICATE_FINDING = "DUPLICATE_FINDING"
    DUPLICATE_GROUP = "DUPLICATE_GROUP"
    CONTRADICTION_INVALID = "CONTRADICTION_INVALID"
    COMMITMENT_CITATION_INVALID = "COMMITMENT_CITATION_INVALID"
    OUTPUT_EXCEEDS_BOUNDS = "OUTPUT_EXCEEDS_BOUNDS"
    TRANSACTION_BOUND_EXCEEDED = "TRANSACTION_BOUND_EXCEEDED"


type AgentRejectionCode = AgentRejection | InvestigationRejection


class AgentError(Exception):
    """Base agent failure carrying only a safe code and bounded reason codes.

    A plain exception rather than a frozen dataclass, for the same reason the persistence
    errors are: parts of the Python exception protocol assign to ``__traceback__``, which a
    frozen dataclass refuses, and an error that cannot be re-raised is an error that gets
    replaced by an unrelated failure at the point it mattered most.
    """

    __slots__ = ("code", "reason_codes", "retryable")

    code: AgentErrorCode
    reason_codes: tuple[str, ...]
    retryable: bool

    def __init__(
        self,
        code: AgentErrorCode,
        reason_codes: tuple[str, ...] = (),
        retryable: bool = False,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.reason_codes = reason_codes
        self.retryable = retryable

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code.value!r}, "
            f"reason_codes={self.reason_codes!r}, retryable={self.retryable!r})"
        )


class AgentContractViolationError(AgentError):
    """The answer was structurally or semantically unusable; the whole output is rejected."""

    def __init__(self, reasons: tuple[AgentRejectionCode, ...]) -> None:
        if not reasons:
            raise ValueError("a contract violation must name at least one reason")
        ordered = tuple(sorted({reason.value for reason in reasons}))
        super().__init__(AgentErrorCode.AGENT_CONTRACT_VIOLATION, ordered, retryable=False)


class AgentOutputDriftError(AgentError):
    """A settled fact slot was re-proposed with materially different content.

    Two Monitor invocations read the same messages and answered differently about the same
    authoritative lineage. The first answer is already durable and immutable, so there are
    only two honest outcomes: refuse, or silently overwrite an immutable fact with a later
    model's opinion. This is the refusal.

    It is not retryable. Repeating the request re-asks the question that produced the
    disagreement. A genuine correction is an explicit deterministic supersession path with a
    human or a validated assessment behind it, not a second guess from intake.
    """

    def __init__(self) -> None:
        super().__init__(
            AgentErrorCode.AGENT_CONTRACT_VIOLATION, ("AGENT_OUTPUT_DRIFT",), retryable=False
        )


class AgentTimeoutError(AgentError):
    """The runtime did not answer in time and produced no result to persist."""

    def __init__(self) -> None:
        super().__init__(AgentErrorCode.AGENT_TIMEOUT, ("AGENT_TIMEOUT",), retryable=True)


class AgentDependencyError(AgentError):
    """AgentCore or Bedrock failed.

    ``retryable`` is decided by the adapter, not by the caller: a throttle or a transient 5xx
    before any result exists may be retried once with the same invocation identity, while a
    definite rejection may not.
    """

    def __init__(self, reason: str = "AGENTCORE_UNAVAILABLE", *, retryable: bool = True) -> None:
        super().__init__(AgentErrorCode.AGENT_DEPENDENCY_ERROR, (reason,), retryable=retryable)


class MonitorAgentPort(Protocol):
    """Invoke the Monitor runtime exactly once with one bounded payload."""

    async def invoke_monitor(self, invocation: MonitorInvocation) -> MonitorResult:
        """Return the strict result envelope, or raise a closed :class:`AgentError`.

        Implementations must not retry internally: retry identity belongs to the application
        use case, which owns the invocation ID and knows whether anything was persisted.
        """


class InvestigatorAgentPort(Protocol):
    """Invoke the Investigator runtime exactly once with one bounded case payload."""

    async def invoke_investigator(self, invocation: InvestigationInvocation) -> InvestigationResult:
        """Return the strict result envelope, or raise a closed :class:`AgentError`.

        Same contract as the Monitor port and for the same reason: the adapter carries one
        payload out and one envelope back, and never decides that a second pass over a private
        case is safe.
        """
