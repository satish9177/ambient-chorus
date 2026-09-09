"""The one shape every internal Lambda invocation in this system takes.

``{"operation": "<name>", "payload": {...}}`` -- a declared operation name from a closed set the
callee owns, and a JSON object. Both halves of every internal call speak it: the sender reaching
the compiler for a fence, the API reaching the watcher, the API handing a job to the worker.

Why one envelope rather than one event shape per function
----------------------------------------------------------
The transport is one ``lambda:InvokeFunction`` grant against one exact ARN, and what varies is
which operation of that callee is being asked for. Putting the operation in the envelope keeps
the dispatch table in the callee, where the implementations are, instead of spreading it across
every caller's payload shape.

The operation is **declared, never inferred**. A handler selects its branch on this field alone
and refuses an unknown value; nothing anywhere reads the payload's shape to guess what was
meant, because a dispatcher that guesses is a dispatcher a payload can steer.

Refusal happens here, before anything is constructed
-----------------------------------------------------
:func:`read_envelope` runs before a client exists, before a repository is built, and before a
single row is read. A malformed event fails at the parse, which is the only place where failing
costs nothing.

The error result carries a **reason code and nothing else** -- no traceback, no ``repr`` of the
event, no downstream body. A Lambda's return value lands in CloudWatch, in an X-Ray trace, and
sometimes in a caller's log, so it is treated as an external surface.
"""

from __future__ import annotations

from typing import Any, Final

OPERATION_FIELD: Final = "operation"
PAYLOAD_FIELD: Final = "payload"

MAX_OPERATION_LENGTH: Final = 64
"""A bound on the one caller-supplied string, so an unknown operation cannot be a large one."""


class EnvelopeError(ValueError):
    """The delivered event is not an internal invocation this function can read.

    Raised before any side effect. Its message is a fixed sentence chosen from this module and
    never contains any part of the event.
    """


class InvocationFailedError(RuntimeError):
    """A retryable infrastructure failure. Let the Lambda invocation itself fail.

    This is the deliberate alternative to :func:`failure`. ``failure()`` returns a normal
    payload, which for an asynchronous invocation is an **acknowledgement** -- EventBridge
    Scheduler and Lambda's own async delivery both read a normal return as "handled" and never
    try again. A durable clock read that failed for an infrastructure reason -- DynamoDB
    unavailable, a throttle, a transport error -- has determined nothing and changed nothing;
    telling the caller "handled" would make that transient failure permanent and silent.

    Raising this instead makes AWS treat the invocation as failed: an ``Event`` invocation gets
    Lambda's own asynchronous retry (and, where configured, a DLQ); a ``Scheduler`` target gets
    its ``RetryPolicy``; a synchronous ``RequestResponse`` caller sees ``FunctionError`` and
    :class:`~chorus.infrastructure.lambdas.invoker.SynchronousLambdaInvoker` already turns that
    into the accepted ``ExternalDependencyError`` the rest of the system expects.

    Reserved for failures that are provably safe to retry: raised only **before** any
    side-effecting call the retry could duplicate (a claim, an SES send, a fence acquisition).
    A deterministic business refusal -- stale version, invalid state, an already-settled
    outcome, ``SEND_UNKNOWN`` -- is never raised this way; those return normally, exactly as an
    async delivery's contract requires, because they are answers, not failures.

    The message is a fixed, safe sentence supplied by the raiser -- never a payload fragment,
    a downstream exception's own text, or a traceback.
    """


def read_envelope(event: object, *, accepted: frozenset[str]) -> tuple[str, dict[str, Any]]:
    """Return the declared operation and its payload, or refuse the event.

    ``accepted`` is the callee's own closed set. An operation outside it fails closed here
    rather than falling through to a default branch -- there is no default branch.
    """

    if not isinstance(event, dict):
        raise EnvelopeError("the event is not an object")
    operation = event.get(OPERATION_FIELD)
    if not isinstance(operation, str) or not operation:
        raise EnvelopeError("the event declares no operation")
    if len(operation) > MAX_OPERATION_LENGTH:
        raise EnvelopeError("the event declares an over-long operation")
    if operation not in accepted:
        raise EnvelopeError("the event declares an unsupported operation")
    payload = event.get(PAYLOAD_FIELD)
    if not isinstance(payload, dict):
        raise EnvelopeError("the event carries no payload object")
    return operation, payload


def failure(reason_code: str) -> dict[str, Any]:
    """The complete failure body: a status and a safe reason code.

    Deliberately not an exception message, not a class name, and not a field path. Every value
    passed here is a constant declared in the handler that raises it.
    """

    return {"status": "REFUSED", "reason_code": reason_code}


__all__ = [
    "MAX_OPERATION_LENGTH",
    "OPERATION_FIELD",
    "PAYLOAD_FIELD",
    "EnvelopeError",
    "InvocationFailedError",
    "failure",
    "read_envelope",
]
