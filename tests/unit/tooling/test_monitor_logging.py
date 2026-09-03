"""No private text reaches a log, an error, or an audit row on any Phase 3 path.

The Monitor payload is private community text by construction, so the interesting question is
not whether the happy path logs it -- it is whether any *failure* path does. Errors are the
usual leak: a validation report quotes its input, an SDK exception quotes a response, and a
formatter that serializes an object serializes everything on it.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from tests.fixtures.monitor_outputs import build_invocation

from chorus.infrastructure.observability import ContentSafeJsonFormatter
from chorus.ports.agents import (
    AgentContractViolationError,
    AgentDependencyError,
    AgentRejection,
    AgentTimeoutError,
)
from chorus.ports.errors import IdempotencyConflictError

SENTINEL = "SECRET_SENTINEL_MOTHER_HEALTH"


def _render(**extra: object) -> str:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(ContentSafeJsonFormatter())
    logger = logging.getLogger(f"test.monitor.{len(extra)}.{id(extra)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info("ignored", extra=extra)
    return output.getvalue()


def test_an_agent_invocation_event_carries_identifiers_hashes_and_counts() -> None:
    rendered = _render(
        event_name="agent.invocation.completed",
        service="worker",
        invocation_id="0f3f0a58-2bd9-4c58-9d33-0d1a5c8e0f11",
        prompt_version="monitor/v1",
        input_hash="sha256:" + "a" * 64,
        output_hash="sha256:" + "b" * 64,
        outcome="SUCCEEDED",
        counts={"reports": 6, "facts": 6, "noise": 16},
        duration_ms=1_820,
        attempt=1,
    )

    payload = json.loads(rendered)
    assert payload["event_name"] == "agent.invocation.completed"
    assert payload["prompt_version"] == "monitor/v1"
    assert payload["counts"] == {"reports": 6, "facts": 6, "noise": 16}
    assert payload["attempt"] == 1


def test_a_message_body_has_no_field_to_be_logged_in() -> None:
    """There is no allowlisted field for text, so text simply never appears."""

    rendered = _render(
        event_name="message.accepted",
        text="My mother has SECRET_SENTINEL_MOTHER_HEALTH in Apartment 4B",
        raw_text=SENTINEL,
        summary=SENTINEL,
        prompt=SENTINEL,
        completion=SENTINEL,
    )

    assert SENTINEL not in rendered
    assert "Apartment" not in rendered
    payload = json.loads(rendered)
    assert set(payload) == {"timestamp", "level", "event_name"}


def test_a_count_object_carrying_text_is_redacted_whole() -> None:
    rendered = _render(event_name="agent.invocation.completed", counts={"note": SENTINEL})

    assert SENTINEL not in rendered
    assert json.loads(rendered)["counts"] == "REDACTED"


def test_a_reason_code_that_is_not_a_safe_token_is_redacted() -> None:
    rendered = _render(
        event_name="agent.contract.denied", reason_codes=[SENTINEL, "SOURCE_SPAN_INVALID"]
    )

    assert SENTINEL not in rendered
    assert json.loads(rendered)["reason_codes"] == ["REDACTED"]


def test_a_contract_violation_carries_only_bounded_reason_codes() -> None:
    error = AgentContractViolationError(
        (AgentRejection.SOURCE_SPAN_INVALID, AgentRejection.UNKNOWN_MESSAGE_ID)
    )

    assert error.reason_codes == ("SOURCE_SPAN_INVALID", "UNKNOWN_MESSAGE_ID")
    assert str(error) == "AGENT_CONTRACT_VIOLATION"
    for value in (str(error), repr(error)):
        assert SENTINEL not in value
        assert "quote" not in value


@pytest.mark.parametrize(
    "error",
    [
        AgentTimeoutError(),
        AgentDependencyError(),
        IdempotencyConflictError("COMMUNITY_MESSAGE"),
    ],
)
def test_no_phase_three_error_renders_a_payload(error: Exception) -> None:
    invocation = build_invocation()
    text = invocation.payload.messages[0].text

    for rendered in (str(error), repr(error)):
        assert text not in rendered


def test_a_rejected_answer_is_reported_by_code_and_never_by_content() -> None:
    """The API response for a refused answer names the gate, not the offending value."""

    error = AgentContractViolationError((AgentRejection.MODEL_SUPPLIED_IDENTIFIER,))

    rendered = _render(
        event_name="agent.contract.denied",
        outcome="DENIED",
        reason_codes=list(error.reason_codes),
    )

    payload = json.loads(rendered)
    assert payload["reason_codes"] == ["MODEL_SUPPLIED_IDENTIFIER"]
    assert payload["outcome"] == "DENIED"
