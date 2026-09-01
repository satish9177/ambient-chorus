"""Fault-injecting storage driver used to exercise ambiguous transaction outcomes.

The decorator wraps a real driver so the *observable* state after an injected fault is the
state a real ambiguous failure could leave behind: the transaction may have been applied
before the response was lost, or it may never have been applied at all. Nothing about the
outcome is communicated to the caller except the ambiguous error itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from chorus.ports.errors import (
    ExternalDependencyError,
    PersistenceConflictError,
    PersistenceErrorCode,
    UnknownTransactionOutcomeError,
)
from chorus.ports.storage import (
    DeleteItem,
    ItemKey,
    PutItem,
    QueryRequest,
    QueryResult,
    StorageDriver,
    StoredItem,
    WriteOperation,
)


class TransactBehaviour(StrEnum):
    """What one ``transact_write`` call does before returning to the caller."""

    SUCCEED = "SUCCEED"
    AMBIGUOUS_AFTER_APPLY = "AMBIGUOUS_AFTER_APPLY"
    AMBIGUOUS_WITHOUT_APPLY = "AMBIGUOUS_WITHOUT_APPLY"
    DEFINITE_FAILURE = "DEFINITE_FAILURE"


class ReadBehaviour(StrEnum):
    """What one ``get_item`` call does.

    The two failure modes are exactly what the DynamoDB error mapping produces for a read it
    could not complete: a throttle or read timeout, and an unavailable dependency. Both are
    ``retryable`` dependency errors, which is precisely why they must not be allowed to
    escape a resolution attempt for a write whose outcome is already ambiguous.
    """

    SUCCEED = "SUCCEED"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(slots=True)
class FaultInjectingDriver:
    """Wrap a driver and script the outcome of successive transactional writes."""

    inner: StorageDriver
    script: list[TransactBehaviour] = field(default_factory=list)
    read_script: list[ReadBehaviour] = field(default_factory=list)
    transact_calls: int = field(default=0, init=False)
    read_calls: int = field(default=0, init=False)
    transact_tokens: list[str] = field(default_factory=list, init=False)

    async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
        index = self.read_calls
        self.read_calls += 1
        behaviour = (
            self.read_script[index] if index < len(self.read_script) else ReadBehaviour.SUCCEED
        )
        match behaviour:
            case ReadBehaviour.SUCCEED:
                return await self.inner.get_item(key, consistent=consistent)
            case ReadBehaviour.TIMEOUT:
                raise ExternalDependencyError(
                    "READ", code=PersistenceErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True
                )
            case ReadBehaviour.UNAVAILABLE:
                raise ExternalDependencyError(
                    "READ", code=PersistenceErrorCode.DEPENDENCY_REJECTED, retryable=True
                )
            case _:  # pragma: no cover - closed enum
                raise AssertionError("unreachable read behaviour")

    async def batch_get_items(
        self, keys: tuple[ItemKey, ...], *, consistent: bool
    ) -> tuple[StoredItem, ...]:
        return await self.inner.batch_get_items(keys, consistent=consistent)

    async def query(self, request: QueryRequest) -> QueryResult:
        return await self.inner.query(request)

    async def write_item(self, operation: PutItem | DeleteItem) -> None:
        await self.inner.write_item(operation)

    async def transact_write(
        self, operations: tuple[WriteOperation, ...], *, client_request_token: str
    ) -> None:
        index = self.transact_calls
        self.transact_calls += 1
        self.transact_tokens.append(client_request_token)
        behaviour = self.script[index] if index < len(self.script) else TransactBehaviour.SUCCEED
        match behaviour:
            case TransactBehaviour.SUCCEED:
                await self.inner.transact_write(
                    operations, client_request_token=client_request_token
                )
            case TransactBehaviour.AMBIGUOUS_AFTER_APPLY:
                await self.inner.transact_write(
                    operations, client_request_token=client_request_token
                )
                raise UnknownTransactionOutcomeError("TRANSACTION")
            case TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY:
                raise UnknownTransactionOutcomeError("TRANSACTION")
            case TransactBehaviour.DEFINITE_FAILURE:
                # A definite rejection leaves no ambiguity, so nothing about it may licence a
                # retry. It is scripted here so that "only ambiguity is resolved" is a test
                # rather than a reading of the control flow.
                raise PersistenceConflictError("TRANSACTION")
            case _:  # pragma: no cover - closed enum
                raise AssertionError("unreachable transaction behaviour")
