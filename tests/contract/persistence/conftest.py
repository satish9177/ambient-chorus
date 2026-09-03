"""Driver fixtures for the persistence contract suite.

Every test in this package runs twice: once against the in-memory driver and once against a
real DynamoDB Local endpoint. That is the whole point of the suite -- an expectation that only
holds for the emulator is not a contract.

DynamoDB Local is skipped when the endpoint is not reachable, and the skip says exactly why.
Setting ``CHORUS_REQUIRE_DYNAMODB_LOCAL=1`` turns that skip into a failure, which is how CI
proves the integration path actually ran.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from tests.fixtures.drivers import (
    DRIVER_PARAMS,
    DYNAMODB_LOCAL_AVAILABLE,
    ENDPOINT,
    REGION,
    REQUIRED,
    SKIP_REASON,
    admin_client,
    create_tables,
    delete_tables,
    storage_driver,
    table_names,
)

from chorus.infrastructure.dynamodb.client import create_dynamodb_client
from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.ports.storage import StorageDriver


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    """Yield one empty storage driver per test, for each adapter under contract."""

    yield from storage_driver(str(request.param), prefix="test")


@pytest.fixture
def memory() -> InMemoryStorageDriver:
    """An in-memory driver for expectations that need injected staleness."""

    return InMemoryStorageDriver()


@dataclass(frozen=True, slots=True)
class WireHarness:
    """A driver over the *production* client factory, plus the wire attempts it makes.

    Everything else in this suite injects faults above the storage port, which is exactly
    where an SDK-level retry would hide. This harness keeps botocore in the picture: the
    client is the one ``create_dynamodb_client`` builds, and ``attempts`` is appended to by a
    ``before-send`` handler, so one entry is one outgoing HTTP request.
    """

    client: Any
    driver: DynamoDbStorageDriver
    attempts: list[str]

    def transact_attempts(self) -> int:
        return sum(1 for target in self.attempts if target.endswith("TransactWriteItems"))


@pytest.fixture
def wire() -> Iterator[WireHarness]:
    """Yield a driver built exactly as a composition root builds it, against DynamoDB Local."""

    if not DYNAMODB_LOCAL_AVAILABLE:
        if REQUIRED:
            pytest.fail(SKIP_REASON)
        pytest.skip(SKIP_REASON)
    names = table_names("wire")
    admin = admin_client()
    create_tables(admin, names)
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "local")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")
    # The narrow ``DynamoDbClient`` protocol deliberately exposes only the six approved calls,
    # so observing the wire means reaching past it to botocore's own event system. That is a
    # test concern, and it is exactly what makes this harness able to count real attempts.
    client: Any = create_dynamodb_client(region_name=REGION, endpoint_url=ENDPOINT)
    attempts: list[str] = []

    def record(request: Any, **_: object) -> None:
        target = request.headers.get("X-Amz-Target", "")
        attempts.append(target.decode() if isinstance(target, bytes) else str(target))
        return None

    client.meta.events.register_first("before-send.dynamodb", record)
    try:
        yield WireHarness(
            client=client,
            driver=DynamoDbStorageDriver(client=client, table_names=names),
            attempts=attempts,
        )
    finally:
        delete_tables(admin, names)
