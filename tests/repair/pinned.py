"""One fully pinned proposal/view pair, shared by the citation and golden regressions.

Every identifier, instant, label, and digest below is a literal. Nothing is seeded, derived
from a test name, or generated at run time, because a golden over rendered bytes is only a
statement about the renderer if every other input is fixed by hand.

The citation shape is deliberate and embodies the F05 reference-integrity rule:
* ``FACT_ONE``   -- cited by claim 1, claim 2, the request, and caveat 1
  (shared across multiple sections; two claims cite same fact)
* ``FACT_THREE`` -- cited by the request and by nothing else (**request-only**)
* ``FACT_FOUR``  -- cited by caveat 2 and by nothing else (**caveat-only**)
* ``FACT_TWO``   -- present in the bound view, uncited

Under F05, reference markers represent cited facts in first-use order, so
two claims citing the same fact both render [C1], and References contains
each cited fact exactly once with no dangling markers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from chorus.domain.entities import (
    ActionCaveat,
    ActionClaim,
    ActionProposal,
    ActionProposalStatus,
    ActionTone,
    DestinationKind,
    DisclosureScope,
    EvidenceStatus,
    FactType,
    Purpose,
)
from chorus.domain.ids import (
    ActionId,
    CaseId,
    DestinationId,
    ExportFactId,
    SafeEvidenceRefId,
    Sha256Digest,
    ViewId,
)
from chorus.ports.records import (
    StoredMandateVersionRef,
    StoredSafeDestination,
    StoredSafeEvidenceRef,
    StoredShareableFact,
    StoredShareableView,
    TransformationKind,
)
from chorus.privacy.compiler import POLICY_BUILD_HASH

FROM_IDENTITY_ID = "chorus-demo-sender"
DESTINATION_ID = DestinationId("property_manager:demo")
VIEW_HASH = Sha256Digest("sha256:" + "1a" * 32)

VIEW_ID = ViewId(UUID("0a1b2c3d-0000-4000-8000-000000000001"))
CASE_ID = CaseId(UUID("0c0a5e00-0000-4000-8000-000000000002"))
ACTION_ID = ActionId(UUID("0ac710a0-0000-4000-8000-000000000003"))

FACT_ONE = UUID("f1a0b1c2-0000-4000-8000-000000000001")
FACT_TWO = UUID("f2a0b1c2-0000-4000-8000-000000000002")
FACT_THREE = UUID("f3a0b1c2-0000-4000-8000-000000000003")
FACT_FOUR = UUID("f4a0b1c2-0000-4000-8000-000000000004")

EVIDENCE_REF_ID = SafeEvidenceRefId(UUID("e51de4ce-0000-4000-8000-000000000005"))
MANDATE_ID = UUID("0a4da7e0-0000-4000-8000-000000000006")
ROUTING_TOKEN = UUID("40c71460-0000-4000-8000-000000000007")
AUDIT_REF = UUID("a0d17e00-0000-4000-8000-000000000008")
INVOCATION_ID = UUID("10cca110-0000-4000-8000-000000000009")

GENERATED_AT = datetime(2030, 1, 20, 9, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2030, 1, 21, 9, 0, tzinfo=UTC)
CREATED_AT = datetime(2030, 1, 20, 10, 0, tzinfo=UTC)
DEADLINE = datetime(2030, 1, 27, 9, 0, tzinfo=UTC)

_FACT_TEXT = {
    FACT_ONE: "The elevator was out of service on 2030-01-14.",
    FACT_TWO: "4 residents reported an impact on access to the building.",
    FACT_THREE: "The elevator was out of service on 2030-01-19.",
    FACT_FOUR: "A reported repair visit is recorded as disputed.",
}


def _digest(label: str) -> Sha256Digest:
    """A fixed, structurally valid digest. Pinned by hand so a golden stays a golden."""

    body = label.encode("ascii").hex()
    return Sha256Digest("sha256:" + (body * 64)[:64])


def pinned_view() -> StoredShareableView:
    """The exact stored view both goldens render against."""

    return StoredShareableView(
        schema_version="shareable-case-view/v2",
        view_id=VIEW_ID,
        case_id=CASE_ID,
        community_public_label="Maple Court",
        case_version=3,
        authorization_version=2,
        policy_version="policy/v1",
        compiler_version="compiler/v1",
        policy_build_hash=POLICY_BUILD_HASH,
        destination=StoredSafeDestination(
            destination_id=DESTINATION_ID,
            kind=DestinationKind.PROPERTY_MANAGER,
            registry_version=1,
            routing_token=ROUTING_TOKEN,
            display_label="Property Management",
        ),
        purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
        generated_at=GENERATED_AT,
        expires_at=EXPIRES_AT,
        mandate_version_set=(
            StoredMandateVersionRef(mandate_id=MANDATE_ID, version=1, terms_hash=_digest("terms")),
        ),
        authorization_snapshot_hash=_digest("snapshot"),
        shareable_facts=tuple(
            StoredShareableFact(
                export_fact_id=ExportFactId(fact_id),
                fact_type=FactType.INCIDENT_OCCURRENCE,
                safe_text=text,
                effective_scope=DisclosureScope.EXTERNAL_ACTION,
                evidence_status=EvidenceStatus.CORROBORATED,
                contributor_count=4,
                transformation=TransformationKind.AGGREGATED,
                transformation_rule_id="aggregate-incidents/v1",
                safe_evidence_ref_ids=(EVIDENCE_REF_ID,),
                content_hash=_digest("fact"),
            )
            for fact_id, text in sorted(_FACT_TEXT.items(), key=lambda item: str(item[0]))
        ),
        safe_evidence_refs=(
            StoredSafeEvidenceRef(
                safe_evidence_ref_id=EVIDENCE_REF_ID,
                media_type="image/png",
                export_handle_id=UUID("0e40a1d0-0000-4000-8000-00000000000a"),
                sha256=_digest("derivative"),
                caption="A reviewed elevator out-of-service photo is available.",
                created_by_rule_id="evidence-derivative/v1",
                content_hash=_digest("evidence"),
            ),
        ),
        audit_refs=(AUDIT_REF,),
        view_hash=VIEW_HASH,
    )


def pinned_proposal() -> ActionProposal:
    """The exact immutable proposal both goldens render.

    Two claims, a request that introduces one fact of its own, and two caveats one of which
    introduces a fact nothing else cites.
    """

    return ActionProposal(
        action_id=ACTION_ID,
        case_id=CASE_ID,
        case_version=3,
        authorization_version=2,
        view_id=VIEW_ID,
        view_hash=VIEW_HASH,
        subject="Repeated elevator outages at Maple Court",
        claims=(
            ActionClaim(
                claim_id=UUID("c1a10000-0000-4000-8000-00000000000b"),
                text="The elevator was out of service on 2030-01-14.",
                export_fact_ids=(FACT_ONE,),
                claim_hash=_digest("claim1"),
            ),
            ActionClaim(
                claim_id=UUID("c1a20000-0000-4000-8000-00000000000c"),
                text="4 residents reported an impact on access to the building.",
                export_fact_ids=(FACT_ONE,),
                claim_hash=_digest("claim2"),
            ),
        ),
        requested_action="Please inspect and repair the elevator, then confirm the schedule.",
        requested_deadline=DEADLINE,
        request_fact_ids=(FACT_ONE, FACT_THREE),
        caveats=(
            ActionCaveat(
                caveat_id=UUID("cae10000-0000-4000-8000-00000000000d"),
                text="Resident counts are aggregated and not independently inspected.",
                export_fact_ids=(FACT_ONE,),
                caveat_hash=_digest("caveat1"),
            ),
            ActionCaveat(
                caveat_id=UUID("cae20000-0000-4000-8000-00000000000e"),
                text="One reported repair visit is disputed.",
                export_fact_ids=(FACT_FOUR,),
                caveat_hash=_digest("caveat2"),
            ),
        ),
        tone=ActionTone.NEUTRAL,
        agent_invocation_id=INVOCATION_ID,
        prompt_version="action/v1",
        preview_hash=_digest("preview"),
        proposal_hash=_digest("proposal"),
        status=ActionProposalStatus.DRAFT,
        created_at=CREATED_AT,
    )
