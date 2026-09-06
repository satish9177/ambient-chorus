"""F01 -- the message the model actually receives must carry every required binding.

Codex proved the application-side ``ActionInput`` was complete while the *rendered* user
message was not: it printed ``view_id`` and ``case_id`` and nothing else of the five values
:class:`~chorus.contracts.action.ActionProposalDraft` requires the model to echo. So
``case_version``, ``authorization_version``, and ``view_hash`` had to be produced by a model
that had never been shown them, and the validator refuses any answer whose values differ from
the view that was sent -- an honest model could not pass.

The whole point of this file is that it exercises the **real** rendering path. Every assertion
below runs against the string :func:`render_action_user_message` returns for the exact payload
that crossed the port. A scripted responder handed the raw DTO would have been green through
the entire defect, which is why the prior suite was.
"""

from __future__ import annotations

import pytest
from runtimes.action.prompt import (
    ACTION_SYSTEM_PROMPT,
    BINDING_FIELDS,
    derive_fence,
    render_action_user_message,
)

from chorus.contracts.action import ActionInput, ActionProposalDraft
from tests.fixtures.action import ActionHarness
from tests.fixtures.compile import SENTINEL_PATTERN

pytestmark = pytest.mark.anyio

REQUIRED_BINDINGS = ("case_id", "case_version", "authorization_version", "view_id", "view_hash")


async def _rendered(harness: ActionHarness) -> tuple[str, ActionInput]:
    """The exact user message the runtime would build for this invocation."""

    await harness.prepare()
    await harness.propose_action().execute(await harness.command())
    assert len(harness.agent.invocations) == 1
    invocation = harness.agent.invocations[0]
    payload: ActionInput = invocation.payload
    fence = derive_fence(payload, invocation.invocation_id)
    return render_action_user_message(payload, fence=fence), payload


# ---------------------------------------------------------------------------------------
# The five values the structured output requires
# ---------------------------------------------------------------------------------------


async def test_the_rendered_message_states_every_required_binding(
    harness: ActionHarness,
) -> None:
    rendered, payload = await _rendered(harness)

    assert f"case_id={payload.case_id}" in rendered
    assert f"case_version={payload.case_version}" in rendered
    assert f"authorization_version={payload.authorization_version}" in rendered
    assert f"view_id={payload.view_id}" in rendered
    assert f"view_hash={payload.view_hash}" in rendered


async def test_every_binding_value_comes_from_the_persisted_view(
    harness: ActionHarness,
) -> None:
    """The source is the ``ActionInput`` mirror of the compiled view and nothing else.

    Asserted against the *stored* artifact rather than against the payload, so a projection
    that had quietly recomputed one of the five would fail here.
    """

    rendered, _ = await _rendered(harness)
    view = harness.view

    assert f"case_id={view.case_id}" in rendered
    assert f"case_version={view.case_version}" in rendered
    assert f"authorization_version={view.authorization_version}" in rendered
    assert f"view_id={view.view_id}" in rendered
    assert f"view_hash={view.view_hash.value}" in rendered


async def test_the_binding_field_list_matches_the_structured_output_requirement(
    harness: ActionHarness,
) -> None:
    """A sixth required binding added to the draft schema fails here, not in production."""

    required = {
        name
        for name, field in ActionProposalDraft.model_fields.items()
        if field.is_required() and name in ActionProposalDraft.model_fields
    }

    assert set(BINDING_FIELDS) == set(REQUIRED_BINDINGS)
    assert set(BINDING_FIELDS) <= required


async def test_the_system_prompt_tells_the_model_to_echo_them(harness: ActionHarness) -> None:
    """A rule the prompt never states is a hidden requirement, which is what failed here."""

    for field in BINDING_FIELDS:
        assert field in ACTION_SYSTEM_PROMPT
    assert "BINDING VALUES" in ACTION_SYSTEM_PROMPT


async def test_the_rendered_message_still_carries_the_data_the_prompt_needs(
    harness: ActionHarness,
) -> None:
    """The repair added bindings; it removed nothing the drafting task depends on."""

    rendered, payload = await _rendered(harness)
    fence = derive_fence(payload, harness.agent.invocations[0].invocation_id)

    assert f"<<<{fence}{payload.community_public_label}{fence}>>>" in rendered
    assert f"<<<{fence}{payload.destination.display_label}{fence}>>>" in rendered
    assert f"purpose={payload.purpose}" in rendered
    for fact in payload.shareable_facts:
        assert f"export_fact_id={fact.export_fact_id}" in rendered
        assert f"<<<{fence}{fact.safe_text}{fence}>>>" in rendered
        assert f"evidence_status={fact.evidence_status}" in rendered


async def test_binding_values_are_stated_outside_the_data_fences(
    harness: ActionHarness,
) -> None:
    """They are typed contract values the runtime states, not text a person wrote.

    Fencing them would say the opposite of what is true about them, and would invite the model
    to treat the identity of its own answer as quoted material.
    """

    rendered, payload = await _rendered(harness)
    fence = derive_fence(payload, harness.agent.invocations[0].invocation_id)

    binding_block = rendered.split("VIEW\n", 1)[0]
    assert fence not in binding_block.split("DATA MARKERS", 1)[1].split("\n", 1)[1]


# ---------------------------------------------------------------------------------------
# What still may not reach the prompt
# ---------------------------------------------------------------------------------------


async def test_no_recipient_address_reaches_the_prompt(harness: ActionHarness) -> None:
    rendered, _ = await _rendered(harness)

    assert "@" not in rendered
    assert "recipient organisation" in rendered  # a label, which is the whole of the routing


async def test_no_sending_identity_reaches_the_prompt(harness: ActionHarness) -> None:
    """``from_identity_id`` is a renderer input held by the application (ADR-022 § 4)."""

    from tests.fixtures.action import FROM_IDENTITY_ID

    rendered, _ = await _rendered(harness)

    assert FROM_IDENTITY_ID not in rendered
    assert "from_identity" not in rendered


async def test_no_private_case_state_reaches_the_prompt(harness: ActionHarness) -> None:
    """The Core case is strongly read for freshness and never rendered into the prompt."""

    rendered, _ = await _rendered(harness)
    case = await harness.compile.core.load_case(harness.scope)

    assert case.title not in rendered
    assert case.issue_type not in rendered
    assert case.state.value not in rendered
    assert case.state_reason_code not in rendered
    for report_id in case.report_ids:
        assert str(report_id) not in rendered


async def test_no_mandate_record_reaches_the_prompt(harness: ActionHarness) -> None:
    """The mandate set is inside the payload as opaque triples and is not rendered at all."""

    rendered, payload = await _rendered(harness)

    assert payload.mandate_version_set
    for ref in payload.mandate_version_set:
        assert str(ref.mandate_id) not in rendered
        assert ref.terms_hash not in rendered
    assert "mandate" not in rendered.lower()


async def test_no_investigation_assessment_reaches_the_prompt(
    harness: ActionHarness,
) -> None:
    """ADR-021 § 3: the Action Agent never reads an ``InvestigationAssessment``."""

    rendered, _ = await _rendered(harness)

    for absent in ("assessment", "contradiction_id", "materiality", "alternative"):
        assert absent not in rendered.lower()


async def test_no_compiler_exclusion_reaches_the_prompt(harness: ActionHarness) -> None:
    """Why a fact did not travel is private lineage and stays in the audit projection."""

    rendered, _ = await _rendered(harness)

    for absent in ("excluded", "reason_codes", "gates", "necessity", "intended_usage"):
        assert absent not in rendered.lower()


async def test_no_planted_private_sentinel_reaches_the_prompt(
    harness: ActionHarness,
) -> None:
    """The fixture's health condition, unit number, personal name, and private URI."""

    rendered, _ = await _rendered(harness)

    assert SENTINEL_PATTERN.search(rendered) is None


async def test_no_private_identifier_reaches_the_prompt(harness: ActionHarness) -> None:
    rendered, _ = await _rendered(harness)
    context = harness.compile.fixture.context

    for identifier in (
        *(str(fact.fact_id) for fact in context.facts),
        *(str(report.report_id) for report in context.reports),
        *(str(item.evidence_id) for item in context.evidence_items),
        *(str(report.contributor_id) for report in context.reports),
    ):
        assert identifier not in rendered, identifier
