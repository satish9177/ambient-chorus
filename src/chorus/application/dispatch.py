"""The deployed operation dispatcher: one handover, one transport, five named operations.

This is what replaces ``InProcessOperationDispatcher`` in a deployed system (deployment contract
SS 14). An operation that lives in a request process dies with it, so the request path writes
the durable operation, hands the job over asynchronously, and returns ``202``.

It holds an :class:`~chorus.ports.invocation.AsynchronousInvokerPort` and nothing else. It has
no client, no ARN, no region, and no retry policy of its own -- the invoker was constructed
against one exact configured function, so nothing reachable from here can name a different one.

Duplicate delivery is not solved here, and must not be
-------------------------------------------------------
AWS asynchronous invocation may deliver a job more than once. This dispatcher does **nothing**
about that -- no cache, no seen-set, no process-local deduplication -- because a process-local
answer to a cross-process question is an answer that stops being true the moment a second
execution environment exists. The durable operation and its conditional ``PENDING -> RUNNING``
claim are the duplicate-execution boundary (deployment contract SS 14), and the ``SEND_ACTION``
path is protected by something stronger still: the execution's own
``APPROVED@v -> SENDING@v+1`` compare-and-swap, which permits an SES call from exactly one
state.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.application.commands.extract_commitment_operation import ExtractCommitmentJob
from chorus.application.jobs import WorkerJobKind, encode_job
from chorus.ports.invocation import AsynchronousInvokerPort
from chorus.ports.operations import (
    InvestigationOperationJob,
    MonitorOperationJob,
    ProposeActionOperationJob,
    SendActionOperationJob,
)


@dataclass(frozen=True, slots=True)
class RemoteOperationDispatcher:
    """Hand each job to the operation worker, at least once, and expect no answer."""

    invoker: AsynchronousInvokerPort

    async def dispatch_monitor(self, job: MonitorOperationJob) -> None:
        await self._hand_over(WorkerJobKind.MONITOR, job)

    async def dispatch_investigation(self, job: InvestigationOperationJob) -> None:
        await self._hand_over(WorkerJobKind.INVESTIGATE, job)

    async def dispatch_propose_action(self, job: ProposeActionOperationJob) -> None:
        await self._hand_over(WorkerJobKind.PROPOSE_ACTION, job)

    async def dispatch_extract_commitment(self, job: object) -> None:
        if not isinstance(job, ExtractCommitmentJob):
            raise TypeError("an extraction handover carries an ExtractCommitmentJob")
        await self._hand_over(WorkerJobKind.EXTRACT_COMMITMENT, job)

    async def dispatch_send_action(self, job: SendActionOperationJob) -> None:
        await self._hand_over(WorkerJobKind.SEND_ACTION, job)

    async def _hand_over(self, kind: WorkerJobKind, job: object) -> None:
        """Encode, then invoke. Nothing is read back: an ``Event`` invocation has no result."""

        await self.invoker.dispatch(operation=kind.value, payload=encode_job(job))


__all__ = ["RemoteOperationDispatcher"]
