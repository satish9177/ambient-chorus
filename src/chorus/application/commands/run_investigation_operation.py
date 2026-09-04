"""The worker body for one asynchronous investigation operation.

It owns exactly four things: proving the job belongs to the operation it names, claiming that
operation, running the use case, and recording an outcome. It does not decide what an
assessment means and it does not persist anything the use case did not.

Binding before claiming
-----------------------
The same seven facts the Monitor worker proves, generalized by ADR-016 and preserved in
substance: kind, namespace, operation identity, actor hash, request hash, the operation's own
recorded ``agent_invocation_id``, and ``agent_binding_hash`` recomputed from the job's own
content. A mismatch claims nothing, invokes nothing, and mutates nothing.

The exposure this closes is narrower than the Monitor's and it is real. An investigation job
cannot steer *what* the Investigator is shown -- the payload is assembled by the use case from
storage -- but until ADR-016 the invocation identity itself was unbound, and the pre-invocation
replay check reads the durable record **by invocation identity**. A redelivery presenting a
fresh identity would find no record, conclude the run had not happened, and spend a second model
pass over the same private case.

No resume path
--------------
An investigation applies in one transaction, so there is nothing to resume: no frozen input, no
validated plan, no apply-progress row, and no ``RUNNING -> PENDING`` edge. Every ending is
``SUCCEEDED`` or ``FAILED``.

A ``RUNNING`` operation is handled by the same one-directional rule the Monitor uses. Inside the
execution window a redelivery does nothing at all; past it, a redelivery may record that the
attempt is over, and only that. It never starts a second invocation from an ambiguous state,
because "the worker vanished" and "the worker already called the model" look identical from
here and one of those readings costs a duplicate pass over a private case.

Failures are recorded, never re-raised at the caller. A worker that propagated an exception to
an at-least-once dispatcher would be asking to be retried by whatever invoked it, which is the
one thing an agent invocation over private text must not do implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.application import observability
from chorus.application.commands.run_investigation import (
    InvestigationReason,
    RunInvestigation,
    RunInvestigationCommand,
)
from chorus.application.operations import ApplicationOperations, investigate_binding_hash
from chorus.domain.entities import (
    ApplicationOperation,
    ApplicationOperationKind,
    ApplicationOperationStatus,
)
from chorus.domain.errors import DomainError, StateTransitionError
from chorus.ports.agents import AgentError
from chorus.ports.errors import PersistenceError
from chorus.ports.operations import InvestigationOperationJob
from chorus.ports.records import AgentInvocationOutcome
from chorus.ports.scopes import CaseScope

INTERNAL_ERROR_CODE = "INTERNAL_ERROR"


class InvestigationJobBinding:
    """Why one job did not belong to the operation it named."""

    KIND = "OPERATION_KIND_MISMATCH"
    NAMESPACE = "OPERATION_NAMESPACE_MISMATCH"
    CASE = "OPERATION_CASE_MISMATCH"
    ACTOR = "OPERATION_ACTOR_MISMATCH"
    REQUEST = "OPERATION_REQUEST_MISMATCH"
    INVOCATION = "OPERATION_INVOCATION_MISMATCH"
    BINDING = "OPERATION_BINDING_MISMATCH"
    UNBOUND = "OPERATION_HANDOVER_MISSING"


@dataclass(slots=True)
class InvestigationOperationWorker:
    """Run one investigation operation to a terminal status."""

    operations: ApplicationOperations
    run_investigation: RunInvestigation

    async def execute(self, job: InvestigationOperationJob) -> ApplicationOperation:
        with observability.emitting_as(observability.SERVICE_WORKER):
            return await self._execute(job)

    async def _execute(self, job: InvestigationOperationJob) -> ApplicationOperation:
        operation = await self.operations.load(
            namespace=job.namespace, operation_id=job.operation_id
        )
        mismatches = self._binding_failures(job, operation)
        if mismatches:
            # Not this worker's operation. Claiming it would let a misrouted delivery end work
            # it knows nothing about, and failing it would be worse: a MONITOR command would be
            # recorded as having failed inside the Investigator.
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
            return await self._handle_running(job, operation)

        try:
            claimed = await self.operations.claim(operation)
        except (StateTransitionError, PersistenceError):
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )

        command = RunInvestigationCommand(
            namespace=job.namespace,
            community_id=job.community_id,
            case_id=job.case_id,
            operation_id=job.operation_id,
            invocation_id=job.invocation_id,
            correlation_id=job.correlation_id,
            actor_id_hash=job.actor_id_hash,
            expected_case_version=job.expected_case_version,
            reason=InvestigationReason(job.reason),
            idempotency_key=job.idempotency_key,
        )
        try:
            result = await self.run_investigation.execute(command)
        except (AgentError, PersistenceError, DomainError) as error:
            return await self._settle(job, claimed, error_code=_safe_code(error))
        except Exception:
            # Nothing unmapped may escape into an at-least-once dispatcher: it would be read as
            # "retry me", and the operation would sit in RUNNING until it went stale.
            return await self._settle(job, claimed, error_code=INTERNAL_ERROR_CODE)
        try:
            return await self.operations.succeed(claimed, result_refs=result.result_refs)
        except (StateTransitionError, PersistenceError):
            reloaded = await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )
            if reloaded.status is not ApplicationOperationStatus.RUNNING:
                return reloaded
            finished = await self._finish_if_recorded(job, reloaded)
            return reloaded if finished is None else finished

    # -- binding --------------------------------------------------------------------------

    def _binding_failures(
        self, job: InvestigationOperationJob, operation: ApplicationOperation
    ) -> tuple[str, ...]:
        """Every way this job disagrees with the durable operation it claims to be about.

        All of them are reported rather than the first one, because a misrouted job is a
        routing defect and an operator fixing it wants the whole disagreement rather than one
        symptom at a time.

        The binding hash is **recomputed from the job's own fields** and compared with the value
        the operation has carried since it was created. A copied request hash is not enough on
        its own: it names the *command*, while the binding names the exact work one invocation
        is authorized to do, and the two differ precisely where a redelivery could otherwise
        substitute a different case version or reason under a valid-looking request.
        """

        failures: list[str] = []
        if operation.kind is not ApplicationOperationKind.INVESTIGATE:
            failures.append(InvestigationJobBinding.KIND)
        if operation.namespace != job.namespace or operation.operation_id != job.operation_id:
            failures.append(InvestigationJobBinding.NAMESPACE)
        if operation.case_id != job.case_id:
            failures.append(InvestigationJobBinding.CASE)
        if operation.actor_id_hash != job.actor_id_hash:
            failures.append(InvestigationJobBinding.ACTOR)
        if operation.request_hash != job.request_hash:
            failures.append(InvestigationJobBinding.REQUEST)
        if failures:
            # The operation is not this job's, so its handover is not this job's to read either.
            return tuple(failures)
        if operation.agent_invocation_id is None or operation.agent_binding_hash is None:
            # An agent-invoking operation without a handover identity cannot authorize
            # anything. It is refused rather than trusted: the alternative is exactly the gap
            # this field pair was generalized to close.
            return (InvestigationJobBinding.UNBOUND,)
        if job.invocation_id != operation.agent_invocation_id:
            failures.append(InvestigationJobBinding.INVOCATION)
        expected = investigate_binding_hash(
            case_id=job.case_id,
            expected_case_version=job.expected_case_version,
            reason=job.reason,
        )
        if expected != operation.agent_binding_hash:
            failures.append(InvestigationJobBinding.BINDING)
        return tuple(failures)

    # -- outcomes -------------------------------------------------------------------------

    async def _handle_running(
        self, job: InvestigationOperationJob, operation: ApplicationOperation
    ) -> ApplicationOperation:
        """Finish a finished one, leave a live one alone, and end a lost one exactly once."""

        finished = await self._finish_if_recorded(job, operation)
        if finished is not None:
            return finished
        recovered = await self.operations.abandon_if_stale(operation)
        if recovered is not None:
            return recovered
        return await self.operations.load(namespace=job.namespace, operation_id=job.operation_id)

    async def _finish_if_recorded(
        self, job: InvestigationOperationJob, operation: ApplicationOperation
    ) -> ApplicationOperation | None:
        """Transition an operation whose apply already committed to ``SUCCEEDED``.

        The evidence is the durable successful invocation record, which the apply transaction
        commits together with the assessment and the case update. Its presence means the whole
        apply is durable and the only thing missing is the status write, so this is a
        *transcription* of an outcome that already happened rather than a judgement about one
        that might not have.
        """

        scope = CaseScope(
            namespace=job.namespace, community_id=job.community_id, case_id=job.case_id
        )
        record = await self.operations.core.load_agent_invocation(scope, job.invocation_id)
        if record is None or record.outcome is not AgentInvocationOutcome.SUCCEEDED:
            return None
        observability.operation_resumed(
            namespace=job.namespace,
            community_id=job.community_id,
            operation_id=job.operation_id,
            invocation_id=job.invocation_id,
            correlation_id=job.correlation_id,
            completed_steps=1,
            total_steps=1,
        )
        try:
            return await self.operations.succeed(
                operation, result_refs=tuple(ref.entity_id for ref in record.result_refs)
            )
        except (StateTransitionError, PersistenceError):
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )

    async def _settle(
        self,
        job: InvestigationOperationJob,
        claimed: ApplicationOperation,
        *,
        error_code: str,
    ) -> ApplicationOperation:
        try:
            return await self.operations.fail(claimed, error_code=error_code)
        except (StateTransitionError, PersistenceError):
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )

    def _emit_replay(self, job: InvestigationOperationJob, outcome: str) -> None:
        observability.lambda_replay(
            namespace=job.namespace,
            community_id=job.community_id,
            operation_id=job.operation_id,
            invocation_id=job.invocation_id,
            correlation_id=job.correlation_id,
            outcome=outcome,
        )


def _safe_code(error: Exception) -> str:
    """The closed code an operation record may carry for one failure."""

    safe = getattr(error, "safe_code", None)
    if isinstance(safe, str):
        return safe
    code = getattr(error, "code", None)
    value = getattr(code, "value", None)
    return value if isinstance(value, str) else INTERNAL_ERROR_CODE
