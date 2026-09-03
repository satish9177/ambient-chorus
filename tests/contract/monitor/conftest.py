"""Driver fixtures for the Phase 3 ingestion and Monitor contract suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver
from tests.fixtures.monitor import MonitorHarness

from chorus.ports.storage import StorageDriver


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    """Yield one empty storage driver per test, for each adapter under contract."""

    yield from storage_driver(str(request.param), prefix="monitor")


@pytest.fixture
def harness(storage: StorageDriver) -> MonitorHarness:
    return MonitorHarness(driver=storage)
