"""Pure deterministic implementation of the ordered policy/v1 compiler."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from chorus.domain.entities import (
    CaseState,
    CommunityCase,
    DisclosureScope,
    EvidenceItem,
    EvidenceRoot,
    ExtractionStatus,
    FactType,
    MalwareScanStatus,
    MandateStatus,
    Purpose,
    SensitivityCategory,
)
from chorus.domain.facts import (
    Fact,
    FactStatus,
    IncidentOccurrence,
    LocationArea,
    Report,
    ServiceImpact,
    independent_source_count,
)
from chorus.domain.ids import (
    EvidenceItemId,
    ExportFactId,
    FactId,
    OperationId,
    SafeEvidenceRefId,
    Sha256Digest,
    ViewId,
)
from chorus.domain.mandates import CurrentMandatePointer, DisclosureMandate, FactGrant
from chorus.privacy.canonical import hash_mandate_terms, hash_value, to_canonical_primitive
from chorus.privacy.policy import (
    AGGREGATE_PRIVACY_MIN,
    COMPILER_VERSION,
    CORROBORATION_MIN,
    POLICY_VERSION,
    VIEW_LIFETIME_SECONDS,
    CompileCommand,
    CompileDecision,
    CompilerAuditDecision,
    CompileReason,
    CompileReasonCode,
    CompilerGate,
    ExcludedFact,
    GateOutcome,
    IncludedFact,
    IntendedUsage,
    Necessity,
    RequestedFact,
    SafeDestination,
    SafeEvidenceCandidate,
    validate_compile_command_shape,
)
from chorus.privacy.transformations import (
    ShareableEvidenceRef,
    ShareableFact,
    build_safe_evidence_ref,
    transform_evidence_description,
    transform_facts,
)

_EMPTY_DIGEST = Sha256Digest("sha256:" + "0" * 64)
_UNSAFE_KEY = re.compile(
    r"(?i)(?:^|_)(?:raw|private|contact|email|unit|apartment|health|uri|object_key|mandate_record)(?:_|$)"
)
_UNSAFE_VALUE = re.compile(
    r"(?i)(?:https?://|s3://|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"\b(?:unit|apartment|apt)\s*#?[A-Za-z0-9-]+|SECRET_SENTINEL|mother_health|"
    r"\b(?:mother|father|daughter|son|medical|health|diagnos\w*|wheelchair)\b)"
)
_HARD_INTERNAL_TYPES = {FactType.UNIT_LOCATION, FactType.HEALTH_DETAIL}
_HARD_INTERNAL_SENSITIVITIES = {
    SensitivityCategory.CONTACT,
    SensitivityCategory.UNIT_LOCATION,
    SensitivityCategory.HEALTH,
    SensitivityCategory.MINOR,
    SensitivityCategory.PRIVATE_QUOTE,
    SensitivityCategory.PRIVATE_EVIDENCE_URI,
}
_MINIMUM_NECESSARY_TYPES = {
    FactType.INCIDENT_OCCURRENCE,
    FactType.SERVICE_IMPACT,
    FactType.LOCATION_AREA,
    FactType.IDENTITY_ATTRIBUTE,
    FactType.CONTRADICTION,
    FactType.EVIDENCE_DESCRIPTION,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class MandateVersionRef:
    mandate_id: UUID
    version: int
    terms_hash: Sha256Digest


@dataclass(frozen=True, slots=True, init=False)
class ShareableCaseView:
    """Immutable compiler-produced DTO and sole Action input.

    The private factory prevents accidental construction through the normal API. Python object
    construction is not a security boundary; runtime isolation, IAM, and storage policy are.
    """

    schema_version: str
    view_id: ViewId
    case_id: UUID
    community_public_label: str
    case_version: int
    policy_version: str
    compiler_version: str
    destination: SafeDestination
    purpose: Purpose
    generated_at: datetime
    expires_at: datetime
    mandate_version_set: tuple[MandateVersionRef, ...]
    authorization_snapshot_hash: Sha256Digest
    shareable_facts: tuple[ShareableFact, ...]
    safe_evidence_refs: tuple[ShareableEvidenceRef, ...]
    audit_refs: tuple[UUID, ...]
    view_hash: Sha256Digest

    def __init__(self) -> None:
        raise TypeError("ShareableCaseView is compiler-produced; use PrivacyCompiler.compile")


def _construct_view(*, values: dict[str, object]) -> ShareableCaseView:
    """Populate the closed DTO after the compiler has completed policy evaluation."""

    expected = {item.name for item in fields(ShareableCaseView)}
    if set(values) != expected:
        raise ValueError("shareable view fields are incomplete")
    view = object.__new__(ShareableCaseView)
    for name, value in values.items():
        object.__setattr__(view, name, value)
    return view


@dataclass(frozen=True, slots=True, kw_only=True)
class CompileContext:
    """Strongly loaded state supplied to the pure compiler by a later adapter."""

    case: CommunityCase
    community_public_label: str
    facts: tuple[Fact, ...]
    reports: tuple[Report, ...]
    evidence_items: tuple[EvidenceItem, ...]
    evidence_roots: tuple[EvidenceRoot, ...]
    mandates: tuple[DisclosureMandate, ...]
    mandate_pointers: tuple[CurrentMandatePointer, ...]
    destination_registry_entry: SafeDestination
    safe_evidence_candidates: tuple[SafeEvidenceCandidate, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= len(self.community_public_label) <= 120:
            raise ValueError("community public label length is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompileAllow:
    decision: CompileDecision
    compile_id: UUID
    view: ShareableCaseView
    included: tuple[IncludedFact, ...]
    excluded: tuple[ExcludedFact, ...]
    audit_event_id: UUID
    audit_decisions: tuple[CompilerAuditDecision, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CompileDeny:
    decision: CompileDecision
    compile_id: UUID
    case_id: UUID | None
    current_case_version: int | None
    reasons: tuple[CompileReason, ...]
    audit_event_id: UUID
    audit_decisions: tuple[CompilerAuditDecision, ...]


type CompileResult = CompileAllow | CompileDeny


@dataclass(slots=True)
class _Candidate:
    request: RequestedFact
    fact: Fact
    pointer: CurrentMandatePointer | None = None
    mandate: DisclosureMandate | None = None
    grant: FactGrant | None = None
    excluded_reasons: list[CompileReasonCode] = field(default_factory=list)
    export_ids: list[UUID] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return not self.excluded_reasons


@dataclass(frozen=True, slots=True)
class _CompileArtifactIds:
    """UUIDv5 identities derived only from one compile id and stable typed labels."""

    compile_id: UUID

    def audit_id(self) -> OperationId:
        return OperationId(uuid5(self.compile_id, "compiler/v1:audit"))

    def view_id(self) -> ViewId:
        return ViewId(uuid5(self.compile_id, "compiler/v1:view"))

    def export_fact_id(self, label: str, facts: tuple[Fact, ...]) -> ExportFactId:
        source_key = ",".join(sorted(str(fact.fact_id) for fact in facts))
        return ExportFactId(uuid5(self.compile_id, f"compiler/v1:export-fact:{label}:{source_key}"))

    def safe_evidence_ref_id(self, source_evidence_id: EvidenceItemId) -> SafeEvidenceRefId:
        return SafeEvidenceRefId(
            uuid5(self.compile_id, f"compiler/v1:safe-evidence:{source_evidence_id}")
        )


class PrivacyCompiler:
    """Run the frozen 22 gates without persistence, AWS, an LLM, or ambient authority."""

    def __init__(self) -> None:
        self._policy_build_hash = hash_value(
            {
                "policy_version": POLICY_VERSION,
                "compiler_version": COMPILER_VERSION,
                "rules": (
                    "p1.incident.anonymous.v1",
                    "p1.impact.aggregate.v1",
                    "p1.impact.anonymous.v1",
                    "p1.contradiction.safe.v1",
                    "p1.evidence.photo.v1",
                    "p1.identity.named.v1",
                ),
            }
        )

    def compile(self, command: CompileCommand, context: CompileContext) -> CompileResult:
        """Evaluate policy in its normative order and return an auditable typed result."""

        artifact_ids = _CompileArtifactIds(command.compile_id)
        audit_id = artifact_ids.audit_id().value
        audit: list[CompilerAuditDecision] = []

        # 1. Request/schema.
        try:
            validate_compile_command_shape(command)
        except (TypeError, ValueError):
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                CompilerGate.REQUEST_SCHEMA,
                CompileReasonCode.INVALID_REQUEST,
            )
        self._passed(audit, CompilerGate.REQUEST_SCHEMA)

        # 2. Namespace/community.
        if command.namespace != context.case.namespace:
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                CompilerGate.NAMESPACE_COMMUNITY,
                CompileReasonCode.CASE_NOT_FOUND_OR_FORBIDDEN,
                expose_case=False,
            )
        self._passed(audit, CompilerGate.NAMESPACE_COMMUNITY)

        # 3. Case identity/version.
        inactive_states = {CaseState.RESOLVED, CaseState.CLOSED_UNRESOLVED}
        if (
            command.case_id != context.case.case_id
            or command.expected_case_version != context.case.version
            or context.case.state in inactive_states
        ):
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                CompilerGate.CASE_IDENTITY_VERSION,
                CompileReasonCode.STALE_CASE_VERSION,
            )
        self._passed(audit, CompilerGate.CASE_IDENTITY_VERSION)

        # 4. Cross-case references. Foreign optional input still denies the entire compile.
        for requested in command.requested_facts:
            fact_matches = tuple(
                fact for fact in context.facts if fact.fact_id == requested.fact_id
            )
            if any(
                fact.case_id != context.case.case_id
                or fact.community_id != context.case.community_id
                or fact.namespace != context.case.namespace
                for fact in fact_matches
            ):
                return self._deny(
                    command,
                    context,
                    audit_id,
                    audit,
                    CompilerGate.CROSS_CASE_REFERENCES,
                    CompileReasonCode.CROSS_CASE_REFERENCE,
                    subject_ref=str(requested.fact_id),
                )
        for evidence_id in command.requested_evidence_ids:
            evidence_matches = tuple(
                evidence
                for evidence in context.evidence_items
                if evidence.evidence_id == evidence_id
            )
            if any(
                evidence.case_id != context.case.case_id
                or evidence.community_id != context.case.community_id
                or evidence.namespace != context.case.namespace
                for evidence in evidence_matches
            ):
                return self._deny(
                    command,
                    context,
                    audit_id,
                    audit,
                    CompilerGate.CROSS_CASE_REFERENCES,
                    CompileReasonCode.CROSS_CASE_REFERENCE,
                    subject_ref=str(evidence_id),
                )
        self._passed(audit, CompilerGate.CROSS_CASE_REFERENCES)

        # 5. Existence.
        for requested in command.requested_facts:
            if not any(fact.fact_id == requested.fact_id for fact in context.facts):
                return self._deny(
                    command,
                    context,
                    audit_id,
                    audit,
                    CompilerGate.EXISTENCE,
                    CompileReasonCode.FACT_NOT_FOUND,
                    subject_ref=str(requested.fact_id),
                )
        for evidence_id in command.requested_evidence_ids:
            if not any(item.evidence_id == evidence_id for item in context.evidence_items):
                return self._deny(
                    command,
                    context,
                    audit_id,
                    audit,
                    CompilerGate.EXISTENCE,
                    CompileReasonCode.EVIDENCE_NOT_FOUND,
                    subject_ref=str(evidence_id),
                )
        self._passed(audit, CompilerGate.EXISTENCE)

        # 6. Ownership/contributor lineage.
        if (
            len({fact.fact_id for fact in context.facts}) != len(context.facts)
            or len({report.report_id for report in context.reports}) != len(context.reports)
            or len({item.evidence_id for item in context.evidence_items})
            != len(context.evidence_items)
            or len({root.root_id for root in context.evidence_roots}) != len(context.evidence_roots)
            or len({(root.community_id, root.root_sha256) for root in context.evidence_roots})
            != len(context.evidence_roots)
        ):
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                CompilerGate.OWNERSHIP,
                CompileReasonCode.OWNERSHIP_INTEGRITY_ERROR,
            )
        facts_by_id = {fact.fact_id: fact for fact in context.facts}
        evidence_by_id = {item.evidence_id: item for item in context.evidence_items}
        reports_by_id = {report.report_id: report for report in context.reports}
        roots_by_id = {root.root_id: root for root in context.evidence_roots}
        candidates = [
            _Candidate(request=requested, fact=facts_by_id[requested.fact_id])
            for requested in sorted(command.requested_facts, key=lambda item: str(item.fact_id))
        ]
        requested_fact_evidence = {
            evidence_id for candidate in candidates for evidence_id in candidate.fact.evidence_ids
        }
        if any(
            evidence_id not in requested_fact_evidence
            for evidence_id in command.requested_evidence_ids
        ):
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                CompilerGate.OWNERSHIP,
                CompileReasonCode.OWNERSHIP_INTEGRITY_ERROR,
            )
        for candidate in candidates:
            report = reports_by_id.get(candidate.fact.report_id)
            if (
                candidate.fact.fact_id not in context.case.fact_ids
                or candidate.fact.report_id not in context.case.report_ids
                or report is None
                or report.case_id != candidate.fact.case_id
                or report.community_id != candidate.fact.community_id
                or report.contributor_id != candidate.fact.contributor_id
                or report.namespace != candidate.fact.namespace
                or not set(candidate.fact.source_message_ids).issubset(report.source_message_ids)
            ):
                return self._deny(
                    command,
                    context,
                    audit_id,
                    audit,
                    CompilerGate.OWNERSHIP,
                    CompileReasonCode.OWNERSHIP_INTEGRITY_ERROR,
                    subject_ref=str(candidate.fact.fact_id),
                )
        for evidence_id in command.requested_evidence_ids:
            evidence = evidence_by_id[evidence_id]
            root = roots_by_id.get(evidence.root_id)
            linked_owners = {
                candidate.fact.contributor_id
                for candidate in candidates
                if evidence_id in candidate.fact.evidence_ids
            }
            if (
                root is None
                or root.community_id != context.case.community_id
                or root.namespace != context.case.namespace
                or evidence.submitted_by_contributor_id not in linked_owners
            ):
                return self._deny(
                    command,
                    context,
                    audit_id,
                    audit,
                    CompilerGate.OWNERSHIP,
                    CompileReasonCode.OWNERSHIP_INTEGRITY_ERROR,
                    subject_ref=str(evidence_id),
                )
        self._passed(audit, CompilerGate.OWNERSHIP)

        # 7. Current mandate selection.
        if len({pointer.contributor_id for pointer in context.mandate_pointers}) != len(
            context.mandate_pointers
        ) or len(
            {(pointer.mandate_id, pointer.version) for pointer in context.mandate_pointers}
        ) != len(context.mandate_pointers):
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                CompilerGate.CURRENT_MANDATE_SELECTION,
                CompileReasonCode.MANDATE_INTEGRITY_ERROR,
            )
        pointers_by_contributor = {
            pointer.contributor_id: pointer for pointer in context.mandate_pointers
        }
        gate_reasons: list[CompileReasonCode] = []
        for candidate in candidates:
            pointer = pointers_by_contributor.get(candidate.fact.contributor_id)
            if pointer is None:
                denied = self._ineligible(
                    candidate,
                    CompileReasonCode.MANDATE_NOT_FOUND,
                    command,
                    context,
                    audit_id,
                    audit,
                    CompilerGate.CURRENT_MANDATE_SELECTION,
                )
                if denied is not None:
                    return denied
                gate_reasons.append(CompileReasonCode.MANDATE_NOT_FOUND)
            else:
                candidate.pointer = pointer
        self._gate_complete(audit, CompilerGate.CURRENT_MANDATE_SELECTION, gate_reasons)

        # 8. Mandate version integrity. Any mismatch is structural and always denies.
        mandate_keys = tuple((mandate.mandate_id, mandate.version) for mandate in context.mandates)
        if len(set(mandate_keys)) != len(mandate_keys):
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                CompilerGate.MANDATE_VERSION_INTEGRITY,
                CompileReasonCode.MANDATE_INTEGRITY_ERROR,
            )
        mandates_by_version = {
            (mandate.mandate_id, mandate.version): mandate for mandate in context.mandates
        }
        for candidate in self._eligible(candidates):
            pointer = candidate.pointer
            mandate = (
                mandates_by_version.get((pointer.mandate_id, pointer.version))
                if pointer is not None
                else None
            )
            candidate.mandate = mandate
            if (
                pointer is None
                or mandate is None
                or pointer.case_id != candidate.fact.case_id
                or pointer.contributor_id != candidate.fact.contributor_id
                or pointer.terms_hash != mandate.terms_hash
                or pointer.version != mandate.version
                or mandate.case_id != candidate.fact.case_id
                or mandate.community_id != candidate.fact.community_id
                or mandate.contributor_id != candidate.fact.contributor_id
                or mandate.namespace != candidate.fact.namespace
                or hash_mandate_terms(mandate) != mandate.terms_hash
            ):
                return self._deny(
                    command,
                    context,
                    audit_id,
                    audit,
                    CompilerGate.MANDATE_VERSION_INTEGRITY,
                    CompileReasonCode.MANDATE_INTEGRITY_ERROR,
                    subject_ref=str(candidate.fact.fact_id),
                )
            candidate.grant = next(
                (grant for grant in mandate.fact_grants if grant.fact_id == candidate.fact.fact_id),
                None,
            )
        self._passed(audit, CompilerGate.MANDATE_VERSION_INTEGRITY)

        # 9-15. Item-level ordered authorization gates.
        denied = self._apply_item_gate(
            candidates,
            command,
            context,
            audit_id,
            audit,
            CompilerGate.MANDATE_APPROVAL,
            lambda item: (
                None
                if item.mandate is not None
                and item.mandate.status
                in {MandateStatus.APPROVED, MandateStatus.REVOKED, MandateStatus.EXPIRED}
                and item.mandate.decision_actor_id == item.mandate.contributor_id
                else CompileReasonCode.MANDATE_NOT_APPROVED
            ),
        )
        if denied is not None:
            return denied

        denied = self._apply_item_gate(
            candidates,
            command,
            context,
            audit_id,
            audit,
            CompilerGate.REVOCATION,
            lambda item: (
                CompileReasonCode.MANDATE_REVOKED
                if item.mandate is not None
                and (
                    item.mandate.status is MandateStatus.REVOKED
                    or (
                        item.mandate.revoked_at is not None
                        and item.mandate.revoked_at <= command.requested_at
                    )
                )
                else None
            ),
        )
        if denied is not None:
            return denied

        denied = self._apply_item_gate(
            candidates,
            command,
            context,
            audit_id,
            audit,
            CompilerGate.EXPIRATION,
            lambda item: (
                CompileReasonCode.MANDATE_EXPIRED
                if item.mandate is None
                or item.mandate.status is MandateStatus.EXPIRED
                or command.requested_at < item.mandate.valid_from
                or (
                    item.mandate.expires_at is not None
                    and command.requested_at >= item.mandate.expires_at
                )
                else None
            ),
        )
        if denied is not None:
            return denied

        denied = self._apply_item_gate(
            candidates,
            command,
            context,
            audit_id,
            audit,
            CompilerGate.DESTINATION,
            lambda item: (
                CompileReasonCode.DESTINATION_NOT_ALLOWED
                if item.mandate is None
                or command.destination != context.destination_registry_entry
                or command.destination.destination_id not in item.mandate.allowed_destination_ids
                else None
            ),
        )
        if denied is not None:
            return denied

        denied = self._apply_item_gate(
            candidates,
            command,
            context,
            audit_id,
            audit,
            CompilerGate.PURPOSE,
            lambda item: (
                CompileReasonCode.PURPOSE_NOT_ALLOWED
                if item.mandate is None or command.purpose not in item.mandate.allowed_purposes
                else None
            ),
        )
        if denied is not None:
            return denied

        denied = self._apply_item_gate(
            candidates,
            command,
            context,
            audit_id,
            audit,
            CompilerGate.DISCLOSURE_SCOPE,
            self._scope_reason,
        )
        if denied is not None:
            return denied

        denied = self._apply_item_gate(
            candidates,
            command,
            context,
            audit_id,
            audit,
            CompilerGate.IDENTITY,
            self._identity_reason,
        )
        if denied is not None:
            return denied

        # 16. Aggregation threshold is distinct-contributor privacy, not corroboration.
        aggregate_groups: dict[FactType, list[_Candidate]] = {}
        for candidate in self._eligible(candidates):
            if (
                candidate.grant is not None
                and candidate.grant.max_scope is DisclosureScope.AGGREGATE_ONLY
            ):
                aggregate_groups.setdefault(candidate.fact.fact_type, []).append(candidate)
        gate_reasons = []
        for group in aggregate_groups.values():
            if len({item.fact.contributor_id for item in group}) < AGGREGATE_PRIVACY_MIN:
                for candidate in group:
                    denied = self._ineligible(
                        candidate,
                        CompileReasonCode.AGGREGATE_PRIVACY_MIN_NOT_MET,
                        command,
                        context,
                        audit_id,
                        audit,
                        CompilerGate.AGGREGATION_THRESHOLD,
                    )
                    if denied is not None:
                        return denied
                    gate_reasons.append(CompileReasonCode.AGGREGATE_PRIVACY_MIN_NOT_MET)
        self._gate_complete(audit, CompilerGate.AGGREGATION_THRESHOLD, gate_reasons)

        # 17. Evidence independence recheck remains fixed at two.
        try:
            case_facts = tuple(
                fact
                for fact in context.facts
                if fact.case_id == context.case.case_id
                and fact.community_id == context.case.community_id
                and fact.namespace == context.case.namespace
            )
            computed_sources = independent_source_count(
                case_facts,
                context.reports,
                context.evidence_items,
                context.evidence_roots,
            )
        except ValueError:
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                CompilerGate.INDEPENDENCE,
                CompileReasonCode.OWNERSHIP_INTEGRITY_ERROR,
            )
        if min(computed_sources, context.case.corroboration_source_count) < CORROBORATION_MIN:
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                CompilerGate.INDEPENDENCE,
                CompileReasonCode.CORROBORATION_MIN_NOT_MET,
            )
        self._passed(audit, CompilerGate.INDEPENDENCE)

        # 18. Typed re-identification rules complement earlier scope/identity exclusions.
        denied = self._apply_reidentification_gate(
            candidates,
            aggregate_groups,
            command,
            context,
            audit_id,
            audit,
        )
        if denied is not None:
            return denied

        # 19. Permission alone does not make a fact necessary.
        denied = self._apply_item_gate(
            candidates,
            command,
            context,
            audit_id,
            audit,
            CompilerGate.MINIMUM_NECESSITY,
            lambda item: self._minimum_necessity_reason(item, command),
        )
        if denied is not None:
            return denied

        # 20. Safe evidence metadata is accepted only after a reviewed derivative exists.
        if len(
            {candidate.source_evidence_id for candidate in context.safe_evidence_candidates}
        ) != len(context.safe_evidence_candidates) or len(
            {candidate.export_handle_id for candidate in context.safe_evidence_candidates}
        ) != len(context.safe_evidence_candidates):
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                CompilerGate.EVIDENCE_SAFETY,
                CompileReasonCode.UNSAFE_EVIDENCE,
            )
        safe_candidates = {
            candidate.source_evidence_id: candidate
            for candidate in context.safe_evidence_candidates
        }
        evidence_failure = self._evidence_safety_reason(
            command, candidates, evidence_by_id, safe_candidates
        )
        if evidence_failure is not None:
            failing_candidate, reason = evidence_failure
            denied = self._ineligible(
                failing_candidate,
                reason,
                command,
                context,
                audit_id,
                audit,
                CompilerGate.EVIDENCE_SAFETY,
            )
            if denied is not None:
                return denied
            self._gate_complete(audit, CompilerGate.EVIDENCE_SAFETY, [reason])
        else:
            self._passed(audit, CompilerGate.EVIDENCE_SAFETY)

        # 21. Execute only versioned transformations, construct strict safe types, scan recursively.
        try:
            shareable_facts, safe_refs = self._transform(
                candidates,
                command,
                evidence_by_id,
                safe_candidates,
                artifact_ids,
            )
        except (TypeError, ValueError):
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                CompilerGate.TRANSFORMATION,
                CompileReasonCode.TRANSFORMATION_ERROR,
            )
        if not shareable_facts:
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                CompilerGate.TRANSFORMATION,
                CompileReasonCode.NO_SHAREABLE_FACTS,
            )

        relied_mandates = {
            (candidate.mandate.mandate_id, candidate.mandate.version): candidate.mandate
            for candidate in self._eligible(candidates)
            if candidate.export_ids and candidate.mandate is not None
        }
        mandate_refs = tuple(
            MandateVersionRef(
                mandate_id=mandate.mandate_id.value,
                version=mandate.version,
                terms_hash=mandate.terms_hash,
            )
            for mandate in sorted(
                relied_mandates.values(), key=lambda item: (str(item.mandate_id), item.version)
            )
        )
        expiries = [
            mandate.expires_at
            for mandate in relied_mandates.values()
            if mandate.expires_at is not None
        ]
        expires_at = command.requested_at + timedelta(seconds=VIEW_LIFETIME_SECONDS)
        if expiries:
            expires_at = min(expires_at, *expiries)

        evaluated_contributors = {candidate.fact.contributor_id for candidate in candidates}
        evaluated_pointers = tuple(
            sorted(
                (
                    pointer
                    for pointer in context.mandate_pointers
                    if pointer.contributor_id in evaluated_contributors
                ),
                key=lambda pointer: (str(pointer.mandate_id), pointer.version),
            )
        )
        case_report_ids = {fact.report_id for fact in case_facts}
        corroboration_reports = tuple(
            sorted(
                (report for report in context.reports if report.report_id in case_report_ids),
                key=lambda report: str(report.report_id),
            )
        )
        corroboration_evidence_ids = {
            evidence_id for fact in case_facts for evidence_id in fact.evidence_ids
        }
        authorization_snapshot_hash = hash_value(
            {
                "case_id": context.case.case_id,
                "case_version": context.case.version,
                "corroboration_source_count": context.case.corroboration_source_count,
                "policy_build_hash": self._policy_build_hash,
                "compiler_version": COMPILER_VERSION,
                "destination": command.destination,
                "purpose": command.purpose,
                "current_mandates": evaluated_pointers,
                "evaluated_facts": tuple(
                    {
                        "fact_id": candidate.fact.fact_id,
                        "version": candidate.fact.version,
                        "status": candidate.fact.status,
                        "evidence_status": candidate.fact.evidence_status,
                    }
                    for candidate in candidates
                ),
                "corroboration_facts": tuple(
                    {
                        "fact_id": fact.fact_id,
                        "case_id": fact.case_id,
                        "community_id": fact.community_id,
                        "namespace": fact.namespace,
                        "report_id": fact.report_id,
                        "contributor_id": fact.contributor_id,
                        "version": fact.version,
                        "status": fact.status,
                        "evidence_ids": tuple(sorted(fact.evidence_ids, key=str)),
                    }
                    for fact in sorted(case_facts, key=lambda item: str(item.fact_id))
                ),
                "corroboration_reports": tuple(
                    {
                        "report_id": report.report_id,
                        "case_id": report.case_id,
                        "community_id": report.community_id,
                        "namespace": report.namespace,
                        "contributor_id": report.contributor_id,
                        "version": report.version,
                        "status": report.status,
                        "duplicate_of_report_id": report.duplicate_of_report_id,
                    }
                    for report in corroboration_reports
                ),
                "corroboration_evidence": tuple(
                    {
                        "evidence_id": evidence_by_id[evidence_id].evidence_id,
                        "case_id": evidence_by_id[evidence_id].case_id,
                        "community_id": evidence_by_id[evidence_id].community_id,
                        "namespace": evidence_by_id[evidence_id].namespace,
                        "root_id": evidence_by_id[evidence_id].root_id,
                        "version": evidence_by_id[evidence_id].version,
                        "sha256": evidence_by_id[evidence_id].sha256,
                    }
                    for evidence_id in sorted(corroboration_evidence_ids, key=str)
                ),
                "evidence_roots": tuple(
                    {
                        "root_id": root.root_id,
                        "community_id": root.community_id,
                        "namespace": root.namespace,
                        "version": root.version,
                        "root_sha256": root.root_sha256,
                        "derivation_kind": root.derivation_kind,
                        "parent_root_id": root.parent_root_id,
                    }
                    for root in sorted(context.evidence_roots, key=lambda item: str(item.root_id))
                ),
                "evaluated_evidence": tuple(
                    {
                        "evidence_id": evidence_by_id[evidence_id].evidence_id,
                        "case_id": evidence_by_id[evidence_id].case_id,
                        "community_id": evidence_by_id[evidence_id].community_id,
                        "namespace": evidence_by_id[evidence_id].namespace,
                        "root_id": evidence_by_id[evidence_id].root_id,
                        "submitted_by_contributor_id": evidence_by_id[
                            evidence_id
                        ].submitted_by_contributor_id,
                        "version": evidence_by_id[evidence_id].version,
                        "sha256": evidence_by_id[evidence_id].sha256,
                        "media_type": evidence_by_id[evidence_id].media_type,
                        "byte_length": evidence_by_id[evidence_id].byte_length,
                        "malware_scan_status": evidence_by_id[evidence_id].malware_scan_status,
                        "extraction_status": evidence_by_id[evidence_id].extraction_status,
                        "has_extracted_text": (
                            evidence_by_id[evidence_id].extracted_text is not None
                        ),
                        "safe_derivative": safe_candidates.get(evidence_id),
                    }
                    for evidence_id in sorted(command.requested_evidence_ids, key=str)
                ),
            }
        )
        view_id = artifact_ids.view_id()
        values: dict[str, object] = {
            "schema_version": "shareable-case-view/v1",
            "view_id": view_id,
            "case_id": context.case.case_id.value,
            "community_public_label": context.community_public_label,
            "case_version": context.case.version,
            "policy_version": POLICY_VERSION,
            "compiler_version": COMPILER_VERSION,
            "destination": command.destination,
            "purpose": command.purpose,
            "generated_at": command.requested_at,
            "expires_at": expires_at,
            "mandate_version_set": mandate_refs,
            "authorization_snapshot_hash": authorization_snapshot_hash,
            "shareable_facts": tuple(
                sorted(shareable_facts, key=lambda item: str(item.export_fact_id))
            ),
            "safe_evidence_refs": tuple(
                sorted(safe_refs, key=lambda item: str(item.safe_evidence_ref_id))
            ),
            "audit_refs": (audit_id,),
            "view_hash": _EMPTY_DIGEST,
        }
        draft_view = _construct_view(values=values)
        if self._unsafe_output(draft_view):
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                CompilerGate.TRANSFORMATION,
                CompileReasonCode.UNSAFE_OUTPUT,
            )
        values["view_hash"] = hash_value(draft_view, omit_fields=frozenset({"view_hash"}))
        view = _construct_view(values=values)
        self._passed(audit, CompilerGate.TRANSFORMATION)

        # 22. Phase 1 produces the canonical audit/hash artifact; Phase 6 persists atomically.
        self._passed(audit, CompilerGate.AUDIT_HASH)
        included = tuple(
            IncludedFact(
                fact_id=candidate.fact.fact_id,
                export_fact_ids=tuple(sorted(set(candidate.export_ids), key=str)),
            )
            for candidate in candidates
            if candidate.export_ids
        )
        excluded = tuple(
            ExcludedFact(
                fact_id=candidate.fact.fact_id,
                reason_codes=tuple(dict.fromkeys(candidate.excluded_reasons)),
            )
            for candidate in candidates
            if candidate.excluded_reasons
        )
        return CompileAllow(
            decision=CompileDecision.ALLOW,
            compile_id=command.compile_id,
            view=view,
            included=included,
            excluded=excluded,
            audit_event_id=audit_id,
            audit_decisions=tuple(audit),
        )

    @staticmethod
    def _passed(audit: list[CompilerAuditDecision], gate: CompilerGate) -> None:
        audit.append(CompilerAuditDecision(gate=gate, outcome=GateOutcome.PASSED))

    @staticmethod
    def _gate_complete(
        audit: list[CompilerAuditDecision],
        gate: CompilerGate,
        reasons: list[CompileReasonCode],
    ) -> None:
        audit.append(
            CompilerAuditDecision(
                gate=gate,
                outcome=GateOutcome.EXCLUDED if reasons else GateOutcome.PASSED,
                reason_codes=tuple(sorted(set(reasons), key=str)),
            )
        )

    @staticmethod
    def _eligible(candidates: list[_Candidate]) -> list[_Candidate]:
        return [candidate for candidate in candidates if candidate.eligible]

    def _deny(
        self,
        command: CompileCommand,
        context: CompileContext,
        audit_id: UUID,
        audit: list[CompilerAuditDecision],
        gate: CompilerGate,
        reason: CompileReasonCode,
        *,
        subject_ref: str | None = None,
        expose_case: bool = True,
    ) -> CompileDeny:
        audit.append(
            CompilerAuditDecision(
                gate=gate,
                outcome=GateOutcome.DENIED,
                reason_codes=(reason,),
            )
        )
        return CompileDeny(
            decision=CompileDecision.DENY,
            compile_id=command.compile_id,
            case_id=context.case.case_id.value if expose_case else None,
            current_case_version=context.case.version if expose_case else None,
            reasons=(CompileReason(code=reason, subject_ref=subject_ref),),
            audit_event_id=audit_id,
            audit_decisions=tuple(audit),
        )

    def _ineligible(
        self,
        candidate: _Candidate,
        reason: CompileReasonCode,
        command: CompileCommand,
        context: CompileContext,
        audit_id: UUID,
        audit: list[CompilerAuditDecision],
        gate: CompilerGate,
    ) -> CompileDeny | None:
        if candidate.request.necessity is Necessity.REQUIRED:
            return self._deny(
                command,
                context,
                audit_id,
                audit,
                gate,
                reason,
                subject_ref=str(candidate.fact.fact_id),
            )
        candidate.excluded_reasons.append(reason)
        return None

    def _apply_item_gate(
        self,
        candidates: list[_Candidate],
        command: CompileCommand,
        context: CompileContext,
        audit_id: UUID,
        audit: list[CompilerAuditDecision],
        gate: CompilerGate,
        evaluator: Callable[[_Candidate], CompileReasonCode | None],
    ) -> CompileDeny | None:
        reasons: list[CompileReasonCode] = []
        for candidate in self._eligible(candidates):
            reason = evaluator(candidate)
            if reason is None:
                continue
            if not isinstance(reason, CompileReasonCode):
                raise TypeError("gate evaluator returned an invalid reason")
            denied = self._ineligible(candidate, reason, command, context, audit_id, audit, gate)
            if denied is not None:
                return denied
            reasons.append(reason)
        self._gate_complete(audit, gate, reasons)
        return None

    @staticmethod
    def _scope_reason(candidate: _Candidate) -> CompileReasonCode | None:
        fact = candidate.fact
        grant = candidate.grant
        if fact.status is not FactStatus.ACTIVE:
            return CompileReasonCode.INTERNAL_ONLY
        if (
            fact.fact_type in _HARD_INTERNAL_TYPES
            or fact.sensitivity in _HARD_INTERNAL_SENSITIVITIES
        ):
            return CompileReasonCode.INTERNAL_ONLY
        if grant is None or grant.max_scope is DisclosureScope.INTERNAL_ONLY:
            return CompileReasonCode.INTERNAL_ONLY
        if not grant.allow_safe_transformation:
            return CompileReasonCode.SCOPE_NOT_ALLOWED
        usage = candidate.request.intended_usage
        if grant.max_scope is DisclosureScope.AGGREGATE_ONLY:
            return (
                None
                if usage is IntendedUsage.AGGREGATION_INPUT
                else CompileReasonCode.SCOPE_NOT_ALLOWED
            )
        if usage is IntendedUsage.AGGREGATION_INPUT:
            return CompileReasonCode.SCOPE_NOT_ALLOWED
        if (
            usage is IntendedUsage.EVIDENCE
            and grant.max_scope is not DisclosureScope.EXTERNAL_ACTION
        ):
            return CompileReasonCode.SCOPE_NOT_ALLOWED
        return None

    @staticmethod
    def _identity_reason(candidate: _Candidate) -> CompileReasonCode | None:
        if candidate.fact.fact_type is not FactType.IDENTITY_ATTRIBUTE:
            return None
        mandate = candidate.mandate
        grant = candidate.grant
        if mandate is None or grant is None:
            return CompileReasonCode.IDENTITY_NOT_ALLOWED
        if grant.max_scope not in {DisclosureScope.NAMED_CASE, DisclosureScope.EXTERNAL_ACTION}:
            return CompileReasonCode.IDENTITY_NOT_ALLOWED
        identity = mandate.identity_grant
        if not identity.externally_shareable or identity.max_scope not in {
            DisclosureScope.NAMED_CASE,
            DisclosureScope.EXTERNAL_ACTION,
        }:
            return CompileReasonCode.IDENTITY_NOT_ALLOWED
        if (
            grant.max_scope is DisclosureScope.EXTERNAL_ACTION
            and identity.max_scope is not DisclosureScope.EXTERNAL_ACTION
        ):
            return CompileReasonCode.IDENTITY_NOT_ALLOWED
        return None

    def _apply_reidentification_gate(
        self,
        candidates: list[_Candidate],
        aggregate_groups: dict[FactType, list[_Candidate]],
        command: CompileCommand,
        context: CompileContext,
        audit_id: UUID,
        audit: list[CompilerAuditDecision],
    ) -> CompileDeny | None:
        """Apply the exact typed policy/v1 checks that remain after gates 14 and 15."""

        reasons: list[CompileReasonCode] = []
        for candidate in self._eligible(candidates):
            if self._reidentification_reason(candidate) is None:
                continue
            denied = self._ineligible(
                candidate,
                CompileReasonCode.REIDENTIFICATION_RISK,
                command,
                context,
                audit_id,
                audit,
                CompilerGate.REIDENTIFICATION,
            )
            if denied is not None:
                return denied
            reasons.append(CompileReasonCode.REIDENTIFICATION_RISK)

        for fact_type in sorted(aggregate_groups, key=str):
            group = [candidate for candidate in aggregate_groups[fact_type] if candidate.eligible]
            if not group:
                continue
            bucket_count = self._aggregate_category_bucket_count(fact_type, group)
            if bucket_count > 1:
                continue
            for candidate in group:
                denied = self._ineligible(
                    candidate,
                    CompileReasonCode.REIDENTIFICATION_RISK,
                    command,
                    context,
                    audit_id,
                    audit,
                    CompilerGate.REIDENTIFICATION,
                )
                if denied is not None:
                    return denied
                reasons.append(CompileReasonCode.REIDENTIFICATION_RISK)

        self._gate_complete(audit, CompilerGate.REIDENTIFICATION, reasons)
        return None

    @staticmethod
    def _reidentification_reason(candidate: _Candidate) -> CompileReasonCode | None:
        fact = candidate.fact
        if (
            fact.fact_type in _HARD_INTERNAL_TYPES
            or fact.sensitivity in _HARD_INTERNAL_SENSITIVITIES
        ):
            return CompileReasonCode.REIDENTIFICATION_RISK
        if fact.fact_type is FactType.MANAGEMENT_STATEMENT:
            # A structured management statement still contains a direct private quote. Only a
            # separately typed CONTRADICTION can produce the neutral policy/v1 transformation.
            return CompileReasonCode.REIDENTIFICATION_RISK
        if fact.fact_type is FactType.INCIDENT_OCCURRENCE:
            # The closed incident transformer always reduces UTC instants to calendar-day spans.
            return (
                None
                if isinstance(fact.value, IncidentOccurrence)
                else CompileReasonCode.REIDENTIFICATION_RISK
            )
        if fact.fact_type is FactType.LOCATION_AREA:
            # LocationArea contains only the four frozen common-area enum values, never unit data.
            return (
                None
                if isinstance(fact.value, LocationArea)
                else CompileReasonCode.REIDENTIFICATION_RISK
            )
        if fact.fact_type is FactType.SERVICE_IMPACT:
            # The transformer emits ImpactCode only; relationship/health narrative is discarded.
            return (
                None
                if isinstance(fact.value, ServiceImpact)
                else CompileReasonCode.REIDENTIFICATION_RISK
            )
        # Identity facts reaching this point already passed the independent identity grant gate.
        return None

    @staticmethod
    def _aggregate_category_bucket_count(fact_type: FactType, group: list[_Candidate]) -> int:
        if fact_type is not FactType.SERVICE_IMPACT:
            return 0
        impacts = tuple(candidate.fact.value for candidate in group)
        if not all(isinstance(impact, ServiceImpact) for impact in impacts):
            return 0
        return len({impact.impact_code for impact in impacts if isinstance(impact, ServiceImpact)})

    @staticmethod
    def _minimum_necessity_reason(
        candidate: _Candidate, command: CompileCommand
    ) -> CompileReasonCode | None:
        if candidate.fact.fact_type not in _MINIMUM_NECESSARY_TYPES:
            return CompileReasonCode.NOT_MINIMUM_NECESSARY
        if candidate.fact.fact_type is FactType.EVIDENCE_DESCRIPTION:
            requested = set(command.requested_evidence_ids).intersection(
                candidate.fact.evidence_ids
            )
            if candidate.request.intended_usage is not IntendedUsage.EVIDENCE or not requested:
                return CompileReasonCode.NOT_MINIMUM_NECESSARY
        return None

    @staticmethod
    def _evidence_safety_reason(
        command: CompileCommand,
        candidates: list[_Candidate],
        evidence_by_id: dict[EvidenceItemId, EvidenceItem],
        safe_candidates: dict[EvidenceItemId, SafeEvidenceCandidate],
    ) -> tuple[_Candidate, CompileReasonCode] | None:
        evidence_requests = set(command.requested_evidence_ids)
        for candidate in candidates:
            if not candidate.eligible:
                continue
            requested_for_fact = evidence_requests.intersection(candidate.fact.evidence_ids)
            if (
                candidate.request.intended_usage is IntendedUsage.EVIDENCE
                and not requested_for_fact
            ):
                return candidate, CompileReasonCode.UNSAFE_EVIDENCE
            if (
                candidate.fact.fact_type is FactType.EVIDENCE_DESCRIPTION
                and len(requested_for_fact) != 1
            ):
                return candidate, CompileReasonCode.UNSAFE_EVIDENCE
            for evidence_id in requested_for_fact:
                item = evidence_by_id[evidence_id]
                safe = safe_candidates.get(evidence_id)
                if (
                    candidate.grant is None
                    or candidate.grant.max_scope is not DisclosureScope.EXTERNAL_ACTION
                    or item.malware_scan_status is not MalwareScanStatus.CLEAN
                    or item.media_type not in {"image/jpeg", "image/png"}
                    or item.byte_length > 10_000_000
                    or item.extraction_status is not ExtractionStatus.NOT_NEEDED
                    or item.extracted_text is not None
                    or safe is None
                    or not safe.human_reviewed
                ):
                    return candidate, CompileReasonCode.UNSAFE_EVIDENCE
        return None

    def _transform(
        self,
        candidates: list[_Candidate],
        command: CompileCommand,
        evidence_by_id: dict[EvidenceItemId, EvidenceItem],
        safe_candidates: dict[EvidenceItemId, SafeEvidenceCandidate],
        artifact_ids: _CompileArtifactIds,
    ) -> tuple[list[ShareableFact], list[ShareableEvidenceRef]]:
        facts: list[ShareableFact] = []
        safe_refs: list[ShareableEvidenceRef] = []
        safe_refs_by_source: dict[EvidenceItemId, ShareableEvidenceRef] = {}
        grouped_ids: set[FactId] = set()

        def add_group(group: list[_Candidate], scope: DisclosureScope, stable_label: str) -> None:
            source_facts = tuple(item.fact for item in group)
            export_id = artifact_ids.export_fact_id(stable_label, source_facts)
            output = transform_facts(
                export_fact_id=export_id,
                facts=source_facts,
                effective_scope=scope,
            )
            facts.append(output)
            for item in group:
                item.export_ids.append(export_id.value)
                grouped_ids.add(item.fact.fact_id)

        eligible = self._eligible(candidates)
        aggregate_groups: dict[FactType, list[_Candidate]] = {}
        incident_groups: dict[DisclosureScope, list[_Candidate]] = {}
        for candidate in eligible:
            if candidate.grant is None:
                continue
            if candidate.grant.max_scope is DisclosureScope.AGGREGATE_ONLY:
                aggregate_groups.setdefault(candidate.fact.fact_type, []).append(candidate)
            elif candidate.fact.fact_type is FactType.INCIDENT_OCCURRENCE:
                incident_groups.setdefault(candidate.grant.max_scope, []).append(candidate)
        for fact_type in sorted(aggregate_groups, key=str):
            add_group(
                aggregate_groups[fact_type],
                DisclosureScope.AGGREGATE_ONLY,
                f"aggregate:{fact_type.value}",
            )
        for scope in sorted(incident_groups, key=str):
            add_group(
                incident_groups[scope],
                scope,
                f"incident:{scope.value}",
            )

        for candidate in eligible:
            if candidate.fact.fact_id in grouped_ids or candidate.grant is None:
                continue
            scope = candidate.grant.max_scope
            requested_evidence = sorted(
                set(command.requested_evidence_ids).intersection(candidate.fact.evidence_ids),
                key=str,
            )
            if candidate.fact.fact_type is FactType.EVIDENCE_DESCRIPTION and requested_evidence:
                evidence_id = requested_evidence[0]
                item = evidence_by_id[evidence_id]
                safe_candidate = safe_candidates[evidence_id]
                safe_ref = safe_refs_by_source.get(evidence_id)
                if safe_ref is None:
                    safe_ref = build_safe_evidence_ref(
                        candidate=safe_candidate,
                        safe_evidence_ref_id=artifact_ids.safe_evidence_ref_id(evidence_id),
                        media_type=item.media_type,
                    )
                    safe_refs_by_source[evidence_id] = safe_ref
                    safe_refs.append(safe_ref)
                export_id = artifact_ids.export_fact_id("evidence-description", (candidate.fact,))
                output = transform_evidence_description(
                    export_fact_id=export_id,
                    fact=candidate.fact,
                    effective_scope=scope,
                    safe_ref=safe_ref,
                )
            else:
                export_id = artifact_ids.export_fact_id("single", (candidate.fact,))
                output = transform_facts(
                    export_fact_id=export_id,
                    facts=(candidate.fact,),
                    effective_scope=scope,
                )
            facts.append(output)
            candidate.export_ids.append(export_id.value)
        return facts, safe_refs

    @staticmethod
    def _unsafe_output(view: ShareableCaseView) -> bool:
        primitive = to_canonical_primitive(view)

        def walk(value: object) -> bool:
            if isinstance(value, str):
                return _UNSAFE_VALUE.search(value) is not None
            if isinstance(value, list):
                return any(walk(item) for item in value)
            if isinstance(value, dict):
                return any(
                    _UNSAFE_KEY.search(str(key)) is not None or walk(item)
                    for key, item in value.items()
                )
            return False

        return walk(primitive)
