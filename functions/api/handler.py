"""The API Lambda's entry point: the FastAPI application bound to API Gateway payload v2.

The deployed host is **API Gateway HTTP API with payload format version 2.0**, and the ASGI
adapter is Mangum, pinned in ``pyproject.toml`` (deployment contract § 14). Mangum already
understands the v2 event shape -- ``requestContext.http.method``/``path``, ``rawQueryString``,
the ``cookies`` array, single-valued ``headers``, ``isBase64Encoded`` -- and translates the ASGI
response back into it. Hand-rolling that translation would be a second, less-tested
implementation of a format AWS documents and an accepted adapter already handles.

``lifespan="off"`` on purpose
------------------------------
The application is *built*, not discovered: :func:`~chorus_api.main.build_app` takes a fully
constructed container, so there is no start-up hook that has to run and nothing for a lifespan
event to initialise. Leaving lifespan on would make every cold start wait for a protocol this
application does not implement.

Where the boundaries are
-------------------------
The bearer-token check and the logical-time binding are **middleware inside the ASGI
application**, not code here -- so they apply identically to a request that arrives through
Mangum and to one a contract test drives with an ASGI client, and there is no deployed-only path
that a local test never exercises. This module builds the container, builds the app, and adapts
the event; it makes no decision of its own.

**Cold start touches no network.** The container is built lazily on the first invocation, so
importing this module reads no environment, constructs no client, and makes no call.
"""

from __future__ import annotations

from typing import Any, Final

from chorus_api.main import build_app
from chorus_api.problem_details import PROBLEM_MEDIA_TYPE
from mangum import Mangum
from mangum.adapter import DEFAULT_TEXT_MIME_TYPES

from chorus.settings import Settings
from functions.api.composition import api_settings, build_api_container

LIFESPAN: Final = "off"
"""No start-up hook exists to run: the container is constructed before the app is built."""

TEXT_MIME_TYPES: Final = [*DEFAULT_TEXT_MIME_TYPES, PROBLEM_MEDIA_TYPE]
"""Mangum base64-encodes any response whose media type it does not recognise as text.

``application/problem+json`` is not in its default list, so **every error this API returns**
-- every 401, 404, 409, 422 and 503, which is the entire frozen error contract of
[08-api-design.md](../../docs/architecture/08-api-design.md) -- would reach a browser as an
opaque base64 blob with ``isBase64Encoded: true``. The happy path would look perfect and only
the failures would be unreadable, which is the worst possible way for this to be discovered.

Added explicitly rather than by widening the default, and asserted by a payload-v2 test that
decodes a real error body.
"""

_adapter: Mangum | None = None


def adapter() -> Mangum:
    """Build the application and its event adapter once per execution environment.

    Lazily rather than at import, so importing this module needs no configuration and no AWS
    credential metadata lookup -- which is what lets a handler-import test run with credentials
    disabled and prove that nothing here reaches for one at load time.
    """

    global _adapter
    if _adapter is None:
        container = build_api_container(api_settings(Settings.load()))
        _adapter = Mangum(build_app(container), lifespan=LIFESPAN, text_mime_types=TEXT_MIME_TYPES)
    return _adapter


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """The Lambda entry point. One API Gateway payload-v2 request in, one response out.

    ``context`` is typed loosely because the only thing that reads it is Mangum, which wants the
    runtime's own ``LambdaContext``; nothing in this repository touches a field of it, so a
    stricter annotation would be a claim about a value this module never inspects.
    """

    response: dict[str, Any] = adapter()(event, context)
    return response


__all__ = ["LIFESPAN", "TEXT_MIME_TYPES", "adapter", "handler"]
