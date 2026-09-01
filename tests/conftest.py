"""Shared pytest configuration.

The persistence suite is asynchronous, so anyio's plugin needs a backend fixture. Only the
asyncio backend is used, matching the runtime the application will run under.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"
