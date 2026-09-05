"""Translate the compiler's ``ShareableCaseView`` into the record persistence stores.

The two shapes are field-for-field identical and deliberately not the same type: ``chorus.ports``
may not import ``chorus.privacy``, so the stored view is restated there and a parity test
asserts the field sets never drift. This module is the one place the restatement is crossed,
and it lives in the application layer because that is the only layer permitted to see both.

The translation is total and mechanical. It computes nothing, drops nothing, and defaults
nothing: a field that appeared in the compiled artifact appears in the stored one with the same
value, because a persisted view that differed from the hashed view by even a whitespace would
fail its own hash verification and be unusable by everything downstream.
"""

from __future__ import annotations

from chorus.domain.ids import CaseId
from chorus.ports.records import (
    StoredMandateVersionRef,
    StoredSafeDestination,
    StoredSafeEvidenceRef,
    StoredShareableFact,
    StoredShareableView,
    TransformationKind,
)
from chorus.privacy.compiler import ShareableCaseView


def to_stored_view(view: ShareableCaseView) -> StoredShareableView:
    """Mirror the compiled artifact exactly, converting only nominal identifier types."""

    return StoredShareableView(
        schema_version=view.schema_version,
        view_id=view.view_id,
        case_id=CaseId(view.case_id),
        community_public_label=view.community_public_label,
        case_version=view.case_version,
        policy_version=view.policy_version,
        compiler_version=view.compiler_version,
        destination=StoredSafeDestination(
            destination_id=view.destination.destination_id,
            kind=view.destination.kind,
            registry_version=view.destination.registry_version,
            routing_token=view.destination.routing_token,
            display_label=view.destination.display_label,
        ),
        purpose=view.purpose,
        generated_at=view.generated_at,
        expires_at=view.expires_at,
        mandate_version_set=tuple(
            StoredMandateVersionRef(
                mandate_id=ref.mandate_id,
                version=ref.version,
                terms_hash=ref.terms_hash,
            )
            for ref in view.mandate_version_set
        ),
        authorization_snapshot_hash=view.authorization_snapshot_hash,
        shareable_facts=tuple(
            StoredShareableFact(
                export_fact_id=fact.export_fact_id,
                fact_type=fact.fact_type,
                safe_text=fact.safe_text,
                effective_scope=fact.effective_scope,
                evidence_status=fact.evidence_status,
                contributor_count=fact.contributor_count,
                transformation=TransformationKind(fact.transformation.value),
                transformation_rule_id=fact.transformation_rule_id,
                safe_evidence_ref_ids=fact.safe_evidence_ref_ids,
                content_hash=fact.content_hash,
            )
            for fact in view.shareable_facts
        ),
        safe_evidence_refs=tuple(
            StoredSafeEvidenceRef(
                safe_evidence_ref_id=ref.safe_evidence_ref_id,
                media_type=ref.media_type,
                export_handle_id=ref.export_handle_id,
                sha256=ref.sha256,
                caption=ref.caption,
                created_by_rule_id=ref.created_by_rule_id,
                content_hash=ref.content_hash,
            )
            for ref in view.safe_evidence_refs
        ),
        audit_refs=view.audit_refs,
        view_hash=view.view_hash,
    )
