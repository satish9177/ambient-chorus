"""Driver-level behaviour that a live endpoint cannot easily be made to exhibit."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from chorus.infrastructure.dynamodb.client import BatchGetOutput, GetItemOutput, QueryOutput
from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver
from chorus.ports.errors import ExternalDependencyError, UnknownTransactionOutcomeError
from chorus.ports.limits import BATCH_GET_MAX_KEYS, TRANSACTION_MAX_OPERATIONS
from chorus.ports.storage import (
    ItemKey,
    KeyAbsent,
    PutItem,
    QueryRequest,
    SortKeyBeginsWith,
    TableName,
)

pytestmark = pytest.mark.anyio

TABLE_NAMES = {
    TableName.CORE: "core-table",
    TableName.SHAREABLE: "shareable-table",
    TableName.AUDIT: "audit-table",
}


def key(index: int) -> ItemKey:
    return ItemKey(table=TableName.CORE, partition_key="NS#TEST_DRIVER", sort_key=f"ITEM#{index}")


@dataclass
class StubClient:
    """A recording stand-in for the narrow DynamoDB client port."""

    batch_pages: list[BatchGetOutput] = field(default_factory=list)
    query_output: QueryOutput = field(default_factory=lambda: QueryOutput(Items=[]))
    get_output: GetItemOutput = field(default_factory=GetItemOutput)
    transact_error: Exception | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def get_item(self, **kwargs: object) -> GetItemOutput:
        self.calls.append(("get_item", dict(kwargs)))
        return self.get_output

    def batch_get_item(self, **kwargs: object) -> BatchGetOutput:
        self.calls.append(("batch_get_item", dict(kwargs)))
        if self.batch_pages:
            return self.batch_pages.pop(0)
        return BatchGetOutput(Responses={}, UnprocessedKeys={})

    def query(self, **kwargs: object) -> QueryOutput:
        self.calls.append(("query", dict(kwargs)))
        return self.query_output

    def put_item(self, **kwargs: object) -> object:
        self.calls.append(("put_item", dict(kwargs)))
        return {}

    def delete_item(self, **kwargs: object) -> object:
        self.calls.append(("delete_item", dict(kwargs)))
        return {}

    def transact_write_items(self, **kwargs: object) -> object:
        self.calls.append(("transact_write_items", dict(kwargs)))
        if self.transact_error is not None:
            raise self.transact_error
        return {}


def driver(client: StubClient) -> DynamoDbStorageDriver:
    return DynamoDbStorageDriver(client=client, table_names=TABLE_NAMES)


async def test_a_read_never_asks_for_more_keys_than_the_batch_limit() -> None:
    client = StubClient()
    keys = tuple(key(index) for index in range(BATCH_GET_MAX_KEYS + 5))

    await driver(client).batch_get_items(keys, consistent=True)

    requests = [call for name, call in client.calls if name == "batch_get_item"]
    assert len(requests) == 2
    sizes = [len(call["RequestItems"]["core-table"]["Keys"]) for call in requests]
    assert sizes == [BATCH_GET_MAX_KEYS, 5]


async def test_a_batch_read_addresses_exactly_one_table() -> None:
    client = StubClient()
    mixed = (
        key(0),
        ItemKey(table=TableName.AUDIT, partition_key="NS#TEST_DRIVER", sort_key="EVENT#1"),
    )

    with pytest.raises(ValueError, match="exactly one table"):
        await driver(client).batch_get_items(mixed, consistent=True)


async def test_unprocessed_keys_are_retried_then_reported() -> None:
    unprocessed = BatchGetOutput(
        Responses={"core-table": []},
        UnprocessedKeys={
            "core-table": {"Keys": [{"PK": {"S": "NS#TEST_DRIVER"}, "SK": {"S": "ITEM#0"}}]}
        },
    )
    client = StubClient(batch_pages=[unprocessed, unprocessed, unprocessed])

    with pytest.raises(ExternalDependencyError):
        await driver(client).batch_get_items((key(0),), consistent=True)

    assert len([call for name, call in client.calls if name == "batch_get_item"]) == 3


async def test_a_read_declares_its_consistency_explicitly() -> None:
    client = StubClient()

    await driver(client).get_item(key(0), consistent=True)
    await driver(client).query(
        QueryRequest(
            table=TableName.CORE,
            partition_key="NS#TEST_DRIVER",
            sort_key=SortKeyBeginsWith("ITEM#"),
            consistent=False,
        )
    )

    assert client.calls[0][1]["ConsistentRead"] is True
    assert client.calls[1][1]["ConsistentRead"] is False


async def test_a_query_is_always_bounded_to_one_partition() -> None:
    client = StubClient()

    await driver(client).query(
        QueryRequest(
            table=TableName.CORE, partition_key="NS#TEST_DRIVER", sort_key=SortKeyBeginsWith("A#")
        )
    )

    call = client.calls[0][1]
    assert call["KeyConditionExpression"] == "#pk = :pk AND begins_with(#sk, :sk)"
    assert call["Limit"] == 100


async def test_an_oversized_transaction_is_rejected_before_the_network() -> None:
    client = StubClient()
    operations = tuple(
        PutItem(
            key=key(index),
            item={"PK": "NS#TEST_DRIVER", "SK": f"ITEM#{index}"},
            condition=KeyAbsent(),
        )
        for index in range(TRANSACTION_MAX_OPERATIONS + 1)
    )

    with pytest.raises(Exception, match="TRANSACTION_LIMIT_EXCEEDED"):
        await driver(client).transact_write(operations, client_request_token="token")

    assert client.calls == []


async def test_a_lost_transaction_response_becomes_an_unknown_outcome() -> None:
    client = StubClient(transact_error=ReadTimeoutError(endpoint_url="http://endpoint.invalid"))
    operation = PutItem(
        key=key(0), item={"PK": "NS#TEST_DRIVER", "SK": "ITEM#0"}, condition=KeyAbsent()
    )

    with pytest.raises(UnknownTransactionOutcomeError):
        await driver(client).transact_write((operation,), client_request_token="token")


async def test_a_rejected_transaction_never_leaks_aws_text() -> None:
    secret = "SECRET_SENTINEL_RESPONSE"
    client = StubClient(
        transact_error=ClientError(
            {"Error": {"Code": "ValidationException", "Message": secret}}, "TransactWriteItems"
        )
    )
    operation = PutItem(
        key=key(0), item={"PK": "NS#TEST_DRIVER", "SK": "ITEM#0"}, condition=KeyAbsent()
    )

    with pytest.raises(ExternalDependencyError) as raised:
        await driver(client).transact_write((operation,), client_request_token="token")

    assert secret not in f"{raised.value!s} {raised.value!r}"


async def test_the_wire_token_binds_the_plan_token_to_the_rendered_request() -> None:
    """The same logical token against different physical tables is a different request."""

    first = StubClient()
    second = StubClient()
    operation = PutItem(
        key=key(0), item={"PK": "NS#TEST_DRIVER", "SK": "ITEM#0"}, condition=KeyAbsent()
    )
    other_tables = {**TABLE_NAMES, TableName.CORE: "core-table-2"}

    await driver(first).transact_write((operation,), client_request_token="token")
    await DynamoDbStorageDriver(client=second, table_names=other_tables).transact_write(
        (operation,), client_request_token="token"
    )

    assert first.calls[0][1]["ClientRequestToken"] != second.calls[0][1]["ClientRequestToken"]


async def test_the_wire_token_is_stable_for_an_identical_retry() -> None:
    client = StubClient()
    operation = PutItem(
        key=key(0), item={"PK": "NS#TEST_DRIVER", "SK": "ITEM#0"}, condition=KeyAbsent()
    )

    await driver(client).transact_write((operation,), client_request_token="token")
    await driver(client).transact_write((operation,), client_request_token="token")

    tokens = {call["ClientRequestToken"] for _, call in client.calls}
    assert len(tokens) == 1
    assert len(next(iter(tokens))) == 36


async def test_a_condition_is_rendered_with_generated_placeholders() -> None:
    client = StubClient()
    operation = PutItem(
        key=key(0), item={"PK": "NS#TEST_DRIVER", "SK": "ITEM#0"}, condition=KeyAbsent()
    )

    await driver(client).write_item(operation)

    call = client.calls[0][1]
    assert call["ConditionExpression"] == "attribute_not_exists(#a1) AND attribute_not_exists(#a2)"
    assert call["ExpressionAttributeNames"] == {"#a1": "PK", "#a2": "SK"}


async def test_the_wire_token_discloses_no_request_content() -> None:
    """The token is sent to AWS in the clear, so it must be a digest of the request."""

    client = StubClient()
    private = "PRIVATE_SENTINEL_VALUE"
    operation = PutItem(
        key=key(0),
        item={"PK": "NS#TEST_DRIVER", "SK": "ITEM#0", "text": private},
        condition=KeyAbsent(),
    )

    await driver(client).transact_write((operation,), client_request_token=private)

    token = client.calls[0][1]["ClientRequestToken"]
    assert isinstance(token, str)
    assert private not in token
    assert private.lower() not in token
    assert len(token) == 36
