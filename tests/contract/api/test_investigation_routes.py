"""``POST /v1/cases/{case_id}/investigations``: a 202, a handover, and no model call.

The route's whole job is to create one durable ``INVESTIGATE`` operation carrying its agent
handover identity and hand it over. Every policy decision -- what the model is shown, what its
answer means, whether the case becomes ready -- happens in the worker, behind the operation.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.contract.api.conftest import ApiHarness

from chorus.application.operations import investigate_binding_hash
from chorus.domain.entities import ApplicationOperationKind, ApplicationOperationStatus
from chorus.domain.ids import CaseId, OperationId

CASE_ID = "3f2a1b0c-4d5e-4f60-8a1b-2c3d4e5f6071"
HEADER = {"Idempotency-Key": "investigate-0000000001"}


def body(*, version: int = 1, reason: str = "INITIAL") -> dict[str, object]:
    return {"expected_case_version": version, "reason": reason}


def test_the_route_returns_202_and_a_pollable_operation(api: ApiHarness) -> None:
    response = api.client.post(
        f"/v1/cases/{CASE_ID}/investigations",
        json=body(),
        headers=api.presenter_headers(**HEADER),
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == ApplicationOperationStatus.PENDING.value
    assert payload["poll_url"] == f"/v1/operations/{payload['operation_id']}"
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.anyio
async def test_the_operation_carries_its_agent_handover_before_dispatch(
    api: ApiHarness,
) -> None:
    """ADR-016. The first delivery is bound exactly as tightly as the hundredth."""

    response = api.client.post(
        f"/v1/cases/{CASE_ID}/investigations",
        json=body(),
        headers=api.presenter_headers(**HEADER),
    )
    operation_id = OperationId(__import__("uuid").UUID(response.json()["operation_id"]))

    operation = await api.harness.operations.load(
        namespace=api.harness.namespace, operation_id=operation_id
    )

    assert operation.kind is ApplicationOperationKind.INVESTIGATE
    assert operation.case_id == CaseId(__import__("uuid").UUID(CASE_ID))
    assert operation.agent_invocation_id is not None
    assert operation.agent_binding_hash == investigate_binding_hash(
        case_id=CaseId(__import__("uuid").UUID(CASE_ID)),
        expected_case_version=1,
        reason="INITIAL",
    )


def test_the_job_is_dispatched_with_the_operations_own_invocation_identity(
    api: ApiHarness,
) -> None:
    api.client.post(
        f"/v1/cases/{CASE_ID}/investigations",
        json=body(),
        headers=api.presenter_headers(**HEADER),
    )

    assert len(api.dispatcher.investigations) == 1  # type: ignore[union-attr]
    job = api.dispatcher.investigations[0]  # type: ignore[union-attr]
    assert job.expected_case_version == 1
    assert job.reason == "INITIAL"
    assert job.case_id == CaseId(__import__("uuid").UUID(CASE_ID))


def test_an_exact_replay_returns_the_same_operation(api: ApiHarness) -> None:
    first = api.client.post(
        f"/v1/cases/{CASE_ID}/investigations",
        json=body(),
        headers=api.presenter_headers(**HEADER),
    )
    second = api.client.post(
        f"/v1/cases/{CASE_ID}/investigations",
        json=body(),
        headers=api.presenter_headers(**HEADER),
    )

    assert first.json()["operation_id"] == second.json()["operation_id"]


def test_the_same_key_under_a_different_request_conflicts(api: ApiHarness) -> None:
    api.client.post(
        f"/v1/cases/{CASE_ID}/investigations",
        json=body(),
        headers=api.presenter_headers(**HEADER),
    )
    conflicting = api.client.post(
        f"/v1/cases/{CASE_ID}/investigations",
        json=body(version=2),
        headers=api.presenter_headers(**HEADER),
    )

    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_a_reopen_of_the_same_case_version_is_a_different_request(api: ApiHarness) -> None:
    """An initial run and a reopen share a shape and must not share an invocation identity."""

    first = api.client.post(
        f"/v1/cases/{CASE_ID}/investigations",
        json=body(),
        headers=api.presenter_headers(**HEADER),
    )
    second = api.client.post(
        f"/v1/cases/{CASE_ID}/investigations",
        json=body(reason="REOPEN"),
        headers=api.presenter_headers(**{"Idempotency-Key": "investigate-0000000002"}),
    )

    assert first.json()["operation_id"] != second.json()["operation_id"]
    jobs = api.dispatcher.investigations  # type: ignore[union-attr]
    assert {job.invocation_id for job in jobs} == {jobs[0].invocation_id, jobs[1].invocation_id}
    assert len({job.invocation_id for job in jobs}) == 2


def test_a_resident_persona_cannot_start_an_investigation(api: ApiHarness) -> None:
    response = api.client.post(
        f"/v1/cases/{CASE_ID}/investigations",
        json=body(),
        headers=api.actor_headers("resident_a", **HEADER),
    )

    assert response.status_code == 403


def test_an_unknown_field_is_refused(api: ApiHarness) -> None:
    response = api.client.post(
        f"/v1/cases/{CASE_ID}/investigations",
        json={**body(), "fact_ids": [str(uuid4())]},
        headers=api.presenter_headers(**HEADER),
    )

    assert response.status_code == 422
    assert len(api.dispatcher.investigations) == 0  # type: ignore[union-attr]


def test_an_unknown_reason_is_refused(api: ApiHarness) -> None:
    response = api.client.post(
        f"/v1/cases/{CASE_ID}/investigations",
        json=body(reason="BECAUSE_I_SAID_SO"),
        headers=api.presenter_headers(**HEADER),
    )

    assert response.status_code == 422


def test_the_route_requires_an_idempotency_key(api: ApiHarness) -> None:
    response = api.client.post(
        f"/v1/cases/{CASE_ID}/investigations",
        json=body(),
        headers=api.presenter_headers(),
    )

    assert response.status_code == 422
