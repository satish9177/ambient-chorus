"""The runnable local ASGI entry point.

``uv run chorus-api serve`` (see ``pyproject.toml``'s ``[project.scripts]``) runs uvicorn over
the ``app`` module attribute this file exposes, built from :func:`build_local_container` over
:class:`~chorus.settings.Settings` loaded from the process environment. No AWS credentials, no
network call, no deployed adapter -- see :mod:`chorus.composition.local` for exactly what is and
is not wired.
"""

from __future__ import annotations

from chorus.composition.local import build_local_container
from chorus.settings import Settings
from chorus_api.main import build_app

app = build_app(build_local_container(Settings()))

__all__ = ["app"]
