"""Gated live evaluation: can a real model find the pattern without being told what it is?

This is the only test in the suite that spends money and needs credentials, so it is skipped
unless a deployed Monitor runtime is explicitly configured. It never invents a credential, never
weakens a validator, and never falls back to the local stand-in: an unconfigured environment
skips, and a configured one runs for real.

What is asserted is the *shape* of the discovery, never the wording. The model must classify
every message, link several residents' reports into one candidate, leave the unrelated messages
alone, treat the injected instruction as data, and produce an answer the deterministic validator
accepts unchanged. Which phrases it keyed on is its business.
"""

from __future__ import annotations

import os

import pytest
from tests.fixtures.monitor import MonitorHarness

from chorus.application.commands.run_monitor import RunMonitorResult
from chorus.infrastructure.agentcore.client import create_agentcore_invoker
from chorus.infrastructure.agentcore.monitor import AgentCoreMonitorAgent
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.ports.pagination import PageRequest
from chorus.ports.scopes import CaseScope
from chorus.settings import Settings

ENABLE_VARIABLE = "AMBIENT_CHORUS_LIVE_MONITOR_EVAL"
"""The gate, deliberately outside the ``CHORUS_`` configuration prefix.

``Settings.load`` refuses to start when it sees an unknown ``CHORUS_`` variable, which is what
keeps a typo in deployment configuration from being ignored. A test-only switch under that
prefix would break the very process this evaluation runs against.
"""
RUNTIME_ARN_VARIABLE = "CHORUS_MONITOR_RUNTIME_ARN"
REGION_VARIABLE = "CHORUS_AWS_REGION"

MIN_LINKED_CONTRIBUTORS = 3
"""How many distinct residents must appear in the discovered candidate.

The corpus contains four. Requiring three rather than four leaves the model one miss before the
evaluation fails, which is an honest quality bar rather than a demand for perfection; the
frozen recall target is measured separately over repeated runs.
"""

NOISE_CHANNEL_IDS = frozenset(
    {
        "feed-001",
        "feed-003",
        "feed-009",
        "feed-010",
        "feed-013",
        "feed-015",
        "feed-017",
        "feed-019",
        "feed-020",
        "feed-021",
        "feed-022",
        "feed-023",
        "feed-024",
    }
)
"""Messages that are unambiguously about something else.

Deliberately excludes the four private-context messages and the injection, because a model may
reasonably attach a resident's own health or unit remark to their incident report.
"""

pytestmark = [pytest.mark.anyio, pytest.mark.live_agent]


def _configured() -> tuple[str, str] | None:
    """Resolve the live target, or say why there is not one.

    Two outcomes, and the difference between them is the whole point. Not asking for a live run
    is a skip. *Asking* for one and not supplying somewhere to send it is a failure: an
    operator who set the enable flag has said they want the real model exercised, and answering
    that with a green skip is how "we never ran it" gets mistaken for "it passed".
    """

    if os.environ.get(ENABLE_VARIABLE) != "1":
        return None
    runtime_arn = os.environ.get(RUNTIME_ARN_VARIABLE, "")
    region = os.environ.get(REGION_VARIABLE, "")
    if not runtime_arn or not region:
        pytest.fail(
            f"{ENABLE_VARIABLE}=1 requests a live Monitor evaluation, but "
            f"{RUNTIME_ARN_VARIABLE} and {REGION_VARIABLE} are not both set. "
            "Nothing was run; this is a failure, not a skip.",
            pytrace=False,
        )
    return runtime_arn, region


@pytest.fixture
def live_agent() -> AgentCoreMonitorAgent:
    configuration = _configured()
    if configuration is None:
        pytest.skip(
            f"set {ENABLE_VARIABLE}=1 with {RUNTIME_ARN_VARIABLE} and {REGION_VARIABLE} "
            "to run the live Monitor evaluation, or use tools/run_live_monitor_eval.py"
        )
    runtime_arn, region = configuration
    return AgentCoreMonitorAgent(
        invoker=create_agentcore_invoker(
            region_name=region, timeout_seconds=Settings(agent_mode="fake").agent_timeout_seconds
        ),
        runtime_arn=runtime_arn,
    )


@pytest.fixture
def harness() -> MonitorHarness:
    return MonitorHarness(driver=InMemoryStorageDriver())


async def _run(harness: MonitorHarness, agent: AgentCoreMonitorAgent) -> RunMonitorResult:
    await harness.seed()
    locators = await harness.ingest_feed()
    return await harness.run_monitor(agent).execute(harness.monitor_command(locators))


async def test_the_live_monitor_discovers_one_candidate_from_the_frozen_feed(
    harness: MonitorHarness, live_agent: AgentCoreMonitorAgent
) -> None:
    result = await _run(harness, live_agent)

    assert len(result.created_case_ids) == 1
    scope = CaseScope(
        namespace=harness.namespace,
        community_id=harness.community_id,
        case_id=result.created_case_ids[0],
    )
    case = await harness.core.load_case(scope)
    assert case.issue_type == "ELEVATOR_FAILURE"

    reports = await harness.core.read_case_reports(scope, PageRequest(limit=100))
    contributors = {report.contributor_id for report in reports.items}
    assert len(contributors) >= MIN_LINKED_CONTRIBUTORS


async def test_the_live_monitor_leaves_unrelated_messages_alone(
    harness: MonitorHarness, live_agent: AgentCoreMonitorAgent
) -> None:
    result = await _run(harness, live_agent)
    scope = CaseScope(
        namespace=harness.namespace,
        community_id=harness.community_id,
        case_id=result.created_case_ids[0],
    )
    reports = await harness.core.read_case_reports(scope, PageRequest(limit=100))

    linked = {message_id for report in reports.items for message_id in report.source_message_ids}
    noise_ids = await _message_ids_for(harness, NOISE_CHANNEL_IDS)
    assert not (linked & noise_ids)


async def test_the_live_monitor_gives_the_injected_instruction_no_authority(
    harness: MonitorHarness, live_agent: AgentCoreMonitorAgent
) -> None:
    """It may read the message. It cannot act on it, and nothing it returns can."""

    result = await _run(harness, live_agent)
    scope = CaseScope(
        namespace=harness.namespace,
        community_id=harness.community_id,
        case_id=result.created_case_ids[0],
    )
    case = await harness.core.load_case(scope)
    facts = await harness.core.read_case_facts(scope, PageRequest(limit=100))

    from chorus.domain.entities import CaseState, EvidenceStatus

    assert case.state is CaseState.CANDIDATE
    assert case.current_view_id is None
    assert case.current_action_id is None
    assert all(fact.evidence_status is EvidenceStatus.REPORTED for fact in facts.items)


async def test_a_live_answer_survives_the_deterministic_validator_unchanged(
    harness: MonitorHarness, live_agent: AgentCoreMonitorAgent
) -> None:
    """Reaching a result at all means every citation, span, and owner was already proved."""

    result = await _run(harness, live_agent)

    assert result.replayed is False
    assert result.report_count >= MIN_LINKED_CONTRIBUTORS
    assert result.fact_count >= 1
    assert result.noise_message_count >= 1


async def _message_ids_for(harness: MonitorHarness, channel_ids: frozenset[str]) -> set[object]:
    from datetime import timedelta

    page = await harness.core.read_message_feed(
        harness.core_scope,
        start=harness.adapter.messages()[0].sent_at - timedelta(days=1),
        end=harness.adapter.messages()[-1].sent_at + timedelta(days=1),
        request=PageRequest(limit=100),
    )
    return {
        message.message_id for message in page.items if message.channel_message_id in channel_ids
    }
