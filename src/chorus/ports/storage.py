"""Narrow table-driver port shared by the DynamoDB and in-memory adapters.

The driver is deliberately the *only* place adapters differ. Repositories, the key grammar,
the item codec, cross-scope revalidation, idempotency, pointers, and the send fence are
implemented once above this port so business expectations cannot fork per adapter.

The port models the exact DynamoDB operations approved for V1: ``GetItem``, ``BatchGetItem``,
``Query``, single conditional writes, and ``TransactWriteItems``. There is deliberately no
scan operation, no attribute-level update, and no expression string: conditions are closed
typed values that each driver renders or evaluates itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

type StoredValue = str | int | bool | tuple[StoredValue, ...] | Mapping[str, StoredValue] | None
"""Restricted persisted value domain: no floats, bytes, sets, or open metadata bags."""

type StoredItem = Mapping[str, StoredValue]


class TableName(StrEnum):
    """Trust-aligned physical tables; there are exactly three in V1."""

    CORE = "CORE"
    SHAREABLE = "SHAREABLE"
    AUDIT = "AUDIT"


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemKey:
    """Fully qualified item address."""

    table: TableName
    partition_key: str
    sort_key: str


@dataclass(frozen=True, slots=True)
class KeyAbsent:
    """``attribute_not_exists(PK) AND attribute_not_exists(SK)``."""


@dataclass(frozen=True, slots=True)
class KeyPresent:
    """``attribute_exists(PK) AND attribute_exists(SK)``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AttributeEqualsString:
    name: str
    value: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AttributeEqualsNumber:
    name: str
    value: int


@dataclass(frozen=True, slots=True, kw_only=True)
class AttributeAtMostNumber:
    """True when the numeric attribute exists and is less than or equal to ``value``."""

    name: str
    value: int


@dataclass(frozen=True, slots=True)
class AllOf:
    conditions: tuple[ItemCondition, ...]

    def __post_init__(self) -> None:
        if not self.conditions:
            raise ValueError("AllOf requires at least one condition")


@dataclass(frozen=True, slots=True)
class AnyOf:
    conditions: tuple[ItemCondition, ...]

    def __post_init__(self) -> None:
        if not self.conditions:
            raise ValueError("AnyOf requires at least one condition")


type ItemCondition = (
    KeyAbsent
    | KeyPresent
    | AttributeEqualsString
    | AttributeEqualsNumber
    | AttributeAtMostNumber
    | AllOf
    | AnyOf
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PutItem:
    """Whole-item conditional write; partial attribute updates are intentionally absent."""

    key: ItemKey
    item: StoredItem
    condition: ItemCondition


@dataclass(frozen=True, slots=True, kw_only=True)
class DeleteItem:
    key: ItemKey
    condition: ItemCondition


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckItem:
    """Condition-only participant used to fence a mutation on unrelated state."""

    key: ItemKey
    condition: ItemCondition


type WriteOperation = PutItem | DeleteItem | CheckItem


@dataclass(frozen=True, slots=True)
class SortKeyAll:
    """Every item in the partition."""


@dataclass(frozen=True, slots=True)
class SortKeyBeginsWith:
    prefix: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SortKeyBetween:
    low: str
    high: str

    def __post_init__(self) -> None:
        if self.low > self.high:
            raise ValueError("sort key range is inverted")


type SortKeyCondition = SortKeyAll | SortKeyBeginsWith | SortKeyBetween


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryRequest:
    """A bounded single-partition query; there is no scan equivalent."""

    table: TableName
    partition_key: str
    sort_key: SortKeyCondition = SortKeyAll()
    consistent: bool = True
    limit: int = 100
    exclusive_start_sort_key: str | None = None
    ascending: bool = True

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("query limit must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryResult:
    items: tuple[StoredItem, ...]
    last_evaluated_sort_key: str | None


class StorageDriver(Protocol):
    """The complete persisted data-plane surface available to repositories."""

    async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
        """Return one item or ``None``; ``consistent`` selects a strongly consistent read."""

    async def batch_get_items(
        self, keys: tuple[ItemKey, ...], *, consistent: bool
    ) -> tuple[StoredItem, ...]:
        """Return every found item for the requested keys, in unspecified order."""

    async def query(self, request: QueryRequest) -> QueryResult:
        """Return one bounded page from a single partition."""

    async def write_item(self, operation: PutItem | DeleteItem) -> None:
        """Apply one conditional single-item write."""

    async def transact_write(
        self, operations: tuple[WriteOperation, ...], *, client_request_token: str
    ) -> None:
        """Apply all operations atomically or none of them."""
