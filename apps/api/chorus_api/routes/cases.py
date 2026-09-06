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
    """The safe ``DRAFT`` projection. Its absent fields are absent on purpose (ADR-022 § 1)."""

    execution_id: UUID
    state: str


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


class CaseSurfaceResponse(BaseModel):
    """The frozen case surface, with the sections this phase owns.

    ``current_action`` is ``null`` when the case has never held a proposal. That is a state, not
    an error: a case in ``READY_FOR_ACTION`` legitimately has no action yet.
    """

    case_id: UUID
    current_action: CurrentActionResponse | None


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
    projection = await container.read_current_action.execute(scope)
    return CaseSurfaceResponse(
        case_id=case_id,
        current_action=None if projection is None else _project(projection),
    )


def _project(projection: CurrentActionProjection) -> CurrentActionResponse:
    """Map the query's projection onto the transport shape, adding nothing."""

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
        ),
    )


__all__ = ["CaseSurfaceResponse", "CurrentActionResponse", "router"]
