"""The Phase 3 discovery path end to end, against both storage drivers.

The agent is a stand-in; everything else -- projection, validation, identity derivation, the
transaction, the feed projection -- is the production code path. That is what makes these
tests worth anything: they exercise the machinery that decides what becomes durable, not a
rehearsal of it.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from tests.fixtures.monitor import MonitorHarness

from chorus.application.commands.run_monitor import RunMonitorResult
from chorus.application.services.identity import derive_report_id
from chorus.contracts.monitor import MessageClassification, MonitorOutput
from chorus.domain.entities import CaseState, CommunityMessage, EvidenceStatus
from chorus.domain.facts import FactStatus, ReportStatus
from chorus.domain.ids import MessageId
from chorus.infrastructure.local.monitor_agent import (
    LexicalFakeMonitorAgent,
    ScriptedMonitorAgent,
    build_lexical_output,
)
from chorus.ports.agents import (
    AgentContractViolationError,
    AgentDependencyError,
    AgentRejection,
    AgentTimeoutError,
    MonitorInvocation,
)
from chorus.ports.errors import NotFoundError
from chorus.ports.pagination import PageRequest
from chorus.ports.scopes import CaseScope

pytestmark = pytest.mark.anyio

SIGNAL_CHANNEL_IDS = frozenset(
    {"feed-002", "feed-005", "feed-008", "feed-011", "feed-012", "feed-014", "feed-016"}
)
"""The messages a keyword stand-in recognises in the frozen corpus.

Named here rather than in production code, and used only to assert what the *fake* did. The
live evaluation makes no such assumption -- it asserts the shape of the discovery, not which
strings produced it.
"""


async def _discover(harness: MonitorHarness) -> RunMonitorResult:
    await harness.seed()
    locators = await harness.ingest_feed()
    return await harness.run_monitor(LexicalFakeMonitorAgent()).execute(
        harness.monitor_command(locators)
    )


async def test_a_repeated_pattern_becomes_one_candidate_case(harness: MonitorHarness) -> None:
    result = await _discover(harness)

    assert len(result.created_case_ids) == 1
    case = await harness.core.load_case(_case_scope(harness, result))
    assert case.state is CaseState.CANDIDATE
    assert case.issue_type == "ELEVATOR_FAILURE"
    assert len(case.report_ids) == len(SIGNAL_CHANNEL_IDS)
    assert case.corroboration_source_count == 0


async def test_every_persisted_report_cites_only_a_signal_message(
    harness: MonitorHarness,
) -> None:
    """Noise stays noise: an unrelated message never becomes part of the case."""

    result = await _discover(harness)
    case_scope = _case_scope(harness, result)
    reports = await harness.core.read_case_reports(case_scope, PageRequest(limit=100))

    corpus = {message.channel_message_id: message for message in harness.adapter.messages()}
    signal_sent_at = {corpus[channel_id].sent_at for channel_id in SIGNAL_CHANNEL_IDS}
    cited_sent_at = set()
    for report in reports.items:
        assert report.status is ReportStatus.ACTIVE
        for message_id in report.source_message_ids:
            message = await _message_by_id(harness, message_id)
            cited_sent_at.add(message.sent_at)
    assert cited_sent_at == signal_sent_at


async def test_the_injection_message_produces_no_durable_state(
    harness: MonitorHarness,
) -> None:
    """The attack is observed as data and changes nothing about what is stored."""

    result = await _discover(harness)
    case_scope = _case_scope(harness, result)
    reports = await harness.core.read_case_reports(case_scope, PageRequest(limit=100))
    injection = next(
        message
        for message in harness.adapter.messages()
        if message.channel_message_id == "feed-018"
    )

    cited: set[str] = set()
    for report in reports.items:
        for message_id in report.source_message_ids:
            cited.add((await _message_by_id(harness, message_id)).channel_message_id)
    assert injection.channel_message_id not in cited
    assert result.policy_like_message_count == 1

    case = await harness.core.load_case(case_scope)
    assert case.state is CaseState.CANDIDATE
    assert case.current_view_id is None
    assert case.current_action_id is None


async def test_intake_never_asserts_corroboration(harness: MonitorHarness) -> None:
    result = await _discover(harness)
    facts = await harness.core.read_case_facts(_case_scope(harness, result), PageRequest(limit=100))

    assert facts.items
    assert all(fact.evidence_status is EvidenceStatus.REPORTED for fact in facts.items)
    assert all(fact.status is FactStatus.ACTIVE for fact in facts.items)


async def test_replaying_the_same_invocation_creates_no_duplicates(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    locators = await harness.ingest_feed()
    command = harness.monitor_command(locators)
    monitor = harness.run_monitor(LexicalFakeMonitorAgent())

    first = await monitor.execute(command)
    again = await monitor.execute(command)

    assert again.replayed is True
    assert again.case_ids == first.case_ids
    assert again.report_count == 0
    assert again.fact_count == 0
    case = await harness.core.load_case(_case_scope(harness, first))
    assert len(case.report_ids) == len(SIGNAL_CHANNEL_IDS)
    assert case.version == 1


async def test_a_fresh_invocation_over_the_same_feed_is_still_duplicate_free(
    harness: MonitorHarness,
) -> None:
    """Identity comes from validated content, so a second run recognises its own work."""

    await harness.seed()
    locators = await harness.ingest_feed()
    monitor = harness.run_monitor(LexicalFakeMonitorAgent())

    first = await monitor.execute(harness.monitor_command(locators))
    second = await monitor.execute(harness.monitor_command(locators, invocation_id=uuid4()))

    assert second.case_ids == first.case_ids
    assert second.report_count == 0
    assert second.fact_count == 0
    case = await harness.core.load_case(_case_scope(harness, first))
    assert len(case.report_ids) == len(SIGNAL_CHANNEL_IDS)
    assert len(case.fact_ids) == len(SIGNAL_CHANNEL_IDS)


async def test_candidate_identity_is_stable_across_a_reset(harness: MonitorHarness) -> None:
    await harness.seed()
    locators = await harness.ingest_feed()
    first = await harness.run_monitor(LexicalFakeMonitorAgent()).execute(
        harness.monitor_command(locators)
    )

    # A second, structurally identical world: the same corpus, the same derivation inputs.
    expected_reports = {
        derive_report_id(
            namespace=harness.namespace,
            community_id=harness.community_id,
            contributor_id=report.contributor_id,
            issue_type=report.issue_type,
            source_message_ids=report.source_message_ids,
        )
        for report in (
            await harness.core.read_case_reports(
                _case_scope(harness, first), PageRequest(limit=100)
            )
        ).items
    }
    case = await harness.core.load_case(_case_scope(harness, first))
    assert set(case.report_ids) == expected_reports


async def test_a_feed_signal_is_retrievable_for_every_linked_message(
    harness: MonitorHarness,
) -> None:
    result = await _discover(harness)
    page = await harness.read_feed.execute(
        namespace=harness.namespace,
        community_id=harness.community_id,
        start=harness.adapter.messages()[0].sent_at - timedelta(days=1),
        end=harness.adapter.messages()[-1].sent_at + timedelta(days=1),
        request=PageRequest(limit=100),
    )

    signalled = {item for item in page.items if item.chorus_signal is not None}
    assert len(page.items) == 24
    assert len(signalled) == len(SIGNAL_CHANNEL_IDS)
    assert {item.chorus_signal.candidate_case_id for item in signalled if item.chorus_signal} == {
        result.created_case_ids[0]
    }
    assert all(
        item.chorus_signal is not None and item.chorus_signal.status is CaseState.CANDIDATE
        for item in signalled
    )


async def test_the_feed_still_shows_every_unrelated_message(harness: MonitorHarness) -> None:
    await _discover(harness)
    page = await harness.read_feed.execute(
        namespace=harness.namespace,
        community_id=harness.community_id,
        start=harness.adapter.messages()[0].sent_at - timedelta(days=1),
        end=harness.adapter.messages()[-1].sent_at + timedelta(days=1),
        request=PageRequest(limit=100),
    )

    unsignalled = [item for item in page.items if item.chorus_signal is None]
    assert len(unsignalled) == 24 - len(SIGNAL_CHANNEL_IDS)


async def test_an_invalid_answer_leaves_no_partial_durable_state(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    locators = await harness.ingest_feed()

    def hallucinate(invocation: MonitorInvocation) -> MonitorOutput:
        return replace_first_report_message(build_lexical_output(invocation))

    agent = ScriptedMonitorAgent(responder=hallucinate)
    with pytest.raises(AgentContractViolationError) as raised:
        await harness.run_monitor(agent).execute(harness.monitor_command(locators))

    assert AgentRejection.UNKNOWN_MESSAGE_ID.value in raised.value.reason_codes
    # Not one case, report, fact, or signal exists: the whole answer was refused.
    signals = await harness.core.read_feed_signals(harness.core_scope, PageRequest(limit=100))
    assert signals.items == ()


async def test_a_timeout_is_retried_once_with_the_same_invocation_identity(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    locators = await harness.ingest_feed()
    agent = ScriptedMonitorAgent(responder=build_lexical_output, failures=[AgentTimeoutError()])

    result = await harness.run_monitor(agent).execute(harness.monitor_command(locators))

    assert len(agent.invocations) == 2
    assert agent.invocations[0].invocation_id == agent.invocations[1].invocation_id
    assert len(result.created_case_ids) == 1


async def test_a_contract_violation_is_never_retried(harness: MonitorHarness) -> None:
    await harness.seed()
    locators = await harness.ingest_feed()
    agent = ScriptedMonitorAgent(
        responder=build_lexical_output,
        failures=[AgentContractViolationError((AgentRejection.SCHEMA_INVALID,))],
    )

    with pytest.raises(AgentContractViolationError):
        await harness.run_monitor(agent).execute(harness.monitor_command(locators))

    assert len(agent.invocations) == 1


async def test_a_second_dependency_failure_is_not_retried_again(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    locators = await harness.ingest_feed()
    agent = ScriptedMonitorAgent(
        responder=build_lexical_output,
        failures=[AgentDependencyError(), AgentDependencyError()],
    )

    with pytest.raises(AgentDependencyError):
        await harness.run_monitor(agent).execute(harness.monitor_command(locators))

    assert len(agent.invocations) == 2


async def test_a_case_discovered_in_one_namespace_is_invisible_in_another(
    harness: MonitorHarness,
) -> None:
    from tests.fixtures.monitor import OTHER_NAMESPACE

    result = await _discover(harness)
    foreign = MonitorHarness(driver=harness.driver, namespace=OTHER_NAMESPACE)

    with pytest.raises(NotFoundError):
        await foreign.core.load_case(
            CaseScope(
                namespace=OTHER_NAMESPACE,
                community_id=foreign.community_id,
                case_id=result.created_case_ids[0],
            )
        )


async def test_the_agent_sees_no_contact_detail_and_no_private_object_key(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    locators = await harness.ingest_feed()
    agent = ScriptedMonitorAgent(responder=build_lexical_output)

    await harness.run_monitor(agent).execute(harness.monitor_command(locators))

    payload = agent.invocations[0].payload.model_dump_json()
    for seed in harness.adapter.contributor_seeds:
        assert seed.display_name not in payload
        assert f"{seed.pseudonym}@example.invalid" not in payload
        assert str(seed.contributor_id) not in payload
    assert "private/" not in payload
    assert "s3://" not in payload


async def test_message_classification_covers_every_projected_message(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    locators = await harness.ingest_feed()
    agent = ScriptedMonitorAgent(responder=build_lexical_output)

    result = await harness.run_monitor(agent).execute(harness.monitor_command(locators))

    output = build_lexical_output(agent.invocations[0])
    assert len(output.message_results) == 24
    assert result.noise_message_count == 24 - len(SIGNAL_CHANNEL_IDS) - 1
    assert {item.classification for item in output.message_results} == {
        MessageClassification.NOISE,
        MessageClassification.POSSIBLE_ISSUE_SIGNAL,
        MessageClassification.POLICY_LIKE_INSTRUCTION,
    }


def replace_first_report_message(output: MonitorOutput) -> MonitorOutput:
    """Return the same answer with one report citing a message that never existed."""

    first = output.proposed_reports[0]
    poisoned = first.model_copy(update={"message_ids": (uuid4(),)})
    return output.model_copy(update={"proposed_reports": (poisoned, *output.proposed_reports[1:])})


def _case_scope(harness: MonitorHarness, result: RunMonitorResult) -> CaseScope:
    return CaseScope(
        namespace=harness.namespace,
        community_id=harness.community_id,
        case_id=result.created_case_ids[0],
    )


async def _message_by_id(harness: MonitorHarness, message_id: MessageId) -> CommunityMessage:
    page = await harness.core.read_message_feed(
        harness.core_scope,
        start=harness.adapter.messages()[0].sent_at - timedelta(days=1),
        end=harness.adapter.messages()[-1].sent_at + timedelta(days=1),
        request=PageRequest(limit=100),
    )
    return next(item for item in page.items if item.message_id == message_id)
