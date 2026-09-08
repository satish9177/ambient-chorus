"""``GET /v1/cases/{case_id}/audit``: the safe, paginated audit page.

Presenter-only. ``actor_id_hash`` and ``idempotency_key_hash`` are never returned -- they
identify *who* and *which request* rather than *what happened*, and no field on
``AuditEventResponse`` can carry either. ``safe_details`` is a bounded count and a closed rule
identifier; there is no field here that can hold free text or a raw payload.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from chorus.domain.ids import CaseId
from chorus.ports.pagination import PageCursor, PageRequest
from chorus.ports.scopes import CaseScope
from chorus_api.dependencies import (
    ApiContainer,
    DemoActor,
    container_of,
    require_actor,
    require_presenter,
)

router = APIRouter(tags=["audit"])


class AuditEntityRefResponse(BaseModel):
    entity_type: str
    entity_id: UUID
    version: int | None


class AuditDetailsResponse(BaseModel):
    count: int | None
    rule_id: str | None


class AuditEventResponse(BaseModel):
    audit_event_id: UUID
    event_type: str
    occurred_at: str
    actor_type: str
    decision: str
    reason_codes: tuple[str, ...]
    entity_refs: tuple[AuditEntityRefResponse, ...]
    safe_details: AuditDetailsResponse
    correlation_id: UUID
    causation_id: UUID | None
    input_hash: str | None
    output_hash: str | None


class AuditPageResponse(BaseModel):
    items: tuple[AuditEventResponse, ...]
    next_cursor: str | None


@router.get("/cases/{case_id}/audit", response_model=AuditPageResponse)
async def read_case_audit(
    request: Request,
    case_id: UUID,
    actor: Annotated[DemoActor, Depends(require_actor)],
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query()] = None,
) -> AuditPageResponse:
    """Page one case's safe audit trail, presenter-only, in occurrence order."""

    require_presenter(actor)
    container: ApiContainer = container_of(request)
    if container.audit_page is None:
        raise HTTPException(status_code=503, detail="The audit surface is not wired.")
    scope = CaseScope(
        namespace=container.namespace,
        community_id=container.community_id,
        case_id=CaseId(case_id),
    )
    page = await container.audit_page.execute(
        scope, PageRequest(limit=limit, cursor=None if cursor is None else PageCursor(cursor))
    )
    return AuditPageResponse(
        items=tuple(
            AuditEventResponse(
                audit_event_id=item.audit_event_id,
                event_type=item.event_type,
                occurred_at=item.occurred_at.isoformat(),
                actor_type=item.actor_type.value,
                decision=item.decision.value,
                reason_codes=item.reason_codes,
                entity_refs=tuple(
                    AuditEntityRefResponse(
                        entity_type=ref.entity_type, entity_id=ref.entity_id, version=ref.version
                    )
                    for ref in item.entity_refs
                ),
                safe_details=AuditDetailsResponse(
                    count=item.safe_details.count, rule_id=item.safe_details.rule_id
                ),
                correlation_id=item.correlation_id,
                causation_id=item.causation_id,
                input_hash=item.input_hash,
                output_hash=item.output_hash,
            )
            for item in page.items
        ),
        next_cursor=None if page.next_cursor is None else str(page.next_cursor),
    )


__all__ = ["AuditPageResponse", "router"]
