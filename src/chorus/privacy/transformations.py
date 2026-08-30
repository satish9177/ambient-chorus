"""Versioned allowlisted policy/v1 transformations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from uuid import UUID

from chorus.domain.entities import DisclosureScope, EvidenceStatus, FactType
from chorus.domain.facts import (
    Contradiction,
    EvidenceDescription,
    Fact,
    IdentityAttribute,
    IncidentOccurrence,
    LocationArea,
    ServiceImpact,
)
from chorus.domain.ids import ExportFactId, SafeEvidenceRefId, Sha256Digest
from chorus.privacy.canonical import hash_value
from chorus.privacy.policy import SafeEvidenceCandidate, TransformationKind

_EMPTY_DIGEST = Sha256Digest("sha256:" + "0" * 64)


@dataclass(frozen=True, slots=True, kw_only=True)
class ShareableFact:
    """External-safe fact with no private source lineage."""

    export_fact_id: ExportFactId
    fact_type: FactType
    safe_text: str
    effective_scope: DisclosureScope
    evidence_status: EvidenceStatus
    contributor_count: int
    transformation: TransformationKind
    transformation_rule_id: str
    safe_evidence_ref_ids: tuple[SafeEvidenceRefId, ...]
    content_hash: Sha256Digest

    def __post_init__(self) -> None:
        if not 1 <= len(self.safe_text) <= 500:
            raise ValueError("safe fact text length is invalid")
        if self.contributor_count < 1:
            raise ValueError("shareable fact requires a contributor")
        if len(set(self.safe_evidence_ref_ids)) != len(self.safe_evidence_ref_ids):
            raise ValueError("safe evidence references must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class ShareableEvidenceRef:
    """Opaque reference to a separately created external-safe derivative."""

    safe_evidence_ref_id: SafeEvidenceRefId
    media_type: str
    export_handle_id: UUID
    sha256: Sha256Digest
    caption: str
    created_by_rule_id: str
    content_hash: Sha256Digest

    def __post_init__(self) -> None:
        if not 1 <= len(self.media_type) <= 120:
            raise ValueError("safe evidence media type length is invalid")
        if not 1 <= len(self.caption) <= 300:
            raise ValueError("safe evidence caption length is invalid")


def _evidence_status(facts: tuple[Fact, ...]) -> EvidenceStatus:
    statuses = {fact.evidence_status for fact in facts}
    if EvidenceStatus.CONTRADICTED in statuses:
        return EvidenceStatus.CONTRADICTED
    if statuses == {EvidenceStatus.VERIFIED}:
        return EvidenceStatus.VERIFIED
    if EvidenceStatus.CORROBORATED in statuses or EvidenceStatus.VERIFIED in statuses:
        return EvidenceStatus.CORROBORATED
    if EvidenceStatus.REPORTED in statuses:
        return EvidenceStatus.REPORTED
    return EvidenceStatus.UNKNOWN


def _date_span(values: tuple[date, ...]) -> str:
    first = min(values).isoformat()
    last = max(values).isoformat()
    return first if first == last else f"{first} to {last}"


def build_shareable_fact(
    *,
    export_fact_id: ExportFactId,
    facts: tuple[Fact, ...],
    safe_text: str,
    effective_scope: DisclosureScope,
    transformation: TransformationKind,
    rule_id: str,
    safe_evidence_ref_ids: tuple[SafeEvidenceRefId, ...] = (),
) -> ShareableFact:
    """Build and content-hash one safe fact without exporting source IDs."""

    if not facts:
        raise ValueError("transformation requires source facts")
    draft = ShareableFact(
        export_fact_id=export_fact_id,
        fact_type=facts[0].fact_type,
        safe_text=safe_text,
        effective_scope=effective_scope,
        evidence_status=_evidence_status(facts),
        contributor_count=len({fact.contributor_id for fact in facts}),
        transformation=transformation,
        transformation_rule_id=rule_id,
        safe_evidence_ref_ids=tuple(sorted(safe_evidence_ref_ids, key=str)),
        content_hash=_EMPTY_DIGEST,
    )
    return replace(draft, content_hash=hash_value(draft, omit_fields=frozenset({"content_hash"})))


def transform_facts(
    *,
    export_fact_id: ExportFactId,
    facts: tuple[Fact, ...],
    effective_scope: DisclosureScope,
) -> ShareableFact:
    """Apply the single registered policy/v1 rule for a homogeneous fact group."""

    ordered = tuple(sorted(facts, key=lambda fact: str(fact.fact_id)))
    fact_type = ordered[0].fact_type
    if any(fact.fact_type is not fact_type for fact in ordered):
        raise ValueError("transformation group mixes fact types")

    if fact_type is FactType.INCIDENT_OCCURRENCE:
        values = tuple(fact.value for fact in ordered)
        if not all(isinstance(value, IncidentOccurrence) for value in values):
            raise ValueError("incident transformation received the wrong value")
        incidents = tuple(value for value in values if isinstance(value, IncidentOccurrence))
        span = _date_span(tuple(value.occurred_at.date() for value in incidents))
        modes = ", ".join(
            mode.value.lower().replace("_", " ")
            for mode in sorted({item.failure_mode for item in incidents})
        )
        return build_shareable_fact(
            export_fact_id=export_fact_id,
            facts=ordered,
            safe_text=f"Elevator incidents were reported on {span}; observed mode: {modes}.",
            effective_scope=effective_scope,
            transformation=TransformationKind.ANONYMIZED,
            rule_id="p1.incident.anonymous.v1",
        )

    if fact_type is FactType.SERVICE_IMPACT:
        values = tuple(fact.value for fact in ordered)
        if not all(isinstance(value, ServiceImpact) for value in values):
            raise ValueError("impact transformation received the wrong value")
        impacts = tuple(value for value in values if isinstance(value, ServiceImpact))
        categories = ", ".join(sorted({value.impact_code.value.lower() for value in impacts}))
        if effective_scope is DisclosureScope.AGGREGATE_ONLY:
            text = (
                f"{len({fact.contributor_id for fact in ordered})} residents reported "
                f"generalized elevator impacts: {categories}."
            )
            kind = TransformationKind.AGGREGATED
            rule = "p1.impact.aggregate.v1"
        else:
            text = f"An elevator incident caused a generalized impact: {categories}."
            kind = TransformationKind.GENERALIZED
            rule = "p1.impact.anonymous.v1"
        return build_shareable_fact(
            export_fact_id=export_fact_id,
            facts=ordered,
            safe_text=text,
            effective_scope=effective_scope,
            transformation=kind,
            rule_id=rule,
        )

    fact = ordered[0]
    if fact_type is FactType.LOCATION_AREA and isinstance(fact.value, LocationArea):
        area = fact.value.area.value.lower().replace("_", " ")
        return build_shareable_fact(
            export_fact_id=export_fact_id,
            facts=ordered,
            safe_text=f"The reported issue concerns the {area}.",
            effective_scope=effective_scope,
            transformation=TransformationKind.GENERALIZED,
            rule_id="p1.location.common-area.v1",
        )
    if fact_type is FactType.IDENTITY_ATTRIBUTE and isinstance(fact.value, IdentityAttribute):
        return build_shareable_fact(
            export_fact_id=export_fact_id,
            facts=ordered,
            safe_text=fact.value.display_name,
            effective_scope=effective_scope,
            transformation=TransformationKind.DIRECT,
            rule_id="p1.identity.named.v1",
        )
    if fact_type is FactType.CONTRADICTION and isinstance(fact.value, Contradiction):
        return build_shareable_fact(
            export_fact_id=export_fact_id,
            facts=ordered,
            safe_text=(
                "Available reports conflict with a management statement about the elevator issue."
            ),
            effective_scope=effective_scope,
            transformation=TransformationKind.GENERALIZED,
            rule_id="p1.contradiction.safe.v1",
        )
    raise ValueError("no policy/v1 transformation exists for the requested fact type")


def build_safe_evidence_ref(
    *, candidate: SafeEvidenceCandidate, safe_evidence_ref_id: SafeEvidenceRefId, media_type: str
) -> ShareableEvidenceRef:
    """Hash metadata for a reviewed derivative without exposing its source ID or locator."""

    draft = ShareableEvidenceRef(
        safe_evidence_ref_id=safe_evidence_ref_id,
        media_type=media_type,
        export_handle_id=candidate.export_handle_id,
        sha256=candidate.derivative_sha256,
        caption=candidate.caption,
        created_by_rule_id=candidate.transformation_rule_id,
        content_hash=_EMPTY_DIGEST,
    )
    return replace(draft, content_hash=hash_value(draft, omit_fields=frozenset({"content_hash"})))


def transform_evidence_description(
    *,
    export_fact_id: ExportFactId,
    fact: Fact,
    effective_scope: DisclosureScope,
    safe_ref: ShareableEvidenceRef,
) -> ShareableFact:
    if not isinstance(fact.value, EvidenceDescription):
        raise ValueError("safe evidence transformation requires evidence description")
    return build_shareable_fact(
        export_fact_id=export_fact_id,
        facts=(fact,),
        safe_text=safe_ref.caption,
        effective_scope=effective_scope,
        transformation=TransformationKind.GENERALIZED,
        rule_id="p1.evidence.photo.v1",
        safe_evidence_ref_ids=(safe_ref.safe_evidence_ref_id,),
    )
