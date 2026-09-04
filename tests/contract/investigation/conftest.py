"""Driver fixtures for the Phase 5 investigation contract suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver
from tests.fixtures.investigation import InvestigationHarness

from chorus.ports.storage import StorageDriver


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    """Yield one empty storage driver per test, for each adapter under contract."""

    yield from storage_driver(str(request.param), prefix="investigation")


@pytest.fixture
def harness(storage: StorageDriver) -> InvestigationHarness:
    return InvestigationHarness(driver=storage)
