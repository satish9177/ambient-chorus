"""P2-9: the case surface exposes the safe half of the commitment's schedule projection.

`CommitmentSafeResponse` gains `schedule_status` (`PENDING_SCHEDULE`/`CREATED`) and
`schedule_last_error_code` -- both read from `CommitmentScheduleProjection`, which the
extraction transaction already writes. Never `schedule_name` (transport addressing) or the
due-event/replay identities the watcher itself authenticates against.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.smoke.conftest import PRESENTER
from tests.smoke.test_local_scheduler_recovery import _executed_case, _post_reply

from chorus.composition.local import LocalComposition

pytestmark = pytest.mark.anyio


async def test_the_case_surface_shows_the_commitment_was_scheduled(
    client: TestClient, composition: LocalComposition
) -> None:
    case_id = await _executed_case(client, composition)

    reply = _post_reply(client, "p29-reply-0001")
    assert reply.status_code == 202, reply.text
    await composition.dispatcher.drain()

    final_case = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    assert final_case["commitments"], "the reply should have produced a commitment"
    commitment = final_case["commitments"][0]

    # The safe half of the schedule projection is present and correctly typed...
    assert commitment["schedule_status"] in {"PENDING_SCHEDULE", "CREATED"}
    assert commitment["schedule_status"] == "CREATED", "the local scheduler answers synchronously"
    assert commitment["schedule_last_error_code"] is None

    # ...and nothing unsafe rides along with it.
    body_text = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).text
    assert "scheduler_name" not in body_text
    assert "due_event_id" not in body_text
    assert "chorus-local-scheduler" not in body_text.lower()
