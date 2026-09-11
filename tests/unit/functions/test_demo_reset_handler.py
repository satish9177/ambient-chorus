"""The dedicated demo reset function's entry point: a thin transport over the shared service.

Review R2. All the reset logic is in
:class:`~chorus.composition.deployed_demo_reset.DeployedDemoReset` (proved in
``test_deployed_demo_reset.py``); this file asserts only the transport boundary -- the
environment gate, the envelope, the error translation, and the safe result shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from functions.demo_reset.handler import (
    IDEMPOTENCY_CONFLICT,
    IN_FLIGHT_SEND,
    MALFORMED_EVENT,
    RESET_CONFIRMATION,
    RESET_INFRASTRUCTURE,
    RESET_OPERATION,
    WRONG_CONFIRMATION,
    WRONG_ENVIRONMENT,
    WRONG_NAMESPACE,
    run,
)
from functions.envelope import InvocationFailedError

from chorus.composition.demo_reset import (
    DemoResetInFlightSend,
    DemoResetRefused,
    DemoResetResult,
    ResetCounts,
)
from chorus.domain.ids import CommunityId, Sha256Digest
from chorus.ports.demo_reset import DemoManifestUnavailableError
from chorus.ports.errors import IdempotencyConflictError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _demo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_ENVIRONMENT", "demo")
    monkeypatch.setenv("CHORUS_NAMESPACE", "DEMO")


def _result(*, replayed: bool = False) -> DemoResetResult:
    return DemoResetResult(
        reset_id=UUID("11111111-1111-4111-8111-111111111111"),
        namespace="DEMO",
        seed_version="elevator/v1",
        corpus_sha256=Sha256Digest("sha256:" + "a" * 64),
        logical_now=datetime(2030, 1, 14, 9, tzinfo=UTC),
        community_id=CommunityId(UUID("22222222-2222-4222-8222-222222222222")),
        destination_id="property_manager:demo",
        contributors=(),
        evidence=(),
        counts=ResetCounts(deleted=42, messages=24, contributors=4, evidence=2),
        replayed=replayed,
        audit_event_id=UUID("33333333-3333-4333-8333-333333333333"),
    )


class _FakeDeployed:
    def __init__(self, outcome: DemoResetResult | Exception) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def reset(self, **kwargs: Any) -> DemoResetResult:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _event(**payload: Any) -> dict[str, Any]:
    body = {"namespace": "DEMO", "confirm": RESET_CONFIRMATION}
    body.update(payload)
    return {"operation": RESET_OPERATION, "payload": body}


async def test_a_wrong_environment_is_refused_before_anything_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHORUS_ENVIRONMENT", "development")
    assert await run(_event()) == {"status": "REFUSED", "reason_code": WRONG_ENVIRONMENT}


async def test_a_wrong_namespace_in_the_payload_is_refused() -> None:
    result = await run(
        _event(namespace="OTHER"),
        built=_FakeDeployed(_result()),  # type: ignore[arg-type]
    )
    assert result == {"status": "REFUSED", "reason_code": WRONG_NAMESPACE}


async def test_the_confirmation_string_is_a_second_barrier() -> None:
    result = await run(
        _event(confirm="nope"),
        built=_FakeDeployed(_result()),  # type: ignore[arg-type]
    )
    assert result == {"status": "REFUSED", "reason_code": WRONG_CONFIRMATION}


async def test_an_unknown_operation_is_refused_at_the_envelope() -> None:
    result = await run(
        {"operation": "something-else", "payload": {}},
        built=_FakeDeployed(_result()),  # type: ignore[arg-type]
    )
    assert result == {"status": "REFUSED", "reason_code": MALFORMED_EVENT}


async def test_a_successful_reset_returns_only_safe_metadata() -> None:
    fake = _FakeDeployed(_result(replayed=False))
    result = await run(_event(idempotency_key="op-1"), built=fake)  # type: ignore[arg-type]

    assert result["status"] == "COMPLETED"
    assert result["reset_id"] == "11111111-1111-4111-8111-111111111111"
    assert result["replayed"] is False
    assert result["counts"] == {
        "deleted": 42,
        "messages": 24,
        "contributors": 4,
        "evidence": 2,
    }
    assert "duration_ms" in result
    # nothing sensitive
    body = str(result)
    assert "@" not in body and "secret" not in body.lower() and "token" not in body.lower()
    # the transport passed the idempotency key straight through
    assert fake.calls[0]["idempotency_key"] == "op-1"


async def test_a_replay_is_reported_as_replayed() -> None:
    result = await run(
        _event(idempotency_key="op-2"),
        built=_FakeDeployed(_result(replayed=True)),  # type: ignore[arg-type]
    )
    assert result["replayed"] is True


async def test_a_refusal_from_the_service_becomes_its_reason_code() -> None:
    result = await run(
        _event(seed_version="elevator/v2"),
        built=_FakeDeployed(DemoResetRefused("RESET_SEED_VERSION")),  # type: ignore[arg-type]
    )
    assert result == {"status": "REFUSED", "reason_code": "RESET_SEED_VERSION"}


async def test_an_in_flight_send_is_a_refusal() -> None:
    result = await run(
        _event(),
        built=_FakeDeployed(DemoResetInFlightSend("x")),  # type: ignore[arg-type]
    )
    assert result == {"status": "REFUSED", "reason_code": IN_FLIGHT_SEND}


async def test_an_idempotency_conflict_is_a_refusal() -> None:
    result = await run(
        _event(idempotency_key="op-3"),
        built=_FakeDeployed(IdempotencyConflictError("DEMO_RESET")),  # type: ignore[arg-type]
    )
    assert result == {"status": "REFUSED", "reason_code": IDEMPOTENCY_CONFLICT}


async def test_an_infrastructure_failure_fails_the_invocation_not_the_answer() -> None:
    with pytest.raises(InvocationFailedError, match=RESET_INFRASTRUCTURE):
        await run(
            _event(),
            built=_FakeDeployed(DemoManifestUnavailableError("no manifest")),  # type: ignore[arg-type]
        )
