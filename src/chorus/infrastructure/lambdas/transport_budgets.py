"""Caller-specific synchronous-invocation transport budgets (review P2-9).

Each synchronous Lambda-to-Lambda call has a *client-side* transport read timeout that must sit
between the callee's own Lambda timeout and the caller's, so the caller gives up
**deterministically** -- mapping to the accepted dependency failure -- before the platform
hard-terminates it, and a slow-but-successful callee is never turned into an artificial client
timeout merely because botocore's default read timeout (~60 s) is shorter than the callee's
real budget.

The Lambda timeouts referenced below are the values in ``functions/<name>/lambda.toml``;
``tests/unit/functions/test_transport_budgets.py`` asserts they still agree, so the two cannot
drift.

API request path -- ``POST /v1/compile`` and ``POST /v1/demo/clock/advance``
--------------------------------------------------------------------------
The presenter waits on the API Lambda, which fans out synchronously to the compiler (a
deterministic compile of the frozen fixture: image sanitize + one transaction + one S3 put,
seconds of work) and to the watcher (a strong clock read + commitment reload + one CAS, also
seconds). The chain::

    compiler / watcher Lambda timeout   (24 s -- functions/compiler|commitment_watcher/lambda.toml)
        < API_DOWNSTREAM_READ_TIMEOUT_SECONDS   (25 s -- the API's client read timeout for both)
        < API_LAMBDA_TIMEOUT_SECONDS             (29 s -- functions/api/lambda.toml)
        < HTTP_API_INTEGRATION_CEILING_SECONDS   (30 s -- API Gateway HTTP API hard limit)

A genuinely-stuck compiler or watcher self-terminates at 24 s; the API's client read then times
out at 25 s, raising ``ReadTimeoutError`` -> the invoker's typed ``ExternalDependencyError``
(``DEPENDENCY_REJECTED``, not retryable) -> the API's existing dependency-failure handler. The
25 s read timeout is below the API Lambda's 29 s hard timeout, which is below API Gateway's
30 s ceiling: that ordering leaves **nominal** termination headroom for the API to render the
typed error, and is *not* an end-to-end latency guarantee -- connection setup, request handling
before the downstream call, and response serialization all consume part of the difference. What
it does guarantee is that no partial work is ever reported as a completed response.

Worker -> sender (the ``SEND_ACTION`` branch only)
------------------------------------------------
The sender may legitimately run its full send-authorization fence life plus one deliberate SES
attempt (its SES read timeout is pinned to the 60 s fence lifetime), so its Lambda timeout is
90 s. The worker must out-wait that::

    SENDER_LAMBDA_TIMEOUT_SECONDS           (90 s -- functions/sender/lambda.toml)
        < WORKER_SENDER_READ_TIMEOUT_SECONDS
        < WORKER_LAMBDA_TIMEOUT_SECONDS      (120 s -- functions/worker/lambda.toml)

The 105 s read timeout is above the sender's 90 s Lambda timeout (so a slow-but-successful send
is not clipped) and below the worker's 120 s (so the worker gets a ``ReadTimeoutError`` rather
than being hard-terminated). The ``120 - 105`` difference is **nominal** headroom for the
worker to decode the send result and run the outcome projection -- connection setup and the
worker's prior work in the same invocation consume part of it; it is not a guaranteed 15 s of
persistence budget. This is a **sender-specific** client; the worker's other synchronous calls
keep tighter budgets. Nothing here weakens the one-deliberate-attempt rule, the ``APPROVED ->
SENDING`` compare-and-swap, the ``SEND_UNKNOWN`` quarantine, or the batch-4 invocation-error
typing -- a client read timeout is classified exactly as any other transport failure and
authorizes no resend.
"""

from __future__ import annotations

from typing import Final

HTTP_API_INTEGRATION_CEILING_SECONDS: Final = 30
"""API Gateway HTTP API's hard integration timeout. Nothing on the request path may exceed it."""

API_LAMBDA_TIMEOUT_SECONDS: Final = 29
"""``functions/api/lambda.toml`` -- one second under the gateway ceiling."""

API_DOWNSTREAM_CONNECT_TIMEOUT_SECONDS: Final = 3
API_DOWNSTREAM_READ_TIMEOUT_SECONDS: Final = 25
"""The API's client read timeout for its synchronous compiler and watcher calls.

One second over the compiler's and watcher's own 24 s Lambda timeouts (so a stuck callee
self-terminates first) and below the API's 29 s Lambda timeout (so the API sees a
``ReadTimeoutError`` and renders the typed dependency error rather than being hard-terminated).
The margin to the API's own timeout is nominal, not an end-to-end latency guarantee. Both
downstream operations are sub-second of deterministic work in practice, so this is generous
headroom, not a tight race.
"""

WORKER_LAMBDA_TIMEOUT_SECONDS: Final = 120
"""``functions/worker/lambda.toml``."""

SENDER_LAMBDA_TIMEOUT_SECONDS: Final = 90
"""``functions/sender/lambda.toml`` -- fence life (60 s) + fence acquire/release headroom."""

WORKER_SENDER_CONNECT_TIMEOUT_SECONDS: Final = 5
WORKER_SENDER_READ_TIMEOUT_SECONDS: Final = 105
"""The worker's client read timeout for its synchronous ``SEND_ACTION`` invoke of the sender.

Greater than the sender's own 90 s Lambda timeout so a slow-but-successful send is not clipped,
and below the worker's 120 s so the worker sees a ``ReadTimeoutError`` and can still run the
outcome projection. The 15 s difference is nominal headroom, not a guaranteed persistence
window -- connection setup and the worker's prior work in the invocation consume part of it.
"""

SENDER_COMPILER_CONNECT_TIMEOUT_SECONDS: Final = 3
SENDER_COMPILER_READ_TIMEOUT_SECONDS: Final = 20
"""The sender's client read timeout for its synchronous compiler fence acquire/release.

A fence op is a couple of DynamoDB writes; 20 s is well inside the sender's SES budget and
fails fast if the compiler is unreachable.
"""

# The inequality chains, asserted at import so a careless edit to one constant fails loudly.
assert (
    API_DOWNSTREAM_READ_TIMEOUT_SECONDS
    < API_LAMBDA_TIMEOUT_SECONDS
    < HTTP_API_INTEGRATION_CEILING_SECONDS
), "API request-path budget inequality violated"
assert (
    SENDER_LAMBDA_TIMEOUT_SECONDS
    < WORKER_SENDER_READ_TIMEOUT_SECONDS
    < WORKER_LAMBDA_TIMEOUT_SECONDS
), "worker -> sender budget inequality violated"
assert SENDER_COMPILER_READ_TIMEOUT_SECONDS < SENDER_LAMBDA_TIMEOUT_SECONDS, (
    "sender -> compiler budget inequality violated"
)


__all__ = [
    "API_DOWNSTREAM_CONNECT_TIMEOUT_SECONDS",
    "API_DOWNSTREAM_READ_TIMEOUT_SECONDS",
    "API_LAMBDA_TIMEOUT_SECONDS",
    "HTTP_API_INTEGRATION_CEILING_SECONDS",
    "SENDER_COMPILER_CONNECT_TIMEOUT_SECONDS",
    "SENDER_COMPILER_READ_TIMEOUT_SECONDS",
    "SENDER_LAMBDA_TIMEOUT_SECONDS",
    "WORKER_LAMBDA_TIMEOUT_SECONDS",
    "WORKER_SENDER_CONNECT_TIMEOUT_SECONDS",
    "WORKER_SENDER_READ_TIMEOUT_SECONDS",
]
