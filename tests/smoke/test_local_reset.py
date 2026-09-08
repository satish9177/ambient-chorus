"""Phase 10 local demo reset: cleanup, idempotency, the in-flight-send guard, and the CLI.

These cover the repair findings against the frozen reset contract:

* P1-1  -- reset restores the deterministic seed state (delete-and-seed), not merely writes it.
* P1-1  -- reset fails closed while an execution is SENDING or SEND_UNKNOWN.
* reset idempotency -- replay returns the recorded receipt; a materially different request
  under the same key conflicts; a fresh key after progression performs a real cleanup.
* P2-4  -- ``chorus-demo reset`` drives the *running* API, not a throwaway container.
* P2-6  -- reset verifies both private evidence objects before reporting success.
* P2-7  -- a create conflict is resolved by a strong read-back and exact match, never swallowed.

The reset-after-progression test is the load-bearing regression: it runs the whole hero flow,
resets under a fresh key, and proves every progressed row, object, and schedule is gone and a
fresh demo runs again.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.smoke.conftest import PRESENTER
from tests.smoke.test_local_hero_flow import test_hero_flow
from tests.smoke.test_local_negative import _seeded_case

from chorus.composition import cli_demo
from chorus.composition import demo_reset as demo_reset_module
from chorus.composition.demo_reset import predict_demo_case_id
from chorus.composition.local import LocalComposition
from chorus.domain.entities import ActionExecution, ActionExecutionState
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import ActionId, CaseId, Namespace
from chorus.infrastructure.dynamodb import codec_share
from chorus.infrastructure.dynamodb.codec import ATTR_ENTITY_TYPE, EntityType
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.ports.scopes import ActionScope

pytestmark = pytest.mark.anyio

RESET_BODY = {"namespace": "DEMO", "confirm": "RESET DEMO", "seed_version": "elevator/v1"}


def _reset(client: TestClient, key: str, **body_overrides: str) -> Any:
    return client.post(
        "/v1/demo/reset",
        headers={**PRESENTER, "Idempotency-Key": key},
        json={**RESET_BODY, **body_overrides},
    )


def _mem(composition: LocalComposition) -> InMemoryStorageDriver:
    driver = composition.driver
    assert isinstance(driver, InMemoryStorageDriver)
    return driver


# -- reset idempotency --------------------------------------------------------------------


async def test_replay_under_the_same_key_returns_the_recorded_receipt(
    client: TestClient, composition: LocalComposition
) -> None:
    first = _reset(client, "reset-replay-key-0001")
    assert first.status_code == 200, first.text
    # Progress the demo so a naive second reset would have visible work to do.
    await _seeded_case(client, composition)

    replay = _reset(client, "reset-replay-key-0001")
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["reset_id"] == first.json()["reset_id"]
    assert replay.json()["audit_event_id"] == first.json()["audit_event_id"]
    assert replay.json()["counts"] == first.json()["counts"]

    # The replay performed no destructive reset: the case discovered above still exists.
    feed = client.get(
        "/v1/feed",
        headers=PRESENTER,
        params={"community_id": first.json()["community_id"], "limit": 50},
    ).json()
    assert any(item["chorus_signal"] for item in feed["items"])


async def test_the_same_key_under_a_materially_different_request_conflicts(
    client: TestClient, composition: LocalComposition
) -> None:
    assert _reset(client, "reset-conflict-key-0001").status_code == 200
    conflicting = _reset(client, "reset-conflict-key-0001", seed_version="elevator/v2")
    assert conflicting.status_code == 409, conflicting.text
    assert conflicting.json()["code"] == "IDEMPOTENCY_CONFLICT"


async def test_a_fresh_key_after_progression_performs_a_real_cleanup(
    client: TestClient, composition: LocalComposition
) -> None:
    assert _reset(client, "reset-fresh-key-0001").status_code == 200
    case_id = await _seeded_case(client, composition)
    assert client.get(f"/v1/cases/{case_id}", headers=PRESENTER).status_code == 200

    fresh = _reset(client, "reset-fresh-key-0002")
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["replayed"] is False
    assert fresh.json()["counts"]["deleted"] > 0
    assert client.get(f"/v1/cases/{case_id}", headers=PRESENTER).status_code == 404


# -- P1-1: reset after a full hero flow restores a fresh demo ----------------------------


async def test_reset_after_full_progression_restores_a_fresh_demo(
    client: TestClient, composition: LocalComposition
) -> None:
    await test_hero_flow(client, composition)
    community_id = str(composition.adapter.community.community_id)

    feed = client.get(
        "/v1/feed", headers=PRESENTER, params={"community_id": community_id, "limit": 50}
    ).json()
    progressed_case_id = next(
        item["chorus_signal"]["candidate_case_id"]
        for item in feed["items"]
        if item["chorus_signal"]
    )
    case_before = client.get(f"/v1/cases/{progressed_case_id}", headers=PRESENTER).json()
    assert case_before["commitments"], "the progressed case carries a commitment"
    assert composition.scheduler.created, "the progressed run made a scheduler request"

    reset = _reset(client, "reset-after-progress-key-0002")
    assert reset.status_code == 200, reset.text
    assert reset.json()["replayed"] is False
    assert reset.json()["counts"]["deleted"] > 0
    assert reset.json()["counts"] == {
        "deleted": reset.json()["counts"]["deleted"],
        "messages": 24,
        "contributors": 4,
        "evidence": 2,
    }

    # Every progressed row, object, and schedule is gone.
    assert client.get(f"/v1/cases/{progressed_case_id}", headers=PRESENTER).status_code == 404
    assert composition.scheduler.created == []
    feed_after = client.get(
        "/v1/feed", headers=PRESENTER, params={"community_id": community_id, "limit": 50}
    ).json()
    assert feed_after["items"], "the 24-message seed corpus is back"
    assert all(item["chorus_signal"] is None for item in feed_after["items"]), (
        "no candidate case survives a reset"
    )

    # And the whole hero flow runs again against the restored seed.
    await test_hero_flow(client, composition)


# -- P2-5: reset's predicted case identity equals the live Monitor's ---------------------


async def test_reset_predicted_case_identity_equals_the_monitor_discovery(
    client: TestClient, composition: LocalComposition
) -> None:
    case_id = await _seeded_case(client, composition)
    predicted = predict_demo_case_id(
        composition.adapter,
        namespace=Namespace("DEMO"),
        community_id=composition.adapter.community.community_id,
    )
    assert case_id == str(predicted)

    # And the evidence reset pre-seeded is under that exact case, so investigation can load it.
    investigation = client.get(f"/v1/cases/{case_id}/investigation", headers=PRESENTER)
    assert investigation.status_code == 200, investigation.text


# -- P1: reset fails closed on ANY in-flight / ambiguous send in the namespace ----------


def _stored_executions(driver: InMemoryStorageDriver) -> list[tuple[Any, ActionExecution]]:
    """Every ``ACTION_EXECUTION`` row currently in the store, decoded, with its address."""

    found: list[tuple[Any, ActionExecution]] = []
    for address, item in driver._current.items():
        if item.get(ATTR_ENTITY_TYPE) != EntityType.ACTION_EXECUTION.value:
            continue
        _scope, execution = codec_share.decode_execution(item)
        found.append((address, execution))
    return found


def _rewrite_execution(
    driver: InMemoryStorageDriver,
    scope: ActionScope,
    execution: ActionExecution,
    *,
    address_to_drop: Any | None = None,
) -> None:
    item = codec_share.encode_execution(scope, execution)
    key = codec_share.execution_key(scope, execution.execution_id)
    if address_to_drop is not None:
        driver._current.pop(address_to_drop, None)
    driver._current[(key.table, key.partition_key, key.sort_key)] = item


def _into_state(execution: ActionExecution, state: ActionExecutionState) -> ActionExecution:
    """A field-presence-valid execution in ``state``, derived from a real SENT one."""

    if state is ActionExecutionState.SEND_UNKNOWN:
        return dataclasses.replace(execution, state=state, version=execution.version + 1)
    # SENDING forbids the post-send fields a SENT row carries.
    return dataclasses.replace(
        execution,
        state=state,
        ses_message_id=None,
        finished_at=None,
        reconciled_at=None,
        version=execution.version + 1,
    )


async def _sent_execution(
    client: TestClient, composition: LocalComposition
) -> tuple[ActionScope, ActionExecution]:
    """Run the whole hero flow, then hand back its one SENT execution and scope."""

    await test_hero_flow(client, composition)
    assert isinstance(composition.driver, InMemoryStorageDriver)
    rows = _stored_executions(composition.driver)
    assert len(rows) == 1, rows
    _address, execution = rows[0]
    scope = ActionScope(
        namespace=composition.container.namespace,
        community_id=composition.adapter.community.community_id,
        case_id=execution.case_id,
        action_id=execution.action_id,
    )
    return scope, execution


@pytest.mark.parametrize("state", [ActionExecutionState.SENDING, ActionExecutionState.SEND_UNKNOWN])
@pytest.mark.parametrize("different_case", [False, True], ids=["predicted-case", "other-case"])
async def test_reset_fails_closed_on_any_in_flight_execution_in_the_namespace(
    client: TestClient,
    composition: LocalComposition,
    state: ActionExecutionState,
    different_case: bool,
) -> None:
    scope, execution = await _sent_execution(client, composition)
    assert isinstance(composition.driver, InMemoryStorageDriver)

    if different_case:
        # Re-home the execution under a case the reset predictor never names, in a fresh
        # partition, to prove the guard covers the whole namespace and not one pointer.
        scope = dataclasses.replace(scope, case_id=CaseId(uuid4()), action_id=ActionId(uuid4()))
        execution = dataclasses.replace(execution, case_id=scope.case_id, action_id=scope.action_id)
    _rewrite_execution(composition.driver, scope, _into_state(execution, state))

    rows_before = dict(_mem(composition)._current)
    scheduler_before = list(composition.scheduler.created)
    outbox_dir = composition.settings.local_data_dir / "outbox"
    outbox_before = sorted(p.name for p in outbox_dir.glob("*.json"))

    refused = _reset(client, "reset-guard-key-0001")
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "STATE_TRANSITION_ERROR"

    # Nothing was erased -- rows, schedules, and outbox files are all unchanged.
    assert _mem(composition)._current == rows_before
    assert list(composition.scheduler.created) == scheduler_before
    assert sorted(p.name for p in outbox_dir.glob("*.json")) == outbox_before


async def test_reset_succeeds_when_no_execution_is_in_flight(
    client: TestClient, composition: LocalComposition
) -> None:
    # A fully progressed hero flow leaves its execution SENT (not in-flight), so reset proceeds.
    await test_hero_flow(client, composition)
    done = _reset(client, "reset-guard-clear-key-0001")
    assert done.status_code == 200, done.text
    assert done.json()["counts"]["deleted"] > 0


# -- P2-6: both private evidence objects are verified before success --------------------


async def test_reset_refuses_when_a_private_evidence_object_is_missing(
    composition: LocalComposition, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_service = composition.container.reset_demo
    assert reset_service is not None
    monkeypatch.setattr(
        type(reset_service.objects),
        "seed_private_evidence",
        lambda _self, **_kwargs: "unwritten-key",
    )
    with pytest.raises(IntegrityError):
        await reset_service.reset(
            namespace="DEMO",
            confirm="RESET DEMO",
            seed_version="elevator/v1",
            idempotency_key=None,
        )


async def test_reset_refuses_when_a_private_evidence_object_is_corrupt(
    composition: LocalComposition, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_service = composition.container.reset_demo
    assert reset_service is not None
    monkeypatch.setattr(demo_reset_module, "_fixture_bytes", lambda _fixture: b"corrupt-bytes")
    with pytest.raises(IntegrityError):
        await reset_service.reset(
            namespace="DEMO",
            confirm="RESET DEMO",
            seed_version="elevator/v1",
            idempotency_key=None,
        )


# -- P2-7: a create conflict is resolved by an exact read-back, never swallowed ---------


async def test_a_seed_create_conflict_with_a_divergent_row_fails_closed(
    composition: LocalComposition, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from chorus.domain.entities import Community, CommunityStatus
    from chorus.ports.scopes import NamespaceScope
    from chorus.ports.unit_of_work import TransactionPlan

    reset_service = composition.container.reset_demo
    assert reset_service is not None

    # A community row that shares the seed's identity but not its name.
    now = datetime(2030, 1, 14, 9, 0, tzinfo=UTC)
    imposter = Community(
        community_id=reset_service.community_id,
        namespace=reset_service.namespace,
        name="NOT THE SEED",
        timezone=composition.adapter.community.timezone,
        status=CommunityStatus.ACTIVE,
        version=1,
        created_at=now,
        updated_at=now,
    )
    await reset_service.unit_of_work.commit(
        TransactionPlan(
            name="test-plant-divergent-community",
            operations=(
                reset_service.core.stage_create_community(
                    NamespaceScope(namespace=reset_service.namespace), imposter
                ),
            ),
            audit_required=False,
        )
    )

    # Skip the purge so the divergent row is still there when the seed write conflicts.
    async def no_purge(_self: object, _namespace: str) -> int:
        return 0

    monkeypatch.setattr(type(reset_service.driver), "purge_namespace", no_purge)

    with pytest.raises(IntegrityError):
        await reset_service.reset(
            namespace="DEMO",
            confirm="RESET DEMO",
            seed_version="elevator/v1",
            idempotency_key=None,
        )


# -- P2: a corpus mutated after composition is refused before any purge ------------------


def _mutate_corpus(composition: LocalComposition, how: str) -> None:
    messages = list(composition.adapter.messages())
    if how == "reverse":
        messages.reverse()
    elif how == "alter":
        messages[0] = dataclasses.replace(messages[0], text=messages[0].text + " [tampered]")
    elif how == "remove":
        messages.pop()
    else:  # pragma: no cover - guarded by the parametrization
        raise AssertionError(how)
    # `_messages` is a slot the adapter set once at construction; this replaces it in memory
    # without touching the frozen files on disk, exactly as the re-review's repro does.
    object.__setattr__(composition.adapter, "_messages", tuple(messages))


@pytest.mark.parametrize("how", ["reverse", "alter", "remove"])
async def test_reset_refuses_a_corpus_mutated_after_composition_before_any_purge(
    client: TestClient, composition: LocalComposition, how: str
) -> None:
    case_id = await _seeded_case(client, composition)
    rows_before = dict(_mem(composition)._current)

    _mutate_corpus(composition, how)
    refused = _reset(client, f"reset-fixture-{how}-key-0001")

    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "VALIDATION_ERROR"
    # Nothing was purged or re-seeded: the progressed namespace is byte-for-byte unchanged.
    assert _mem(composition)._current == rows_before
    assert client.get(f"/v1/cases/{case_id}", headers=PRESENTER).status_code == 200


async def test_a_valid_reset_after_progression_still_predicts_the_monitor_case(
    client: TestClient, composition: LocalComposition
) -> None:
    await test_hero_flow(client, composition)
    assert _reset(client, "reset-fixture-valid-key-0002").json()["replayed"] is False

    # Re-ingest and re-discover on the restored seed: the case the Monitor creates is exactly
    # the one reset predicted and seeded evidence against.
    rediscovered = await _seeded_case(client, composition)
    predicted = predict_demo_case_id(
        composition.adapter,
        namespace=Namespace("DEMO"),
        community_id=composition.adapter.community.community_id,
    )
    assert rediscovered == str(predicted)
    assert (
        client.get(f"/v1/cases/{rediscovered}/investigation", headers=PRESENTER).status_code == 200
    )


# -- P2: EvidenceItem create-conflict read-back checks the full provenance ---------------


@pytest.mark.parametrize(
    "field",
    ["source_message_id", "submitted_by_contributor_id", "sha256", "root_id", "locator"],
)
async def test_reset_refuses_a_divergent_evidence_item(
    client: TestClient,
    composition: LocalComposition,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    from uuid import uuid4 as _uuid4

    from chorus.domain.ids import (
        ContributorId,
        EvidenceRootId,
        MessageId,
        SensitiveStr,
        Sha256Digest,
    )
    from chorus.infrastructure.dynamodb import codec_case
    from chorus.ports.scopes import CaseScope as _CaseScope

    await _seeded_case(client, composition)
    reset_service = composition.container.reset_demo
    assert reset_service is not None
    driver = composition.driver
    assert isinstance(driver, InMemoryStorageDriver)

    # Take the already-seeded photo EvidenceItem row and corrupt one immutable provenance
    # field in place, keeping its evidence_id, case, and partition so reset's create conflicts.
    address, item = next(
        (addr, it)
        for addr, it in driver._current.items()
        if it.get(ATTR_ENTITY_TYPE) == EntityType.EVIDENCE_ITEM.value
    )
    _decoded, evidence = codec_case.decode_evidence_item(item)
    scope = _CaseScope(
        namespace=evidence.namespace,
        community_id=evidence.community_id,
        case_id=evidence.case_id,
    )

    corruption: dict[str, dict[str, Any]] = {
        "source_message_id": {"source_message_id": MessageId(_uuid4())},
        "submitted_by_contributor_id": {"submitted_by_contributor_id": ContributorId(_uuid4())},
        "sha256": {"sha256": Sha256Digest("sha256:" + "0" * 64)},
        "root_id": {"root_id": EvidenceRootId(_uuid4())},
        "locator": {
            "private_object_key": SensitiveStr("ns/DEMO/community/x/case/y/evidence/z/v1/original")
        },
    }
    tampered = dataclasses.replace(evidence, **corruption[field])
    driver._current[address] = codec_case.encode_evidence_item(scope, tampered)

    async def no_purge(_self: object, _namespace: str) -> int:
        return 0

    monkeypatch.setattr(type(driver), "purge_namespace", no_purge)

    with pytest.raises(IntegrityError):
        await reset_service.reset(
            namespace="DEMO",
            confirm="RESET DEMO",
            seed_version="elevator/v1",
            idempotency_key="reset-evidence-divergent-key-0001",
        )


class _EvidenceConflictUnitOfWork:
    """Wraps a real UnitOfWork and makes only the evidence-item seed write conflict."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def commit(self, plan: Any) -> Any:
        from chorus.ports.errors import PersistenceConflictError

        if getattr(plan, "name", "") == "demo-reset-seed-evidence-item":
            raise PersistenceConflictError("EVIDENCE_ITEM")
        return await self._inner.commit(plan)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_reset_refuses_when_the_evidence_row_is_absent_after_a_conflict(
    composition: LocalComposition, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_service = composition.container.reset_demo
    assert reset_service is not None

    # The create-only write of the evidence item conflicts, but no row is actually there:
    # an absent row after a conflict must fail closed, not be treated as an idempotent replay.
    monkeypatch.setattr(
        reset_service, "unit_of_work", _EvidenceConflictUnitOfWork(reset_service.unit_of_work)
    )

    with pytest.raises(IntegrityError):
        await reset_service.reset(
            namespace="DEMO",
            confirm="RESET DEMO",
            seed_version="elevator/v1",
            idempotency_key=None,
        )


# -- P3: seed entity timestamps are deterministic across a reset after progression -------


async def test_seed_timestamps_are_identical_across_a_reset_after_progression(
    client: TestClient, composition: LocalComposition
) -> None:
    from chorus.ports.scopes import CommunityScope, NamespaceScope

    reset_service = composition.container.reset_demo
    assert reset_service is not None
    ns_scope = NamespaceScope(namespace=reset_service.namespace)
    comm_scope = CommunityScope(
        namespace=reset_service.namespace, community_id=reset_service.community_id
    )

    _reset(client, "reset-determinism-key-0001")
    fresh_community = await reset_service.core.load_community(ns_scope, reset_service.community_id)
    fresh_contributors = {
        seed.contributor_id: await reset_service.core.load_contributor(
            comm_scope, seed.contributor_id
        )
        for seed in composition.adapter.contributor_seeds
    }

    await test_hero_flow(client, composition)
    _reset(client, "reset-determinism-key-0002")

    after_community = await reset_service.core.load_community(ns_scope, reset_service.community_id)
    assert after_community.created_at == fresh_community.created_at
    assert after_community.updated_at == fresh_community.updated_at
    assert after_community.community_id == fresh_community.community_id
    for seed in composition.adapter.contributor_seeds:
        before = fresh_contributors[seed.contributor_id]
        after = await reset_service.core.load_contributor(comm_scope, seed.contributor_id)
        assert after.created_at == before.created_at
        assert after.updated_at == before.updated_at
        assert after.contributor_id == before.contributor_id
        assert after.pseudonym == before.pseudonym


# -- P2-4: the CLI drives the running API ----------------------------------------------


class _TestClientConnection:
    """A drop-in for ``http.client.HTTPConnection`` that speaks to an in-process app."""

    def __init__(self, client: TestClient) -> None:
        self._client = client
        self._response: Any = None

    def request(self, method: str, path: str, *, body: bytes, headers: dict[str, str]) -> None:
        self._response = self._client.request(method, path, content=body, headers=headers)

    def getresponse(self) -> Any:
        response = self._response
        assert response is not None
        return SimpleNamespace(status=response.status_code, read=lambda: response.content)

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass


async def test_cli_reset_resets_the_same_running_api(
    client: TestClient, composition: LocalComposition, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = await _seeded_case(client, composition)
    assert client.get(f"/v1/cases/{case_id}", headers=PRESENTER).status_code == 200

    monkeypatch.setattr(
        cli_demo, "HTTPConnection", lambda host, port, timeout: _TestClientConnection(client)
    )
    exit_code = cli_demo.main(
        [
            "reset",
            "--namespace",
            "DEMO",
            "--confirm",
            "RESET DEMO",
            "--seed",
            "elevator/v1",
            "--api-base-url",
            "http://127.0.0.1:8080",
        ]
    )
    assert exit_code == 0

    # The SAME running API now serves a fresh demo: the progressed case is gone.
    assert client.get(f"/v1/cases/{case_id}", headers=PRESENTER).status_code == 404


async def test_cli_reset_propagates_a_problem_details_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_demo, "HTTPConnection", lambda host, port, timeout: _TestClientConnection(client)
    )
    exit_code = cli_demo.main(
        [
            "reset",
            "--namespace",
            "DEMO",
            "--confirm",
            "WRONG CONFIRMATION",
            "--seed",
            "elevator/v1",
        ]
    )
    assert exit_code == 1
