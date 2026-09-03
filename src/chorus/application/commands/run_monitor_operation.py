"""The worker body for one asynchronous Monitor operation.

This is what a Lambda handler will call, and what a local in-process dispatcher calls today.
It owns exactly four things: proving the job belongs to the operation it names, claiming that
operation, running the use case, and recording an outcome. It does not decide what a Monitor
answer means and it does not persist anything the use case did not.

Binding before claiming
-----------------------
A job arrives as data on a queue, and data on a queue can be wrong: a misrouted delivery, a
replayed message from another command family, a job whose operation identity does not match
its content. So the durable operation is loaded and *bound* to the job before anything is
claimed -- kind, namespace, operation identity, actor, request hash, the invocation the
operation authorizes, and the digest of the exact new-message set it authorizes. A mismatch
claims nothing, invokes nothing, and mutates nothing: the operation is somebody else's and
this worker has no business ending it.

There is deliberately no "nothing has been written yet, so accept it" case. That gap used to
exist, and it was the whole hole: on a *first* delivery the operation had no durable records
to contradict, so any invocation identity and any subset of the locators were taken on trust,
and a caller who retained a valid request hash could still steer which messages the Monitor
was given. The operation row now carries its handover identity from the moment it is created,
before dispatch and before the first model call, so the first delivery is bound exactly as
tightly as the hundredth.

Delivery may repeat, and that is handled by the claim rather than by hope. An operation
already terminal is returned unchanged; a concurrent claim loses on the version condition and
stops instead of invoking the model a second time over the same private payload.

A ``RUNNING`` operation is usually ambiguous: the worker that claimed it may be mid-invocation,
or may have died after calling the model. Two rules cover it, and the first removes most of the
ambiguity outright.

If a **successful invocation record** exists for this operation's invocation, the run is over.
The finalization step writes that record in the same transaction that completes apply progress,
so its presence means every apply step and the record itself are durable, and the only thing
missing is the status write. That is transcribed to ``SUCCEEDED`` immediately, fresh claim or
stale -- there is nothing to wait for, no model call, and no mutation beyond the status. Ageing
such an operation into ``FAILED`` was the defect: it recorded finished work as failed, and did
it to exactly the runs that had completed everything and then lost one response.

Otherwise the original conservative rule stands, one-directional as before. While the claim is
fresh, a redelivery does nothing at all. Once it is older than any legitimate worker could still
be running for, a redelivery may record that the attempt is over -- and only that. It never
starts a second invocation from an ambiguous state, because "the worker vanished" and "the
worker already called the model" look identical from here, and one of those readings costs a
duplicate pass over private text.

Interruption is not failure
---------------------------
A storage failure part-way through a *frozen* apply plan is different in kind from every other
failure here, and it is the one case that must not end the operation. The model has already
answered, the answer is snapshotted, and some steps are durably committed. So the operation
goes back to ``PENDING`` and a redelivery finishes the remainder with zero model calls.
Recording it as ``FAILED`` would abandon valid committed state and leave the rest reachable
only by a human minting a new invocation.

The opposite case still fails, and says so precisely: a frozen plan that can no longer legally
finish -- a case moved to a version the plan does not expect -- settles as ``FAILED`` with
``PARTIAL_APPLY_CONFLICT``, which does not pretend the operation was atomic.

Failures are recorded, never re-raised at the caller. A worker that propagated an exception to
an at-least-once dispatcher would be asking to be retried by whatever invoked it, which is the
one thing an agent invocation over private text must not do implicitly. That includes failures
nobody anticipated: an unmapped exception settles the operation rather than escaping and
stranding it in ``RUNNING`` forever.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.application import observability
from chorus.application.commands.run_monitor import (
    MonitorApplyInterruptedError,
    RunMonitor,
    RunMonitorCommand,
)
from chorus.application.operations import ApplicationOperations, monitor_locator_hash
from chorus.domain.entities import (
    ApplicationOperation,
    ApplicationOperationKind,
    ApplicationOperationStatus,
)
from chorus.domain.errors import DomainError, StateTransitionError
from chorus.ports.agents import AgentError
from chorus.ports.errors import PersistenceError
from chorus.ports.operations import MonitorOperationJob
from chorus.ports.records import AgentInvocationOutcome
from chorus.ports.scopes import OperationScope

INTERNAL_ERROR_CODE = "INTERNAL_ERROR"


class JobBinding:
    """Why one job did not belong to the operation it named."""

    KIND = "OPERATION_KIND_MISMATCH"
    NAMESPACE = "OPERATION_NAMESPACE_MISMATCH"
    ACTOR = "OPERATION_ACTOR_MISMATCH"
    REQUEST = "OPERATION_REQUEST_MISMATCH"
    INVOCATION = "OPERATION_INVOCATION_MISMATCH"
    LOCATORS = "OPERATION_LOCATOR_MISMATCH"
    UNBOUND = "OPERATION_HANDOVER_MISSING"


@dataclass(slots=True)
class MonitorOperationWorker:
    """Run one Monitor operation to a terminal status, or hand it back to be resumed."""

    operations: ApplicationOperations
    run_monitor: RunMonitor

    async def execute(self, job: MonitorOperationJob) -> ApplicationOperation:
        with observability.emitting_as(observability.SERVICE_WORKER):
            return await self._execute(job)

    async def _execute(self, job: MonitorOperationJob) -> ApplicationOperation:
        operation = await self.operations.load(
            namespace=job.namespace, operation_id=job.operation_id
        )
        mismatches = await self._binding_failures(job, operation)
        if mismatches:
            # Not this worker's operation. Claiming it would let a misrouted delivery end work
            # it knows nothing about, and failing it would be worse: a PROPOSE_ACTION command
            # would be recorded as having failed inside the Monitor.
            observability.worker_job_mismatch(
                namespace=job.namespace,
                operation_id=job.operation_id,
                invocation_id=job.invocation_id,
                correlation_id=job.correlation_id,
                reason_codes=mismatches,
            )
            return operation

        if self.operations.is_terminal(operation):
            # A completed operation is the authoritative outcome. Re-running it would produce
            # a second invocation of the model for work already recorded as finished.
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

        command = RunMonitorCommand(
            namespace=job.namespace,
            community_id=job.community_id,
            operation_id=job.operation_id,
            invocation_id=job.invocation_id,
            correlation_id=job.correlation_id,
            actor_id_hash=job.actor_id_hash,
            message_locators=job.message_locators,
        )
        try:
            result = await self.run_monitor.execute(command)
        except MonitorApplyInterruptedError as error:
            return await self._release(job, claimed, reason_code=error.safe_code)
        except (AgentError, PersistenceError, DomainError) as error:
            return await self._settle(job, claimed, error_code=_safe_code(error))
        except Exception:
            # Nothing unmapped may escape into an at-least-once dispatcher: it would be read
            # as "retry me", and the operation would sit in RUNNING until it went stale.
            return await self._settle(job, claimed, error_code=INTERNAL_ERROR_CODE)
        if result.noop_reason_code is not None:
            observability.monitor_batch_noop(
                namespace=job.namespace,
                community_id=job.community_id,
                operation_id=job.operation_id,
                invocation_id=job.invocation_id,
                correlation_id=job.correlation_id,
                reason_code=result.noop_reason_code,
            )
        try:
            return await self.operations.succeed(claimed, result_refs=result.result_refs)
        except (StateTransitionError, PersistenceError):
            # Either somebody already wrote a terminal status for this attempt -- a
            # stale-recovery worker, most likely, whose record stands -- or this write's own
            # outcome was lost. A strong read settles which, and a still-``RUNNING`` operation
            # whose finalization is durable is finished here rather than left for the queue.
            reloaded = await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )
            if reloaded.status is not ApplicationOperationStatus.RUNNING:
                return reloaded
            finalized = await self._finalize_if_complete(job, reloaded)
            return reloaded if finalized is None else finalized

    # -- binding ------------------------------------------------------------------------

    async def _binding_failures(
        self, job: MonitorOperationJob, operation: ApplicationOperation
    ) -> tuple[str, ...]:
        """Every way this job disagrees with the durable operation it claims to be about.

        All of them are reported rather than the first one, because a misrouted job is a
        routing defect and an operator fixing it wants the whole disagreement rather than one
        symptom at a time.

        The invocation and the locator set are checked against the operation row itself,
        which carries both from creation. A copied request hash is not enough on its own: it
        names the *command*, and a caller can retain a valid one while delivering a different
        message set under it, so the exact authorized set is bound by digest as well.

        The operation's own durable records are then checked as a second, independent witness.
        A frozen input, a validated plan, an apply-progress row, or an invocation result under
        this operation all name the invocation that owns it, so a record disagreeing with the
        row would be an integrity failure rather than a routing one -- and either way this job
        does not get to proceed on it.
        """

        failures: list[str] = []
        if operation.kind is not ApplicationOperationKind.MONITOR:
            failures.append(JobBinding.KIND)
        if operation.namespace != job.namespace or operation.operation_id != job.operation_id:
            failures.append(JobBinding.NAMESPACE)
        if operation.actor_id_hash != job.actor_id_hash:
            failures.append(JobBinding.ACTOR)
        if operation.request_hash != job.request_hash:
            failures.append(JobBinding.REQUEST)
        if failures:
            # The operation is not this job's, so its private records are not this job's to
            # read either.
            return tuple(failures)
        if operation.monitor_invocation_id is None or operation.monitor_locator_hash is None:
            # A MONITOR operation without a handover identity cannot authorize anything. It is
            # refused rather than trusted: the alternative is exactly the gap this field pair
            # was added to close.
            return (JobBinding.UNBOUND,)
        if job.invocation_id != operation.monitor_invocation_id:
            failures.append(JobBinding.INVOCATION)
        if monitor_locator_hash(job.message_locators) != operation.monitor_locator_hash:
            failures.append(JobBinding.LOCATORS)
        if failures:
            return tuple(failures)
        if not await self._records_agree(job):
            failures.append(JobBinding.INVOCATION)
        return tuple(failures)

    async def _records_agree(self, job: MonitorOperationJob) -> bool:
        """True unless a durable record under this operation names another invocation.

        Redundant with the operation row by design, and cheap: one bounded query on a partition
        the caller already holds. The row is authoritative and immutable, so the two can only
        disagree if something wrote a record under an invocation the operation never authorized
        -- which is not a delivery this worker should continue from either.
        """

        recorded = await self.operations.core.load_operation_invocation_ids(
            OperationScope(namespace=job.namespace, operation_id=job.operation_id)
        )
        return all(invocation_id == job.invocation_id for invocation_id in recorded)

    # -- outcomes -----------------------------------------------------------------------

    async def _handle_running(
        self, job: MonitorOperationJob, operation: ApplicationOperation
    ) -> ApplicationOperation:
        """Finish a finished one, leave a live one alone, and end a lost one exactly once.

        The order matters and the first branch is the one that was missing. A ``RUNNING``
        operation whose finalization step is durable is not ambiguous at all: the model
        answered, the plan applied, and the invocation record says so. The only thing that did
        not happen is the status write. Ageing that into ``FAILED`` would record completed,
        correct, durable work as a failure, and would do it precisely to the operations that
        succeeded hardest -- the ones that finished everything and then lost one response.

        Finishing it costs nothing and risks nothing: no model call, no apply, no new mutation,
        just the conditional transition the lost worker did not get to make. So it is done
        whether the claim is fresh or stale, because there is nothing left to wait for.

        Only when no finalized invocation exists does the original conservative rule apply --
        inside the execution window, do nothing; past it, record that the attempt is over.
        """

        finalized = await self._finalize_if_complete(job, operation)
        if finalized is not None:
            return finalized
        recovered = await self.operations.abandon_if_stale(operation)
        if recovered is not None:
            return recovered
        return await self.operations.load(namespace=job.namespace, operation_id=job.operation_id)

    async def _finalize_if_complete(
        self, job: MonitorOperationJob, operation: ApplicationOperation
    ) -> ApplicationOperation | None:
        """Transition an already-finalized operation to ``SUCCEEDED``, or return ``None``.

        The evidence is the durable successful invocation record, which the finalization step
        commits together with the progress row that completes the plan. Its presence is what
        makes this transition a *transcription* of an outcome that already happened rather than
        a judgement about one that might not have.

        A lost transition response is handled by the same code path as a lost worker, and both
        end at a strong read: whoever won the conditional write, the operation this returns is
        the one storage actually holds.
        """

        scope = OperationScope(namespace=job.namespace, operation_id=job.operation_id)
        record = await self.operations.core.load_operation_agent_invocation(
            scope, job.invocation_id
        )
        if record is None or record.outcome is not AgentInvocationOutcome.SUCCEEDED:
            return None
        progress = await self.operations.core.load_monitor_progress(scope, job.invocation_id)
        steps = 0 if progress is None else progress.total_steps
        observability.operation_resumed(
            namespace=job.namespace,
            community_id=job.community_id,
            operation_id=job.operation_id,
            invocation_id=job.invocation_id,
            correlation_id=job.correlation_id,
            completed_steps=0 if progress is None else progress.completed_steps,
            total_steps=steps,
        )
        try:
            return await self.operations.succeed(
                operation, result_refs=tuple(ref.entity_id for ref in record.result_refs)
            )
        except (StateTransitionError, PersistenceError):
            # Somebody else made the transition, or the row moved underneath this one. The
            # durable status is whatever storage now holds, never what this worker intended.
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )

    async def _release(
        self, job: MonitorOperationJob, claimed: ApplicationOperation, *, reason_code: str
    ) -> ApplicationOperation:
        """Hand an interrupted frozen plan back to the queue instead of ending it."""

        observability.operation_resume_scheduled(
            namespace=job.namespace,
            community_id=job.community_id,
            operation_id=job.operation_id,
            invocation_id=job.invocation_id,
            correlation_id=job.correlation_id,
            reason_code=reason_code,
        )
        try:
            return await self.operations.release_for_resume(claimed)
        except (StateTransitionError, PersistenceError):
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )

    async def _settle(
        self, job: MonitorOperationJob, claimed: ApplicationOperation, *, error_code: str
    ) -> ApplicationOperation:
        try:
            return await self.operations.fail(claimed, error_code=error_code)
        except (StateTransitionError, PersistenceError):
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )

    def _emit_replay(self, job: MonitorOperationJob, outcome: str) -> None:
        observability.lambda_replay(
            namespace=job.namespace,
            community_id=job.community_id,
            operation_id=job.operation_id,
            invocation_id=job.invocation_id,
            correlation_id=job.correlation_id,
            outcome=outcome,
        )


def _safe_code(error: Exception) -> str:
    """The closed code an operation record may carry for one failure.

    A gate denial and a partial-apply conflict both map onto broad domain error codes, and
    recording only those would tell an operator that "a state transition was refused" when the
    answerable question is *which* refusal it was. Errors that name a more specific safe code
    are asked for it; everything else falls back to its closed taxonomy code.
    """

    safe = getattr(error, "safe_code", None)
    if isinstance(safe, str):
        return safe
    code = getattr(error, "code", None)
    value = getattr(code, "value", None)
    return value if isinstance(value, str) else INTERNAL_ERROR_CODE
