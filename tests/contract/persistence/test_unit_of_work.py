"""Transaction contract: atomicity, atomic audit, and resolution of unknown outcomes.

The unknown-outcome cases are labelled A-E so a reviewer can check each one against the
frozen decision table:

A. the transaction did commit, and no duplicate is written;
B. the transaction definitely did not commit, and exactly one retry runs;
C. the retry is itself ambiguous but did commit, so the command still succeeds once;
D. the retry is ambiguous and did not commit, so the caller is told the outcome is unknown;
E. no commit proof exists, so nothing is ever retried;
F. the outcome could not be read at all, so it stays unknown and never becomes retryable.

Row F is the one that matters most for callers. Once a write is ambiguous, a failure to
*prove* what happened must not hand back a retryable dependency error: a caller obeying
``retryable`` would run a command that may already have committed.
"""

from __future__ import annotations

import pytest
from tests.fixtures.faults import FaultInjectingDriver, ReadBehaviour, TransactBehaviour
from tests.fixtures.persistence import NOW, PRIMARY, Repositories, build_repositories, digest

from chorus.domain.errors import IntegrityError
from chorus.domain.ids import FactId, Sha256Digest
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.ports.errors import (
    ExternalDependencyError,
    IdempotencyConflictError,
    PersistenceConflictError,
    PersistenceErrorCode,
    UnknownTransactionOutcomeError,
)
from chorus.ports.idempotency import IdempotencyStatus
from chorus.ports.limits import TRANSACTION_MAX_OPERATIONS
from chorus.ports.pagination import Page, PageRequest
from chorus.ports.storage import KeyAbsent, PutItem, StorageDriver, TableName
from chorus.ports.unit_of_work import (
    CommitProof,
    TransactionCommitted,
    TransactionNotCommitted,
    TransactionOutcomeUnproven,
    TransactionPlan,
)

pytestmark = pytest.mark.anyio

REQUEST_HASH = digest("apply-investigation-request")


def proof_operation(
    repositories: Repositories, request_hash: Sha256Digest = REQUEST_HASH
) -> tuple[PutItem, CommitProof]:
    key = PRIMARY.idempotency_key()
    operation = repositories.idempotency.stage_create_completed(
        key,
        request_hash=request_hash,
        result_entity_refs=(),
        response_status=200,
        now=NOW,
    )
    return operation, repositories.idempotency.commit_proof(key, request_hash=request_hash)


def case_plan(
    repositories: Repositories,
    *,
    with_proof: bool = True,
    request_hash: Sha256Digest = REQUEST_HASH,
) -> TransactionPlan:
    """A realistic audited case mutation, optionally carrying its commit proof."""

    operation, proof = proof_operation(repositories, request_hash)
    operations = (
        repositories.core.stage_create_case(PRIMARY.case_scope, PRIMARY.case()),
        repositories.core.stage_create_fact(PRIMARY.case_scope, PRIMARY.fact()),
        repositories.audit.stage_append_case_event(PRIMARY.case_scope, PRIMARY.audit_event()),
        operation,
    )
    return TransactionPlan(
        name="apply-investigation",
        operations=operations,
        audit_required=True,
        commit_proof=proof if with_proof else None,
    )


async def test_a_plan_commits_every_operation_atomically(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)

    await repositories.unit_of_work.commit(case_plan(repositories))

    assert await repositories.core.load_case(PRIMARY.case_scope) == PRIMARY.case()
    assert (await repositories.core.load_facts(PRIMARY.case_scope, (PRIMARY.fact_id,)))[0]
    page = await repositories.audit.read_case_events(PRIMARY.case_scope, PageRequest(limit=10))
    assert len(page.items) == 1


async def test_a_failed_condition_rolls_back_every_operation(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    await storage.write_item(
        repositories.core.stage_create_case(PRIMARY.case_scope, PRIMARY.case())
    )

    with pytest.raises(PersistenceConflictError):
        await repositories.unit_of_work.commit(case_plan(repositories))

    page = await repositories.audit.read_case_events(PRIMARY.case_scope, PageRequest(limit=10))
    assert page.items == ()
    assert await repositories.idempotency.load(PRIMARY.idempotency_key()) is None


async def test_an_audit_row_is_written_in_the_same_transaction(
    storage: StorageDriver,
) -> None:
    """There is no window in which a mutation exists without its audit row."""

    repositories = build_repositories(storage)
    plan = case_plan(repositories)
    audit_writes = [
        operation for operation in plan.operations if operation.key.table is TableName.AUDIT
    ]

    assert len(audit_writes) == 1
    await repositories.unit_of_work.commit(plan)
    page = await repositories.audit.read_case_events(PRIMARY.case_scope, PageRequest(limit=10))
    assert len(page.items) == 1


async def test_a_transaction_at_the_operation_limit_commits(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    facts = tuple(
        repositories.core.stage_create_fact(
            PRIMARY.case_scope,
            PRIMARY.fact(fact_id=FactId(PRIMARY.uuid(f"bulk-fact:{index}"))),
        )
        for index in range(TRANSACTION_MAX_OPERATIONS - 1)
    )
    plan = TransactionPlan(
        name="bulk-facts",
        operations=(
            *facts,
            repositories.audit.stage_append_case_event(PRIMARY.case_scope, PRIMARY.audit_event()),
        ),
        audit_required=True,
    )

    await repositories.unit_of_work.commit(plan)

    page = await repositories.core.read_case_facts(PRIMARY.case_scope, PageRequest(limit=100))
    assert len(page.items) == TRANSACTION_MAX_OPERATIONS - 1


async def test_case_a_a_committed_transaction_is_not_written_twice(
    storage: StorageDriver,
) -> None:
    faulty = FaultInjectingDriver(inner=storage, script=[TransactBehaviour.AMBIGUOUS_AFTER_APPLY])
    repositories = build_repositories(faulty)
    plan = case_plan(repositories)

    await StorageUnitOfWork(driver=faulty).commit(plan)

    assert faulty.transact_calls == 1
    page = await repositories.audit.read_case_events(PRIMARY.case_scope, PageRequest(limit=10))
    assert len(page.items) == 1


async def test_case_b_a_definitely_uncommitted_transaction_is_retried_once(
    storage: StorageDriver,
) -> None:
    faulty = FaultInjectingDriver(inner=storage, script=[TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY])
    repositories = build_repositories(faulty)
    plan = case_plan(repositories)

    await StorageUnitOfWork(driver=faulty).commit(plan)

    assert faulty.transact_calls == 2
    record = await repositories.idempotency.load(PRIMARY.idempotency_key())
    assert record is not None
    assert record.status is IdempotencyStatus.COMPLETED


async def test_case_c_an_ambiguous_retry_that_committed_succeeds_once(
    storage: StorageDriver,
) -> None:
    faulty = FaultInjectingDriver(
        inner=storage,
        script=[
            TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY,
            TransactBehaviour.AMBIGUOUS_AFTER_APPLY,
        ],
    )
    repositories = build_repositories(faulty)
    plan = case_plan(repositories)

    await StorageUnitOfWork(driver=faulty).commit(plan)

    assert faulty.transact_calls == 2
    page = await repositories.audit.read_case_events(PRIMARY.case_scope, PageRequest(limit=10))
    assert len(page.items) == 1


async def test_case_d_an_ambiguous_retry_that_did_not_commit_stays_unknown(
    storage: StorageDriver,
) -> None:
    faulty = FaultInjectingDriver(
        inner=storage,
        script=[
            TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY,
            TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY,
        ],
    )
    repositories = build_repositories(faulty)
    plan = case_plan(repositories)

    with pytest.raises(UnknownTransactionOutcomeError):
        await StorageUnitOfWork(driver=faulty).commit(plan)

    assert faulty.transact_calls == 2
    assert await repositories.idempotency.load(PRIMARY.idempotency_key()) is None


async def test_case_e_a_plan_without_proof_is_never_retried(storage: StorageDriver) -> None:
    """Duplicating a mutation is worse than failing the command."""

    faulty = FaultInjectingDriver(inner=storage, script=[TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY])
    repositories = build_repositories(faulty)
    plan = case_plan(repositories, with_proof=False)

    with pytest.raises(UnknownTransactionOutcomeError):
        await StorageUnitOfWork(driver=faulty).commit(plan)

    assert faulty.transact_calls == 1


async def test_resolution_reads_the_proof_strongly(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    plan = case_plan(repositories)
    unit_of_work = StorageUnitOfWork(driver=storage)

    assert isinstance(await unit_of_work.resolve_outcome(plan), TransactionNotCommitted)
    await unit_of_work.commit(plan)
    assert isinstance(await unit_of_work.resolve_outcome(plan), TransactionCommitted)


async def test_resolution_without_a_proof_is_unproven(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    plan = case_plan(repositories, with_proof=False)

    outcome = await StorageUnitOfWork(driver=storage).resolve_outcome(plan)

    assert isinstance(outcome, TransactionOutcomeUnproven)


async def test_a_proof_bound_to_another_request_is_a_conflict(
    storage: StorageDriver,
) -> None:
    """A record under the same key but a different request never counts as our commit.

    Both plans are internally consistent -- each persists the hash its own proof names -- so
    this is the real collision the runtime check exists for: two different commands reached
    the same idempotency key, and the stored record belongs to the other one.
    """

    repositories = build_repositories(storage)
    await repositories.unit_of_work.commit(case_plan(repositories))
    other = case_plan(repositories, request_hash=digest("a-different-request"))

    with pytest.raises(IdempotencyConflictError):
        await StorageUnitOfWork(driver=storage).resolve_outcome(other)


async def test_a_definite_failure_is_never_retried(storage: StorageDriver) -> None:
    """Only a *proven* non-commit licences a retry, never the mere fact that a call threw."""

    faulty = FaultInjectingDriver(inner=storage, script=[TransactBehaviour.DEFINITE_FAILURE])
    repositories = build_repositories(faulty)
    plan = case_plan(repositories)

    with pytest.raises(PersistenceConflictError):
        await StorageUnitOfWork(driver=faulty).commit(plan)

    assert faulty.transact_calls == 1
    assert faulty.read_calls == 0


async def test_a_definite_failure_after_an_ambiguous_one_still_stops(
    storage: StorageDriver,
) -> None:
    """The single licenced retry may fail definitively, and that failure ends the command."""

    faulty = FaultInjectingDriver(
        inner=storage,
        script=[TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY, TransactBehaviour.DEFINITE_FAILURE],
    )
    repositories = build_repositories(faulty)
    plan = case_plan(repositories)

    with pytest.raises(PersistenceConflictError):
        await StorageUnitOfWork(driver=faulty).commit(plan)

    assert faulty.transact_calls == 2


async def test_the_licenced_retry_resends_the_identical_token(storage: StorageDriver) -> None:
    """The retry is the same request, so DynamoDB's own window can still deduplicate it."""

    faulty = FaultInjectingDriver(inner=storage, script=[TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY])
    repositories = build_repositories(faulty)
    plan = case_plan(repositories)

    await StorageUnitOfWork(driver=faulty).commit(plan)

    assert faulty.transact_tokens == [plan.client_request_token, plan.client_request_token]


async def test_an_ambiguous_outcome_is_resolved_by_a_strongly_consistent_read() -> None:
    """A stale read of the proof would licence a duplicate mutation, so it must not be used."""

    memory = InMemoryStorageDriver(stale_eventual_reads=True)
    faulty = FaultInjectingDriver(inner=memory, script=[TransactBehaviour.AMBIGUOUS_AFTER_APPLY])
    repositories = build_repositories(faulty)
    plan = case_plan(repositories)

    await StorageUnitOfWork(driver=faulty).commit(plan)

    proof = plan.commit_proof
    assert proof is not None
    # The fixture is only discriminating while the two consistencies actually disagree.
    assert await memory.get_item(proof.key, consistent=False) is None
    assert await memory.get_item(proof.key, consistent=True) is not None
    # So resolution can only have seen the proof by reading strongly, and no retry followed.
    assert faulty.read_calls == 1
    assert faulty.transact_calls == 1


async def test_case_f_a_proof_read_timeout_stays_unknown(storage: StorageDriver) -> None:
    """An ambiguous write plus an unreadable proof is unknown, never retryable."""

    faulty = FaultInjectingDriver(
        inner=storage,
        script=[TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY],
        read_script=[ReadBehaviour.TIMEOUT],
    )
    repositories = build_repositories(faulty)
    plan = case_plan(repositories)

    with pytest.raises(UnknownTransactionOutcomeError) as raised:
        await StorageUnitOfWork(driver=faulty).commit(plan)

    assert raised.value.retryable is False
    assert raised.value.code is PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME
    assert faulty.transact_calls == 1
    assert await repositories.audit.read_case_events(
        PRIMARY.case_scope, PageRequest(limit=10)
    ) == Page(items=(), next_cursor=None)


async def test_case_f_a_proof_read_dependency_failure_stays_unknown(
    storage: StorageDriver,
) -> None:
    """An unavailable dependency during resolution is quarantined the same way."""

    faulty = FaultInjectingDriver(
        inner=storage,
        script=[TransactBehaviour.AMBIGUOUS_AFTER_APPLY],
        read_script=[ReadBehaviour.UNAVAILABLE],
    )
    repositories = build_repositories(faulty)

    with pytest.raises(UnknownTransactionOutcomeError) as raised:
        await StorageUnitOfWork(driver=faulty).commit(case_plan(repositories))

    assert raised.value.retryable is False
    # The transaction actually did commit; the point is that the caller is not told it is
    # safe to run again just because the proof could not be read.
    assert faulty.transact_calls == 1


async def test_case_f_an_unreadable_proof_after_the_retry_stays_unknown(
    storage: StorageDriver,
) -> None:
    """The licensed retry gets the same treatment: no second retry, no retryable error."""

    faulty = FaultInjectingDriver(
        inner=storage,
        script=[
            TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY,
            TransactBehaviour.AMBIGUOUS_AFTER_APPLY,
        ],
        read_script=[ReadBehaviour.SUCCEED, ReadBehaviour.TIMEOUT],
    )
    repositories = build_repositories(faulty)

    with pytest.raises(UnknownTransactionOutcomeError) as raised:
        await StorageUnitOfWork(driver=faulty).commit(case_plan(repositories))

    assert raised.value.retryable is False
    assert faulty.transact_calls == 2


async def test_an_unreadable_proof_chains_its_cause_without_leaking_it(
    storage: StorageDriver,
) -> None:
    """The dependency failure is preserved for operators but never becomes the answer."""

    faulty = FaultInjectingDriver(
        inner=storage,
        script=[TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY],
        read_script=[ReadBehaviour.TIMEOUT],
    )
    repositories = build_repositories(faulty)

    with pytest.raises(UnknownTransactionOutcomeError) as raised:
        await StorageUnitOfWork(driver=faulty).commit(case_plan(repositories))

    assert isinstance(raised.value.__cause__, ExternalDependencyError)
    assert str(raised.value) == "UNKNOWN_TRANSACTION_OUTCOME: COMMIT_PROOF"


async def test_a_proof_conflict_is_not_laundered_into_unknown(storage: StorageDriver) -> None:
    """A definite answer keeps its own identity; only unresolved outcomes become unknown."""

    repositories = build_repositories(storage)
    await repositories.unit_of_work.commit(case_plan(repositories))
    faulty = FaultInjectingDriver(inner=storage, script=[TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY])
    other = case_plan(build_repositories(faulty), request_hash=digest("a-different-request"))

    with pytest.raises(IdempotencyConflictError):
        await StorageUnitOfWork(driver=faulty).commit(other)

    assert faulty.transact_calls == 1


async def test_a_corrupt_proof_is_not_laundered_into_unknown(storage: StorageDriver) -> None:
    """Stored corruption is an integrity failure, not an unresolved transaction outcome."""

    repositories = build_repositories(storage)
    plan = case_plan(repositories)
    proof = plan.commit_proof
    assert proof is not None
    stored = await storage.get_item(proof.key, consistent=True)
    assert stored is None
    await storage.write_item(
        PutItem(
            key=proof.key,
            item={"PK": proof.key.partition_key, "SK": proof.key.sort_key, "junk": "smuggled"},
            condition=KeyAbsent(),
        )
    )
    faulty = FaultInjectingDriver(inner=storage, script=[TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY])

    with pytest.raises(IntegrityError):
        await StorageUnitOfWork(driver=faulty).commit(plan)

    assert faulty.transact_calls == 1
