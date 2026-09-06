"""What the Action runtime is actually handed, asserted from the captured invocation.

Every other proof about the Action Agent's isolation is structural -- an import scan, an IAM
deny, an artifact allowlist. This file is the behavioural one: it captures the exact envelope
that crossed the port and asserts what is in it.

The claim being tested is narrow and total. The payload is the serialized ``ShareableCaseView``
and **nothing else**. Not the case row the application strongly read to enforce freshness, not a
fact identifier, not a report, not a mandate record, not a compiler exclusion, not the recipient,
not the sending identity, and not a "helpful context" field somebody thought would improve the
wording.
"""

from __future__ import annotations

import json

import pytest
from tests.fixtures.action import ActionHarness
from tests.fixtures.compile import SENTINEL_PATTERN

from chorus.contracts.action import ActionInput
from chorus.contracts.common import AgentName
from chorus.privacy.policy import POLICY_VERSION

pytestmark = pytest.mark.anyio


async def _captured(harness: ActionHarness) -> object:
    await harness.prepare()
    await harness.propose_action().execute(await harness.command())
    assert len(harness.agent.invocations) == 1
    return harness.agent.invocations[0]


async def test_the_action_input_is_exactly_the_serialized_view(
    harness: ActionHarness,
) -> None:
    """Field for field, with the same values the persisted artifact carries."""

    invocation = await _captured(harness)
    payload: ActionInput = invocation.payload  # type: ignore[attr-defined]
    view = harness.view

    assert payload.view_id == view.view_id.value
    assert payload.view_hash == view.view_hash.value
    assert payload.case_id == view.case_id.value
    assert payload.case_version == view.case_version
    assert payload.authorization_version == view.authorization_version
    assert payload.policy_version == view.policy_version
    assert payload.compiler_version == view.compiler_version
    assert payload.authorization_snapshot_hash == view.authorization_snapshot_hash.value
    assert len(payload.shareable_facts) == len(view.shareable_facts)
    assert len(payload.safe_evidence_refs) == len(view.safe_evidence_refs)
    assert len(payload.mandate_version_set) == len(view.mandate_version_set)


async def test_the_envelope_carries_identifiers_and_versions_and_no_payload_of_its_own(
    harness: ActionHarness,
) -> None:
    invocation = await _captured(harness)

    assert invocation.agent_name is AgentName.ACTION  # type: ignore[attr-defined]
    assert invocation.schema_version == "agent-input/v1"  # type: ignore[attr-defined]
    assert invocation.policy_version == POLICY_VERSION  # type: ignore[attr-defined]
    assert invocation.case_id == harness.case_id.value  # type: ignore[attr-defined]
    assert set(type(invocation).model_fields) == {  # type: ignore[attr-defined]
        "schema_version",
        "invocation_id",
        "namespace",
        "agent_name",
        "case_id",
        "case_version",
        "requested_at",
        "policy_version",
        "payload",
    }


async def test_no_private_identifier_reaches_the_payload(harness: ActionHarness) -> None:
    """Not one fact, report, evidence, contributor, or mandate identifier from the Core zone.

    The compiler mints fresh ``export_fact_id`` values precisely so a safe fact cannot be joined
    back to its private source, and this asserts the property end to end rather than trusting
    that the projection kept it.
    """

    invocation = await _captured(harness)
    serialized = invocation.payload.model_dump_json()  # type: ignore[attr-defined]
    context = harness.compile.fixture.context

    private_ids = {
        *(str(fact.fact_id) for fact in context.facts),
        *(str(report.report_id) for report in context.reports),
        *(str(item.evidence_id) for item in context.evidence_items),
        *(str(item.root_id) for item in context.evidence_items),
        *(str(report.contributor_id) for report in context.reports),
    }
    for identifier in private_ids:
        assert identifier not in serialized, identifier


async def test_no_private_value_reaches_the_payload(harness: ActionHarness) -> None:
    """The fixture's planted sentinels: a health condition, a unit number, a name, a URI."""

    invocation = await _captured(harness)
    serialized = invocation.payload.model_dump_json()  # type: ignore[attr-defined]

    assert SENTINEL_PATTERN.search(serialized) is None


async def test_the_private_case_read_for_freshness_is_never_appended_to_the_payload(
    harness: ActionHarness,
) -> None:
    """The application *must* strongly read the Core case; it must not pass it on.

    Every private field of that row -- the title, the issue type, the state, the report and fact
    lists, the assessment pointer, the reason code -- is absent from what the model sees.
    """

    invocation = await _captured(harness)
    serialized = invocation.payload.model_dump_json()  # type: ignore[attr-defined]
    case = await harness.compile.core.load_case(harness.scope)

    assert case.title not in serialized
    assert case.issue_type not in serialized
    assert case.state_reason_code not in serialized
    assert "state" not in json.loads(serialized)
    for report_id in case.report_ids:
        assert str(report_id) not in serialized


async def test_the_payload_carries_no_recipient_and_no_sending_identity(
    harness: ActionHarness,
) -> None:
    """The destination is a label plus opaque version and routing token, never an address.

    ``from_identity_id`` is a renderer input held by the application, so the model never learns
    which identity the message will claim to be from either.
    """

    invocation = await _captured(harness)
    payload: ActionInput = invocation.payload  # type: ignore[attr-defined]
    serialized = payload.model_dump_json()

    assert "@" not in payload.destination.display_label
    assert "chorus-demo-sender" not in serialized
    assert "from_identity" not in serialized
    assert "recipient" not in serialized


async def test_the_payload_carries_no_compiler_exclusion_or_audit_projection(
    harness: ActionHarness,
) -> None:
    """Why a fact did *not* travel is private lineage, and it stays in the audit projection."""

    invocation = await _captured(harness)
    serialized = invocation.payload.model_dump_json()  # type: ignore[attr-defined]

    for absent in ("excluded", "reason_codes", "gates", "necessity", "intended_usage"):
        assert absent not in serialized


async def test_the_payload_round_trips_through_the_strict_contract(
    harness: ActionHarness,
) -> None:
    """What crossed the port is exactly what the runtime will parse, byte for byte.

    A payload that only *nearly* validates would mean the application and the runtime disagree
    about the contract, which is the disagreement the strict model exists to make impossible.
    """

    invocation = await _captured(harness)
    raw = invocation.payload.model_dump_json()  # type: ignore[attr-defined]

    assert ActionInput.model_validate_json(raw) == invocation.payload  # type: ignore[attr-defined]


async def test_the_runtime_entrypoint_accepts_the_exact_envelope_the_application_sends(
    harness: ActionHarness,
) -> None:
    """The two halves of the contract meet here, without a model.

    The runtime's own validation is separate from the application's semantic validation, and
    neither trusts the other to have done its half. This proves the *first* half accepts what
    the application actually produces.
    """

    from runtimes.action.entrypoint import parse_invocation

    invocation = await _captured(harness)
    raw = invocation.model_dump_json().encode("utf-8")  # type: ignore[attr-defined]

    parsed = parse_invocation(raw)

    assert parsed.agent_name is AgentName.ACTION
    assert parsed.payload == invocation.payload  # type: ignore[attr-defined]


async def test_the_runtime_refuses_an_envelope_addressed_to_another_agent(
    harness: ActionHarness,
) -> None:
    from runtimes.action.entrypoint import RuntimeContractError, parse_invocation

    invocation = await _captured(harness)
    foreign = invocation.model_copy(  # type: ignore[attr-defined]
        update={"agent_name": AgentName.INVESTIGATOR}
    )

    with pytest.raises(RuntimeContractError):
        parse_invocation(foreign.model_dump_json().encode("utf-8"))


async def test_the_runtime_refuses_a_payload_naming_a_different_case(
    harness: ActionHarness,
) -> None:
    """The envelope and the view are written by the same caller, so a disagreement is a defect."""

    from uuid import uuid4

    from runtimes.action.entrypoint import RuntimeContractError, parse_invocation

    invocation = await _captured(harness)
    mismatched = invocation.model_copy(update={"case_id": uuid4()})  # type: ignore[attr-defined]

    with pytest.raises(RuntimeContractError):
        parse_invocation(mismatched.model_dump_json().encode("utf-8"))


async def test_the_rendered_prompt_fences_every_safe_fact(harness: ActionHarness) -> None:
    """Fact text is quoted between per-invocation markers, never spliced into instructions."""

    from runtimes.action.prompt import derive_fence, render_action_user_message

    invocation = await _captured(harness)
    payload: ActionInput = invocation.payload  # type: ignore[attr-defined]
    fence = derive_fence(payload, invocation.invocation_id)  # type: ignore[attr-defined]

    rendered = render_action_user_message(payload, fence=fence)

    for fact in payload.shareable_facts:
        assert f"<<<{fence}{fact.safe_text}{fence}>>>" in rendered
