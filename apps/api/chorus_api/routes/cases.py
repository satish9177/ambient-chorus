"""``GET /v1/cases/{case_id}``: the authorized case surface, and the Phase-7 half of it.

[08-api-design.md](../../../../docs/architecture/08-api-design.md) § Case surfaces freezes this
URL and the shape of what it returns. Phase 7 owns exactly one section of that shape --
``current_action``: the immutable proposal, its **regenerated** preview, and the safe ``DRAFT``
execution projection. The remaining sections of ``CaseSurfaceResponse`` -- the evidence summary,
the current shareable view, commitments, and the privacy counts -- belong to phases that own
those artifacts and are added by them, not invented here.

Why the preview is regenerated rather than read
-----------------------------------------------
ADR-022 § 3 persists ``preview_hash`` and **neither body**. The renderer is a pure function of
immutable inputs -- the proposal, its exact bound view, the template version, and the safe
``from_identity_id`` -- so a stored body could only ever agree with a regenerated one or be a
second version of the truth, and it would put the exact external message text into a table the
observability rules forbid it from reaching in logs.

Regeneration is therefore also a *check*. ``preview_matches_committed_hash`` reports whether the
freshly rendered bytes still hash to what the proposal committed, so a template change or a
configuration change is visible to a reader rather than silently served beside an approval that
bound different bytes.

What this surface may not contain
---------------------------------
No raw model output, no prompt text, no recipient address, no sending identity, no private case
title, no private fact, no mandate record, no ``InvestigationAssessment``, no compiler
exclusion, no private evidence locator, no ``AgentInvocationResult``, and no operation binding
hash. The projection is built from the safe proposal, the safe view, and the execution's state,
and there is nowhere in the response model to put anything else.

This is not an approval surface. Reading a ``DRAFT`` is Phase 7; approving or sending one is
Phase 8, and neither verb exists here.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from chorus.application.queries.case_surface import CaseSurfaceExtras
from chorus.application.queries.current_action import CurrentActionProjection
from chorus.domain.ids import CaseId
from chorus.ports.scopes import CaseScope
from chorus_api.dependencies import (
    ApiContainer,
    DemoActor,
    container_of,
    require_actor,
    require_case_reader,
)
from chorus_api.routes.views import ShareableCaseViewBody, view_body

router = APIRouter(tags=["cases"])


class ClaimProjection(BaseModel):
    """One claim's own text and the short export-fact identifiers it cites."""

    text: str
    export_fact_ids: tuple[str, ...]


class CaveatProjection(BaseModel):
    text: str
    export_fact_ids: tuple[str, ...]


class RenderedPreviewProjection(BaseModel):
    """The regenerated bodies and the digest the proposal committed.

    ``preview_hash`` is the *committed* value and ``matches_committed_hash`` says whether the
    bytes above still hash to it. Two fields rather than one, because "these are the bytes" and
    "these are the bytes that were approved" are different claims.
    """

    template_version: str
    text_body: str
    html_body: str
    preview_hash: str
    matches_committed_hash: bool


class ExecutionProjection(BaseModel):
    """The safe execution projection. Its absent fields are absent on purpose (ADR-022 § 1).

    ``approval_id`` is ``None`` at ``DRAFT`` and the row's own durable value from ``APPROVED``
    onward -- surfaced (P2-4) so a browser that approved a proposal and then reloaded before
    executing can read back the exact binding it needs for ``POST .../executions`` instead of
    depending on the one-time approval response it may no longer hold.
    """

    execution_id: UUID
    state: str
    version: int
    approval_id: UUID | None


class CurrentActionResponse(BaseModel):
    action_id: UUID
    status: str
    view_id: UUID
    view_hash: str
    case_version: int
    authorization_version: int
    subject: str
    claims: tuple[ClaimProjection, ...]
    requested_action: str
    caveats: tuple[CaveatProjection, ...]
    tone: str
    proposal_hash: str
    preview: RenderedPreviewProjection
    execution: ExecutionProjection
    approval_authorization_current: bool
    """Whether the durable approval (if any) is still valid for the case's authorization epoch.

    ``True`` when there is no durable approval yet, or when the epoch recorded on the approval
    row equals the case's *current* ``authorization_version``. ``False`` once a mandate
    revocation (or any other authorization-sensitive edge) has bumped that epoch: the approval a
    browser still holds no longer authorizes a send, so the frontend must hide "Execute" and
    require a fresh compile/proposal/approval path. Send-time authority is still re-derived by
    the Phase-8 fence regardless of this hint -- it exists so the UI stops offering a control
    the server would refuse (P2), not to become a second authority.
    """


class EvidenceSummaryRowResponse(BaseModel):
    fact_id: UUID
    fact_type: str
    sensitivity: str
    evidence_status: str
    status: str
    contributor_id: UUID
    evidence_ids: tuple[UUID, ...]
    version: int


class CommitmentSafeResponse(BaseModel):
    """Every field of ``Commitment`` except the four deliberately omitted ones.

    ``source_evidence_id`` and ``due_event_id`` are the correlation/replay identities the
    watcher authenticates against, ``scheduler_name`` is transport addressing, and
    ``verification_evidence_id`` names a private artifact. ``case_id`` and ``schema_version``
    are already carried by the path and the response version.

    ``schedule_status``/``schedule_last_error_code`` (P2-9) are the safe half of
    ``CommitmentScheduleProjection`` -- whether the one-time schedule exists yet, and the one
    closed code naming why not, never ``schedule_name`` or the due-event/replay identities the
    watcher itself authenticates against.
    """

    commitment_id: UUID
    action_id: UUID | None
    obligor: str
    action_text: str
    due_at: str
    verification_method: str
    status: str
    schedule_generation: int
    version: int
    verified_by_contributor_id: UUID | None
    outcome_note: str | None
    created_at: str
    updated_at: str
    schedule_status: str | None
    schedule_last_error_code: str | None


class PrivacyCountsResponse(BaseModel):
    compile_id: UUID
    included: int
    excluded: int
    denied_by_reason: dict[str, int]


class CaseHeaderResponse(BaseModel):
    case_id: UUID
    title: str | None
    state: str
    version: int
    authorization_version: int
    issue_type: str
    corroboration_source_count: int
    state_reason_code: str


class CaseSurfaceResponse(BaseModel):
    """The frozen case surface: the Phase 7 section plus the five Phase 10 completes.

    ``current_action`` is ``null`` when the case has never held a proposal. That is a state, not
    an error: a case in ``READY_FOR_ACTION`` legitimately has no action yet.

    For ``case_approver``, ``case.title``, ``evidence_summary``, and ``privacy_counts`` are
    omitted -- the private title/fact labels and privacy exclusion reasons stay presenter-only,
    and only view/action-safe fields remain.
    """

    case_id: UUID
    case: CaseHeaderResponse | None
    evidence_summary: tuple[EvidenceSummaryRowResponse, ...] | None
    current_shareable_view: ShareableCaseViewBody | None
    current_action: CurrentActionResponse | None
    commitments: tuple[CommitmentSafeResponse, ...]
    privacy_counts: PrivacyCountsResponse | None


@router.get("/cases/{case_id}", response_model=CaseSurfaceResponse)
async def read_case(
    request: Request,
    case_id: UUID,
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> CaseSurfaceResponse:
    """Return the authorized safe surface for one case, preview regenerated on read."""

    require_case_reader(actor)
    container: ApiContainer = container_of(request)
    # The namespace and the community are resolved server-side from the container, never from
    # the request, so a caller cannot read across an isolation boundary by naming one. A case
    # identifier outside this scope simply has no current action, which is the same answer a
    # case that has not been proposed against gives -- non-enumerable by construction.
    scope = CaseScope(
        namespace=container.namespace,
        community_id=container.community_id,
        case_id=CaseId(case_id),
    )
    action_projection = await container.read_current_action.execute(scope)
    presenter = actor is DemoActor.PRESENTER_ADMIN

    extras: CaseSurfaceExtras | None = None
    if container.case_surface is not None:
        extras = await container.case_surface.execute(scope)

    return CaseSurfaceResponse(
        case_id=case_id,
        case=(
            None
            if extras is None
            else CaseHeaderResponse(
                case_id=extras.case.case_id,
                title=extras.case.title if presenter else None,
                state=extras.case.state.value,
                version=extras.case.version,
                authorization_version=extras.case.authorization_version,
                issue_type=extras.case.issue_type,
                corroboration_source_count=extras.case.corroboration_source_count,
                state_reason_code=extras.case.state_reason_code,
            )
        ),
        evidence_summary=(
            None
            if extras is None or not presenter
            else tuple(
                EvidenceSummaryRowResponse(
                    fact_id=row.fact_id,
                    fact_type=row.fact_type.value,
                    sensitivity=row.sensitivity,
                    evidence_status=row.evidence_status.value,
                    status=row.status.value,
                    contributor_id=row.contributor_id,
                    evidence_ids=row.evidence_ids,
                    version=row.version,
                )
                for row in extras.evidence_summary
            )
        ),
        current_shareable_view=(
            None
            if extras is None or extras.current_shareable_view is None
            else view_body(extras.current_shareable_view)
        ),
        current_action=(
            None
            if action_projection is None
            else _project(
                action_projection,
                current_authorization_version=(
                    None if extras is None else extras.case.authorization_version
                ),
            )
        ),
        commitments=(
            ()
            if extras is None
            else tuple(
                CommitmentSafeResponse(
                    commitment_id=item.commitment_id,
                    action_id=item.action_id,
                    obligor=item.obligor,
                    action_text=item.action_text,
                    due_at=item.due_at,
                    verification_method=item.verification_method,
                    status=item.status.value,
                    schedule_generation=item.schedule_generation,
                    version=item.version,
                    verified_by_contributor_id=item.verified_by_contributor_id,
                    outcome_note=item.outcome_note,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    schedule_status=(
                        None if item.schedule_status is None else item.schedule_status.value
                    ),
                    schedule_last_error_code=item.schedule_last_error_code,
                )
                for item in extras.commitments
            )
        ),
        privacy_counts=(
            None
            if extras is None or extras.privacy_counts is None or not presenter
            else PrivacyCountsResponse(
                compile_id=extras.privacy_counts.compile_id,
                included=extras.privacy_counts.included,
                excluded=extras.privacy_counts.excluded,
                denied_by_reason=extras.privacy_counts.denied_by_reason,
            )
        ),
    )


def _project(
    projection: CurrentActionProjection,
    *,
    current_authorization_version: int | None,
) -> CurrentActionResponse:
    """Map the query's projection onto the transport shape.

    The one derived field is ``approval_authorization_current``: a comparison between the
    epoch the durable approval recorded and the case's live ``authorization_version``, both of
    which are authoritative stored values. It is ``True`` when no approval exists yet, or when
    the case surface was served without its header (the Phase-7 fallback), where there is no
    current epoch to compare against and this route has nothing new to assert.
    """

    approval_authorization_current = (
        projection.approval_authorization_version is None
        or current_authorization_version is None
        or projection.approval_authorization_version == current_authorization_version
    )
    return CurrentActionResponse(
        action_id=projection.action_id.value,
        status=projection.status.value,
        view_id=projection.view_id.value,
        view_hash=projection.view_hash.value,
        case_version=projection.case_version,
        authorization_version=projection.authorization_version,
        subject=projection.subject,
        claims=tuple(
            ClaimProjection(text=text, export_fact_ids=citations)
            for text, citations in projection.claims
        ),
        requested_action=projection.requested_action,
        caveats=tuple(
            CaveatProjection(text=text, export_fact_ids=citations)
            for text, citations in projection.caveats
        ),
        tone=projection.tone,
        proposal_hash=projection.proposal_hash.value,
        preview=RenderedPreviewProjection(
            template_version=projection.template_version,
            text_body=projection.text_body,
            html_body=projection.html_body,
            preview_hash=projection.preview_hash.value,
            matches_committed_hash=projection.preview_matches_committed_hash,
        ),
        execution=ExecutionProjection(
            execution_id=projection.execution_id.value,
            state=projection.execution_state.value,
            version=projection.execution_version,
            approval_id=None if projection.approval_id is None else projection.approval_id.value,
        ),
        approval_authorization_current=approval_authorization_current,
    )


__all__ = ["CaseSurfaceResponse", "CurrentActionResponse", "router"]
