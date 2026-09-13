"""P1, Phase 11 batch 4 repair: compile -> propose -> approve -> send share one clock, always.

Before this repair, the compiler read ``SystemClock`` while the rest of the case world -- the
worker's proposal, the approval, and the sender -- read the demo's durable logical clock. A view
the compiler stamped with a real wall-clock instant and a proposal or a send stamped with the
demo's logical instant were then two different facts about "now", and nothing in the pipeline
noticed, because nothing compared them directly -- the mismatch only ever surfaced downstream,
as a freshness check comparing two clocks that were never the same clock to begin with.

This test proves the positive property the repair establishes: every timestamp the full
preparation/execution path produces -- the compiled view's ``generated_at``, and the send
execution's ``created_at``/``updated_at``/``started_at``/``finished_at`` -- traces back to the
**one** clock reading the harness controls, and never to a real wall-clock reading taken while
the test runs. Before batch 4, the view's timestamp would have come from ``SystemClock`` and
would *not* have equalled the harness's controlled reading; asserting equality here is what
would have caught the regression.

Fake model, fake transport, no AWS: this drives the real application classes
(:class:`~chorus.application.commands.compile_view.CompileView`,
:class:`~chorus.application.commands.propose_action.ProposeAction`,
:class:`~chorus.application.commands.approve_action.ApproveAction`,
:class:`~chorus.application.commands.send_action.SendAction`) the same way every other contract
test in this suite does, through :class:`~tests.fixtures.send.SendHarness` --
``ScriptedActionAgent`` answers the proposal and ``ScriptedSender`` answers the send. The
handler-level regression tests
for the two named principals (``tests/unit/functions/test_compiler_handler.py`` and
``tests/unit/functions/test_sender_handler.py``) separately prove each one reads the *same*
durable ``DemoClockStorePort`` abstraction across a real Lambda-shaped boundary; this test proves
what that buys the pipeline as a whole.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.fixtures.send import SendHarness

pytestmark = pytest.mark.anyio


async def test_every_produced_timestamp_traces_to_the_one_shared_clock(
    send_harness: SendHarness,
) -> None:
    """The direct proof of the P1 repair: one clock, read by every principal, never two.

    Before batch 4, the compiler's ``generated_at`` would have come from ``SystemClock`` -- a
    real reading that would not equal the harness's controlled instant. Asserting every produced
    timestamp equals that one controlled reading, across compile, propose, approve, and send, is
    what makes "the compiler and the sender share a clock" a property of the produced artifacts
    rather than an implementation detail nobody checks.
    """

    harness = send_harness
    controlled_instant = harness.action.compile.clock.instant
    real_wall_clock_reading = datetime.now(UTC)
    # The two could coincide only by a coincidence of when the suite happens to run; the harness
    # fixture seeds its own clock years away from "now" specifically so they never do.
    assert abs((controlled_instant - real_wall_clock_reading).total_seconds()) > 3600

    view = await harness.prepare()
    assert view.generated_at == controlled_instant

    await harness.approve()
    await harness.send()
    execution = await harness.execution()

    assert execution.state.value == "SENT"
    for instant in (
        execution.created_at,
        execution.updated_at,
        execution.started_at,
        execution.finished_at,
    ):
        assert instant is not None
        assert instant == controlled_instant
        assert instant != real_wall_clock_reading
