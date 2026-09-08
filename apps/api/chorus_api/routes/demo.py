"""``POST /v1/demo/reset`` and ``GET /v1/session``.

Reset is the Phase 10 local reset service, wired at composition time
(:mod:`chorus.composition.demo_reset`); this route is a thin transport shell around it -- it
reads no case state and decides nothing the service does not already decide.

Session is a read of ``container.contributor_by_actor`` for the *calling* persona only. It
resolves who the caller already is; it cannot enumerate other personas or name one, because the
container never hands the whole mapping to a route -- only ``.get(actor)``.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StringConstraints

from chorus.composition.demo_reset import DemoResetRefused
from chorus_api.dependencies import (
    RESIDENT_ACTORS,
    ApiContainer,
    DemoActor,
    container_of,
    require_actor,
    require_presenter,
    resolve_contributor,
)

router = APIRouter(tags=["demo"])

IdempotencyKeyStr = Annotated[
    str, StringConstraints(min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$")
]


class TransportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResetRequest(TransportRequest):
    namespace: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    confirm: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    seed_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class ResetContributorResponse(BaseModel):
    actor: str
    pseudonym: str
    contributor_id: UUID


class ResetEvidenceResponse(BaseModel):
    evidence_id: UUID
    media_type: str
    sha256: str


class ResetCountsResponse(BaseModel):
    deleted: int
    messages: int
    contributors: int
    evidence: int


class ResetResponse(BaseModel):
    reset_id: UUID
    namespace: str
    seed_version: str
    corpus_sha256: str
    logical_now: str
    community_id: UUID
    destination_id: str
    contributors: tuple[ResetContributorResponse, ...]
    evidence: tuple[ResetEvidenceResponse, ...]
    counts: ResetCountsResponse
    replayed: bool
    audit_event_id: UUID


@router.post("/demo/reset", status_code=200, response_model=ResetResponse)
async def reset_demo(
    request: Request,
    response: Response,
    body: ResetRequest,
    actor: Annotated[DemoActor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKeyStr | None, Header(alias="Idempotency-Key")] = None,
) -> ResetResponse:
    """Delete-and-seed the demo namespace, idempotently, against local adapters."""

    require_presenter(actor)
    container: ApiContainer = container_of(request)
    if container.reset_demo is None:
        raise HTTPException(status_code=503, detail="The reset surface is not wired.")
    try:
        result = await container.reset_demo.reset(
            namespace=body.namespace,
            confirm=body.confirm,
            seed_version=body.seed_version,
            idempotency_key=idempotency_key,
        )
    except DemoResetRefused as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    response.headers["Cache-Control"] = "no-store"
    return ResetResponse(
        reset_id=result.reset_id,
        namespace=result.namespace,
        seed_version=result.seed_version,
        corpus_sha256=result.corpus_sha256.value,
        logical_now=result.logical_now.isoformat(),
        community_id=result.community_id.value,
        destination_id=result.destination_id,
        contributors=tuple(
            ResetContributorResponse(
                actor=c.actor, pseudonym=c.pseudonym, contributor_id=c.contributor_id.value
            )
            for c in result.contributors
        ),
        evidence=tuple(
            ResetEvidenceResponse(
                evidence_id=e.evidence_id.value, media_type=e.media_type, sha256=e.sha256.value
            )
            for e in result.evidence
        ),
        counts=ResetCountsResponse(
            deleted=result.counts.deleted,
            messages=result.counts.messages,
            contributors=result.counts.contributors,
            evidence=result.counts.evidence,
        ),
        replayed=result.replayed,
        audit_event_id=result.audit_event_id,
    )


# -- GET /session -------------------------------------------------------------------------

_CAPABILITIES: dict[DemoActor, tuple[str, ...]] = {
    DemoActor.PRESENTER_ADMIN: (
        "READ_FEED",
        "INGEST_MESSAGES",
        "READ_CASE",
        "READ_INVESTIGATION",
        "START_INVESTIGATION",
        "COMPILE_VIEW",
        "PROPOSE_ACTION",
        "READ_AUDIT",
        "DELIVER_EXTERNAL_REPLY",
        "ADVANCE_DEMO_CLOCK",
        "RESET_DEMO",
    ),
    DemoActor.RESIDENT_A: ("DECIDE_MANDATE", "VERIFY_COMMITMENT"),
    DemoActor.RESIDENT_B: ("DECIDE_MANDATE", "VERIFY_COMMITMENT"),
    DemoActor.RESIDENT_C: ("DECIDE_MANDATE", "VERIFY_COMMITMENT"),
    DemoActor.RESIDENT_D: ("DECIDE_MANDATE", "VERIFY_COMMITMENT"),
    DemoActor.CASE_APPROVER: ("READ_CASE", "APPROVE_ACTION", "INVALIDATE_ACTION", "EXECUTE_ACTION"),
}
"""Display guidance only, never authorization -- every route re-decides independently."""


class SessionResponse(BaseModel):
    actor: str
    contributor_id: UUID | None
    community_id: UUID
    namespace: str
    capabilities: tuple[str, ...]


@router.get("/session", status_code=200, response_model=SessionResponse)
async def read_session(
    request: Request,
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> SessionResponse:
    """Resolve the calling persona. Cannot enumerate or name any other persona."""

    container: ApiContainer = container_of(request)
    contributor_id = resolve_contributor(container, actor) if actor in RESIDENT_ACTORS else None
    return SessionResponse(
        actor=actor.value,
        contributor_id=None if contributor_id is None else contributor_id.value,
        community_id=container.community_id.value,
        namespace=container.namespace.value,
        capabilities=_CAPABILITIES.get(actor, ()),
    )


# -- GET /demo/corpus ---------------------------------------------------------------------


class DemoCorpusAttachmentResponse(BaseModel):
    evidence_id: UUID
    media_type: str
    byte_length: int
    sha256: str


class DemoCorpusMessageResponse(BaseModel):
    """One message in exactly the shape `POST /ingest/messages` accepts for a single item.

    So the frontend never needs to reshape this response before replaying it — see
    `DemoCorpusResponse` (P2-7).
    """

    adapter: Literal["SYNTHETIC"]
    channel_message_id: str
    contributor_id: UUID | None
    sent_at: str
    text: str
    attachments: tuple[DemoCorpusAttachmentResponse, ...]


class DemoCorpusResponse(BaseModel):
    """The immutable public ingestion input for the demo corpus, server-owned (P2-7).

    Before this route, the only way a browser could replay the seeded corpus to trigger the
    Monitor was to mirror the checked-in fixture file as a frontend constant -- a second copy
    of `demo/fixtures/elevator-v1/{manifest,feed}.json` that could silently drift from the one
    the server actually seeds with. This route serves the same
    :class:`~chorus.infrastructure.fixtures.synthetic_feed.SyntheticAmbientAdapter` reset
    itself reads, so a fixture change on the server is picked up by the browser with no
    frontend change at all.

    It carries no secret: every field here is exactly what `GET /feed` already shows a
    presenter after reset, resolved into the shape `POST /ingest/messages` accepts. It is
    presenter-only and wired only where `reset_demo` is (local/demo compositions), because it
    exists to serve the demo's own replay step and nothing else.
    """

    seed_version: str
    corpus_sha256: str
    community_id: UUID
    messages: tuple[DemoCorpusMessageResponse, ...]


@router.get("/demo/corpus", status_code=200, response_model=DemoCorpusResponse)
async def read_demo_corpus(
    request: Request,
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> DemoCorpusResponse:
    """Serve the exact seeded corpus, resolved into the shape ingest already accepts."""

    require_presenter(actor)
    container: ApiContainer = container_of(request)
    if container.reset_demo is None:
        raise HTTPException(status_code=503, detail="The demo corpus surface is not wired.")
    adapter = container.reset_demo.adapter
    contributor_ids = adapter.contributor_ids_by_pseudonym
    return DemoCorpusResponse(
        seed_version=adapter.seed_version,
        corpus_sha256=adapter.corpus_sha256.value,
        community_id=container.community_id.value,
        messages=tuple(
            DemoCorpusMessageResponse(
                adapter="SYNTHETIC",
                channel_message_id=message.channel_message_id,
                contributor_id=(
                    None
                    if message.contributor_pseudonym is None
                    else contributor_ids[message.contributor_pseudonym].value
                ),
                sent_at=message.sent_at.isoformat(),
                text=message.text,
                attachments=tuple(
                    DemoCorpusAttachmentResponse(
                        evidence_id=attachment.evidence_id.value,
                        media_type=attachment.media_type,
                        byte_length=attachment.byte_length,
                        sha256=attachment.sha256.value,
                    )
                    for attachment in message.attachments
                ),
            )
            for message in adapter.messages()
        ),
    )


__all__ = ["DemoCorpusResponse", "ResetResponse", "SessionResponse", "router"]
