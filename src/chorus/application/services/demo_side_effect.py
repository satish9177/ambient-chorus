"""Admit a DEMO side effect through the existing durable command-reservation contract.

Each actual attempt has its own reservation in the command's already tracked partition.
Reset refuses an in-progress reservation after taking its lock. Completion means the external
call returned a definite outcome; exceptions/unknown outcomes intentionally retain the intent.
This record grants no content authority and does not finalize or publish an evidence object.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from hashlib import sha256

from chorus.domain.ids import IdGenerator, Sha256Digest
from chorus.ports.clock import Clock
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.idempotency import IdempotencyKey, IdempotencyStarted
from chorus.ports.repositories import IdempotencyRepositoryPort
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork

SIDE_EFFECT_ACTOR = Sha256Digest("sha256:" + sha256(b"DEMO_SIDE_EFFECT_ATTEMPT/v1").hexdigest())


@dataclass(slots=True)
class SideEffectAttempt:
    quiescent: bool = True


_active_attempt: ContextVar[SideEffectAttempt | None] = ContextVar(
    "demo_side_effect_attempt", default=None
)


def retain_demo_side_effect_intent() -> None:
    """An observed object does not prove an earlier ambiguous request cannot finish later."""
    attempt = _active_attempt.get()
    if attempt is not None:
        attempt.quiescent = False


@asynccontextmanager
async def demo_side_effect(
    *,
    key: IdempotencyKey,
    repository: IdempotencyRepositoryPort,
    unit_of_work: UnitOfWork,
    clock: Clock,
    ids: IdGenerator,
) -> AsyncIterator[SideEffectAttempt]:
    attempt = SideEffectAttempt()
    if key.partition.namespace.value != "DEMO":
        yield attempt
        return
    attempt_key = replace(
        key,
        actor_id_hash=SIDE_EFFECT_ACTOR,
        key_hash=Sha256Digest("sha256:" + sha256(str(ids.new_uuid()).encode()).hexdigest()),
    )
    claimed = await repository.begin(attempt_key, request_hash=key.key_hash, now=clock.now())
    if not isinstance(claimed, IdempotencyStarted):
        raise ValueError("a side-effect attempt identity must be unique")
    token = _active_attempt.set(attempt)
    try:
        yield attempt
    finally:
        _active_attempt.reset(token)
    if attempt.quiescent:
        plan = TransactionPlan(
            name="demo-side-effect-complete",
            audit_required=False,
            operations=(
                repository.stage_complete(
                    claimed.record,
                    result_entity_refs=(),
                    response_status=204,
                    now=clock.now(),
                ),
            ),
            commit_proof=repository.completion_proof(claimed.record),
        )
        # Reset will observe this IN_PROGRESS attempt and refuse before purge. If its lock
        # raced our completion, wait for release and retry only this same completion CAS.
        # The external call is never retried here, and the completion token stays stable.
        import asyncio

        for retry in range(2_300):
            try:
                await unit_of_work.commit(plan)
                break
            except PersistenceConflictError:
                if retry == 2_299:
                    raise
                await asyncio.sleep(0.05)
