"""Fault-injecting storage driver used to exercise ambiguous transaction outcomes.

The decorator wraps a real driver so the *observable* state after an injected fault is the
state a real ambiguous failure could leave behind: the transaction may have been applied
before the response was lost, or it may never have been applied at all. Nothing about the
outcome is communicated to the caller except the ambiguous error itself.
"""

from __future__ import annotations

from collections.abc import Callable
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


CASE_SORT_KEY = "CASE"
INVOCATION_SORT_KEY_PREFIX = "AGENT_INVOCATION#"
OPERATION_SORT_KEY = "OPERATION"


def monitor_finalization(operations: tuple[WriteOperation, ...]) -> bool:
    """True for the transaction that finalizes a Monitor apply, and only that one.

    The finalization step writes the operation-scoped successful invocation record and touches
    no case row. Both halves of that are needed to name it: a case's *first* apply step also
    appends an ``AGENT_INVOCATION`` item -- the case-scoped audit copy -- so matching on the
    sort key alone would aim the fault at step one instead of the tail.

    Selecting by shape rather than by ordinal keeps the intent stable if the plan ahead of the
    tail grows or shrinks a step.
    """

    return any(
        operation.key.sort_key.startswith(INVOCATION_SORT_KEY_PREFIX) for operation in operations
    ) and not any(operation.key.sort_key == CASE_SORT_KEY for operation in operations)


def operation_creation(operations: tuple[WriteOperation, ...]) -> bool:
    """True for the transaction that creates an application operation.

    That transaction is also the one that completes the route's idempotency reservation, and
    the two commit together by design, so failing it is how a test says "the request died
    between reserving the key and owning an operation".
    """

    return any(operation.key.sort_key == OPERATION_SORT_KEY for operation in operations)


def operation_transitions(operation: PutItem | DeleteItem) -> bool:
    """True for a single conditional write of an application-operation row.

    Every operation status transition is a bare conditional write rather than a transaction --
    that is what makes the claim exclusive -- so scripting its failure needs its own hook.
    """

    return operation.key.sort_key == OPERATION_SORT_KEY


def monitor_apply_steps(operations: tuple[WriteOperation, ...]) -> bool:
    """True for a transaction that commits one Monitor apply step.

    A Monitor run now writes more than its apply steps: it snapshots the frozen input before
    the model is called and the validated plan before step one, each in its own bounded
    transaction. Scripting faults by raw transaction ordinal would therefore aim at whichever
    write happened to be third that week, and a test meaning "the second apply step fails"
    would quietly become "the plan snapshot fails" the next time the lifecycle grew a stage.

    Selecting by shape instead keeps the intent stable: every apply step writes its case row,
    and nothing else in the Monitor path does.
    """

    return any(operation.key.sort_key == CASE_SORT_KEY for operation in operations)


class WriteBehaviour(StrEnum):
    """What one single-item ``write_item`` call does before returning to the caller."""

    SUCCEED = "SUCCEED"
    AMBIGUOUS_AFTER_APPLY = "AMBIGUOUS_AFTER_APPLY"
    AMBIGUOUS_WITHOUT_APPLY = "AMBIGUOUS_WITHOUT_APPLY"
    DEFINITE_FAILURE = "DEFINITE_FAILURE"


@dataclass(slots=True)
class FaultInjectingDriver:
    """Wrap a driver and script the outcome of successive transactional writes.

    ``scripted`` narrows which transactions the script applies to. Transactions it rejects are
    passed straight through and do not consume a script entry, so a test can say "the second
    apply step fails" without counting every unrelated write that precedes it.
    """

    inner: StorageDriver
    script: list[TransactBehaviour] = field(default_factory=list)
    read_script: list[ReadBehaviour] = field(default_factory=list)
    write_script: list[WriteBehaviour] = field(default_factory=list)
    scripted: Callable[[tuple[WriteOperation, ...]], bool] | None = None
    write_scripted: Callable[[PutItem | DeleteItem], bool] | None = None
    transact_calls: int = field(default=0, init=False)
    scripted_calls: int = field(default=0, init=False)
    read_calls: int = field(default=0, init=False)
    write_calls: int = field(default=0, init=False)
    scripted_writes: int = field(default=0, init=False)
    transact_tokens: list[str] = field(default_factory=list, init=False)
    transact_sizes: list[int] = field(default_factory=list, init=False)
    """How many operations each ``transact_write`` call carried, in call order.

    Exists so a test can assert a bound on transaction *size* -- not merely on how many
    transactions happened -- without needing its own driver wrapper. Recorded for every call
    regardless of scripting, because the size of what was attempted is the fact under test,
    not the outcome the script gave it.
    """

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
        """Apply the single-item write, or the scripted failure standing in for it.

        Scripted the same way transactions are, and for the same reason: the operation status
        transition is a bare conditional write, so "the SUCCEEDED transition was refused" and
        "the SUCCEEDED transition's response was lost" have no other way to be reproduced.
        """

        self.write_calls += 1
        if self.write_scripted is not None and not self.write_scripted(operation):
            await self.inner.write_item(operation)
            return
        index = self.scripted_writes
        self.scripted_writes += 1
        behaviour = (
            self.write_script[index] if index < len(self.write_script) else WriteBehaviour.SUCCEED
        )
        match behaviour:
            case WriteBehaviour.SUCCEED:
                await self.inner.write_item(operation)
            case WriteBehaviour.AMBIGUOUS_AFTER_APPLY:
                await self.inner.write_item(operation)
                raise UnknownTransactionOutcomeError("WRITE")
            case WriteBehaviour.AMBIGUOUS_WITHOUT_APPLY:
                raise UnknownTransactionOutcomeError("WRITE")
            case WriteBehaviour.DEFINITE_FAILURE:
                raise PersistenceConflictError("WRITE")
            case _:  # pragma: no cover - closed enum
                raise AssertionError("unreachable write behaviour")

    async def transact_write(
        self, operations: tuple[WriteOperation, ...], *, client_request_token: str
    ) -> None:
        self.transact_calls += 1
        self.transact_tokens.append(client_request_token)
        self.transact_sizes.append(len(operations))
        if self.scripted is not None and not self.scripted(operations):
            await self.inner.transact_write(operations, client_request_token=client_request_token)
            return
        index = self.scripted_calls
        self.scripted_calls += 1
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
