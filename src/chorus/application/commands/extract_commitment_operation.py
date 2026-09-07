"""The ``EXTRACT_COMMITMENT`` operation: one reply in, one invocation, one apply.

Its own operation kind rather than a reply-triggered Investigator run
([ADR-027](../../../../docs/adr/ADR-027-commitment-extraction-grounding-and-authority.md) § 1).
The Phase-5 investigation apply writes fact statuses, ``corroboration_source_count``, and the
assessment pointer, and takes ``INVESTIGATING -> READY_FOR_ACTION``. From ``ACTIONED`` that edge
does not exist, and widening it to make one command serve two purposes would mean a
reply-triggered run could rewrite the evidence statuses of a case whose message has already been
sent. ``POST /v1/cases/{case_id}/investigations`` therefore keeps its state guard unchanged.

What the model is given
------------------------
The case identifier and the **single** inbound artifact's normalized ``extracted_text``,
delimited as untrusted data. Not the case, not other evidence, not facts, not mandates, not
contributor data. The model that reads a stranger's email is given nothing else to leak.

Recovery
---------
An ``EXTRACT_COMMITMENT`` operation found ``RUNNING`` resumes by reading the durable
agent-invocation record, and that record is proof only when scope, invocation identity,
``agent == INVESTIGATOR``, prompt version, the input hash recomputed from the immutable
evidence, and an exact ``{COMMITMENT}`` or empty result-reference set all verify. A committed
apply therefore costs **zero** additional model calls, which is the property that matters: a
second pass here is a second reading of somebody's private email.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from chorus.application import observability
from chorus.application.commands.apply_commitment import (
    ApplyCommitment,
    ApplyCommitmentCommand,
    ApplyCommitmentResult,
)
from chorus.application.operations import ApplicationOperations, extract_commitment_binding_hash
from chorus.contracts.commitment import (
    COMMITMENT_EXTRACTION_PROMPT_VERSION,
    CommitmentExtractionInput,
    CommitmentExtractionOutput,
)
from chorus.contracts.common import (
    AGENT_INPUT_SCHEMA_VERSION,
    AgentInputEnvelope,
)
from chorus.contracts.common import AgentName as ContractAgentName
from chorus.domain.entities import (
    ApplicationOperation,
    ApplicationOperationKind,
    ApplicationOperationStatus,
    EvidenceItem,
)
from chorus.domain.errors import DomainError, StateTransitionError
from chorus.domain.ids import (
    ActionId,
    CaseId,
    CommunityId,
    EvidenceItemId,
    Namespace,
    OperationId,
    Sha256Digest,
)
from chorus.ports.agents import (
    AgentContractViolationError,
    AgentError,
    CommitmentExtractionPort,
    CommitmentExtractionRejection,
)
from chorus.ports.clock import Clock
from chorus.ports.errors import PersistenceError
from chorus.ports.records import AgentInvocationOutcome
from chorus.ports.repositories import CoreRepositoryPort
from chorus.ports.scopes import CaseScope
from chorus.privacy.canonical import hash_value

INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
EXTRACTION_INPUT_SCHEMA = "commitment-extraction-input-hash/v1"
EXTRACTION_OUTPUT_SCHEMA = "commitment-extraction-output-hash/v1"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractCommitmentJob:
    """One extraction of one artifact, addressed by identity alone.

    There is deliberately no field for the reply text, for a proposed deadline, for an obligor,
    or for anything the model might return: the payload is assembled by the use case from the
    stored artifact, so neither a queue message nor an HTTP caller can steer what the model reads
    or what it may say.
    """

    operation_id: OperationId
    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    evidence_id: EvidenceItemId
    invocation_id: UUID
    correlation_id: UUID
    actor_id_hash: Sha256Digest
    request_hash: Sha256Digest
    """The digest of the *command*, carried so the worker can bind before it claims."""

    evidence_sha256: Sha256Digest
    """The digest of the artifact's own bytes, and the third member of the binding.

    Carried separately from ``request_hash`` for the reason the investigation job carries
    ``reason`` separately: the request hash names the command, and the binding names the exact
    work one *invocation* is authorized to do. Deriving one from the other would make a job that
    named a different artifact under a valid-looking request indistinguishable from a correct
    one -- and the model would then read somebody else's email under an invocation identity that
    had already been recorded.
    """

    @property
    def scope(self) -> CaseScope:
        return CaseScope(
            namespace=self.namespace, community_id=self.community_id, case_id=self.case_id
        )


class ExtractCommitmentJobBinding:
    """Why one job did not belong to the operation it named."""

    KIND = "OPERATION_KIND_MISMATCH"
    NAMESPACE = "OPERATION_NAMESPACE_MISMATCH"
    CASE = "OPERATION_CASE_MISMATCH"
    ACTOR = "OPERATION_ACTOR_MISMATCH"
    REQUEST = "OPERATION_REQUEST_MISMATCH"
    INVOCATION = "OPERATION_INVOCATION_MISMATCH"
    BINDING = "OPERATION_BINDING_MISMATCH"
    UNBOUND = "OPERATION_HANDOVER_MISSING"


def extraction_input_hash(payload: CommitmentExtractionInput) -> Sha256Digest:
    """Digest the exact payload the model was given, over the stored text's digest.

    The reply text is hashed rather than absorbed, so this value can be recomputed from the
    immutable evidence during recovery without the recovery path ever holding the text.
    """

    return hash_value(
        {
            "schema": EXTRACTION_INPUT_SCHEMA,
            "case_id": str(payload.case_id),
            "source_evidence_id": str(payload.source_evidence_id),
            "reply_text_sha256": hash_value(payload.reply_text).value,
        }
    )


def extraction_output_hash(output: CommitmentExtractionOutput) -> Sha256Digest:
    """Digest one extraction answer, structurally and without carrying its restatements."""

    return hash_value(
        {
            "schema": EXTRACTION_OUTPUT_SCHEMA,
            "case_id": str(output.case_id),
            "source_evidence_id": str(output.source_evidence_id),
            "commitments": [
                {
                    "obligor_span": [item.obligor_span.start, item.obligor_span.end],
                    "action_span": [item.action_span.start, item.action_span.end],
                    "due_date_span": [item.due_date_span.start, item.due_date_span.end],
                    "obligor_sha256": hash_value(item.obligor).value,
                    "action_text_sha256": hash_value(item.action_text).value,
                    "due_at": item.due_at.isoformat(),
                    "refusal_detected": item.refusal_detected,
                }
                for item in output.commitments
            ],
        }
    )


@dataclass(slots=True)
class ExtractCommitment:
    """Invoke the extraction once over one artifact, then apply what deterministic code allows."""

    core: CoreRepositoryPort
    agent: CommitmentExtractionPort
    apply: ApplyCommitment
    clock: Clock
    policy_version: str

    async def execute(self, job: ExtractCommitmentJob) -> ApplyCommitmentResult:
        artifact = await self._artifact(job)
        payload = CommitmentExtractionInput(
            case_id=job.case_id.value,
            source_evidence_id=job.evidence_id.value,
            reply_text=self._text(artifact),
        )
        input_hash = extraction_input_hash(payload)

        recovered = await self._recovered(job, input_hash)
        if recovered is not None:
            return recovered

        invocation = AgentInputEnvelope[CommitmentExtractionInput](
            schema_version=AGENT_INPUT_SCHEMA_VERSION,
            invocation_id=job.invocation_id,
            namespace=job.namespace.value,
            agent_name=ContractAgentName.INVESTIGATOR,
            # No case on the *envelope*, and deliberately so. An extraction is bound to one
            # immutable artifact rather than to a version of a case, so a ``case_version`` here
            # would be a value nothing checks and everything could drift from. The case
            # identifier the model may read is in the payload, where the output has to echo it
            # back and the envelope guard compares the two.
            case_id=None,
            case_version=None,
            requested_at=self.clock.now(),
            policy_version=self.policy_version,
            payload=payload,
        )
        result = await self.agent.invoke_commitment_extraction(invocation)
        self._require_envelope(job, result)
        return await self.apply.execute(
            ApplyCommitmentCommand(
                namespace=job.namespace,
                community_id=job.community_id,
                case_id=job.case_id,
                action_id=job.action_id,
                evidence_id=job.evidence_id,
                invocation_id=job.invocation_id,
                correlation_id=job.correlation_id,
                actor_id_hash=job.actor_id_hash,
                output=result.output,
                input_hash=input_hash,
                output_hash=extraction_output_hash(result.output),
            )
        )

    # -- guards --------------------------------------------------------------------------

    def _require_envelope(self, job: ExtractCommitmentJob, result: object) -> None:
        """Refuse an answer about a different reply, a different case, or a different prompt.

        These five refuse the whole envelope. They are not the grounding checks: a run that
        answered about somebody else's reply has not made a proposal this system can evaluate,
        so there is nothing to validate proposal by proposal.
        """

        reasons: list[CommitmentExtractionRejection] = []
        prompt_version = getattr(result, "prompt_version", None)
        if prompt_version != COMMITMENT_EXTRACTION_PROMPT_VERSION:
            reasons.append(CommitmentExtractionRejection.PROMPT_VERSION_MISMATCH)
        if getattr(result, "invocation_id", None) != job.invocation_id:
            reasons.append(CommitmentExtractionRejection.ENVELOPE_MISMATCH)
        if getattr(result, "namespace", None) != job.namespace.value:
            reasons.append(CommitmentExtractionRejection.ENVELOPE_MISMATCH)
        output = getattr(result, "output", None)
        if getattr(output, "case_id", None) != job.case_id.value:
            reasons.append(CommitmentExtractionRejection.CASE_MISMATCH)
        if getattr(output, "source_evidence_id", None) != job.evidence_id.value:
            reasons.append(CommitmentExtractionRejection.EVIDENCE_MISMATCH)
        if reasons:
            raise AgentContractViolationError(tuple(dict.fromkeys(reasons)))

    async def _artifact(self, job: ExtractCommitmentJob) -> EvidenceItem:
        items = await self.core.load_evidence_items(job.scope, (job.evidence_id,))
        return items[0]

    @staticmethod
    def _text(artifact: EvidenceItem) -> str:
        return "" if artifact.extracted_text is None else artifact.extracted_text.reveal()

    async def _recovered(
        self, job: ExtractCommitmentJob, input_hash: Sha256Digest
    ) -> ApplyCommitmentResult | None:
        """Answer from the durable invocation record rather than calling the model again.

        The record is proof only when every one of scope, invocation identity, agent, prompt
        version, and the recomputed input hash verifies. A record that agreed about the
        invocation but not about the input would mean the run it describes read something else.
        """

        record = await self.core.load_agent_invocation(job.scope, job.invocation_id)
        if record is None or record.outcome is not AgentInvocationOutcome.SUCCEEDED:
            return None
        if (
            record.prompt_version != COMMITMENT_EXTRACTION_PROMPT_VERSION
            or record.input_hash != input_hash
            or record.case_id != job.case_id
        ):
            return None
        return await self.apply.execute(
            ApplyCommitmentCommand(
                namespace=job.namespace,
                community_id=job.community_id,
                case_id=job.case_id,
                action_id=job.action_id,
                evidence_id=job.evidence_id,
                invocation_id=job.invocation_id,
                correlation_id=job.correlation_id,
                actor_id_hash=job.actor_id_hash,
                output=CommitmentExtractionOutput(
                    case_id=job.case_id.value,
                    source_evidence_id=job.evidence_id.value,
                    commitments=(),
                ),
                input_hash=input_hash,
                # The recorded output hash, so the replay reads the *same* idempotency record
                # its first attempt wrote rather than minting a second one under a fresh digest.
                output_hash=record.output_hash or input_hash,
            )
        )


@dataclass(slots=True)
class ExtractCommitmentOperationWorker:
    """Run one extraction operation to a terminal status.

    The same four jobs the other agent workers own: prove the job belongs to the operation it
    names, claim that operation, run the use case, record an outcome. It decides nothing about
    what a reply means and persists nothing the use case did not.
    """

    operations: ApplicationOperations
    extract: ExtractCommitment

    async def execute(self, job: ExtractCommitmentJob) -> ApplicationOperation:
        with observability.emitting_as(observability.SERVICE_WORKER):
            return await self._execute(job)

    async def _execute(self, job: ExtractCommitmentJob) -> ApplicationOperation:
        operation = await self.operations.load(
            namespace=job.namespace, operation_id=job.operation_id
        )
        mismatches = self._binding_failures(job, operation)
        if mismatches:
            observability.worker_job_mismatch(
                namespace=job.namespace,
                operation_id=job.operation_id,
                invocation_id=job.invocation_id,
                correlation_id=job.correlation_id,
                reason_codes=mismatches,
            )
            return operation
        if self.operations.is_terminal(operation):
            self._emit_replay(job, operation.status.value)
            return operation
        if operation.status is ApplicationOperationStatus.RUNNING:
            self._emit_replay(job, operation.status.value)
            recovered = await self.operations.abandon_if_stale(operation)
            if recovered is not None:
                return recovered
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )

        try:
            claimed = await self.operations.claim(operation)
        except (StateTransitionError, PersistenceError):
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )
        try:
            result = await self.extract.execute(job)
        except (AgentError, PersistenceError, DomainError) as error:
            return await self._settle(job, claimed, error_code=_safe_code(error))
        except Exception:
            # Nothing unmapped may escape into an at-least-once dispatcher: it would be read as
            # "retry me", and a retry here is a second model pass over a stranger's email.
            return await self._settle(job, claimed, error_code=INTERNAL_ERROR_CODE)
        refs = () if result.commitment_id is None else (result.commitment_id.value,)
        try:
            return await self.operations.succeed(claimed, result_refs=refs)
        except (StateTransitionError, PersistenceError):
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )

    def _binding_failures(
        self, job: ExtractCommitmentJob, operation: ApplicationOperation
    ) -> tuple[str, ...]:
        failures: list[str] = []
        if operation.kind is not ApplicationOperationKind.EXTRACT_COMMITMENT:
            failures.append(ExtractCommitmentJobBinding.KIND)
        if operation.namespace != job.namespace or operation.operation_id != job.operation_id:
            failures.append(ExtractCommitmentJobBinding.NAMESPACE)
        if operation.case_id != job.case_id:
            failures.append(ExtractCommitmentJobBinding.CASE)
        if operation.actor_id_hash != job.actor_id_hash:
            failures.append(ExtractCommitmentJobBinding.ACTOR)
        if operation.request_hash != job.request_hash:
            failures.append(ExtractCommitmentJobBinding.REQUEST)
        if failures:
            return tuple(failures)
        if operation.agent_invocation_id is None or operation.agent_binding_hash is None:
            return (ExtractCommitmentJobBinding.UNBOUND,)
        if job.invocation_id != operation.agent_invocation_id:
            failures.append(ExtractCommitmentJobBinding.INVOCATION)
        expected = extract_commitment_binding_hash(
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            evidence_sha256=job.evidence_sha256,
        )
        if expected != operation.agent_binding_hash:
            failures.append(ExtractCommitmentJobBinding.BINDING)
        return tuple(failures)

    async def _settle(
        self, job: ExtractCommitmentJob, claimed: ApplicationOperation, *, error_code: str
    ) -> ApplicationOperation:
        try:
            return await self.operations.fail(claimed, error_code=error_code)
        except (StateTransitionError, PersistenceError):
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )

    def _emit_replay(self, job: ExtractCommitmentJob, outcome: str) -> None:
        observability.lambda_replay(
            namespace=job.namespace,
            community_id=job.community_id,
            operation_id=job.operation_id,
            invocation_id=job.invocation_id,
            correlation_id=job.correlation_id,
            outcome=outcome,
        )


def _safe_code(error: Exception) -> str:
    safe = getattr(error, "safe_code", None)
    if isinstance(safe, str):
        return safe
    code = getattr(error, "code", None)
    value = getattr(code, "value", None)
    return value if isinstance(value, str) else INTERNAL_ERROR_CODE


__all__ = [
    "EXTRACTION_INPUT_SCHEMA",
    "EXTRACTION_OUTPUT_SCHEMA",
    "ExtractCommitment",
    "ExtractCommitmentJob",
    "ExtractCommitmentJobBinding",
    "ExtractCommitmentOperationWorker",
    "extraction_input_hash",
    "extraction_output_hash",
]
