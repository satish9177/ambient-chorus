"""The two shapes of internal function-to-function call, as ports rather than as an SDK.

Every deployed CHORUS component reaches another one in exactly one of two ways, and the
difference is not a tuning knob -- it is whether the caller is entitled to an answer:

* :class:`SynchronousInvokerPort` -- the caller needs the callee's decision *before* it can
  proceed. The sender cannot send without a fence; the demo route cannot answer without the
  watcher's outcome. Deployed, this is ``InvocationType="RequestResponse"``.
* :class:`AsynchronousInvokerPort` -- the caller is handing work over and must not wait. The
  request path starts an agent-invoking operation and returns ``202``. Deployed, this is
  ``InvocationType="Event"``, and there is deliberately **no return value at all**: an ``Event``
  invocation's response says only that the request was accepted, so a port that returned
  anything would be a port inviting somebody to read a result that does not exist.

The target is not a parameter. Each implementation is constructed against one exact function
identity from deployment configuration, so **no caller-supplied value can ever name what is
invoked** -- there is no "invoke an arbitrary Lambda" capability in this system, at the port or
anywhere below it.

``operation`` is a string chosen from a closed set the callee declares. It is a discriminator
rather than a method per operation for the reason the compiler's invoker already gives: the
transport is one grant against one ARN, and the operation list belongs with the dispatch that
implements it.
"""

from __future__ import annotations

from typing import Protocol


class SynchronousInvokerPort(Protocol):
    """Invoke one named operation, wait, and return its decoded body or raise.

    It never returns a default and never resolves an ambiguity permissively: a transport
    success is not an application success, and "the callee could not be reached" and "the callee
    said yes" must never be the same value.
    """

    async def invoke(self, *, operation: str, payload: dict[str, object]) -> dict[str, object]:
        """Return the operation's response body, or raise."""


class AsynchronousInvokerPort(Protocol):
    """Hand one named operation over for execution elsewhere, at least once."""

    async def dispatch(self, *, operation: str, payload: dict[str, object]) -> None:
        """Return once the handover is accepted; raise if it was not.

        At-least-once is the contract on purpose. The durable application operation and its
        conditional ``PENDING -> RUNNING`` claim are what make a repeated delivery a no-op, so a
        dispatcher never has to promise exactly-once semantics it cannot actually provide.
        """


__all__ = ["AsynchronousInvokerPort", "SynchronousInvokerPort"]
