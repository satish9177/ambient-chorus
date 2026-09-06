"""One deliberate SES attempt per approved execution, in the order ADR-025 SS 3 freezes.

The order is the design, and it is normative
--------------------------------------------
The frozen pipeline used to run *claim, fence, render, send*. That could not be executed: the
presence table makes ``rendered_message_hash`` and ``ses_request_token_hash`` **required** the
moment an execution reaches ``SENDING``, so the first step had to write two values the third and
fourth had not produced yet.

Rendering is a pure function of immutable inputs with no side effect, so it can happen before
the claim at no cost -- and the presence table means it must::

     1. strong-load pointer, proposal, approval, execution, and the exact bound view
     2. verify locally, calling nothing external
     3. render deterministically
     4. REQUIRE rendered_message_hash == proposal.preview_hash   <- no SES, no claim, on failure
     5. derive ses_request_token_hash; mint this attempt's claim owner
     6. CLAIM  APPROVED@v -> SENDING@v+1, writing the claim owner  <- transaction C
    6b. REQUIRE durable SENDING carrying **this** claim owner      <- no SES if it is another's
     7. acquire the send fence; the compiler revalidates the case side from inside it
     8. re-read the clock against the fence expiry
     9. resolve the recipient; assert exactly one
    9b. re-read the clock again, with nothing between it and the call
    10. ONE ses:SendEmail
    11. persist the outcome                                       <- transaction D, E, or F
    12. release the fence, in a finally

Steps 6b and 9b are the two places a lost outcome and a passing second respectively used to
reach SES. 6b exists because the claim's commit proof is keyed on the *execution*, so a proof
another worker wrote resolves an ambiguous claim as "committed" -- which is true, and about
somebody else's transaction. 9b exists because step 8's sample sits before two awaited registry
lookups, and a clock read a later await can invalidate is not a check on the instant that
matters.

Step 4 is what the phase exists for, and it is a comparison rather than a tautology only because
the two digests have different owners and different moments: ``preview_hash`` was sealed by the
proposal a human approved, ``rendered_message_hash`` is produced here. It fires when the
template version, the sending identity, or the destination routing has changed since approval,
because all four are inside the digest.

The duplicate-send boundary is the claim, not the fence
--------------------------------------------------------
**At most one deliberate SES call is made per ``ActionExecution``, and the thing that guarantees
it is the conditional ``APPROVED@v -> SENDING@v+1`` write.** The fence is per *case* and admits
a replay by the same ``execution_id``, so it cannot and does not prevent a second worker from
sending the same execution -- it exists to order a send against a mandate revocation. A process
that loses the claim observes a state from which :data:`REPLAY_TABLE` forbids an SES call,
unconditionally and without inspecting anything else.

``FAILED`` requires proof; everything else is ``SEND_UNKNOWN``
--------------------------------------------------------------
A received error response proves SES processed the request and declined it. A connection that
never established proves at the transport layer that no request was transmitted. Everything
between those two proofs, **and every exception class nobody enumerated**, is unknown. The
adapter classifies, and this module wraps the call anyway, so an adapter that breaks its own
contract still lands on the safe side rather than on a stack trace.

CHORUS does **not** deliver email exactly once and this module does not claim to. A message may
have been delivered and recorded as ``SEND_UNKNOWN``; that is the accepted residual, it is
visible and alarmed, and the alternative -- resending to be sure -- would turn an unknown into a
certainty of duplication.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from chorus.application import observability
from chorus.application.errors import SendAuthorizationInProgressError
from chorus.application.services.action_authorization import (
    send_claim_key_hash,
    send_key,
    send_request_hash,
    send_result_key_hash,
)
from chorus.application.services.action_renderer import (
    TEMPLATE_VERSION,
    RenderedPreview,
    render_preview,
)
from chorus.application.services.ses_message import (
    build_email_request,
    send_claim_owner_hash,
    ses_request_token_hash,
)
from chorus.domain.entities import (
    ActionExecution,
    ActionExecutionState,
    ActionProposal,
    ActionProposalStatus,
    ActorType,
    Approval,
    ApprovalDecision,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
)
from chorus.domain.errors import DomainError, DomainErrorCode, IntegrityError
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CaseId,
    CommunityId,
    ExecutionId,
    IdGenerator,
    Namespace,
    Sha256Digest,
)
from chorus.domain.state import transition_action_execution
from chorus.ports.clock import Clock
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.idempotency import EntityRef, IdempotencyKey
from chorus.ports.records import StoredSafeDestination, StoredShareableView
from chorus.ports.repositories import (
    AuditRepositoryPort,
    IdempotencyRepositoryPort,
    ShareableRepositoryPort,
)
from chorus.ports.scopes import ActionScope, CaseScope
from chorus.ports.send_authorization import (
    SendAuthorizationDenied,
    SendAuthorizationPort,
    SendAuthorizationRequest,
)
from chorus.ports.sender import (
    DestinationRegistryError,
    DestinationRegistryPort,
    EmailSenderPort,
    ResolvedDestination,
    SendFailureCode,
    SendingIdentity,
    SendUnknownReason,
    SesAccepted,
    SesDefiniteFailure,
    SesUnknown,
)
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork
from chorus.privacy.canonical import (
    APPROVAL_HASH_OMITTED_FIELDS,
    hash_action_proposal,
    verify_hash,
)

CLAIM_TRANSACTION = "send-action-claim"
RESULT_TRANSACTION = "send-action-result"

CLAIM_PARTICIPANTS = 3
"""Shape C: the execution compare-and-swap, ``action.send.started``, and the claim proof.

No fence check -- a fence can only be held by an execution that has already passed
``APPROVED``, and participant 1's own condition is that this one has not. No approval
``ConditionCheck`` either: the approval is immutable, so the strong read at step 2 is not a
value a condition could improve on.
"""

RESULT_PARTICIPANTS = 3
"""Shapes D, E, and F: one shape, three terminal outcomes.

The execution transition, the matching audit event, and the send-result commit proof. D is also
the shape used for the pre-SES failures at steps 4 and 7, with ``STALE_AUTHORIZATION`` and no
SES call having been made.
"""


class SendReplayOutcome(StrEnum):
    """What a delivery finding this state must do. ``PROCEED`` is the only one that calls SES."""

    CONFLICT = "CONFLICT"
    PROCEED = "PROCEED"
    IN_PROGRESS = "IN_PROGRESS"
    ALREADY_SENT = "ALREADY_SENT"
    TERMINAL_FAILED = "TERMINAL_FAILED"
    QUARANTINED = "QUARANTINED"


REPLAY_TABLE: dict[ActionExecutionState, SendReplayOutcome] = {
    ActionExecutionState.DRAFT: SendReplayOutcome.CONFLICT,
    ActionExecutionState.APPROVED: SendReplayOutcome.PROCEED,
    ActionExecutionState.SENDING: SendReplayOutcome.IN_PROGRESS,
    ActionExecutionState.SENT: SendReplayOutcome.ALREADY_SENT,
    ActionExecutionState.FAILED: SendReplayOutcome.TERMINAL_FAILED,
    ActionExecutionState.SEND_UNKNOWN: SendReplayOutcome.QUARANTINED,
}
"""The frozen replay table, consulted before anything else and without exception.

A table rather than a chain of conditionals, so "which states may call SES" is one readable
fact and a state added to the enum has no default. Exactly one entry is ``PROCEED``.
"""


class SendDeniedError(DomainError):
    """This delivery may not send, and the state it found says why."""

    __slots__ = ("outcome", "state")

    def __init__(self, outcome: SendReplayOutcome, state: ActionExecutionState) -> None:
        super().__init__(DomainErrorCode.STATE_TRANSITION_ERROR, outcome.value)
        self.outcome = outcome
        self.state = state

    @property
    def safe_code(self) -> str:
        return self.outcome.value


class SendFailureReason(StrEnum):
    """Definite pre-SES failure causes, each with proof that no request was transmitted."""

    STALE_AUTHORIZATION = "STALE_AUTHORIZATION"
    RENDERED_HASH_MISMATCH = "RENDERED_HASH_MISMATCH"
    VIEW_EXPIRED = "VIEW_EXPIRED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
    SUPERSEDED_PROPOSAL = "SUPERSEDED_PROPOSAL"
    DESTINATION_REGISTRY_CHANGED = "DESTINATION_REGISTRY_CHANGED"
    ROUTING_TOKEN_CHANGED = "ROUTING_TOKEN_CHANGED"  # noqa: S105 - a reason code
    SENDER_IDENTITY_CHANGED = "SENDER_IDENTITY_CHANGED"
    TEMPLATE_VERSION_CHANGED = "TEMPLATE_VERSION_CHANGED"
    FENCE_EXPIRED = "FENCE_EXPIRED"


@dataclass(frozen=True, slots=True, kw_only=True)
class SendActionCommand:
    """The frozen execute body, and nothing a caller could steer a message with.

    It accepts no recipient, no subject, no body, no claim, no attachment, no template, and no
    retry flag. There is no field in which any of those could be supplied, which is why the
    absence is a property of the type rather than a validation rule.
    """

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    execution_id: ExecutionId
    approval_id: ApprovalId
    expected_execution_version: int
    actor_id_hash: Sha256Digest
    correlation_id: UUID
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.expected_execution_version < 1:
            raise ValueError("expected_execution_version must be positive")

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
class SendActionResult:
    """The durable outcome, in identifiers and closed codes. Never a body, never an address."""

    execution_id: ExecutionId
    state: ActionExecutionState
    version: int
    ses_message_id: str | None
    failure_code: str | None
    reason_codes: tuple[str, ...]
    ses_call_made: bool
    """Whether **one** deliberate SES call was issued by this delivery.

    Reported rather than inferred, because the safety property is about the number of deliberate
    attempts and a test that had to infer it from an outcome could not tell a definite failure
    that reached SES from one that never did.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class _SendState:
    """Everything one send strongly read and locally verified before touching anything."""

    proposal: ActionProposal
    approval: Approval
    execution: ActionExecution
    view: StoredShareableView


@dataclass(slots=True)
class SendAction:
    """Render, compare, claim, fence, send once, and record what happened."""

    shareable: ShareableRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    authorization: SendAuthorizationPort
    """The compiler boundary, and the sender's **only** route to Core state.

    A *port* rather than the in-process implementation, and that is the boundary rather than a
    style preference: a deployed sender's role denies ``dynamodb:*`` on the Core table outright,
    so the in-process authority -- which is built over a ``CoreRepository`` -- is not
    constructible there at all. Naming the concrete class here would have made the deployed
    composition unable to satisfy its own type.

    There is deliberately no ``core`` field on this use case. A repository handle here would be
    a capability the deployed principal does not have -- code that worked locally and failed in
    an account. Every case-side question, and both halves of the fence, go through this one
    object.
    """

    sender: EmailSenderPort
    registry: DestinationRegistryPort
    clock: Clock
    ids: IdGenerator
    destination: StoredSafeDestination
    from_identity_id: str
    configuration_set: str
    template_version: str = TEMPLATE_VERSION

    async def execute(self, command: SendActionCommand) -> SendActionResult:
        with observability.emitting_as(observability.SERVICE_SENDER):
            return await self._execute(command)

    async def _execute(self, command: SendActionCommand) -> SendActionResult:
        # Step 1-2. The replay table is consulted before anything is rendered, resolved, or
        # claimed, so a redelivery into SENDING or into any terminal state cannot reach SES by
        # any path at all.
        execution = await self.shareable.load_execution(command.action_scope, command.execution_id)
        outcome = REPLAY_TABLE[execution.state]
        if outcome is not SendReplayOutcome.PROCEED:
            return self._replayed(execution, outcome)

        state, denials = await self._load_and_verify(command, execution)
        if denials:
            # A definite pre-send failure. Nothing was claimed, no fence exists, and no SES
            # call was made -- shape D from APPROVED.
            return await self._fail(command, state.execution, denials, ses_call_made=False)

        # Step 3-4. Render, then compare. This is the invariant the phase exists for, and it
        # happens before anything is consumed.
        preview = render_preview(
            state.proposal,
            state.view,
            from_identity_id=self.from_identity_id,
            template_version=self.template_version,
        )
        if preview.preview_hash != state.proposal.preview_hash:
            return await self._fail(
                command,
                state.execution,
                (
                    SendFailureReason.RENDERED_HASH_MISMATCH.value,
                    SendFailureReason.STALE_AUTHORIZATION.value,
                ),
                ses_call_made=False,
            )
        return await self._claim_and_send(command, state, preview)

    # -- steps 5 to 12 -----------------------------------------------------------------------

    async def _claim_and_send(
        self, command: SendActionCommand, state: _SendState, preview: RenderedPreview
    ) -> SendActionResult:
        """Claim once, fence, send at most once, and release on every terminal outcome."""

        send_execution_key = state.execution.idempotency_key
        if send_execution_key is None:  # pragma: no cover - required at APPROVED
            raise IntegrityError("ACTION_EXECUTION")
        # Step 5. Derived, not stored -- a recovery path recomputes it from the same durable
        # values without having kept it.
        token = ses_request_token_hash(
            namespace=command.namespace,
            action_id=command.action_id,
            execution_id=command.execution_id,
            idempotency_key=send_execution_key,
        )
        # Step 5b. The claim owner, minted here and nowhere else. Every other derivation on
        # this path is a pure function of durable values, so two workers racing one execution
        # compute all of them identically -- which is exactly why none of them can say who owns
        # a claim.
        owner = send_claim_owner_hash(
            namespace=command.namespace,
            action_id=command.action_id,
            execution_id=command.execution_id,
            claim_nonce=self.ids.new_uuid(),
        )
        # Step 6. The one-attempt boundary. Exactly one of any number of concurrent workers
        # commits this; every other gets a conflict and, on reload, a state the replay table
        # forbids sending from.
        await self._claim(
            command,
            state,
            preview=preview,
            token=token,
            owner=owner,
            send_execution_key=send_execution_key,
        )
        # Step 6b. Ownership is proved by the row, never by the commit's own answer.
        claimed = await self.shareable.load_execution(command.action_scope, command.execution_id)
        if not self._owns(claimed, owner):
            # The claim's outcome was ambiguous and durable state says somebody else owns this
            # execution. **Zero** SES calls on this branch, and nothing is written: only the
            # owner of an attempt may persist an outcome for it.
            return self._not_ours(command, claimed)

        fence_held = False
        try:
            # Step 7. The compiler revalidates the whole case side -- from inside the fence, so
            # a revocation cannot commit between the validation and the acquisition -- and it
            # does so because the sender holds no Core access and cannot check any of it.
            granted = await self.authorization.authorize(self._authorization_request(state))
            if isinstance(granted, SendAuthorizationDenied):
                return await self._fail(
                    command,
                    claimed,
                    (*granted.reason_codes, SendFailureReason.STALE_AUTHORIZATION.value),
                    ses_call_made=False,
                )
            fence_held = True
            # Step 8. The clock, sampled again. Expiry is the one freshness fact no storage
            # condition can express, because the passage of time mutates no row.
            if self._expired(granted.fence.expires_at):
                return await self._fail(
                    command,
                    claimed,
                    (
                        SendFailureReason.FENCE_EXPIRED.value,
                        SendFailureReason.STALE_AUTHORIZATION.value,
                    ),
                    ses_call_made=False,
                )
            # Step 9. Server-side, from the registry, against the exact triple the preview hash
            # binds. Exactly one recipient is asserted by the payload type itself.
            try:
                destination, identity = await self._resolve(state)
            except DestinationRegistryError as error:
                return await self._fail(command, claimed, (error.reason_code,), ses_call_made=False)
            return await self._send_once(
                command,
                claimed,
                state,
                preview,
                destination,
                identity,
                fence_expires_at=granted.fence.expires_at,
            )
        finally:
            if fence_held:
                # Step 12. Released on **every** terminal outcome, including the ambiguous one.
                # A fence retained to mark "something happened here" would permanently refuse
                # every future mandate decision and revocation on this case, which inverts the
                # guarantee the fence exists to provide: it is a sixty-second ordering window
                # for contributors' authority, not a lien on it.
                await self._release(command)

    def _expired(self, expires_at: datetime) -> bool:
        """Equality at expiry means expired, here as everywhere else."""

        return self.clock.now() >= expires_at

    @staticmethod
    def _owns(durable: ActionExecution, owner: Sha256Digest) -> bool:
        """Whether **this attempt** owns the claim on the row that was just read.

        The commit's own answer is not sufficient, and that is the whole point. A claim whose
        transport outcome is lost is resolved by the unit of work against a commit proof keyed
        on the *execution*, so a proof another worker wrote reads back as "the transaction
        committed" -- true, and about somebody else's transaction. Before the claim owner
        existed that shared proof was the only evidence a recovering worker had, and it
        authorized a second deliberate SES call for one approved message.

        A strongly consistent read of the execution answers what the proof cannot:

        * ``SENDING`` carrying this attempt's owner -- this attempt claimed and may continue;
        * ``SENDING`` carrying another owner -- somebody else claimed and this one must not send;
        * anything else -- the row has moved past a claim, and no state but ``APPROVED`` admits
          an SES call at all.

        The read costs one strongly consistent get on the happy path, and it is paid on every
        send rather than only where ambiguity was noticed: a check that runs only where somebody
        remembered it was needed is a check the next change moves out from under.
        """

        return durable.state is ActionExecutionState.SENDING and durable.claim_owner_hash == owner

    def _not_ours(self, command: SendActionCommand, execution: ActionExecution) -> SendActionResult:
        """Answer from the durable row after losing an ambiguous claim. Zero SES calls."""

        observability.execution_claim_not_owned(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            execution_id=execution.execution_id.value,
        )
        return self._replayed(execution, REPLAY_TABLE[execution.state])

    async def _send_once(
        self,
        command: SendActionCommand,
        claimed: ActionExecution,
        state: _SendState,
        preview: RenderedPreview,
        destination: ResolvedDestination,
        identity: SendingIdentity,
        *,
        fence_expires_at: datetime,
    ) -> SendActionResult:
        """Step 10 and 11. One call, then the outcome its classification demands."""

        request = build_email_request(
            namespace=command.namespace,
            execution_id=command.execution_id,
            identity=identity,
            destination=destination,
            configuration_set=self.configuration_set,
            subject=preview.document.subject,
            text_body=preview.text_body,
            html_body=preview.html_body,
        )
        if self._expired(fence_expires_at):
            # The last sample, taken after **every** awaited resolution and after the payload
            # is built, with nothing between it and the call. Step 8's check happens before two
            # awaits -- a destination lookup and an identity lookup, each of which reaches a
            # secret store -- and a clock read that a later await can invalidate is not a check
            # on the instant that matters.
            #
            # The fence expiry is ``min(now + 60s, view.expires_at, approval.expires_at,
            # earliest relied-on mandate expiry)``, so this one comparison is every frozen
            # temporal boundary at once. Checking them separately here would be four ways to
            # spell one number, and the ways would drift.
            return await self._fail(
                command,
                claimed,
                (
                    SendFailureReason.FENCE_EXPIRED.value,
                    SendFailureReason.STALE_AUTHORIZATION.value,
                ),
                ses_call_made=False,
            )
        try:
            outcome = await self.sender.send(request)
        except Exception:
            # The port's contract is to classify rather than raise. An adapter that breaks it
            # must still land on the safe side, and the safe side is that a request may have
            # been transmitted.
            outcome = SesUnknown(reason_code=SendUnknownReason.SES_TRANSPORT_AMBIGUOUS)

        match outcome:
            case SesAccepted(message_id=message_id):
                return await self._succeed(command, claimed, message_id, preview)
            case SesDefiniteFailure(failure_code=code, detail_safe=detail):
                return await self._fail(
                    command, claimed, (code.value,), ses_call_made=True, detail_safe=detail
                )
            case SesUnknown(reason_code=reason):
                return await self._unknown(command, claimed, reason)
            case _:  # pragma: no cover - the outcome union is closed
                raise AssertionError("unreachable SES outcome")

    # -- transaction C -----------------------------------------------------------------------

    async def _claim(
        self,
        command: SendActionCommand,
        state: _SendState,
        *,
        preview: RenderedPreview,
        token: Sha256Digest,
        owner: Sha256Digest,
        send_execution_key: str,
    ) -> ActionExecution:
        claimed = transition_action_execution(
            state.execution,
            ActionExecutionState.SENDING,
            expected_version=state.execution.version,
            now=self.clock.now(),
            claim_owner_hash=owner,
            rendered_message_hash=preview.preview_hash,
            ses_request_token_hash=token,
            started_at=self.clock.now(),
        )
        key, request_hash = self._key(command, send_claim_key_hash, send_execution_key)
        now = self.clock.now()
        operations = (
            self.shareable.stage_update_execution(
                command.action_scope, claimed, expected_version=state.execution.version
            ),
            self.audit.stage_append_case_event(
                command.scope,
                self._audit_event(
                    command,
                    execution=claimed,
                    event_type="action.send.started",
                    reason_codes=(),
                    now=now,
                    input_hash=state.proposal.preview_hash,
                    output_hash=claimed.rendered_message_hash,
                ),
            ),
            self.idempotency.stage_create_completed(
                key,
                request_hash=request_hash,
                result_entity_refs=(
                    EntityRef(
                        entity_type="ACTION_EXECUTION",
                        entity_id=claimed.execution_id.value,
                        version=claimed.version,
                    ),
                ),
                response_status=202,
                now=now,
            ),
        )
        if len(operations) != CLAIM_PARTICIPANTS:  # pragma: no cover - arithmetic guard
            raise IntegrityError("ACTION_EXECUTION")
        try:
            await self.unit_of_work.commit(
                TransactionPlan(
                    name=CLAIM_TRANSACTION,
                    operations=operations,
                    audit_required=True,
                    commit_proof=self.idempotency.commit_proof(key, request_hash=request_hash),
                )
            )
        except PersistenceConflictError:
            # Another worker claimed first, or a human withdrew. Either way this delivery has
            # not consumed anything and will never call SES: the reload finds a state the
            # replay table refuses.
            raise
        observability.execution_sending(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            execution_id=claimed.execution_id.value,
            proposal_hash=state.proposal.proposal_hash,
            preview_hash=state.proposal.preview_hash,
        )
        return claimed

    # -- transactions D, E, F ------------------------------------------------------------------

    async def _succeed(
        self,
        command: SendActionCommand,
        claimed: ActionExecution,
        message_id: str,
        preview: RenderedPreview,
    ) -> SendActionResult:
        now = self.clock.now()
        sent = transition_action_execution(
            claimed,
            ActionExecutionState.SENT,
            expected_version=claimed.version,
            now=now,
            ses_message_id=message_id,
            finished_at=now,
        )
        await self._persist_outcome(command, sent, "action.sent", (), now)
        observability.execution_sent(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            execution_id=sent.execution_id.value,
            preview_hash=preview.preview_hash,
        )
        return SendActionResult(
            execution_id=sent.execution_id,
            state=sent.state,
            version=sent.version,
            ses_message_id=message_id,
            failure_code=None,
            reason_codes=(),
            ses_call_made=True,
        )

    async def _fail(
        self,
        command: SendActionCommand,
        execution: ActionExecution,
        reason_codes: tuple[str, ...],
        *,
        ses_call_made: bool,
        detail_safe: str | None = None,
    ) -> SendActionResult:
        """Shape D. Also the shape for every pre-SES failure, from ``APPROVED`` or ``SENDING``."""

        now = self.clock.now()
        failure_code = reason_codes[0] if reason_codes else SendFailureCode.SES_REJECTED.value
        failed = transition_action_execution(
            execution,
            ActionExecutionState.FAILED,
            expected_version=execution.version,
            now=now,
            finished_at=now,
            failure_code=failure_code,
            failure_detail_safe=detail_safe,
        )
        await self._persist_outcome(command, failed, "action.send.failed", reason_codes, now)
        observability.execution_failed(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            execution_id=failed.execution_id.value,
            reason_codes=reason_codes,
        )
        return SendActionResult(
            execution_id=failed.execution_id,
            state=failed.state,
            version=failed.version,
            ses_message_id=None,
            failure_code=failure_code,
            reason_codes=reason_codes,
            ses_call_made=ses_call_made,
        )

    async def _unknown(
        self, command: SendActionCommand, claimed: ActionExecution, reason: SendUnknownReason
    ) -> SendActionResult:
        """Shape F. The honest answer, and the one no path ever retries."""

        now = self.clock.now()
        # ``failure_detail_safe`` is deliberately **not** written here, although the presence
        # table permits it at ``SEND_UNKNOWN``. Presence is monotonic, and ``reconciled_at``
        # aside, ``failure_detail_safe`` is ``ABSENT`` at ``SENT`` -- so a row that recorded the
        # ambiguity here could never take the frozen ``SEND_UNKNOWN -> SENT`` edge on positive
        # evidence. The reason code belongs in the ``action.send.unknown`` audit event and the
        # log line, which is where ADR-025 SS 14 puts it, and the quarantine itself is what the
        # row records.
        unknown = transition_action_execution(
            claimed,
            ActionExecutionState.SEND_UNKNOWN,
            expected_version=claimed.version,
            now=now,
            finished_at=now,
        )
        await self._persist_outcome(command, unknown, "action.send.unknown", (reason.value,), now)
        observability.execution_unknown(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            execution_id=unknown.execution_id.value,
            reason_codes=(reason.value,),
        )
        return SendActionResult(
            execution_id=unknown.execution_id,
            state=unknown.state,
            version=unknown.version,
            ses_message_id=None,
            failure_code=None,
            reason_codes=(reason.value,),
            ses_call_made=True,
        )

    async def _persist_outcome(
        self,
        command: SendActionCommand,
        execution: ActionExecution,
        event_type: str,
        reason_codes: tuple[str, ...],
        now: datetime,
    ) -> None:
        """The one three-participant shape behind all three terminal outcomes."""

        key, request_hash = self._key(command, send_result_key_hash, _require_send_key(execution))
        operations = (
            self.shareable.stage_update_execution(
                command.action_scope, execution, expected_version=execution.version - 1
            ),
            self.audit.stage_append_case_event(
                command.scope,
                self._audit_event(
                    command,
                    execution=execution,
                    event_type=event_type,
                    reason_codes=reason_codes,
                    now=now,
                    input_hash=execution.rendered_message_hash,
                    output_hash=execution.ses_request_token_hash,
                ),
            ),
            self.idempotency.stage_create_completed(
                key,
                request_hash=request_hash,
                result_entity_refs=(
                    EntityRef(
                        entity_type="ACTION_EXECUTION",
                        entity_id=execution.execution_id.value,
                        version=execution.version,
                    ),
                ),
                response_status=200,
                now=now,
            ),
        )
        if len(operations) != RESULT_PARTICIPANTS:  # pragma: no cover - arithmetic guard
            raise IntegrityError("ACTION_EXECUTION")
        await self.unit_of_work.commit(
            TransactionPlan(
                name=RESULT_TRANSACTION,
                operations=operations,
                audit_required=True,
                commit_proof=self.idempotency.commit_proof(key, request_hash=request_hash),
            )
        )

    # -- loading and local verification -------------------------------------------------------

    async def _load_and_verify(
        self, command: SendActionCommand, execution: ActionExecution
    ) -> tuple[_SendState, tuple[str, ...]]:
        """Step 2, calling nothing external and deciding nothing about the case.

        The case-side facts -- state, epoch, mandates -- are the compiler's at step 7, because
        the sender cannot read Core at all. What is checked here is what the sender *can* see:
        the artifacts' own integrity, their bindings to each other, their expiry, and the
        deployment values that are inside ``preview_hash``.
        """

        proposal = await self.shareable.load_proposal(command.action_scope)
        view = await self.shareable.load_view(command.scope, proposal.view_id)
        approval = await self.shareable.load_approval(command.action_scope, command.approval_id)
        pointer = await self.shareable.load_current_action_pointer(command.scope)
        state = _SendState(proposal=proposal, approval=approval, execution=execution, view=view)

        if hash_action_proposal(proposal) != proposal.proposal_hash:
            raise IntegrityError("ACTION_PROPOSAL")
        if not verify_hash(view, view.view_hash, omit_fields=frozenset({"view_hash"})):
            raise IntegrityError("SHAREABLE_VIEW")
        if not verify_hash(
            approval, approval.approval_hash, omit_fields=APPROVAL_HASH_OMITTED_FIELDS
        ):
            raise IntegrityError("APPROVAL")
        if (
            approval.execution_id != command.execution_id
            or approval.action_id != command.action_id
            or approval.case_id != command.case_id
            or approval.proposal_hash != proposal.proposal_hash
            or approval.view_hash != proposal.view_hash
        ):
            raise IntegrityError("APPROVAL")
        if execution.approval_id != approval.approval_id:
            raise IntegrityError("ACTION_EXECUTION")
        if execution.version != command.expected_execution_version:
            raise SendDeniedError(SendReplayOutcome.CONFLICT, execution.state)

        now = self.clock.now()
        reasons: list[str] = []
        if approval.decision is not ApprovalDecision.APPROVED:
            reasons.append(SendFailureReason.APPROVAL_NOT_APPROVED.value)
        # Equality at expiry means expired, in both places.
        if now >= approval.expires_at:
            reasons.append(SendFailureReason.APPROVAL_EXPIRED.value)
        if now >= view.expires_at:
            reasons.append(SendFailureReason.VIEW_EXPIRED.value)
        if (
            pointer is None
            or pointer.action_id != command.action_id
            or pointer.execution_id != command.execution_id
            or pointer.proposal_hash != proposal.proposal_hash
            or pointer.status is not ActionProposalStatus.DRAFT
        ):
            reasons.append(SendFailureReason.SUPERSEDED_PROPOSAL.value)
        reasons.extend(self._configuration_reasons(view))
        return state, tuple(dict.fromkeys(reasons))

    def _configuration_reasons(self, view: StoredShareableView) -> tuple[str, ...]:
        """The deployment values that live inside ``preview_hash``, checked by exact equality.

        Step 4 would catch every one of these structurally, because all of them are in the
        digest. They are checked here as well so the recorded ``failure_code`` names the
        *specific* cause an operator has to repair rather than the generic digest mismatch.
        """

        current = self.destination
        reasons: list[str] = []
        if (
            view.destination.destination_id != current.destination_id
            or view.destination.registry_version != current.registry_version
            or view.destination.display_label != current.display_label
            or view.destination.kind is not current.kind
        ):
            reasons.append(SendFailureReason.DESTINATION_REGISTRY_CHANGED.value)
        if view.destination.routing_token != current.routing_token:
            reasons.append(SendFailureReason.ROUTING_TOKEN_CHANGED.value)
        if self.template_version != TEMPLATE_VERSION:
            reasons.append(SendFailureReason.TEMPLATE_VERSION_CHANGED.value)
        return tuple(reasons)

    async def _resolve(self, state: _SendState) -> tuple[ResolvedDestination, SendingIdentity]:
        """Resolve the recipient and the letterhead, from the registry the human cannot see.

        The triple comes from ``view.destination`` rather than from configuration, so what is
        resolved is the routing the *compiler authorized* and the approval bound -- not whatever
        happens to be configured when the sender runs.
        """

        destination = await self.registry.resolve_destination(
            destination_id=state.view.destination.destination_id,
            registry_version=state.view.destination.registry_version,
            routing_token=state.view.destination.routing_token,
        )
        identity = await self.registry.resolve_sending_identity(self.from_identity_id)
        if identity.identity_id != self.from_identity_id:  # pragma: no cover - registry guard
            raise DestinationRegistryError(SendFailureReason.SENDER_IDENTITY_CHANGED.value)
        return destination, identity

    # -- helpers -------------------------------------------------------------------------------

    def _authorization_request(self, state: _SendState) -> SendAuthorizationRequest:
        return SendAuthorizationRequest(
            namespace=state.approval.namespace,
            community_id=state.approval.community_id,
            case_id=state.approval.case_id,
            action_id=state.approval.action_id,
            execution_id=state.approval.execution_id,
            approval_id=state.approval.approval_id,
            proposal_hash=state.proposal.proposal_hash,
            view_id=state.view.view_id,
            view_hash=state.view.view_hash,
            authorization_version=state.view.authorization_version,
            policy_version=state.view.policy_version,
            compiler_version=state.view.compiler_version,
            policy_build_hash=state.view.policy_build_hash,
            destination_id=state.view.destination.destination_id,
            destination_registry_version=state.view.destination.registry_version,
            routing_token=state.view.destination.routing_token,
            purpose=state.view.purpose,
            authorization_snapshot_hash=state.view.authorization_snapshot_hash,
            requested_at=self.clock.now(),
        )

    async def _release(self, command: SendActionCommand) -> None:
        """Return the fence through the same authority that granted it.

        Through the compiler boundary rather than through Core directly, because the sender
        holds no Core access at all: in a deployed topology both acquisition and release are
        invocations of the compiler's typed operation, and reaching past it here would make
        this code work locally and fail in an account.
        """

        await self.authorization.release(command.scope, command.execution_id)

    def _key(
        self,
        command: SendActionCommand,
        domain: Callable[[str], Sha256Digest],
        execution_key: str,
    ) -> tuple[IdempotencyKey, Sha256Digest]:
        """One of the three ``EXECUTION``-partition send domains, keyed on the **attempt**.

        ``execution_key`` is the execution's own ``idempotency_key`` --
        ``sha256(namespace | action_id | execution_id | proposal_hash | view_hash |
        approval_id)`` -- and **not** the client key the request arrived under. That is what
        makes these records replay-safe regardless of how the worker was invoked or how many
        times: they identify the attempt, not the request that asked for it.

        Keying on the client key would be a real defect rather than a stylistic one. Two
        workers arriving under different ``Idempotency-Key`` values for one execution would
        write two different result records for one attempt, and each would look like proof that
        its own outcome had been persisted (ADR-025 SS 12).
        """

        key = send_key(
            namespace=command.namespace,
            action_id=command.action_id,
            actor_id_hash=command.actor_id_hash,
            key_hash=domain(execution_key),
        )
        request_hash = send_request_hash(
            case_id=command.case_id,
            action_id=command.action_id,
            execution_id=command.execution_id,
            approval_id=command.approval_id,
            expected_execution_version=command.expected_execution_version,
        )
        return key, request_hash

    def _audit_event(
        self,
        command: SendActionCommand,
        *,
        execution: ActionExecution,
        event_type: str,
        reason_codes: tuple[str, ...],
        now: datetime,
        input_hash: Sha256Digest | None,
        output_hash: Sha256Digest | None,
    ) -> AuditEvent:
        """Hashes, identifiers, versions, and closed codes.

        **No audit event on this path carries the subject, either body, a claim, a caveat, a
        recipient address, or a reply-to address.** The digest chain proves which exact message
        was authorized and sent, and the bodies are regenerable from immutable inputs by anyone
        entitled to see them; duplicating the external message text into a table with a
        ninety-day TTL would put the one artifact that leaves the system into a second store for
        no evidentiary gain.
        """

        refs = [
            AuditEntityRef(
                entity_type="ACTION_EXECUTION",
                entity_id=execution.execution_id.value,
                version=execution.version,
            )
        ]
        if event_type == "action.send.started":
            refs.append(
                AuditEntityRef(
                    entity_type="APPROVAL", entity_id=command.approval_id.value, version=None
                )
            )
        return AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            actor_type=ActorType.SYSTEM,
            actor_id_hash=command.actor_id_hash,
            event_type=event_type,
            occurred_at=now,
            correlation_id=command.correlation_id,
            causation_id=None,
            idempotency_key_hash=send_result_key_hash(_require_send_key(execution)),
            entity_refs=tuple(refs),
            decision=AuditDecision.ALLOW if not reason_codes else AuditDecision.DENY,
            reason_codes=reason_codes,
            safe_details=AuditDetails(count=None, rule_id=self.template_version),
            input_hash=input_hash,
            output_hash=output_hash,
        )

    @staticmethod
    def _replayed(execution: ActionExecution, outcome: SendReplayOutcome) -> SendActionResult:
        """Answer from the durable row. **Zero** SES calls, on every branch here."""

        if outcome is SendReplayOutcome.ALREADY_SENT:
            return SendActionResult(
                execution_id=execution.execution_id,
                state=execution.state,
                version=execution.version,
                ses_message_id=execution.ses_message_id,
                failure_code=None,
                reason_codes=(),
                ses_call_made=False,
            )
        if outcome is SendReplayOutcome.IN_PROGRESS:
            return SendActionResult(
                execution_id=execution.execution_id,
                state=execution.state,
                version=execution.version,
                ses_message_id=None,
                failure_code=None,
                reason_codes=(outcome.value,),
                ses_call_made=False,
            )
        # DRAFT, FAILED, and SEND_UNKNOWN are all refusals, and each for its own reason:
        # nothing has been approved, the action is terminal, or the outcome is quarantined and
        # no retry route exists.
        raise SendDeniedError(outcome, execution.state)


__all__ = [
    "CLAIM_PARTICIPANTS",
    "REPLAY_TABLE",
    "RESULT_PARTICIPANTS",
    "SendAction",
    "SendActionCommand",
    "SendActionResult",
    "SendAuthorizationInProgressError",
    "SendDeniedError",
    "SendFailureReason",
    "SendReplayOutcome",
]


def _require_send_key(execution: ActionExecution) -> str:
    """The execution's own send key, which exists from ``APPROVED`` onwards.

    Raising rather than defaulting: a row without one cannot address its own proof records, and
    a fabricated key would let two attempts share a record.
    """

    key = execution.idempotency_key
    if key is None:  # pragma: no cover - required from APPROVED onwards
        raise IntegrityError("ACTION_EXECUTION")
    return key
