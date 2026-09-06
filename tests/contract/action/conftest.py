"""Driver fixtures for the Phase 7 proposal and Phase 8 approval/send contract suites."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.fixtures.action import ActionHarness
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver
from tests.fixtures.send import SendHarness

from chorus.ports.storage import StorageDriver


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    """Yield one empty storage driver per test, for each adapter under contract."""

    yield from storage_driver(str(request.param), prefix="action")


@pytest.fixture
def harness(storage: StorageDriver) -> ActionHarness:
    return ActionHarness(driver=storage)


@pytest.fixture
def send_harness(harness: ActionHarness) -> SendHarness:
    """The Phase-8 harness over the same driver as the proposal it decides about.

    Built on the Phase-7 one rather than beside it, because a decision that bound a fabricated
    proposal would be checking the fixture rather than the production digest chain.
    """

    return SendHarness(action=harness)
