"""DynamoDB implementation of the storage driver port.

Blocking SDK calls are contained here and executed through a bounded worker thread, so the
async application layer never blocks and no thread pool grows without limit. Every SDK failure
passes through the single error-translation boundary before it leaves this module.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Final, TypeVar
from uuid import UUID

import anyio
from anyio import CapacityLimiter
from botocore.exceptions import BotoCoreError, ClientError

from chorus.infrastructure.dynamodb.attributes import (
    AttributeMap,
    decode_item,
    encode_item,
    encode_value,
)
from chorus.infrastructure.dynamodb.client import DynamoDbClient
from chorus.infrastructure.dynamodb.codec import (
    ATTR_PARTITION_KEY,
    ATTR_SORT_KEY,
    require_addressed_item,
)
from chorus.infrastructure.dynamodb.conditions import render_condition
from chorus.infrastructure.dynamodb.errors import DynamoOperation, map_transport_error
from chorus.ports.errors import ExternalDependencyError, TransactionLimitExceededError
from chorus.ports.limits import BATCH_GET_MAX_KEYS, TRANSACTION_MAX_OPERATIONS
from chorus.ports.storage import (
    CheckItem,
    DeleteItem,
    ItemKey,
    PutItem,
    QueryRequest,
    QueryResult,
    SortKeyAll,
    SortKeyBeginsWith,
    SortKeyBetween,
    SortKeyCondition,
    StoredItem,
    TableName,
    WriteOperation,
)

DEFAULT_THREAD_LIMIT: Final = 8
UNPROCESSED_KEY_ATTEMPTS: Final = 3

ResultT = TypeVar("ResultT")


def _wire_token(plan_token: str, items: list[dict[str, object]]) -> str:
    """Bind the plan's token to the exact request this driver is about to send.

    A plan names logical tables, so two deployments -- or two test fixtures -- can express
    the same plan against different physical tables. DynamoDB rejects a reused token whose
    request differs, and silently deduplicates one whose request matches, so the token has
    to cover the rendered request as well as the plan that produced it. A retry re-renders
    the identical request and therefore reproduces the identical token.
    """

    digest = sha256(plan_token.encode("utf-8"))
    digest.update(b"")
    digest.update(json.dumps(items, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return str(UUID(bytes=digest.digest()[:16], version=4))


def _sort_key_expression(condition: SortKeyCondition) -> tuple[str, AttributeMap] | None:
    match condition:
        case SortKeyAll():
            return None
        case SortKeyBeginsWith(prefix):
            return "begins_with(#sk, :sk)", {":sk": encode_value(prefix)}
        case SortKeyBetween(low=low, high=high):
            return (
                "#sk BETWEEN :sklow AND :skhigh",
                {":sklow": encode_value(low), ":skhigh": encode_value(high)},
            )
        case _:  # pragma: no cover - the condition union is closed
            raise AssertionError("unreachable sort key condition")


@dataclass(slots=True)
class DynamoDbStorageDriver:
    """Storage driver backed by DynamoDB or a local DynamoDB-compatible endpoint."""

    client: DynamoDbClient
    table_names: Mapping[TableName, str]
    limiter: CapacityLimiter = field(default_factory=lambda: CapacityLimiter(DEFAULT_THREAD_LIMIT))

    def _table(self, table: TableName) -> str:
        name = self.table_names.get(table)
        if name is None:  # pragma: no cover - composition roots supply all three
            raise ExternalDependencyError(table.value, retryable=False)
        return name

    async def _call(
        self, function: Callable[[], ResultT], *, operation: DynamoOperation
    ) -> ResultT:
        try:
            return await anyio.to_thread.run_sync(function, limiter=self.limiter)
        except (ClientError, BotoCoreError) as error:
            raise map_transport_error(error, operation=operation) from None

    def _key_map(self, key: ItemKey) -> AttributeMap:
        return {
            ATTR_PARTITION_KEY: encode_value(key.partition_key),
            ATTR_SORT_KEY: encode_value(key.sort_key),
        }

    async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
        table = self._table(key.table)
        key_map = self._key_map(key)

        def call() -> object:
            return self.client.get_item(TableName=table, Key=key_map, ConsistentRead=consistent)

        response = await self._call(call, operation=DynamoOperation.READ)
        if not isinstance(response, dict):  # pragma: no cover - SDK contract
            raise ExternalDependencyError("GET_ITEM")
        raw = response.get("Item")
        if raw is None:
            return None
        return decode_item(raw)

    async def batch_get_items(
        self, keys: tuple[ItemKey, ...], *, consistent: bool
    ) -> tuple[StoredItem, ...]:
        if not keys:
            return ()
        tables = {key.table for key in keys}
        if len(tables) != 1:
            raise ValueError("a batch read addresses exactly one table")
        table = self._table(next(iter(tables)))
        found: list[StoredItem] = []
        for start in range(0, len(keys), BATCH_GET_MAX_KEYS):
            chunk = keys[start : start + BATCH_GET_MAX_KEYS]
            pending: list[AttributeMap] = [self._key_map(key) for key in chunk]
            for _ in range(UNPROCESSED_KEY_ATTEMPTS):
                if not pending:
                    break
                request: dict[str, object] = {
                    table: {"Keys": list(pending), "ConsistentRead": consistent}
                }

                def call(request: dict[str, object] = request) -> object:
                    return self.client.batch_get_item(RequestItems=request)

                response = await self._call(call, operation=DynamoOperation.READ)
                if not isinstance(response, dict):  # pragma: no cover - SDK contract
                    raise ExternalDependencyError("BATCH_GET")
                responses = response.get("Responses") or {}
                for raw in responses.get(table, []):
                    found.append(decode_item(raw))
                unprocessed = response.get("UnprocessedKeys") or {}
                entry = unprocessed.get(table)
                pending = list(entry["Keys"]) if isinstance(entry, dict) else []
            if pending:
                raise ExternalDependencyError("BATCH_GET")
        return tuple(found)

    async def query(self, request: QueryRequest) -> QueryResult:
        table = self._table(request.table)
        names = {"#pk": ATTR_PARTITION_KEY}
        values: AttributeMap = {":pk": encode_value(request.partition_key)}
        expression = "#pk = :pk"
        sort_condition = _sort_key_expression(request.sort_key)
        if sort_condition is not None:
            fragment, sort_values = sort_condition
            names["#sk"] = ATTR_SORT_KEY
            values.update(sort_values)
            expression = f"{expression} AND {fragment}"
        arguments: dict[str, object] = {
            "TableName": table,
            "KeyConditionExpression": expression,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
            "ConsistentRead": request.consistent,
            "Limit": request.limit,
            "ScanIndexForward": request.ascending,
        }
        if request.exclusive_start_sort_key is not None:
            arguments["ExclusiveStartKey"] = {
                ATTR_PARTITION_KEY: encode_value(request.partition_key),
                ATTR_SORT_KEY: encode_value(request.exclusive_start_sort_key),
            }

        def call() -> object:
            return self.client.query(**arguments)

        response = await self._call(call, operation=DynamoOperation.READ)
        if not isinstance(response, dict):  # pragma: no cover - SDK contract
            raise ExternalDependencyError("QUERY")
        items = tuple(decode_item(raw) for raw in response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        last_sort_key: str | None = None
        if isinstance(last_key, dict):
            decoded = decode_item(last_key)
            candidate = decoded.get(ATTR_SORT_KEY)
            last_sort_key = candidate if isinstance(candidate, str) else None
        return QueryResult(items=items, last_evaluated_sort_key=last_sort_key)

    async def write_item(self, operation: PutItem | DeleteItem) -> None:
        table = self._table(operation.key.table)
        rendered = render_condition(operation.condition)
        arguments: dict[str, object] = {
            "TableName": table,
            "ConditionExpression": rendered.expression,
        }
        if rendered.names is not None:
            arguments["ExpressionAttributeNames"] = rendered.names
        if rendered.values is not None:
            arguments["ExpressionAttributeValues"] = rendered.values
        if isinstance(operation, PutItem):
            require_addressed_item(operation.key, operation.item)
            arguments["Item"] = encode_item(operation.item)

            def call() -> object:
                return self.client.put_item(**arguments)

        else:
            arguments["Key"] = self._key_map(operation.key)

            def call() -> object:
                return self.client.delete_item(**arguments)

        await self._call(call, operation=DynamoOperation.WRITE)

    def _transact_item(self, operation: WriteOperation) -> dict[str, object]:
        table = self._table(operation.key.table)
        rendered = render_condition(operation.condition)
        body: dict[str, object] = {
            "TableName": table,
            "ConditionExpression": rendered.expression,
        }
        if rendered.names is not None:
            body["ExpressionAttributeNames"] = rendered.names
        if rendered.values is not None:
            body["ExpressionAttributeValues"] = rendered.values
        match operation:
            case PutItem(item=item):
                require_addressed_item(operation.key, item)
                body["Item"] = encode_item(item)
                return {"Put": body}
            case DeleteItem():
                body["Key"] = self._key_map(operation.key)
                return {"Delete": body}
            case CheckItem():
                body["Key"] = self._key_map(operation.key)
                return {"ConditionCheck": body}
            case _:  # pragma: no cover - the operation union is closed
                raise AssertionError("unreachable write operation")

    async def transact_write(
        self, operations: tuple[WriteOperation, ...], *, client_request_token: str
    ) -> None:
        if not operations:
            raise ValueError("a transaction requires at least one operation")
        if len(operations) > TRANSACTION_MAX_OPERATIONS:
            raise TransactionLimitExceededError(DynamoOperation.TRANSACTION.value)
        items = [self._transact_item(operation) for operation in operations]
        token = _wire_token(client_request_token, items)

        def call() -> object:
            return self.client.transact_write_items(TransactItems=items, ClientRequestToken=token)

        await self._call(call, operation=DynamoOperation.TRANSACTION)
