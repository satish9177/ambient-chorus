"""The FastAPI composition root for the Phase 3 and Phase 4 surfaces.

The routes are the discovery surface -- ingest a batch, read the ambient feed -- and the private
mandate surface: accept a candidate, read one contributor's thread, record one decision. Each
is the transport half of an application use case that holds all of the policy.

The application is built, not discovered. :func:`build_app` takes a fully constructed
container, so there is no import-time global, no environment read inside a route, and no path
by which a test and the deployed service end up wired differently.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response

from chorus_api.dependencies import ApiContainer
from chorus_api.problem_details import (
    CORRELATION_HEADER,
    register_problem_handlers,
    register_transport_handlers,
)
from chorus_api.routes import feed, ingest, mandates

API_PREFIX = "/v1"
API_TITLE = "Ambient CHORUS"
API_VERSION = "0.4.0"


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

    app.include_router(ingest.router, prefix=API_PREFIX)
    app.include_router(feed.router, prefix=API_PREFIX)
    app.include_router(mandates.router, prefix=API_PREFIX)
    return app


def _correlation_id(supplied: str | None) -> UUID:
    if supplied is None:
        return uuid4()
    try:
        return UUID(supplied)
    except ValueError:
        return uuid4()
