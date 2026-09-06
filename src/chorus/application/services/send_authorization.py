"""Send-time authorization: the case-side revalidation Phase 6 deliberately left absent.

The sender has **no Core access at all** -- that is a total explicit deny, not an absent grant
-- so it cannot check the case, the mandates, or the fence row. Every case-side check therefore
belongs to the compiler, performed inside fence acquisition, and that is why the operation
takes a *request* rather than an execution identifier
([ADR-025](../../../../docs/adr/ADR-025-one-deliberate-ses-attempt.md) SS 4).

This is the **in-process** implementation of :class:`chorus.ports.send_authorization.
SendAuthorizationPort`, and it is the one a local run and every contract test use. A deployed
sender reaches the same authority through an ``lambda:InvokeFunction`` adapter against the
compiler function, because a role holding a total Core deny cannot construct this object's
dependencies.

The order inside :meth:`SendAuthorization.authorize` is load-bearing
-------------------------------------------------------------------
**Acquire the fence, then revalidate while holding it.** Validating first and acquiring second
leaves a window that nobody owns: a contributor's revocation committing between the two is
invisible to the validation, which has already run, and invisible to the acquisition, which
checks only the fence -- and the message goes out under a mandate that had been withdrawn.

Acquisition is not a decision. It establishes the sixty-second ordering window inside which a
decision may be made: while the fence is live, every authorization-sensitive Core mutation fails
its ``ConditionCheck``, so nothing the revalidation reads can move underneath it. A denial
releases the fence at once, so a refused send holds the case only for the duration of its own
reads.

What is re-derived, and what is refused
---------------------------------------
Authority is **re-derived from live state every time**. The approval is consulted here for
*integrity* -- does this artifact still hash to what it claims -- and never for authority. That
is what makes the revocation-after-approval race resolve correctly: an approval made at epoch
``A`` is a perfectly valid artifact at epoch ``A+1``, and it authorizes nothing.

The one prohibition matters more than any check. ``CommunityCase.version`` is **never**
compared against the proposal's recorded ``case_version``. Lifecycle progression moved that
number on purpose -- the ``READY_FOR_ACTION -> ACTION_PROPOSED`` edge that created this
proposal moved it -- so requiring equality would fail every first send in the system.
[ADR-020](../../../../docs/adr/ADR-020-case-authorization-version.md) SS 6 removed that deadlock
and this module does not reintroduce it.

How the mandate check is performed, and why it is this rather than a digest
---------------------------------------------------------------------------
ADR-025 SS 4 requires the mandate row to be a *real* check -- "a revoked, adjusted, expired, or
newly-superseded mandate denies even if every counter happened to line up" -- and names whole
``authorization_snapshot_hash`` recomputation as the mechanism.

That exact mechanism is not reachable from the frozen artifacts. The compiler's snapshot covers
the requested-fact list, the evaluated fact rows, the corroboration reports, and the evidence
set, and the request that produced it is not stored on the view, in the pointer, or in any
locator: ``shareable-case-view/v2`` carries ``audit_refs`` holding the audit event identifier,
not the ``compile_id`` that addresses the compile's private lineage. Rebuilding the digest would
require re-running a whole compile against a request nobody kept.

So the **property** is implemented directly, over the terms that are actually authorization,
and it is strictly sharper than the digest would have been for this purpose. Every mandate the
view relied on is reloaded from live Core state and required to be, right now: named by the
current pointer at the *exact* same version, carrying the *exact* same ``terms_hash``, still
``APPROVED``, and unexpired. A revoke, an adjust, or a refusal creates version N+1 and moves the
pointer, so all three deny; a tampered stored version denies on the terms hash; an expiry that
has passed denies on the clock, which no stored digest could have expressed anyway. The set of
relied mandates is then digested and compared as a whole, so a mandate *disappearing* from the
live set denies too.

This is recorded as a documented deviation in mechanism, not in guarantee, and the case's
``authorization_version`` equality check stands beside it as the coarse backstop it was always
meant to be.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from chorus.application import observability
from chorus.application.errors import SendAuthorizationInProgressError
from chorus.application.services.action_authorization import (
    MIN_SEND_FENCE_WINDOW,
    SEND_FENCE_LIFETIME,
)
from chorus.domain.entities import (
    ActionProposalStatus,
    Approval,
    ApprovalDecision,
    CaseState,
    MandateStatus,
    Purpose,
)
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import (
    ExecutionId,
    MandateId,
    Sha256Digest,
)
from chorus.domain.mandates import DisclosureMandate
from chorus.ports.clock import Clock
from chorus.ports.errors import NotFoundError, PersistenceConflictError
from chorus.ports.records import SendFence, StoredShareableView
from chorus.ports.repositories import CoreRepositoryPort, ShareableRepositoryPort
from chorus.ports.scopes import CaseScope
from chorus.ports.send_authorization import (
    SendAuthorizationDenied,
    SendAuthorizationGranted,
    SendAuthorizationOutcome,
    SendAuthorizationRequest,
)
from chorus.privacy.canonical import (
    APPROVAL_HASH_OMITTED_FIELDS,
    hash_action_proposal,
    hash_approval,
    hash_value,
    verify_hash,
)

MANDATE_AUTHORITY_SCHEMA = "send-mandate-authority/v1"


class SendDenial(StrEnum):
    """Why send authorization was refused. Closed codes; never a value and never a reason blob."""

    CASE_NOT_ACTION_PROPOSED = "CASE_NOT_ACTION_PROPOSED"
    AUTHORIZATION_VERSION_MOVED = "AUTHORIZATION_VERSION_MOVED"
    VIEW_NOT_CURRENT = "VIEW_NOT_CURRENT"
    VIEW_EXPIRED = "VIEW_EXPIRED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
    APPROVAL_NOT_BOUND = "APPROVAL_NOT_BOUND"
    PROPOSAL_NOT_CURRENT = "PROPOSAL_NOT_CURRENT"
    MANDATE_AUTHORITY_MOVED = "MANDATE_AUTHORITY_MOVED"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    DESTINATION_REGISTRY_CHANGED = "DESTINATION_REGISTRY_CHANGED"
    ROUTING_TOKEN_CHANGED = "ROUTING_TOKEN_CHANGED"  # noqa: S105 - a reason code
    POLICY_BUILD_CHANGED = "POLICY_BUILD_CHANGED"
    PURPOSE_MISMATCH = "PURPOSE_MISMATCH"
    AUTHORIZATION_WINDOW_TOO_SHORT = "AUTHORIZATION_WINDOW_TOO_SHORT"


def mandate_authority_hash(mandates: tuple[DisclosureMandate, ...]) -> Sha256Digest:
    """Digest the exact authority a send relies on, order-independently.

    Sorted on immutable identity so two readings of one unchanged world agree, and covering the
    version and the terms digest of each mandate so an adjust -- which creates version N+1 with
    new terms -- cannot produce the same value as the version it superseded.

    It carries identifiers, versions, and digests. No grant, no scope, no contributor, and no
    fact identifier enters it.
    """

    return hash_value(
        {
            "schema": MANDATE_AUTHORITY_SCHEMA,
            "mandates": [
                {
                    "mandate_id": str(mandate.mandate_id),
                    "version": mandate.version,
                    "terms_hash": mandate.terms_hash.value,
                }
                for mandate in sorted(
                    mandates, key=lambda item: (str(item.mandate_id), item.version)
                )
            ],
        }
    )


@dataclass(slots=True)
class SendAuthorization:
    """The compiler-side authority behind ``AcquireSendAuthorizationFence``.

    It holds Core and Shareable *reads* and exactly one Core write -- the fence -- and that is
    the whole of its relationship with a case. It takes no case edge, bumps no counter, and
    never touches the proposal, the approval, or the execution.
    """

    core: CoreRepositoryPort
    shareable: ShareableRepositoryPort
    clock: Clock

    policy_version: str
    compiler_version: str
    policy_build_hash: Sha256Digest
    purpose: Purpose

    async def authorize(self, request: SendAuthorizationRequest) -> SendAuthorizationOutcome:
        """Take the fence, revalidate the case side **while holding it**, and grant or deny.

        The order is the repair, and it is the whole of it. Validating first and acquiring
        second leaves a window with no owner: a contributor's revocation that commits between
        the two is invisible to the validation, which has already run, and invisible to the
        acquisition, which checks only the fence. The message then goes out under a mandate
        that had been withdrawn.

        Acquisition is not a decision. It establishes the sixty-second ordering window in which
        a decision may be made -- a live fence makes every authorization-sensitive Core
        mutation fail its ``ConditionCheck`` -- so every fact the revalidation reads afterwards
        is a fact that cannot move underneath it. A denial releases the fence immediately, so
        the window costs a refused send exactly the duration of its own reads.

        Every failure returns a *denial* rather than raising, because the sender's correct
        response to a denial is a definite ``FAILED / STALE_AUTHORIZATION`` transition with no
        SES call -- and an exception would make that a control-flow accident rather than the
        recorded outcome it has to be.
        """

        now = self.clock.now()
        scope = request.scope
        expires_at = await self._expiry(request, now=now)
        if expires_at - now < MIN_SEND_FENCE_WINDOW:
            # Fewer than five seconds of remaining authority. Refusing is the answer that
            # cannot produce an SES call still in flight when its authority lapses. Checked
            # before acquisition because a fence nobody could use is not worth taking.
            return self._denied(request, (SendDenial.AUTHORIZATION_WINDOW_TOO_SHORT.value,))

        fence = SendFence(
            namespace=request.namespace,
            community_id=request.community_id,
            case_id=request.case_id,
            execution_id=request.execution_id,
            action_id=request.action_id,
            approval_id=request.approval_id,
            view_id=request.view_id,
            authorization_snapshot_hash=request.authorization_snapshot_hash,
            acquired_at=now,
            expires_at=expires_at,
        )
        held = await self.core.load_send_fence(scope)
        if held is not None and now < held.expires_at and held.execution_id != request.execution_id:
            # Another execution holds this case. Retryable for at most sixty seconds, which is
            # the only answer that keeps two holders from both believing they won.
            raise SendAuthorizationInProgressError(("SEND_FENCE_ACTIVE",))
        try:
            acquired = await self.core.acquire_send_fence(scope, fence)
        except PersistenceConflictError:
            raise SendAuthorizationInProgressError(("SEND_FENCE_ACTIVE",)) from None
        replayed = held is not None and held.execution_id == request.execution_id

        try:
            denials = await self._denials(request, now=self.clock.now())
        except BaseException:
            # An integrity failure inside the revalidation is still a fence this send will
            # never use. Released rather than left to expire, because sixty seconds of refused
            # mandate decisions is a cost somebody else pays for our exception.
            await self.release(scope, request.execution_id)
            raise
        if denials:
            await self.release(scope, request.execution_id)
            return self._denied(request, denials)

        observability.send_fence_acquired(
            namespace=request.namespace,
            community_id=request.community_id,
            case_id=request.case_id,
            execution_id=acquired.execution_id.value,
            replayed=replayed,
        )
        return SendAuthorizationGranted(fence=acquired, replayed=replayed)

    def _denied(
        self, request: SendAuthorizationRequest, reason_codes: tuple[str, ...]
    ) -> SendAuthorizationDenied:
        observability.send_fence_denied(
            namespace=request.namespace,
            community_id=request.community_id,
            case_id=request.case_id,
            execution_id=request.execution_id.value,
            reason_codes=reason_codes,
        )
        return SendAuthorizationDenied(reason_codes=reason_codes)

    async def release(self, scope: CaseScope, execution_id: ExecutionId) -> None:
        """Return the fence, conditioned on the holder's execution identity.

        The second half of the compiler's typed fence boundary, and it lives here rather than
        on the sender for the same reason acquisition does: the sender holds no Core access at
        all, so in a deployed topology both are invocations of the compiler's operation. Having
        one object own both is what keeps that true as the code changes.

        A conditional failure is **not** swallowed. It means the fence belongs to another
        execution or has already gone, and a release that reported success either way would
        make "the fence is clear" impossible to rely on.
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
            return
        observability.send_fence_released(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            execution_id=execution_id.value,
        )

    # -- the checks ---------------------------------------------------------------------

    async def _denials(
        self, request: SendAuthorizationRequest, *, now: datetime
    ) -> tuple[str, ...]:
        """Every way this send is no longer authorized, reported together.

        All of them rather than the first, because an operator reading ``action.send.failed``
        wants the whole disagreement: a routing token that moved *and* an epoch that moved are
        two different repairs.
        """

        scope = request.scope
        reasons: list[str] = []

        case = await self.core.load_case(scope)
        if case.state is not CaseState.ACTION_PROPOSED:
            reasons.append(SendDenial.CASE_NOT_ACTION_PROPOSED.value)
        if case.authorization_version != request.authorization_version:
            # The coarse backstop, and the one the revocation-after-approval race trips.
            # ``case.version`` is deliberately NOT compared: see the module docstring.
            reasons.append(SendDenial.AUTHORIZATION_VERSION_MOVED.value)

        view = await self.shareable.load_view(scope, request.view_id)
        reasons.extend(self._view_denials(request, view, now=now))
        reasons.extend(await self._pointer_denials(request))
        reasons.extend(await self._artifact_denials(request, view))
        reasons.extend(self._configuration_denials(request, view))
        reasons.extend(await self._mandate_denials(scope, view, now=now))
        return tuple(dict.fromkeys(reasons))

    def _view_denials(
        self, request: SendAuthorizationRequest, view: StoredShareableView, *, now: datetime
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if view.view_hash != request.view_hash or not _view_hash_verifies(view):
            reasons.append(SendDenial.VIEW_NOT_CURRENT.value)
        if view.authorization_version != request.authorization_version:
            reasons.append(SendDenial.AUTHORIZATION_VERSION_MOVED.value)
        # Equality at expiry means expired. This is the one freshness fact no storage condition
        # can express, because the passage of time mutates no row.
        if now >= view.expires_at:
            reasons.append(SendDenial.VIEW_EXPIRED.value)
        return tuple(reasons)

    async def _pointer_denials(self, request: SendAuthorizationRequest) -> tuple[str, ...]:
        pointer = await self.shareable.load_current_view_pointer(request.scope)
        reasons: list[str] = []
        if (
            pointer is None
            or pointer.view_id != request.view_id
            or pointer.view_hash != request.view_hash
        ):
            reasons.append(SendDenial.VIEW_NOT_CURRENT.value)
        action_pointer = await self.shareable.load_current_action_pointer(request.scope)
        if (
            action_pointer is None
            or action_pointer.action_id != request.action_id
            or action_pointer.execution_id != request.execution_id
            or action_pointer.proposal_hash != request.proposal_hash
            or action_pointer.status is not ActionProposalStatus.DRAFT
        ):
            reasons.append(SendDenial.PROPOSAL_NOT_CURRENT.value)
        return tuple(reasons)

    async def _artifact_denials(
        self, request: SendAuthorizationRequest, view: StoredShareableView
    ) -> tuple[str, ...]:
        """Integrity of the proposal and the approval; never authority from either."""

        reasons: list[str] = []
        proposal = await self.shareable.load_proposal(request.action_scope)
        if (
            proposal.proposal_hash != request.proposal_hash
            or hash_action_proposal(proposal) != proposal.proposal_hash
            or proposal.view_id != view.view_id
            or proposal.view_hash != view.view_hash
        ):
            raise IntegrityError("ACTION_PROPOSAL")
        approval = await self.shareable.load_approval(request.action_scope, request.approval_id)
        _require_approval_integrity(approval, request)
        if approval.decision is not ApprovalDecision.APPROVED:
            reasons.append(SendDenial.APPROVAL_NOT_APPROVED.value)
        if self.clock.now() >= approval.expires_at:
            reasons.append(SendDenial.APPROVAL_EXPIRED.value)
        return tuple(reasons)

    def _configuration_denials(
        self, request: SendAuthorizationRequest, view: StoredShareableView
    ) -> tuple[str, ...]:
        """Deployment-owned values, by exact equality, exactly as at proposal and approval.

        These are outside ``authorization_version`` on purpose (ADR-020 SS 3), so a verified
        snapshot proves the old view is internally coherent rather than that the deployment
        still runs the build that produced it.
        """

        reasons: list[str] = []
        if (
            view.policy_version != self.policy_version
            or view.compiler_version != self.compiler_version
            or view.policy_build_hash != self.policy_build_hash
            or request.policy_version != self.policy_version
            or request.compiler_version != self.compiler_version
            or request.policy_build_hash != self.policy_build_hash
        ):
            reasons.append(SendDenial.POLICY_BUILD_CHANGED.value)
        destination = view.destination
        if (
            destination.destination_id != request.destination_id
            or destination.registry_version != request.destination_registry_version
        ):
            reasons.append(SendDenial.DESTINATION_REGISTRY_CHANGED.value)
        if destination.routing_token != request.routing_token:
            reasons.append(SendDenial.ROUTING_TOKEN_CHANGED.value)
        if view.purpose is not self.purpose or request.purpose is not self.purpose:
            reasons.append(SendDenial.PURPOSE_MISMATCH.value)
        return tuple(reasons)

    async def _mandate_denials(
        self, scope: CaseScope, view: StoredShareableView, *, now: datetime
    ) -> tuple[str, ...]:
        """Reload every relied mandate from live Core state and require it to still authorize.

        The module docstring records why this, rather than whole-snapshot recomputation, is the
        mechanism. What it must catch, and does: a revocation, an adjustment, a refusal, a
        newly-superseded version, an expiry that has passed, and a mandate that has left the
        live set entirely.
        """

        mandates = await self._live_relied_mandates(scope, view)
        if mandates is None:
            return (SendDenial.MANDATE_AUTHORITY_MOVED.value,)
        reasons: list[str] = []
        expected = hash_value(
            {
                "schema": MANDATE_AUTHORITY_SCHEMA,
                "mandates": [
                    {
                        "mandate_id": str(ref.mandate_id),
                        "version": ref.version,
                        "terms_hash": ref.terms_hash.value,
                    }
                    for ref in sorted(
                        view.mandate_version_set,
                        key=lambda item: (str(item.mandate_id), item.version),
                    )
                ],
            }
        )
        if mandate_authority_hash(mandates) != expected:
            reasons.append(SendDenial.MANDATE_AUTHORITY_MOVED.value)
        for mandate in mandates:
            if mandate.status is not MandateStatus.APPROVED:
                reasons.append(SendDenial.MANDATE_AUTHORITY_MOVED.value)
            if mandate.expires_at is not None and now >= mandate.expires_at:
                reasons.append(SendDenial.MANDATE_EXPIRED.value)
        return tuple(reasons)

    async def _live_relied_mandates(
        self, scope: CaseScope, view: StoredShareableView
    ) -> tuple[DisclosureMandate, ...] | None:
        """The current version of each mandate the view relied on, or ``None`` if any moved.

        The *pointer* is what decides which version is current, so this reads the pointer first
        and then the version it names. Reading the version the view names directly would answer
        "is that old row still there" -- which it always is, because mandate versions are
        append-only -- rather than "is it still the one in force".
        """

        loaded: list[DisclosureMandate] = []
        for ref in view.mandate_version_set:
            mandate_id = MandateId(ref.mandate_id)
            try:
                pointer = await self.core.load_current_mandate_pointer(scope, mandate_id)
            except NotFoundError:
                # A mandate the view relied on has no current pointer at all. That is not a
                # readable authority, so it is not one.
                return None
            loaded.append(
                await self.core.load_mandate_version(scope, mandate_id, pointer.pointer.version)
            )
        return tuple(loaded)

    # -- expiry -------------------------------------------------------------------------

    async def _expiry(self, request: SendAuthorizationRequest, *, now: datetime) -> datetime:
        """``min(now + 60s, view.expires_at, approval.expires_at, earliest mandate expiry)``.

        Every term is an authority that could lapse mid-send, so the fence stops authorizing at
        the first of them. A fence outliving any one of them would be a window in which a send
        proceeded under an authority that had already ended.
        """

        view = await self.shareable.load_view(request.scope, request.view_id)
        approval = await self.shareable.load_approval(request.action_scope, request.approval_id)
        candidates = [now + SEND_FENCE_LIFETIME, view.expires_at, approval.expires_at]
        mandates = await self._live_relied_mandates(request.scope, view)
        for mandate in mandates or ():
            if mandate.expires_at is not None:
                candidates.append(mandate.expires_at)
        latest = min(candidates)
        # A fence whose expiry is not strictly after its acquisition is not a fence; the entity
        # refuses one, and the caller has already been told the window is too short.
        return max(latest, now + timedelta(microseconds=1))


def _require_approval_integrity(approval: Approval, request: SendAuthorizationRequest) -> None:
    """Recompute the decision's own digest and require it to bind this exact send.

    An approval that does not verify is an integrity failure rather than a policy answer, and
    it fails closed. This is the check that only became meaningful once the approval stopped
    carrying a mutable field (ADR-023 SS 1, T32).
    """

    if not verify_hash(approval, approval.approval_hash, omit_fields=APPROVAL_HASH_OMITTED_FIELDS):
        raise IntegrityError("APPROVAL")
    if hash_approval(approval) != approval.approval_hash:  # pragma: no cover - same computation
        raise IntegrityError("APPROVAL")
    if (
        approval.case_id != request.case_id
        or approval.action_id != request.action_id
        or approval.execution_id != request.execution_id
        or approval.proposal_hash != request.proposal_hash
        or approval.view_hash != request.view_hash
    ):
        raise IntegrityError("APPROVAL")


def _view_hash_verifies(view: StoredShareableView) -> bool:
    return verify_hash(view, view.view_hash, omit_fields=frozenset({"view_hash"}))


__all__ = [
    "MANDATE_AUTHORITY_SCHEMA",
    "SendAuthorization",
    "SendAuthorizationDenied",
    "SendAuthorizationGranted",
    "SendAuthorizationOutcome",
    "SendAuthorizationRequest",
    "SendDenial",
    "mandate_authority_hash",
]
