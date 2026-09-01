"""Driver fixtures for the persistence contract suite.

Every test in this package runs twice: once against the in-memory driver and once against a
real DynamoDB Local endpoint. That is the whole point of the suite -- an expectation that only
holds for the emulator is not a contract.

DynamoDB Local is skipped when the endpoint is not reachable, and the skip says exactly why.
Setting ``CHORUS_REQUIRE_DYNAMODB_LOCAL=1`` turns that skip into a failure, which is how CI
proves the integration path actually ran.
"""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError

from chorus.infrastructure.dynamodb.client import create_dynamodb_client
from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.ports.storage import StorageDriver, TableName

ENDPOINT = os.environ.get("CHORUS_DYNAMODB_LOCAL_ENDPOINT", "http://127.0.0.1:8000")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
REQUIRED = os.environ.get("CHORUS_REQUIRE_DYNAMODB_LOCAL") == "1"


def _endpoint_host_port() -> tuple[str, int]:
    remainder = ENDPOINT.split("://", 1)[-1]
    host, _, port = remainder.partition(":")
    return host or "127.0.0.1", int(port or "8000")


def _reachable() -> bool:
    host, port = _endpoint_host_port()
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


DYNAMODB_LOCAL_AVAILABLE = _reachable()

SKIP_REASON = f"DynamoDB Local is not reachable at {ENDPOINT}; start it with `docker compose up -d`"


def _admin_client() -> Any:
    """Table administration is a test concern, so it never touches the narrow client port."""

    return boto3.client(
        "dynamodb",
        region_name=REGION,
        endpoint_url=ENDPOINT,
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )


def _create_tables(client: Any, names: dict[TableName, str]) -> None:
    for name in names.values():
        client.create_table(
            TableName=name,
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )


def _delete_tables(client: Any, names: dict[TableName, str]) -> None:
    for name in names.values():
        with contextlib.suppress(ClientError, BotoCoreError):
            client.delete_table(TableName=name)


@pytest.fixture(
    params=[
        pytest.param("memory", id="memory"),
        pytest.param(
            "dynamodb-local",
            id="dynamodb-local",
            marks=pytest.mark.skipif(
                not DYNAMODB_LOCAL_AVAILABLE and not REQUIRED, reason=SKIP_REASON
            ),
        ),
    ]
)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    """Yield one empty storage driver per test, for each adapter under contract."""

    if request.param == "memory":
        yield InMemoryStorageDriver()
        return
    if not DYNAMODB_LOCAL_AVAILABLE:
        pytest.fail(SKIP_REASON)
    suffix = uuid4().hex[:12]
    names = {
        TableName.CORE: f"chorus-core-test-{suffix}",
        TableName.SHAREABLE: f"chorus-shareable-test-{suffix}",
        TableName.AUDIT: f"chorus-audit-test-{suffix}",
    }
    admin = _admin_client()
    _create_tables(admin, names)
    # The production client factory takes no credentials, so the local endpoint gets the
    # placeholder credentials DynamoDB Local ignores through the standard environment.
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "local")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")
    client = create_dynamodb_client(region_name=REGION, endpoint_url=ENDPOINT)
    try:
        yield DynamoDbStorageDriver(client=client, table_names=names)
    finally:
        _delete_tables(admin, names)


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
    suffix = uuid4().hex[:12]
    names = {
        TableName.CORE: f"chorus-core-wire-{suffix}",
        TableName.SHAREABLE: f"chorus-shareable-wire-{suffix}",
        TableName.AUDIT: f"chorus-audit-wire-{suffix}",
    }
    admin = _admin_client()
    _create_tables(admin, names)
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
        _delete_tables(admin, names)
