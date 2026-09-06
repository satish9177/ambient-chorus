"""The worker body for one asynchronous ``PROPOSE_ACTION`` operation.

It owns exactly four things: proving the job belongs to the operation it names, claiming that
operation, running the use case, and recording an outcome. It does not decide what a proposal
means and it does not persist anything the use case did not.

Binding before claiming
-----------------------
The same seven facts the Monitor and Investigator workers prove, generalized by ADR-016: kind,
namespace, operation identity, actor hash, request hash, the operation's own recorded
``agent_invocation_id``, and ``agent_binding_hash`` recomputed from the job's own content
through the existing :func:`propose_action_binding_hash`. A mismatch claims nothing, invokes
nothing, and mutates nothing.

The exposure this closes is the same one and it is sharper here, because the invocation
identity is what the durable record is keyed by. A redelivery presenting a fresh identity would
find no invocation record, conclude the proposal had not happened, mint a second action, and
spend a second model pass writing a second candidate message for one case.

No resume path
--------------
A proposal applies in one transaction, so there is nothing to resume: no frozen input, no
validated plan, no apply-progress row, and no ``RUNNING -> PENDING`` edge. Every ending is
``SUCCEEDED`` or ``FAILED``.

Recovery is through the durable invocation record and nothing else. If the apply committed but
the status write was lost, a redelivery reads that record, concludes the apply is already
durable, transitions the operation to ``SUCCEEDED``, and **invokes no model**. That is a
transcription of an outcome that already happened rather than a judgement about one that might
not have.

The record is proof only if it is *this* operation's
------------------------------------------------------
``outcome == SUCCEEDED`` is not provenance. Every recovery path here loads the record through
:meth:`ProposeAction.load_verified_invocation_record`, which proves scope, invocation identity,
agent, prompt artifact, the expected input hash recomputed from the immutable bound view, and an
exact result-reference set before the record is allowed to finish anything. A record that
disagrees fails closed; it never transitions ``RUNNING -> SUCCEEDED``.

An unknown apply outcome is not a failure
-----------------------------------------
``UNKNOWN_TRANSACTION_OUTCOME`` means the ten-participant transaction may or may not have
committed. Settling it ``FAILED`` records the one answer that cannot be revised: the apply may
be fully durable -- proposal, ``DRAFT`` execution, pointers, case transition and all -- and the
operation would then be permanently terminal over state that is complete, for every future
redelivery.

So an ambiguous outcome is never terminal until durable state proves the apply did not commit.
The worker first attempts recovery from the durable proof participants; failing that it leaves
the operation ``RUNNING`` and **recoverable**, invoking no model. A later delivery retries the
*proof read*, never the Action model.

The stale-``RUNNING`` timeout is gated behind the same proof. Time elapsing is not evidence
about a transaction, so a proof read that is itself unavailable leaves the operation alone
rather than letting :meth:`ApplicationOperations.abandon_if_stale` convert an unresolved
outcome into a terminal failure. No extra field records this: the durable invocation record
*is* the distinguishing evidence, and a status flag beside it could only ever agree with it or
be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.application import observability
from chorus.application.commands.propose_action import (
    ProposeAction,
    ProposeActionCommand,
)
from chorus.application.errors import ApplicationError
from chorus.application.operations import ApplicationOperations, propose_action_binding_hash
from chorus.domain.entities import (
    ApplicationOperation,
    ApplicationOperationKind,
    ApplicationOperationStatus,
)
from chorus.domain.errors import DomainError, StateTransitionError
from chorus.ports.agents import AgentError
from chorus.ports.errors import PersistenceError, PersistenceErrorCode
from chorus.ports.operations import ProposeActionOperationJob
from chorus.ports.records import AgentInvocationOutcome
from chorus.ports.scopes import CaseScope

INTERNAL_ERROR_CODE = "INTERNAL_ERROR"


class ProposeActionJobBinding:
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
class ProposeActionOperationWorker:
    """Run one proposal operation to a terminal status."""

    operations: ApplicationOperations
    propose_action: ProposeAction

    async def execute(self, job: ProposeActionOperationJob) -> ApplicationOperation:
        with observability.emitting_as(observability.SERVICE_WORKER):
            return await self._execute(job)

    async def _execute(self, job: ProposeActionOperationJob) -> ApplicationOperation:
        operation = await self.operations.load(
            namespace=job.namespace, operation_id=job.operation_id
        )
        mismatches = self._binding_failures(job, operation)
        if mismatches:
            # Not this worker's operation. Claiming it would let a misrouted delivery end work
            # it knows nothing about, and failing it would be worse: an INVESTIGATE command
            # would be recorded as having failed inside the Action path.
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

        command = self._command(job)
        try:
            result = await self.propose_action.execute(command)
        except PersistenceError as error:
            if error.code is PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME:
                # The apply may already be durable. A terminal failure here is the one answer
                # that cannot be revised, so it is not taken until something proves the
                # transaction did not commit.
                return await self._resolve_unknown_outcome(job, claimed, command)
            return await self._settle(job, claimed, error_code=_safe_code(error))
        except (AgentError, DomainError, ApplicationError) as error:
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

    @staticmethod
    def _command(job: ProposeActionOperationJob) -> ProposeActionCommand:
        """The one command this job authorizes, built in exactly one place.

        Recovery derives its expected input hash from the same ``{view_id, view_hash}`` the
        execution path would have used, so a worker cannot verify a record against a different
        view than the one it would have proposed against.
        """

        return ProposeActionCommand(
            namespace=job.namespace,
            community_id=job.community_id,
            case_id=job.case_id,
            operation_id=job.operation_id.value,
            invocation_id=job.invocation_id,
            correlation_id=job.correlation_id,
            actor_id_hash=job.actor_id_hash,
            expected_case_version=job.expected_case_version,
            view_id=job.view_id,
            view_hash=job.view_hash,
            idempotency_key=job.idempotency_key,
        )

    # -- binding --------------------------------------------------------------------------

    def _binding_failures(
        self, job: ProposeActionOperationJob, operation: ApplicationOperation
    ) -> tuple[str, ...]:
        """Every way this job disagrees with the durable operation it claims to be about.

        All of them are reported rather than the first, because a misrouted job is a routing
        defect and an operator fixing it wants the whole disagreement rather than one symptom at
        a time.

        The binding hash is recomputed from the job's own ``{case_id, view_id, view_hash}``
        through the existing frozen helper, and compared with the value the operation has
        carried since it was created. A copied request hash is not enough: it names the
        *command*, while the binding names the exact view one invocation is authorized to
        propose against -- and those differ precisely where a redelivery could otherwise
        substitute a newer or older view under a valid-looking request.
        """

        failures: list[str] = []
        if operation.kind is not ApplicationOperationKind.PROPOSE_ACTION:
            failures.append(ProposeActionJobBinding.KIND)
        if operation.namespace != job.namespace or operation.operation_id != job.operation_id:
            failures.append(ProposeActionJobBinding.NAMESPACE)
        if operation.case_id != job.case_id:
            failures.append(ProposeActionJobBinding.CASE)
        if operation.actor_id_hash != job.actor_id_hash:
            failures.append(ProposeActionJobBinding.ACTOR)
        if operation.request_hash != job.request_hash:
            failures.append(ProposeActionJobBinding.REQUEST)
        if failures:
            # The operation is not this job's, so its handover is not this job's to read either.
            return tuple(failures)
        if operation.agent_invocation_id is None or operation.agent_binding_hash is None:
            # An agent-invoking operation without a handover identity cannot authorize
            # anything. Refused rather than trusted.
            return (ProposeActionJobBinding.UNBOUND,)
        if job.invocation_id != operation.agent_invocation_id:
            failures.append(ProposeActionJobBinding.INVOCATION)
        expected = propose_action_binding_hash(
            case_id=job.case_id, view_id=job.view_id.value, view_hash=job.view_hash
        )
        if expected != operation.agent_binding_hash:
            failures.append(ProposeActionJobBinding.BINDING)
        return tuple(failures)

    # -- outcomes -------------------------------------------------------------------------

    async def _handle_running(
        self, job: ProposeActionOperationJob, operation: ApplicationOperation
    ) -> ApplicationOperation:
        """Finish a finished one, leave a live one alone, and end a lost one exactly once.

        The stale timeout is deliberately reached only through a proof read that *succeeded and
        found nothing*. An unavailable read leaves the apply outcome unresolved, and elapsed
        time is not evidence about a transaction -- so the operation stays ``RUNNING`` and a
        later delivery retries the proof rather than the model.
        """

        try:
            finished = await self._finish_if_recorded(job, operation)
        except PersistenceError as error:
            if _is_unresolved(error):
                self._emit_unresolved(job)
                return operation
            raise
        if finished is not None:
            return finished
        recovered = await self.operations.abandon_if_stale(operation)
        if recovered is not None:
            return recovered
        return await self.operations.load(namespace=job.namespace, operation_id=job.operation_id)

    async def _resolve_unknown_outcome(
        self,
        job: ProposeActionOperationJob,
        claimed: ApplicationOperation,
        command: ProposeActionCommand,
    ) -> ApplicationOperation:
        """Answer an ambiguous apply from durable proof, or keep the operation recoverable.

        Three outcomes, and the third is the repair:

        * the durable ``ACTION`` invocation record proves the apply committed -- settle
          ``SUCCEEDED``, with **zero** model calls;
        * a successful proof read finds no record, which proves the apply did not commit --
          settle ``FAILED`` under the frozen safe code, exactly as any other definite failure;
        * the proof read is itself unavailable -- leave the operation ``RUNNING`` and
          recoverable. It is not settled, not released to ``PENDING``, and the model is not
          invoked. A later delivery retries the **proof read**.
        """

        scope = CaseScope(
            namespace=job.namespace, community_id=job.community_id, case_id=job.case_id
        )
        try:
            record = await self.propose_action.load_verified_invocation_record(command, scope)
        except PersistenceError as error:
            if not _is_unresolved(error):
                raise
            self._emit_unresolved(job)
            return claimed
        if record is None:
            # A strong read that found nothing is proof of non-commit: participant 6 commits
            # inside the same transaction as the proposal, so its absence is the transaction's
            # absence.
            return await self._settle(
                job, claimed, error_code=PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME.value
            )
        if record.outcome is not AgentInvocationOutcome.SUCCEEDED:
            return await self._settle(
                job, claimed, error_code=record.failure_code or INTERNAL_ERROR_CODE
            )
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
                claimed, result_refs=tuple(ref.entity_id for ref in record.result_refs)
            )
        except (StateTransitionError, PersistenceError):
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )

    def _emit_unresolved(self, job: ProposeActionOperationJob) -> None:
        """Record that this delivery ended with the apply outcome still unresolved."""

        observability.lambda_replay(
            namespace=job.namespace,
            community_id=job.community_id,
            operation_id=job.operation_id,
            invocation_id=job.invocation_id,
            correlation_id=job.correlation_id,
            outcome=PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME.value,
        )

    async def _finish_if_recorded(
        self, job: ProposeActionOperationJob, operation: ApplicationOperation
    ) -> ApplicationOperation | None:
        """Transition an operation whose apply already committed to ``SUCCEEDED``.

        The evidence is participant 6 of the apply transaction -- the durable successful
        invocation record, committed atomically with the proposal, the ``DRAFT`` execution, the
        pointers, and the case transition. Its presence means the whole apply is durable and the
        only thing missing is the status write.

        **Zero model calls.** That is the entire reason the record is inside the transaction
        rather than written beside it: without it, a lost status write would leave an operation
        that looks unfinished over state that is complete, and the only way to find out would be
        a second model pass over the same view.
        """

        scope = CaseScope(
            namespace=job.namespace, community_id=job.community_id, case_id=job.case_id
        )
        # Loaded *and verified*: scope, invocation identity, agent, prompt artifact, the input
        # hash recomputed from the immutable bound view, and an exact result-reference set. A
        # record that disagrees raises rather than finishing somebody else's operation.
        record = await self.propose_action.load_verified_invocation_record(
            self._command(job), scope
        )
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
        job: ProposeActionOperationJob,
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

    def _emit_replay(self, job: ProposeActionOperationJob, outcome: str) -> None:
        observability.lambda_replay(
            namespace=job.namespace,
            community_id=job.community_id,
            operation_id=job.operation_id,
            invocation_id=job.invocation_id,
            correlation_id=job.correlation_id,
            outcome=outcome,
        )


def _is_unresolved(error: PersistenceError) -> bool:
    """Whether this storage failure leaves an outcome unproven rather than answering it.

    ``UNKNOWN_TRANSACTION_OUTCOME`` is the explicit case. ``DEPENDENCY_UNAVAILABLE`` and
    ``DEPENDENCY_REJECTED`` are the reads that could not be performed at all, which is the same
    epistemic state: nothing was established, so nothing may be concluded.
    """

    return error.code in {
        PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME,
        PersistenceErrorCode.DEPENDENCY_UNAVAILABLE,
        PersistenceErrorCode.DEPENDENCY_REJECTED,
    }


def _safe_code(error: Exception) -> str:
    """The closed code an operation record may carry for one failure."""

    safe = getattr(error, "safe_code", None)
    if isinstance(safe, str):
        return safe
    code = getattr(error, "code", None)
    value = getattr(code, "value", None)
    return value if isinstance(value, str) else INTERNAL_ERROR_CODE


__all__ = ["ProposeActionJobBinding", "ProposeActionOperationWorker"]
