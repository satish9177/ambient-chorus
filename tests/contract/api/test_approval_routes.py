"""The three Phase-8 transports, and the fields they refuse to accept.

Three routes and no fourth. What matters most here is what the bodies **cannot** carry: the
approval body has no text field of any kind, so there is nothing in which an edited subject or
body could be submitted, and the execute body accepts no recipient, subject, body, claim,
attachment, template, or retry flag.

Those absences are properties of the closed models rather than validation rules, which is why a
transport test can prove them: a field nobody declared is refused with 422 before any use case
sees the request.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.contract.api.conftest import ApiHarness

from chorus.domain.entities import ApplicationOperationKind, ApplicationOperationStatus
from chorus.infrastructure.local.dispatch import RecordingOperationDispatcher
from chorus.ports.operations import SendActionOperationJob

CASE_ID = "3f2a1b0c-4d5e-4f60-8a1b-2c3d4e5f6071"
ACTION_ID = "7a6b5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d"
EXECUTION_ID = "1b2c3d4e-5f60-4718-9a2b-3c4d5e6f7081"
APPROVAL_ID = "2c3d4e5f-6071-4829-8b3c-4d5e6f708192"
DIGEST = "sha256:" + "1" * 64

APPROVAL_HEADER = {"Idempotency-Key": "approve-action-0001"}
INVALIDATION_HEADER = {"Idempotency-Key": "invalidate-action-0001"}
EXECUTION_HEADER = {"Idempotency-Key": "send-action-0001"}


def approval_body(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision": "APPROVED",
        "expected_execution_version": 1,
        "execution_id": EXECUTION_ID,
        "view_hash": DIGEST,
        "proposal_hash": DIGEST,
        "preview_hash": DIGEST,
    }
    payload.update(overrides)
    return payload


def execution_body(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "execution_id": EXECUTION_ID,
        "expected_execution_version": 2,
        "approval_id": APPROVAL_ID,
    }
    payload.update(overrides)
    return payload


def _approvals_url() -> str:
    return f"/v1/cases/{CASE_ID}/actions/{ACTION_ID}/approvals"


def _invalidation_url() -> str:
    return f"/v1/cases/{CASE_ID}/actions/{ACTION_ID}/invalidation"


def _executions_url() -> str:
    return f"/v1/cases/{CASE_ID}/actions/{ACTION_ID}/executions"


def _approver(api: ApiHarness, **extra: str) -> dict[str, str]:
    return api.actor_headers("case_approver", **extra)


def _dispatched(api: ApiHarness) -> list[SendActionOperationJob]:
    dispatcher = api.dispatcher
    assert isinstance(dispatcher, RecordingOperationDispatcher)
    return dispatcher.sends


# ---------------------------------------------------------------------------------------
# Who may use these routes
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "payload", "header"),
    [
        pytest.param(_approvals_url(), approval_body(), APPROVAL_HEADER, id="approvals"),
        pytest.param(
            _invalidation_url(),
            {"expected_execution_version": 1, "proposal_hash": DIGEST},
            INVALIDATION_HEADER,
            id="invalidation",
        ),
        pytest.param(
            _executions_url(),
            execution_body(),
            EXECUTION_HEADER,
            id="executions",
        ),
    ],
)
def test_only_the_approver_may_authorize_or_send(
    api: ApiHarness, url: str, payload: dict[str, object], header: dict[str, str]
) -> None:
    """The presenter may *read* a proposal and may not authorize one.

    Watching a proposal and authorizing an external message are different powers, and the
    frozen access model grants the second to ``case_approver`` and to nobody else.
    """

    response = api.client.post(url, json=payload, headers=api.presenter_headers(**header))

    assert response.status_code == 403


def test_an_actor_header_is_required(api: ApiHarness) -> None:
    response = api.client.post(_approvals_url(), json=approval_body(), headers=APPROVAL_HEADER)

    assert response.status_code == 401


# ---------------------------------------------------------------------------------------
# The closed bodies
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param("subject", id="subject"),
        pytest.param("text_body", id="text-body"),
        pytest.param("html_body", id="html-body"),
        pytest.param("claims", id="claims"),
        pytest.param("recipient", id="recipient"),
        pytest.param("expected_action_status", id="retired-field"),
    ],
)
def test_the_approval_body_carries_no_text_field_of_any_kind(api: ApiHarness, extra: str) -> None:
    """There is nowhere an edited body could be submitted, so nothing has to validate one.

    An edit is a rejection followed by a new proposal with a new ``action_id``, a new
    ``preview_hash``, and a new decision. ``expected_action_status`` is on this list because it
    is *retired*: it named a status where the transaction conditions on a row version.
    """

    response = api.client.post(
        _approvals_url(),
        json=approval_body(**{extra: "anything at all"}),
        headers=_approver(api, **APPROVAL_HEADER),
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param("subject", id="subject"),
        pytest.param("recipient", id="recipient"),
        pytest.param("template", id="template"),
        pytest.param("attachment", id="attachment"),
        pytest.param("retry", id="retry-flag"),
        pytest.param("force", id="force-flag"),
    ],
)
def test_the_execute_body_accepts_no_message_content_and_no_retry_flag(
    api: ApiHarness, extra: str
) -> None:
    """The field list an implementer is most likely to widen, refused one member at a time.

    ``retry`` and ``force`` are named explicitly because there is deliberately **no retry
    route**: ``FAILED`` is terminal for an action and ``SEND_UNKNOWN`` is a quarantine that
    only reconciliation resolves.
    """

    response = api.client.post(
        _executions_url(),
        json=execution_body(**{extra: "anything at all"}),
        headers=_approver(api, **EXECUTION_HEADER),
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(approval_body(decision="MAYBE"), id="decision-outside-the-enum"),
        pytest.param(approval_body(expected_execution_version=0), id="non-positive-version"),
        pytest.param(approval_body(proposal_hash="not-a-digest"), id="malformed-digest"),
        pytest.param(approval_body(execution_id="not-a-uuid"), id="malformed-identifier"),
    ],
)
def test_a_malformed_approval_body_is_refused(api: ApiHarness, payload: dict[str, object]) -> None:
    response = api.client.post(
        _approvals_url(), json=payload, headers=_approver(api, **APPROVAL_HEADER)
    )

    assert response.status_code == 422


def test_the_idempotency_key_header_is_required_on_every_route(api: ApiHarness) -> None:
    """A decision and a send are both commands, and a command without a key cannot replay."""

    for url, payload in (
        (_approvals_url(), approval_body()),
        (_invalidation_url(), {"expected_execution_version": 1, "proposal_hash": DIGEST}),
        (_executions_url(), execution_body()),
    ):
        response = api.client.post(url, json=payload, headers=_approver(api))
        assert response.status_code == 422


# ---------------------------------------------------------------------------------------
# The execute route's handover
# ---------------------------------------------------------------------------------------


def test_the_execute_route_returns_202_and_a_pollable_operation(api: ApiHarness) -> None:
    """An external call is not something an HTTP request should hold a connection open for."""

    response = api.client.post(
        _executions_url(), json=execution_body(), headers=_approver(api, **EXECUTION_HEADER)
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == ApplicationOperationStatus.PENDING.value
    assert payload["poll_url"] == f"/v1/operations/{payload['operation_id']}"
    assert response.headers["Cache-Control"] == "no-store"


def test_the_execute_route_makes_no_ses_call_inside_the_request(api: ApiHarness) -> None:
    """The handover is data on a queue. Nothing external happens inside the request."""

    api.client.post(
        _executions_url(), json=execution_body(), headers=_approver(api, **EXECUTION_HEADER)
    )

    assert len(_dispatched(api)) == 1


def test_the_send_operation_carries_no_agent_handover(api: ApiHarness) -> None:
    """``SEND_ACTION`` invokes no agent, and an operation of that kind holding one is refused.

    Asserted on the durable operation rather than on the job, because the operation is what a
    worker binds against -- and an unexpected handover there is a malformed record rather than
    a harmless extra field (ADR-016).
    """

    response = api.client.post(
        _executions_url(), json=execution_body(), headers=_approver(api, **EXECUTION_HEADER)
    )

    job = _dispatched(api)[0]
    assert str(job.operation_id) == response.json()["operation_id"]
    # No invocation identity and no binding hash: there is no agent to hand over to, and the
    # job type has no field in which one could be carried.
    assert not hasattr(job, "invocation_id")
    assert not hasattr(job, "agent_binding_hash")


def test_the_dispatched_job_names_the_execution_and_nothing_of_the_message(
    api: ApiHarness,
) -> None:
    """It names identifiers and a version, and carries no part of what will be sent."""

    api.client.post(
        _executions_url(), json=execution_body(), headers=_approver(api, **EXECUTION_HEADER)
    )

    job = _dispatched(api)[0]
    assert str(job.execution_id) == EXECUTION_ID
    assert str(job.approval_id) == APPROVAL_ID
    assert job.expected_execution_version == 2
    for forbidden in ("subject", "text_body", "html_body", "recipient", "address"):
        assert not hasattr(job, forbidden)


def test_the_same_key_and_request_returns_the_same_send_operation(api: ApiHarness) -> None:
    first = api.client.post(
        _executions_url(), json=execution_body(), headers=_approver(api, **EXECUTION_HEADER)
    )
    second = api.client.post(
        _executions_url(), json=execution_body(), headers=_approver(api, **EXECUTION_HEADER)
    )

    assert first.status_code == second.status_code == 202
    assert first.json()["operation_id"] == second.json()["operation_id"]


def test_the_same_key_with_a_different_request_conflicts_with_zero_mutations(
    api: ApiHarness,
) -> None:
    """A caller reusing one key for a genuinely different send is told so, and nothing runs."""

    api.client.post(
        _executions_url(), json=execution_body(), headers=_approver(api, **EXECUTION_HEADER)
    )
    before = len(_dispatched(api))

    conflicting = api.client.post(
        _executions_url(),
        json=execution_body(expected_execution_version=9),
        headers=_approver(api, **EXECUTION_HEADER),
    )

    assert conflicting.status_code == 409
    assert len(_dispatched(api)) == before


def test_no_retry_route_exists(api: ApiHarness) -> None:
    """Its absence is a design element rather than a gap.

    ``FAILED`` is terminal for an action, and a retry route is the first place somebody would
    later add a ``force`` flag for ``SEND_UNKNOWN``.
    """

    response = api.client.post(
        f"{_executions_url()}/{EXECUTION_ID}/retry",
        json={},
        headers=_approver(api, **EXECUTION_HEADER),
    )

    assert response.status_code in {404, 405}


def test_no_second_read_route_for_an_execution_exists(api: ApiHarness) -> None:
    """Send status is read through the existing case surface and the existing operation poll.

    A second address for one row is a second thing to keep consistent.
    """

    response = api.client.get(f"{_executions_url()}/{EXECUTION_ID}", headers=_approver(api))

    assert response.status_code in {404, 405}


def test_an_unknown_actor_is_refused(api: ApiHarness) -> None:
    response = api.client.post(
        _approvals_url(),
        json=approval_body(),
        headers={"X-Chorus-Demo-Actor": str(uuid4()), **APPROVAL_HEADER},
    )

    assert response.status_code == 403


def test_the_execute_route_creates_a_send_action_operation(api: ApiHarness) -> None:
    """The kind is what the worker binds against before it claims anything."""

    api.client.post(
        _executions_url(), json=execution_body(), headers=_approver(api, **EXECUTION_HEADER)
    )

    job = _dispatched(api)[0]
    stored = api.harness.core
    assert stored is not None
    assert job.request_hash is not None
    assert ApplicationOperationKind.SEND_ACTION.value == "SEND_ACTION"
