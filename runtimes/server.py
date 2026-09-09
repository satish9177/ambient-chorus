"""The AgentCore HTTP binding: two routes, one handler, and no framework.

AgentCore Runtime invokes a direct-code artifact over HTTP on ``0.0.0.0:8080`` and requires
exactly two routes -- ``GET /ping`` and ``POST /invocations``. That is the whole contract, and
this module is the whole implementation of it.

Why a bare ASGI application
---------------------------
The smallest correct binding, measured in what actually ships inside the zip. ``uvicorn`` is
already in the artifact's locked dependency closure -- ``strands-agents`` pulls ``mcp``, which
pulls ``sse-starlette`` and ``uvicorn`` -- so a fifty-line ASGI callable served by uvicorn adds
**no package at all** to the archive. The AgentCore server SDK would add one, and a web
framework would add a router, a dependency-injection system, and a request model layer to
dispatch two fixed paths.

The application boundary stays where it already is. This module holds no contract, no schema,
no branch on payload content, and no state between calls: it reads bounded bytes off the wire,
hands them to the runtime's own ``handle``, and writes the bytes back. Every rule about what a
request may contain is enforced by the entry point, which is where every existing boundary test
already points.

Nothing is logged, and that is deliberate rather than an omission. The request body is private
community text by construction, the response is derived from it, and an exception message from
a validator quotes the input that failed. The runtime therefore answers with a closed reason
code and a status; AgentCore's own invocation records carry the rest.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any, Final

HOST: Final = "0.0.0.0"  # noqa: S104 - the container-local bind AgentCore requires
PORT: Final = 8080
"""The frozen listen address. AgentCore reaches the container on this exact host and port, so
neither is configurable: a request that could choose either would be choosing deployment
configuration."""

PING_PATH: Final = "/ping"
INVOCATIONS_PATH: Final = "/invocations"

MAX_REQUEST_BYTES: Final = 1_048_576
"""The transport bound, identical to the frozen per-runtime payload bound.

Enforced here as well as in ``handle`` because the entry point cannot refuse bytes it has
already been made to buffer. A body that exceeds it is refused while it is still arriving.
"""

HEALTHY_STATUS: Final = "Healthy"
"""The only health value this runtime reports.

CHORUS runtimes are synchronous: one invocation occupies the process until it answers, and the
service is either able to accept one or it is not. ``HealthyBusy`` describes a runtime with
background work outliving its request, which this design does not have and must not acquire --
so reporting it would be a claim about a lifecycle nothing here implements.
"""

_PING_BODY: Final = b'{"status":"Healthy"}'
_INVALID_REQUEST_BODY: Final = b'{"error":"INVALID_REQUEST"}'
_NOT_FOUND_BODY: Final = b'{"error":"NOT_FOUND"}'
_METHOD_NOT_ALLOWED_BODY: Final = b'{"error":"METHOD_NOT_ALLOWED"}'
_PAYLOAD_TOO_LARGE_BODY: Final = b'{"error":"PAYLOAD_TOO_LARGE"}'
_RUNTIME_FAILURE_BODY: Final = b'{"error":"RUNTIME_FAILURE"}'
_BUDGET_EXCEEDED_BODY: Final = b'{"error":"RUNTIME_BUDGET_EXCEEDED"}'
"""Closed reason codes and nothing else.

Never an exception message, never a validation report, never a truncated echo of the request. A
Pydantic ``ValidationError`` quotes the value that failed, and the value that failed here is a
private case, a stranger's email, or a compiled safe view.
"""

type InvocationHandler = Callable[[bytes], Awaitable[bytes]]
type Message = MutableMapping[str, Any]
type Scope = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]


class _PayloadTooLargeError(Exception):
    """The body exceeded the transport bound and was refused while still arriving."""


class _ClientGoneError(Exception):
    """The caller disconnected before the body finished; there is nobody to answer."""


@dataclass(frozen=True, slots=True)
class AgentCoreServer:
    """One runtime's ASGI application: ``/ping``, ``/invocations``, and nothing else.

    Frozen and slotted, so the object genuinely cannot accumulate per-request attributes. The
    three fields are decided once at import time by the runtime's own ``main.py``; there is no
    registry, no route table to add to, and no second handler to reach.

    ``contract_error`` and ``budget_error`` are the entry point's own exception types, passed in
    rather than imported, because this module must not know which runtime it is serving. They
    are what turn a refusal into the right status: a malformed or unaddressed request is the
    caller's error, an exhausted runtime budget is a gateway timeout, and everything else is an
    unclassified failure that says nothing about what it was processing.
    """

    handler: InvocationHandler
    contract_error: type[Exception]
    budget_error: type[Exception]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind == "lifespan":
            await _run_lifespan(receive, send)
            return
        if kind != "http":
            # No WebSocket, no other protocol. AgentCore speaks plain HTTP to a direct-code
            # runtime, and a second protocol would be a second way in.
            await send({"type": "websocket.close", "code": 1008})
            return
        await self._respond(scope, receive, send)

    async def _respond(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        method = scope.get("method", "")
        if path == PING_PATH:
            if method != "GET":
                await _reply(send, 405, _METHOD_NOT_ALLOWED_BODY)
                return
            await _reply(send, 200, _PING_BODY)
            return
        if path != INVOCATIONS_PATH:
            await _reply(send, 404, _NOT_FOUND_BODY)
            return
        if method != "POST":
            await _reply(send, 405, _METHOD_NOT_ALLOWED_BODY)
            return
        await self._invoke(receive, send)

    async def _invoke(self, receive: Receive, send: Send) -> None:
        try:
            raw = await _read_body(receive)
        except _PayloadTooLargeError:
            await _reply(send, 413, _PAYLOAD_TOO_LARGE_BODY)
            return
        except _ClientGoneError:
            return
        try:
            answer = await self.handler(raw)
        except self.budget_error:
            await _reply(send, 504, _BUDGET_EXCEEDED_BODY)
            return
        except self.contract_error:
            # Refused before any model call, and refused deterministically: the same bytes
            # always produce the same refusal.
            await _reply(send, 400, _INVALID_REQUEST_BODY)
            return
        except Exception:
            # The taxonomy the caller sees is the status code. Nothing about the exception --
            # its type, its message, its traceback -- crosses the boundary or is recorded.
            await _reply(send, 500, _RUNTIME_FAILURE_BODY)
            return
        await _reply(send, 200, answer)


async def _read_body(receive: Receive) -> bytes:
    """Buffer the request body, refusing it the moment it passes the transport bound."""

    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        kind = message.get("type")
        if kind == "http.disconnect":
            raise _ClientGoneError
        if kind != "http.request":
            continue
        chunk: bytes = message.get("body", b"")
        total += len(chunk)
        if total > MAX_REQUEST_BYTES:
            raise _PayloadTooLargeError
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _reply(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _run_lifespan(receive: Receive, send: Send) -> None:
    """Acknowledge startup and shutdown without doing either.

    There is nothing to warm and nothing to drain: the runtime constructs its model client per
    invocation and holds no pool, no cache, and no session. Answering the protocol is still
    required, because a server whose lifespan never completes never starts listening.
    """

    while True:
        message = await receive()
        kind = message.get("type")
        if kind == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif kind == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


def serve(app: AgentCoreServer, *, host: str = HOST, port: int = PORT) -> None:  # pragma: no cover
    """Run one runtime's application on the address AgentCore expects.

    Imported inside the function so the application object can be built, inspected, and driven
    by an in-process ASGI transport in tests without uvicorn ever binding a socket.

    Access logging is off. A uvicorn access line carries a method, a path, and a status, none of
    which is private -- but it is one configuration change away from carrying a query string,
    and this runtime's answer to "should this be logged" is uniformly no.
    """

    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        access_log=False,
        log_level="warning",
        lifespan="on",
        server_header=False,
    )


__all__ = [
    "HEALTHY_STATUS",
    "HOST",
    "INVOCATIONS_PATH",
    "MAX_REQUEST_BYTES",
    "PING_PATH",
    "PORT",
    "AgentCoreServer",
    "InvocationHandler",
    "serve",
]
