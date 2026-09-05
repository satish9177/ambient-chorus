"""The private compile explanation: the join the safe view deliberately does not carry.

This is the projection the investigation surface reads. What matters about it is symmetrical:
it must say enough for a presenter to see why a fact did not travel, and it must contain no
value that would make it a second private corpus if it were ever read somewhere weaker.
"""

from __future__ import annotations

import pytest
from tests.fixtures.compile import SENTINEL_PATTERN, CompileHarness, harness_uuid, photo_bytes

from chorus.application.commands.compile_view import CompileView
from chorus.application.errors import PolicyDeniedError
from chorus.application.queries.compile_audit import ReadCompileExplanation
from chorus.domain.entities import DisclosureScope
from chorus.ports.records import CompileDecisionOutcome

pytestmark = pytest.mark.anyio


async def _compiled(harness: CompileHarness) -> CompileView:
    raw = photo_bytes()
    await harness.seed(evidence_items=harness.align_photo_digest(raw), photo=raw)
    return harness.compile_view()


async def test_an_allowed_compile_explains_every_requested_fact(
    harness: CompileHarness,
) -> None:
    compile_view = await _compiled(harness)
    command = harness.command()
    await compile_view.execute(command)

    explanation = await ReadCompileExplanation(audit=harness.audit).read(
        harness.scope, command.compile_id
    )

    assert explanation is not None
    assert explanation.decision is CompileDecisionOutcome.ALLOW
    assert len(explanation.facts) == len(command.requested_facts)
    assert explanation.included_count + explanation.excluded_count == len(explanation.facts)
    assert explanation.gates


async def test_the_explanation_names_why_a_private_fact_stayed_home(
    harness: CompileHarness,
) -> None:
    """The whole point of the surface: a presenter can see the rule, not just the absence."""

    compile_view = await _compiled(harness)
    command = harness.command()
    await compile_view.execute(command)

    explanation = await ReadCompileExplanation(audit=harness.audit).read(
        harness.scope, command.compile_id
    )

    assert explanation is not None
    health = next(
        item for item in explanation.facts if item.fact_id == harness.fixture.health_fact_id
    )
    assert health.included is False
    assert health.granted_scope is DisclosureScope.INTERNAL_ONLY
    assert "INTERNAL_ONLY" in health.reason_codes


async def test_the_explanation_joins_a_source_item_to_its_safe_handle(
    harness: CompileHarness,
) -> None:
    compile_view = await _compiled(harness)
    command = harness.command()
    result = await compile_view.execute(command)

    explanation = await ReadCompileExplanation(audit=harness.audit).read(
        harness.scope, command.compile_id
    )

    assert explanation is not None
    assert result.view is not None
    entry = next(
        item
        for item in explanation.evidence
        if item.source_evidence_id == harness.fixture.photo_evidence_id
    )
    assert entry.included is True
    assert entry.export_handle_id == result.view.safe_evidence_refs[0].export_handle_id
    assert entry.derivative_sha256 == result.view.safe_evidence_refs[0].sha256


async def test_the_explanation_carries_no_bucket_key_or_private_text(
    harness: CompileHarness,
) -> None:
    """Identifiers, codes, versions and digests. A locator would be a second place to leak."""

    compile_view = await _compiled(harness)
    command = harness.command()
    await compile_view.execute(command)

    explanation = await ReadCompileExplanation(audit=harness.audit).read(
        harness.scope, command.compile_id
    )

    rendered = repr(explanation)
    assert SENTINEL_PATTERN.search(rendered) is None
    assert "ns/" not in rendered
    assert "chorus-export-evidence" not in rendered
    assert "s3://" not in rendered


async def test_a_denied_compile_is_explained_too(harness: CompileHarness) -> None:
    """A refusal is durable, so it is also readable -- and it names no view."""

    compile_view = await _compiled(harness)
    command = harness.command(
        compile_id=harness_uuid("compile:explained-deny"),
        expected_case_version=harness.case.version + 4,
    )

    with pytest.raises(PolicyDeniedError):
        await compile_view.execute(command)

    explanation = await ReadCompileExplanation(audit=harness.audit).read(
        harness.scope, command.compile_id
    )

    assert explanation is not None
    assert explanation.decision is CompileDecisionOutcome.DENY
    assert explanation.reason_codes == ("STALE_CASE_VERSION",)
    assert explanation.view_id is None
    assert explanation.included_count == 0


async def test_an_unknown_compile_has_no_explanation(harness: CompileHarness) -> None:
    await _compiled(harness)

    explanation = await ReadCompileExplanation(audit=harness.audit).read(
        harness.scope, harness_uuid("compile:never-ran")
    )

    assert explanation is None
