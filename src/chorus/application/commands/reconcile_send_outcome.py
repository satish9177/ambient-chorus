"""``ReconcileSendOutcome``: one command, two named callers, and never an SES call.

Nothing runs this on a timer, nothing runs it as a side effect of a read, and it never sends.
Its two callers are the **application worker**, when a replay finds an execution in ``SENDING``
past the recovery window, and an **operator route**, for the evidence a configuration-set event
supplies (ADR-025 SS 10).

The three transitions, and what each one costs
-----------------------------------------------
``SENDING -> SEND_UNKNOWN`` requires that the fence's maximum life has passed **and** that no
live fence holds the case. Elapsed time alone is not evidence about a transaction; the fence
check is what distinguishes "the sender is still working" from "the sender is gone".

``SEND_UNKNOWN -> SENT`` requires positive evidence: an event from the deployment's own
configuration set, whose ``chorus_execution`` tag equals the recomputed derivation for this
exact execution, carrying a message ID. All three, because any two of them are satisfiable by
an event about a different execution or from a different deployment.

Those three are *correlation*, and correlation was never provenance
--------------------------------------------------------------------
A repair pass proved the difference. All three checks passed for an envelope a caller had typed
-- right configuration set, recomputed tag, invented ``mail.messageId`` -- and the row moved to
``SENT`` carrying the invented identifier. Every check had done its job; none of them was ever
about *where the observation came from*.

So the evidence this command accepts is no longer a decoded payload. It is
:class:`chorus.application.services.ses_events.AttestedSesEventEvidence`, minted only by the SES
event adapter after an authenticated transport, and checked here against a verifier that cannot
mint. A caller-built mapping, a caller-built ``SesEventEvidence``, and a caller-built attested
wrapper are all refused under ``UNATTESTED_EVIDENCE`` before any state is read; a deployment
with no boundary wired refuses under ``TRUST_BOUNDARY_UNAVAILABLE`` and leaves the quarantine
exactly where it was.

``SEND_UNKNOWN -> FAILED`` requires positive evidence that SES never accepted -- an event, or an
operator attestation recorded under its own reason code. Uncertainty stays unknown indefinitely
rather than being resolved by anything short of proof.

A disagreeing message ID is refused, never applied
---------------------------------------------------
Monotonic presence forbids rewriting ``ses_message_id`` once it is set, so a forged or tampered
identifier is at worst a **rejected reconciliation** and never a silent replacement of a
recorded outcome (T35). This module raises before staging anything, so the refusal is visible
rather than a conditional failure somebody has to interpret.

Why this transaction carries no idempotency record on the terminal branches
----------------------------------------------------------------------------
The execution row already answers the question a record would. The transition is conditional on
the exact row version, and monotonic presence refuses a rewrite, so a second reconciliation
presenting the same evidence finds a row that has moved and is refused, and one presenting
different evidence is refused earlier still. A commit proof here would be a second answer to a
question the row settles -- and the ``SENDING -> SEND_UNKNOWN`` branch, which genuinely *is*
persisting a send result that was lost, does carry the send-result proof for exactly that reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from chorus.application import observability
from chorus.application.services.action_authorization import (
    SEND_RECOVERY_WINDOW,
    send_key,
    send_request_hash,
    send_result_key_hash,
)
from chorus.application.services.ses_events import (
    AttestedSesEventEvidence,
    SesEventEvidence,
    SesEventEvidenceVerifier,
)
from chorus.application.services.ses_message import execution_tag_value
from chorus.domain.entities import (
    ActionExecution,
    ActionExecutionState,
    ActorType,
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
from chorus.ports.idempotency import EntityRef
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    IdempotencyRepositoryPort,
    ShareableRepositoryPort,
)
from chorus.ports.scopes import ActionScope, CaseScope
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork

RECONCILE_TRANSACTION = "reconcile-send-outcome"

QUARANTINE_PARTICIPANTS = 3
"""``SENDING -> SEND_UNKNOWN``: the transition, the audit event, and the send-result proof.

This branch *is* the send result, persisted late by whoever found the row abandoned, so it
writes the record the lost transaction would have written.
"""

TERMINAL_PARTICIPANTS = 2
"""``SEND_UNKNOWN -> SENT`` or ``-> FAILED``: the transition and the audit event.

See the module docstring: the execution row is its own idempotency here.
"""


class ReconciliationReason(StrEnum):
    """The closed reason codes an ``action.send.reconciled`` event may carry."""

    RECONCILED_SENT = "RECONCILED_SENT"
    RECONCILED_FAILED = "RECONCILED_FAILED"
    RECONCILED_UNKNOWN = "RECONCILED_UNKNOWN"


class ReconciliationRefusal(StrEnum):
    """Why reconciliation declined to move anything."""

    NOT_RECONCILABLE = "NOT_RECONCILABLE"
    RECOVERY_WINDOW_OPEN = "RECOVERY_WINDOW_OPEN"
    SEND_FENCE_ACTIVE = "SEND_FENCE_ACTIVE"
    INSUFFICIENT_PROOF = "INSUFFICIENT_PROOF"
    FOREIGN_CONFIGURATION_SET = "FOREIGN_CONFIGURATION_SET"
    TAG_MISMATCH = "TAG_MISMATCH"
    UNATTESTED_EVIDENCE = "UNATTESTED_EVIDENCE"
    """Offered evidence the trust boundary did not mint. A shape, not an observation."""

    TRUST_BOUNDARY_UNAVAILABLE = "TRUST_BOUNDARY_UNAVAILABLE"
    """No verifier is wired, so no evidence can be authenticated here at all (Phase 11)."""


class ReconciliationRefusedError(DomainError):
    """Reconciliation declined, under one closed code, having written nothing."""

    __slots__ = ("refusal",)

    def __init__(self, refusal: ReconciliationRefusal) -> None:
        super().__init__(DomainErrorCode.STATE_TRANSITION_ERROR, refusal.value)
        self.refusal = refusal

    @property
    def safe_code(self) -> str:
        return self.refusal.value


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconcileSendOutcomeCommand:
    """Reconcile one execution from whatever proof the caller actually has.

    ``evidence`` is present for the operator route and absent for the worker replay, and that
    asymmetry is the whole shape of the command: a worker with no evidence can only quarantine,
    and an operator with evidence can only resolve a quarantine.
    """

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    execution_id: ExecutionId
    actor_id_hash: Sha256Digest
    correlation_id: UUID
    evidence: AttestedSesEventEvidence | None = None
    """Attested evidence, or nothing. There is no parameter here a caller could forge into one.

    The type is the boundary. A ``SesEventEvidence`` -- the decoded payload -- is not accepted,
    because anybody can build one; what is accepted is the wrapper the SES event adapter mints
    after authenticating the transport, which nobody can build without the adapter's own key.
    """

    operator_attestation_code: str | None = None
    """A closed code recording that a person inspected SES and attests no acceptance occurred.

    Never a free-text note, and never sufficient for ``-> SENT``: an attestation can establish
    that something did *not* happen, which is a claim a human can responsibly make, and cannot
    establish a message identifier, which is a value only SES can produce.
    """

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
class ReconcileSendOutcomeResult:
    execution_id: ExecutionId
    state: ActionExecutionState
    version: int
    ses_message_id: str | None
    reason_code: str
    moved: bool


@dataclass(slots=True)
class ReconcileSendOutcome:
    """Repair durable classification when -- and only when -- proof supports it."""

    shareable: ShareableRepositoryPort
    core: CoreRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    clock: Clock
    ids: IdGenerator
    configuration_set: str
    evidence_trust: SesEventEvidenceVerifier | None = None
    """The checking half of the SES event trust boundary, or nothing.

    ``None`` is the honest Phase-8 default and not an oversight: the boundary's other half needs
    a :class:`chorus.ports.ses_events.SesEventTransportAuthenticator` over a deployed transport,
    and Phase 11 owns that. A command wired without it can still quarantine and can still take
    an operator attestation to ``FAILED``; what it cannot do is resolve anything to ``SENT``.
    """

    async def execute(self, command: ReconcileSendOutcomeCommand) -> ReconcileSendOutcomeResult:
        execution = await self.shareable.load_execution(command.action_scope, command.execution_id)
        if execution.state is ActionExecutionState.SENDING:
            return await self._quarantine(command, execution)
        if execution.state is ActionExecutionState.SEND_UNKNOWN:
            return await self._resolve(command, execution)
        # Every other state is already authoritative. Reconciliation is a repair, not a second
        # opinion, and there is nothing here to repair.
        raise ReconciliationRefusedError(ReconciliationRefusal.NOT_RECONCILABLE)

    async def _quarantine(
        self, command: ReconcileSendOutcomeCommand, execution: ActionExecution
    ) -> ReconcileSendOutcomeResult:
        """``SENDING -> SEND_UNKNOWN``, and only once nothing could still be working on it."""

        now = self.clock.now()
        started = execution.started_at
        if started is None:  # pragma: no cover - required at SENDING
            raise IntegrityError("ACTION_EXECUTION")
        if now < started + SEND_RECOVERY_WINDOW:
            raise ReconciliationRefusedError(ReconciliationRefusal.RECOVERY_WINDOW_OPEN)
        fence = await self.core.load_send_fence(command.scope)
        if fence is not None and now < fence.expires_at:
            # A live fence means a sender still holds this case. Quarantining underneath a
            # working process would record an outcome about an attempt that has not finished.
            raise ReconciliationRefusedError(ReconciliationRefusal.SEND_FENCE_ACTIVE)

        # No ``failure_detail_safe``, for the reason the send command records: it is ``ABSENT``
        # at ``SENT``, presence is monotonic, and a quarantined row must stay able to take the
        # frozen ``SEND_UNKNOWN -> SENT`` edge when proof arrives. ``SENDER_PROCESS_LOST`` is
        # carried by the audit event instead.
        unknown = transition_action_execution(
            execution,
            ActionExecutionState.SEND_UNKNOWN,
            expected_version=execution.version,
            now=now,
            finished_at=now,
        )
        key = send_key(
            namespace=command.namespace,
            action_id=command.action_id,
            actor_id_hash=command.actor_id_hash,
            key_hash=send_result_key_hash(execution.idempotency_key or ""),
        )
        request_hash = send_request_hash(
            case_id=command.case_id,
            action_id=command.action_id,
            execution_id=command.execution_id,
            approval_id=ApprovalId(_require(execution.approval_id).value),
            expected_execution_version=execution.version,
        )
        operations = (
            self.shareable.stage_update_execution(
                command.action_scope, unknown, expected_version=execution.version
            ),
            self.audit.stage_append_case_event(
                command.scope,
                self._audit_event(
                    command,
                    execution=unknown,
                    reason_code=ReconciliationReason.RECONCILED_UNKNOWN,
                    now=now,
                ),
            ),
            self.idempotency.stage_create_completed(
                key,
                request_hash=request_hash,
                result_entity_refs=(
                    EntityRef(
                        entity_type="ACTION_EXECUTION",
                        entity_id=unknown.execution_id.value,
                        version=unknown.version,
                    ),
                ),
                response_status=200,
                now=now,
            ),
        )
        if len(operations) != QUARANTINE_PARTICIPANTS:  # pragma: no cover - arithmetic guard
            raise IntegrityError("ACTION_EXECUTION")
        await self.unit_of_work.commit(
            TransactionPlan(
                name=RECONCILE_TRANSACTION,
                operations=operations,
                audit_required=True,
                commit_proof=self.idempotency.commit_proof(key, request_hash=request_hash),
            )
        )
        self._emit(command, unknown, ReconciliationReason.RECONCILED_UNKNOWN)
        return ReconcileSendOutcomeResult(
            execution_id=unknown.execution_id,
            state=unknown.state,
            version=unknown.version,
            ses_message_id=None,
            reason_code=ReconciliationReason.RECONCILED_UNKNOWN.value,
            moved=True,
        )

    async def _resolve(
        self, command: ReconcileSendOutcomeCommand, execution: ActionExecution
    ) -> ReconcileSendOutcomeResult:
        """``SEND_UNKNOWN -> SENT`` or ``-> FAILED``, on positive evidence only."""

        now = self.clock.now()
        target, message_id, reason = self._classify(command, execution)
        # ``reconciled_at`` is ``OPTIONAL`` at ``SENT`` and ``ABSENT`` at ``FAILED``, so it is
        # written only on the branch the presence table admits it on. The fact that a
        # reconciliation happened at all is carried by the ``action.send.reconciled`` audit
        # event either way.
        moved = transition_action_execution(
            execution,
            target,
            expected_version=execution.version,
            now=now,
            reconciliation_proof=True,
            ses_message_id=message_id,
            finished_at=execution.finished_at or now,
            failure_code=(None if target is ActionExecutionState.SENT else reason.value),
            reconciled_at=now if target is ActionExecutionState.SENT else None,
        )
        operations = (
            self.shareable.stage_update_execution(
                command.action_scope, moved, expected_version=execution.version
            ),
            self.audit.stage_append_case_event(
                command.scope,
                self._audit_event(command, execution=moved, reason_code=reason, now=now),
            ),
        )
        if len(operations) != TERMINAL_PARTICIPANTS:  # pragma: no cover - arithmetic guard
            raise IntegrityError("ACTION_EXECUTION")
        await self.unit_of_work.commit(
            TransactionPlan(name=RECONCILE_TRANSACTION, operations=operations, audit_required=True)
        )
        self._emit(command, moved, reason)
        return ReconcileSendOutcomeResult(
            execution_id=moved.execution_id,
            state=moved.state,
            version=moved.version,
            ses_message_id=moved.ses_message_id,
            reason_code=reason.value,
            moved=True,
        )

    def _classify(
        self, command: ReconcileSendOutcomeCommand, execution: ActionExecution
    ) -> tuple[ActionExecutionState, str | None, ReconciliationReason]:
        """Turn the offered proof into a target state, or refuse to move at all.

        Every refusal happens here, before anything is staged, so a forged identifier is a
        rejected reconciliation rather than a conditional failure downstream.
        """

        attested = command.evidence
        if attested is None:
            if command.operator_attestation_code:
                # A person attesting that SES never accepted. Sufficient for FAILED and never
                # for SENT: a human can responsibly say something did not happen and cannot
                # produce a message identifier.
                return (
                    ActionExecutionState.FAILED,
                    None,
                    ReconciliationReason.RECONCILED_FAILED,
                )
            raise ReconciliationRefusedError(ReconciliationRefusal.INSUFFICIENT_PROOF)

        evidence = self._authenticated(attested)
        if evidence.configuration_set != self.configuration_set:
            raise ReconciliationRefusedError(ReconciliationRefusal.FOREIGN_CONFIGURATION_SET)
        expected_tag = execution_tag_value(
            namespace=command.namespace, execution_id=command.execution_id
        )
        if evidence.execution_tag != expected_tag:
            raise ReconciliationRefusedError(ReconciliationRefusal.TAG_MISMATCH)
        if not evidence.accepted:
            return ActionExecutionState.FAILED, None, ReconciliationReason.RECONCILED_FAILED
        message_id = evidence.message_id
        if not message_id:  # pragma: no cover - refused by the evidence type
            raise ReconciliationRefusedError(ReconciliationRefusal.INSUFFICIENT_PROOF)
        recorded = execution.ses_message_id
        if recorded is not None and recorded != message_id:
            # An identifier that disagrees with one already recorded is an integrity failure and
            # never an overwrite. This is the check that makes a forged event, at worst, a
            # rejected reconciliation.
            raise IntegrityError("ACTION_EXECUTION")
        return ActionExecutionState.SENT, message_id, ReconciliationReason.RECONCILED_SENT

    def _authenticated(self, attested: AttestedSesEventEvidence) -> SesEventEvidence:
        """Unwrap evidence the boundary minted, and refuse everything else.

        This runs before the configuration set, before the tag, and before any row is compared,
        because those comparisons describe *what an observation says* and this one asks whether
        there was an observation. Getting that order wrong is how a typed envelope became a
        ``SENT`` row.
        """

        if self.evidence_trust is None:
            raise ReconciliationRefusedError(ReconciliationRefusal.TRUST_BOUNDARY_UNAVAILABLE)
        if not self.evidence_trust.attests(attested):
            raise ReconciliationRefusedError(ReconciliationRefusal.UNATTESTED_EVIDENCE)
        return attested.evidence

    def _emit(
        self,
        command: ReconcileSendOutcomeCommand,
        execution: ActionExecution,
        reason: ReconciliationReason,
    ) -> None:
        observability.execution_reconciled(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            execution_id=execution.execution_id.value,
            outcome=execution.state.value,
            reason_codes=(reason.value,),
        )

    def _audit_event(
        self,
        command: ReconcileSendOutcomeCommand,
        *,
        execution: ActionExecution,
        reason_code: ReconciliationReason,
        now: datetime,
    ) -> AuditEvent:
        return AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            actor_type=ActorType.SYSTEM,
            actor_id_hash=command.actor_id_hash,
            event_type="action.send.reconciled",
            occurred_at=now,
            correlation_id=command.correlation_id,
            causation_id=None,
            idempotency_key_hash=None,
            entity_refs=(
                AuditEntityRef(
                    entity_type="ACTION_EXECUTION",
                    entity_id=execution.execution_id.value,
                    version=execution.version,
                ),
            ),
            decision=(
                AuditDecision.ALLOW
                if reason_code is ReconciliationReason.RECONCILED_SENT
                else AuditDecision.NONE
            ),
            reason_codes=(reason_code.value,),
            safe_details=AuditDetails(count=None, rule_id=None),
            input_hash=execution.rendered_message_hash,
            output_hash=execution.ses_request_token_hash,
        )


def _require(value: ApprovalId | None) -> ApprovalId:
    if value is None:  # pragma: no cover - required from APPROVED onwards
        raise IntegrityError("ACTION_EXECUTION")
    return value


__all__ = [
    "QUARANTINE_PARTICIPANTS",
    "TERMINAL_PARTICIPANTS",
    "AttestedSesEventEvidence",
    "ReconcileSendOutcome",
    "ReconcileSendOutcomeCommand",
    "ReconcileSendOutcomeResult",
    "ReconciliationReason",
    "ReconciliationRefusal",
    "ReconciliationRefusedError",
    "SesEventEvidence",
]
