"""The investigation transport: start one skeptical review of one case.

The body is ``{expected_case_version, reason}`` and nothing else. It names no fact, no report,
no evidence, and no finding, because the Investigator's payload is assembled by the application
from the case at that version -- a client that could name what the model reads would be doing
the investigating.

The response is ``202``. Invoking a model is not something an HTTP request should hold a
connection open for, so the caller polls an operation. What the operation eventually reports is
a status and a result reference; the assessment itself is private and is read through the
authorized case surface.

A stale ``expected_case_version`` is ``409`` with nothing written. The check happens here so a
caller learns immediately, and it happens *again* inside the worker against a strong read, and a
third time as the apply transaction's version condition -- because the case can move between any
two of those points and an assessment bound to a version that no longer exists must never be
applied.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from chorus.application.commands.run_investigation import InvestigationReason
from chorus.application.operations import (
    StartedOperation,
    StartReservation,
    investigate_binding_hash,
)
from chorus.application.services.mandate_terms import key_hash
from chorus.domain.entities import ApplicationOperationKind, ApplicationOperationStatus
from chorus.domain.ids import CaseId, Sha256Digest
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.idempotency import IdempotentCommand
from chorus.ports.operations import InvestigationOperationJob
from chorus_api.dependencies import (
    ApiContainer,
    DemoActor,
    actor_id_hash,
    container_of,
    require_actor,
    require_presenter,
)

router = APIRouter(tags=["investigations"])

IdempotencyKeyStr = Annotated[
    str, StringConstraints(min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$")
]


class TransportRequest(BaseModel):
    """A closed HTTP request body; a field nobody declared can never ride along."""

    model_config = ConfigDict(extra="forbid")


class StartInvestigationRequest(TransportRequest):
    expected_case_version: Annotated[int, Field(ge=1)]
    reason: Literal["INITIAL", "NEW_EVIDENCE", "REOPEN"]


class OperationReference(BaseModel):
    operation_id: UUID
    status: str
    poll_url: str


@router.post("/cases/{case_id}/investigations", status_code=202, response_model=OperationReference)
async def start_investigation(
    request: Request,
    response: Response,
    case_id: UUID,
    body: StartInvestigationRequest,
    idempotency_key: Annotated[IdempotencyKeyStr, Header(alias="Idempotency-Key")],
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> OperationReference:
    """Create one ``INVESTIGATE`` operation and hand it over. No model is called here."""

    require_presenter(actor)
    container: ApiContainer = container_of(request)
    actor_hash: Sha256Digest = actor_id_hash(actor)
    identity = CaseId(case_id)
    reason = InvestigationReason(body.reason)

    binding = investigate_binding_hash(
        case_id=identity,
        expected_case_version=body.expected_case_version,
        reason=reason.value,
    )
    reserved = await container.operations.reserve_start(
        namespace=container.namespace,
        command=IdempotentCommand.APPLY_INVESTIGATION,
        actor_id_hash=actor_hash,
        key_hash=_key_hash(idempotency_key),
        # The binding digest doubles as this command's request hash. The two cover the same
        # three values -- case, expected version, and reason -- because that is exactly what
        # the request *is*, and deriving one from the other keeps them from ever describing
        # different requests under one key.
        request_hash=binding,
        correlation_id=request.state.correlation_id,
    )
    operation = await _start_investigation_operation(
        container=container,
        case_id=identity,
        actor_hash=actor_hash,
        reserved=reserved,
        binding=binding,
        expected_case_version=body.expected_case_version,
        reason=reason,
        idempotency_key=idempotency_key,
        correlation_id=request.state.correlation_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return operation


async def _start_investigation_operation(
    *,
    container: ApiContainer,
    case_id: CaseId,
    actor_hash: Sha256Digest,
    reserved: StartReservation | StartedOperation,
    binding: Sha256Digest,
    expected_case_version: int,
    reason: InvestigationReason,
    idempotency_key: str,
    correlation_id: UUID,
) -> OperationReference:
    """Complete this request's reservation into a durable operation, or answer from the record.

    The operation is created carrying its **agent handover identity**: the invocation it
    authorizes and the digest of the exact work that invocation may do. Both are written before
    dispatch and before the first model call, which is what lets the worker refuse a misrouted
    *first* delivery -- the one delivery that would otherwise have no durable record to disagree
    with, and could therefore present a fresh invocation identity, find no invocation record,
    and spend a second model pass over the same private case.

    A replay that finds the operation still ``PENDING`` dispatches it **again**, for the same
    reason ingestion does: dispatch is the one step after the durable record that can fail on
    its own, and an operation whose only delivery was lost would otherwise sit ``PENDING``
    forever. The worker's conditional claim, not the dispatcher, is where duplicate execution is
    actually prevented.
    """

    if isinstance(reserved, StartReservation):
        started = await container.operations.complete_start(
            reserved,
            namespace=container.namespace,
            kind=ApplicationOperationKind.INVESTIGATE,
            actor_id_hash=actor_hash,
            case_id=case_id,
            agent_binding_hash=binding,
            correlation_id=correlation_id,
        )
    else:
        started = reserved
    if started.operation.case_id != case_id:
        # The key is bound to another case's investigation. Answering with that operation would
        # tell this caller their case is being investigated when it is not.
        raise PersistenceConflictError("APPLICATION_OPERATION")
    if started.operation.status is ApplicationOperationStatus.PENDING:
        await container.dispatcher.dispatch_investigation(
            InvestigationOperationJob(
                operation_id=started.operation.operation_id,
                namespace=container.namespace,
                community_id=container.community_id,
                case_id=case_id,
                invocation_id=started.invocation_id,
                correlation_id=correlation_id,
                actor_id_hash=actor_hash,
                request_hash=started.operation.request_hash,
                expected_case_version=expected_case_version,
                reason=reason.value,
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

    return key_hash(f"investigate-start\x1f{idempotency_key}")
