"""Closed policy/v1 compile contracts and reason codes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from uuid import UUID

from chorus.domain.entities import (
    DestinationKind,
    DisclosureScope,
    FactType,
    Purpose,
    SensitivityCategory,
)
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
COMPILER_VERSION = "compiler/1.1.0"
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


ALLOWED_PURPOSES: frozenset[Purpose] = frozenset({Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE})
"""Every purpose policy/v1 recognises. A new purpose requires a policy/ADR update."""

ALLOWED_DESTINATION_KINDS: frozenset[DestinationKind] = frozenset(
    {DestinationKind.PROPERTY_MANAGER}
)
"""Every destination kind policy/v1 recognises. There is no wildcard destination in V1."""


SCOPE_PERMITS: dict[DisclosureScope, frozenset[DisclosureScope]] = {
    DisclosureScope.INTERNAL_ONLY: frozenset({DisclosureScope.INTERNAL_ONLY}),
    DisclosureScope.AGGREGATE_ONLY: frozenset(
        {DisclosureScope.INTERNAL_ONLY, DisclosureScope.AGGREGATE_ONLY}
    ),
    DisclosureScope.ANONYMOUS_CASE: frozenset(
        {
            DisclosureScope.INTERNAL_ONLY,
            DisclosureScope.AGGREGATE_ONLY,
            DisclosureScope.ANONYMOUS_CASE,
        }
    ),
    DisclosureScope.NAMED_CASE: frozenset(
        {
            DisclosureScope.INTERNAL_ONLY,
            DisclosureScope.AGGREGATE_ONLY,
            DisclosureScope.ANONYMOUS_CASE,
            DisclosureScope.NAMED_CASE,
        }
    ),
    DisclosureScope.EXTERNAL_ACTION: frozenset(DisclosureScope),
}
"""Which scopes a ceiling permits a grant to hold. An explicit table, never an enum ordinal.

The frozen contract is emphatic that scopes are capabilities rather than a numeric ordering,
so this is written out rather than computed from member order. Two entries deserve their
reasoning stated, because they are the ones that look like an ordering and are not.

``AGGREGATE_ONLY`` appears under every non-internal ceiling. That is not because it sits low on
a scale but because of what it can produce: a compiler-created aggregate backed by at least
three distinct contributors, with no source-level text and no identity. It discloses strictly
less than a standalone anonymous case fact, so a contributor whose ceiling is
``ANONYMOUS_CASE`` or higher can always choose it and always discloses less by doing so.

``EXTERNAL_ACTION`` permits the full set, including ``NAMED_CASE`` -- but permitting a *content*
scope is not permitting identity. Identity requires its own grant, evaluated independently at
compiler gate 15, and no entry in this table can supply it.
"""


def scope_permits(ceiling: DisclosureScope, requested: DisclosureScope) -> bool:
    """True when a ceiling permits a grant at exactly the requested scope."""

    return requested in SCOPE_PERMITS[ceiling]


_FACT_TYPE_CEILING: dict[FactType, DisclosureScope] = {
    FactType.INCIDENT_OCCURRENCE: DisclosureScope.EXTERNAL_ACTION,
    FactType.SERVICE_IMPACT: DisclosureScope.EXTERNAL_ACTION,
    FactType.LOCATION_AREA: DisclosureScope.EXTERNAL_ACTION,
    FactType.CONTRADICTION: DisclosureScope.EXTERNAL_ACTION,
    FactType.EVIDENCE_DESCRIPTION: DisclosureScope.EXTERNAL_ACTION,
    # An identity fact stops at NAMED_CASE. policy/v1's only identity rule is
    # `p1.identity.named.v1`, which produces a display name inside a named case; there is no
    # rule that attaches a name to an outbound action, so a higher ceiling would be an
    # authorization for an export the compiler can never construct.
    FactType.IDENTITY_ATTRIBUTE: DisclosureScope.NAMED_CASE,
    # policy/v1 hard-codes these as non-exportable. A mandate that purported to allow one would
    # be overridden by the compiler anyway; refusing it here means it is never written down as
    # an authorization in the first place.
    FactType.UNIT_LOCATION: DisclosureScope.INTERNAL_ONLY,
    FactType.HEALTH_DETAIL: DisclosureScope.INTERNAL_ONLY,
    # A structured management statement still carries a direct private quote, and the
    # re-identification gate rejects every one of them. Only a separately typed CONTRADICTION
    # produces the neutral policy/v1 transformation.
    FactType.MANAGEMENT_STATEMENT: DisclosureScope.INTERNAL_ONLY,
    # Not in the minimum-necessary allowlist for the policy/v1 purpose.
    FactType.COMMITMENT_TERM: DisclosureScope.INTERNAL_ONLY,
}

_HARD_INTERNAL_SENSITIVITIES: frozenset[SensitivityCategory] = frozenset(
    {
        SensitivityCategory.CONTACT,
        SensitivityCategory.UNIT_LOCATION,
        SensitivityCategory.HEALTH,
        SensitivityCategory.MINOR,
        SensitivityCategory.PRIVATE_QUOTE,
        SensitivityCategory.PRIVATE_EVIDENCE_URI,
    }
)
"""Sensitivity categories policy/v1 keeps internal whatever the fact type says.

The same set the compiler's scope gate uses. Sensitivity is checked *after* type because it
can only ever narrow: a ``SERVICE_IMPACT`` carrying a health category is internal, and no fact
type can raise a ceiling that its sensitivity has lowered.
"""

_PROPOSED_SCOPE: dict[FactType, DisclosureScope] = {
    FactType.INCIDENT_OCCURRENCE: DisclosureScope.ANONYMOUS_CASE,
    FactType.SERVICE_IMPACT: DisclosureScope.ANONYMOUS_CASE,
    FactType.LOCATION_AREA: DisclosureScope.ANONYMOUS_CASE,
    FactType.CONTRADICTION: DisclosureScope.ANONYMOUS_CASE,
}
"""What a proposal offers by default, which is never the ceiling.

Only the four fact types that carry no person-level detail are offered at all, and they are
offered at ``ANONYMOUS_CASE`` -- enough to state that an incident happened, not enough to
attach it to anyone. Everything absent from this mapping is proposed ``INTERNAL_ONLY`` and
has to be widened by a deliberate contributor decision.

``EVIDENCE_DESCRIPTION`` is deliberately absent even though its ceiling is
``EXTERNAL_ACTION``: exporting a photo derivative is exactly the kind of choice that must be
opted into rather than arrived at by accepting a default.
"""

IDENTITY_CEILING: DisclosureScope = DisclosureScope.NAMED_CASE
"""The most identity permission policy/v1 lets a contributor give, for the reason above."""


class MandateDenialCode(StrEnum):
    """Every deterministic reason a proposed or adjusted mandate term is refused.

    Bounded codes, so a denial can be returned, logged, and counted without the fact
    identifier, the scope the caller asked for, or any private value travelling with it.

    ``UNKNOWN_FACT`` deliberately covers four distinct situations: the identifier names no
    fact, it names a fact in another case, it names a fact in another community or namespace,
    and it names a fact owned by a different contributor. Separate codes would be more helpful
    to a well-behaved client and would also be an oracle -- a caller could walk identifiers and
    learn which ones exist inside a case they are not entitled to read, and which of their
    neighbours owns what. One code answers all four identically.
    """

    UNKNOWN_FACT = "UNKNOWN_FACT"
    DUPLICATE_FACT_GRANT = "DUPLICATE_FACT_GRANT"
    SCOPE_EXCEEDS_POLICY_MAXIMUM = "SCOPE_EXCEEDS_POLICY_MAXIMUM"
    IDENTITY_EXCEEDS_POLICY_MAXIMUM = "IDENTITY_EXCEEDS_POLICY_MAXIMUM"
    GRANT_NOT_PROPOSED = "GRANT_NOT_PROPOSED"
    DESTINATION_NOT_ALLOWED = "DESTINATION_NOT_ALLOWED"
    PURPOSE_NOT_ALLOWED = "PURPOSE_NOT_ALLOWED"
    EXPIRY_ALREADY_PASSED = "EXPIRY_ALREADY_PASSED"
    NO_GRANTABLE_FACT = "NO_GRANTABLE_FACT"


def policy_maximum_scope(fact_type: FactType, sensitivity: SensitivityCategory) -> DisclosureScope:
    """Return the highest scope policy/v1 permits a contributor to grant for one fact.

    Authorization is necessary but never sufficient: the compiler re-derives every one of these
    rules at gates 14, 15, 18, and 19 and would exclude the fact anyway. Applying the ceiling
    here means an over-broad grant is never *recorded* as an authorization -- the contributor is
    never shown a permission the system would refuse to honour, and the stored mandate never
    claims something policy would override.
    """

    if sensitivity in _HARD_INTERNAL_SENSITIVITIES:
        return DisclosureScope.INTERNAL_ONLY
    return _FACT_TYPE_CEILING[fact_type]


def proposed_scope(fact_type: FactType, sensitivity: SensitivityCategory) -> DisclosureScope:
    """Return the least-permissive useful scope a proposal offers for one fact."""

    ceiling = policy_maximum_scope(fact_type, sensitivity)
    offered = _PROPOSED_SCOPE.get(fact_type, DisclosureScope.INTERNAL_ONLY)
    return offered if scope_permits(ceiling, offered) else DisclosureScope.INTERNAL_ONLY


def identity_maximum_scope() -> DisclosureScope:
    """Return the highest identity scope policy/v1 permits, independently of any content."""

    return IDENTITY_CEILING


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
