"""What an Action run is allowed to say about itself, and what it must never say.

The observability rules for this phase are narrower than for any other, because the thing being
described is draft text intended for somebody outside the community. Identifiers, hashes,
counts, versions, closed reason codes, prompt version, timings, and a model profile hash are
permitted. Proposal prose, view text, model completions, prompt text, recipient contact details,
and private data are not.

These are sentinel tests: they plant a distinctive string in the one place a leak could come
from and assert it never appears in any emitted record. A test that only checked the fields we
remembered to redact would prove nothing about the field somebody adds next.
"""

from __future__ import annotations

import json
import logging

import pytest
from tests.fixtures.action import ActionHarness, grounded_draft

from chorus.application import observability
from chorus.contracts.action import ActionClaimDraft, ActionProposalDraft
from chorus.ports.agents import ActionRejection, AgentContractViolationError

pytestmark = pytest.mark.anyio

SENTINEL_SUBJECT = "Repair request"


@pytest.fixture
def records(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger=observability.LOGGER_NAME)
    return caplog


def _events(records: pytest.LogCaptureFixture) -> set[str]:
    return {str(getattr(record, "event_name", "")) for record in records.records}


def _named(records: pytest.LogCaptureFixture, event_name: str) -> list[logging.LogRecord]:
    return [record for record in records.records if getattr(record, "event_name", "") == event_name]


def _serialized(records: pytest.LogCaptureFixture) -> str:
    """Every attribute of every emitted record, as one searchable string.

    Serialized whole rather than field by field, because the interesting failure is a *new*
    field carrying text -- and a test that only inspected the known fields would miss exactly
    that.
    """

    payloads = [
        {key: str(value) for key, value in vars(record).items() if not key.startswith("_")}
        for record in records.records
    ]
    return json.dumps(payloads)


async def test_a_successful_proposal_emits_identifiers_hashes_and_counts(
    harness: ActionHarness, records: pytest.LogCaptureFixture
) -> None:
    await harness.prepare()
    result = await harness.propose_action().execute(await harness.command())

    names = _events(records)
    assert observability.EventName.PROPOSAL_REQUESTED in names
    assert observability.EventName.PROPOSAL_VALIDATED in names
    assert observability.EventName.PROPOSAL_PERSISTED in names

    text = _serialized(records)
    assert str(result.action_id) in text
    assert result.proposal_hash.value in text
    assert result.preview_hash.value in text


async def test_no_proposal_prose_reaches_any_emitted_record(
    harness: ActionHarness, records: pytest.LogCaptureFixture
) -> None:
    """The subject, the claim text, the request, and the caveats -- none of them.

    The counts are what an operator can act on; the prose is the thing the rules forbid a log
    line from carrying.
    """

    view = await harness.prepare()
    fact_text = view.shareable_facts[0].safe_text
    await harness.propose_action().execute(await harness.command())

    text = _serialized(records)
    assert SENTINEL_SUBJECT not in text
    assert fact_text not in text
    assert "Please inspect and repair" not in text


async def test_no_rendered_body_reaches_any_emitted_record(
    harness: ActionHarness, records: pytest.LogCaptureFixture
) -> None:
    await harness.prepare()
    await harness.propose_action().execute(await harness.command())
    projection = await harness.read_current_action().execute(harness.scope)
    assert projection is not None

    text = _serialized(records)
    assert projection.html_body not in text
    assert "<h1>" not in text
    assert "Evidence-backed observations" not in text


async def test_no_sending_identity_or_recipient_reaches_any_emitted_record(
    harness: ActionHarness, records: pytest.LogCaptureFixture
) -> None:
    await harness.prepare()
    await harness.propose_action().execute(await harness.command())

    text = _serialized(records)
    assert "chorus-demo-sender" not in text
    assert "@" not in text.replace("\\u0040", "")


async def test_a_refusal_emits_bounded_reason_codes_and_no_offending_text(
    harness: ActionHarness, records: pytest.LogCaptureFixture
) -> None:
    """A rejection is logged, audited, and counted; the string that caused it is not.

    Every ``ActionRejection`` member is a closed uppercase code precisely so this holds without
    a redaction rule of its own.
    """

    await harness.prepare()
    offending = "Bob Smith called 555-123-4567 about it."

    def responder(invocation: object) -> ActionProposalDraft:
        payload = invocation.payload  # type: ignore[attr-defined]
        draft = grounded_draft(payload)
        return draft.model_copy(
            update={
                "claims": (
                    ActionClaimDraft(
                        claim_id=draft.claims[0].claim_id,
                        text=offending,
                        export_fact_ids=draft.claims[0].export_fact_ids,
                    ),
                )
            }
        )

    harness.agent.responder = responder

    with pytest.raises(AgentContractViolationError):
        await harness.propose_action().execute(await harness.command())

    text = _serialized(records)
    assert offending not in text
    assert "Bob Smith" not in text
    assert "555-123-4567" not in text
    assert ActionRejection.PHONE_PATTERN.value in text


async def test_the_stale_event_says_whether_a_model_call_was_spent(
    harness: ActionHarness, records: pytest.LogCaptureFixture
) -> None:
    """The field that distinguishes the two failure-matrix rows.

    A stale-before-invocation refusal spends nothing; a current-view pointer that moved *during*
    the invocation has already spent one pass and must not spend a second.
    """

    from uuid import uuid4

    from chorus.application.errors import StaleAuthorizationError

    await harness.prepare()

    async def recompile(_invocation: object) -> None:
        await harness.compile.compile_view().execute(
            harness.compile.command(compile_id=uuid4(), idempotency_key="compile-key-midflight")
        )

    harness.agent.on_invoke = recompile

    with pytest.raises(StaleAuthorizationError):
        await harness.propose_action().execute(await harness.command())

    stale = _named(records, observability.EventName.PROPOSAL_STALE_REJECTED)
    assert stale
    assert stale[0].counts["model_invocations"] == 1  # type: ignore[attr-defined]


async def test_a_replay_records_zero_model_invocations(
    harness: ActionHarness, records: pytest.LogCaptureFixture
) -> None:
    await harness.prepare()
    await harness.propose_action().execute(await harness.command())
    records.clear()

    await harness.propose_action().execute(await harness.command())

    replayed = _named(records, observability.EventName.PROPOSAL_REPLAYED)
    assert replayed
    assert replayed[0].counts["model_invocations"] == 0  # type: ignore[attr-defined]


async def test_the_audit_event_carries_codes_and_hashes_and_no_prose(
    harness: ActionHarness,
) -> None:
    """The audit row has a wider read audience than the proposal, so it holds even less."""

    from chorus.ports.pagination import PageRequest

    view = await harness.prepare()
    await harness.propose_action().execute(await harness.command())

    page = await harness.compile.audit.read_case_events(harness.scope, PageRequest(limit=50))
    proposed = [event for event in page.items if event.event_type == "action.proposed"]
    assert proposed

    serialized = str(proposed[0])
    assert SENTINEL_SUBJECT not in serialized
    assert view.shareable_facts[0].safe_text not in serialized
    assert proposed[0].reason_codes == ("ACTION_PROPOSED",)
    assert proposed[0].output_hash is not None
