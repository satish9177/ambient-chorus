"""One human decision about one immutable proposal, committed as five participants or six.

What this command is
--------------------
It is the only place in the system where a person contributes authority, and it contributes
exactly one bit. The model wrote the words; the human says yes or no to a digest chain that
reaches the bytes; and **there is no field anywhere in the request through which edited text
could enter**. An edit is a rejection followed by a new proposal with a new ``action_id``, a
new ``preview_hash``, and a new decision (ADR-023 SS 7).

The one-decision boundary
-------------------------
It is the execution's row version, and nothing else. Every decision moves the ``DRAFT``
execution out of ``DRAFT`` under ``expected_version``, so ``DRAFT -> APPROVED`` and
``DRAFT -> FAILED`` are guarded compare-and-swaps on the same row: exactly one of any number of
concurrent decisions commits and every other fails with a conflict.

The condition the frozen contract used to name -- ``attribute_not_exists(active approval)`` --
was not a condition at all. ``approval_id`` is minted per request, so an existence check against
a key nobody else will choose is true for every caller, always. The approval row is create-only
under its own identifier, which prevents a decision from being *overwritten*; it is not, and
never was, what prevents a second decision from being *made* (ADR-023 SS 6).

A stale tab is refused three ways over
---------------------------------------
It holds an old ``proposal_hash`` (check 3), an old ``expected_execution_version`` (check 4),
and an ``action_id`` the current pointer no longer names (check 2). The transaction then repeats
checks 2, 4, and 5 as participants, so a tab that wins every read and loses the race commits
nothing.

Rejection re-checks almost nothing, deliberately
------------------------------------------------
Checks 3 to 7 do not apply to a ``REJECTED`` decision. A human must always be able to say no,
and rejecting a proposal that has gone stale is the *correct* response to a proposal that has
gone stale. A reject path that staleness could block would leave a case holding a proposal
nobody can approve and nobody can clear.

A committed decision whose receipt was lost is recovered, not re-decided
------------------------------------------------------------------------
The two idempotency domains this command writes are the transaction's commit proof and the
caller's HTTP receipt, and they can only be written in that order. A crash in between leaves the
decision durable and the receipt unfinished -- and the retry the caller is told to make used to
fall straight through to the checks, find the execution no longer ``DRAFT``, and answer
``EXECUTION_NOT_DRAFT``. A human was told their approval had failed while it sat committed one
row away.

:meth:`ApproveAction._recover` closes that: an ``IN_PROGRESS`` receipt is resolved by *reading*
domain 2's proof before anything is decided again. Present and bound to this request, the
recorded artifacts are verified and replayed; absent, the transaction did not commit and the
checks may safely run again; present under a different request hash, it is a conflict. No branch
creates a second approval and no branch re-attempts the ``DRAFT`` compare-and-swap.

A proof says a transaction committed. It does not say *which rows*
------------------------------------------------------------------
That distinction is the second repair on this path, and the failure it closes was reproduced on
both drivers. The approval commits; the domain-1 completion is lost; the domain-2 proof is then
corrupted so its ``ACTION_EXECUTION`` reference names a **different** execution under the same
action. The identical retry recovered, replayed the proof, loaded the row the proof named, and
answered with the foreign execution -- then finished domain 1 with the foreign reference, making
the receipt permanently describe something the caller never asked about.

Verifying the *requested* execution independently could not have caught it: the requested row
was intact and verified perfectly. What was wrong was the proof's own provenance, so that is
what is checked now. :func:`approval_proof_failures` requires the reference set to be exactly one
approval and one execution and requires the execution reference to be the requested one, before
anything is loaded; :func:`approval_artifact_failures` then requires the loaded artifacts to
agree with each other and with the request, field by field, before domain 1 is finished. Every
disagreement is an ``IntegrityError`` and none of them completes a receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from chorus.application import observability
from chorus.application.errors import StaleAuthorizationError
from chorus.application.services.action_authorization import (
    approval_expires_at,
    approval_key,
    approval_request_hash,
    approval_start_key_hash,
    approval_transaction_key_hash,
    execution_send_key,
)
from chorus.application.services.action_renderer import TEMPLATE_VERSION, render_preview
from chorus.application.services.case_readiness import (
    ReadinessDecision,
    evaluate_invalidation_readiness,
    stage_case_after_invalidation,
)
from chorus.domain.entities import (
    ActionExecution,
    ActionExecutionState,
    ActionProposal,
    ActionProposalStatus,
    ActorType,
    Approval,
    ApprovalDecision,
    ApproverAssurance,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    CaseState,
    CommunityCase,
)
from chorus.domain.errors import (
    DomainError,
    DomainErrorCode,
    IntegrityError,
    ValidationError,
)
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
from chorus.ports.errors import IdempotencyConflictError, PersistenceConflictError
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyFailedFinal,
    IdempotencyInProgress,
    IdempotencyKey,
    IdempotencyRecord,
    IdempotencyReplay,
    IdempotencyStarted,
)
from chorus.ports.records import (
    ActionPointerExpectation,
    CurrentActionPointer,
    StoredSafeDestination,
    StoredShareableView,
)
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    IdempotencyRepositoryPort,
    ShareableRepositoryPort,
)
from chorus.ports.scopes import ActionScope, CaseScope
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork
from chorus.privacy.canonical import hash_action_proposal, hash_approval

APPROVE_TRANSACTION = "approve-action"
REJECT_TRANSACTION = "reject-action"

APPROVAL_RESULT_REFS = 2
"""One approval and one execution. A proof carrying anything else describes another command."""

APPROVE_PARTICIPANTS = 5
"""Shape A. Approval, execution CAS, audit, commit proof, and the case ``ConditionCheck``.

No case *write*: an approval is not a lifecycle transition. No no-live-fence check either -- a
fence can only be held by an execution that has already passed ``APPROVED``, and participant 2's
own condition is that this one has not, so the guard is implied rather than staged.
"""

REJECT_PARTICIPANTS = 6
"""Shape B. Shape A plus the current action pointer moving to ``INVALIDATED``.

The case participant is a guarded ``PutItem`` when readiness remains and a ``CheckItem`` when it
does not, so the **count does not move between the two branches** and one arithmetic assertion
holds for both.
"""


class ApprovalDenial(StrEnum):
    """Why a decision was refused. Closed codes only; never a value, never a hash mismatch pair."""

    CROSS_CASE = "CROSS_CASE_VIOLATION"
    NO_CURRENT_PROPOSAL = "NO_CURRENT_PROPOSAL"
    PROPOSAL_NOT_CURRENT = "PROPOSAL_NOT_CURRENT"
    POINTER_NOT_DRAFT = "POINTER_NOT_DRAFT"
    PROPOSAL_HASH_MISMATCH = "PROPOSAL_HASH_MISMATCH"
    PREVIEW_HASH_MISMATCH = "PREVIEW_HASH_MISMATCH"
    VIEW_HASH_MISMATCH = "VIEW_HASH_MISMATCH"
    EXECUTION_NOT_DRAFT = "EXECUTION_NOT_DRAFT"
    EXECUTION_NOT_CURRENT = "EXECUTION_NOT_CURRENT"
    EXECUTION_VERSION_MISMATCH = "EXECUTION_VERSION_MISMATCH"
    CASE_NOT_ACTION_PROPOSED = "CASE_NOT_ACTION_PROPOSED"
    STALE_AUTHORIZATION = "STALE_AUTHORIZATION"
    VIEW_EXPIRED = "VIEW_EXPIRED"
    DEPLOYMENT_CONFIGURATION_MOVED = "DEPLOYMENT_CONFIGURATION_MOVED"
    PREVIEW_BINDING_MOVED = "PREVIEW_BINDING_MOVED"
    """The preview regenerated under current configuration no longer matches the approved one.

    Distinct from ``DEPLOYMENT_CONFIGURATION_MOVED``, which compares the stored *view* against
    the deployment. Two of the values ADR-023 SS 3 binds -- ``from_identity_id`` and
    ``template_version`` -- are on no stored row at all; they live inside ``preview_hash`` and
    are only reachable by regenerating the preview and comparing the digest.
    """


class ApprovalDeniedError(DomainError):
    """A decision refused before anything was staged, under one closed code."""

    __slots__ = ("denial",)

    def __init__(self, denial: ApprovalDenial) -> None:
        super().__init__(DomainErrorCode.VALIDATION_ERROR, denial.value)
        self.denial = denial

    @property
    def safe_code(self) -> str:
        return self.denial.value


class ApprovalRecoveryFailure(StrEnum):
    """Every way a durable proof can fail to be *this* request's, under closed codes.

    Reported as a set rather than as the first hit: a proof that disagrees in several places is
    a storage or routing defect, and an operator wants the whole disagreement rather than
    whichever check happened to run first.
    """

    RESULT_REFS_MISMATCH = "RESULT_REFS_MISMATCH"
    """Not exactly one ``APPROVAL`` reference and one ``ACTION_EXECUTION`` reference."""

    REQUEST_HASH_MISMATCH = "REQUEST_HASH_MISMATCH"
    """The proof was written for a different request under this key."""

    EXECUTION_MISMATCH = "EXECUTION_MISMATCH"
    """The proof names an execution that is not the one this request asked about."""

    APPROVAL_MISMATCH = "APPROVAL_MISMATCH"
    """The replayed approval is not the one the proof names."""

    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    """A foreign namespace, community, or case reached the reference set."""

    ACTION_MISMATCH = "ACTION_MISMATCH"
    """A foreign action -- the proposal partition itself disagrees."""

    DECISION_MISMATCH = "DECISION_MISMATCH"
    """The recorded decision is not the decision being retried."""

    REQUEST_KEY_MISMATCH = "REQUEST_KEY_MISMATCH"
    """The approval was made under a different client key."""

    APPROVER_MISMATCH = "APPROVER_MISMATCH"
    """The approval was made by a different person than the one retrying."""

    APPROVAL_HASH_MISMATCH = "APPROVAL_HASH_MISMATCH"
    """The approval no longer verifies against its own recomputed digest."""

    PROPOSAL_BINDING_MISMATCH = "PROPOSAL_BINDING_MISMATCH"
    """The approval's ``proposal_hash`` / ``view_hash`` disagree with the stored proposal."""

    AUTHORIZATION_VERSION_MISMATCH = "AUTHORIZATION_VERSION_MISMATCH"
    """The approval records a disclosure epoch the proposal it names never had."""

    REQUESTED_ARTIFACT_MISMATCH = "REQUESTED_ARTIFACT_MISMATCH"
    """An *approval* whose bound artifacts are not the ones this request approved.

    Checked for ``APPROVED`` only. A rejection is allowed to be stale by design -- checks 3 and
    5 never run for one -- so requiring its digests to equal the caller's would refuse a
    recovery the original decision was entitled to make.
    """

    EXECUTION_BINDING_MISMATCH = "EXECUTION_BINDING_MISMATCH"
    """The execution does not name the approval the proof replayed."""

    EXECUTION_VERSION_MISMATCH = "EXECUTION_VERSION_MISMATCH"
    """The proof records a version this request's decision could not have produced."""


def approval_proof_failures(
    command: ApproveActionCommand, record: IdempotencyRecord, request_hash: Sha256Digest
) -> tuple[str, ...]:
    """Every way this durable record fails to be a proof about the requested approval.

    Pure, and it runs **before any row is loaded**, because the first thing a corrupted proof
    can do is send the reader to a row that has nothing to do with the request. Checking the
    reference set afterwards would mean the foreign row had already been read and returned.
    """

    failures: list[str] = []
    if record.request_hash != request_hash:
        failures.append(ApprovalRecoveryFailure.REQUEST_HASH_MISMATCH.value)
    approval_refs = [ref for ref in record.result_entity_refs if ref.entity_type == "APPROVAL"]
    execution_refs = [
        ref for ref in record.result_entity_refs if ref.entity_type == "ACTION_EXECUTION"
    ]
    if (
        len(record.result_entity_refs) != APPROVAL_RESULT_REFS
        or len(approval_refs) != 1
        or len(execution_refs) != 1
    ):
        failures.append(ApprovalRecoveryFailure.RESULT_REFS_MISMATCH.value)
        return tuple(failures)
    if execution_refs[0].entity_id != command.execution_id.value:
        # The finding this predicate exists for: a proof under the right key, for the right
        # request hash, naming another execution in the same action partition.
        failures.append(ApprovalRecoveryFailure.EXECUTION_MISMATCH.value)
    return tuple(failures)


def approval_artifact_failures(
    command: ApproveActionCommand, recovered: RecoveredApproval
) -> tuple[str, ...]:
    """Every way the artifacts a proof named fail to describe the approval this request made.

    The comparison that matters is between *the proof's own artifacts* and *the request*, never
    between the requested row and itself. A recovery that loaded ``command.execution_id``,
    verified it, and answered would pass while returning a completely different execution --
    which is exactly what happened.
    """

    approval = recovered.approval
    proposal = recovered.proposal
    execution = recovered.execution
    result = recovered.result
    failures: list[str] = []

    if (
        approval.approval_id != result.approval_id
        or approval.approval_id.value != recovered.approval_ref.entity_id
    ):
        failures.append(ApprovalRecoveryFailure.APPROVAL_MISMATCH.value)
    if (
        approval.execution_id != command.execution_id
        or result.execution_id != command.execution_id
        or execution.execution_id != command.execution_id
        or recovered.execution_ref.entity_id != command.execution_id.value
    ):
        failures.append(ApprovalRecoveryFailure.EXECUTION_MISMATCH.value)
    if (
        approval.namespace != command.namespace
        or approval.community_id != command.community_id
        or approval.case_id != command.case_id
        or proposal.case_id != command.case_id
    ):
        failures.append(ApprovalRecoveryFailure.SCOPE_MISMATCH.value)
    if approval.action_id != command.action_id or proposal.action_id != command.action_id:
        failures.append(ApprovalRecoveryFailure.ACTION_MISMATCH.value)
    if approval.decision is not command.decision or result.decision is not command.decision:
        failures.append(ApprovalRecoveryFailure.DECISION_MISMATCH.value)
    if approval.request_key_hash != approval_start_key_hash(command.idempotency_key):
        failures.append(ApprovalRecoveryFailure.REQUEST_KEY_MISMATCH.value)
    if approval.approver_id_hash != command.approver_id_hash:
        failures.append(ApprovalRecoveryFailure.APPROVER_MISMATCH.value)
    if hash_approval(approval) != approval.approval_hash:
        failures.append(ApprovalRecoveryFailure.APPROVAL_HASH_MISMATCH.value)
    if approval.proposal_hash != proposal.proposal_hash or approval.view_hash != proposal.view_hash:
        failures.append(ApprovalRecoveryFailure.PROPOSAL_BINDING_MISMATCH.value)
    if approval.authorization_version != proposal.authorization_version:
        failures.append(ApprovalRecoveryFailure.AUTHORIZATION_VERSION_MISMATCH.value)
    if command.decision is ApprovalDecision.APPROVED and (
        approval.proposal_hash != command.proposal_hash or approval.view_hash != command.view_hash
    ):
        failures.append(ApprovalRecoveryFailure.REQUESTED_ARTIFACT_MISMATCH.value)
    if (
        command.decision is ApprovalDecision.APPROVED
        and execution.approval_id != approval.approval_id
    ):
        failures.append(ApprovalRecoveryFailure.EXECUTION_BINDING_MISMATCH.value)
    if result.execution_version != execution.version or (
        recovered.execution_ref.version != command.expected_execution_version + 1
    ):
        # The proof's own version is compared against the *request*, never against the live
        # row: a decision moves ``DRAFT@n`` to ``n + 1`` exactly, and the row is free to have
        # advanced further before the retry arrives. Comparing to the row would refuse a
        # perfectly good recovery for the crime of a send having started meanwhile.
        failures.append(ApprovalRecoveryFailure.EXECUTION_VERSION_MISMATCH.value)
    return tuple(failures)


@dataclass(frozen=True, slots=True, kw_only=True)
class ApproveActionCommand:
    """The frozen approval body, plus the identity the transport resolved.

    Six caller-chosen values and not one more. There is no subject, no body, no claim, no
    caveat, no recipient, no template, and no ``expected_action_status`` -- the last retired
    because the transaction conditions on a row version and two ways to say "the thing I saw"
    is one too many (ADR-023 SS Consequences).
    """

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    decision: ApprovalDecision
    expected_execution_version: int
    execution_id: ExecutionId
    view_hash: Sha256Digest
    proposal_hash: Sha256Digest
    preview_hash: Sha256Digest
    approver_id_hash: Sha256Digest
    approver_assurance: ApproverAssurance
    correlation_id: UUID
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.expected_execution_version < 1:
            raise ValueError("expected_execution_version must be positive")
        if not self.idempotency_key:
            raise ValueError("an approval names the key it was made under")

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
class ApproveActionResult:
    """Identifiers, hashes, versions, and states. Never a body and never an address."""

    approval_id: ApprovalId
    approval_hash: Sha256Digest
    decision: ApprovalDecision
    expires_at: datetime
    action_id: ActionId
    execution_id: ExecutionId
    execution_state: ActionExecutionState
    execution_version: int
    pointer_status: ActionProposalStatus
    case_state: CaseState
    case_version: int
    authorization_version: int
    replayed: bool

    @property
    def result_refs(self) -> tuple[EntityRef, ...]:
        return (
            EntityRef(entity_type="APPROVAL", entity_id=self.approval_id.value),
            EntityRef(
                entity_type="ACTION_EXECUTION",
                entity_id=self.execution_id.value,
                version=self.execution_version,
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveredApproval:
    """The durable artifacts a recovery read back, gathered so one predicate can judge them.

    All four are loaded from what the **proof** names -- not from what the request asked for --
    which is the only arrangement in which "the proof describes another execution" is a
    detectable statement.
    """

    approval_ref: EntityRef
    execution_ref: EntityRef
    approval: Approval
    proposal: ActionProposal
    execution: ActionExecution
    result: ApproveActionResult


@dataclass(frozen=True, slots=True, kw_only=True)
class _DecisionState:
    """Everything one decision strongly read before anything was staged."""

    case: CommunityCase
    proposal: ActionProposal
    pointer: CurrentActionPointer
    execution: ActionExecution
    view: StoredShareableView | None
    """``None`` only for a rejection, which never loads a view -- see the module docstring."""


@dataclass(slots=True)
class ApproveAction:
    """Record one immutable human decision and move the ``DRAFT`` execution out of ``DRAFT``."""

    core: CoreRepositoryPort
    shareable: ShareableRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    clock: Clock
    ids: IdGenerator
    destination: StoredSafeDestination
    from_identity_id: str
    policy_version: str
    compiler_version: str
    policy_build_hash: Sha256Digest
    template_version: str = TEMPLATE_VERSION

    async def execute(self, command: ApproveActionCommand) -> ApproveActionResult:
        """Claim the request key, prove the decision, commit it, and complete the key.

        Domain 1 -- the HTTP request record -- is claimed *before* anything is proved, so a
        replay under the same key and the same request hash answers from the record rather than
        re-deciding. A **different** decision under a different key is a conflict, not a
        correction: a second command must not discard a human's decision without a human
        deciding to.
        """

        request_hash = approval_request_hash(
            case_id=command.case_id,
            action_id=command.action_id,
            execution_id=command.execution_id,
            decision=command.decision,
            expected_execution_version=command.expected_execution_version,
            view_hash=command.view_hash,
            proposal_hash=command.proposal_hash,
            preview_hash=command.preview_hash,
        )
        start_key = approval_key(
            namespace=command.namespace,
            action_id=command.action_id,
            actor_id_hash=command.approver_id_hash,
            key_hash=approval_start_key_hash(command.idempotency_key),
        )
        outcome = await self.idempotency.begin(
            start_key, request_hash=request_hash, now=self.clock.now()
        )
        match outcome:
            case IdempotencyReplay(record=record):
                return await self._replay(command, record)
            case IdempotencyFailedFinal():
                raise PersistenceConflictError("APPROVAL")
            case IdempotencyStarted(record=record):
                reservation = record
            case IdempotencyInProgress(record=record):
                reservation = record
                # This key's own unfinished attempt. Before deciding again, find out whether
                # the decision already happened.
                recovered = await self._recover(command, reservation)
                if recovered is not None:
                    return recovered
            case _:  # pragma: no cover - the outcome union is closed
                raise AssertionError("unreachable idempotency outcome")

        result = await self._decide(command)
        await self._complete_start(reservation, result)
        return result

    async def _recover(
        self, command: ApproveActionCommand, reservation: IdempotencyRecord
    ) -> ApproveActionResult | None:
        """Answer from domain 2's commit proof when the decision committed and the receipt did not.

        The failure this exists for is narrow and it is real. The approval transaction commits;
        the ``approve-action-complete`` write that finishes domain 1 does not. Domain 1 is left
        ``IN_PROGRESS``, and an identical retry -- the retry the caller is *told* to make --
        used to fall straight through to the seven checks, find the execution no longer
        ``DRAFT``, and answer ``EXECUTION_NOT_DRAFT``. A human was told their approval had
        failed while it sat committed one row away, and the case was left holding an ``APPROVED``
        execution nobody believed in.

        Recovery is a **read**, and it decides nothing:

        * no domain-2 record -- the transaction did not commit, and the seven checks may run
          again against a ``DRAFT@1`` execution, which is the frozen safe retry;
        * a domain-2 record bound to a different request hash -- a different decision under one
          key, which is a conflict and never a correction;
        * a domain-2 record for this request -- read back the artifacts it names, verify them
          against this request, finish domain 1, and answer what actually committed.

        Nothing here creates an approval, and nothing here re-attempts the ``DRAFT``
        compare-and-swap. Both would be a second decision, and there was only ever one.
        """

        key, request_hash = self._transaction_key(command)
        record = await self.idempotency.load(key)
        if record is None:
            return None
        if record.request_hash != request_hash:
            # The same client key carrying a different decision. Answering with the recorded
            # one would silently convert a human's "no" into a "yes", or the reverse.
            raise IdempotencyConflictError("APPROVAL")
        self._require_bound_proof(command, record, request_hash)
        result = await self._replay(command, record)
        await self._verify_recovered(command, record, result)
        await self._complete_start(reservation, result)
        return result

    def _require_bound_proof(
        self,
        command: ApproveActionCommand,
        record: IdempotencyRecord,
        request_hash: Sha256Digest,
    ) -> None:
        """Refuse a proof whose reference set is not about this request, before reading a row."""

        self._fail_closed(command, approval_proof_failures(command, record, request_hash))

    async def _verify_recovered(
        self,
        command: ApproveActionCommand,
        record: IdempotencyRecord,
        result: ApproveActionResult,
    ) -> None:
        """Require the artifacts a recovery replays to be the ones this request asked for.

        A commit proof says a transaction committed. It does not say the rows it names are the
        rows this caller asked about, nor that they are still intact. So recovery re-reads all
        of them -- the approval the proof names, the immutable proposal it binds, and the
        execution it moved -- re-derives the approval's own digest, and requires the whole set
        to agree with each other and with the request.

        The load that matters is ``result.execution_id``: the execution the **proof** replayed,
        not the one the command asked for. Loading the requested row and verifying it in
        isolation is what let a corrupted proof answer with a foreign execution while every
        individual check passed.
        """

        approval_ref, execution_ref = _proof_refs(record)
        approval = await self.shareable.load_approval(command.action_scope, result.approval_id)
        proposal = await self.shareable.load_proposal(command.action_scope)
        execution = await self.shareable.load_execution(command.action_scope, result.execution_id)
        self._fail_closed(
            command,
            approval_artifact_failures(
                command,
                RecoveredApproval(
                    approval_ref=approval_ref,
                    execution_ref=execution_ref,
                    approval=approval,
                    proposal=proposal,
                    execution=execution,
                    result=result,
                ),
            ),
        )

    def _fail_closed(self, command: ApproveActionCommand, failures: tuple[str, ...]) -> None:
        """Report the whole disagreement, then refuse. Never completes a receipt."""

        if not failures:
            return
        observability.approval_conflict(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            actor_id_hash=command.approver_id_hash,
            reason_codes=failures,
        )
        raise IntegrityError("APPROVAL")

    async def _decide(self, command: ApproveActionCommand) -> ApproveActionResult:
        now = self.clock.now()
        try:
            state = await self._load_and_prove(command, now=now)
        except ApprovalDeniedError as error:
            observability.approval_conflict(
                namespace=command.namespace,
                community_id=command.community_id,
                case_id=command.case_id,
                correlation_id=command.correlation_id,
                actor_id_hash=command.approver_id_hash,
                reason_codes=(error.denial.value,),
            )
            raise
        if command.decision is ApprovalDecision.APPROVED:
            return await self._commit_approval(command, state, now=now)
        return await self._commit_rejection(command, state, now=now)

    # -- the seven checks ------------------------------------------------------------------

    async def _load_and_prove(
        self, command: ApproveActionCommand, *, now: datetime
    ) -> _DecisionState:
        """The frozen approval-time checks, in the frozen order (ADR-023 SS 5).

        Checks 1, 2, and 4 run for every decision. Checks 3, 5, 6, and 7 run only for an
        approval, because a rejection must never be blocked by staleness.

        Note what is **deliberately absent**: the case's OCC ``version`` is never compared
        against the proposal's recorded ``case_version``. Lifecycle progression moved that
        number on purpose, and requiring it here would reproduce at approval time the deadlock
        ADR-020 removed from send time.
        """

        # 1. scope. The action partition is addressed by action_id alone, so the loaded
        #    proposal's own case is what proves this action belongs to this case.
        proposal = await self.shareable.load_proposal(command.action_scope)
        if proposal.case_id != command.case_id or proposal.action_id != command.action_id:
            raise ApprovalDeniedError(ApprovalDenial.CROSS_CASE)

        # 2. the current pointer names this action, at DRAFT, with this proposal hash.
        pointer = await self.shareable.load_current_action_pointer(command.scope)
        if pointer is None:
            raise ApprovalDeniedError(ApprovalDenial.NO_CURRENT_PROPOSAL)
        if pointer.action_id != command.action_id:
            raise ApprovalDeniedError(ApprovalDenial.PROPOSAL_NOT_CURRENT)
        if pointer.status is not ActionProposalStatus.DRAFT:
            raise ApprovalDeniedError(ApprovalDenial.POINTER_NOT_DRAFT)
        if pointer.proposal_hash != command.proposal_hash:
            raise ApprovalDeniedError(ApprovalDenial.PROPOSAL_HASH_MISMATCH)

        # 4. the execution is this pointer's, in DRAFT, at the expected version.
        if pointer.execution_id != command.execution_id:
            raise ApprovalDeniedError(ApprovalDenial.EXECUTION_NOT_CURRENT)
        execution = await self.shareable.load_execution(command.action_scope, command.execution_id)
        if execution.state is not ActionExecutionState.DRAFT:
            raise ApprovalDeniedError(ApprovalDenial.EXECUTION_NOT_DRAFT)
        if execution.version != command.expected_execution_version:
            raise ApprovalDeniedError(ApprovalDenial.EXECUTION_VERSION_MISMATCH)

        case = await self.core.load_case(command.scope)
        if command.decision is ApprovalDecision.REJECTED:
            return _DecisionState(
                case=case, proposal=proposal, pointer=pointer, execution=execution, view=None
            )

        # 3. the proposal recomputes, and its preview digest is the one the human saw.
        if proposal.proposal_hash != command.proposal_hash:
            raise ApprovalDeniedError(ApprovalDenial.PROPOSAL_HASH_MISMATCH)
        if hash_action_proposal(proposal) != proposal.proposal_hash:
            # The stored artifact does not hash to what its own field claims. An integrity
            # failure rather than a policy answer, and it fails closed.
            raise IntegrityError("ACTION_PROPOSAL")
        if proposal.preview_hash != command.preview_hash:
            raise ApprovalDeniedError(ApprovalDenial.PREVIEW_HASH_MISMATCH)
        if proposal.view_hash != command.view_hash:
            raise ApprovalDeniedError(ApprovalDenial.VIEW_HASH_MISMATCH)

        # 5. the case is ACTION_PROPOSED at the proposal's epoch.
        if case.state is not CaseState.ACTION_PROPOSED:
            raise ApprovalDeniedError(ApprovalDenial.CASE_NOT_ACTION_PROPOSED)
        if case.authorization_version != proposal.authorization_version:
            raise ApprovalDeniedError(ApprovalDenial.STALE_AUTHORIZATION)

        # 6. the bound view loads, agrees with the body and the proposal, and is unexpired.
        view = await self.shareable.load_view(command.scope, proposal.view_id)
        if view.view_hash != proposal.view_hash or view.view_hash != command.view_hash:
            raise ApprovalDeniedError(ApprovalDenial.VIEW_HASH_MISMATCH)
        if view.authorization_version != proposal.authorization_version:
            raise ApprovalDeniedError(ApprovalDenial.STALE_AUTHORIZATION)
        # Equality at expiry means expired, as it does everywhere else.
        if now >= view.expires_at:
            raise ApprovalDeniedError(ApprovalDenial.VIEW_EXPIRED)

        # 7. deployment configuration, by exact equality.
        self._require_current_configuration(view)

        # 7b. regenerate the preview under *current* configuration and require the approved
        #     binding exactly. This is how `from_identity_id` and `template_version` are
        #     checked: neither is stored on the view, on the pointer, or anywhere else check 7
        #     can reach. ADR-023 SS 3 binds both -- transitively, through `preview_hash` -- and
        #     a transitive binding is verified by *recomputation*, never by comparing fields
        #     copied off the proposal, which would be the proposal agreeing with itself.
        #
        #     Regenerating here rather than leaving it to the sender is the whole point of
        #     ADR-023 SS 5's "refusing at approval is strictly kinder": a human who approves a
        #     letterhead the deployment no longer has is a human whose decision the send fence
        #     will discard.
        self._require_current_preview_binding(proposal, view)
        return _DecisionState(
            case=case, proposal=proposal, pointer=pointer, execution=execution, view=view
        )

    def _require_current_preview_binding(
        self, proposal: ActionProposal, view: StoredShareableView
    ) -> None:
        """Render the approved proposal again, now, and require the same digest.

        The renderer is deterministic over ``{proposal, view, template_version,
        from_identity_id}``, so this comparison fires exactly when one of those four has moved
        since the proposal was sealed -- which is the same comparison ADR-025 SS 3 step 4 makes
        at send time, run at the moment a human is being asked to commit.
        """

        try:
            preview = render_preview(
                proposal,
                view,
                from_identity_id=self.from_identity_id,
                template_version=self.template_version,
            )
        except ValidationError:
            # A deployment whose template version the renderer no longer recognises cannot
            # reproduce the approved preview at all, which is the same answer as reproducing a
            # different one -- and it is an answer rather than a crash the approver has to
            # interpret.
            raise ApprovalDeniedError(ApprovalDenial.PREVIEW_BINDING_MOVED) from None
        if preview.preview_hash != proposal.preview_hash:
            raise ApprovalDeniedError(ApprovalDenial.PREVIEW_BINDING_MOVED)

    def _require_current_configuration(self, view: StoredShareableView) -> None:
        """Refuse an approval the send fence would refuse anyway.

        These values are deployment-owned rather than case-owned, so they are outside
        ``authorization_version`` (ADR-020 SS 3) and a verified ``proposal_hash`` proves only
        that the old artifact is internally coherent. Refusing here is strictly kinder than
        letting a human approve a message the fence will decline.
        """

        current = self.destination
        moved = (
            view.policy_version != self.policy_version
            or view.compiler_version != self.compiler_version
            or view.policy_build_hash != self.policy_build_hash
            or view.destination.destination_id != current.destination_id
            or view.destination.kind is not current.kind
            or view.destination.registry_version != current.registry_version
            or view.destination.routing_token != current.routing_token
            or view.destination.display_label != current.display_label
        )
        if moved:
            raise ApprovalDeniedError(ApprovalDenial.DEPLOYMENT_CONFIGURATION_MOVED)

    # -- shape A ---------------------------------------------------------------------------

    async def _commit_approval(
        self, command: ApproveActionCommand, state: _DecisionState, *, now: datetime
    ) -> ApproveActionResult:
        """Five participants: approval, execution CAS, audit, commit proof, case check."""

        assert state.view is not None  # check 6 ran for every APPROVED decision
        approval = self._approval(command, state, view=state.view, now=now)
        send_key = execution_send_key(
            namespace=command.namespace,
            action_id=command.action_id,
            execution_id=command.execution_id,
            proposal_hash=state.proposal.proposal_hash,
            view_hash=state.proposal.view_hash,
            approval_id=approval.approval_id,
        )
        approved = transition_action_execution(
            state.execution,
            ActionExecutionState.APPROVED,
            expected_version=state.execution.version,
            now=now,
            approval_id=approval.approval_id,
            idempotency_key=send_key,
        )
        key, request_hash = self._transaction_key(command)
        operations = (
            self.shareable.stage_append_approval(command.action_scope, approval),
            self.shareable.stage_update_execution(
                command.action_scope, approved, expected_version=state.execution.version
            ),
            self.audit.stage_append_case_event(
                command.scope,
                self._audit_event(
                    command,
                    approval=approval,
                    event_type="action.approved",
                    reason_codes=(),
                    now=now,
                ),
            ),
            self.idempotency.stage_create_completed(
                key,
                request_hash=request_hash,
                result_entity_refs=(
                    EntityRef(entity_type="APPROVAL", entity_id=approval.approval_id.value),
                    EntityRef(
                        entity_type="ACTION_EXECUTION",
                        entity_id=approved.execution_id.value,
                        version=approved.version,
                    ),
                ),
                response_status=200,
                now=now,
            ),
            # The case is a read-only condition and appears in no other form. An approval is
            # not a lifecycle transition, so it moves neither counter.
            self.core.stage_require_case_version(
                command.scope,
                expected_version=state.case.version,
                expected_authorization_version=state.case.authorization_version,
                expected_state=CaseState.ACTION_PROPOSED,
            ),
        )
        await self._commit(
            command, APPROVE_TRANSACTION, operations, APPROVE_PARTICIPANTS, key, request_hash
        )
        observability.approval_recorded(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            actor_id_hash=command.approver_id_hash,
            execution_id=approved.execution_id.value,
            decision=ApprovalDecision.APPROVED.value,
            proposal_hash=state.proposal.proposal_hash,
            view_hash=state.proposal.view_hash,
            preview_hash=state.proposal.preview_hash,
        )
        return ApproveActionResult(
            approval_id=approval.approval_id,
            approval_hash=approval.approval_hash,
            decision=ApprovalDecision.APPROVED,
            expires_at=approval.expires_at,
            action_id=command.action_id,
            execution_id=approved.execution_id,
            execution_state=approved.state,
            execution_version=approved.version,
            pointer_status=state.pointer.status,
            case_state=state.case.state,
            case_version=state.case.version,
            authorization_version=state.case.authorization_version,
            replayed=False,
        )

    # -- shape B ---------------------------------------------------------------------------

    async def _commit_rejection(
        self, command: ApproveActionCommand, state: _DecisionState, *, now: datetime
    ) -> ApproveActionResult:
        """Six participants: shape A plus the pointer moving to ``INVALIDATED``.

        The rejection binds the proposal and view hashes the *proposal* carries rather than the
        ones the caller sent, because a rejection is allowed to be stale and the durable record
        of what was rejected must describe the artifact that actually exists.
        """

        approval = self._approval(command, state, view=None, now=now)
        failed = transition_action_execution(
            state.execution,
            ActionExecutionState.FAILED,
            expected_version=state.execution.version,
            now=now,
            finished_at=now,
            failure_code="PROPOSAL_REJECTED",
        )
        pointer = _invalidated(state.pointer, now=now)
        readiness = await self._readiness(command, state, now=now)
        key, request_hash = self._transaction_key(command)
        operations = (
            self.shareable.stage_append_approval(command.action_scope, approval),
            self.shareable.stage_update_execution(
                command.action_scope, failed, expected_version=state.execution.version
            ),
            self.audit.stage_append_case_event(
                command.scope,
                self._audit_event(
                    command,
                    approval=approval,
                    event_type="action.rejected",
                    reason_codes=("PROPOSAL_REJECTED",),
                    now=now,
                ),
            ),
            self.idempotency.stage_create_completed(
                key,
                request_hash=request_hash,
                result_entity_refs=(
                    EntityRef(entity_type="APPROVAL", entity_id=approval.approval_id.value),
                    EntityRef(
                        entity_type="ACTION_EXECUTION",
                        entity_id=failed.execution_id.value,
                        version=failed.version,
                    ),
                ),
                response_status=200,
                now=now,
            ),
            self.shareable.stage_replace_current_action_pointer(
                command.scope,
                pointer,
                expected=ActionPointerExpectation(
                    row_version=state.pointer.version,
                    proposal_hash=state.pointer.proposal_hash,
                ),
            ),
            stage_case_after_invalidation(
                core=self.core, scope=command.scope, case=state.case, readiness=readiness, now=now
            ),
        )
        await self._commit(
            command, REJECT_TRANSACTION, operations, REJECT_PARTICIPANTS, key, request_hash
        )
        observability.approval_recorded(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            actor_id_hash=command.approver_id_hash,
            execution_id=failed.execution_id.value,
            decision=ApprovalDecision.REJECTED.value,
            proposal_hash=state.proposal.proposal_hash,
            view_hash=state.proposal.view_hash,
            preview_hash=state.proposal.preview_hash,
        )
        return ApproveActionResult(
            approval_id=approval.approval_id,
            approval_hash=approval.approval_hash,
            decision=ApprovalDecision.REJECTED,
            expires_at=approval.expires_at,
            action_id=command.action_id,
            execution_id=failed.execution_id,
            execution_state=failed.state,
            execution_version=failed.version,
            pointer_status=pointer.status,
            case_state=readiness.next_case.state,
            case_version=readiness.next_case.version,
            authorization_version=readiness.next_case.authorization_version,
            replayed=False,
        )

    async def _readiness(
        self, command: ApproveActionCommand, state: _DecisionState, *, now: datetime
    ) -> ReadinessDecision:
        """Decide the case branch against the *same* instant the artifacts were sealed at.

        One clock sample per command, threaded rather than re-read: a second read here could
        put the view's expiry on one side of the boundary while the approval it is bound to
        sits on the other, and the two would then disagree about one moment.
        """

        return await evaluate_invalidation_readiness(
            shareable=self.shareable, scope=command.scope, case=state.case, now=now
        )

    # -- shared -----------------------------------------------------------------------------

    def _approval(
        self,
        command: ApproveActionCommand,
        state: _DecisionState,
        *,
        view: StoredShareableView | None,
        now: datetime,
    ) -> Approval:
        """Build the immutable decision and seal it with its own digest.

        ``expires_at`` is ``min(approved_at + 15 minutes, view.expires_at)`` so an approval
        never outlives the disclosure authority it was made against. A rejection has no view
        loaded -- it is allowed to be stale -- so its expiry is the approval lifetime alone;
        nothing consults the expiry of a decision that authorizes nothing.
        """

        draft = Approval(
            approval_id=self.ids.new(ApprovalId),
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            action_id=command.action_id,
            execution_id=command.execution_id,
            proposal_hash=state.proposal.proposal_hash,
            view_hash=state.proposal.view_hash,
            authorization_version=state.proposal.authorization_version,
            approver_id_hash=command.approver_id_hash,
            approver_assurance=command.approver_assurance,
            decision=command.decision,
            approved_at=now,
            expires_at=(
                approval_expires_at(approved_at=now, view_expires_at=view.expires_at)
                if view is not None
                else approval_expires_at(approved_at=now, view_expires_at=now + _REJECTION_WINDOW)
            ),
            approval_hash=_PLACEHOLDER_HASH,
            request_key_hash=approval_start_key_hash(command.idempotency_key),
            version=1,
            created_at=now,
            updated_at=now,
        )
        return replace(draft, approval_hash=hash_approval(draft))

    def _transaction_key(
        self, command: ApproveActionCommand
    ) -> tuple[IdempotencyKey, Sha256Digest]:
        """Domain 2: the transaction's own commit proof, under its own domain-separated hash."""

        key = approval_key(
            namespace=command.namespace,
            action_id=command.action_id,
            actor_id_hash=command.approver_id_hash,
            key_hash=approval_transaction_key_hash(command.idempotency_key),
        )
        request_hash = approval_request_hash(
            case_id=command.case_id,
            action_id=command.action_id,
            execution_id=command.execution_id,
            decision=command.decision,
            expected_execution_version=command.expected_execution_version,
            view_hash=command.view_hash,
            proposal_hash=command.proposal_hash,
            preview_hash=command.preview_hash,
        )
        return key, request_hash

    async def _commit(
        self,
        command: ApproveActionCommand,
        name: str,
        operations: tuple[object, ...],
        expected: int,
        key: IdempotencyKey,
        request_hash: Sha256Digest,
    ) -> None:
        if len(operations) != expected:  # pragma: no cover - arithmetic guard
            raise IntegrityError("APPROVAL")
        try:
            await self.unit_of_work.commit(
                TransactionPlan(
                    name=name,
                    operations=operations,  # type: ignore[arg-type]
                    audit_required=True,
                    commit_proof=self.idempotency.commit_proof(key, request_hash=request_hash),
                )
            )
        except PersistenceConflictError:
            # Something moved between the reads and the write: another decision won the
            # execution's compare-and-swap, the pointer was replaced, or the case advanced.
            # Nothing was persisted, and a losing decision is a conflict rather than a
            # correction.
            observability.approval_conflict(
                namespace=command.namespace,
                community_id=command.community_id,
                case_id=command.case_id,
                correlation_id=command.correlation_id,
                actor_id_hash=command.approver_id_hash,
                reason_codes=(ApprovalDenial.EXECUTION_VERSION_MISMATCH.value,),
            )
            raise

    def _audit_event(
        self,
        command: ApproveActionCommand,
        *,
        approval: Approval,
        event_type: str,
        reason_codes: tuple[str, ...],
        now: datetime,
    ) -> AuditEvent:
        """Identifiers, versions, digests, and closed codes. Never prose and never a body.

        ``actor_type`` is ``HUMAN`` here and only here on this path: this is the one event in
        Phase 8 that records a person's decision rather than a process's action.
        """

        return AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            actor_type=ActorType.HUMAN,
            actor_id_hash=command.approver_id_hash,
            event_type=event_type,
            occurred_at=now,
            correlation_id=command.correlation_id,
            causation_id=None,
            idempotency_key_hash=approval.request_key_hash,
            entity_refs=(
                AuditEntityRef(
                    entity_type="APPROVAL", entity_id=approval.approval_id.value, version=None
                ),
                AuditEntityRef(
                    entity_type="ACTION_EXECUTION",
                    entity_id=approval.execution_id.value,
                    version=None,
                ),
                AuditEntityRef(
                    entity_type="ACTION_PROPOSAL", entity_id=command.action_id.value, version=None
                ),
            ),
            decision=(
                AuditDecision.ALLOW
                if command.decision is ApprovalDecision.APPROVED
                else AuditDecision.DENY
            ),
            reason_codes=reason_codes,
            safe_details=AuditDetails(count=None, rule_id=self.template_version),
            input_hash=approval.proposal_hash,
            output_hash=approval.approval_hash,
        )

    # -- replay ------------------------------------------------------------------------------

    async def _replay(
        self, command: ApproveActionCommand, record: IdempotencyRecord
    ) -> ApproveActionResult:
        """Answer from the durable record. No second decision and no mutation.

        The answer is read back from the persisted artifacts rather than reconstructed, because
        a replay has to describe what actually committed.
        """

        approval_ref, execution_ref = _proof_refs(record)
        if execution_ref.entity_id != command.execution_id.value:
            # A record under this key naming another execution is a corrupted proof, on either
            # domain, and reading the row it names would answer about the wrong approval.
            raise IntegrityError("APPROVAL")
        approval = await self.shareable.load_approval(
            command.action_scope, ApprovalId(approval_ref.entity_id)
        )
        execution = await self.shareable.load_execution(
            command.action_scope, ExecutionId(execution_ref.entity_id)
        )
        pointer = await self.shareable.load_current_action_pointer(command.scope)
        case = await self.core.load_case(command.scope)
        return ApproveActionResult(
            approval_id=approval.approval_id,
            approval_hash=approval.approval_hash,
            decision=approval.decision,
            expires_at=approval.expires_at,
            action_id=command.action_id,
            execution_id=execution.execution_id,
            execution_state=execution.state,
            execution_version=execution.version,
            pointer_status=(
                ActionProposalStatus.INVALIDATED if pointer is None else pointer.status
            ),
            case_state=case.state,
            case_version=case.version,
            authorization_version=case.authorization_version,
            replayed=True,
        )

    async def _complete_start(
        self, reservation: IdempotencyRecord, result: ApproveActionResult
    ) -> None:
        """Complete the HTTP request record after the decision is durable.

        A crash between the transaction and this write leaves domain 1 ``IN_PROGRESS``, which a
        same-hash retry recognises as its own unfinished attempt. That retry re-runs the
        decision, whose own conditions refuse -- the execution is no longer ``DRAFT@1`` -- and
        the answer then comes from domain 2's proof. The record is a convenience for the
        caller; the transaction's own commit proof is what settles whether it happened.
        """

        try:
            await self.unit_of_work.commit(
                TransactionPlan(
                    name="approve-action-complete",
                    operations=(
                        self.idempotency.stage_complete(
                            reservation,
                            result_entity_refs=result.result_refs,
                            response_status=200,
                            now=self.clock.now(),
                        ),
                    ),
                    audit_required=False,
                    commit_proof=self.idempotency.completion_proof(reservation),
                )
            )
        except PersistenceConflictError:
            # Somebody else completed this reservation with the same answer. Nothing to do.
            return


def _proof_refs(record: IdempotencyRecord) -> tuple[EntityRef, EntityRef]:
    """The one ``APPROVAL`` and one ``ACTION_EXECUTION`` reference an approval proof must carry.

    Anything else -- a missing reference, a duplicate, a foreign entity type -- is a malformed
    proof rather than a proof about something else, and it fails closed here.
    """

    approval_refs = [ref for ref in record.result_entity_refs if ref.entity_type == "APPROVAL"]
    execution_refs = [
        ref for ref in record.result_entity_refs if ref.entity_type == "ACTION_EXECUTION"
    ]
    if (
        len(record.result_entity_refs) != APPROVAL_RESULT_REFS
        or len(approval_refs) != 1
        or len(execution_refs) != 1
    ):
        raise IntegrityError("APPROVAL")
    return approval_refs[0], execution_refs[0]


_PLACEHOLDER_HASH = Sha256Digest("sha256:" + "0" * 64)
"""Written once and immediately replaced by the sealing digest, never persisted."""

_REJECTION_WINDOW = timedelta(minutes=15)
"""The expiry ceiling for a rejection, which has no view to bound it.

A rejection is allowed to be stale, so no view is loaded and none can supply the ``min`` term.
Fifteen minutes is the approval lifetime itself, applied twice over to the same value, so the
field is populated with the only honest answer available -- and nothing ever reads it, because
a decision that authorizes nothing has no window to be inside.
"""


def _invalidated(pointer: CurrentActionPointer, *, now: datetime) -> CurrentActionPointer:
    """The same pointer at ``INVALIDATED``, which is the only thing that frees the case.

    Every field but the status, the row version, and the timestamp is carried forward: the
    pointer still names which proposal was cleared, and a pointer that forgot would make the
    replacement rule in ADR-022 SS 6 uncheckable.
    """

    return replace(
        pointer,
        status=ActionProposalStatus.INVALIDATED,
        version=pointer.version + 1,
        updated_at=now,
    )


__all__ = [
    "APPROVAL_RESULT_REFS",
    "APPROVE_PARTICIPANTS",
    "REJECT_PARTICIPANTS",
    "ApprovalDenial",
    "ApprovalDeniedError",
    "ApprovalRecoveryFailure",
    "ApproveAction",
    "ApproveActionCommand",
    "ApproveActionResult",
    "RecoveredApproval",
    "StaleAuthorizationError",
    "approval_artifact_failures",
    "approval_proof_failures",
]
