"""In-memory storage driver used by the ``test`` environment.

This is not a dictionary with a repository bolted on. It emulates the exact DynamoDB
semantics the repositories depend on: create-only and version conditions, condition-only
participants, all-or-nothing transactions, the hundred-operation transaction bound, the
rejection of two operations addressing one item, single-partition queries with sort-key
ranges, and the difference between a strongly and an eventually consistent read.

``stale_eventual_reads`` makes every eventually consistent read return the previous committed
value of an item. Real DynamoDB may legally do this, so enabling it in the contract suite
turns "an authorization path accidentally used an eventual read" from a silent risk into a
failing test.

Transaction-token deduplication expires here exactly as it does on DynamoDB. An emulator that
remembered a token forever would let a contract test assert that re-committing an identical
plan is a silent no-op, which stops being true ten minutes later on the real service -- there
the plan's own create-only and version conditions are what reject it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from chorus.domain.time import Clock, SystemClock
from chorus.infrastructure.dynamodb.codec import require_addressed_item
from chorus.ports.errors import (
    IdempotencyConflictError,
    PersistenceConflictError,
    TransactionLimitExceededError,
)
from chorus.ports.limits import TRANSACTION_MAX_OPERATIONS, TRANSACTION_TOKEN_WINDOW_SECONDS
from chorus.ports.storage import (
    AllOf,
    AnyOf,
    AttributeAtMostNumber,
    AttributeEqualsNumber,
    AttributeEqualsString,
    CheckItem,
    DeleteItem,
    ItemCondition,
    ItemKey,
    KeyAbsent,
    KeyPresent,
    PutItem,
    QueryRequest,
    QueryResult,
    SortKeyAll,
    SortKeyBeginsWith,
    SortKeyBetween,
    SortKeyCondition,
    StoredItem,
    StoredValue,
    TableName,
    WriteOperation,
)

type _Address = tuple[TableName, str, str]

_WRITE: Final = "WRITE"
_TRANSACTION: Final = "TRANSACTION"


def _copy(value: StoredValue) -> StoredValue:
    """Deep-copy a stored value so callers can never mutate committed state."""

    if isinstance(value, tuple):
        return tuple(_copy(item) for item in value)
    if isinstance(value, Mapping):
        return {key: _copy(item) for key, item in value.items()}
    return value


def _copy_item(item: StoredItem) -> StoredItem:
    return {key: _copy(value) for key, value in item.items()}


def _evaluate(condition: ItemCondition, item: StoredItem | None) -> bool:
    match condition:
        case KeyAbsent():
            return item is None
        case KeyPresent():
            return item is not None
        case AttributeEqualsString(name=name, value=value):
            if item is None:
                return False
            stored = item.get(name)
            return isinstance(stored, str) and stored == value
        case AttributeEqualsNumber(name=name, value=value):
            if item is None:
                return False
            stored = item.get(name)
            return isinstance(stored, int) and not isinstance(stored, bool) and stored == value
        case AttributeAtMostNumber(name=name, value=value):
            if item is None:
                return False
            stored = item.get(name)
            return isinstance(stored, int) and not isinstance(stored, bool) and stored <= value
        case AllOf(conditions):
            return all(_evaluate(inner, item) for inner in conditions)
        case AnyOf(conditions):
            return any(_evaluate(inner, item) for inner in conditions)
        case _:  # pragma: no cover - the condition union is closed
            raise AssertionError("unreachable item condition")


def _require_addressed_item(operation: WriteOperation) -> None:
    if isinstance(operation, PutItem):
        require_addressed_item(operation.key, operation.item)


def _matches_sort_key(condition: SortKeyCondition, sort_key: str) -> bool:
    match condition:
        case SortKeyAll():
            return True
        case SortKeyBeginsWith(prefix):
            return sort_key.startswith(prefix)
        case SortKeyBetween(low=low, high=high):
            return low <= sort_key <= high
        case _:  # pragma: no cover - the condition union is closed
            raise AssertionError("unreachable sort key condition")


@dataclass(slots=True)
class InMemoryStorageDriver:
    """Deterministic emulation of the approved DynamoDB data-plane operations."""

    stale_eventual_reads: bool = False
    clock: Clock = field(default_factory=SystemClock)
    _current: dict[_Address, StoredItem] = field(default_factory=dict, init=False)
    _previous: dict[_Address, StoredItem | None] = field(default_factory=dict, init=False)
    _tokens: dict[str, tuple[datetime, tuple[WriteOperation, ...]]] = field(
        default_factory=dict, init=False
    )

    @staticmethod
    def _address(key: ItemKey) -> _Address:
        return (key.table, key.partition_key, key.sort_key)

    def _read(self, address: _Address, *, consistent: bool) -> StoredItem | None:
        if not consistent and self.stale_eventual_reads and address in self._previous:
            stale = self._previous[address]
            return None if stale is None else _copy_item(stale)
        item = self._current.get(address)
        return None if item is None else _copy_item(item)

    def _apply(self, operation: WriteOperation) -> None:
        if isinstance(operation, CheckItem):
            return
        address = self._address(operation.key)
        self._previous[address] = self._current.get(address)
        if isinstance(operation, PutItem):
            self._current[address] = _copy_item(operation.item)
        else:
            self._current.pop(address, None)

    async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
        return self._read(self._address(key), consistent=consistent)

    async def batch_get_items(
        self, keys: tuple[ItemKey, ...], *, consistent: bool
    ) -> tuple[StoredItem, ...]:
        if not keys:
            return ()
        # The real adapter refuses a batch that spans tables, because one BatchGetItem request
        # names one table per entry and a repository that mixed them would fail only against
        # DynamoDB. Rejecting it here keeps that a contract rather than an adapter accident.
        if len({key.table for key in keys}) != 1:
            raise ValueError("a batch read addresses exactly one table")
        found: list[StoredItem] = []
        for key in keys:
            item = self._read(self._address(key), consistent=consistent)
            if item is not None:
                found.append(item)
        return tuple(found)

    async def query(self, request: QueryRequest) -> QueryResult:
        matching = sorted(
            (
                address
                for address in self._current
                if address[0] is request.table
                and address[1] == request.partition_key
                and _matches_sort_key(request.sort_key, address[2])
            ),
            key=lambda address: address[2],
            reverse=not request.ascending,
        )
        start = request.exclusive_start_sort_key
        if start is not None:
            matching = [
                address
                for address in matching
                if (address[2] > start if request.ascending else address[2] < start)
            ]
        page = matching[: request.limit]
        items = tuple(
            item
            for item in (self._read(address, consistent=request.consistent) for address in page)
            if item is not None
        )
        # DynamoDB returns a continuation key whenever it stopped at the limit, even when
        # nothing remains, so a caller can legitimately receive a cursor whose next page is
        # empty. Reproducing that here stops a caller assuming "a cursor means more items".
        last_key = page[-1][2] if len(page) == request.limit else None
        return QueryResult(items=items, last_evaluated_sort_key=last_key)

    async def write_item(self, operation: PutItem | DeleteItem) -> None:
        _require_addressed_item(operation)
        address = self._address(operation.key)
        if not _evaluate(operation.condition, self._current.get(address)):
            # The reference is the operation family, exactly as the DynamoDB adapter reports
            # it. A key would say which item lost the race, which the real adapter never
            # reveals and no caller may come to depend on.
            raise PersistenceConflictError(_WRITE)
        self._apply(operation)

    async def transact_write(
        self, operations: tuple[WriteOperation, ...], *, client_request_token: str
    ) -> None:
        if not operations:
            raise ValueError("a transaction requires at least one operation")
        if len(operations) > TRANSACTION_MAX_OPERATIONS:
            raise TransactionLimitExceededError(_TRANSACTION)
        # DynamoDB deduplicates a repeated request under the same client request token and
        # rejects a token reused with a different request, but only inside a bounded window.
        # Both the deduplication and its expiry are emulated, so a caller cannot depend on
        # either one lasting longer than the real service provides.
        now = self.clock.now()
        self._tokens = {
            token: entry
            for token, entry in self._tokens.items()
            if (now - entry[0]).total_seconds() < TRANSACTION_TOKEN_WINDOW_SECONDS
        }
        previous = self._tokens.get(client_request_token)
        if previous is not None:
            if previous[1] != operations:
                raise IdempotencyConflictError(_TRANSACTION)
            return
        addresses = tuple(self._address(operation.key) for operation in operations)
        if len(set(addresses)) != len(addresses):
            raise ValueError("a transaction cannot address the same item twice")
        for operation, address in zip(operations, addresses, strict=True):
            _require_addressed_item(operation)
            if not _evaluate(operation.condition, self._current.get(address)):
                raise PersistenceConflictError(_TRANSACTION)
        for operation in operations:
            self._apply(operation)
        self._tokens[client_request_token] = (now, operations)
