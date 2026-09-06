"""A storage driver that commits for real and then loses the acknowledgement.

The whole point of F04 is a transaction that **did** commit. A mock that raises before the
write reproduces nothing: there is no durable state to recover, so every recovery path is
trivially correct and a suite full of such mocks stays green through the defect.

So this driver delegates ``transact_write`` to the real driver, lets it commit, and only then
raises :class:`UnknownTransactionOutcomeError` -- exactly the shape a lost acknowledgement has.
``get_item`` can be made unavailable for a bounded number of calls, which is how the commit
proof read and the first recovery read are made to fail while the durable rows sit there.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from chorus.ports.errors import ExternalDependencyError, UnknownTransactionOutcomeError
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


@dataclass(slots=True)
class AmbiguousCommitDriver:
    """Wrap a real driver so one chosen transaction commits and then reports an unknown outcome."""

    inner: StorageDriver
    lose_ack_when: Callable[[tuple[WriteOperation, ...]], bool] | None = None
    """Selects which transaction loses its acknowledgement. ``None`` loses none."""

    unavailable_get_items: int = 0
    """How many of the next ``get_item`` calls are answered with an unavailable dependency.

    Counted down as they fire. It is a count rather than a flag because the scenario needs the
    *commit proof* read and the *first recovery* read to fail while later reads succeed, which
    is what makes "retry the proof, never the model" observable.
    """

    unavailable_after_lost_ack: int = 0
    """Reads to make unavailable **starting at the lost acknowledgement**, not before it.

    Arming from the moment the transaction commits is what makes the fault land on the reads
    the scenario is about -- the commit proof, then the first recovery read -- rather than on
    whichever unrelated load the worker happened to perform first.
    """

    unavailable_proof_reads: int = 0
    """Reads to fail, counting **only** the proof reads named by :func:`is_proof_read`.

    Targeted rather than blanket, so a scenario can make the commit proof and the durable
    invocation record unreadable while every other load -- the operation row, the case, the
    view -- still works. A blanket outage would fail the worker before it reached the code
    under test, and would prove nothing about how an unresolved outcome is handled.
    """

    committed_plans: list[tuple[WriteOperation, ...]] = field(default_factory=list)
    lost_acks: int = 0

    async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
        if self.unavailable_get_items > 0:
            self.unavailable_get_items -= 1
            raise ExternalDependencyError("STORAGE")
        if self.unavailable_proof_reads > 0 and is_proof_read(key):
            self.unavailable_proof_reads -= 1
            raise ExternalDependencyError("STORAGE")
        return await self.inner.get_item(key, consistent=consistent)

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
        await self.inner.transact_write(operations, client_request_token=client_request_token)
        self.committed_plans.append(operations)
        if self.lose_ack_when is not None and self.lose_ack_when(operations):
            self.lost_acks += 1
            self.unavailable_get_items = self.unavailable_after_lost_ack
            # Committed. Acknowledged to nobody.
            raise UnknownTransactionOutcomeError("TRANSACT_WRITE")


def is_proof_read(key: ItemKey) -> bool:
    """Whether this read is one of the two durable proofs an ambiguous apply is resolved by.

    The apply commit proof lives in the idempotency table; the durable ``ACTION`` invocation
    record lives in the case partition of the core table under an ``AGENT_INVOCATION`` sort key.
    Those two, and nothing else, are what "the proof read is unavailable" means.
    """

    return key.table.value == "IDEMPOTENCY" or key.sort_key.startswith("AGENT_INVOCATION")


def is_proposal_apply(operations: tuple[WriteOperation, ...]) -> bool:
    """Recognise the ten-participant proposal apply from its staged items alone.

    Matched on content rather than on a plan name so the predicate sees what storage sees --
    a driver is handed operations, not a plan, which is the level at which a lost
    acknowledgement actually happens.
    """

    if len(operations) != 10:
        return False
    return any(
        isinstance(operation, PutItem) and operation.item.get("entity_type") == "ACTION_PROPOSAL"
        for operation in operations
    )
