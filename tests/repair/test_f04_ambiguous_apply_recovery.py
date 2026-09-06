"""F04 -- a committed apply whose acknowledgement was lost must never become terminal FAILED.

Codex's reproduction, exactly:

```text
the ten-participant transaction actually commits
the acknowledgement is lost
the immediate commit-proof read is unavailable
ProposeAction raises UnknownTransactionOutcomeError
the worker settles the ApplicationOperation to FAILED     <- the defect
the durable ACTION invocation record says SUCCEEDED
every redelivery afterwards returns the terminal FAILED, forever
```

The operation is a projection over an authorization commit that *did* happen -- the proposal,
the ``DRAFT`` execution, the pointers, the history locator, the audit row, and the case
transition are all durable. Recording it as failed is the one answer that cannot be revised.

The invariant the repair holds:

> **An unknown apply outcome is not turned into a terminal failure until durable state proves
> the apply did not commit.**

These tests drive a driver that commits for real and then raises -- see ``faults.py``. A mock
that raised *before* the write would leave nothing durable to recover and would have been green
through the whole defect.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from chorus.application.commands.propose_action import PROPOSAL_APPLY_TRANSACTION
from chorus.domain.entities import ApplicationOperationStatus, CaseState
from chorus.ports.errors import UnknownTransactionOutcomeError
from chorus.ports.records import AgentInvocationOutcome
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import StorageDriver
from tests.fixtures.action import ActionHarness
from tests.repair.faults import AmbiguousCommitDriver, is_proposal_apply

pytestmark = pytest.mark.anyio


@pytest.fixture
def faulty(storage: StorageDriver) -> AmbiguousCommitDriver:
    return AmbiguousCommitDriver(inner=storage)


@pytest.fixture
def harness(faulty: AmbiguousCommitDriver) -> ActionHarness:
    """The real harness over a driver that can lose one acknowledgement."""

    return ActionHarness(driver=faulty)


def _scope(harness: ActionHarness) -> CaseScope:
    return CaseScope(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
    )


def _arm(faulty: AmbiguousCommitDriver, *, unavailable_reads: int) -> None:
    """Lose the proposal apply's acknowledgement and make the next reads unavailable.

    The unavailability starts *at the lost acknowledgement*, so it lands on the commit-proof
    read and then on the worker's recovery read -- the two reads the scenario is about.
    """

    faulty.lose_ack_when = is_proposal_apply
    faulty.unavailable_after_lost_ack = unavailable_reads


# ---------------------------------------------------------------------------------------
# The reproduction, end to end through the worker
# ---------------------------------------------------------------------------------------


async def test_a_committed_but_unacknowledged_apply_recovers_to_succeeded(
    harness: ActionHarness, faulty: AmbiguousCommitDriver
) -> None:
    """Codex's exact sequence, and the count that matters: **one** model call in total."""

    await harness.prepare()
    started = await harness.start_operation()
    job = await harness.job(started)

    # The apply commits; the acknowledgement is lost; the commit-proof read and the worker's
    # first recovery read are both unavailable.
    _arm(faulty, unavailable_reads=2)
    settled = await harness.worker().execute(job)

    assert faulty.lost_acks == 1
    # Recoverable, not terminal.
    assert settled.status is ApplicationOperationStatus.RUNNING
    assert settled.error_code is None

    # The apply really is durable: this is what a terminal FAILED would have been lying about.
    record = await harness.compile.core.load_agent_invocation(
        _scope(harness), harness.invocation_id
    )
    assert record is not None
    assert record.outcome is AgentInvocationOutcome.SUCCEEDED
    case = await harness.compile.core.load_case(harness.scope)
    assert case.state is CaseState.ACTION_PROPOSED
    assert await harness.compile.shareable.load_current_action_pointer(harness.scope) is not None

    # Redelivery, with storage healthy again.
    faulty.lose_ack_when = None
    faulty.unavailable_after_lost_ack = 0
    faulty.unavailable_get_items = 0
    finished = await harness.worker().execute(job)

    assert finished.status is ApplicationOperationStatus.SUCCEEDED
    assert len(harness.agent.invocations) == 1


async def test_the_recovery_pass_invokes_no_model(
    harness: ActionHarness, faulty: AmbiguousCommitDriver
) -> None:
    """Recovery transcribes an outcome that already happened; it never re-asks."""

    await harness.prepare()
    started = await harness.start_operation()
    job = await harness.job(started)

    _arm(faulty, unavailable_reads=2)
    await harness.worker().execute(job)
    calls_after_ambiguity = len(harness.agent.invocations)

    faulty.lose_ack_when = None
    faulty.unavailable_after_lost_ack = 0
    faulty.unavailable_get_items = 0
    await harness.worker().execute(job)
    await harness.worker().execute(job)

    assert calls_after_ambiguity == 1
    assert len(harness.agent.invocations) == 1


async def test_recovery_on_the_same_pass_when_the_proof_read_is_available(
    harness: ActionHarness, faulty: AmbiguousCommitDriver
) -> None:
    """Only the commit-proof read fails, so the worker's own recovery read answers it.

    Zero model calls beyond the first, and no redelivery required.
    """

    await harness.prepare()
    started = await harness.start_operation()
    job = await harness.job(started)

    _arm(faulty, unavailable_reads=1)
    settled = await harness.worker().execute(job)

    assert settled.status is ApplicationOperationStatus.SUCCEEDED
    assert len(harness.agent.invocations) == 1
    assert set(settled.result_refs)


# ---------------------------------------------------------------------------------------
# The stale timeout must not settle an unresolved outcome
# ---------------------------------------------------------------------------------------


async def test_a_stale_running_timeout_does_not_fail_an_unresolved_outcome(
    harness: ActionHarness, faulty: AmbiguousCommitDriver
) -> None:
    """Time elapsing is not evidence about a transaction.

    The operation is left ``RUNNING`` by the ambiguous pass and then aged past the worker
    execution window. A redelivery whose proof read is *still* unavailable must leave it alone
    rather than let the generic stale path record a terminal failure.
    """

    from datetime import timedelta

    from chorus.application.operations import MAX_WORKER_EXECUTION_WINDOW

    await harness.prepare()
    started = await harness.start_operation()
    job = await harness.job(started)

    _arm(faulty, unavailable_reads=2)
    await harness.worker().execute(job)

    harness.compile.clock.instant = (
        harness.compile.clock.instant + MAX_WORKER_EXECUTION_WINDOW + timedelta(minutes=1)
    )
    faulty.lose_ack_when = None
    # Only the *proofs* stay unreadable; every other load still works, so the worker reaches
    # the stale path and must decline to take it.
    faulty.unavailable_proof_reads = 5
    aged = await harness.worker().execute(job)

    assert aged.status is ApplicationOperationStatus.RUNNING
    assert aged.error_code is None

    # And once the proof is readable again, it settles as what actually happened.
    faulty.unavailable_proof_reads = 0
    finished = await harness.worker().execute(job)
    assert finished.status is ApplicationOperationStatus.SUCCEEDED


async def test_a_proven_non_commit_still_settles_failed(
    harness: ActionHarness, faulty: AmbiguousCommitDriver
) -> None:
    """The other side of the invariant: proof of non-commit is a definite answer.

    Here the ambiguity is real but the apply genuinely did not commit -- there is no durable
    ``ACTION`` invocation record -- so a successful proof read that finds nothing settles the
    operation terminally, exactly as any other definite failure does.
    """

    await harness.prepare()
    started = await harness.start_operation()
    job = await harness.job(started)

    # Raise the ambiguous outcome from the unit of work *without* committing anything, which
    # is the shape of a transaction that failed and could not be classified.
    harness.unit_of_work.fail_next.append(UnknownTransactionOutcomeError("TRANSACT_WRITE"))
    settled = await harness.worker().execute(job)

    assert settled.status is ApplicationOperationStatus.FAILED
    assert settled.error_code == "UNKNOWN_TRANSACTION_OUTCOME"
    assert await harness.compile.shareable.load_current_action_pointer(harness.scope) is None
    assert len(harness.agent.invocations) == 1


async def test_the_ambiguous_transaction_is_the_ten_participant_apply(
    harness: ActionHarness, faulty: AmbiguousCommitDriver
) -> None:
    """Guard the fault predicate itself: the injected ambiguity is the proposal apply."""

    await harness.prepare()
    started = await harness.start_operation()
    job = await harness.job(started)

    _arm(faulty, unavailable_reads=2)
    await harness.worker().execute(job)

    applies = [
        plan for plan in harness.unit_of_work.plans if plan.name == PROPOSAL_APPLY_TRANSACTION
    ]
    assert len(applies) == 1
    assert len(applies[0].operations) == 10
    assert replace(applies[0]).commit_proof is not None
