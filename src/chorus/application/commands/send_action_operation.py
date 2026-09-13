"""The worker body for one asynchronous ``SEND_ACTION`` operation.

It owns four things: proving the job belongs to the operation it names, claiming that
operation, running the send, and projecting the outcome onto the private case. It decides
nothing about whether a message may be sent -- that is entirely the send command's and the
compiler's -- and it never calls SES itself.

Binding without a handover
--------------------------
``SEND_ACTION`` invokes no agent, so there is no ``agent_invocation_id`` and no
``agent_binding_hash`` to check, and an operation of this kind carrying one is refused at
construction (ADR-016). The binding is the ordinary set: kind, namespace, operation identity,
actor hash, and request hash.

That is *sufficient here* for a reason that does not apply to the agent workers. A misrouted or
replayed send job cannot cause a second external message, because the send command's first act
is to read the execution and the frozen replay table permits an SES call from exactly one state.
The expensive thing an unbound agent job could do -- spend a second model pass over private text
-- has no analogue on this path.

Why an unknown outcome is never a failed operation
---------------------------------------------------
An ambiguous send is a *successful* operation reporting a ``SEND_UNKNOWN`` execution. Recording
the operation ``FAILED`` would invite the one reading of the record this phase must never
support: that something went wrong and should be tried again. The execution row carries the
uncertainty, where it is visible, alarmed, and quarantined from every retry path.

Recovery, and the three things it is allowed to do
---------------------------------------------------
Recovery reads the **execution** and lets it decide, because the operation record answers a
different question than the one that matters:

1. ``APPROVED`` -- no claim ever committed, so the attempt is unfinished rather than done, and
   the frozen recovery table permits retrying the same claim under the same key. This branch
   used to fall through and leave the operation ``RUNNING`` forever beside an approved message
   nothing would ever send.
2. ``SENDING`` past the recovery window with no live fence -- ``ReconcileSendOutcome``
   quarantines it. Nothing is sent.
3. a terminal execution whose case projection is still owed -- the projection is repaired, and
   the operation is not allowed to report success until it lands. A projection failure used to
   be swallowed into a ``SUCCEEDED`` record, and every later delivery returned early on that
   status, leaving a ``SENT`` execution beside an ``ACTION_PROPOSED`` case permanently.

That is the entire recovery capability on this path. It never calls the Action model, never
creates a proposal, never mutates approved content, and **never issues another SES request after
a possible prior attempt**.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.application import observability
from chorus.application.commands.project_action_outcome import (
    ProjectActionOutcome,
    ProjectActionOutcomeCommand,
)
from chorus.application.commands.reconcile_send_outcome import (
    ReconcileSendOutcome,
    ReconcileSendOutcomeCommand,
    ReconciliationRefusedError,
)
from chorus.application.commands.send_action import (
    SendActionCommand,
    SendActionResult,
    SendDeniedError,
)
from chorus.application.errors import ApplicationError
from chorus.application.operations import ApplicationOperations
from chorus.application.send_contract import SendActionRunner
from chorus.domain.entities import (
    ActionExecutionState,
    ApplicationOperation,
    ApplicationOperationKind,
    ApplicationOperationStatus,
)
from chorus.domain.errors import DomainError, StateTransitionError
from chorus.ports.errors import PersistenceError, PersistenceErrorCode
from chorus.ports.operations import SendActionOperationJob
from chorus.ports.repositories import ShareableRepositoryPort

INTERNAL_ERROR_CODE = "INTERNAL_ERROR"


class SendActionJobBinding:
    """Why one job did not belong to the operation it named."""

    KIND = "OPERATION_KIND_MISMATCH"
    NAMESPACE = "OPERATION_NAMESPACE_MISMATCH"
    CASE = "OPERATION_CASE_MISMATCH"
    ACTOR = "OPERATION_ACTOR_MISMATCH"
    REQUEST = "OPERATION_REQUEST_MISMATCH"
    HANDOVER = "OPERATION_UNEXPECTED_HANDOVER"


@dataclass(slots=True)
class SendActionOperationWorker:
    """Run one send operation to a terminal status, then project what happened."""

    operations: ApplicationOperations
    send_action: SendActionRunner
    """The sender: :class:`~chorus.application.commands.send_action.SendAction` in a local
    composition, and :class:`~chorus.application.send_contract.RemoteSendAction` -- one
    synchronous invocation of the sender Lambda -- in a deployed one. Typed as the runner
    protocol because the deployed sender is a separate principal with a total Core deny, the
    only SES grant, and the destination secret, none of which may sit in this process."""

    shareable: ShareableRepositoryPort
    """The worker's own read handle onto the execution row.

    Held here rather than reached for through ``send_action``, because the resume path has to
    read the execution's state *before* deciding whether a send may be attempted at all -- and
    in a deployed topology the sender is behind a Lambda boundary while this read is an
    ordinary Shareable ``GetItem`` the worker's role already grants."""

    project: ProjectActionOutcome
    reconcile: ReconcileSendOutcome

    async def execute(self, job: SendActionOperationJob) -> ApplicationOperation:
        with observability.emitting_as(observability.SERVICE_WORKER):
            return await self._execute(job)

    async def _execute(self, job: SendActionOperationJob) -> ApplicationOperation:
        operation = await self.operations.load(
            namespace=job.namespace, operation_id=job.operation_id
        )
        mismatches = self._binding_failures(job, operation)
        if mismatches:
            # Not this worker's operation. Claiming it would let a misrouted delivery end work
            # it knows nothing about; failing it would record a different command's failure
            # inside the send path.
            observability.worker_job_mismatch(
                namespace=job.namespace,
                operation_id=job.operation_id,
                invocation_id=job.correlation_id,
                correlation_id=job.correlation_id,
                reason_codes=mismatches,
            )
            return operation

        if self.operations.is_terminal(operation):
            # Terminal for the *operation*, which is not the same as "Phase 8 owes this case
            # nothing". A send whose execution is durably ``SENT`` still owes the case its
            # ``ACTIONED`` edge, and that projection is the one step here that can fail on its
            # own after the send is irreversible. Replaying it is a read plus, at most, one
            # conditional write; it calls nothing external and it never reaches SES.
            await self._repair_projection(job)
            return operation
        if operation.status is ApplicationOperationStatus.RUNNING:
            # A redelivery of work already in flight, or left behind by a lost process. The
            # send command's own replay table settles which of those it is, and neither
            # reaches SES from a state that forbids it.
            return await self._resume(job, operation)

        try:
            claimed = await self.operations.claim(operation)
        except (StateTransitionError, PersistenceError):
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )
        return await self._run(job, claimed)

    async def _run(
        self, job: SendActionOperationJob, claimed: ApplicationOperation
    ) -> ApplicationOperation:
        try:
            result = await self.send_action.execute(self._command(job))
        except SendDeniedError as error:
            # A state the replay table refuses. Terminal for this operation and correct: the
            # execution is already authoritative and nothing here may change it.
            return await self._settle(job, claimed, error_code=error.safe_code)
        except (DomainError, ApplicationError) as error:
            return await self._settle(job, claimed, error_code=_safe_code(error))
        except PersistenceError as error:
            if error.code is PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME:
                # The claim or the result write may or may not have committed. A later
                # delivery resolves it by *reading* the execution, never by sending again.
                return self._leave_recoverable(job, claimed)
            return await self._settle(job, claimed, error_code=_safe_code(error))
        except Exception:
            # Nothing unmapped escapes into an at-least-once dispatcher. On this path that
            # matters more than anywhere else: an escaping exception reads as "retry me".
            return await self._settle(job, claimed, error_code=INTERNAL_ERROR_CODE)

        if not await self._project_if_sent(job, result):
            # The send is durable and the case is not. The operation stays **recoverable**
            # rather than succeeding, because a ``SUCCEEDED`` record is what a later delivery
            # returns early on -- and returning early is what left a ``SENT`` execution beside
            # an ``ACTION_PROPOSED`` case with nothing owing to move it. Nothing is retried
            # externally: the next delivery repeats a read and, at most, one conditional write.
            return self._leave_recoverable(job, claimed)
        # Every classified outcome -- SENT, FAILED, and SEND_UNKNOWN -- is a *successful*
        # operation. The execution row carries which one it was.
        return await self._succeed(job, claimed, result)

    async def _resume(
        self, job: SendActionOperationJob, operation: ApplicationOperation
    ) -> ApplicationOperation:
        """Answer a redelivery from durable state, and let the **execution** decide what is owed.

        Four branches and only one of them can reach SES, which is the one where durable state
        proves that nothing was ever claimed:

        * ``APPROVED`` -- no claim committed, so the attempt is simply unfinished. The frozen
          recovery table says retrying the same claim under the same key is safe here, and it is
          the branch that used to fall through and strand the operation ``RUNNING`` forever with
          an approved message nobody would ever send.
        * ``SENDING`` past its window with no live fence -- quarantined, and nothing is sent.
        * ``SENDING`` inside its window -- left exactly alone, because elapsed time is not
          evidence and another process may still be working.
        * terminal -- projected if the projection is still owed, and the operation finished.

        The ``APPROVED`` branch is safe *because* of the claim owner: a claim that did commit
        leaves the row at ``SENDING``, so reading ``APPROVED`` is positive evidence that no
        attempt was ever made, not an assumption that none was.
        """

        command = self._command(job)
        execution = await self.shareable.load_execution(command.action_scope, job.execution_id)
        if execution.state is ActionExecutionState.APPROVED:
            return await self._run(job, operation)
        if execution.state in {
            ActionExecutionState.SENT,
            ActionExecutionState.FAILED,
            ActionExecutionState.SEND_UNKNOWN,
        }:
            if not await self._project_if_sent(job, None):
                return self._leave_recoverable(job, operation)
            return await self._succeed(job, operation, None)
        if execution.state is ActionExecutionState.SENDING:
            try:
                await self.reconcile.execute(
                    ReconcileSendOutcomeCommand(
                        namespace=job.namespace,
                        community_id=job.community_id,
                        case_id=job.case_id,
                        action_id=job.action_id,
                        execution_id=job.execution_id,
                        actor_id_hash=job.actor_id_hash,
                        correlation_id=job.correlation_id,
                    )
                )
            except ReconciliationRefusedError:
                # Still inside the window, or a live fence says a sender holds the case. The
                # operation stays RUNNING and a later delivery tries the *read* again.
                return operation
            return await self._succeed(job, operation, None)
        return operation

    async def _project_if_sent(
        self, job: SendActionOperationJob, result: SendActionResult | None
    ) -> bool:
        """Move the case only on ``SENT``, and report whether the projection is still owed.

        A failure here is still not fatal -- the send already happened and is durable, and
        reporting it as failed because a case row did not move would be the one answer that
        cannot be revised. But it is no longer *silent*: the boolean is what keeps the operation
        recoverable until the case has taken its edge.

        ``True`` means nothing is owed: the case moved, it had already moved, or the outcome
        legitimately takes no case edge at all (``FAILED`` and ``SEND_UNKNOWN`` both leave the
        case ``ACTION_PROPOSED``).
        """

        if result is not None and result.state is not ActionExecutionState.SENT:
            return True
        try:
            await self.project.execute(
                ProjectActionOutcomeCommand(
                    namespace=job.namespace,
                    community_id=job.community_id,
                    case_id=job.case_id,
                    action_id=job.action_id,
                    execution_id=job.execution_id,
                    actor_id_hash=job.actor_id_hash,
                    correlation_id=job.correlation_id,
                )
            )
        except (DomainError, PersistenceError):
            observability.lambda_replay(
                namespace=job.namespace,
                community_id=job.community_id,
                operation_id=job.operation_id,
                invocation_id=job.correlation_id,
                correlation_id=job.correlation_id,
                outcome="PROJECTION_DEFERRED",
            )
            return False
        return True

    async def _repair_projection(self, job: SendActionOperationJob) -> None:
        """Re-attempt the case projection for an operation that is already terminal.

        Terminal operations exist from before this repair, and a delivery can also arrive after
        another process settled one. Either way the question a replay must answer is about the
        *case*, not about the operation record: is a ``SENT`` execution still missing its
        ``ACTIONED`` edge. The projection command re-reads both rows and refuses to move
        anything it does not owe, so calling it unconditionally here is a read on the happy
        path and a repair on the one that needs it.

        It cannot send. The projection holds no sender, no registry, and no SES adapter.
        """

        await self._project_if_sent(job, None)

    # -- binding ---------------------------------------------------------------------------

    def _binding_failures(
        self, job: SendActionOperationJob, operation: ApplicationOperation
    ) -> tuple[str, ...]:
        """Every way this job disagrees with the durable operation it claims to be about."""

        failures: list[str] = []
        if operation.kind is not ApplicationOperationKind.SEND_ACTION:
            failures.append(SendActionJobBinding.KIND)
        if operation.namespace != job.namespace or operation.operation_id != job.operation_id:
            failures.append(SendActionJobBinding.NAMESPACE)
        if operation.case_id != job.case_id:
            failures.append(SendActionJobBinding.CASE)
        if operation.actor_id_hash != job.actor_id_hash:
            failures.append(SendActionJobBinding.ACTOR)
        if operation.request_hash != job.request_hash:
            failures.append(SendActionJobBinding.REQUEST)
        if operation.agent_invocation_id is not None or operation.agent_binding_hash is not None:
            # A send operation carrying an agent handover is malformed, not merely unusual.
            # Refused rather than ignored, because the pair means something on other paths.
            failures.append(SendActionJobBinding.HANDOVER)
        return tuple(failures)

    # -- outcomes ---------------------------------------------------------------------------

    @staticmethod
    def _command(job: SendActionOperationJob) -> SendActionCommand:
        return SendActionCommand(
            namespace=job.namespace,
            community_id=job.community_id,
            case_id=job.case_id,
            action_id=job.action_id,
            execution_id=job.execution_id,
            approval_id=job.approval_id,
            expected_execution_version=job.expected_execution_version,
            actor_id_hash=job.actor_id_hash,
            correlation_id=job.correlation_id,
            idempotency_key=job.idempotency_key,
        )

    async def _succeed(
        self,
        job: SendActionOperationJob,
        operation: ApplicationOperation,
        result: SendActionResult | None,
    ) -> ApplicationOperation:
        refs = () if result is None else (result.execution_id.value,)
        try:
            return await self.operations.succeed(operation, result_refs=refs)
        except (StateTransitionError, PersistenceError):
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )

    async def _settle(
        self, job: SendActionOperationJob, claimed: ApplicationOperation, *, error_code: str
    ) -> ApplicationOperation:
        try:
            return await self.operations.fail(claimed, error_code=error_code)
        except (StateTransitionError, PersistenceError):
            return await self.operations.load(
                namespace=job.namespace, operation_id=job.operation_id
            )

    def _leave_recoverable(
        self, job: SendActionOperationJob, claimed: ApplicationOperation
    ) -> ApplicationOperation:
        """Leave the operation ``RUNNING`` with the send outcome unresolved.

        Not settled, not released, and the send is not retried. A later delivery retries the
        **read**, and if the row is still ``SENDING`` past its window it is quarantined.
        """

        observability.lambda_replay(
            namespace=job.namespace,
            community_id=job.community_id,
            operation_id=job.operation_id,
            invocation_id=job.correlation_id,
            correlation_id=job.correlation_id,
            outcome=PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME.value,
        )
        return claimed


def _safe_code(error: Exception) -> str:
    """The closed code an operation record may carry for one failure."""

    safe = getattr(error, "safe_code", None)
    if isinstance(safe, str):
        return safe
    code = getattr(error, "code", None)
    value = getattr(code, "value", None)
    return value if isinstance(value, str) else INTERNAL_ERROR_CODE


__all__ = ["SendActionJobBinding", "SendActionOperationWorker"]
