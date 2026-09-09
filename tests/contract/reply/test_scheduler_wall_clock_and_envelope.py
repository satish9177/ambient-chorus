"""P1/P2-1/P2-2, Phase 11 batch 4 repair: real wall-clock scheduling and a real watcher envelope.

Two defects Astra found in the real scheduler producer, both proved here against production
code -- not a manufactured stand-in:

**P2-2.** ``CreateDueSchedule`` used the *logical* clock to compute ``actual_now`` -- the real
instant EventBridge Scheduler should fire at. With the demo's logical clock advanced into 2030,
that scheduled a real AWS resource for 2030 real time, which never fires. The fix is a second,
distinct ``wall_clock`` field, read only for that one arithmetic step
(:mod:`chorus.application.commands.create_due_schedule`).

**P2-1.** The scheduler's ``Target.Input`` carried the bare ``commitment-due/v1`` event, but
what EventBridge Scheduler delivers to a Lambda target *is* ``Target.Input`` verbatim as the
invocation event -- and the production ``functions.commitment_watcher.handler`` entry point
understands only the operation-wrapped ``commitment-watcher-request/v1`` envelope. The fix
builds that exact envelope once, in
:func:`chorus.application.services.commitment_schedule.scheduled_watcher_invocation`, and
carries it as ``DueScheduleRequest.target_input``. The regression test below is the one the
review asked for by name: the *real* encoder's output is fed to the *real* production handler,
never a handler envelope manufactured independently in the test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from functions.commitment_watcher.composition import WatcherComposition
from functions.commitment_watcher.handler import ACCEPTED_OPERATIONS
from functions.commitment_watcher.handler import run as watcher_run
from functions.envelope import read_envelope
from tests.fixtures.compile import FixedClock
from tests.fixtures.reply import ReplyHarness

from chorus.application.commands.create_due_schedule import (
    CreateDueSchedule,
    CreateDueScheduleCommand,
)
from chorus.application.services.commitment_schedule import (
    due_schedule_request,
    scheduled_watcher_invocation,
)
from chorus.application.watcher_contract import decode_watcher_request
from chorus.domain.entities import Commitment
from chorus.domain.ids import CommunityId, Uuid4Generator
from chorus.ports.records import CommitmentScheduleStatus

pytestmark = pytest.mark.anyio


def _near(harness: ReplyHarness) -> datetime:
    """A received instant close enough to 2030-01-14 for the 30-day range to admit it.

    The fixture corpus states a fixed date so the reviewed text is stable; the clock is what
    moves. Setting the harness clock as well keeps ``uploaded_at`` and the range check reading
    the same world.
    """

    instant = datetime(2030, 1, 2, 9, 0, tzinfo=UTC)
    harness.send.action.compile.clock.instant = instant
    return instant


async def _commitment(harness: ReplyHarness, **overrides: object) -> Commitment:
    """Extract the one commitment the ``MANAGER_HEDGE``-shaped reply already promises.

    The reply fixtures promise ``2030-01-14`` in text
    (``src/chorus/infrastructure/fixtures/inbound_replies.py``), so the resulting commitment's
    ``due_at`` is a **real** far-future instant with no synthetic entity construction needed.
    """

    overrides.setdefault("received_at", _near(harness))
    ingested = await harness.ingest_reply(**overrides)
    job = await harness.extraction_job(ingested)
    result = await harness.extract().execute(job)
    assert result.commitment_id is not None, result.rejection_codes
    return await harness.commitment(result.commitment_id)


def _schedule_command(
    harness: ReplyHarness, commitment: Commitment, *, logical_now: datetime
) -> CreateDueScheduleCommand:
    from tests.fixtures.send import APPROVER_HASH

    return CreateDueScheduleCommand(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        commitment=commitment,
        actor_id_hash=APPROVER_HASH,
        correlation_id=commitment.commitment_id.value,
        logical_now=logical_now,
    )


def _create_due_schedule(harness: ReplyHarness, *, wall_clock: FixedClock) -> CreateDueSchedule:
    return CreateDueSchedule(
        shareable=harness.send.action.compile.shareable,
        audit=harness.send.action.compile.audit,
        unit_of_work=harness.send.action.unit_of_work,  # type: ignore[arg-type]
        scheduler=harness.scheduler,
        clock=harness.send.action.compile.clock,
        wall_clock=wall_clock,
        ids=Uuid4Generator(),
        scheduler_environment="test",
    )


# -- P2-2: the schedule fires at wall_now + logical delay, never at logical_now + delay -------


async def test_the_real_schedule_time_uses_wall_clock_not_logical_clock(
    reply_harness: ReplyHarness,
) -> None:
    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness)

    logical_now = reply_harness.send.action.compile.clock.instant
    # Deliberately a wall-clock reading nowhere near ``logical_now``: if the defect regressed
    # and ``actual_now`` were read from the logical clock again, the scheduled instant would
    # equal ``logical_now + delay`` instead, which is nowhere near this value either -- so the
    # two are easy to tell apart by construction.
    wall_now = datetime(2027, 3, 1, tzinfo=UTC)
    create_due_schedule = _create_due_schedule(reply_harness, wall_clock=FixedClock(wall_now))

    outcome = await create_due_schedule.execute(
        _schedule_command(reply_harness, commitment, logical_now=logical_now)
    )
    assert outcome.status is CommitmentScheduleStatus.CREATED

    request = reply_harness.scheduler.created[-1]
    expected_delay = max(timedelta(minutes=10), commitment.due_at - logical_now)
    assert request.at_utc == wall_now + expected_delay
    # And never the defect's own answer: wall time nowhere near ``logical_now`` cannot have
    # been produced by adding the delay to ``logical_now`` instead.
    assert request.at_utc != logical_now + expected_delay


async def test_the_schedule_is_a_real_future_aws_resource_even_when_the_logical_clock_is_far_ahead(
    reply_harness: ReplyHarness,
) -> None:
    """The exact scenario Astra reproduced: a logical clock advanced into 2030 must not push a
    real EventBridge Scheduler resource to 2030 real time."""

    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness)

    # The demo's logical clock, advanced far into the future relative to any real wall time.
    logical_now = datetime(2030, 1, 13, tzinfo=UTC)
    wall_now = datetime(2026, 1, 14, tzinfo=UTC)
    create_due_schedule = _create_due_schedule(reply_harness, wall_clock=FixedClock(wall_now))

    await create_due_schedule.execute(
        _schedule_command(reply_harness, commitment, logical_now=logical_now)
    )

    request = reply_harness.scheduler.created[-1]
    # A schedule anchored to 2030 real time would never fire. This one is anchored to the real
    # wall-clock reading plus a bounded delay -- nowhere near 2030.
    assert request.at_utc.year < 2028


# -- P2-1: the real encoder's Target.Input is accepted by the real production handler ---------


async def test_the_real_target_input_is_accepted_by_the_production_watcher_handler(
    reply_harness: ReplyHarness,
) -> None:
    """The regression the review asked for by name: not a handler envelope manufactured
    independently in the test, but the *actual* ``CreateDueSchedule``/scheduler-encoder output,
    fed to the *actual* ``functions.commitment_watcher.handler`` entry point.
    """

    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness)
    logical_now = reply_harness.send.action.compile.clock.instant
    create_due_schedule = _create_due_schedule(
        reply_harness, wall_clock=FixedClock(datetime(2027, 1, 1, tzinfo=UTC))
    )

    await create_due_schedule.execute(
        _schedule_command(reply_harness, commitment, logical_now=logical_now)
    )
    request = reply_harness.scheduler.created[-1]

    # ``target_input`` is exactly the JSON object a real ``CreateSchedule`` call's
    # ``Target.Input`` carries, and Scheduler delivers it to the Lambda target verbatim as the
    # invocation event -- so it is passed to the production handler's ``run()`` exactly as AWS
    # would, with no envelope built by this test.
    event = request.target_input
    _operation, payload = read_envelope(event, accepted=ACCEPTED_OPERATIONS)
    command = decode_watcher_request(payload)

    assert command.event.commitment_id == commitment.commitment_id
    assert command.event.expected_generation == commitment.schedule_generation
    assert command.community_id == reply_harness.scope.community_id
    assert command.trigger == "SCHEDULE"

    # And the full handler, not just the decoder -- proving the same watcher application logic
    # this event would reach in a real Scheduler-triggered invocation actually runs.
    graph = _watcher_graph(reply_harness)
    result = await watcher_run(event, built=graph)
    assert result["schema"] == "commitment-watcher-result/v1"


async def test_a_retried_create_schedule_call_submits_byte_identical_target_input(
    reply_harness: ReplyHarness,
) -> None:
    """Any equality/idempotency verification of ``Target.Input`` (review § 7): a retry under
    the same client token must submit the identical bytes, or AWS could reject it as a
    conflicting request under a token that is supposed to make retries safe.
    """

    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness)

    first = due_schedule_request(
        environment="test",
        namespace=reply_harness.scope.namespace,
        community_id=reply_harness.scope.community_id,
        case_id=commitment.case_id,
        commitment_id=commitment.commitment_id,
        generation=commitment.schedule_generation,
        due_at=commitment.due_at,
        at_utc=commitment.due_at,
    )
    second = due_schedule_request(
        environment="test",
        namespace=reply_harness.scope.namespace,
        community_id=reply_harness.scope.community_id,
        case_id=commitment.case_id,
        commitment_id=commitment.commitment_id,
        generation=commitment.schedule_generation,
        due_at=commitment.due_at,
        at_utc=commitment.due_at,
    )

    assert json.dumps(first.target_input, sort_keys=True) == json.dumps(
        second.target_input, sort_keys=True
    )
    assert first.client_token == second.client_token
    assert first.schedule_name == second.schedule_name


def test_scheduled_watcher_invocation_names_no_persona_and_no_fresh_uuid() -> None:
    """``actor_id_hash`` and ``correlation_id`` must be deterministic functions of
    ``{commitment_id, generation}`` -- never a fresh UUID -- or two calls for the identical
    retry would already disagree before anything else does."""

    from chorus.domain.ids import CaseId, CommitmentId, Namespace
    from chorus.domain.time import require_utc

    namespace = Namespace("TEST_ELEVATOR_V1")
    community_id = CommunityId(Uuid4Generator().new(CommunityId).value)
    case_id = CaseId(Uuid4Generator().new(CaseId).value)
    commitment_id = CommitmentId(Uuid4Generator().new(CommitmentId).value)
    due_at = require_utc(datetime(2030, 2, 13, tzinfo=UTC))

    first = scheduled_watcher_invocation(
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        commitment_id=commitment_id,
        generation=1,
        due_at=due_at,
    )
    second = scheduled_watcher_invocation(
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        commitment_id=commitment_id,
        generation=1,
        due_at=due_at,
    )
    assert first == second


def _watcher_graph(harness: ReplyHarness) -> WatcherComposition:
    from chorus.infrastructure.persistent_clock import ScopedLogicalClock
    from chorus.ports.demo_clock import DemoClockRecord

    class _StubClockStore:
        async def read(self) -> DemoClockRecord:
            instant = harness.send.action.compile.clock.instant
            return DemoClockRecord(
                logical_time=instant,
                version=1,
                reset_generation=1,
                seed_instant=instant,
                advance_count=0,
            )

        async def advance(self, expected: DemoClockRecord, *, to: datetime) -> DemoClockRecord:
            raise AssertionError("the watcher must never advance the clock")

    return WatcherComposition(
        watcher=harness.watcher(),
        clock_store=_StubClockStore(),
        scope=ScopedLogicalClock(),
    )
