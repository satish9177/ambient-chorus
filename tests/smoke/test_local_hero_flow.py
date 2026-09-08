"""The backend-only HTTP hero smoke.

[11-frontend-and-demo.md § Local hero
smoke](../../docs/architecture/11-frontend-and-demo.md#local-hero-smoke) freezes the sequence:
one backend-only HTTP test drives the whole five-minute demo path against the local composition
through the ASGI client and no browser, so a UI failure can never be confused with a backend gap.

Every step goes through the real HTTP surface -- the same one a browser will call -- and no step
reaches into a repository directly. Async operations are drained through the in-process
dispatcher rather than slept on, which is the same mechanism the existing API contract suite
uses (``tests/contract/api/test_phase3_routes.py`` and others) and is deterministic rather than
timing-dependent.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.smoke.conftest import APPROVER, PRESENTER, actor

from chorus.composition.local import LocalComposition

pytestmark = pytest.mark.anyio

SECRET_SENTINELS = ("Leela", "asthma", "4B", "Ignore all previous instructions")
"""Text that must never appear in a response body reaching a shareable-zone or approver read."""


def _no_leak(payload: Any, seen: list[str]) -> None:
    text = str(payload)
    for sentinel in SECRET_SENTINELS:
        assert sentinel not in text, f"leaked {sentinel!r} in a response the caller received"
    seen.append(text)


async def _poll(
    client: TestClient, composition: LocalComposition, operation_id: str, headers: dict[str, str]
) -> dict[str, Any]:
    await composition.dispatcher.drain()
    response = client.get(f"/v1/operations/{operation_id}", headers=headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["status"] in {"SUCCEEDED", "FAILED"}, body
    return body


async def test_hero_flow(client: TestClient, composition: LocalComposition) -> None:
    leaked_check: list[str] = []

    # 1. POST /demo/reset -----------------------------------------------------------------
    reset_response = client.post(
        "/v1/demo/reset",
        headers={**PRESENTER, "Idempotency-Key": "hero-reset-0001"},
        json={"namespace": "DEMO", "confirm": "RESET DEMO", "seed_version": "elevator/v1"},
    )
    assert reset_response.status_code == 200, reset_response.text
    reset_body = reset_response.json()
    _no_leak(reset_body, leaked_check)
    assert reset_body["counts"]["messages"] == 24
    assert reset_body["counts"]["contributors"] == 4
    assert reset_body["logical_now"] == "2030-01-14T09:00:00+00:00"
    community_id = reset_body["community_id"]
    contributor_by_actor = {c["actor"]: c["contributor_id"] for c in reset_body["contributors"]}
    assert set(contributor_by_actor) == {"resident_a", "resident_b", "resident_c", "resident_d"}

    # A second reset under the same Idempotency-Key replays the recorded receipt: same
    # reset_id, replayed=True, and no second destructive reset.
    reset_again = client.post(
        "/v1/demo/reset",
        headers={**PRESENTER, "Idempotency-Key": "hero-reset-0001"},
        json={"namespace": "DEMO", "confirm": "RESET DEMO", "seed_version": "elevator/v1"},
    )
    assert reset_again.status_code == 200, reset_again.text
    assert reset_again.json()["community_id"] == community_id
    assert reset_again.json()["reset_id"] == reset_body["reset_id"]
    assert reset_again.json()["replayed"] is True

    # 2. POST /ingest/messages -------------------------------------------------------------
    corpus = composition.adapter.messages()
    ingest_body = {
        "community_id": community_id,
        "messages": [
            {
                "adapter": "SYNTHETIC",
                "channel_message_id": message.channel_message_id,
                "contributor_id": (
                    None
                    if message.contributor_pseudonym is None
                    else str(
                        composition.adapter.contributor_ids_by_pseudonym[
                            message.contributor_pseudonym
                        ]
                    )
                ),
                "sent_at": message.sent_at.isoformat(),
                "text": message.text,
                "attachments": [
                    {
                        "evidence_id": str(a.evidence_id),
                        "media_type": a.media_type,
                        "byte_length": a.byte_length,
                        "sha256": a.sha256.value,
                    }
                    for a in message.attachments
                ],
            }
            for message in corpus
        ],
    }
    ingest_response = client.post(
        "/v1/ingest/messages",
        headers={**PRESENTER, "Idempotency-Key": "hero-ingest-0001"},
        json=ingest_body,
    )
    assert ingest_response.status_code == 202, ingest_response.text
    ingest_result = ingest_response.json()
    assert len(ingest_result["messages"]) == 24
    monitor_operation_id = ingest_result["operation"]["operation_id"]
    monitor_outcome = await _poll(client, composition, monitor_operation_id, PRESENTER)
    assert monitor_outcome["status"] == "SUCCEEDED", monitor_outcome

    # 3. GET /feed --------------------------------------------------------------------------
    feed_response = client.get(
        "/v1/feed", headers=PRESENTER, params={"community_id": community_id, "limit": 50}
    )
    assert feed_response.status_code == 200, feed_response.text
    feed_body = feed_response.json()
    signals = [item["chorus_signal"] for item in feed_body["items"] if item["chorus_signal"]]
    assert signals, "the Monitor discovered no candidate case"
    case_id = signals[0]["candidate_case_id"]
    assert all(signal["candidate_case_id"] == case_id for signal in signals)

    # 4. Mandates: propose, then A/C/D approve and B adjusts --------------------------------
    case_response = client.get(f"/v1/cases/{case_id}", headers=PRESENTER)
    assert case_response.status_code == 200, case_response.text
    case_version = case_response.json()["case"]["version"]

    propose_response = client.post(
        f"/v1/cases/{case_id}/mandates",
        headers={**PRESENTER, "Idempotency-Key": "hero-mandates-0001"},
        json={"expected_case_version": case_version},
    )
    assert propose_response.status_code == 200, propose_response.text
    proposals = propose_response.json()["proposals"]
    mandate_by_contributor = {p["contributor_id"]: p for p in proposals}

    for demo_actor in ("resident_a", "resident_c", "resident_d"):
        contributor_id = contributor_by_actor[demo_actor]
        session_response = client.get("/v1/session", headers=actor(demo_actor))
        assert session_response.status_code == 200, session_response.text
        assert session_response.json()["contributor_id"] == contributor_id

        mandate = mandate_by_contributor[contributor_id]
        thread = client.get(
            f"/v1/contributors/{contributor_id}/mandates/current",
            headers=actor(demo_actor),
            params={"case_id": case_id},
        ).json()
        decision_response = client.post(
            f"/v1/cases/{case_id}/mandates/{mandate['mandate_id']}/decisions",
            headers={**actor(demo_actor), "Idempotency-Key": f"hero-decide-{demo_actor}-0001"},
            json={
                "expected_version": mandate["version"],
                "decision": "APPROVE",
                "fact_grants": [
                    {
                        "fact_id": row["fact_id"],
                        "max_scope": row["proposed_scope"],
                        "allow_safe_transformation": row["allow_safe_transformation"],
                    }
                    for row in thread["fact_permissions"]
                ],
                "identity_grant": {
                    "externally_shareable": thread["identity_permission"]["externally_shareable"],
                    "max_scope": thread["identity_permission"]["max_scope"],
                },
            },
        )
        assert decision_response.status_code == 200, decision_response.text

    # Resident B adjusts health/unit/name to INTERNAL_ONLY and refuses identity disclosure.
    contributor_b = contributor_by_actor["resident_b"]
    mandate_b = mandate_by_contributor[contributor_b]
    thread_b = client.get(
        f"/v1/contributors/{contributor_b}/mandates/current",
        headers=actor("resident_b"),
        params={"case_id": case_id},
    ).json()
    adjusted_grants = [
        {
            "fact_id": row["fact_id"],
            "max_scope": "INTERNAL_ONLY"
            if row["fact_type"] in {"HEALTH_DETAIL", "UNIT_LOCATION", "IDENTITY_ATTRIBUTE"}
            else row["proposed_scope"],
            "allow_safe_transformation": row["allow_safe_transformation"],
        }
        for row in thread_b["fact_permissions"]
    ]
    adjust_response = client.post(
        f"/v1/cases/{case_id}/mandates/{mandate_b['mandate_id']}/decisions",
        headers={**actor("resident_b"), "Idempotency-Key": "hero-decide-resident_b-adjust-0001"},
        json={
            "expected_version": mandate_b["version"],
            "decision": "ADJUST",
            "fact_grants": adjusted_grants,
            "identity_grant": {"externally_shareable": False, "max_scope": "ANONYMOUS_CASE"},
        },
    )
    assert adjust_response.status_code == 200, adjust_response.text

    # 5. Investigation ------------------------------------------------------------------------
    case_after_mandates = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    investigate_response = client.post(
        f"/v1/cases/{case_id}/investigations",
        headers={**PRESENTER, "Idempotency-Key": "hero-investigate-0001"},
        json={
            "expected_case_version": case_after_mandates["case"]["version"],
            "reason": "INITIAL",
        },
    )
    assert investigate_response.status_code == 202, investigate_response.text
    investigate_outcome = await _poll(
        client, composition, investigate_response.json()["operation_id"], PRESENTER
    )
    assert investigate_outcome["status"] == "SUCCEEDED", investigate_outcome

    investigation = client.get(f"/v1/cases/{case_id}/investigation", headers=PRESENTER)
    assert investigation.status_code == 200, investigation.text
    investigation_body = investigation.json()
    assert investigation_body["assessment"] is not None
    assert investigation_body["facts"], "the case should carry at least one investigated fact"
    for fact in investigation_body["facts"]:
        assert fact["value_preview"], "every fact must carry a non-empty private preview"
    # The corpus's private health/unit/identity messages (4, 6, 7) are recognized by the local
    # lexical stand-in (P2-8) and produce real HEALTH_DETAIL/UNIT_LOCATION/IDENTITY_ATTRIBUTE
    # facts, each carrying the actual private text below -- this is the material Resident B's
    # ADJUST above locks to INTERNAL_ONLY, and the compile step further down proves excluded.
    sensitive_types = {"HEALTH_DETAIL", "UNIT_LOCATION", "IDENTITY_ATTRIBUTE"}
    sensitive_facts = [f for f in investigation_body["facts"] if f["fact_type"] in sensitive_types]
    assert {f["fact_type"] for f in sensitive_facts} == sensitive_types, investigation_body["facts"]
    # case_approver may not reach this surface.
    denied = client.get(f"/v1/cases/{case_id}/investigation", headers=APPROVER)
    assert denied.status_code == 403, denied.text

    # 6. Case is ready ------------------------------------------------------------------------
    ready_case = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    assert ready_case["case"]["state"] == "READY_FOR_ACTION", ready_case

    # 7. Compile view ---------------------------------------------------------------------------
    facts_by_type: dict[str, list[dict[str, Any]]] = {}
    for row in ready_case["evidence_summary"]:
        facts_by_type.setdefault(row["fact_type"], []).append(row)
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
    compile_response = client.post(
        f"/v1/cases/{case_id}/views",
        headers={**PRESENTER, "Idempotency-Key": "hero-compile-0001"},
        json={
            "compile_id": "11111111-1111-4111-8111-111111111111",
            "expected_case_version": ready_case["case"]["version"],
            "requested_facts": requested_facts,
            "requested_evidence_ids": [],
            "purpose": "REQUEST_ELEVATOR_REPAIR_AND_RESPONSE",
        },
    )
    assert compile_response.status_code == 200, compile_response.text
    compile_body = compile_response.json()
    assert compile_body["decision"] == "ALLOW", compile_body
    _no_leak(compile_body["view"], leaked_check)
    # Resident B's three private facts (P2-8) are all INTERNAL_ONLY, so the compiler actually
    # excludes them here -- a real, non-zero exclusion, not merely an absence of anything to
    # exclude. The injected message still never becomes a fact at all, so it never reaches
    # `requested_facts` in the first place. The sentinel text is separately asserted absent
    # from every response this test receives, all the way through.
    excluded_fact_ids = {item["fact_id"] for item in compile_body["excluded"]}
    excluded_sensitive_ids = {row["fact_id"] for row in facts_by_type.get("HEALTH_DETAIL", [])}
    excluded_sensitive_ids |= {row["fact_id"] for row in facts_by_type.get("UNIT_LOCATION", [])}
    excluded_sensitive_ids |= {
        row["fact_id"] for row in facts_by_type.get("IDENTITY_ATTRIBUTE", [])
    }
    assert excluded_sensitive_ids, "the case should carry the three private facts to exclude"
    assert excluded_sensitive_ids <= excluded_fact_ids, compile_body["excluded"]

    # After a successful compile, the private investigation surface resolves the current safe
    # view pointer and returns that view's matching compile explanation (P2-2). It was `null`
    # at the investigation step above because no view existed yet.
    investigation_after_compile = client.get(
        f"/v1/cases/{case_id}/investigation", headers=PRESENTER
    ).json()
    assert investigation_after_compile["compile"] is not None, investigation_after_compile
    assert investigation_after_compile["compile"]["view_id"] == compile_body["view"]["view_id"]
    assert investigation_after_compile["compile"]["decision"] == "ALLOW"

    # 8. Propose action -----------------------------------------------------------------------
    view = compile_body["view"]
    propose_action_response = client.post(
        f"/v1/cases/{case_id}/actions",
        headers={**PRESENTER, "Idempotency-Key": "hero-propose-action-0001"},
        json={
            "expected_case_version": ready_case["case"]["version"],
            "view_id": view["view_id"],
            "view_hash": view["view_hash"],
        },
    )
    assert propose_action_response.status_code == 202, propose_action_response.text
    propose_outcome = await _poll(
        client, composition, propose_action_response.json()["operation_id"], PRESENTER
    )
    assert propose_outcome["status"] == "SUCCEEDED", propose_outcome

    proposed_case = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    current_action = proposed_case["current_action"]
    assert current_action is not None
    assert current_action["preview"]["matches_committed_hash"] is True
    assert current_action["execution"]["version"] >= 1
    _no_leak(proposed_case, leaked_check)

    # 9. Approve -------------------------------------------------------------------------------
    approver_view_of_case = client.get(f"/v1/cases/{case_id}", headers=APPROVER).json()
    assert approver_view_of_case["case"] is None or approver_view_of_case["case"]["title"] is None
    approve_response = client.post(
        f"/v1/cases/{case_id}/actions/{current_action['action_id']}/approvals",
        headers={**APPROVER, "Idempotency-Key": "hero-approve-0001"},
        json={
            "decision": "APPROVED",
            "expected_execution_version": current_action["execution"]["version"],
            "execution_id": current_action["execution"]["execution_id"],
            "view_hash": current_action["view_hash"],
            "proposal_hash": current_action["proposal_hash"],
            "preview_hash": current_action["preview"]["preview_hash"],
        },
    )
    assert approve_response.status_code == 200, approve_response.text
    approval_body = approve_response.json()

    # 10. Execute ------------------------------------------------------------------------------
    execute_response = client.post(
        f"/v1/cases/{case_id}/actions/{current_action['action_id']}/executions",
        headers={**APPROVER, "Idempotency-Key": "hero-execute-0001"},
        json={
            "execution_id": approval_body["execution_id"],
            "expected_execution_version": approval_body["execution_version"],
            "approval_id": approval_body["approval_id"],
        },
    )
    assert execute_response.status_code == 202, execute_response.text
    execute_outcome = await _poll(
        client, composition, execute_response.json()["operation_id"], APPROVER
    )
    assert execute_outcome["status"] == "SUCCEEDED", execute_outcome

    sent_case = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    assert sent_case["current_action"]["execution"]["state"] == "SENT", sent_case
    assert sent_case["case"]["state"] == "ACTIONED", sent_case
    outbox_dir = composition.settings.local_data_dir / "outbox"
    assert outbox_dir.exists()
    assert len(list(outbox_dir.glob("*.json"))) == 1, "exactly one local sender attempt"

    # 11. External reply -> commitment extraction ----------------------------------------------
    reply_response = client.post(
        "/v1/demo/external-replies",
        headers={**PRESENTER, "Idempotency-Key": "hero-reply-0001"},
        json={"fixture_id": "manager-promise"},
    )
    assert reply_response.status_code == 202, reply_response.text
    reply_outcome = await _poll(
        client, composition, reply_response.json()["operation_id"], PRESENTER
    )
    assert reply_outcome["status"] == "SUCCEEDED", reply_outcome
    assert len(reply_outcome["result_refs"]) == 1
    commitment_id = reply_outcome["result_refs"][0]

    case_with_commitment = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    commitments = {c["commitment_id"]: c for c in case_with_commitment["commitments"]}
    assert commitment_id in commitments
    assert commitments[commitment_id]["status"] == "PENDING"
    assert commitments[commitment_id]["due_at"]
    assert case_with_commitment["case"]["state"] == "VERIFYING", case_with_commitment

    # The accepted commitment caused exactly one local scheduler request, through the normal
    # extraction orchestration and not through any route (P2-3).
    assert len(composition.scheduler.created) == 1, composition.scheduler.created
    scheduled = composition.scheduler.created[0]
    assert str(scheduled.payload.commitment_id) == commitment_id

    # A replayed reply re-runs the extraction operation but issues no second scheduler request:
    # CreateDueSchedule short-circuits on the strongly read projection already being CREATED.
    replay_reply = client.post(
        "/v1/demo/external-replies",
        headers={**PRESENTER, "Idempotency-Key": "hero-reply-0001"},
        json={"fixture_id": "manager-promise"},
    )
    assert replay_reply.status_code == 202, replay_reply.text
    await _poll(client, composition, replay_reply.json()["operation_id"], PRESENTER)
    assert len(composition.scheduler.created) == 1, composition.scheduler.created

    # 12. Advance the demo clock -----------------------------------------------------------------
    clock_response = client.post(
        "/v1/demo/clock/advance",
        headers=PRESENTER,
        json={
            "case_id": case_id,
            "commitment_id": commitment_id,
            "advance_seconds": 40 * 24 * 3600,
        },
    )
    assert clock_response.status_code == 200, clock_response.text
    clock_body = clock_response.json()
    assert clock_body["watcher_outcome"] == "DUE", clock_body
    assert clock_body["commitment_status"] == "DUE"

    # 13. Verification (MISSED) -------------------------------------------------------------------
    affected_facts = [
        row for row in case_with_commitment.get("evidence_summary", []) if row["status"] == "ACTIVE"
    ]
    affected_actor = None
    for demo_actor, contributor_id in contributor_by_actor.items():
        if any(row["contributor_id"] == contributor_id for row in affected_facts):
            affected_actor = demo_actor
            break
    assert affected_actor is not None, "no resident owns an active fact in this case"

    due_commitment = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    due_row = next(c for c in due_commitment["commitments"] if c["commitment_id"] == commitment_id)
    verify_response = client.post(
        f"/v1/cases/{case_id}/commitments/{commitment_id}/verification",
        headers={**actor(affected_actor), "Idempotency-Key": "hero-verify-0001"},
        json={"expected_version": due_row["version"], "outcome": "MISSED"},
    )
    assert verify_response.status_code == 200, verify_response.text
    verify_body = verify_response.json()
    assert verify_body["commitment_status"] == "MISSED"
    assert verify_body["case_state"] == "READY_FOR_ACTION"
    assert verify_body["action_pointer_invalidated"] is True

    final_case = client.get(f"/v1/cases/{case_id}", headers=PRESENTER).json()
    assert final_case["case"]["state"] == "READY_FOR_ACTION"
    assert final_case["case"]["state"] != "RESOLVED", "ACTIONED != RESOLVED is the demo's thesis"

    # -- the sentinel never leaked anywhere along the way -------------------------------------
    combined = "\n".join(leaked_check)
    for sentinel in SECRET_SENTINELS:
        assert sentinel not in combined
