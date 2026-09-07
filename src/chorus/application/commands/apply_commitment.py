"""Transaction B: a grounded promise becomes a ``PENDING`` commitment and a ``VERIFYING`` case.

Six participants, Core / Shareable / Audit, run by the application
([ADR-027](../../../../docs/adr/ADR-027-commitment-extraction-grounding-and-authority.md) § 7):

1. the ``Commitment`` in ``PENDING``, create-only;
2. the commitment schedule projection in ``PENDING_SCHEDULE``, create-only;
3. the guarded case update ``ACTIONED -> VERIFYING``, conditioned on the exact ``version``,
   ``authorization_version``, and ``state``, moving ``version`` only;
4. the successful ``EXTRACT_COMMITMENT`` agent-invocation record, so a redelivery learns the
   apply happened rather than spending a second model pass over a stranger's email;
5. the ``commitment.created`` audit event;
6. the completed ``CREATE_COMMITMENT`` idempotency record, this plan's commit proof.

**``ACTIONED -> VERIFYING`` is not a transaction of its own.** It is participant 3, and
separating it would create a window in which a case is ``VERIFYING`` with no commitment. Its
durable predicate is the commitment the *same transaction* creates, so the guard is satisfied by
the transaction's own participant and never by a caller-set flag.

**B″ -- no valid commitment. Two participants**: the ``commitment.rejected`` audit event carrying
the per-proposal codes, and the completed idempotency record carrying the deterministic
rejection response, so a redelivered command replays its answer instead of spending a second
model pass.

The identifier is UUIDv4
-------------------------
[ADR-011](../../../../docs/adr/ADR-011-monitor-deterministic-identities.md) names ``Commitment``
explicitly among the entities that stay UUIDv4, and this does not widen that exception. Replay
safety here already has two stronger guarantees: the ``CREATE_COMMITMENT`` record is this
transaction's own commit proof, so a redelivered apply replays the recorded result; and check 9
refuses a second commitment for an action that already has a live one, whatever identifier a
second attempt would mint. A derived identity would buy nothing and would put model-influenced
text -- ``action_text`` -- inside an entity identifier, which ADR-011 forbids in as many words.

What this command does not hold
--------------------------------
No SES port, no compiler port, no scheduler port, and no case-resolution verb. The schedule is
created **outside and after** this transaction
([ADR-028](../../../../docs/adr/ADR-028-deadline-watcher-and-scheduler-boundary.md)), by a
different object, so at the moment this command reads model output there is nothing here that
could send, compile, or schedule anything (T38).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from chorus.application import observability
from chorus.application.services.commitment_schedule import due_event_id, schedule_name
from chorus.application.services.commitment_validation import (
    CommitmentValidationOutcome,
    ValidatedCommitment,
    validate_extraction,
)
from chorus.application.services.mandate_terms import key_hash
from chorus.contracts.commitment import (
    COMMITMENT_EXTRACTION_PROMPT_VERSION,
    CommitmentExtractionOutput,
)
from chorus.domain.entities import (
    ActorType,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    CaseState,
    Commitment,
    CommitmentStatus,
    CommunityCase,
    EvidenceItem,
)
from chorus.domain.errors import IntegrityError, ValidationError
from chorus.domain.ids import (
    ActionId,
    CaseId,
    CommitmentId,
    CommunityId,
    EvidenceItemId,
    IdGenerator,
    Namespace,
    Sha256Digest,
)
from chorus.domain.state import CaseTransitionContext, transition_case
from chorus.ports.clock import Clock
from chorus.ports.errors import IdempotencyConflictError
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotencyStatus,
    IdempotentCommand,
)
from chorus.ports.records import (
    AgentInvocationOutcome,
    AgentInvocationResult,
    CommitmentScheduleProjection,
    CommitmentScheduleStatus,
)
from chorus.ports.records import AgentName as StoredAgentName
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    IdempotencyRepositoryPort,
    ShareableRepositoryPort,
)
from chorus.ports.scopes import CaseScope
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork

APPLY_TRANSACTION = "apply-commitment"
REJECT_TRANSACTION = "reject-commitment"

APPLY_PARTICIPANTS = 6
"""Commitment, schedule projection, case edge, invocation record, audit event, commit proof."""

REJECT_PARTICIPANTS = 3
"""The rejection audit event, the agent-invocation record, and the idempotency record.

Three rather than the two ADR-027 § 7 names for B'': the invocation record is the durable proof
:func:`chorus.application.commands.extract_commitment_operation.ExtractCommitment._recovered`
reads to avoid a second model call over a rejected reply. Without it, a redelivery of an already
-rejected extraction found no ``SUCCEEDED`` invocation record for its own invocation identity --
only the accept path (``_apply``) ever wrote one -- and fell through to invoking the model again
over a stranger's email a second time (Astra P2-4). This is a deliberate, reported deviation from
the ADR's own participant count, made to close that gap without widening what a caller may pass.
"""

COMMITMENT_CREATED_REASON_CODE = "COMMITMENT_CREATED"
COMMITMENT_REJECTED_REASON_CODE = "COMMITMENT_REJECTED"
INITIAL_SCHEDULE_GENERATION = 1
"""V1 has no reschedule verb, so every commitment lives its whole life at generation one."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyCommitmentCommand:
    """One extraction answer about one artifact, plus the invocation that produced it."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    evidence_id: EvidenceItemId
    invocation_id: UUID
    correlation_id: UUID
    actor_id_hash: Sha256Digest
    output: CommitmentExtractionOutput
    input_hash: Sha256Digest
    output_hash: Sha256Digest

    @property
    def scope(self) -> CaseScope:
        return CaseScope(
            namespace=self.namespace, community_id=self.community_id, case_id=self.case_id
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyCommitmentResult:
    """What one apply produced: a commitment, or the codes that refused every proposal."""

    commitment_id: CommitmentId | None
    case_state: CaseState
    case_version: int
    rejection_codes: tuple[str, ...]
    replayed: bool


@dataclass(slots=True)
class ApplyCommitment:
    """Validate one extraction against the nine checks and persist at most one commitment."""

    core: CoreRepositoryPort
    shareable: ShareableRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    clock: Clock
    ids: IdGenerator
    destination_label: str
    """The safe organization label of the deployment's destination.

    Check 4 compares the model's ``obligor`` with this and never with anything the reply said.
    It is a value from the non-secret safe destination configuration, so the "wrong responsible
    party" failure is closed structurally rather than by asking a model to be careful (SEC-23).
    """
    scheduler_environment: str

    async def execute(self, command: ApplyCommitmentCommand) -> ApplyCommitmentResult:
        key = self._key(command)
        replayed = await self._replay(key, command)
        if replayed is not None:
            return replayed

        artifact = await self._artifact(command)
        case = await self.core.load_case(command.scope)
        live = await self.shareable.load_live_commitment(command.scope, command.action_id)

        outcome = validate_extraction(
            command.output.commitments,
            extracted_text=self._text(artifact),
            destination_label=self.destination_label,
            received_at=self._received_at(artifact),
            action_has_live_commitment=live is not None,
        )
        observability.commitment_extracted(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            invocation_id=command.invocation_id,
            prompt_version=COMMITMENT_EXTRACTION_PROMPT_VERSION,
            proposed=len(command.output.commitments),
        )
        if outcome.accepted is None:
            if live is not None:
                # An action that already has a live commitment gets the existing one back
                # rather than a second: check 9 is a cap, not an error.
                return await self._reject(command, case, outcome, existing=live)
            return await self._reject(command, case, outcome, existing=None)
        if case.state is not CaseState.ACTIONED:
            # ``ACTIONED -> VERIFYING`` is the only edge this command may take, and a case that
            # moved while the model was answering must not be dragged onto it.
            return await self._reject(command, case, outcome, existing=None)
        return await self._apply(command, case, outcome, key=key)

    # -- the two shapes -------------------------------------------------------------------

    async def _apply(
        self,
        command: ApplyCommitmentCommand,
        case: CommunityCase,
        outcome: CommitmentValidationOutcome,
        *,
        key: IdempotencyKey,
    ) -> ApplyCommitmentResult:
        accepted = outcome.accepted
        if accepted is None:  # pragma: no cover - the caller checks first
            raise IntegrityError("COMMITMENT")
        now = self.clock.now()
        commitment_id = self.ids.new(CommitmentId)
        commitment = self._commitment(command, accepted, commitment_id=commitment_id, now=now)
        verifying = transition_case(
            case,
            CaseState.VERIFYING,
            expected_version=case.version,
            reason_code=COMMITMENT_CREATED_REASON_CODE,
            now=now,
            # Satisfied by participant 1 of this same transaction, and by nothing a caller set.
            context=CaseTransitionContext(commitment_or_verification_exists=True),
        )
        if verifying.authorization_version != case.authorization_version:  # pragma: no cover
            # ADR-020 row 9. Creating a commitment changes no fact, status, mandate, or count.
            raise IntegrityError("COMMUNITY_CASE")

        operations = (
            self.shareable.stage_create_commitment(command.scope, commitment),
            self.shareable.stage_create_commitment_schedule(
                command.scope,
                CommitmentScheduleProjection(
                    namespace=command.namespace,
                    community_id=command.community_id,
                    case_id=command.case_id,
                    commitment_id=commitment_id,
                    status=CommitmentScheduleStatus.PENDING_SCHEDULE,
                    schedule_name=commitment.scheduler_name,
                    generation=INITIAL_SCHEDULE_GENERATION,
                    attempts=0,
                    version=1,
                    created_at=now,
                    updated_at=now,
                ),
            ),
            self.core.stage_update_case(
                command.scope,
                verifying,
                expected_version=case.version,
                expected_authorization_version=case.authorization_version,
                expected_state=CaseState.ACTIONED,
            ),
            self.core.stage_append_agent_invocation(
                command.scope,
                AgentInvocationResult(
                    invocation_id=command.invocation_id,
                    namespace=command.namespace,
                    community_id=command.community_id,
                    case_id=command.case_id,
                    operation_id=None,
                    agent_name=StoredAgentName.INVESTIGATOR,
                    prompt_version=COMMITMENT_EXTRACTION_PROMPT_VERSION,
                    input_hash=command.input_hash,
                    output_hash=command.output_hash,
                    outcome=AgentInvocationOutcome.SUCCEEDED,
                    result_refs=(
                        EntityRef(entity_type="COMMITMENT", entity_id=commitment_id.value),
                    ),
                    reason_codes=outcome.reason_codes,
                    created_at=now,
                ),
            ),
            self.audit.stage_append_case_event(
                command.scope,
                self._audit_event(
                    command,
                    case=verifying,
                    commitment_id=commitment_id,
                    now=now,
                    event_type=observability.EventName.COMMITMENT_CREATED,
                    # The siblings that failed are audited **with** the one that passed. A
                    # dropped proposal is a decision this system made about a stranger's text,
                    # and recording it only when nothing survived would hide exactly the case
                    # where the model got one of three right.
                    reason_codes=(COMMITMENT_CREATED_REASON_CODE, *outcome.reason_codes),
                    decision=AuditDecision.ALLOW,
                ),
            ),
            self.idempotency.stage_create_completed(
                key,
                request_hash=command.output_hash,
                result_entity_refs=(
                    EntityRef(
                        entity_type="COMMITMENT",
                        entity_id=commitment_id.value,
                        version=commitment.version,
                    ),
                    EntityRef(
                        entity_type="COMMUNITY_CASE",
                        entity_id=command.case_id.value,
                        version=verifying.version,
                    ),
                ),
                response_status=201,
                now=now,
            ),
        )
        if len(operations) != APPLY_PARTICIPANTS:  # pragma: no cover - arithmetic guard
            raise IntegrityError("COMMITMENT")
        await self.unit_of_work.commit(
            TransactionPlan(
                name=APPLY_TRANSACTION,
                operations=operations,
                audit_required=True,
                commit_proof=self.idempotency.commit_proof(key, request_hash=command.output_hash),
            )
        )
        observability.commitment_created(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            commitment_id=commitment_id.value,
            case_version=verifying.version,
        )
        return ApplyCommitmentResult(
            commitment_id=commitment_id,
            case_state=verifying.state,
            case_version=verifying.version,
            rejection_codes=outcome.reason_codes,
            replayed=False,
        )

    async def _reject(
        self,
        command: ApplyCommitmentCommand,
        case: CommunityCase,
        outcome: CommitmentValidationOutcome,
        *,
        existing: Commitment | None,
    ) -> ApplyCommitmentResult:
        """Three participants: the audit row, the invocation proof, and the redelivery record.

        The case is not touched. An invalid or irrelevant reply cannot change case state: the
        artifact is stored and private, and no edge is taken.
        """

        now = self.clock.now()
        key = self._key(command)
        codes = outcome.reason_codes or (COMMITMENT_REJECTED_REASON_CODE,)
        refs: tuple[EntityRef, ...] = ()
        if existing is not None:
            refs = (
                EntityRef(
                    entity_type="COMMITMENT",
                    entity_id=existing.commitment_id.value,
                    version=existing.version,
                ),
            )
        operations = (
            self.audit.stage_append_case_event(
                command.scope,
                self._audit_event(
                    command,
                    case=case,
                    commitment_id=existing.commitment_id if existing else None,
                    now=now,
                    event_type=observability.EventName.COMMITMENT_REJECTED,
                    reason_codes=codes,
                    decision=AuditDecision.DENY,
                ),
            ),
            self.core.stage_append_agent_invocation(
                command.scope,
                AgentInvocationResult(
                    invocation_id=command.invocation_id,
                    namespace=command.namespace,
                    community_id=command.community_id,
                    case_id=command.case_id,
                    operation_id=None,
                    agent_name=StoredAgentName.INVESTIGATOR,
                    prompt_version=COMMITMENT_EXTRACTION_PROMPT_VERSION,
                    input_hash=command.input_hash,
                    output_hash=command.output_hash,
                    outcome=AgentInvocationOutcome.SUCCEEDED,
                    result_refs=(),
                    reason_codes=codes,
                    created_at=now,
                ),
            ),
            self.idempotency.stage_create_completed(
                key,
                request_hash=command.output_hash,
                result_entity_refs=refs,
                response_status=200,
                now=now,
            ),
        )
        if len(operations) != REJECT_PARTICIPANTS:  # pragma: no cover - arithmetic guard
            raise IntegrityError("COMMITMENT")
        await self.unit_of_work.commit(
            TransactionPlan(
                name=REJECT_TRANSACTION,
                operations=operations,
                audit_required=True,
                commit_proof=self.idempotency.commit_proof(key, request_hash=command.output_hash),
            )
        )
        observability.commitment_rejected(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            reason_codes=codes,
            proposed=len(command.output.commitments),
        )
        return ApplyCommitmentResult(
            commitment_id=existing.commitment_id if existing else None,
            case_state=case.state,
            case_version=case.version,
            rejection_codes=codes,
            replayed=False,
        )

    # -- pieces ---------------------------------------------------------------------------

    def _commitment(
        self,
        command: ApplyCommitmentCommand,
        accepted: ValidatedCommitment,
        *,
        commitment_id: CommitmentId,
        now: datetime,
    ) -> Commitment:
        """All three schedule fields are **derived at creation**, before any AWS call.

        ``scheduler_name``, ``schedule_generation``, and ``due_event_id`` are deterministic
        functions of ``commitment_id`` and ``generation = 1``, so the entity's non-optional
        fields are satisfiable here and nothing has to be attached afterwards (ADR-028 § 4).
        """

        return Commitment(
            commitment_id=commitment_id,
            case_id=command.case_id,
            action_id=command.action_id,
            source_evidence_id=command.evidence_id,
            obligor=accepted.obligor,
            action_text=accepted.action_text,
            due_at=accepted.due_at,
            verification_method=accepted.verification_method,
            status=CommitmentStatus.PENDING,
            scheduler_name=schedule_name(
                environment=self.scheduler_environment,
                namespace=command.namespace,
                commitment_id=commitment_id,
                generation=INITIAL_SCHEDULE_GENERATION,
            ),
            schedule_generation=INITIAL_SCHEDULE_GENERATION,
            due_event_id=due_event_id(
                commitment_id=commitment_id, generation=INITIAL_SCHEDULE_GENERATION
            ),
            verified_by_contributor_id=None,
            verification_evidence_id=None,
            outcome_note=None,
            version=1,
            created_at=now,
            updated_at=now,
        )

    async def _artifact(self, command: ApplyCommitmentCommand) -> EvidenceItem:
        """Load the one inbound artifact this extraction was about, and prove it is one.

        An evidence item with no external source binding is a resident upload, and a commitment
        grounded against one would be a promise attributed to a neighbour.
        """

        items = await self.core.load_evidence_items(command.scope, (command.evidence_id,))
        if len(items) != 1:  # pragma: no cover - the loader raises first
            raise IntegrityError("EVIDENCE_ITEM")
        artifact = items[0]
        if artifact.external_source_binding is None:
            raise ValidationError("EVIDENCE_ITEM")
        if artifact.external_source_binding.correlated_action_id != command.action_id:
            raise IntegrityError("EVIDENCE_ITEM")
        return artifact

    @staticmethod
    def _text(artifact: EvidenceItem) -> str:
        """The exact stored text the spans index into, or the empty string.

        Never a reconstruction and never a re-normalization: the value the model was given and
        the value deterministic code checks against are the same stored bytes.
        """

        return "" if artifact.extracted_text is None else artifact.extracted_text.reveal()

    @staticmethod
    def _received_at(artifact: EvidenceItem) -> datetime:
        received = artifact.captured_at
        if received is None:  # pragma: no cover - the binding always carries one
            raise IntegrityError("EVIDENCE_ITEM")
        return received

    def _key(self, command: ApplyCommitmentCommand) -> IdempotencyKey:
        """``CREATE_COMMITMENT``, ``CASE`` partition, keyed on evidence and invocation.

        ``sha256(evidence_id | invocation_id)``: one artifact answered by one invocation is one
        command, so a redelivered apply reads its own answer rather than running a second model
        pass over a stranger's email.
        """

        return IdempotencyKey(
            partition=IdempotencyPartition(
                kind=IdempotencyPartitionKind.CASE,
                namespace=command.namespace,
                case_id=command.case_id,
            ),
            command=IdempotentCommand.CREATE_COMMITMENT,
            actor_id_hash=command.actor_id_hash,
            key_hash=key_hash(
                f"create-commitment\x1f{command.evidence_id}\x1f{command.invocation_id}"
            ),
        )

    async def _replay(
        self, key: IdempotencyKey, command: ApplyCommitmentCommand
    ) -> ApplyCommitmentResult | None:
        """Answer from this transaction's own commit proof, never from the caller's output.

        The idempotency record alone proves *that* this invocation already completed; the exact
        codes a rejection carried live on the agent-invocation record this same invocation wrote
        (``_apply`` and ``_reject`` both write one now), addressed by ``invocation_id`` the same
        way the pre-model recovery in ``ExtractCommitment._recovered`` addresses it. A replay
        that found the completed idempotency record but not a matching invocation record is a
        proof that says only "some rejection completed" and is refused, never guessed at.

        The completed record's ``request_hash`` is bound to ``command.output_hash`` before any
        of that: the same key completed for one model output must never answer for a retry
        that carries a different one (empty proposals, a sibling proposal, an altered clause),
        so a mismatch fails closed here rather than replaying a stranger's outcome.
        """

        record = await self.idempotency.load(key)
        if record is None or record.status is not IdempotencyStatus.COMPLETED:
            return None
        if record.request_hash != command.output_hash:
            raise IdempotencyConflictError("COMMITMENT")
        case = await self.core.load_case(command.scope)
        commitment_ref = next(
            (ref for ref in record.result_entity_refs if ref.entity_type == "COMMITMENT"), None
        )
        reason_codes: tuple[str, ...] = ()
        if commitment_ref is None:
            invocation = await self.core.load_agent_invocation(command.scope, command.invocation_id)
            if (
                invocation is None
                or invocation.outcome is not AgentInvocationOutcome.SUCCEEDED
                or invocation.prompt_version != COMMITMENT_EXTRACTION_PROMPT_VERSION
                or invocation.input_hash != command.input_hash
                or invocation.case_id != command.case_id
            ):
                raise IntegrityError("AGENT_INVOCATION")
            reason_codes = invocation.reason_codes
        return ApplyCommitmentResult(
            commitment_id=(
                CommitmentId(commitment_ref.entity_id) if commitment_ref is not None else None
            ),
            case_state=case.state,
            case_version=case.version,
            rejection_codes=reason_codes,
            replayed=True,
        )

    def _audit_event(
        self,
        command: ApplyCommitmentCommand,
        *,
        case: CommunityCase,
        commitment_id: CommitmentId | None,
        now: datetime,
        event_type: str,
        reason_codes: tuple[str, ...],
        decision: AuditDecision,
    ) -> AuditEvent:
        refs = [
            AuditEntityRef(
                entity_type="COMMUNITY_CASE",
                entity_id=command.case_id.value,
                version=case.version,
            ),
            AuditEntityRef(
                entity_type="EVIDENCE_ITEM", entity_id=command.evidence_id.value, version=None
            ),
        ]
        if commitment_id is not None:
            refs.insert(
                0,
                AuditEntityRef(entity_type="COMMITMENT", entity_id=commitment_id.value, version=1),
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
            causation_id=command.invocation_id,
            idempotency_key_hash=None,
            entity_refs=tuple(refs),
            decision=decision,
            reason_codes=reason_codes,
            safe_details=AuditDetails(count=len(command.output.commitments), rule_id=None),
            input_hash=command.input_hash,
            output_hash=command.output_hash,
        )


__all__ = [
    "APPLY_PARTICIPANTS",
    "APPLY_TRANSACTION",
    "COMMITMENT_CREATED_REASON_CODE",
    "COMMITMENT_REJECTED_REASON_CODE",
    "INITIAL_SCHEDULE_GENERATION",
    "REJECT_PARTICIPANTS",
    "REJECT_TRANSACTION",
    "ApplyCommitment",
    "ApplyCommitmentCommand",
    "ApplyCommitmentResult",
]
