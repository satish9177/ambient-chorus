"""What a delivered job has to prove before a worker will spend a model call on it.

A job is data on a queue. It can be misrouted, replayed from another command family, or built
by something that kept a valid-looking identifier and changed what sits under it. The worker's
only defence is the durable operation, so the operation has to carry enough to decide -- and
carry it from the moment it is created, not from the moment it first writes something.

That last part is the gap these tests exist for. The earlier binding checked the delivered
invocation against *records under the operation*, which meant a first delivery -- the one
delivery for which no such record can exist -- had nothing to disagree with and was accepted
on trust. A caller holding a valid ``operation_id``, actor hash, and request hash could
therefore substitute its own invocation identity, or hand the Monitor a different slice of the
batch, and the frozen input would be built from whatever arrived.

Every case below is a **first** delivery for that reason: nothing has been written under the
operation yet, and the refusal has to come from the operation row itself. After each refusal
the assertions are the same four: no model call, no snapshot, no progress, and an operation
whose status and version are exactly what they were.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from tests.fixtures.monitor import MonitorHarness
from tests.fixtures.monitor_answers import THREE_GROUPS, grouped_answer

from chorus.application.commands.run_monitor_operation import JobBinding
from chorus.domain.entities import (
    ApplicationOperation,
    ApplicationOperationKind,
    ApplicationOperationStatus,
)
from chorus.domain.ids import Sha256Digest
from chorus.infrastructure.local.monitor_agent import ScriptedMonitorAgent
from chorus.ports.ambient import AmbientMessage
from chorus.ports.operations import MonitorOperationJob
from chorus.ports.records import MessageFeedEntry, MonitorSnapshotKind
from chorus.ports.scopes import OperationScope

pytestmark = pytest.mark.anyio

FOREIGN_HASH = Sha256Digest("sha256:" + "7" * 64)


def _responder() -> ScriptedMonitorAgent:
    return ScriptedMonitorAgent(
        responder=lambda invocation: grouped_answer(invocation.payload, THREE_GROUPS)
    )


def _scope(harness: MonitorHarness, job: MonitorOperationJob) -> OperationScope:
    return OperationScope(namespace=harness.namespace, operation_id=job.operation_id)


async def _bound(
    harness: MonitorHarness,
) -> tuple[ApplicationOperation, MonitorOperationJob, tuple[MessageFeedEntry, ...]]:
    await harness.seed()
    locators = await harness.ingest_feed()
    operation, job = await harness.dispatched(locators)
    assert operation.status is ApplicationOperationStatus.PENDING
    assert operation.agent_invocation_id is not None
    assert operation.agent_binding_hash is not None
    return operation, job, locators


async def _extra_locator(harness: MonitorHarness) -> MessageFeedEntry:
    """One further ingested message that the operation never authorized."""

    newest = harness.adapter.messages()[-1]
    message = AmbientMessage(
        adapter="SYNTHETIC",
        channel_message_id="foreign-001",
        contributor_pseudonym=harness.adapter.contributor_seeds[0].pseudonym,
        sent_at=newest.sent_at + timedelta(minutes=5),
        text="A message the dispatched job was never given.",
    )
    result = await harness.ingest_messages((message,), idempotency_key="foreign-key-000001")
    return MessageFeedEntry(message_id=result.messages[0].message_id, sent_at=message.sent_at)


async def _assert_refused(
    harness: MonitorHarness,
    operation: ApplicationOperation,
    job: MonitorOperationJob,
) -> None:
    """Run the job and assert it changed nothing at all."""

    agent = _responder()
    returned = await harness.worker(agent).execute(job)

    assert agent.invocations == [], "a misrouted job never reaches the model"
    assert returned.status is operation.status
    assert returned.version == operation.version

    current = await harness.operations.load(
        namespace=harness.namespace, operation_id=operation.operation_id
    )
    assert current.status is operation.status
    assert current.version == operation.version
    assert current.error_code == operation.error_code

    scope = _scope(harness, job)
    assert await harness.core.load_monitor_progress(scope, job.invocation_id) is None
    for kind in MonitorSnapshotKind:
        assert (
            await harness.core.load_monitor_snapshot_manifest(
                scope, kind=kind, invocation_id=job.invocation_id
            )
            is None
        )
    assert await harness.core.load_operation_agent_invocation(scope, job.invocation_id) is None


# ---------------------------------------------------------------------------------------
# A -- a substituted invocation identity
# ---------------------------------------------------------------------------------------


async def test_a_first_delivery_naming_another_invocation_is_refused(
    harness: MonitorHarness,
) -> None:
    """The gap, closed: nothing is written yet, and the operation still knows the answer."""

    operation, _job, locators = await _bound(harness)
    forged = harness.job_for(operation, locators, invocation_id=uuid4())

    await _assert_refused(harness, operation, forged)


# ---------------------------------------------------------------------------------------
# B and C -- a substituted message set under a valid request hash
# ---------------------------------------------------------------------------------------


async def test_a_first_delivery_carrying_fewer_locators_is_refused(
    harness: MonitorHarness,
) -> None:
    """One locator instead of twenty-four, with every other field genuine.

    The request hash still matches, because it names the *command* rather than the identifiers
    persisting it produced. Only the locator digest distinguishes "the batch this operation was
    created for" from "some of it", and narrowing the batch is not a smaller version of the
    same run -- it is a different question, answered under an identity that is not its own.
    """

    operation, _job, locators = await _bound(harness)
    assert len(locators) > 1
    narrowed = harness.job_for(operation, locators[:1])

    await _assert_refused(harness, operation, narrowed)


async def test_a_first_delivery_carrying_a_foreign_locator_is_refused(
    harness: MonitorHarness,
) -> None:
    """A message the operation never authorized, added to a set that is otherwise correct."""

    operation, _job, locators = await _bound(harness)
    foreign = await _extra_locator(harness)
    widened = harness.job_for(operation, (*locators, foreign))

    await _assert_refused(harness, operation, widened)


async def test_a_first_delivery_whose_locators_were_reordered_is_accepted(
    harness: MonitorHarness,
) -> None:
    """Order-insensitive on purpose, and the same purpose as the request hash.

    The endpoint takes a batch and Monitor processing canonicalizes its order anyway, so two
    deliveries of the same messages in a different array order are the same work. Refusing one
    of them would make a queue that legitimately reorders look like an attacker, while catching
    nothing: any *set* difference still changes the digest.
    """

    operation, _job, locators = await _bound(harness)
    shuffled = harness.job_for(operation, tuple(reversed(locators)))

    agent = _responder()
    finished = await harness.worker(agent).execute(shuffled)

    assert finished.status is ApplicationOperationStatus.SUCCEEDED
    assert len(agent.invocations) == 1


async def test_a_locator_whose_instant_was_moved_is_refused(
    harness: MonitorHarness,
) -> None:
    """The anchor of the context window is part of the set, so it is part of the digest.

    The earliest new message decides which prior messages the Monitor is shown. Moving one
    instant while keeping every identifier would change what the model reads without changing
    which messages it was given, which is exactly the kind of steering the digest exists to
    make impossible.
    """

    operation, _job, locators = await _bound(harness)
    moved = (
        MessageFeedEntry(
            message_id=locators[0].message_id,
            sent_at=locators[0].sent_at - timedelta(days=30),
        ),
        *locators[1:],
    )

    await _assert_refused(harness, operation, harness.job_for(operation, moved))


# ---------------------------------------------------------------------------------------
# E -- another command family entirely
# ---------------------------------------------------------------------------------------


async def test_a_job_for_a_propose_action_operation_is_refused(
    harness: MonitorHarness,
) -> None:
    """Failing it would be worse than ignoring it: another family's command, recorded failed."""

    await harness.seed()
    locators = await harness.ingest_feed()
    foreign = await harness.bound_operation(locators, kind=ApplicationOperationKind.PROPOSE_ACTION)
    assert foreign.agent_invocation_id is None

    await _assert_refused(harness, foreign, harness.job_for(foreign, locators))


async def test_a_monitor_operation_without_a_handover_identity_is_refused(
    harness: MonitorHarness,
) -> None:
    """An unbound MONITOR operation authorizes nothing, and is never trusted by default.

    This is the shape the old code accepted implicitly. It cannot be produced by the route any
    more, so the test builds one directly -- the point is that if one ever appeared, from an
    older row or a partial migration, it would be refused rather than run.
    """

    await harness.seed()
    locators = await harness.ingest_feed()
    unbound = await harness.operations.create(
        namespace=harness.namespace,
        kind=ApplicationOperationKind.MONITOR,
        actor_id_hash=FOREIGN_HASH,
        request_hash=FOREIGN_HASH,
    )
    job = harness.job_for(unbound, locators, invocation_id=uuid4())

    await _assert_refused(harness, unbound, job)


# ---------------------------------------------------------------------------------------
# The reason codes an operator reads
# ---------------------------------------------------------------------------------------


def test_every_binding_reason_is_a_closed_safe_code() -> None:
    """Reason codes are logged, so they carry no identifier, hash, or message content."""

    codes = (
        JobBinding.KIND,
        JobBinding.NAMESPACE,
        JobBinding.ACTOR,
        JobBinding.REQUEST,
        JobBinding.INVOCATION,
        JobBinding.LOCATORS,
        JobBinding.UNBOUND,
    )
    assert len(set(codes)) == len(codes)
    for code in codes:
        assert code.isupper()
        assert code.replace("_", "").isalnum()
