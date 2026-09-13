"""The deployed commitment watcher's Lambda entry point.

Two callers, **one** watcher. EventBridge Scheduler delivers a one-time due event, and
``POST /v1/demo/clock/advance`` invokes this same function synchronously and returns its outcome
([ADR-028](../../../docs/adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) § 5). Both
arrive through the identical envelope, are parsed by the identical decoder, and reach the
identical :class:`~chorus.application.commands.record_commitment_due.RecordCommitmentDue`. There
is no branch here on which of them called, because a second implementation for the demo path is
precisely the defect § 5 exists to prevent.

The order of one invocation
----------------------------
1. read the envelope; an unknown operation or a malformed payload is refused **before** a client
   exists, a row is read, or anything is bound;
2. strongly read the authoritative logical clock. Missing, corrupt, or unreachable, it never
   falls back to a process-local clock, to ``SystemClock``, or to the event's own timestamp
   ([ADR-029](../../../docs/adr/ADR-029-deployed-demo-clock-authority.md) § 4) -- and the
   invocation itself is made to **fail** rather than answered normally (see below). The
   scheduler event is unsigned and stays that way; reading an authoritative clock does not make
   the event authoritative;
3. bind that reading for the invocation and run the watcher's frozen order.

A clock outage fails the invocation, not the answer
------------------------------------------------------
A normal return from an asynchronous invocation is an acknowledgement: EventBridge Scheduler
reads it as "delivered" and never tries again. A clock SDK failure is a transient
infrastructure condition, not a business fact about the commitment -- nothing has been loaded,
nothing claimed, nothing written -- so it must not be swallowed into a normally-returned
``REFUSED`` the way a real business no-op is. It is raised instead, as
:class:`~functions.envelope.InvocationFailedError`, which lets AWS's own retry machinery
engage: the Scheduler target's ``RetryPolicy`` (and DLQ) for the asynchronous caller, and
``FunctionError`` -- which
:class:`~chorus.infrastructure.lambdas.invoker.SynchronousLambdaInvoker` already turns into the
accepted dependency failure -- for the API's synchronous caller. **The same handler serves
both**; nothing here branches on which one is asking.

Duplicate and late delivery
----------------------------
Nothing is done about them here, and nothing needs to be. A duplicate scheduler delivery, a late
one, the demo clock racing the real schedule, and a commitment a human already satisfied are all
one branch inside the use case: it strongly reloads the commitment, re-verifies namespace, case,
generation, due-event ID and due time, finds a status that is no longer eligible, and succeeds
having written nothing. That is ADR-028 § 2-3 behaving as specified, not an accommodation --
and it remains a **normal return**, because it genuinely is the correct, deterministic answer
and must not trigger a retry.

What this returns
------------------
The small closed result of ``commitment-watcher-result/v1``: an outcome, and the commitment's
status and version where one was loaded. No traceback, no event echo, no row. A Lambda's return
value reaches CloudWatch and a caller's log, so it is treated as an external surface -- and a
refusal carries a reason code and nothing else.

**Cold start touches no network.** Settings are read from the environment and the object graph
is constructed lazily on the first invocation; boto3 clients are created without a credential
lookup or a request, so importing this module needs no AWS anything.
"""

from __future__ import annotations

import secrets
from typing import Any, Final

import anyio

from chorus.application.watcher_contract import (
    WATCHER_OPERATION,
    WatcherRequestError,
    decode_watcher_request,
    encode_watcher_result,
)
from chorus.ports.demo_clock import DemoClockError
from chorus.settings import Settings
from functions.commitment_watcher.composition import (
    WatcherComposition,
    WatcherSettings,
    build_watcher,
)
from functions.envelope import EnvelopeError, InvocationFailedError, failure, read_envelope

ACCEPTED_OPERATIONS: Final = frozenset({WATCHER_OPERATION})
"""The watcher answers exactly one question, and there is no second one to name."""

MALFORMED_EVENT: Final = "MALFORMED_EVENT"
CLOCK_UNAVAILABLE: Final = "CLOCK_UNAVAILABLE"
"""The reason named in the raised :class:`~functions.envelope.InvocationFailedError`.

Not a returned reason code -- a clock outage is never answered normally -- but the same fixed,
safe string every other reason code in this system is, carried in the exception so a CloudWatch
reader sees the same vocabulary either way.
"""

_composition: WatcherComposition | None = None


def watcher_settings(settings: Settings) -> WatcherSettings:
    """Map process configuration onto the watcher's own settings, and nothing wider.

    The cursor secret is a fresh random value per execution environment because the watcher
    issues no pagination cursor that outlives its own process; it is a constructor argument the
    repositories require, not key material anything durable depends on.
    """

    return WatcherSettings(
        region=settings.aws_region,
        namespace=settings.namespace,
        shareable_table=settings.shareable_table,
        audit_table=settings.audit_table,
        cursor_secret=secrets.token_bytes(32),
    )


def composition() -> WatcherComposition:
    """Build the object graph once per execution environment, on first use.

    Lazily rather than at import, so importing this module reads no environment, constructs no
    client, and makes no call -- which is what lets every handler-import test run with AWS
    credential lookup disabled.
    """

    global _composition
    if _composition is None:
        _composition = build_watcher(watcher_settings(Settings.load()))
    return _composition


async def run(event: object, *, built: WatcherComposition | None = None) -> dict[str, Any]:
    """Run one delivered due event to its outcome. The async body the handler drives."""

    try:
        _, payload = read_envelope(event, accepted=ACCEPTED_OPERATIONS)
        command = decode_watcher_request(payload)
    except (EnvelopeError, WatcherRequestError):
        # Refused at the parse: nothing has been loaded, claimed, or written, so there is
        # nothing to undo and nothing to report beyond the reason code.
        return failure(MALFORMED_EVENT)
    graph = built or composition()
    try:
        record = await graph.clock_store.read()
    except DemoClockError as error:
        # Fails the invocation, not the answer. Nothing has been loaded or claimed yet, so
        # this is safe to retry, and AWS's own retry/DLQ machinery is what should see it --
        # not a caller reading a normal return as "handled".
        raise InvocationFailedError(CLOCK_UNAVAILABLE) from error
    with graph.scope.bound_to(record.logical_time):
        result = await graph.watcher.execute(command)
    return encode_watcher_result(result)


def handler(event: object, context: object = None) -> dict[str, Any]:
    """The Lambda entry point. One invocation, one event loop, one authoritative reading."""

    return anyio.run(run, event)


__all__ = [
    "ACCEPTED_OPERATIONS",
    "CLOCK_UNAVAILABLE",
    "MALFORMED_EVENT",
    "composition",
    "handler",
    "run",
    "watcher_settings",
]
