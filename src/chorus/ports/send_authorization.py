"""The narrow authority a sender may reach, and the only shape of question it may ask.

The sender holds a **total Core deny** --- see
[ADR-024](../../../docs/adr/ADR-024-execution-partition-and-sender-boundary.md)
SS 3: not an absent grant, an explicit refusal of ``dynamodb:*`` against the Core table. So it
cannot read the case, the mandates, or the fence row, and every case-side question -- including
both halves of the fence -- belongs to the compiler
([ADR-025](../../../docs/adr/ADR-025-one-deliberate-ses-attempt.md) SS 4).

This module is that boundary written as a type. It exists here, in ``ports``, rather than beside
the compiler-side implementation, because two implementations have to satisfy it and neither may
be the definition of the other:

* an **in-process** one, used in ``test`` and ``development``, where there is no Lambda boundary
  to cross and the same objects can talk to the same storage;
* a **compiler-invocation** one, used in a deployed topology, which turns the request into an
  ``lambda:InvokeFunction`` against the compiler function ARN -- the only Core-adjacent grant the
  sender's role carries.

Before this port existed the deployed composition root built the in-process implementation over a
``CoreRepository``, which is code that works locally and returns ``AccessDenied`` in an account.
A boundary that only one composition can satisfy is not a boundary.

The request is a *request* rather than an execution identifier for the reason ADR-025 SS 4 gives:
the compiler holds no opinion about which execution is interesting, and every value in the
request is already bound by an immutable artifact, so the authority verifies rather than trusts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from chorus.domain.entities import Purpose
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CaseId,
    CommunityId,
    DestinationId,
    ExecutionId,
    Namespace,
    Sha256Digest,
    ViewId,
)
from chorus.ports.records import SendFence
from chorus.ports.scopes import ActionScope, CaseScope


@dataclass(frozen=True, slots=True, kw_only=True)
class SendAuthorizationRequest:
    """Everything the compiler needs to decide whether one exact send may proceed.

    A request rather than an execution identifier, because the sender holds no Core access and
    the compiler holds no opinion about which execution is interesting. Every value in it is
    already bound by an immutable artifact, so the compiler verifies rather than trusts.
    """

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    execution_id: ExecutionId
    approval_id: ApprovalId
    proposal_hash: Sha256Digest
    view_id: ViewId
    view_hash: Sha256Digest
    authorization_version: int
    policy_version: str
    compiler_version: str
    policy_build_hash: Sha256Digest
    destination_id: DestinationId
    destination_registry_version: int
    routing_token: UUID
    purpose: Purpose
    authorization_snapshot_hash: Sha256Digest
    requested_at: datetime

    def __post_init__(self) -> None:
        if self.authorization_version < 1 or self.destination_registry_version < 1:
            raise ValueError("versions must be positive")

    @property
    def scope(self) -> CaseScope:
        return CaseScope(
            namespace=self.namespace, community_id=self.community_id, case_id=self.case_id
        )

    @property
    def action_scope(self) -> ActionScope:
        return ActionScope(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
            action_id=self.action_id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SendAuthorizationDenied:
    """The authority refused. No fence is held, so there is nothing for the caller to release."""

    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SendAuthorizationGranted:
    """The fence this send now holds, and the instant it stops authorizing anything."""

    fence: SendFence
    replayed: bool


type SendAuthorizationOutcome = SendAuthorizationGranted | SendAuthorizationDenied


class SendAuthorizationPort(Protocol):
    """Acquire the fence with the send-time revalidation inside it, and give it back.

    Two methods and no third, because the sender has exactly two things to ask: *may this exact
    send proceed right now*, and *here is the window back*. Anything else would be a case-side
    question, and the sender is not allowed to have one.
    """

    async def authorize(self, request: SendAuthorizationRequest) -> SendAuthorizationOutcome:
        """Take the fence, revalidate the whole case side while holding it, and answer.

        The revalidation happens **inside** the fence rather than before it. A validation that
        ran first would leave an unowned window in which a contributor's revocation could commit
        unseen by both halves, and the send would proceed under authority that had been
        withdrawn.

        A refusal is a returned denial rather than a raised exception, because the caller's
        correct response is a definite ``FAILED / STALE_AUTHORIZATION`` transition with no SES
        call, and that has to be a recorded outcome rather than a control-flow accident.
        """

    async def release(self, scope: CaseScope, execution_id: ExecutionId) -> None:
        """Return the fence, conditioned on the holder's execution identity.

        Conditioned, so a late or crashed process cannot clear a fence another execution has
        since taken.
        """


__all__ = [
    "SendAuthorizationDenied",
    "SendAuthorizationGranted",
    "SendAuthorizationOutcome",
    "SendAuthorizationPort",
    "SendAuthorizationRequest",
]
