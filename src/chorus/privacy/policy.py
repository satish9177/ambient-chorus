"""Closed policy/v1 compile contracts and reason codes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from uuid import UUID

from chorus.domain.entities import DestinationKind, Purpose
from chorus.domain.ids import (
    CaseId,
    DestinationId,
    EvidenceItemId,
    FactId,
    Namespace,
    Sha256Digest,
)
from chorus.domain.time import require_utc

POLICY_VERSION = "policy/v1"
COMPILER_CONTRACT_VERSION = "compiler/v1"
COMPILER_VERSION = "compiler/1.0.0"
AGGREGATE_PRIVACY_MIN = 3
CORROBORATION_MIN = 2
VIEW_LIFETIME_SECONDS = 15 * 60
MAX_REQUESTED_FACTS = 100
MAX_REQUESTED_EVIDENCE = 20


class Necessity(StrEnum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"


class IntendedUsage(StrEnum):
    CLAIM = "CLAIM"
    AGGREGATION_INPUT = "AGGREGATION_INPUT"
    EVIDENCE = "EVIDENCE"


class TransformationKind(StrEnum):
    DIRECT = "DIRECT"
    ANONYMIZED = "ANONYMIZED"
    AGGREGATED = "AGGREGATED"
    GENERALIZED = "GENERALIZED"


class CompileDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class GateOutcome(StrEnum):
    PASSED = "PASSED"
    EXCLUDED = "EXCLUDED"
    DENIED = "DENIED"


class CompilerGate(IntEnum):
    REQUEST_SCHEMA = 1
    NAMESPACE_COMMUNITY = 2
    CASE_IDENTITY_VERSION = 3
    CROSS_CASE_REFERENCES = 4
    EXISTENCE = 5
    OWNERSHIP = 6
    CURRENT_MANDATE_SELECTION = 7
    MANDATE_VERSION_INTEGRITY = 8
    MANDATE_APPROVAL = 9
    REVOCATION = 10
    EXPIRATION = 11
    DESTINATION = 12
    PURPOSE = 13
    DISCLOSURE_SCOPE = 14
    IDENTITY = 15
    AGGREGATION_THRESHOLD = 16
    INDEPENDENCE = 17
    REIDENTIFICATION = 18
    MINIMUM_NECESSITY = 19
    EVIDENCE_SAFETY = 20
    TRANSFORMATION = 21
    AUDIT_HASH = 22


class CompileReasonCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    CASE_NOT_FOUND_OR_FORBIDDEN = "CASE_NOT_FOUND_OR_FORBIDDEN"
    STALE_CASE_VERSION = "STALE_CASE_VERSION"
    CROSS_CASE_REFERENCE = "CROSS_CASE_REFERENCE"
    FACT_NOT_FOUND = "FACT_NOT_FOUND"
    EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"
    OWNERSHIP_INTEGRITY_ERROR = "OWNERSHIP_INTEGRITY_ERROR"
    MANDATE_NOT_FOUND = "MANDATE_NOT_FOUND"
    MANDATE_INTEGRITY_ERROR = "MANDATE_INTEGRITY_ERROR"
    MANDATE_NOT_APPROVED = "MANDATE_NOT_APPROVED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    DESTINATION_NOT_ALLOWED = "DESTINATION_NOT_ALLOWED"
    PURPOSE_NOT_ALLOWED = "PURPOSE_NOT_ALLOWED"
    INTERNAL_ONLY = "INTERNAL_ONLY"
    SCOPE_NOT_ALLOWED = "SCOPE_NOT_ALLOWED"
    IDENTITY_NOT_ALLOWED = "IDENTITY_NOT_ALLOWED"
    AGGREGATE_PRIVACY_MIN_NOT_MET = "AGGREGATE_PRIVACY_MIN_NOT_MET"
    CORROBORATION_MIN_NOT_MET = "CORROBORATION_MIN_NOT_MET"
    REIDENTIFICATION_RISK = "REIDENTIFICATION_RISK"
    NOT_MINIMUM_NECESSARY = "NOT_MINIMUM_NECESSARY"
    UNSAFE_EVIDENCE = "UNSAFE_EVIDENCE"
    TRANSFORMATION_ERROR = "TRANSFORMATION_ERROR"
    UNSAFE_OUTPUT = "UNSAFE_OUTPUT"
    NO_SHAREABLE_FACTS = "NO_SHAREABLE_FACTS"


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestedFact:
    fact_id: FactId
    necessity: Necessity
    intended_usage: IntendedUsage


@dataclass(frozen=True, slots=True, kw_only=True)
class SafeDestination:
    """Address-free destination metadata shared with the Action zone."""

    destination_id: DestinationId
    kind: DestinationKind
    registry_version: int
    routing_token: UUID
    display_label: str

    def __post_init__(self) -> None:
        if self.registry_version < 1:
            raise ValueError("destination registry version must be positive")
        if not 1 <= len(self.display_label) <= 120:
            raise ValueError("destination display label length is invalid")
        if "@" in self.display_label or re.search(r"(?i)https?://", self.display_label):
            raise ValueError("destination metadata cannot contain contact data")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompileCommand:
    compile_id: UUID
    namespace: Namespace
    case_id: CaseId
    expected_case_version: int
    requested_facts: tuple[RequestedFact, ...]
    requested_evidence_ids: tuple[EvidenceItemId, ...]
    destination: SafeDestination
    purpose: Purpose
    requested_at: datetime
    policy_version: str = POLICY_VERSION
    compiler_contract_version: str = COMPILER_CONTRACT_VERSION


@dataclass(frozen=True, slots=True, kw_only=True)
class SafeEvidenceCandidate:
    """Pure input describing a reviewed derivative; Phase 6 creates the bytes."""

    source_evidence_id: EvidenceItemId
    export_handle_id: UUID
    derivative_sha256: Sha256Digest
    caption: str
    human_reviewed: bool
    transformation_rule_id: str = "p1.evidence.photo.v1"

    def __post_init__(self) -> None:
        if not 1 <= len(self.caption) <= 300:
            raise ValueError("safe evidence caption length is invalid")
        if "@" in self.caption or re.search(r"(?i)(?:https?|s3)://", self.caption):
            raise ValueError("safe evidence caption contains a forbidden locator")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompileReason:
    code: CompileReasonCode
    subject_ref: str | None
    retryable: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class CompilerAuditDecision:
    gate: CompilerGate
    outcome: GateOutcome
    reason_codes: tuple[CompileReasonCode, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class IncludedFact:
    fact_id: FactId
    export_fact_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ExcludedFact:
    fact_id: FactId
    reason_codes: tuple[CompileReasonCode, ...]


def validate_compile_command_shape(command: CompileCommand) -> None:
    """Gate 1 shape checks that remain meaningful after boundary parsing."""

    require_utc(command.requested_at)
    if command.expected_case_version < 1:
        raise ValueError("expected case version must be positive")
    if command.policy_version != POLICY_VERSION:
        raise ValueError("unsupported policy version")
    if command.compiler_contract_version != COMPILER_CONTRACT_VERSION:
        raise ValueError("unsupported compiler contract version")
    if not 1 <= len(command.requested_facts) <= MAX_REQUESTED_FACTS:
        raise ValueError("requested fact count is invalid")
    if len(command.requested_evidence_ids) > MAX_REQUESTED_EVIDENCE:
        raise ValueError("requested evidence count is invalid")
    fact_ids = tuple(item.fact_id for item in command.requested_facts)
    if len(set(fact_ids)) != len(fact_ids):
        raise ValueError("requested fact IDs must be unique")
    if len(set(command.requested_evidence_ids)) != len(command.requested_evidence_ids):
        raise ValueError("requested evidence IDs must be unique")
