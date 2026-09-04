"""What a mandate decision does when storage stops answering mid-transaction.

An ambiguous write is the one failure that cannot be handled by retrying, because a retry that
duplicates an authorization decision would mint a second immutable version of one answer -- and
the pointer would move twice for a contributor who clicked once.

So the decision transaction carries a commit proof: the idempotency record it writes itself.
After an ambiguous outcome the unit of work reads that record strongly and only then decides
whether the transaction committed, definitely did not, or remains unproven. These tests drive
all three through the real use case, with the fault injected below the storage port where a
real one would occur.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver
from tests.fixtures.faults import (
    FaultInjectingDriver,
    ReadBehaviour,
    TransactBehaviour,
)
from tests.fixtures.mandates import (
    approve_body,
    build_mandate_world,
    json_of,
)

from chorus.domain.entities import MandateStatus
from chorus.domain.ids import MandateId
from chorus.ports.errors import NotFoundError
from chorus.ports.storage import StorageDriver, WriteOperation

pytestmark = pytest.mark.anyio

MANDATE_VERSION_PREFIX = "MANDATE#"


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    yield from storage_driver(str(request.param), prefix="mandate-unknown")


def decision_transaction(operations: tuple[WriteOperation, ...]) -> bool:
    """True only for a transaction that appends a mandate version.

    Selected by shape rather than by ordinal. The world is built by running real ingestion and
    a real Monitor apply first, so counting transactions would aim the fault at whichever write
    happened to be seventh -- and would move the moment the apply plan grew a step.
    """

    return any(
        operation.key.sort_key.startswith(MANDATE_VERSION_PREFIX)
        and "VERSION#" in operation.key.sort_key
        for operation in operations
    )


def arm(
    faults: FaultInjectingDriver,
    behaviour: TransactBehaviour,
    *,
    proof_read: ReadBehaviour | None = None,
) -> None:
    """Point the fault at the *next* mandate-version transaction, and only that one.

    Counters are zeroed first, and that is not tidiness. Building the world accepts the
    candidate, and acceptance appends version 1 of every proposal -- a mandate-version
    transaction the predicate matches. Without the reset the script index is already past its
    only entry by the time the decision arrives, so the fault never fires and the test passes
    while proving nothing.
    """

    faults.scripted_calls = 0
    faults.read_calls = 0
    faults.script = [behaviour]
    if proof_read is None:
        faults.read_scripted = None
        faults.read_script = []
        return
    # Reads *after* the ambiguous write, which is what makes this the commit-proof read rather
    # than one of the several strong loads a decision performs before it writes anything.
    faults.read_scripted = lambda _key: faults.scripted_calls > 0
    faults.read_script = [proof_read]


async def test_an_ambiguous_outcome_that_did_commit_is_not_written_twice(
    storage: StorageDriver,
) -> None:
    """The response was lost; the transaction landed. The proof says so, and nothing repeats."""

    faults = FaultInjectingDriver(inner=storage, scripted=decision_transaction)
    world = await build_mandate_world(faults)
    assert (await world.accept_candidate()).status_code == 200
    thread = json_of(world.thread("resident-a"))
    arm(faults, TransactBehaviour.AMBIGUOUS_AFTER_APPLY)

    response = world.decide(
        "resident-a", thread["mandate_id"], approve_body(thread), key="unknown-committed-0001"
    )

    assert response.status_code == 200, response.text
    assert faults.scripted_calls == 1, "the fault never fired; the test would prove nothing"
    core = world.api.harness.core
    mandate_id = MandateId(UUID(thread["mandate_id"]))
    pointer = await core.load_current_mandate_pointer(world.case_scope, mandate_id)
    assert pointer.pointer.version == 2
    # Exactly one decision, not two: the resolution returned rather than retrying.
    with pytest.raises(NotFoundError):
        await core.load_mandate_version(world.case_scope, mandate_id, 3)


async def test_an_ambiguous_outcome_that_did_not_commit_is_retried_exactly_once(
    storage: StorageDriver,
) -> None:
    """Non-commit was positively established, so one retry is safe and only one happens."""

    faults = FaultInjectingDriver(inner=storage, scripted=decision_transaction)
    world = await build_mandate_world(faults)
    assert (await world.accept_candidate()).status_code == 200
    thread = json_of(world.thread("resident-a"))
    arm(faults, TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY)

    response = world.decide(
        "resident-a", thread["mandate_id"], approve_body(thread), key="unknown-absent-0001"
    )

    assert response.status_code == 200, response.text
    assert faults.scripted_calls == 2, "expected one ambiguous attempt and exactly one retry"
    core = world.api.harness.core
    mandate_id = MandateId(UUID(thread["mandate_id"]))
    pointer = await core.load_current_mandate_pointer(world.case_scope, mandate_id)
    assert pointer.pointer.version == 2
    assert pointer.status is MandateStatus.APPROVED
    with pytest.raises(NotFoundError):
        await core.load_mandate_version(world.case_scope, mandate_id, 3)


async def test_an_unprovable_outcome_never_retries_and_never_decides(
    storage: StorageDriver,
) -> None:
    """The write was ambiguous and the proof could not be read. Failing is the safe answer.

    A retryable dependency error during resolution is deliberately not allowed to escape: it
    would tell the caller it is safe to run the command again while the original transaction
    may already have recorded their decision.
    """

    faults = FaultInjectingDriver(inner=storage, scripted=decision_transaction)
    world = await build_mandate_world(faults)
    assert (await world.accept_candidate()).status_code == 200
    thread = json_of(world.thread("resident-a"))
    core = world.api.harness.core
    case_before = await core.load_case(world.case_scope)
    arm(faults, TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY, proof_read=ReadBehaviour.UNAVAILABLE)

    response = world.decide(
        "resident-a", thread["mandate_id"], approve_body(thread), key="unknown-unproven-0001"
    )

    assert response.status_code == 503
    assert faults.scripted_calls == 1, "the outcome was resolved, so no retry may have happened"
    body = json_of(response)
    assert body["code"] == "UNKNOWN_TRANSACTION_OUTCOME"
    assert body["retryable"] is False
    # The caller is told to poll, not to retry, and nothing moved.
    assert (await core.load_case(world.case_scope)).version == case_before.version
    pointer = await core.load_current_mandate_pointer(
        world.case_scope, MandateId(UUID(thread["mandate_id"]))
    )
    assert pointer.pointer.version == 1
