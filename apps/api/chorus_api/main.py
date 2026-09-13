"""The FastAPI composition root for the Phase 3 through Phase 8 surfaces.

The routes are the discovery surface -- ingest a batch, read the ambient feed -- the private
mandate surface -- accept a candidate, read one contributor's thread, record one decision --
the investigation surface, which starts one skeptical review of one case, the compile
surface, the action surface, which asks for one external message draft against one exact
compiled view, and the case surface, which returns the current proposal with its preview
regenerated on read, and the approval surface, which records one human decision, clears a
proposal, and starts one send. Each is the transport half of an application use case that
holds all of the policy.

The application is built, not discovered. :func:`build_app` takes a fully constructed
container, so there is no import-time global, no environment read inside a route, and no path
by which a test and the deployed service end up wired differently.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response

from chorus.ports.access import AccessTokenUnavailableError
from chorus.ports.demo_clock import DemoClockError
from chorus_api.dependencies import ApiContainer
from chorus_api.problem_details import (
    CORRELATION_HEADER,
    LOGICAL_TIME_UNAVAILABLE_PROBLEM,
    UNAUTHENTICATED_PROBLEM,
    middleware_problem,
    register_problem_handlers,
    register_transport_handlers,
)
from chorus_api.routes import (
    actions,
    approvals,
    audit,
    cases,
    commitments,
    demo,
    feed,
    ingest,
    investigations,
    mandates,
    replies,
    views,
)

API_PREFIX = "/v1"
API_TITLE = "Ambient CHORUS"
API_VERSION = "0.9.0"

BEARER_SCHEME = "Bearer "
"""The only credential presentation this API accepts, and the comparison is case-sensitive."""


def build_app(container: ApiContainer) -> FastAPI:
    """Build the application around one explicitly constructed container."""

    app = FastAPI(title=API_TITLE, version=API_VERSION, docs_url=None, redoc_url=None)
    app.state.container = container
    register_problem_handlers(app)
    # Installed after the closed domain mapping and before any route: the framework's own
    # validation handler serializes the rejected input, which for a body of private community
    # text would make a 422 a disclosure channel.
    register_transport_handlers(app)

    @app.middleware("http")
    async def correlate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Attach a correlation identifier to every request and every response.

        A client-supplied value is honoured only when it is a UUID. Echoing arbitrary header
        text back into responses, logs, and audit rows would make the correlation field a
        channel for content nobody validated.
        """

        request.state.correlation_id = _correlation_id(request.headers.get(CORRELATION_HEADER))
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = str(request.state.correlation_id)
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    _install_access_boundary(app, container)
    _install_logical_time_boundary(app, container)

    app.include_router(ingest.router, prefix=API_PREFIX)
    app.include_router(feed.router, prefix=API_PREFIX)
    app.include_router(mandates.router, prefix=API_PREFIX)
    app.include_router(investigations.router, prefix=API_PREFIX)
    app.include_router(views.router, prefix=API_PREFIX)
    app.include_router(actions.router, prefix=API_PREFIX)
    app.include_router(approvals.router, prefix=API_PREFIX)
    app.include_router(cases.router, prefix=API_PREFIX)
    # The Phase-9 surfaces: one fixture selector in, and the one human decision that can
    # satisfy a promise or resolve a case.
    app.include_router(replies.router, prefix=API_PREFIX)
    app.include_router(commitments.router, prefix=API_PREFIX)
    # The Phase-10 surfaces: local reset/session, and the two private/safe reads that complete
    # the case surface's contract.
    app.include_router(demo.router, prefix=API_PREFIX)
    app.include_router(audit.router, prefix=API_PREFIX)
    return app


def _install_access_boundary(app: FastAPI, container: ApiContainer) -> None:
    """Require the demo bearer token, where the composition supplied a verifier.

    Installed **only** when ``container.access`` is present. A deployment with no verifier has
    no token check at all rather than a permissive one, which is the difference between "this
    boundary is not part of this composition" and "this boundary accepts anything".

    Registered before the correlation middleware so correlation stays *outside* it: a refused
    request still comes back with ``X-Correlation-Id`` and ``Cache-Control: no-store``, which is
    what makes a 401 traceable in the same way every other answer is.

    Every refusal is the same response. Missing header, wrong scheme, wrong token, and an
    unreadable secret are one shape, because a caller able to tell them apart could probe the
    deployment's state through the one surface that is meant to gate it.
    """

    verifier = container.access
    if verifier is None:
        return

    @app.middleware("http")
    async def require_demo_token(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        header = request.headers.get("Authorization")
        presented = (
            header[len(BEARER_SCHEME) :]
            if header is not None and header.startswith(BEARER_SCHEME)
            else ""
        )
        try:
            accepted = await verifier.verify(presented)
        except AccessTokenUnavailableError:
            # Fails closed. An unreadable secret is not "no token required"; it is a refusal,
            # and the caller is told exactly what a wrong token is told.
            accepted = False
        if not accepted:
            return middleware_problem(
                request=request, code="UNAUTHENTICATED", shape=UNAUTHENTICATED_PROBLEM
            )
        return await call_next(request)


def _install_logical_time_boundary(app: FastAPI, container: ApiContainer) -> None:
    """Bind one authoritative logical instant for the whole of each request, where deployed.

    The deployed demo's clock is a durable row, not a Python object, because the watcher runs in
    a different Lambda than the API that advanced it
    ([ADR-029](../../../docs/adr/ADR-029-deployed-demo-clock-authority.md)). A ``Clock`` answers
    synchronously and a durable read cannot, so the read happens once here -- strongly
    consistent -- and every ``clock.now()`` inside that request returns the same instant. One
    reading per request is also what makes a request's own timestamps agree with each other.

    A clock that is missing, corrupt, or unreachable is a **503 and no work**, never a fallback
    to process-local time, to ``SystemClock``, or to anything else (ADR-029 SS 4).
    """

    logical_time = container.logical_time
    if logical_time is None:
        return

    @app.middleware("http")
    async def bind_logical_time(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            record = await logical_time.store.read()
        except DemoClockError:
            return middleware_problem(
                request=request,
                code="LOGICAL_TIME_UNAVAILABLE",
                shape=LOGICAL_TIME_UNAVAILABLE_PROBLEM,
            )
        with logical_time.scope.bound_to(record.logical_time):
            return await call_next(request)


def _correlation_id(supplied: str | None) -> UUID:
    if supplied is None:
        return uuid4()
    try:
        return UUID(supplied)
    except ValueError:
        return uuid4()
