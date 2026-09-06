"""The proposal transport: ask for one external message draft against one exact view.

The body is ``{expected_case_version, view_id, view_hash}`` and nothing else. It names no
subject, no claim, no caveat, no tone, no recipient, and no deadline, because the Action Agent's
payload is the compiled view assembled by the application from storage -- a client that could
name what the model reads, or what it may say, would be doing the drafting.

The response is ``202``. Invoking a model is not something an HTTP request should hold a
connection open for, so the caller polls the generic operation route. What the operation
eventually reports is a status and result references; the proposal itself and its regenerated
preview are read through the authorized case surface.

A stale ``expected_case_version``, a view that is not current, a mismatched authorization epoch,
or a live ``DRAFT`` proposal is refused with nothing written and **no model call**. The freshness
checks happen inside the worker against strong reads, and again as the apply transaction's
conditions, because the case and the current view can each move between any two of those points.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from chorus.application.operations import (
    StartedOperation,
    StartReservation,
    propose_action_binding_hash,
    propose_action_request_hash,
)
from chorus.application.services.mandate_terms import key_hash
from chorus.domain.entities import ApplicationOperationKind, ApplicationOperationStatus
from chorus.domain.ids import CaseId, Sha256Digest, ViewId
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.idempotency import IdempotentCommand
from chorus.ports.operations import ProposeActionOperationJob
from chorus_api.dependencies import (
    ApiContainer,
    DemoActor,
    actor_id_hash,
    container_of,
    require_actor,
    require_presenter,
)

router = APIRouter(tags=["actions"])

IdempotencyKeyStr = Annotated[
    str, StringConstraints(min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$")
]
Sha256Str = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class TransportRequest(BaseModel):
    """A closed HTTP request body; a field nobody declared can never ride along."""

    model_config = ConfigDict(extra="forbid")


class ProposeActionRequest(TransportRequest):
    """Exactly the three values a caller genuinely chooses.

    ``view_id`` and ``view_hash`` together are not redundant with the current pointer: they are
    what the caller *believes* is current, so a request made against a view a compile has since
    replaced is refused as stale rather than silently proposed against the newer one.
    """

    expected_case_version: Annotated[int, Field(ge=1)]
    view_id: UUID
    view_hash: Sha256Str


class OperationReference(BaseModel):
    operation_id: UUID
    status: str
    poll_url: str


@router.post("/cases/{case_id}/actions", status_code=202, response_model=OperationReference)
async def propose_action(
    request: Request,
    response: Response,
    case_id: UUID,
    body: ProposeActionRequest,
    idempotency_key: Annotated[IdempotencyKeyStr, Header(alias="Idempotency-Key")],
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> OperationReference:
    """Create one ``PROPOSE_ACTION`` operation and hand it over. No model is called here."""

    require_presenter(actor)
    container: ApiContainer = container_of(request)
    actor_hash: Sha256Digest = actor_id_hash(actor)
    identity = CaseId(case_id)
    view_hash = Sha256Digest(body.view_hash)

    binding = propose_action_binding_hash(
        case_id=identity, view_id=body.view_id, view_hash=view_hash
    )
    # Two digests, two concepts. ``binding`` is the ADR-016 handover identity -- the work one
    # *invocation* is authorized to do -- and it covers {case, view, hash}. ``request_hash`` is
    # this HTTP *request's* identity and covers everything the caller chose, which includes
    # ``expected_case_version``. Deriving the second from the first made two different requests
    # -- one asserting case version 1, one asserting 999 -- identical under a single
    # ``Idempotency-Key``, so the second was answered with the first's operation instead of a
    # conflict.
    request_hash = propose_action_request_hash(
        case_id=identity,
        expected_case_version=body.expected_case_version,
        view_id=body.view_id,
        view_hash=view_hash,
    )
    reserved = await container.operations.reserve_start(
        namespace=container.namespace,
        command=IdempotentCommand.PROPOSE_ACTION,
        actor_id_hash=actor_hash,
        key_hash=_key_hash(idempotency_key),
        # A same-key different-request arrival is a 409 with zero mutations, no second
        # operation, and no second dispatch.
        request_hash=request_hash,
        correlation_id=request.state.correlation_id,
    )
    operation = await _start_proposal_operation(
        container=container,
        case_id=identity,
        actor_hash=actor_hash,
        reserved=reserved,
        binding=binding,
        expected_case_version=body.expected_case_version,
        view_id=ViewId(body.view_id),
        view_hash=view_hash,
        idempotency_key=idempotency_key,
        correlation_id=request.state.correlation_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return operation


async def _start_proposal_operation(
    *,
    container: ApiContainer,
    case_id: CaseId,
    actor_hash: Sha256Digest,
    reserved: StartReservation | StartedOperation,
    binding: Sha256Digest,
    expected_case_version: int,
    view_id: ViewId,
    view_hash: Sha256Digest,
    idempotency_key: str,
    correlation_id: UUID,
) -> OperationReference:
    """Complete this request's reservation into a durable operation, or answer from the record.

    The operation is created carrying its **agent handover identity**: the invocation it
    authorizes and the digest of the exact view that invocation may propose against. Both are
    written before dispatch and before the first model call, which is what lets the worker
    refuse a misrouted *first* delivery -- the one delivery with no other durable record to
    disagree with, and the one that could otherwise present a fresh invocation identity, derive
    a fresh action identity from it, and write a second candidate message for one case.

    A replay that finds the operation still ``PENDING`` dispatches it **again**, for the same
    reason ingestion and investigation do: dispatch is the one step after the durable record
    that can fail on its own, and an operation whose only delivery was lost would otherwise sit
    ``PENDING`` forever. The worker's conditional claim, not the dispatcher, is where duplicate
    execution is actually prevented.
    """

    if isinstance(reserved, StartReservation):
        started = await container.operations.complete_start(
            reserved,
            namespace=container.namespace,
            kind=ApplicationOperationKind.PROPOSE_ACTION,
            actor_id_hash=actor_hash,
            case_id=case_id,
            agent_binding_hash=binding,
            correlation_id=correlation_id,
        )
    else:
        started = reserved
    if started.operation.case_id != case_id:
        # The key is bound to another case's proposal. Answering with that operation would tell
        # this caller their case is being drafted when it is not.
        raise PersistenceConflictError("APPLICATION_OPERATION")
    if started.operation.status is ApplicationOperationStatus.PENDING:
        await container.dispatcher.dispatch_propose_action(
            ProposeActionOperationJob(
                operation_id=started.operation.operation_id,
                namespace=container.namespace,
                community_id=container.community_id,
                case_id=case_id,
                invocation_id=started.invocation_id,
                correlation_id=correlation_id,
                actor_id_hash=actor_hash,
                request_hash=started.operation.request_hash,
                expected_case_version=expected_case_version,
                view_id=view_id,
                view_hash=view_hash,
                idempotency_key=idempotency_key,
            )
        )
    return OperationReference(
        operation_id=started.operation.operation_id.value,
        status=started.operation.status.value,
        poll_url=f"/v1/operations/{started.operation.operation_id}",
    )


def _key_hash(idempotency_key: str) -> Sha256Digest:
    """Hash the caller's key, because caller text never enters a storage key."""

    return key_hash(f"propose-action-start\x1f{idempotency_key}")
