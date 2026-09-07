"""Driver fixtures for the Phase 9 inbound reply, commitment, and watcher contract suites."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.fixtures.action import ActionHarness
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver
from tests.fixtures.reply import ReplyHarness
from tests.fixtures.send import SendHarness

from chorus.ports.storage import StorageDriver


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    """Yield one empty storage driver per test, for each adapter under contract."""

    yield from storage_driver(str(request.param), prefix="reply")


@pytest.fixture
def reply_harness(storage: StorageDriver) -> ReplyHarness:
    """The Phase-9 harness over the same driver as the send whose reply it ingests.

    Built on the Phase-8 harness rather than beside it, because a reply that correlated against
    a fabricated locator would be checking the fixture rather than the production channel.
    """

    return ReplyHarness(send=SendHarness(action=ActionHarness(driver=storage)))
