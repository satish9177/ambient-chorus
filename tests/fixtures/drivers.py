"""Storage-driver harness shared by every contract suite.

A contract test that only ever ran against the in-memory emulator would prove that the
emulator agrees with itself. These helpers exist so more than one suite can run the same
expectations against both drivers without copying the table lifecycle, and so "DynamoDB Local
was not reachable" is a visible skip rather than a silently narrower run.
"""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Iterator
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

SKIP_REASON = f"DynamoDB Local is not reachable at {ENDPOINT}; start it with `docker compose up -d`"


def endpoint_host_port() -> tuple[str, int]:
    remainder = ENDPOINT.split("://", 1)[-1]
    host, _, port = remainder.partition(":")
    return host or "127.0.0.1", int(port or "8000")


def _reachable() -> bool:
    host, port = endpoint_host_port()
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


DYNAMODB_LOCAL_AVAILABLE = _reachable()

DRIVER_PARAMS = [
    pytest.param("memory", id="memory"),
    pytest.param(
        "dynamodb-local",
        id="dynamodb-local",
        marks=pytest.mark.skipif(not DYNAMODB_LOCAL_AVAILABLE and not REQUIRED, reason=SKIP_REASON),
    ),
]


def admin_client() -> Any:
    """Table administration is a test concern, so it never touches the narrow client port."""

    return boto3.client(
        "dynamodb",
        region_name=REGION,
        endpoint_url=ENDPOINT,
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )


def create_tables(client: Any, names: dict[TableName, str]) -> None:
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


def delete_tables(client: Any, names: dict[TableName, str]) -> None:
    for name in names.values():
        with contextlib.suppress(ClientError, BotoCoreError):
            client.delete_table(TableName=name)


def table_names(prefix: str) -> dict[TableName, str]:
    suffix = uuid4().hex[:12]
    return {
        TableName.CORE: f"chorus-core-{prefix}-{suffix}",
        TableName.SHAREABLE: f"chorus-shareable-{prefix}-{suffix}",
        TableName.AUDIT: f"chorus-audit-{prefix}-{suffix}",
    }


def storage_driver(param: str, *, prefix: str) -> Iterator[StorageDriver]:
    """Yield one empty driver of the requested kind, cleaning up any tables it created."""

    if param == "memory":
        yield InMemoryStorageDriver()
        return
    if not DYNAMODB_LOCAL_AVAILABLE:
        pytest.fail(SKIP_REASON)
    names = table_names(prefix)
    admin = admin_client()
    create_tables(admin, names)
    # The production client factory takes no credentials, so the local endpoint gets the
    # placeholder credentials DynamoDB Local ignores through the standard environment.
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "local")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")
    client = create_dynamodb_client(region_name=REGION, endpoint_url=ENDPOINT)
    try:
        yield DynamoDbStorageDriver(client=client, table_names=names)
    finally:
        delete_tables(admin, names)
