"""Shared agent envelope, constrained scalars, and the strict model base.

Every agent invocation crosses a trust boundary, so every field is closed, bounded, and
explicitly typed. ``StrictModel`` forbids unknown attributes and disables value coercion:
a runtime that returns ``"3"`` where an integer is required is a contract violation, not a
number to be repaired.

Nothing in this package knows how to persist, authorize, or send anything. A validated model
here means "well formed", never "true" and never "authorized".
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

AGENT_INPUT_SCHEMA_VERSION: Final = "agent-input/v1"
AGENT_OUTPUT_SCHEMA_VERSION: Final = "agent-output/v1"

MONITOR_PROMPT_VERSION: Final = "monitor/v2"
"""The only Monitor prompt identity this contract version accepts.

The prompt is pinned rather than negotiated. A runtime that answers with a different prompt
version is running text this application did not review, so the application refuses the
result instead of trusting it.

``v2`` states the candidate-grouping invariant of ADR-012 in the prompt itself. The version
moved because the model's instructions changed, not because the schema did: a validator rule
the prompt never asks the model to satisfy is a hidden requirement, and a hidden requirement
fails an honest answer's whole batch.
"""


class AgentName(StrEnum):
    """The three frozen agent identities.

    Declared here rather than imported from ``chorus.ports`` because contracts must not
    depend on the persistence boundary. The application maps between the two enums once.
    """

    MONITOR = "MONITOR"
    INVESTIGATOR = "INVESTIGATOR"
    ACTION = "ACTION"


class StrictModel(BaseModel):
    """Closed, immutable boundary model: no unknown fields and no value coercion."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


NamespaceStr = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,31}$")]
PolicyVersionStr = Annotated[str, StringConstraints(pattern=r"^policy/v[0-9]{1,3}$")]
PromptVersionStr = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}/v[0-9]{1,3}$")]
Sha256DigestStr = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]

ConfidenceStr = Annotated[
    str, StringConstraints(pattern=r"^(?:0(?:\.[0-9]{1,6})?|1(?:\.0{1,6})?)$")
]
"""Confidence as an exact decimal string in ``[0, 1]``.

Floating point is forbidden in every artifact that a decision might later be traced through,
and confidence is diagnostic only: it is never a disclosure threshold, never an authorization,
and never evidence that a proposal is true.
"""

ClientRefStr = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
"""A model-local label used to wire one proposal to another inside a single output.

It is not an identifier. Durable identity is derived deterministically by application code
from validated inputs, so a client reference never survives past validation.
"""

ReasonStr = Annotated[str, StringConstraints(min_length=1, max_length=400)]
ShortTextStr = Annotated[str, StringConstraints(min_length=1, max_length=160)]


def require_utc_datetime(value: datetime) -> datetime:
    """Reject a naive or non-UTC instant at the boundary rather than normalising it."""

    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("instant must be timezone-aware UTC")
    return value.astimezone(UTC)


class AgentInputEnvelope[PayloadT: StrictModel](StrictModel):
    """The common invocation envelope every runtime receives.

    ``namespace``, ``policy_version``, and ``case_id`` are context the agent may read and can
    never act on: the agent has no tool, no credential, and no persistence path, so nothing it
    returns about them is treated as a grant.

    There is deliberately no ``prompt_version`` field. A runtime runs exactly one reviewed
    prompt, pinned inside its own artifact, so a caller-supplied prompt version could only be
    ignored -- or, worse, honoured, which would let the caller select the text the model runs.
    The runtime states which prompt it ran in its *result*, and the application refuses any
    result naming a version other than the one it expects for that agent.
    """

    schema_version: Literal["agent-input/v1"] = AGENT_INPUT_SCHEMA_VERSION
    invocation_id: UUID
    namespace: NamespaceStr
    agent_name: AgentName
    case_id: UUID | None
    case_version: int | None
    requested_at: datetime
    policy_version: PolicyVersionStr
    payload: PayloadT

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        require_utc_datetime(self.requested_at)
        if (self.case_id is None) != (self.case_version is None):
            raise ValueError("case_version is required exactly when case_id is present")
        if self.case_version is not None and self.case_version < 1:
            raise ValueError("case_version must be positive")
        return self


class AgentResultEnvelope[OutputT: StrictModel](StrictModel):
    """The common result envelope every runtime returns.

    It carries no chain of thought, no prompt text, no completion text, and no raw provider
    response. ``model_profile_arn_hash`` is a digest so an invocation record can prove which
    inference profile answered without persisting the ARN itself.
    """

    schema_version: Literal["agent-output/v1"] = AGENT_OUTPUT_SCHEMA_VERSION
    invocation_id: UUID
    namespace: NamespaceStr
    agent_name: AgentName
    case_id: UUID | None
    case_version: int | None
    model_profile_arn_hash: Sha256DigestStr
    prompt_version: PromptVersionStr
    started_at: datetime
    completed_at: datetime
    output: OutputT

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        require_utc_datetime(self.started_at)
        require_utc_datetime(self.completed_at)
        if self.completed_at < self.started_at:
            raise ValueError("completed_at precedes started_at")
        if (self.case_id is None) != (self.case_version is None):
            raise ValueError("case_version is required exactly when case_id is present")
        if self.case_version is not None and self.case_version < 1:
            raise ValueError("case_version must be positive")
        return self


def reject_identifier_shaped(value: str, field_name: str) -> str:
    """Reject a client reference that is trying to look like a durable identifier.

    The contract already refuses to carry a durable ID field, but a model that answers with a
    UUID-shaped client reference is attempting to name persistent state. Refusing the shape
    keeps "the model never chooses durable identity" enforceable rather than merely intended.
    """

    try:
        UUID(value)
    except ValueError:
        return value
    raise ValueError(f"{field_name} must not be an identifier chosen by the model")


BoundedReasons = Annotated[tuple[ReasonStr, ...], Field(max_length=8)]
