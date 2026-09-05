"""The compile transport: synchronous, presenter-only, and closed at the body.

What this file proves is transport behaviour, not policy. The compiler's answers are tested
through the application in the compile contract suite; here the questions are whether the route
is synchronous, whether it lets anything through the body that the server should be deciding,
and whether the frozen status mapping is what a caller actually sees.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.contract.api.conftest import ApiHarness
from tests.fixtures.compile import photo_bytes

from chorus.domain.entities import ApplicationOperationKind

pytestmark = pytest.mark.anyio

PATH = "/v1/cases/{case_id}/views"


def _body(harness: ApiHarness, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "compile_id": str(uuid4()),
        "expected_case_version": 1,
        "requested_facts": [
            {
                "fact_id": str(uuid4()),
                "necessity": "OPTIONAL",
                "intended_usage": "CLAIM",
            }
        ],
        "requested_evidence_ids": [],
        "purpose": "REQUEST_ELEVATOR_REPAIR_AND_RESPONSE",
    }
    body.update(overrides)
    return body


def test_the_route_is_synchronous_and_mints_no_operation_kind() -> None:
    """Compile calls no model and holds nothing open, so there is nothing to poll."""

    assert "COMPILE" not in {kind.value for kind in ApplicationOperationKind}


async def test_a_resident_cannot_compile(api: ApiHarness) -> None:
    """The compile surface is presenter-only; a resident decides mandates, not disclosures."""

    response = api.client.post(
        PATH.format(case_id=uuid4()),
        json=_body(api),
        headers=api.actor_headers("resident_a", **{"Idempotency-Key": "compile-key-0001"}),
    )

    assert response.status_code == 403


async def test_an_unknown_body_field_is_refused_without_echoing_it(api: ApiHarness) -> None:
    """``extra='forbid'`` plus the safe validation handler: the field name is never echoed."""

    response = api.client.post(
        PATH.format(case_id=uuid4()),
        json=_body(api, motherLeelaAsthma4B="x"),
        headers=api.actor_headers("presenter_admin", **{"Idempotency-Key": "compile-key-0001"}),
    )

    assert response.status_code == 422
    assert "motherLeelaAsthma4B" not in response.text


async def test_a_caller_cannot_name_its_own_destination(api: ApiHarness) -> None:
    """The destination is deployment configuration, never a request field."""

    response = api.client.post(
        PATH.format(case_id=uuid4()),
        json=_body(api, destination={"destination_id": "property_manager:attacker"}),
        headers=api.actor_headers("presenter_admin", **{"Idempotency-Key": "compile-key-0001"}),
    )

    assert response.status_code == 422
    assert "attacker" not in response.text


async def test_a_missing_idempotency_key_is_refused(api: ApiHarness) -> None:
    response = api.client.post(
        PATH.format(case_id=uuid4()),
        json=_body(api),
        headers=api.actor_headers("presenter_admin"),
    )

    assert response.status_code == 422


async def test_an_unknown_purpose_is_refused_at_the_boundary(api: ApiHarness) -> None:
    """policy/v1 has one purpose, and the transport literal is where that is enforced first."""

    response = api.client.post(
        PATH.format(case_id=uuid4()),
        json=_body(api, purpose="EXFILTRATE_EVERYTHING"),
        headers=api.actor_headers("presenter_admin", **{"Idempotency-Key": "compile-key-0001"}),
    )

    assert response.status_code == 422


async def test_an_absent_case_answers_not_found_without_enumerating(api: ApiHarness) -> None:
    """A case that does not exist and one that is not yours give the same answer."""

    response = api.client.post(
        PATH.format(case_id=uuid4()),
        json=_body(api),
        headers=api.actor_headers("presenter_admin", **{"Idempotency-Key": "compile-key-0001"}),
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_a_policy_denial_is_a_422_problem_with_reason_codes(
    api: ApiHarness,
) -> None:
    """The frozen mapping, unchanged: a deterministic refusal is 422 ``POLICY_DENIED``."""

    from tests.fixtures.compile import CompileHarness

    compile_harness = CompileHarness(driver=api.harness.driver)
    raw = photo_bytes()
    await compile_harness.seed(evidence_items=compile_harness.align_photo_digest(raw), photo=raw)
    api.bind_compile(compile_harness)

    response = api.client.post(
        PATH.format(case_id=str(compile_harness.case.case_id)),
        json=_body(
            api,
            expected_case_version=compile_harness.case.version + 9,
            requested_facts=[
                {
                    "fact_id": str(compile_harness.fixture.incident_fact_ids[0]),
                    "necessity": "OPTIONAL",
                    "intended_usage": "CLAIM",
                }
            ],
        ),
        headers=api.actor_headers("presenter_admin", **{"Idempotency-Key": "compile-key-deny"}),
    )

    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "POLICY_DENIED"
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_an_allowed_compile_returns_the_persisted_view(api: ApiHarness) -> None:
    """The transport half of scenario A: 200, a body, and no private value in it."""

    from tests.fixtures.compile import SENTINEL_PATTERN, CompileHarness

    compile_harness = CompileHarness(driver=api.harness.driver)
    raw = photo_bytes()
    await compile_harness.seed(evidence_items=compile_harness.align_photo_digest(raw), photo=raw)
    api.bind_compile(compile_harness)

    response = api.client.post(
        PATH.format(case_id=str(compile_harness.case.case_id)),
        json=_body(
            api,
            expected_case_version=compile_harness.case.version,
            requested_facts=[
                {
                    "fact_id": str(fact_id),
                    "necessity": "OPTIONAL",
                    "intended_usage": "CLAIM",
                }
                for fact_id in compile_harness.fixture.incident_fact_ids
            ],
        ),
        headers=api.actor_headers("presenter_admin", **{"Idempotency-Key": "compile-key-allow"}),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["decision"] == "ALLOW"
    assert payload["replayed"] is False
    assert payload["view"]["policy_version"] == "policy/v1"
    assert payload["view"]["view_hash"].startswith("sha256:")
    assert response.headers["cache-control"] == "no-store"
    assert SENTINEL_PATTERN.search(response.text) is None
