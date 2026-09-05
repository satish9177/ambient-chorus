"""The compiler boundary's second typed operation: acquire and release the send fence.

The compiler Lambda exposes two operations behind separate actions. ``CompileShareableView`` is
the first. This is the second, and Phase 6 builds it without building a caller: the sender is
Phase 8, and composing the boundary now is what stops Phase 8 from inventing a second fence
mechanism when it arrives.

**Nothing here decides whether a send may happen.** That revalidation -- view, proposal,
approval, snapshot, expiry -- is the Phase-8 acquire path and is deliberately absent. What Phase
6 owns is the primitive's *ownership semantics*, and they are already frozen in Phase 2:

* acquiring takes an absent or expired fence, and the same execution replays its own live fence
  without extending it, so a redelivery cannot quietly widen the authorization window;
* releasing is conditioned on the holder's execution identity, so a crashed or stale process
  cannot clear a fence that belongs to somebody else;
* expiry is compared in exact microseconds against the stored deadline, never against the TTL
  attribute, which exists only so DynamoDB can sweep an abandoned row.

Compile itself does not acquire this fence. It *checks* that none is live, as a condition
inside its own transaction -- the same participant a mandate decision and an investigation apply
already use. Either an authorized send holds the case or an authorization change commits; both
sides can never believe they won.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from chorus.application import observability
from chorus.application.errors import SendAuthorizationInProgressError
from chorus.domain.ids import ExecutionId
from chorus.ports.clock import Clock
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.records import SendFence
from chorus.ports.repositories import CoreRepositoryPort
from chorus.ports.scopes import CaseScope


@dataclass(frozen=True, slots=True, kw_only=True)
class FenceOutcome:
    """What the holder is told: the fence it now holds, and whether it already held it."""

    fence: SendFence
    replayed: bool


@dataclass(slots=True)
class SendAuthorizationFence:
    """The compiler boundary's fence operations, over the one frozen primitive."""

    core: CoreRepositoryPort
    clock: Clock

    async def acquire(self, scope: CaseScope, fence: SendFence) -> FenceOutcome:
        """Take an absent or expired fence, or replay this execution's own live one.

        A second execution meeting a live fence is refused *retryably*: the frozen ordering
        gives it at most sixty seconds, and telling it to come back is the only answer that
        keeps two holders from both believing they won.
        """

        held = await self.core.load_send_fence(scope)
        acquired = await self.core.acquire_send_fence(scope, fence)
        replayed = held is not None and held.execution_id == fence.execution_id
        observability.send_fence_acquired(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            execution_id=acquired.execution_id.value,
            replayed=replayed,
        )
        return FenceOutcome(fence=acquired, replayed=replayed)

    async def release(self, scope: CaseScope, execution_id: ExecutionId) -> None:
        """Delete only this execution's fence.

        A conditional failure here is not an error to swallow. It means the fence belongs to
        another execution or has already gone, and a release that reported success either way
        would make "the fence is clear" impossible to rely on.
        """

        try:
            await self.core.release_send_fence(scope, execution_id)
        except PersistenceConflictError:
            observability.send_fence_release_denied(
                namespace=scope.namespace,
                community_id=scope.community_id,
                case_id=scope.case_id,
                execution_id=execution_id.value,
            )
            raise
        observability.send_fence_released(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            execution_id=execution_id.value,
        )

    async def require_clear(self, scope: CaseScope, *, now: datetime | None = None) -> None:
        """Refuse, retryably, while a live fence holds the case.

        Used to answer a caller *before* a transaction rather than instead of the condition
        inside it. The condition is what actually enforces the ordering; this only turns a
        conditional failure into the right message.
        """

        instant = now or self.clock.now()
        held = await self.core.load_send_fence(scope)
        if held is not None and instant < held.expires_at:
            raise SendAuthorizationInProgressError(("SEND_FENCE_ACTIVE",))
