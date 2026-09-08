"""Runnable composition roots.

Everything under ``tests/fixtures`` wires production classes together for one test file's own
isolated namespace. Nothing there may be imported by a shipped entry point. This package is the
opposite: it is imported by :mod:`chorus_api.asgi` and by the ``chorus-api``/``chorus-demo``/
``chorus-openapi`` commands, and it wires the same production classes against one concrete local
world -- the frozen ``elevator/v1`` demo fixture -- so a developer or a presenter can point a
browser or a CLI at a real, running application with no AWS credentials.
"""

from __future__ import annotations
