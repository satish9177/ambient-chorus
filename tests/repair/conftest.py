"""Storage-driver fixtures for the Phase-7 Codex repair-regression group.

The same two drivers every other contract suite runs against, so a repair proved here is
proved against DynamoDB Local and not only against the in-memory emulator.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from chorus.ports.storage import StorageDriver
from tests.contract.api.conftest import ApiHarness, build_harness
from tests.fixtures.action import ActionHarness
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    yield from storage_driver(str(request.param), prefix="repair")


@pytest.fixture
def harness(storage: StorageDriver) -> ActionHarness:
    return ActionHarness(driver=storage)


@pytest.fixture
def api(storage: StorageDriver) -> Iterator[ApiHarness]:
    """The real FastAPI application over the real container, with a recording dispatcher.

    Built through the API suite's own factory rather than a second wiring, so a route
    regression here exercises the same composition root the API contract tests do.
    """

    built = build_harness(storage, "recording")
    with built.client:
        yield built
