"""Targeted negative tests for the Phase 10 backend-support unblock.

Each test below corresponds to one letter in the frozen "required negative tests" list:

A. local composition refuses production environment
B. GET case does not leak FactValue
C. case_approver cannot receive presenter-only private sections
D. investigation endpoint rejects unauthorized persona
E. audit omits actor_id_hash/idempotency_key_hash/raw payload
F. session cannot enumerate other contributors
G. reset twice produces deterministic identifiers and clean state
H. Phase 9 routes no longer return 503 in local composition
I. execution.version is returned and usable for the next mutation
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.smoke.conftest import APPROVER, PRESENTER, actor

from chorus.composition.demo_reset import PERSONA_BY_PSEUDONYM
from chorus.composition.local import LocalComposition
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.settings import Environment, Settings

pytestmark = pytest.mark.anyio


# -- A. local composition refuses production environment -------------------------------------


def test_a_local_composition_refuses_outside_test_development_demo() -> None:
    # Settings itself refuses to construct DEMO with a fake agent mode (see
    # chorus.settings.Settings.validate_environment_contract), so the environment this test
    # must exercise the refusal for is one Settings *will* construct but the composition root
    # must still not build against: there is no fourth value in the enum, so the guard is
    # exercised directly against the function's own contract instead.
    from chorus.composition.local import ALLOWED_ENVIRONMENTS

    assert {
        Environment.TEST,
        Environment.DEVELOPMENT,
        Environment.DEMO,
    } == ALLOWED_ENVIRONMENTS


def test_a_build_local_container_refuses_a_settings_object_claiming_an_unlisted_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from chorus.composition import local as local_module

    settings = Settings(environment=Environment.DEVELOPMENT, local_data_dir=tmp_path)
    monkeypatch.setattr(local_module, "ALLOWED_ENVIRONMENTS", frozenset())
    with pytest.raises(RuntimeError, match="refuses to build"):
        local_module.build_local(settings, storage=InMemoryStorageDriver())


# -- B/C/D/E/F/G/H/I: exercised against one seeded, live case -------------------------------


async def _seeded_case(client: TestClient, composition: LocalComposition) -> str:
    """Reset, ingest, and discover the candidate case -- the minimum shared state."""

    client.post(
        "/v1/demo/reset",
        headers={**PRESENTER, "Idempotency-Key": "neg-reset-0001"},
        json={"namespace": "DEMO", "confirm": "RESET DEMO", "seed_version": "elevator/v1"},
    )
    corpus = composition.adapter.messages()
    ids_by_pseudonym = composition.adapter.contributor_ids_by_pseudonym
    ingest_response = client.post(
        "/v1/ingest/messages",
        headers={**PRESENTER, "Idempotency-Key": "neg-ingest-0001"},
        json={
            "community_id": str(composition.adapter.community.community_id),
            "messages": [
                {
                    "adapter": "SYNTHETIC",
                    "channel_message_id": m.channel_message_id,
                    "contributor_id": (
                        None
                        if m.contributor_pseudonym is None
                        else str(ids_by_pseudonym[m.contributor_pseudonym])
                    ),
                    "sent_at": m.sent_at.isoformat(),
                    "text": m.text,
                    "attachments": [
                        {
                            "evidence_id": str(a.evidence_id),
                            "media_type": a.media_type,
                            "byte_length": a.byte_length,
                            "sha256": a.sha256.value,
                        }
                        for a in m.attachments
                    ],
                }
                for m in corpus
            ],
        },
    )
    await composition.dispatcher.drain()
    op_id = ingest_response.json()["operation"]["operation_id"]
    client.get(f"/v1/operations/{op_id}", headers=PRESENTER)
    feed = client.get(
        "/v1/feed",
        headers=PRESENTER,
        params={"community_id": str(composition.adapter.community.community_id), "limit": 50},
    ).json()
    signal = next(item["chorus_signal"] for item in feed["items"] if item["chorus_signal"])
    return str(signal["candidate_case_id"])


async def test_b_get_case_does_not_leak_fact_value(
    client: TestClient, composition: LocalComposition
) -> None:
    case_id = await _seeded_case(client, composition)
    response = client.get(f"/v1/cases/{case_id}", headers=PRESENTER)
    assert response.status_code == 200, response.text
    body = response.json()
    # `evidence_summary` rows carry identifiers, a type, a sensitivity, and a status -- never
    # the fact's own value. There is no `value` or `value_preview` key anywhere in this surface.
    for row in body["evidence_summary"]:
        assert set(row) == {
            "fact_id",
            "fact_type",
            "sensitivity",
            "evidence_status",
            "status",
            "contributor_id",
            "evidence_ids",
            "version",
        }
    assert "value_preview" not in response.text
    assert "asthma" not in response.text
    assert "Leela" not in response.text


async def test_c_case_approver_receives_no_presenter_only_section(
    client: TestClient, composition: LocalComposition
) -> None:
    case_id = await _seeded_case(client, composition)
    response = client.get(f"/v1/cases/{case_id}", headers=APPROVER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["case"] is None or body["case"]["title"] is None
    assert body["evidence_summary"] is None
    assert body["privacy_counts"] is None
    # The approver may still read the safe, view/action-safe sections.
    assert "current_action" in body
    assert "commitments" in body


async def test_d_investigation_rejects_unauthorized_persona(
    client: TestClient, composition: LocalComposition
) -> None:
    case_id = await _seeded_case(client, composition)
    for headers in (APPROVER, actor("resident_a")):
        response = client.get(f"/v1/cases/{case_id}/investigation", headers=headers)
        assert response.status_code == 403, response.text
    # The presenter may.
    ok = client.get(f"/v1/cases/{case_id}/investigation", headers=PRESENTER)
    assert ok.status_code == 200, ok.text


async def test_e_audit_omits_actor_and_idempotency_hashes_and_raw_payload(
    client: TestClient, composition: LocalComposition
) -> None:
    case_id = await _seeded_case(client, composition)
    response = client.get(f"/v1/cases/{case_id}/audit", headers=PRESENTER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"], "the case should already carry audit events by this point"
    for item in body["items"]:
        assert "actor_id_hash" not in item
        assert "idempotency_key_hash" not in item
        assert set(item["safe_details"]) == {"count", "rule_id"}
    assert "actor_id_hash" not in response.text
    assert "idempotency_key_hash" not in response.text
    # Only a presenter may reach it.
    denied = client.get(f"/v1/cases/{case_id}/audit", headers=APPROVER)
    assert denied.status_code == 403, denied.text


async def test_f_session_cannot_enumerate_other_contributors(
    client: TestClient, composition: LocalComposition
) -> None:
    await _seeded_case(client, composition)
    resident_a = client.get("/v1/session", headers=actor("resident_a")).json()
    resident_b = client.get("/v1/session", headers=actor("resident_b")).json()
    assert resident_a["contributor_id"] != resident_b["contributor_id"]
    # Nothing in either persona's own session response names the other contributor's identity,
    # and the session shape carries no field that could: there is exactly one contributor_id.
    assert set(resident_a) == {
        "actor",
        "contributor_id",
        "community_id",
        "namespace",
        "capabilities",
    }
    assert resident_a["contributor_id"] not in str(resident_b)
    presenter_session = client.get("/v1/session", headers=PRESENTER).json()
    assert presenter_session["contributor_id"] is None


async def test_g_reset_twice_is_deterministic_and_clean(
    client: TestClient, composition: LocalComposition
) -> None:
    first = client.post(
        "/v1/demo/reset",
        headers={**PRESENTER, "Idempotency-Key": "neg-reset-g-0001"},
        json={"namespace": "DEMO", "confirm": "RESET DEMO", "seed_version": "elevator/v1"},
    ).json()
    second = client.post(
        "/v1/demo/reset",
        headers={**PRESENTER, "Idempotency-Key": "neg-reset-g-0002"},
        json={"namespace": "DEMO", "confirm": "RESET DEMO", "seed_version": "elevator/v1"},
    ).json()
    assert first["community_id"] == second["community_id"]
    assert first["corpus_sha256"] == second["corpus_sha256"]
    assert first["contributors"] == second["contributors"]
    assert first["evidence"] == second["evidence"]
    # Every deterministic seed count is identical run to run; only `deleted` differs, because
    # the second reset (a fresh key) genuinely cleans the first run's seed before re-seeding
    # it, while the first ran against an empty namespace.
    for counts in (first["counts"], second["counts"]):
        assert counts["messages"] == 24
        assert counts["contributors"] == 4
        assert counts["evidence"] == 2
    assert first["counts"]["deleted"] == 0
    assert second["counts"]["deleted"] > 0
    assert second["replayed"] is False
    # Reset alone -- with no ingest in between -- creates no case: the feed carries messages
    # but discovers no candidate case yet.
    feed = client.get(
        "/v1/feed",
        headers=PRESENTER,
        params={"community_id": first["community_id"], "limit": 50},
    ).json()
    assert all(item["chorus_signal"] is None for item in feed["items"])


async def _decide_all_mandates(
    client: TestClient, composition: LocalComposition, case_id: str
) -> None:
    """Approve every contributor's mandate, each as the resident persona who owns it."""

    ids_by_pseudonym = composition.adapter.contributor_ids_by_pseudonym
    actor_by_contributor = {
        str(ids_by_pseudonym[pseudonym]): persona
        for pseudonym, persona in PERSONA_BY_PSEUDONYM.items()
    }
    case_version = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()["case"]["version"]
    propose = client.post(
        f"/v1/cases/{case_id}/mandates",
        headers={**PRESENTER, "Idempotency-Key": "neg-mandates-0001"},
        json={"expected_case_version": case_version},
    ).json()
    for row in propose["proposals"]:
        persona = actor_by_contributor[row["contributor_id"]]
        thread = client.get(
            f"/v1/contributors/{row['contributor_id']}/mandates/current",
            headers=actor(persona),
            params={"case_id": case_id},
        ).json()
        decision = client.post(
            f"/v1/cases/{case_id}/mandates/{row['mandate_id']}/decisions",
            headers={**actor(persona), "Idempotency-Key": f"neg-decide-{row['mandate_id']}"},
            json={
                "expected_version": row["version"],
                "decision": "APPROVE",
                "fact_grants": [
                    {
                        "fact_id": f["fact_id"],
                        "max_scope": f["proposed_scope"],
                        "allow_safe_transformation": f["allow_safe_transformation"],
                    }
                    for f in thread["fact_permissions"]
                ],
                "identity_grant": {
                    "externally_shareable": thread["identity_permission"]["externally_shareable"],
                    "max_scope": thread["identity_permission"]["max_scope"],
                },
            },
        )
        assert decision.status_code == 200, decision.text


async def _case_with_current_action(
    client: TestClient, composition: LocalComposition
) -> tuple[str, dict[str, Any]]:
    """Reset, discover, decide every mandate, investigate, compile, and propose one action."""

    case_id = await _seeded_case(client, composition)
    await _decide_all_mandates(client, composition, case_id)

    case = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    client.post(
        f"/v1/cases/{case_id}/investigations",
        headers={**PRESENTER, "Idempotency-Key": "neg-investigate-0001"},
        json={"expected_case_version": case["case"]["version"], "reason": "INITIAL"},
    )
    await composition.dispatcher.drain()

    ready = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    facts_by_type: dict[str, list[dict[str, Any]]] = {}
    for row in ready["evidence_summary"]:
        facts_by_type.setdefault(row["fact_type"], []).append(row)
    # Only the incident facts are required (P2-8 gives this case real private facts too --
    # HEALTH_DETAIL/UNIT_LOCATION/IDENTITY_ATTRIBUTE default to INTERNAL_ONLY, and marking them
    # REQUIRED would turn their exclusion into a whole-request POLICY_DENIED instead of the
    # ordinary partial exclusion this fixture's callers expect).
    requested_facts = [
        {
            "fact_id": row["fact_id"],
            "necessity": "REQUIRED" if fact_type == "INCIDENT_OCCURRENCE" else "OPTIONAL",
            "intended_usage": "CLAIM"
            if fact_type == "INCIDENT_OCCURRENCE"
            else "AGGREGATION_INPUT",
        }
        for fact_type, rows in facts_by_type.items()
        for row in rows
    ]
    compiled = client.post(
        f"/v1/cases/{case_id}/views",
        headers={**PRESENTER, "Idempotency-Key": "neg-compile-0001"},
        json={
            "compile_id": "22222222-2222-4222-8222-222222222222",
            "expected_case_version": ready["case"]["version"],
            "requested_facts": requested_facts,
            "requested_evidence_ids": [],
            "purpose": "REQUEST_ELEVATOR_REPAIR_AND_RESPONSE",
        },
    )
    assert compiled.status_code == 200, compiled.text
    compiled_body = compiled.json()
    propose_action = client.post(
        f"/v1/cases/{case_id}/actions",
        headers={**PRESENTER, "Idempotency-Key": "neg-propose-action-0001"},
        json={
            "expected_case_version": ready["case"]["version"],
            "view_id": compiled_body["view"]["view_id"],
            "view_hash": compiled_body["view"]["view_hash"],
        },
    )
    assert propose_action.status_code == 202, propose_action.text
    await composition.dispatcher.drain()

    proposed = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    current_action = proposed["current_action"]
    assert current_action is not None
    return case_id, current_action


async def test_h_phase9_routes_no_longer_503_in_local_composition(
    client: TestClient, composition: LocalComposition
) -> None:
    case_id, current_action = await _case_with_current_action(client, composition)
    approve = client.post(
        f"/v1/cases/{case_id}/actions/{current_action['action_id']}/approvals",
        headers={**APPROVER, "Idempotency-Key": "neg-h-approve-0001"},
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
        headers={**APPROVER, "Idempotency-Key": "neg-h-execute-0001"},
        json={
            "execution_id": approve["execution_id"],
            "expected_execution_version": approve["execution_version"],
            "approval_id": approve["approval_id"],
        },
    )
    assert execute.status_code == 202, execute.text
    await composition.dispatcher.drain()

    # POST /demo/external-replies: no longer 503 -- the surface is wired.
    reply = client.post(
        "/v1/demo/external-replies",
        headers={**PRESENTER, "Idempotency-Key": "neg-h-reply-0001"},
        json={"fixture_id": "manager-promise"},
    )
    assert reply.status_code != 503, reply.text
    assert reply.status_code == 202, reply.text
    await composition.dispatcher.drain()
    reply_op = client.get(
        f"/v1/operations/{reply.json()['operation_id']}", headers=PRESENTER
    ).json()
    assert reply_op["status"] == "SUCCEEDED", reply_op
    commitment_id = reply_op["result_refs"][0]

    # POST /demo/clock/advance: no longer 503.
    clock = client.post(
        "/v1/demo/clock/advance",
        headers=PRESENTER,
        json={
            "case_id": case_id,
            "commitment_id": commitment_id,
            "advance_seconds": 40 * 24 * 3600,
        },
    )
    assert clock.status_code != 503, clock.text
    assert clock.status_code == 200, clock.text

    # POST .../verification: no longer 503.
    due = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    due_row = next(c for c in due["commitments"] if c["commitment_id"] == commitment_id)
    ids_by_pseudonym = composition.adapter.contributor_ids_by_pseudonym
    affected_actor = None
    for row in due["evidence_summary"]:
        for pseudonym, persona in PERSONA_BY_PSEUDONYM.items():
            if str(ids_by_pseudonym[pseudonym]) == row["contributor_id"]:
                affected_actor = persona
                break
        if affected_actor:
            break
    assert affected_actor is not None
    verify = client.post(
        f"/v1/cases/{case_id}/commitments/{commitment_id}/verification",
        headers={**actor(affected_actor), "Idempotency-Key": "neg-h-verify-0001"},
        json={"expected_version": due_row["version"], "outcome": "MISSED"},
    )
    assert verify.status_code != 503, verify.text
    assert verify.status_code == 200, verify.text


async def test_i_execution_version_is_returned_and_usable(
    client: TestClient, composition: LocalComposition
) -> None:
    case_id, current_action = await _case_with_current_action(client, composition)
    # This is the field under test: it is present, and it is exactly what
    # `expected_execution_version` needs for the very next mutation.
    execution_version = current_action["execution"]["version"]
    assert isinstance(execution_version, int) and execution_version >= 1

    approve = client.post(
        f"/v1/cases/{case_id}/actions/{current_action['action_id']}/approvals",
        headers={**APPROVER, "Idempotency-Key": "neg-i-approve-0001"},
        json={
            "decision": "APPROVED",
            "expected_execution_version": execution_version,
            "execution_id": current_action["execution"]["execution_id"],
            "view_hash": current_action["view_hash"],
            "proposal_hash": current_action["proposal_hash"],
            "preview_hash": current_action["preview"]["preview_hash"],
        },
    )
    assert approve.status_code == 200, approve.text
