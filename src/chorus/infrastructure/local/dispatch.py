"""Local operation dispatchers.

The deployed dispatcher is an asynchronous Lambda invocation and belongs to the deployment
phase. These two exist so the same worker body can be exercised now:

* :class:`RecordingOperationDispatcher` stores jobs and runs nothing, which is what a test
  wants when it needs to assert that a request returned ``202`` *before* any model was called;
* :class:`InProcessOperationDispatcher` runs the worker as a background task so a developer
  gets the real asynchronous shape -- an immediate ``202`` and a poll -- without AWS.

The in-process dispatcher keeps a strong reference to each task. Without one, Python is free
to garbage-collect a running task, which would silently strand an operation in ``PENDING``
and look exactly like a model that never answered.

It also has to be joinable from a *different* event loop than the one it dispatched on. A
handover happens inside a request, and a caller driving that request through an ASGI test
client runs the application on its own loop in its own thread -- so awaiting the task objects
directly would be awaiting futures belonging to somebody else's loop. Completion is therefore
signalled through a plain :class:`threading.Event` per job, which belongs to no loop at all.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

from chorus.ports.operations import MonitorJobRunner, MonitorOperationJob


class DispatchFailedError(RuntimeError):
    """A handover did not reach whatever executes the job.

    Its own type rather than a bare exception because the case it stands for is specific and
    easy to under-test: the durable operation is already written, so a failed handover leaves
    a record that looks entirely healthy with nothing anywhere that knows to run it.
    """


@dataclass(slots=True)
class RecordingOperationDispatcher:
    """Record dispatched jobs without executing them.

    ``failures`` scripts how many of the next handovers fail before any job is recorded. It
    exists so the recovery path -- a same-key retry re-dispatching a still-``PENDING``
    operation -- can be tested at all, rather than being reasoned about.
    """

    jobs: list[MonitorOperationJob] = field(default_factory=list)
    failures: int = 0

    async def dispatch_monitor(self, job: MonitorOperationJob) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise DispatchFailedError("the job was not handed over")
        self.jobs.append(job)


@dataclass(slots=True)
class InProcessOperationDispatcher:
    """Execute the worker as a background task in this process."""

    worker: MonitorJobRunner
    _tasks: set[asyncio.Task[object]] = field(default_factory=set, init=False)
    _pending: set[threading.Event] = field(default_factory=set, init=False)

    async def dispatch_monitor(self, job: MonitorOperationJob) -> None:
        finished = threading.Event()
        task: asyncio.Task[object] = asyncio.create_task(self.worker.execute(job))
        self._tasks.add(task)
        self._pending.add(finished)

        def settle(completed: asyncio.Task[object]) -> None:
            self._tasks.discard(completed)
            self._pending.discard(finished)
            finished.set()

        task.add_done_callback(settle)

    async def drain(self) -> None:
        """Block until every in-flight job has finished, whatever loop is asking.

        Used by local smoke runs and by tests that need the asynchronous path *and* a
        deterministic point at which it has finished.

        The wait is on a ``threading.Event`` rather than on the task objects, because the
        caller is very often not on the loop that created them: an ASGI test client runs the
        application on its own loop in its own thread, so a job dispatched inside a request
        belongs to that loop and gathering its future from here is a cross-loop error. The
        event belongs to no loop, and ``to_thread`` keeps *this* loop free to run the tasks
        while the wait blocks -- so the same call is correct whether the dispatch happened
        here or elsewhere.

        There is no sleeping and no polling interval. Each wait returns exactly when its job's
        done-callback fires.
        """

        while self._pending:
            for finished in tuple(self._pending):
                await asyncio.to_thread(finished.wait)
