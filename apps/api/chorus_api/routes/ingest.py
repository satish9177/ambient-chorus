"""Ambient ingestion and the Monitor operation it starts.

The request carries messages. It does not carry a report identifier, a fact identifier, a case
identifier, or any statement about which messages belong together -- discovery happens behind
the Monitor contract, and a client that could name a case would be doing the discovering.

The response is ``202``. Persisting the messages is synchronous because it is fast and because
the caller needs the identifiers back; invoking a model is not, so it becomes an operation the
caller polls. Every message result says whether it was newly accepted or replayed, which is
how a redelivered batch stays legible instead of looking like nothing happened.

Command idempotency comes first, and that ordering is load-bearing
------------------------------------------------------------------
The route's ``Idempotency-Key`` owns the *whole request*, not the rows some inner command
happened to write. So it is claimed against the normalized request hash **before the first
mutation**, and only then are messages ingested.

The earlier order was ingest-then-claim, and it produced an answer that contradicted itself:
posting key ``K`` with messages A-C and then key ``K`` with messages D-F returned ``409``
after D-F were already durably stored. The conflict said the request was never accepted while
the feed said otherwise. Now a key that belongs to a different request is refused with nothing
written -- no message, no evidence root, no channel lock, no feed signal, no operation, and no
dispatch.

The claim is a reservation rather than a completion, so an interrupted attempt stays finishable.
An identical retry -- same key, same normalized request -- resumes: per-message ingestion is
replay-safe, and the operation and the completed record commit in one transaction, so at most
one operation is ever created under a key. A crash anywhere in between costs a repeat of
replay-safe work and nothing else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from chorus.application.commands.ingest_messages import (
    IngestAttachment,
    IngestMessage,
    IngestMessagesCommand,
    IngestMessagesResult,
    monitor_operation_identity,
)
from chorus.application.operations import (
    StartedOperation,
    StartReservation,
    monitor_locator_hash,
)
from chorus.domain.entities import ApplicationOperationKind, ApplicationOperationStatus
from chorus.domain.ids import CommunityId, ContributorId, EvidenceItemId, Sha256Digest
from chorus.ports.idempotency import IdempotentCommand
from chorus.ports.operations import MonitorOperationJob
from chorus.ports.records import MessageFeedEntry
from chorus_api.dependencies import (
    ApiContainer,
    DemoActor,
    actor_id_hash,
    container_of,
    require_actor,
    require_presenter,
)

router = APIRouter(prefix="/ingest", tags=["ingest"])

IdempotencyKeyStr = Annotated[
    str, StringConstraints(min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$")
]


class TransportRequest(BaseModel):
    """A closed HTTP request body.

    ``extra="forbid"`` is the security-relevant half and is kept: a field nobody declared can
    never ride along. Pydantic *strict* mode is deliberately not enabled here, and only here.
    A JSON body arrives as parsed primitives, so strict mode would reject every identifier,
    timestamp, and array in a well-formed request; the agent contracts, which are validated
    straight from bytes and where the model is the threat, keep it.

    Nothing is loosened in exchange. Every field below is bounded, patterned, or enumerated,
    and each one is converted into a typed domain value before it reaches a use case.
    """

    model_config = ConfigDict(extra="forbid")


class IngestAttachmentRequest(TransportRequest):
    """Attachment provenance declared by the adapter; bytes are never posted here."""

    evidence_id: UUID
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    byte_length: Annotated[int, Field(ge=1, le=10_485_760, strict=True)]
    """Strict on purpose, and the only strict field in the transport models.

    JSON ``true`` is an ``int`` in Python and ``"1"`` coerces to one, so without ``strict`` an
    attachment could declare a length of ``true`` and be recorded as one byte. A declared size
    is provenance that later validation compares real bytes against, so it has to mean exactly
    what was written.
    """
    sha256: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class IngestMessageRequest(TransportRequest):
    adapter: Literal["SYNTHETIC"] = "SYNTHETIC"
    channel_message_id: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    contributor_id: UUID | None = None
    sent_at: datetime
    text: Annotated[str, StringConstraints(min_length=1, max_length=10_000)]
    attachments: Annotated[tuple[IngestAttachmentRequest, ...], Field(max_length=8)] = ()


class IngestMessagesRequest(TransportRequest):
    community_id: UUID
    messages: Annotated[tuple[IngestMessageRequest, ...], Field(min_length=1, max_length=25)]


class IngestedMessageResponse(BaseModel):
    channel_message_id: str
    message_id: UUID
    replay: bool


class OperationReference(BaseModel):
    operation_id: UUID
    status: str
    poll_url: str


class IngestMessagesResponse(BaseModel):
    messages: tuple[IngestedMessageResponse, ...]
    accepted_count: int
    replayed_count: int
    operation: OperationReference


@router.post("/messages", status_code=202, response_model=IngestMessagesResponse)
async def ingest_messages(
    request: Request,
    response: Response,
    body: IngestMessagesRequest,
    idempotency_key: Annotated[IdempotencyKeyStr, Header(alias="Idempotency-Key")],
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> IngestMessagesResponse:
    """Persist a batch of ambient messages and start one Monitor operation over it."""

    require_presenter(actor)
    container: ApiContainer = container_of(request)
    community_id = CommunityId(body.community_id)
    actor_hash: Sha256Digest = actor_id_hash(actor)

    command = IngestMessagesCommand(
        namespace=container.namespace,
        community_id=community_id,
        actor_id_hash=actor_hash,
        idempotency_key=idempotency_key,
        messages=tuple(_to_command_message(message) for message in body.messages),
        correlation_id=request.state.correlation_id,
    )
    key_hash, request_hash = monitor_operation_identity(command)
    # Before anything is written. A key that belongs to a different request is refused here,
    # and the messages of the request that lost are never stored.
    reserved = await container.operations.reserve_start(
        namespace=container.namespace,
        command=IdempotentCommand.START_MONITOR_OPERATION,
        actor_id_hash=actor_hash,
        key_hash=key_hash,
        request_hash=request_hash,
        correlation_id=request.state.correlation_id,
    )
    result = await container.ingest_messages.execute(command)
    operation = await _start_monitor_operation(
        container=container,
        community_id=community_id,
        actor_hash=actor_hash,
        reserved=reserved,
        body=body,
        result=result,
        correlation_id=request.state.correlation_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return IngestMessagesResponse(
        messages=tuple(
            IngestedMessageResponse(
                channel_message_id=item.channel_message_id,
                message_id=item.message_id.value,
                replay=item.replay,
            )
            for item in result.messages
        ),
        accepted_count=result.accepted_count,
        replayed_count=result.replayed_count,
        operation=operation,
    )


async def _start_monitor_operation(
    *,
    container: ApiContainer,
    community_id: CommunityId,
    actor_hash: Sha256Digest,
    reserved: StartReservation | StartedOperation,
    body: IngestMessagesRequest,
    result: IngestMessagesResult,
    correlation_id: UUID,
) -> OperationReference:
    """Complete this request's reservation into a durable operation, or answer from the record.

    The operation is created carrying its **Monitor handover identity**: the invocation it
    authorizes and the digest of the exact locator set that invocation may treat as its new
    messages. Both are written before dispatch and before the first model call, which is what
    lets the worker refuse a misrouted *first* delivery -- the one delivery that previously had
    no durable record to disagree with, and could therefore substitute another invocation
    identity or a different slice of the batch while keeping a valid request hash.

    The record is written before anything is dispatched. A dispatcher that delivered before the
    record existed would produce a worker with nothing to claim, and the whole point of the
    record is that a repeated delivery has exactly one thing to lose a race against.

    A replay that finds the operation still ``PENDING`` dispatches it **again**. Dispatch is the
    one step after the durable record that can fail on its own, and an operation whose only
    delivery was lost would otherwise sit ``PENDING`` forever while every retry politely
    declined to start it -- the record exists, so nothing looked wrong. Duplicate dispatch is
    the deliberate trade: the job identity is unchanged, and the worker's conditional claim, not
    the dispatcher, is where duplicate execution is actually prevented. A replay that finds the
    operation already running or finished starts nothing.
    """

    sent_at_by_channel = {message.channel_message_id: message.sent_at for message in body.messages}
    locators = tuple(
        MessageFeedEntry(
            message_id=item.message_id, sent_at=sent_at_by_channel[item.channel_message_id]
        )
        for item in result.messages
    )
    if isinstance(reserved, StartReservation):
        started = await container.operations.complete_start(
            reserved,
            namespace=container.namespace,
            kind=ApplicationOperationKind.MONITOR,
            actor_id_hash=actor_hash,
            monitor_locator_hash=monitor_locator_hash(locators),
            correlation_id=correlation_id,
        )
    else:
        started = reserved
    if started.operation.status is ApplicationOperationStatus.PENDING:
        await container.dispatcher.dispatch_monitor(
            MonitorOperationJob(
                operation_id=started.operation.operation_id,
                namespace=container.namespace,
                community_id=community_id,
                invocation_id=started.invocation_id,
                correlation_id=correlation_id,
                actor_id_hash=actor_hash,
                request_hash=started.operation.request_hash,
                message_locators=locators,
            )
        )
    return OperationReference(
        operation_id=started.operation.operation_id.value,
        status=started.operation.status.value,
        poll_url=f"/v1/operations/{started.operation.operation_id}",
    )


def _to_command_message(message: IngestMessageRequest) -> IngestMessage:
    return IngestMessage(
        channel_message_id=message.channel_message_id,
        contributor_id=(
            None if message.contributor_id is None else ContributorId(message.contributor_id)
        ),
        sent_at=message.sent_at,
        text=message.text,
        attachments=tuple(
            IngestAttachment(
                evidence_id=EvidenceItemId(attachment.evidence_id),
                media_type=attachment.media_type,
                byte_length=attachment.byte_length,
                sha256=Sha256Digest(attachment.sha256),
            )
            for attachment in message.attachments
        ),
    )
