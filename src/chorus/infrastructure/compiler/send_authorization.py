"""Reach ``AcquireSendAuthorizationFence`` from a principal that cannot read Core.

Why this adapter exists
-----------------------
The deployed sender's role carries an explicit ``dynamodb:*`` **deny** against the Core table
([ADR-024](../../../../docs/adr/ADR-024-execution-partition-and-sender-boundary.md) SS 3). The
in-process :class:`chorus.application.services.send_authorization.SendAuthorization` is built
over a ``CoreRepository``, so in a deployed topology it cannot answer a single question it is
asked -- every call would return ``AccessDenied``.

The composition root nevertheless built exactly that, for both compositions, which made the
sender's own boundary unenforceable: the synthesized component could not perform the
authorization the send order requires, and the only reason no test caught it is that no test
runs without Core.

This adapter is the deployed half. It carries the sole Core-adjacent capability the sender's
policy grants -- ``lambda:InvokeFunction`` on the compiler function ARN, and nothing else -- and
it turns the frozen request into that invocation.

What it does not do
-------------------
It decides nothing. Every check named in ADR-025 SS 4, the fence's expiry arithmetic, the
release condition, and the entire denial vocabulary live on the compiler side; this module
serializes a request, invokes, and parses one of two answers. A branch here on a case state, a
mandate, or a reason code would be a second implementation of the authority.

It also never falls back. An invocation that fails, an answer it cannot parse, or an outcome
naming neither shape raises rather than returning a grant, because "the authority could not be
reached" and "the authority said yes" must never be the same value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CaseId,
    CommunityId,
    ExecutionId,
    Namespace,
    Sha256Digest,
    ViewId,
)
from chorus.ports.errors import ExternalDependencyError, PersistenceErrorCode
from chorus.ports.records import SendFence
from chorus.ports.scopes import CaseScope
from chorus.ports.send_authorization import (
    SendAuthorizationDenied,
    SendAuthorizationGranted,
    SendAuthorizationOutcome,
    SendAuthorizationRequest,
)

ACQUIRE_OPERATION = "AcquireSendAuthorizationFence"
RELEASE_OPERATION = "ReleaseSendAuthorizationFence"

FENCE_PAYLOAD_SCHEMA = "send-authorization-request/v1"
RELEASE_PAYLOAD_SCHEMA = "send-authorization-release/v1"


class CompilerInvokerPort(Protocol):
    """Invoke one named compiler operation and return its decoded answer.

    Deliberately a *string operation name and a mapping*, not a typed method per operation. The
    transport is one ``lambda:InvokeFunction`` against one ARN; giving it a method per operation
    would put the operation list in two places, and the compiler's own dispatch is the place it
    belongs.
    """

    async def invoke(self, *, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the operation's response body, or raise. It never returns a default."""


def _unusable(operation: str) -> ExternalDependencyError:
    """The authority could not be understood, which is never the same as it saying yes.

    ``retryable=False``: the caller is mid-send with a claimed execution, and a generic retry
    would re-enter the send order rather than record the definite pre-SES failure the send
    command already knows how to write.
    """

    return ExternalDependencyError(
        operation, code=PersistenceErrorCode.DEPENDENCY_REJECTED, retryable=False
    )


def encode_authorization_request(request: SendAuthorizationRequest) -> dict[str, Any]:
    """The frozen wire shape of ``SendAuthorizationRequest``, field for field.

    Every value is an identifier, a version, a digest, or an instant. No body, no address, no
    claim, and no caveat crosses this boundary, because none of them is an input to the
    question being asked.
    """

    return {
        "schema": FENCE_PAYLOAD_SCHEMA,
        "namespace": request.namespace.value,
        "community_id": str(request.community_id),
        "case_id": str(request.case_id),
        "action_id": str(request.action_id),
        "execution_id": str(request.execution_id),
        "approval_id": str(request.approval_id),
        "proposal_hash": request.proposal_hash.value,
        "view_id": str(request.view_id),
        "view_hash": request.view_hash.value,
        "authorization_version": request.authorization_version,
        "policy_version": request.policy_version,
        "compiler_version": request.compiler_version,
        "policy_build_hash": request.policy_build_hash.value,
        "destination_id": str(request.destination_id),
        "destination_registry_version": request.destination_registry_version,
        "routing_token": str(request.routing_token),
        "purpose": request.purpose.value,
        "authorization_snapshot_hash": request.authorization_snapshot_hash.value,
        "requested_at": request.requested_at.isoformat(),
    }


def decode_authorization_outcome(
    request: SendAuthorizationRequest, body: dict[str, Any]
) -> SendAuthorizationOutcome:
    """Parse the compiler's answer into one of exactly two shapes, or refuse.

    An answer that is neither a grant nor a denial is an :class:`ExternalDependencyError`, never
    a grant. The one thing this must never do is resolve an ambiguity in the permissive
    direction: a caller that cannot tell "denied" from "unreachable" would send.
    """

    outcome = body.get("outcome")
    if outcome == "DENIED":
        codes = body.get("reason_codes")
        if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
            raise _unusable(ACQUIRE_OPERATION)
        return SendAuthorizationDenied(reason_codes=tuple(codes))
    if outcome != "GRANTED":
        raise _unusable(ACQUIRE_OPERATION)
    fence = body.get("fence")
    if not isinstance(fence, dict):
        raise _unusable(ACQUIRE_OPERATION)
    try:
        granted = SendFence(
            namespace=Namespace(str(fence["namespace"])),
            community_id=CommunityId(UUID(str(fence["community_id"]))),
            case_id=CaseId(UUID(str(fence["case_id"]))),
            execution_id=ExecutionId(UUID(str(fence["execution_id"]))),
            action_id=ActionId(UUID(str(fence["action_id"]))),
            approval_id=ApprovalId(UUID(str(fence["approval_id"]))),
            view_id=ViewId(UUID(str(fence["view_id"]))),
            authorization_snapshot_hash=Sha256Digest(str(fence["authorization_snapshot_hash"])),
            acquired_at=datetime.fromisoformat(str(fence["acquired_at"])),
            expires_at=datetime.fromisoformat(str(fence["expires_at"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise _unusable(ACQUIRE_OPERATION) from error
    if granted.execution_id != request.execution_id or granted.case_id != request.case_id:
        # A fence about a different execution is not this send's authority, however
        # well-formed. Refused rather than trusted, because the caller is about to make an
        # external call on the strength of it.
        raise _unusable(ACQUIRE_OPERATION)
    return SendAuthorizationGranted(fence=granted, replayed=bool(body.get("replayed", False)))


@dataclass(slots=True)
class CompilerSendAuthorization:
    """The deployed sender's whole relationship with Core, behind one invocation.

    It satisfies :class:`chorus.ports.send_authorization.SendAuthorizationPort` and holds no
    repository, no table name, and no storage driver -- which is what makes "the sender cannot
    read Core" a property of the object graph rather than of a policy nobody executes locally.
    """

    invoker: CompilerInvokerPort

    async def authorize(self, request: SendAuthorizationRequest) -> SendAuthorizationOutcome:
        body = await self.invoker.invoke(
            operation=ACQUIRE_OPERATION, payload=encode_authorization_request(request)
        )
        return decode_authorization_outcome(request, body)

    async def release(self, scope: CaseScope, execution_id: ExecutionId) -> None:
        await self.invoker.invoke(
            operation=RELEASE_OPERATION,
            payload={
                "schema": RELEASE_PAYLOAD_SCHEMA,
                "namespace": scope.namespace.value,
                "community_id": str(scope.community_id),
                "case_id": str(scope.case_id),
                "execution_id": str(execution_id),
            },
        )


__all__ = [
    "ACQUIRE_OPERATION",
    "FENCE_PAYLOAD_SCHEMA",
    "RELEASE_OPERATION",
    "RELEASE_PAYLOAD_SCHEMA",
    "CompilerInvokerPort",
    "CompilerSendAuthorization",
    "decode_authorization_outcome",
    "encode_authorization_request",
]
