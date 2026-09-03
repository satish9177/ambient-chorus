"""Transaction execution with proof-based resolution of ambiguous outcomes.

An ambiguous transport failure is never retried on faith. The unit of work reads the plan's
commit proof with a strongly consistent get and only then decides:

* proof present with the same request hash -- the transaction committed, so return;
* proof absent -- the transaction definitely did not commit, so exactly one retry is safe;
* no proof available, or the proof could not be read -- surface
  ``UnknownTransactionOutcomeError`` and let an operator or a later reconciliation decide.
  Duplicating a mutation is worse than failing the command.

Once a write outcome is ambiguous, nothing may restore generic retryability. A dependency
failure *during resolution* is therefore quarantined as an unknown outcome rather than
allowed to escape as a retryable ``ExternalDependencyError``: the command would otherwise be
told it is safe to run again while the original transaction may already have committed.

Two failures deliberately keep their own identity, because each is a definite answer rather
than an unresolved outcome: a proof bound to a different request is
``IdempotencyConflictError``, and a proof whose stored bytes violate the schema is
``IntegrityError``. Neither is ever laundered into ``UNKNOWN``.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.infrastructure.dynamodb import codec_idempotency
from chorus.ports.errors import (
    ExternalDependencyError,
    IdempotencyConflictError,
    UnknownTransactionOutcomeError,
)
from chorus.ports.storage import StorageDriver, StoredItem
from chorus.ports.unit_of_work import (
    CommitProof,
    TransactionCommitted,
    TransactionNotCommitted,
    TransactionOutcome,
    TransactionOutcomeUnproven,
    TransactionPlan,
)


@dataclass(slots=True)
class StorageUnitOfWork:
    """Commits explicitly composed transaction plans against one storage driver."""

    driver: StorageDriver

    async def _write(self, plan: TransactionPlan) -> None:
        await self.driver.transact_write(
            plan.operations, client_request_token=plan.client_request_token
        )

    async def _read_proof(self, proof: CommitProof) -> StoredItem | None:
        """Read the proof strongly, or declare the outcome unknown.

        A timeout, throttle, or unavailable dependency here means the outcome could not be
        established. That is exactly the state ``UnknownTransactionOutcomeError`` exists to
        describe, so it is raised in place of the retryable dependency error the driver
        produced. The cause is chained, and it carries no request or response content.
        """

        try:
            return await self.driver.get_item(proof.key, consistent=True)
        except UnknownTransactionOutcomeError:
            raise
        except ExternalDependencyError as error:
            raise UnknownTransactionOutcomeError("COMMIT_PROOF") from error

    async def resolve_outcome(self, plan: TransactionPlan) -> TransactionOutcome:
        """Classify an ambiguous outcome by reading the plan's own commit proof."""

        proof = plan.commit_proof
        if proof is None:
            return TransactionOutcomeUnproven()
        item = await self._read_proof(proof)
        if item is None:
            return TransactionNotCommitted()
        _, record = codec_idempotency.decode_idempotency(item)
        if record.request_hash != proof.request_hash:
            raise IdempotencyConflictError("COMMIT_PROOF")
        if proof.completed_version is not None and record.version < proof.completed_version:
            # The record is still the reservation this plan meant to complete, so the guarded
            # write definitely did not land. Presence alone would have said the opposite.
            return TransactionNotCommitted()
        return TransactionCommitted()

    async def commit(self, plan: TransactionPlan) -> None:
        """Apply the plan atomically, resolving an ambiguous outcome before any retry."""

        try:
            await self._write(plan)
        except UnknownTransactionOutcomeError:
            outcome = await self.resolve_outcome(plan)
            if isinstance(outcome, TransactionCommitted):
                return
            if isinstance(outcome, TransactionOutcomeUnproven):
                raise
            await self._retry_once(plan)

    async def _retry_once(self, plan: TransactionPlan) -> None:
        """Retry exactly once, and only after non-commit has been positively established."""

        try:
            await self._write(plan)
        except UnknownTransactionOutcomeError:
            outcome = await self.resolve_outcome(plan)
            if isinstance(outcome, TransactionCommitted):
                return
            raise
