"""The inbound reply transport: a fixture selector, and nothing else a caller may write.

The body is ``{"fixture_id": "..."}`` **and nothing else**. It names a reviewed RFC 822 message
in the repository; the route reads no case, action, destination, sender, received time, subject,
or body from the caller, because a caller-supplied reply is not a reply
([ADR-026](../../../../docs/adr/ADR-026-inbound-reply-trust-and-correlation.md) § Context). The
six provenance fields the old body carried are gone from the API surface entirely -- there is no
schema here that could accept one.

The fixture is fed through the same ``InboundMailAttester`` a deployed delivery uses, over
the local authenticator, so correlation to exactly one ``SENT``
execution, the receipt verdicts, the sender and recipient digest comparisons, and every closed
refusal code all apply unchanged. The demo exercises the boundary instead of bypassing it.

The response is ``202``. Persisting the artifact is synchronous -- it is a bounded transaction --
but the extraction that follows invokes a model, and invoking a model is not something an HTTP
request should hold a connection open for. The caller polls the operation.

A refusal is a ``422`` carrying **one closed code**. Never the envelope, never the sender, never
the subject, never the body, and never any part of the raw MIME: the same rule the audit row and
the log line obey, applied to the response.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StringConstraints

from chorus.application.commands.extract_commitment_operation import ExtractCommitmentJob
from chorus.application.commands.ingest_external_reply import IngestExternalReplyCommand
from chorus.application.operations import (
    StartedOperation,
    StartReservation,
    extract_commitment_binding_hash,
)
from chorus.application.services.inbound_mail import (
    InboundMailUntrusted,
    InboundReplyRejected,
)
from chorus.application.services.mandate_terms import key_hash
from chorus.domain.entities import ApplicationOperationKind, ApplicationOperationStatus
from chorus.domain.ids import Sha256Digest
from chorus.ports.idempotency import IdempotentCommand
from chorus_api.dependencies import (
    ApiContainer,
    DemoActor,
    InboundReplySurface,
    actor_id_hash,
    container_of,
    require_actor,
    require_presenter,
)

router = APIRouter(tags=["replies"])

FixtureIdStr = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")
]
IdempotencyKeyStr = Annotated[
    str, StringConstraints(min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$")
]


class DeliverFixtureReplyRequest(BaseModel):
    """A closed body with exactly one field. There is nowhere to put a reply."""

    model_config = ConfigDict(extra="forbid")

    fixture_id: FixtureIdStr


class ExtractionOperationReference(BaseModel):
    operation_id: UUID
    status: str
    poll_url: str
    evidence_id: UUID
    replayed: bool


@router.post("/demo/external-replies", status_code=202, response_model=ExtractionOperationReference)
async def deliver_external_reply(
    request: Request,
    response: Response,
    body: DeliverFixtureReplyRequest,
    idempotency_key: Annotated[IdempotencyKeyStr, Header(alias="Idempotency-Key")],
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> ExtractionOperationReference:
    """Deliver one reviewed fixture through the trust boundary, then start one extraction."""

    require_presenter(actor)
    container: ApiContainer = container_of(request)
    surface = _require_surface(container)
    actor_hash: Sha256Digest = actor_id_hash(actor)

    try:
        delivery = await surface.demo_replies.deliver(
            body.fixture_id, idempotency_key=idempotency_key
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown reply fixture.") from error

    try:
        attested = await surface.attester.attest(delivery)
    except (InboundMailUntrusted, InboundReplyRejected) as error:
        # One closed code, and nothing about the delivery. The rejection is recorded in the
        # namespace audit partition by the same call, because a refusal nobody can see is a
        # boundary nobody can operate.
        await surface.record_rejection.execute(
            namespace=container.namespace,
            actor_id_hash=actor_hash,
            correlation_id=request.state.correlation_id,
            reason_code=error.safe_code,
        )
        raise HTTPException(status_code=422, detail=error.safe_code) from error

    ingested = await surface.ingest.execute(
        IngestExternalReplyCommand(
            attested=attested,
            actor_id_hash=actor_hash,
            correlation_id=request.state.correlation_id,
        )
    )

    binding = extract_commitment_binding_hash(
        case_id=ingested.case_id,
        evidence_id=ingested.evidence_id,
        evidence_sha256=attested.evidence.raw_sha256,
    )
    reserved = await container.operations.reserve_start(
        namespace=container.namespace,
        command=IdempotentCommand.EXTRACT_COMMITMENT,
        actor_id_hash=actor_hash,
        key_hash=key_hash(f"extract-commitment-start\x1f{idempotency_key}"),
        # The binding digest doubles as this command's request hash: the two cover the same
        # three values -- case, artifact, and the artifact's own content digest -- because that
        # is exactly what the request *is*.
        request_hash=binding,
        correlation_id=request.state.correlation_id,
    )
    started = reserved
    if isinstance(reserved, StartReservation):
        started = await container.operations.complete_start(
            reserved,
            namespace=container.namespace,
            kind=ApplicationOperationKind.EXTRACT_COMMITMENT,
            actor_id_hash=actor_hash,
            case_id=ingested.case_id,
            agent_binding_hash=binding,
            correlation_id=request.state.correlation_id,
        )
    assert isinstance(started, StartedOperation)
    if started.operation.status is ApplicationOperationStatus.PENDING:
        # A replay that finds the operation still ``PENDING`` dispatches again, for the reason
        # ingestion and the investigation route already do: dispatch is the one step after the
        # durable record that can fail on its own. The worker's conditional claim, not the
        # dispatcher, is where duplicate execution is prevented.
        await container.dispatcher.dispatch_extract_commitment(
            ExtractCommitmentJob(
                operation_id=started.operation.operation_id,
                namespace=container.namespace,
                community_id=ingested.community_id,
                case_id=ingested.case_id,
                action_id=ingested.action_id,
                evidence_id=ingested.evidence_id,
                invocation_id=started.invocation_id,
                correlation_id=request.state.correlation_id,
                actor_id_hash=actor_hash,
                # The operation's own request hash, so the worker can bind before it claims,
                # and the artifact digest that is the binding's third member.
                request_hash=binding,
                evidence_sha256=attested.evidence.raw_sha256,
            )
        )
    response.headers["Cache-Control"] = "no-store"
    return ExtractionOperationReference(
        operation_id=started.operation.operation_id.value,
        status=started.operation.status.value,
        poll_url=f"/v1/operations/{started.operation.operation_id}",
        evidence_id=ingested.evidence_id.value,
        replayed=ingested.replayed,
    )


def _require_surface(container: ApiContainer) -> InboundReplySurface:
    """Refuse the route outright when no inbound boundary is wired.

    A deployment with no attester has nothing that can turn a delivery into evidence, and
    answering ``503`` says so. Returning ``202`` and quietly writing nothing would be the worst
    of the three available answers.
    """

    surface = container.inbound_replies
    if surface is None:
        raise HTTPException(status_code=503, detail="The inbound reply boundary is not wired.")
    return surface
