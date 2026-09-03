"""The ambient signal feed and the operation poll that leads a client to it.

Both routes are read-only projections. Neither returns agent output: an operation reports its
status and points at the case the result lives under, and the case surface -- not this file --
decides what a given persona may see there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from chorus.domain.entities import ApplicationOperationStatus
from chorus.domain.ids import CommunityId, OperationId
from chorus.ports.errors import NotFoundError
from chorus.ports.pagination import PageCursor, PageRequest
from chorus_api.dependencies import (
    ApiContainer,
    DemoActor,
    actor_id_hash,
    container_of,
    require_actor,
    require_presenter,
)

router = APIRouter(tags=["feed"])

FEED_WINDOW_START = datetime(2000, 1, 1, tzinfo=UTC)
FEED_WINDOW_END = datetime(2100, 1, 1, tzinfo=UTC)
"""The default feed window.

The frozen feed query is a time-ranged partition query, so a window is always supplied. These
bounds cover every instant the demo can produce, which keeps "show me the feed" a single
query rather than a scan of an unbounded key space.
"""


class ChorusSignalResponse(BaseModel):
    candidate_case_id: UUID
    label: str
    related_count: int
    status: str


class AttachmentThumbnailResponse(BaseModel):
    evidence_id: str
    media_type: str
    caption: str | None


class FeedItemResponse(BaseModel):
    message_id: UUID
    sent_at: datetime
    pseudonym: str | None
    text: str
    attachment_thumbnails: tuple[AttachmentThumbnailResponse, ...]
    chorus_signal: ChorusSignalResponse | None


class FeedResponse(BaseModel):
    items: tuple[FeedItemResponse, ...]
    next_cursor: str | None


class OperationResponse(BaseModel):
    operation_id: UUID
    kind: str
    status: str
    result_refs: tuple[UUID, ...]
    error_code: str | None
    created_at: datetime
    updated_at: datetime


@router.get("/feed", response_model=FeedResponse)
async def read_feed(
    request: Request,
    response: Response,
    actor: Annotated[DemoActor, Depends(require_actor)],
    community_id: Annotated[UUID, Query()],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> FeedResponse:
    """Return one page of ambient messages with any discovered-pattern signal attached."""

    require_presenter(actor)
    container: ApiContainer = container_of(request)
    page = await container.read_feed.execute(
        namespace=container.namespace,
        community_id=CommunityId(community_id),
        start=FEED_WINDOW_START,
        end=FEED_WINDOW_END,
        request=PageRequest(limit=limit, cursor=None if cursor is None else PageCursor(cursor)),
    )
    response.headers["Cache-Control"] = "no-store"
    return FeedResponse(
        items=tuple(
            FeedItemResponse(
                message_id=item.message_id.value,
                sent_at=item.sent_at,
                pseudonym=item.pseudonym,
                text=item.text,
                attachment_thumbnails=tuple(
                    AttachmentThumbnailResponse(
                        evidence_id=thumbnail.evidence_id,
                        media_type=thumbnail.media_type,
                        caption=thumbnail.caption,
                    )
                    for thumbnail in item.attachment_thumbnails
                ),
                chorus_signal=(
                    None
                    if item.chorus_signal is None
                    else ChorusSignalResponse(
                        candidate_case_id=item.chorus_signal.candidate_case_id.value,
                        label=item.chorus_signal.label,
                        related_count=item.chorus_signal.related_count,
                        status=item.chorus_signal.status.value,
                    )
                ),
            )
            for item in page.items
        ),
        next_cursor=None if page.next_cursor is None else page.next_cursor.value,
    )


@router.get("/operations/{operation_id}", response_model=OperationResponse)
async def read_operation(
    request: Request,
    response: Response,
    operation_id: UUID,
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> OperationResponse:
    """Return the status of one durable operation.

    Agent output is never returned here. ``result_refs`` names the case the validated result
    was applied to, and the caller reads it through the endpoint that authorizes that case.

    The presenter may inspect any operation, because operating the demo means watching work
    that other personas started. Everyone else may inspect only their own: an operation
    identifier is a random UUID, but guessing difficulty is not an authorization boundary, so
    the actor hash on the record is compared to the caller's.
    """

    container: ApiContainer = container_of(request)
    operation = await container.operations.load(
        namespace=container.namespace, operation_id=OperationId(operation_id)
    )
    if actor is not DemoActor.PRESENTER_ADMIN and operation.actor_id_hash != actor_id_hash(actor):
        # Answered as absence rather than as refusal: a caller who is not entitled to this
        # operation must not be able to tell "no such operation" from "not yours".
        raise NotFoundError("APPLICATION_OPERATION")
    response.headers["Cache-Control"] = "no-store"
    if operation.status in {
        ApplicationOperationStatus.PENDING,
        ApplicationOperationStatus.RUNNING,
    }:
        response.headers["Retry-After"] = "1"
    return OperationResponse(
        operation_id=operation.operation_id.value,
        kind=operation.kind.value,
        status=operation.status.value,
        result_refs=operation.result_refs,
        error_code=operation.error_code,
        created_at=operation.created_at,
        updated_at=operation.updated_at,
    )
