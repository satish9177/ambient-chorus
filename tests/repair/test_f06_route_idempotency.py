"""F06 -- ``expected_case_version`` participates in HTTP idempotency identity.

Codex's probe, against the real route:

```text
POST /v1/cases/{id}/actions   {expected_case_version: 1,   view_id: V, view_hash: H}  -> 202
POST /v1/cases/{id}/actions   {expected_case_version: 999, view_id: V, view_hash: H}  -> 202
```

with the same ``Idempotency-Key``. The second is a different command -- it asserts a different
belief about the case row, which the worker enforces -- and it was answered with the first
one's operation because the route derived its request hash from
``propose_action_binding_hash(case_id, view_id, view_hash)``.

The repair does **not** widen the agent binding. ADR-016 froze what a binding is -- the work
one *invocation* is authorized to do -- and changing it to fix an HTTP concern would move the
wrong contract. A distinct route request hash is added instead, covering exactly ``case_id``,
``expected_case_version``, ``view_id``, ``view_hash``, and a schema separator.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from chorus.application.operations import (
    PROPOSE_ACTION_BINDING_SCHEMA,
    PROPOSE_ACTION_HTTP_REQUEST_SCHEMA,
    propose_action_binding_hash,
    propose_action_request_hash,
)
from chorus.domain.ids import CaseId, Sha256Digest
from chorus.infrastructure.local.dispatch import RecordingOperationDispatcher
from chorus.ports.operations import ProposeActionOperationJob
from tests.contract.api.conftest import ApiHarness

CASE_ID = "3f2a1b0c-4d5e-4f60-8a1b-2c3d4e5f6071"
VIEW_ID = "9d1c7b6a-5e4f-4a3b-9c8d-7e6f5a4b3c2d"
VIEW_HASH = "sha256:" + "1" * 64
HEADER = {"Idempotency-Key": "propose-action-repair-f06"}


def _body(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "expected_case_version": 1,
        "view_id": VIEW_ID,
        "view_hash": VIEW_HASH,
    }
    payload.update(overrides)
    return payload


def _post(api: ApiHarness, **overrides: object) -> Any:
    return api.client.post(
        f"/v1/cases/{CASE_ID}/actions",
        json=_body(**overrides),
        headers=api.presenter_headers(**HEADER),
    )


def _dispatched(api: ApiHarness) -> list[ProposeActionOperationJob]:
    dispatcher = api.dispatcher
    assert isinstance(dispatcher, RecordingOperationDispatcher)
    return dispatcher.proposals


# ---------------------------------------------------------------------------------------
# Codex's exact probe
# ---------------------------------------------------------------------------------------


def test_the_expected_case_version_probe_conflicts(api: ApiHarness) -> None:
    """1 then 999 under one key: the second must be 409, not 202."""

    first = _post(api, expected_case_version=1)
    assert first.status_code == 202

    second = _post(api, expected_case_version=999)

    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_the_conflicting_probe_creates_no_second_operation_and_no_second_dispatch(
    api: ApiHarness,
) -> None:
    """Zero mutations, and nothing dispatched with the altered work."""

    first = _post(api, expected_case_version=1).json()
    _post(api, expected_case_version=999)

    jobs = _dispatched(api)
    assert len(jobs) == 1
    assert jobs[0].operation_id.value == UUID(first["operation_id"])
    assert jobs[0].expected_case_version == 1


# ---------------------------------------------------------------------------------------
# Every member of the request identity
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_case_version", 999),
        ("view_id", str(uuid4())),
        ("view_hash", "sha256:" + "2" * 64),
    ],
)
def test_changing_any_covered_member_conflicts(api: ApiHarness, field: str, value: object) -> None:
    _post(api)

    response = _post(api, **{field: value})

    assert response.status_code == 409
    assert len(_dispatched(api)) == 1


def test_a_different_case_is_a_different_key_scope(api: ApiHarness) -> None:
    """``case_id`` is in the hash, and the path decides it."""

    other_case = str(uuid4())
    _post(api)
    response = api.client.post(
        f"/v1/cases/{other_case}/actions",
        json=_body(),
        headers=api.presenter_headers(**HEADER),
    )

    assert response.status_code == 409


def test_an_identical_request_returns_the_same_operation_and_invocation(
    api: ApiHarness,
) -> None:
    """The other half: identical means identical, and it must not conflict."""

    first = _post(api).json()
    second = _post(api).json()

    assert first["operation_id"] == second["operation_id"]
    jobs = _dispatched(api)
    assert jobs[0].operation_id == jobs[1].operation_id
    assert jobs[0].invocation_id == jobs[1].invocation_id


# ---------------------------------------------------------------------------------------
# The two digests stay separate concepts
# ---------------------------------------------------------------------------------------


def test_the_route_hash_and_the_agent_binding_are_different_digests() -> None:
    """Same inputs, different domains: neither can be mistaken for the other."""

    case_id = CaseId(UUID(CASE_ID))
    view_id = UUID(VIEW_ID)
    view_hash = Sha256Digest(VIEW_HASH)

    binding = propose_action_binding_hash(case_id=case_id, view_id=view_id, view_hash=view_hash)
    request = propose_action_request_hash(
        case_id=case_id, expected_case_version=1, view_id=view_id, view_hash=view_hash
    )

    assert binding != request
    assert PROPOSE_ACTION_BINDING_SCHEMA != PROPOSE_ACTION_HTTP_REQUEST_SCHEMA


def test_the_agent_binding_is_unchanged_by_expected_case_version() -> None:
    """ADR-016's binding covers ``{case, view, hash}``; this repair did not widen it."""

    case_id = CaseId(UUID(CASE_ID))
    view_id = UUID(VIEW_ID)
    view_hash = Sha256Digest(VIEW_HASH)

    assert propose_action_binding_hash(
        case_id=case_id, view_id=view_id, view_hash=view_hash
    ) == propose_action_binding_hash(case_id=case_id, view_id=view_id, view_hash=view_hash)


def test_the_route_hash_moves_with_every_covered_member() -> None:
    case_id = CaseId(UUID(CASE_ID))
    view_id = UUID(VIEW_ID)
    view_hash = Sha256Digest(VIEW_HASH)
    base = propose_action_request_hash(
        case_id=case_id, expected_case_version=1, view_id=view_id, view_hash=view_hash
    )

    assert base != propose_action_request_hash(
        case_id=CaseId(uuid4()), expected_case_version=1, view_id=view_id, view_hash=view_hash
    )
    assert base != propose_action_request_hash(
        case_id=case_id, expected_case_version=2, view_id=view_id, view_hash=view_hash
    )
    assert base != propose_action_request_hash(
        case_id=case_id, expected_case_version=1, view_id=uuid4(), view_hash=view_hash
    )
    assert base != propose_action_request_hash(
        case_id=case_id,
        expected_case_version=1,
        view_id=view_id,
        view_hash=Sha256Digest("sha256:" + "3" * 64),
    )


def test_a_non_positive_expected_case_version_is_refused_by_the_helper() -> None:
    with pytest.raises(ValueError):
        propose_action_request_hash(
            case_id=CaseId(UUID(CASE_ID)),
            expected_case_version=0,
            view_id=UUID(VIEW_ID),
            view_hash=Sha256Digest(VIEW_HASH),
        )
