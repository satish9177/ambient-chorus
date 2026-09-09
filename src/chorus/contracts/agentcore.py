"""The Investigator runtime's wire envelope: two bounded operations, declared and never inferred.

The Investigator is the one deployed runtime that answers two different questions
([ADR-027](../../../docs/adr/ADR-027-commitment-extraction-grounding-and-authority.md) § 38, and
the Phase 11 deployment contract § 11): it assesses a case under ``investigator/v1``, and it
extracts commitment candidates from one inbound reply under ``commitment-extraction/v1``. One
runtime, one execution role, one inference profile, one log group -- and two reviewed prompts.

Why a wrapper rather than a shape test
--------------------------------------
``AgentInputEnvelope`` carries an ``agent_name`` and no operation, so a runtime that dispatched
on the payload's shape would be deciding what the model is asked *by looking at the data it was
asked about*. A payload that validated as both -- or one whose validation order happened to try
the wrong model first -- would silently select the wrong reviewed prompt for a stranger's email.
So the operation is a **discriminator the caller must state**, checked before either payload is
parsed, and an unrecognised value fails closed with nothing attempted.

The wrapper is deliberately Investigator-only. The Monitor and the Action runtime each answer
exactly one question, so an operation field on their envelopes would be a constant the caller
could get wrong and nothing could get right.

Nothing deployment-owned appears here. There is no region, no model identifier, no profile ARN,
and no role: those are configuration the runtime reads from its own environment, and a field for
any of them would let the caller choose which model read the payload.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field, TypeAdapter

from chorus.contracts.commitment import CommitmentExtractionInput
from chorus.contracts.common import AgentInputEnvelope, StrictModel
from chorus.contracts.investigation import InvestigationInput

INVESTIGATOR_REQUEST_SCHEMA_VERSION: Final[Literal["investigator-request/v1"]] = (
    "investigator-request/v1"
)


class InvestigatorOperation(StrEnum):
    """The two operations the Investigator runtime serves, and the closed set of them.

    A third member is a deployment decision, not a payload one: it would need its own reviewed
    prompt, its own output contract, and its own live evaluation before it could be answered.
    """

    INVESTIGATE = "INVESTIGATE"
    EXTRACT_COMMITMENT = "EXTRACT_COMMITMENT"


class InvestigateRequest(StrictModel):
    """Assess one case. The payload and its envelope are unchanged from Phase 5."""

    schema_version: Literal["investigator-request/v1"] = INVESTIGATOR_REQUEST_SCHEMA_VERSION
    operation: Literal[InvestigatorOperation.INVESTIGATE]
    invocation: AgentInputEnvelope[InvestigationInput]


class ExtractCommitmentRequest(StrictModel):
    """Extract commitment candidates from one reply. The payload is one reply's text and two
    identifiers, exactly as
    :class:`~chorus.contracts.commitment.CommitmentExtractionInput` froze it."""

    schema_version: Literal["investigator-request/v1"] = INVESTIGATOR_REQUEST_SCHEMA_VERSION
    operation: Literal[InvestigatorOperation.EXTRACT_COMMITMENT]
    invocation: AgentInputEnvelope[CommitmentExtractionInput]


type InvestigatorRequest = InvestigateRequest | ExtractCommitmentRequest

INVESTIGATOR_REQUEST_ADAPTER: Final = TypeAdapter[InvestigatorRequest](
    Annotated[
        InvestigateRequest | ExtractCommitmentRequest,
        Field(discriminator="operation"),
    ]
)
"""Parse a request by its declared operation and nothing else.

A tagged union rather than a left-to-right union attempt: Pydantic reads ``operation`` first,
selects exactly one member, and reports an unknown tag without having tried either payload
model. That is the difference between "the operation was declared" and "the operation was
guessed", and it is the whole reason this module exists.
"""


__all__ = [
    "INVESTIGATOR_REQUEST_ADAPTER",
    "INVESTIGATOR_REQUEST_SCHEMA_VERSION",
    "ExtractCommitmentRequest",
    "InvestigateRequest",
    "InvestigatorOperation",
    "InvestigatorRequest",
]
