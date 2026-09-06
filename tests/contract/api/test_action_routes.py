"""``POST /v1/cases/{case_id}/actions``: a 202, a handover, and no model call.

The route's whole job is to create one durable ``PROPOSE_ACTION`` operation carrying its agent
handover identity and hand it over. Every policy decision -- what the model is shown, whether
the view is still current, whether the answer is grounded, whether anything is persisted --
happens in the worker, behind the operation.

What the body may contain is the other half of the design. It names the case version the caller
expected and the exact view they believe is current, and nothing else. There is no field for a
subject, a claim, a caveat, a tone, a deadline, or a recipient, because a client that could name
any of those would be doing the drafting.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.contract.api.conftest import ApiHarness

from chorus.application.operations import propose_action_binding_hash
from chorus.domain.entities import ApplicationOperationKind, ApplicationOperationStatus
from chorus.domain.ids import CaseId, OperationId, Sha256Digest
from chorus.infrastructure.local.dispatch import RecordingOperationDispatcher
from chorus.ports.operations import ProposeActionOperationJob

CASE_ID = "3f2a1b0c-4d5e-4f60-8a1b-2c3d4e5f6071"
VIEW_ID = "9d1c7b6a-5e4f-4a3b-9c8d-7e6f5a4b3c2d"
VIEW_HASH = "sha256:" + "1" * 64
HEADER = {"Idempotency-Key": "propose-action-0001"}


def body(
    *, version: int = 1, view_id: str = VIEW_ID, view_hash: str = VIEW_HASH
) -> dict[str, object]:
    return {
        "expected_case_version": version,
        "view_id": view_id,
        "view_hash": view_hash,
    }


def _dispatched(api: ApiHarness) -> list[ProposeActionOperationJob]:
    """The proposal jobs this request actually handed over.

    The harness always wires the recording dispatcher, because a route test that ran the worker
    would be testing the worker. Narrowing here rather than at each call site keeps the
    assertions about the *job* rather than about which dispatcher happens to be installed.
    """

    dispatcher = api.dispatcher
    assert isinstance(dispatcher, RecordingOperationDispatcher)
    return dispatcher.proposals


def _post(api: ApiHarness, json: dict[str, object] | None = None) -> Any:
    payload = body()
    payload.update(json or {})
    return api.client.post(
        f"/v1/cases/{CASE_ID}/actions", json=payload, headers=api.presenter_headers(**HEADER)
    )


def test_the_route_returns_202_and_a_pollable_operation(api: ApiHarness) -> None:
    response = _post(api)

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == ApplicationOperationStatus.PENDING.value
    assert payload["poll_url"] == f"/v1/operations/{payload['operation_id']}"
    assert response.headers["Cache-Control"] == "no-store"


def test_the_route_calls_no_model(api: ApiHarness) -> None:
    """The handover is data on a queue. Nothing is invoked inside the request."""

    _post(api)

    assert len(_dispatched(api)) == 1


@pytest.mark.anyio
async def test_the_operation_carries_its_agent_handover_before_dispatch(
    api: ApiHarness,
) -> None:
    """ADR-016, and it matters more here than anywhere else.

    The invocation identity also *derives the action identity*, so an unbound first delivery
    could present a fresh identity, mint a second action, and write a second candidate message
    for one case.
    """

    response = _post(api)
    operation_id = OperationId(UUID(response.json()["operation_id"]))

    operation = await api.harness.operations.load(
        namespace=api.harness.namespace, operation_id=operation_id
    )

    assert operation.kind is ApplicationOperationKind.PROPOSE_ACTION
    assert operation.case_id == CaseId(UUID(CASE_ID))
    assert operation.agent_invocation_id is not None
    assert operation.agent_binding_hash == propose_action_binding_hash(
        case_id=CaseId(UUID(CASE_ID)),
        view_id=UUID(VIEW_ID),
        view_hash=Sha256Digest(VIEW_HASH),
    )


def test_the_dispatched_job_names_the_exact_view_and_nothing_of_the_message(
    api: ApiHarness,
) -> None:
    _post(api)
    job = _dispatched(api)[0]

    assert job.view_id.value == UUID(VIEW_ID)
    assert job.view_hash == Sha256Digest(VIEW_HASH)
    assert job.expected_case_version == 1
    for absent in ("subject", "claims", "caveats", "tone", "recipient", "body"):
        assert not hasattr(job, absent)


def test_the_same_key_and_request_returns_the_same_operation(api: ApiHarness) -> None:
    """One key, one operation, one invocation identity -- and no second model call later."""

    first = _post(api).json()
    second = _post(api).json()

    assert first["operation_id"] == second["operation_id"]


def test_the_same_key_with_a_different_request_conflicts_with_zero_mutations(
    api: ApiHarness,
) -> None:
    """409 ``IDEMPOTENCY_CONFLICT``, and the first operation is left exactly as it was."""

    first = _post(api).json()
    response = _post(api, json={"view_id": str(uuid4())})

    assert response.status_code == 409
    assert len(_dispatched(api)) == 1
    assert _dispatched(api)[0].operation_id.value == UUID(first["operation_id"])


def test_a_still_pending_replay_dispatches_again(api: ApiHarness) -> None:
    """Dispatch is the one step after the durable record that can fail on its own.

    An operation whose only delivery was lost would otherwise sit ``PENDING`` forever. The
    worker's conditional claim, not the dispatcher, is where duplicate execution is prevented.
    """

    _post(api)
    _post(api)

    assert len(_dispatched(api)) == 2
    assert _dispatched(api)[0].operation_id == _dispatched(api)[1].operation_id
    assert _dispatched(api)[0].invocation_id == _dispatched(api)[1].invocation_id


def test_the_idempotency_key_header_is_required(api: ApiHarness) -> None:
    response = api.client.post(
        f"/v1/cases/{CASE_ID}/actions", json=body(), headers=api.presenter_headers()
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_case_version": 0, "view_id": VIEW_ID, "view_hash": VIEW_HASH},
        {"expected_case_version": 1, "view_id": "not-a-uuid", "view_hash": VIEW_HASH},
        {"expected_case_version": 1, "view_id": VIEW_ID, "view_hash": "sha256:short"},
        {"expected_case_version": 1, "view_id": VIEW_ID},
    ],
)
def test_a_malformed_body_is_refused(api: ApiHarness, payload: dict[str, object]) -> None:
    response = api.client.post(
        f"/v1/cases/{CASE_ID}/actions",
        json=payload,
        headers=api.presenter_headers(**HEADER),
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "extra",
    ["subject", "claims", "caveats", "tone", "recipient", "body", "requested_action"],
)
def test_a_field_nobody_declared_can_never_ride_along(api: ApiHarness, extra: str) -> None:
    """``extra='forbid'`` on the transport body, asserted per field a caller might try.

    Each of these is something a client might plausibly want to supply, and each would move a
    decision out of the deterministic path and into the request.
    """

    response = api.client.post(
        f"/v1/cases/{CASE_ID}/actions",
        json={**body(), extra: "anything"},
        headers=api.presenter_headers(**HEADER),
    )

    assert response.status_code == 422


def test_only_the_presenter_may_request_a_proposal(api: ApiHarness) -> None:
    response = api.client.post(
        f"/v1/cases/{CASE_ID}/actions",
        json=body(),
        headers={**api.actor_headers("resident_a"), **HEADER},
    )

    assert response.status_code == 403


def test_an_unknown_actor_is_refused(api: ApiHarness) -> None:
    response = api.client.post(
        f"/v1/cases/{CASE_ID}/actions",
        json=body(),
        headers={"X-Chorus-Demo-Actor": "nobody", **HEADER},
    )

    assert response.status_code == 403


def test_the_response_exposes_no_proposal_content(api: ApiHarness) -> None:
    """202 carries an operation reference and nothing about a message that does not exist yet."""

    payload = _post(api).json()

    assert set(payload) == {"operation_id", "status", "poll_url"}
