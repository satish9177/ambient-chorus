"""Driver fixtures for the Phase 6 compile contract suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.fixtures.compile import CompileHarness
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver

from chorus.ports.storage import StorageDriver


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    """Yield one empty storage driver per test, for each adapter under contract."""

    yield from storage_driver(str(request.param), prefix="compile")


@pytest.fixture
def harness(storage: StorageDriver) -> CompileHarness:
    return CompileHarness(driver=storage)
