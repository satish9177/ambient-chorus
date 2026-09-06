"""The three Phase-8 transports: approve or reject, invalidate, and execute.

Three routes and no fourth. There is deliberately **no retry route** and **no read route**:
``FAILED`` is terminal for an action, ``SEND_UNKNOWN`` is a quarantine only reconciliation
resolves, and an execution is already part of ``current_action`` on the existing case surface --
a second address for one row is a second thing to keep consistent.

Every body is closed (``extra='forbid'``), and the closure is the point. The approval body
carries no text field of any kind, so there is nothing in which an edited subject or body could
be submitted; the execute body accepts no recipient, subject, body, claim, attachment, template,
or retry flag. Those absences are properties of the models rather than validation rules, which
is why they cannot be widened by accident.

What the transport does and does not decide
--------------------------------------------
It resolves the persona to an ``approver_id_hash`` and an assurance level, and that is the whole
of its contribution. Every freshness check, every hash comparison, every compare-and-swap, and
every transaction lives in the use case, against strong reads -- because the case, the pointer,
and the execution can each move between any two points in a request.

Approver identity, stated at its real strength
-----------------------------------------------
``approver_assurance`` is ``DEMO_SHARED_TOKEN`` and nothing else exists. That records that
somebody holding the demo access token asserted the approver persona; it is single-presenter
demo access control and **not** authentication of a person. No contributor is minted to
represent the approver, because a contributor is a counted thing -- corroboration, independence
grouping, mandate ownership are all defined over contributors -- and putting a non-participant
into that population would let it influence whether a case may act at all (ADR-023 SS 4).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from chorus.application.commands.approve_action import ApproveActionCommand
from chorus.application.commands.invalidate_action import InvalidateActionCommand
from chorus.application.operations import StartReservation
from chorus.application.services.action_authorization import (
    send_request_hash,
    send_start_key_hash,
)
from chorus.domain.entities import (
    ApplicationOperationKind,
    ApplicationOperationStatus,
    ApprovalDecision,
    ApproverAssurance,
)
from chorus.domain.ids import ActionId, ApprovalId, CaseId, ExecutionId, Sha256Digest
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.idempotency import IdempotentCommand
from chorus.ports.operations import SendActionOperationJob
from chorus_api.dependencies import (
    ApiContainer,
    DemoActor,
    actor_id_hash,
    container_of,
    require_actor,
    require_case_approver,
)

router = APIRouter(tags=["actions"])

IdempotencyKeyStr = Annotated[
    str, StringConstraints(min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$")
]
Sha256Str = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class TransportRequest(BaseModel):
    """A closed HTTP request body; a field nobody declared can never ride along."""

    model_config = ConfigDict(extra="forbid")


class ApprovalRequest(TransportRequest):
    """The frozen approval body, and every field in it is something the human saw.

    ``expected_action_status`` is deliberately absent: it named a status where the transaction
    conditions on a row version, and two ways to say "the thing I saw" is one too many.
    """

    decision: ApprovalDecision
    expected_execution_version: Annotated[int, Field(ge=1)]
    execution_id: UUID
    view_hash: Sha256Str
    proposal_hash: Sha256Str
    preview_hash: Sha256Str


class InvalidationRequest(TransportRequest):
    """Withdraw an approval, or clear a terminal failure. The same body serves both."""

    expected_execution_version: Annotated[int, Field(ge=1)]
    proposal_hash: Sha256Str


class ExecutionRequest(TransportRequest):
    """The frozen execute body. Three identifiers and a version; nothing steerable."""

    execution_id: UUID
    expected_execution_version: Annotated[int, Field(ge=1)]
    approval_id: UUID


class ApprovalResponse(BaseModel):
    approval_id: UUID
    decision: str
    approval_hash: str
    expires_at: str
    execution_id: UUID
    execution_state: str
    execution_version: int
    pointer_status: str
    case_state: str
    case_version: int
    authorization_version: int
    replayed: bool


class InvalidationResponse(BaseModel):
    action_id: UUID
    execution_id: UUID
    execution_state: str
    execution_version: int
    pointer_status: str
    case_state: str
    case_version: int
    authorization_version: int
    reason_code: str


class OperationReference(BaseModel):
    operation_id: UUID
    status: str
    poll_url: str


@router.post(
    "/cases/{case_id}/actions/{action_id}/approvals",
    status_code=200,
    response_model=ApprovalResponse,
)
async def approve_action(
    request: Request,
    response: Response,
    case_id: UUID,
    action_id: UUID,
    body: ApprovalRequest,
    idempotency_key: Annotated[IdempotencyKeyStr, Header(alias="Idempotency-Key")],
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> ApprovalResponse:
    """Record one immutable human decision about one immutable proposal."""

    require_case_approver(actor)
    container: ApiContainer = container_of(request)
    result = await container.approve_action.execute(
        ApproveActionCommand(
            namespace=container.namespace,
            community_id=container.community_id,
            case_id=CaseId(case_id),
            action_id=ActionId(action_id),
            decision=body.decision,
            expected_execution_version=body.expected_execution_version,
            execution_id=ExecutionId(body.execution_id),
            view_hash=Sha256Digest(body.view_hash),
            proposal_hash=Sha256Digest(body.proposal_hash),
            preview_hash=Sha256Digest(body.preview_hash),
            approver_id_hash=actor_id_hash(actor),
            # The only assurance level that exists, and it says exactly what it means.
            approver_assurance=ApproverAssurance.DEMO_SHARED_TOKEN,
            correlation_id=request.state.correlation_id,
            idempotency_key=idempotency_key,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return ApprovalResponse(
        approval_id=result.approval_id.value,
        decision=result.decision.value,
        approval_hash=result.approval_hash.value,
        expires_at=result.expires_at.isoformat().replace("+00:00", "Z"),
        execution_id=result.execution_id.value,
        execution_state=result.execution_state.value,
        execution_version=result.execution_version,
        pointer_status=result.pointer_status.value,
        case_state=result.case_state.value,
        case_version=result.case_version,
        authorization_version=result.authorization_version,
        replayed=result.replayed,
    )


@router.post(
    "/cases/{case_id}/actions/{action_id}/invalidation",
    status_code=200,
    response_model=InvalidationResponse,
)
async def invalidate_action(
    request: Request,
    response: Response,
    case_id: UUID,
    action_id: UUID,
    body: InvalidationRequest,
    idempotency_key: Annotated[IdempotencyKeyStr, Header(alias="Idempotency-Key")],
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> InvalidationResponse:
    """Withdraw an approval, or clear a terminal failure, freeing the case for a new proposal.

    Refused for ``SENDING``, ``SENT``, and ``SEND_UNKNOWN``, each for its own reason: a send in
    flight cannot be taken back, a sent message cannot be recalled, and an ambiguous outcome is
    a quarantine only reconciliation resolves.
    """

    require_case_approver(actor)
    container: ApiContainer = container_of(request)
    result = await container.invalidate_action.execute(
        InvalidateActionCommand(
            namespace=container.namespace,
            community_id=container.community_id,
            case_id=CaseId(case_id),
            action_id=ActionId(action_id),
            expected_execution_version=body.expected_execution_version,
            proposal_hash=Sha256Digest(body.proposal_hash),
            actor_id_hash=actor_id_hash(actor),
            correlation_id=request.state.correlation_id,
            idempotency_key=idempotency_key,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return InvalidationResponse(
        action_id=result.action_id.value,
        execution_id=result.execution_id.value,
        execution_state=result.execution_state.value,
        execution_version=result.execution_version,
        pointer_status=result.pointer_status.value,
        case_state=result.case_state.value,
        case_version=result.case_version,
        authorization_version=result.authorization_version,
        reason_code=result.reason_code,
    )


@router.post(
    "/cases/{case_id}/actions/{action_id}/executions",
    status_code=202,
    response_model=OperationReference,
)
async def start_execution(
    request: Request,
    response: Response,
    case_id: UUID,
    action_id: UUID,
    body: ExecutionRequest,
    idempotency_key: Annotated[IdempotencyKeyStr, Header(alias="Idempotency-Key")],
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> OperationReference:
    """Create one ``SEND_ACTION`` operation and hand it over. No SES call happens here.

    ``202`` and a poll, for the same reason the proposal route returns one: an external call is
    not something an HTTP request should hold a connection open for. The caller reads the
    outcome through the existing operation poll and the existing case surface.
    """

    require_case_approver(actor)
    container: ApiContainer = container_of(request)
    actor_hash = actor_id_hash(actor)
    identity = CaseId(case_id)
    action = ActionId(action_id)
    request_hash = send_request_hash(
        case_id=identity,
        action_id=action,
        execution_id=ExecutionId(body.execution_id),
        approval_id=ApprovalId(body.approval_id),
        expected_execution_version=body.expected_execution_version,
    )
    reserved = await container.operations.reserve_start(
        namespace=container.namespace,
        command=IdempotentCommand.SEND_ACTION,
        actor_id_hash=actor_hash,
        key_hash=send_start_key_hash(idempotency_key),
        # A same-key different-request arrival is a 409 with zero mutations, no second
        # operation, and no second dispatch.
        request_hash=request_hash,
        correlation_id=request.state.correlation_id,
    )
    if isinstance(reserved, StartReservation):
        started = await container.operations.complete_start(
            reserved,
            namespace=container.namespace,
            kind=ApplicationOperationKind.SEND_ACTION,
            actor_id_hash=actor_hash,
            case_id=identity,
            # No agent handover is supplied, and none may be: ``SEND_ACTION`` invokes no agent,
            # and an operation of this kind carrying one is refused at construction (ADR-016).
            correlation_id=request.state.correlation_id,
        )
    else:
        started = reserved
    if started.operation.case_id != identity:
        # The key is bound to another case's send. Answering with that operation would tell
        # this caller their message is going out when it is not.
        raise PersistenceConflictError("APPLICATION_OPERATION")
    if started.operation.status is ApplicationOperationStatus.PENDING:
        # A replay that finds the operation still PENDING dispatches it again, for the reason
        # every other route does: dispatch is the one step after the durable record that can
        # fail on its own. Duplicate execution is prevented by the execution's claim
        # compare-and-swap, never by the dispatcher.
        await container.dispatcher.dispatch_send_action(
            SendActionOperationJob(
                operation_id=started.operation.operation_id,
                namespace=container.namespace,
                community_id=container.community_id,
                case_id=identity,
                action_id=action,
                execution_id=ExecutionId(body.execution_id),
                approval_id=ApprovalId(body.approval_id),
                correlation_id=request.state.correlation_id,
                actor_id_hash=actor_hash,
                request_hash=started.operation.request_hash,
                expected_execution_version=body.expected_execution_version,
                idempotency_key=idempotency_key,
            )
        )
    response.headers["Cache-Control"] = "no-store"
    return OperationReference(
        operation_id=started.operation.operation_id.value,
        status=started.operation.status.value,
        poll_url=f"/v1/operations/{started.operation.operation_id}",
    )
