"""P2: a scheduler adapter failure after commitment apply is recoverable, not terminal.

When ``CreateDueSchedule`` fails once (``SCHEDULER_UNAVAILABLE``) the extraction operation must
not complete as ``SUCCEEDED`` with the deadline unscheduled. It goes back to ``PENDING`` so a
replay re-dispatches it; the redelivery resumes through the durable agent-invocation record
(one model call, ever), does not create a second commitment, and retries only the idempotent
``CreateDueSchedule`` step until exactly one local schedule exists and the projection is
``CREATED``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from tests.smoke.conftest import APPROVER, PRESENTER
from tests.smoke.test_local_negative import _case_with_current_action

from chorus.composition.local import LocalComposition
from chorus.domain.entities import ApplicationOperationStatus
from chorus.domain.ids import CaseId, CommitmentId
from chorus.ports.records import CommitmentScheduleStatus
from chorus.ports.scheduler import ScheduleCreateFailed, ScheduleFailureCode
from chorus.ports.scopes import CaseScope

pytestmark = pytest.mark.anyio


async def _executed_case(client: TestClient, composition: LocalComposition) -> str:
    """Progress to a SENT execution -- the state a manager reply is answered from."""

    case_id, current_action = await _case_with_current_action(client, composition)
    approve = client.post(
        f"/v1/cases/{case_id}/actions/{current_action['action_id']}/approvals",
        headers={**APPROVER, "Idempotency-Key": "sched-recover-approve-0001"},
        json={
            "decision": "APPROVED",
            "expected_execution_version": current_action["execution"]["version"],
            "execution_id": current_action["execution"]["execution_id"],
            "view_hash": current_action["view_hash"],
            "proposal_hash": current_action["proposal_hash"],
            "preview_hash": current_action["preview"]["preview_hash"],
        },
    ).json()
    execute = client.post(
        f"/v1/cases/{case_id}/actions/{current_action['action_id']}/executions",
        headers={**APPROVER, "Idempotency-Key": "sched-recover-execute-0001"},
        json={
            "execution_id": approve["execution_id"],
            "expected_execution_version": approve["execution_version"],
            "approval_id": approve["approval_id"],
        },
    )
    assert execute.status_code == 202, execute.text
    await composition.dispatcher.drain()
    return case_id


def _post_reply(client: TestClient, key: str) -> Any:
    return client.post(
        "/v1/demo/external-replies",
        headers={**PRESENTER, "Idempotency-Key": key},
        json={"fixture_id": "manager-promise"},
    )


def _operation(client: TestClient, operation_id: str) -> dict[str, Any]:
    body: dict[str, Any] = client.get(f"/v1/operations/{operation_id}", headers=PRESENTER).json()
    return body


async def test_scheduler_failure_is_recovered_by_a_replay_without_a_second_model_call(
    client: TestClient, composition: LocalComposition
) -> None:
    commitments = composition.container.commitments
    assert commitments is not None
    case_id = await _executed_case(client, composition)

    # 3. The first CreateDueSchedule attempt fails.
    composition.scheduler.outcomes.append(
        ScheduleCreateFailed(reason_code=ScheduleFailureCode.SCHEDULER_UNAVAILABLE)
    )

    first = _post_reply(client, "sched-recover-reply-0001")
    assert first.status_code == 202, first.text
    operation_id = first.json()["operation_id"]
    await composition.dispatcher.drain()

    # 1-2. The model ran once and the commitment is durable.
    assert len(composition.commitment_extractor.invocations) == 1
    case_body = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    assert len(case_body["commitments"]) == 1
    commitment_id = case_body["commitments"][0]["commitment_id"]

    # 4-5. The operation did not terminally succeed -- it is reachable for recovery.
    after_failure = _operation(client, operation_id)
    assert after_failure["status"] == ApplicationOperationStatus.PENDING.value, after_failure

    scope = CaseScope(
        namespace=composition.container.namespace,
        community_id=composition.container.community_id,
        case_id=CaseId(UUID(case_id)),
    )
    projection = await commitments.load_commitment_schedule(
        scope, CommitmentId(UUID(commitment_id))
    )
    assert projection is not None
    assert projection.status is CommitmentScheduleStatus.PENDING_SCHEDULE

    # 6. Retry: the same reply, same key, re-dispatches the still-PENDING operation.
    retry = _post_reply(client, "sched-recover-reply-0001")
    assert retry.status_code == 202, retry.text
    assert retry.json()["operation_id"] == operation_id
    await composition.dispatcher.drain()

    # 7. No second model call. 8. Still exactly one commitment.
    assert len(composition.commitment_extractor.invocations) == 1
    case_after = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    assert len(case_after["commitments"]) == 1

    # 9. Exactly one local schedule now exists (two deliberate requests: one failed, one made).
    assert len(composition.scheduler.schedules) == 1
    assert composition.scheduler.create_count == 2

    # 10. The projection reached CREATED, and the operation is now SUCCEEDED.
    recovered = await commitments.load_commitment_schedule(scope, CommitmentId(UUID(commitment_id)))
    assert recovered is not None
    assert recovered.status is CommitmentScheduleStatus.CREATED
    assert _operation(client, operation_id)["status"] == (
        ApplicationOperationStatus.SUCCEEDED.value
    )

    # A further replay after successful scheduling makes no third scheduler request.
    done = _post_reply(client, "sched-recover-reply-0001")
    assert done.status_code == 202, done.text
    await composition.dispatcher.drain()
    assert composition.scheduler.create_count == 2
    assert len(composition.commitment_extractor.invocations) == 1


async def test_normal_scheduling_reaches_created_on_the_first_attempt(
    client: TestClient, composition: LocalComposition
) -> None:
    commitments = composition.container.commitments
    assert commitments is not None
    case_id = await _executed_case(client, composition)
    reply = _post_reply(client, "sched-normal-reply-0001")
    assert reply.status_code == 202, reply.text
    await composition.dispatcher.drain()

    outcome = _operation(client, reply.json()["operation_id"])
    assert outcome["status"] == ApplicationOperationStatus.SUCCEEDED.value, outcome
    assert composition.scheduler.create_count == 1
    commitment_id = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()["commitments"][0][
        "commitment_id"
    ]
    scope = CaseScope(
        namespace=composition.container.namespace,
        community_id=composition.container.community_id,
        case_id=CaseId(UUID(case_id)),
    )
    projection = await commitments.load_commitment_schedule(
        scope, CommitmentId(UUID(commitment_id))
    )
    assert projection is not None
    assert projection.status is CommitmentScheduleStatus.CREATED
