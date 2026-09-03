"""Every exploit the independent review found, reproduced and closed, against both drivers.

Each test here is named for the behaviour it protects rather than for the finding it came from,
but the mapping is one to one and deliberate: bounded apply progress that a partial failure can
resume, a feed signal that is a display row rather than a lock, candidate groups that keep two
unrelated ``OTHER`` problems apart, a fact slot that survives a re-answer, a case version that
still means what the agent saw, a terminal case that intake cannot reopen, an apply that changes
nothing changing nothing, an invocation that is never spent twice, and a below-threshold
observation that a later run can still find.

They run through the production use case with a scripted agent, so the projection, validator,
planner, gates, transactions, and feed all execute. Only the model is substituted, which is the
only part of the path that cannot be made to answer on demand.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from tests.fixtures.faults import (
    FaultInjectingDriver,
    TransactBehaviour,
    monitor_apply_steps,
)
from tests.fixtures.monitor import FIXTURE_ID_NAMESPACE, MonitorHarness
from tests.fixtures.monitor_answers import (
    THREE_GROUPS,
    UNPROVABLE_OTHER_GROUP,
    GroupSpec,
    classify_all,
    fact_for,
    grouped_answer,
    report_for,
    whole_span,
)

from chorus.application.commands.run_monitor import MonitorApplyInterruptedError
from chorus.application.services.monitor_apply import (
    CANDIDATE_EXTENDED_REASON_CODE,
    MONITOR_APPLY_ITEM_BUDGET,
    MonitorApplyDenial,
    MonitorApplyDeniedError,
)
from chorus.contracts.monitor import (
    MAX_PROPOSED_FACTS,
    MAX_PROPOSED_REPORTS,
    CandidateLink,
    IncidentOccurrenceValue,
    IssueType,
    LocationAreaValue,
    ManagementStatementValue,
    MonitorFactValue,
    MonitorInput,
    MonitorMessage,
    MonitorMessageResult,
    MonitorOutput,
    MonitorSourceSpan,
    ProposedFact,
    ProposedReport,
    ServiceImpactValue,
)
from chorus.domain.entities import CaseState, CommunityCase, FactType, SensitivityCategory
from chorus.domain.facts import FailureMode, ImpactCode, LocationAreaCode
from chorus.domain.ids import CaseId
from chorus.infrastructure.local.monitor_agent import LexicalFakeMonitorAgent, ScriptedMonitorAgent
from chorus.ports.agents import (
    AgentContractViolationError,
    AgentError,
    AgentOutputDriftError,
    MonitorInvocation,
)
from chorus.ports.ambient import AmbientMessage
from chorus.ports.limits import TRANSACTION_MAX_OPERATIONS
from chorus.ports.pagination import PageRequest
from chorus.ports.records import AgentInvocationOutcome, MessageFeedEntry
from chorus.ports.scopes import CaseScope, CommunityScope, OperationScope
from chorus.ports.storage import PutItem
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------------------
# Scripted answers
# ---------------------------------------------------------------------------------------
#
# The builders live in ``tests.fixtures.monitor_answers`` so this suite and the second
# correction suite cannot drift into two different notions of "a well-formed answer".


def _whole_span(message: MonitorMessage) -> MonitorSourceSpan:
    return whole_span(message)


def _grouped_answer(
    payload: MonitorInput,
    groups: tuple[GroupSpec, ...],
    *,
    failure_mode: FailureMode = FailureMode.STUCK,
) -> MonitorOutput:
    return grouped_answer(payload, groups, failure_mode=failure_mode)


def _classify_all(payload: MonitorInput, signals: set[UUID]) -> tuple[MonitorMessageResult, ...]:
    return classify_all(payload, signals)


def _report_for(message: MonitorMessage, ref: str, issue: IssueType) -> ProposedReport:
    return report_for(message, ref, issue)


def _fact_for(
    message: MonitorMessage,
    ref: str,
    report_ref: str,
    *,
    failure_mode: FailureMode = FailureMode.STUCK,
) -> ProposedFact:
    return fact_for(message, ref, report_ref, failure_mode=failure_mode)


async def _seeded(harness: MonitorHarness) -> tuple[MessageFeedEntry, ...]:
    await harness.seed()
    return await harness.ingest_feed()


# ---------------------------------------------------------------------------------------
# H3 -- issue type is not a candidate group
# ---------------------------------------------------------------------------------------


async def test_three_unrelated_problems_become_three_cases(harness: MonitorHarness) -> None:
    locators = await _seeded(harness)
    agent = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS)
    )

    result = await harness.run_monitor(agent).execute(harness.monitor_command(locators))

    assert len(result.created_case_ids) == 3
    titles = set()
    issue_types = []
    for case_id in result.created_case_ids:
        case = await harness.core.load_case(_scope(harness, case_id))
        titles.add(case.title)
        issue_types.append(case.issue_type)
    assert titles == {group[2] for group in THREE_GROUPS}
    assert issue_types == ["ELEVATOR_FAILURE"] * 3, (
        "every group shares one issue type, so three cases proves the group ref separated them"
    )


async def test_two_problems_sharing_an_issue_type_never_collapse_into_one_case(
    harness: MonitorHarness,
) -> None:
    """The precise regression: same issue type, different group, must stay separate."""

    locators = await _seeded(harness)
    two_groups = THREE_GROUPS[1:]
    agent = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(invocation.payload, two_groups)
    )

    result = await harness.run_monitor(agent).execute(harness.monitor_command(locators))

    assert len(result.created_case_ids) == 2
    reports_per_case = []
    for case_id in result.created_case_ids:
        case = await harness.core.load_case(_scope(harness, case_id))
        reports_per_case.append(len(case.report_ids))
    assert reports_per_case == [2, 2]


async def test_two_other_reports_under_one_group_ref_are_refused(
    harness: MonitorHarness,
) -> None:
    """The other half of the same regression, after ADR-012.

    Two unrelated problems the vocabulary can only call ``OTHER`` no longer become two cases
    -- they become none, and the answer proposing the merge is refused whole. A distinct group
    ref is what keeps two *named* problems apart; it is not a licence to merge two unnamed
    ones.
    """

    locators = await _seeded(harness)
    agent = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(invocation.payload, UNPROVABLE_OTHER_GROUP)
    )

    with pytest.raises(AgentContractViolationError) as raised:
        await harness.run_monitor(agent).execute(harness.monitor_command(locators))
    assert "CANDIDATE_GROUP_UNPROVABLE" in raised.value.reason_codes


async def _ingest_pair(
    harness: MonitorHarness, *, first: tuple[str, str, str], second: tuple[str, str, str]
) -> tuple[MessageFeedEntry, ...]:
    """Ingest two custom messages, each ``(channel_id, pseudonym, text)``, for a grouping probe."""

    await harness.seed()
    anchor = harness.adapter.messages()[-1].sent_at
    batch = (
        AmbientMessage(
            adapter="SYNTHETIC",
            channel_message_id=first[0],
            contributor_pseudonym=first[1],
            sent_at=anchor + timedelta(minutes=100),
            text=first[2],
        ),
        AmbientMessage(
            adapter="SYNTHETIC",
            channel_message_id=second[0],
            contributor_pseudonym=second[1],
            sent_at=anchor + timedelta(minutes=101),
            text=second[2],
        ),
    )
    result = await harness.ingest_messages(batch, idempotency_key=f"{first[0]}-{second[0]}-key")
    return tuple(
        MessageFeedEntry(message_id=item.message_id, sent_at=message.sent_at)
        for item, message in zip(result.messages, batch, strict=True)
    )


async def test_two_unrelated_other_incidents_with_a_shared_vague_title_are_refused(
    harness: MonitorHarness,
) -> None:
    """H-3, at the exact shape the reviewer reproduced.

    A plumbing leak and a low-pressure complaint are unrelated problems that a careless or
    adversarial model can file under one ``candidate_group_ref`` and one title. Both checks
    the validator enforced before ADR-012 pass -- issue type agrees, title agrees -- because
    the model chose both. The location the earlier repair compared is supplied here, and
    supplied *identically*, so this also proves the refusal no longer depends on it.
    """

    locators = await _ingest_pair(
        harness,
        first=(
            "h3-plumbing",
            "resident-a",
            "The kitchen sink in the shared laundry room has been leaking badly for two days.",
        ),
        second=(
            "h3-pressure",
            "resident-b",
            "Water pressure in my unit has been extremely low all week, barely a trickle.",
        ),
    )

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        first, second = payload.messages[0], payload.messages[1]
        reports = tuple(
            _report_for(message, ref, IssueType.OTHER).model_copy(
                update={"location_area": LocationAreaCode.BUILDING}
            )
            for message, ref in ((first, "report-000"), (second, "report-001"))
        )
        links = tuple(
            CandidateLink(
                report_client_ref=ref,
                candidate_group_ref="misc-group",
                proposed_case_title="General building issue",
                similarity_reasons=("adversarial merge",),
                confidence="0.9",
            )
            for ref in ("report-000", "report-001")
        )
        return MonitorOutput(
            message_results=_classify_all(payload, {first.message_id, second.message_id}),
            proposed_reports=reports,
            proposed_facts=(
                _fact_for(first, "fact-000", "report-000"),
                _fact_for(second, "fact-001", "report-001"),
            ),
            candidate_links=links,
        )

    agent = ScriptedMonitorAgent(responder=responder)

    with pytest.raises(AgentContractViolationError) as raised:
        await harness.run_monitor(agent).execute(harness.monitor_command(locators))

    assert "CANDIDATE_GROUP_UNPROVABLE" in raised.value.reason_codes
    assert "CANDIDATE_GROUP_LOCATION_REQUIRED" not in raised.value.reason_codes, (
        "the withdrawn interim rule must not still be deciding anything"
    )


async def test_two_related_reports_under_a_named_issue_type_still_group(
    harness: MonitorHarness,
) -> None:
    """The fix must not over-restrict. Grouping still works -- under a word that names it.

    The counterpart is deliberate: two reports just as alike, filed as ``OTHER``, are refused
    by :func:`test_two_genuinely_related_other_reports_are_still_separated`. What decides is
    the vocabulary, not the model's prose about how alike the reports are.
    """

    locators = await _ingest_pair(
        harness,
        first=(
            "h3-lift-1",
            "resident-a",
            "The lift stopped between floors again this morning and the doors would not open.",
        ),
        second=(
            "h3-lift-2",
            "resident-b",
            "Confirming the lift is stuck again, same fault my neighbour reported earlier.",
        ),
    )

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        first, second = payload.messages[0], payload.messages[1]
        links = tuple(
            CandidateLink(
                report_client_ref=ref,
                candidate_group_ref="lift-group",
                proposed_case_title="Lift stopping between floors",
                similarity_reasons=("same equipment, same fault",),
                confidence="0.9",
            )
            for ref in ("report-000", "report-001")
        )
        return MonitorOutput(
            message_results=_classify_all(payload, {first.message_id, second.message_id}),
            proposed_reports=(
                _report_for(first, "report-000", IssueType.ELEVATOR_FAILURE),
                _report_for(second, "report-001", IssueType.ELEVATOR_FAILURE),
            ),
            proposed_facts=(
                _fact_for(first, "fact-000", "report-000"),
                _fact_for(second, "fact-001", "report-001"),
            ),
            candidate_links=links,
        )

    agent = ScriptedMonitorAgent(responder=responder)
    result = await harness.run_monitor(agent).execute(harness.monitor_command(locators))

    assert len(result.created_case_ids) == 1
    case = await harness.core.load_case(_scope(harness, result.created_case_ids[0]))
    assert len(case.report_ids) == 2


async def test_two_genuinely_related_other_reports_are_still_separated(
    harness: MonitorHarness,
) -> None:
    """The cost of the invariant, asserted rather than left implicit.

    These two residents really are describing one broken intercom. Deterministic code cannot
    tell that from an answer that would read identically if they were not, so the merge is
    refused: false separation, deliberately preferred to false merging (ADR-012). The remedy
    is a vocabulary that can name the problem, not a validator that guesses.
    """

    locators = await _ingest_pair(
        harness,
        first=(
            "h3-lobby-1",
            "resident-a",
            "The lobby intercom has been broken for a week, nobody can buzz visitors in.",
        ),
        second=(
            "h3-lobby-2",
            "resident-b",
            "Confirming the lobby intercom is still dead, same problem my neighbour reported.",
        ),
    )

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        first, second = payload.messages[0], payload.messages[1]
        reports = tuple(
            _report_for(message, ref, IssueType.OTHER).model_copy(
                update={"location_area": LocationAreaCode.LOBBY}
            )
            for message, ref in ((first, "report-000"), (second, "report-001"))
        )
        links = tuple(
            CandidateLink(
                report_client_ref=ref,
                candidate_group_ref="lobby-group",
                proposed_case_title="Broken lobby intercom",
                similarity_reasons=("same equipment, same area",),
                confidence="0.9",
            )
            for ref in ("report-000", "report-001")
        )
        return MonitorOutput(
            message_results=_classify_all(payload, {first.message_id, second.message_id}),
            proposed_reports=reports,
            proposed_facts=(
                _fact_for(first, "fact-000", "report-000"),
                _fact_for(second, "fact-001", "report-001"),
            ),
            candidate_links=links,
        )

    with pytest.raises(AgentContractViolationError) as raised:
        await harness.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(
            harness.monitor_command(locators)
        )
    assert "CANDIDATE_GROUP_UNPROVABLE" in raised.value.reason_codes


async def test_one_group_disagreeing_with_itself_denies_the_whole_answer(
    harness: MonitorHarness,
) -> None:
    locators = await _seeded(harness)

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        output = _grouped_answer(invocation.payload, THREE_GROUPS[:1])
        # Same group reference, a different title. Which case did the model mean?
        poisoned = output.candidate_links[1].model_copy(
            update={"proposed_case_title": "Something entirely different"}
        )
        return output.model_copy(update={"candidate_links": (output.candidate_links[0], poisoned)})

    agent = ScriptedMonitorAgent(responder=responder)

    with pytest.raises(AgentContractViolationError) as raised:
        await harness.run_monitor(agent).execute(harness.monitor_command(locators))

    assert "CANDIDATE_GROUP_INCONSISTENT" in raised.value.reason_codes


async def test_a_link_naming_both_a_case_and_a_group_is_refused_by_the_contract(
    harness: MonitorHarness,
) -> None:
    """Exactly one destination per link, enforced before anything reaches the validator."""

    with pytest.raises(ValueError, match="exactly one"):
        CandidateLink(
            report_client_ref="report-001",
            existing_case_id=uuid4(),
            candidate_group_ref="lift-group",
            proposed_case_title="Both at once",
            similarity_reasons=("because",),
            confidence="0.5",
        )
    with pytest.raises(ValueError, match="exactly one"):
        CandidateLink(
            report_client_ref="report-001",
            proposed_case_title="Neither",
            similarity_reasons=("because",),
            confidence="0.5",
        )


# ---------------------------------------------------------------------------------------
# H4 -- fact slot identity survives a re-answer
# ---------------------------------------------------------------------------------------


async def test_the_same_messages_answered_again_create_no_second_fact(
    harness: MonitorHarness,
) -> None:
    locators = await _seeded(harness)
    agent = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS[:1])
    )
    runner = harness.run_monitor(agent)

    first = await runner.execute(harness.monitor_command(locators))
    scope = _scope(harness, first.created_case_ids[0])
    before = await harness.core.load_case(scope)

    second = await runner.execute(harness.monitor_command(locators, invocation_id=uuid4()))
    after = await harness.core.load_case(scope)

    assert second.fact_count == 0
    assert len(after.fact_ids) == len(before.fact_ids)


async def test_a_changed_fact_value_at_a_settled_slot_is_refused_not_duplicated(
    harness: MonitorHarness,
) -> None:
    """One slot, two answers. Neither a second fact nor an overwritten first."""

    locators = await _seeded(harness)
    first_agent = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS[:1])
    )
    first = await harness.run_monitor(first_agent).execute(harness.monitor_command(locators))
    scope = _scope(harness, first.created_case_ids[0])
    before = await harness.core.load_case(scope)
    stored = await harness.core.load_facts(scope, before.fact_ids)

    drifting = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(
            invocation.payload, THREE_GROUPS[:1], failure_mode=FailureMode.OUT_OF_SERVICE
        )
    )

    with pytest.raises(AgentOutputDriftError) as raised:
        await harness.run_monitor(drifting).execute(
            harness.monitor_command(locators, invocation_id=uuid4())
        )

    assert "AGENT_OUTPUT_DRIFT" in raised.value.reason_codes
    after = await harness.core.load_case(scope)
    assert after.fact_ids == before.fact_ids
    assert await harness.core.load_facts(scope, after.fact_ids) == stored


async def test_a_reordered_answer_is_recognised_as_the_same_answer(
    harness: MonitorHarness,
) -> None:
    locators = await _seeded(harness)
    forward = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS[:1])
    )
    first = await harness.run_monitor(forward).execute(harness.monitor_command(locators))

    def reversed_responder(invocation: MonitorInvocation) -> MonitorOutput:
        output = _grouped_answer(invocation.payload, THREE_GROUPS[:1])
        return output.model_copy(
            update={
                "proposed_reports": tuple(reversed(output.proposed_reports)),
                "proposed_facts": tuple(reversed(output.proposed_facts)),
                "candidate_links": tuple(reversed(output.candidate_links)),
            }
        )

    second = await harness.run_monitor(ScriptedMonitorAgent(responder=reversed_responder)).execute(
        harness.monitor_command(locators, invocation_id=uuid4())
    )

    assert second.created_case_ids == ()
    assert second.report_count == 0
    assert second.fact_count == 0
    assert second.case_ids == first.created_case_ids


# ---------------------------------------------------------------------------------------
# H6 / H7 -- stale case version and terminal case
# ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class _AgentThatMovesTheWorld:
    """Answers, and then advances the case before the answer can be applied.

    This is the only honest way to reproduce the race. The agent must genuinely *see* version
    N -- the summary it is given proves that -- and the case must genuinely become N+1 before
    the apply step reads it. Bumping the case first and calling that a stale version would
    only prove the gate rejects something the agent never saw.
    """

    harness: MonitorHarness
    scope: CaseScope
    seen_versions: list[int] = field(default_factory=list)
    inner: ScriptedMonitorAgent = field(init=False)

    def __post_init__(self) -> None:
        self.inner = ScriptedMonitorAgent(responder=_extend_existing_case)

    async def invoke_monitor(self, invocation: MonitorInvocation) -> object:
        self.seen_versions.extend(
            summary.case_version for summary in invocation.payload.candidate_case_summaries
        )
        answer = await self.inner.invoke_monitor(invocation)
        current = await self.harness.core.load_case(self.scope)
        await _write_case(
            self.harness,
            self.scope,
            replace(current, version=current.version + 1, updated_at=self.harness.clock.now()),
            expected_version=current.version,
        )
        return answer


async def test_a_case_that_changed_after_the_agent_saw_it_rejects_the_link(
    harness: MonitorHarness,
) -> None:
    scope, extra_locator = await _case_and_new_message(harness)
    case = await harness.core.load_case(scope)
    agent = _AgentThatMovesTheWorld(harness=harness, scope=scope)

    with pytest.raises(MonitorApplyDeniedError) as raised:
        await harness.run_monitor(agent).execute(  # type: ignore[arg-type]
            harness.monitor_command((extra_locator,), invocation_id=uuid4())
        )

    assert raised.value.denial is MonitorApplyDenial.CASE_VERSION_STALE
    assert agent.seen_versions == [case.version], "the agent really did see the older version"
    unchanged = await harness.core.load_case(scope)
    assert unchanged.version == case.version + 1, "only the concurrent writer moved it"
    assert unchanged.report_ids == case.report_ids
    assert unchanged.fact_ids == case.fact_ids

    # And nothing at all was written for the attempt: no new report, no signal, no audit row.
    community = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    signals = await harness.core.load_feed_signals(community, (extra_locator.message_id,))
    assert signals == {}


async def test_a_resolved_case_refuses_a_monitor_linked_report(
    harness: MonitorHarness,
) -> None:
    scope, extra_locator = await _case_and_new_message(harness)
    case = await harness.core.load_case(scope)
    resolved = replace(
        case,
        state=CaseState.RESOLVED,
        state_reason_code="HUMAN_VERIFIED_FULFILLED",
        version=case.version + 1,
        updated_at=harness.clock.now(),
    )
    await _write_case(harness, scope, resolved, expected_version=case.version)

    agent = ScriptedMonitorAgent(responder=_extend_existing_case)

    # A terminal case is not offered as a candidate summary at all, so the scripted answer has
    # nothing to link to and every report it proposes is a *new* group -- which the threshold
    # then refuses on its own. Either way the resolved case is untouched.
    result = await harness.run_monitor(agent).execute(
        harness.monitor_command((extra_locator,), invocation_id=uuid4())
    )

    assert scope.case_id not in result.case_ids
    after = await harness.core.load_case(scope)
    assert after.state is CaseState.RESOLVED
    assert after.version == resolved.version
    assert after.state_reason_code == "HUMAN_VERIFIED_FULFILLED"
    assert after.report_ids == resolved.report_ids


@pytest.mark.parametrize("state", [CaseState.RESOLVED, CaseState.CLOSED_UNRESOLVED])
async def test_a_terminal_case_is_never_offered_to_the_agent(
    harness: MonitorHarness, state: CaseState
) -> None:
    scope, extra_locator = await _case_and_new_message(harness)
    case = await harness.core.load_case(scope)
    await _write_case(
        harness,
        scope,
        replace(case, state=state, version=case.version + 1, updated_at=harness.clock.now()),
        expected_version=case.version,
    )

    seen: list[tuple[CaseId, ...]] = []

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        seen.append(
            tuple(
                CaseId(summary.case_id) for summary in invocation.payload.candidate_case_summaries
            )
        )
        return _grouped_answer(invocation.payload, ())

    await harness.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(
        harness.monitor_command((extra_locator,), invocation_id=uuid4())
    )

    assert seen == [()]


# ---------------------------------------------------------------------------------------
# H2 -- the feed signal is a display row, not a lock
# ---------------------------------------------------------------------------------------


async def test_replaying_an_answer_writes_no_second_signal(harness: MonitorHarness) -> None:
    locators = await _seeded(harness)
    agent = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS[:1])
    )
    runner = harness.run_monitor(agent)
    first = await runner.execute(harness.monitor_command(locators))
    community = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    before = await harness.core.read_feed_signals(community, PageRequest(limit=100))

    await runner.execute(harness.monitor_command(locators, invocation_id=uuid4()))

    after = await harness.core.read_feed_signals(community, PageRequest(limit=100))
    assert len(after.items) == len(before.items)
    assert {signal.version for signal in after.items} == {1}
    assert all(signal.case_id in first.created_case_ids for signal in after.items)


async def test_a_signal_refreshes_when_its_case_display_state_changes(
    harness: MonitorHarness,
) -> None:
    """A create-only row could never catch up; a versioned one can, under a condition."""

    locators = await _seeded(harness)
    agent = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS[:1])
    )
    result = await harness.run_monitor(agent).execute(harness.monitor_command(locators))
    scope = _scope(harness, result.created_case_ids[0])
    community = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    signals = await harness.core.read_feed_signals(community, PageRequest(limit=100))
    stale = signals.items[0]

    await harness.unit_of_work.commit(
        _plan(
            harness.core.stage_update_feed_signal(
                community,
                replace(stale, label="An out-of-date label", version=stale.version + 1),
                expected_version=stale.version,
            )
        )
    )

    refreshed = await harness.core.load_feed_signals(community, (stale.message_id,))
    assert refreshed[stale.message_id].label == "An out-of-date label"
    case = await harness.core.load_case(scope)
    assert case.title != "An out-of-date label", "the case itself is unaffected by its projection"


async def test_a_message_bound_to_one_case_cannot_be_moved_to_another(
    harness: MonitorHarness,
) -> None:
    """Phase-3 Monitor cannot relink, and it fails before writing anything at all."""

    locators = await _seeded(harness)
    first = await harness.run_monitor(
        ScriptedMonitorAgent(
            responder=lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS[:1])
        )
    ).execute(harness.monitor_command(locators))
    original_case = first.created_case_ids[0]

    # A second answer files one of those already-bound messages into a different new group.
    # Case identity is derived from the report set, so pairing message 1 with an unbound
    # message names a *different* candidate case and asks for message 1 to be moved into it.
    regrouped = (
        ("different-group", IssueType.ELEVATOR_FAILURE, "A different reading entirely", (1, 0)),
    )

    with pytest.raises(MonitorApplyDeniedError) as raised:
        await harness.run_monitor(
            ScriptedMonitorAgent(
                responder=lambda invocation: _grouped_answer(invocation.payload, regrouped)
            )
        ).execute(harness.monitor_command(locators, invocation_id=uuid4()))

    assert raised.value.denial is MonitorApplyDenial.REPORT_ALREADY_LINKED
    community = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    signals = await harness.core.read_feed_signals(community, PageRequest(limit=100))
    assert {signal.case_id for signal in signals.items} == {original_case}


# ---------------------------------------------------------------------------------------
# M1 -- a no-op apply changes nothing
# ---------------------------------------------------------------------------------------


async def test_a_new_invocation_adding_nothing_does_not_bump_the_case(
    harness: MonitorHarness,
) -> None:
    locators = await _seeded(harness)
    agent = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS[:1])
    )
    runner = harness.run_monitor(agent)
    first = await runner.execute(harness.monitor_command(locators))
    scope = _scope(harness, first.created_case_ids[0])
    before = await harness.core.load_case(scope)

    # A genuinely new invocation identity, so nothing is short-circuited as a recorded replay.
    result = await runner.execute(harness.monitor_command(locators, invocation_id=uuid4()))

    after = await harness.core.load_case(scope)
    assert result.report_count == 0
    assert result.fact_count == 0
    assert after.version == before.version
    assert after.updated_at == before.updated_at
    assert after.state_reason_code == before.state_reason_code


async def test_an_extension_that_does_add_something_still_bumps_the_case(
    harness: MonitorHarness,
) -> None:
    """The no-op rule must not have turned into "never bump"."""

    scope, extra_locator = await _case_and_new_message(harness)
    before = await harness.core.load_case(scope)

    await harness.run_monitor(LexicalFakeMonitorAgent()).execute(
        harness.monitor_command((extra_locator,), invocation_id=uuid4())
    )

    after = await harness.core.load_case(scope)
    assert after.version > before.version
    assert after.state_reason_code == CANDIDATE_EXTENDED_REASON_CODE
    assert len(after.report_ids) == len(before.report_ids) + 1


# ---------------------------------------------------------------------------------------
# M3 -- the model is never asked twice for one invocation
# ---------------------------------------------------------------------------------------


async def test_a_recorded_invocation_replays_without_calling_the_model(
    harness: MonitorHarness,
) -> None:
    locators = await _seeded(harness)
    agent = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS[:1])
    )
    runner = harness.run_monitor(agent)
    command = harness.monitor_command(locators)

    first = await runner.execute(command)
    calls_after_first = len(agent.invocations)

    second = await runner.execute(command)

    assert calls_after_first == 1
    assert len(agent.invocations) == 1, "a redelivered invocation must call no model at all"
    assert second.replayed is True
    assert set(second.case_ids) == set(first.case_ids)


async def test_a_recorded_invocation_reused_for_different_work_is_a_conflict(
    harness: MonitorHarness,
) -> None:
    locators = await _seeded(harness)
    agent = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS[:1])
    )
    runner = harness.run_monitor(agent)
    command = harness.monitor_command(locators)
    await runner.execute(command)

    # Same invocation identity, a different set of messages. The frozen input records which
    # command it was built for, so this is refused rather than quietly answered from somebody
    # else's payload.
    with pytest.raises(AgentContractViolationError):
        await runner.execute(
            harness.monitor_command(locators[:2], invocation_id=command.invocation_id)
        )
    assert len(agent.invocations) == 1


async def test_a_failed_invocation_is_recorded_and_replays_its_failure(
    harness: MonitorHarness,
) -> None:
    """Domain unchanged, safe code durable, and no second model call on redelivery."""

    locators = await _seeded(harness)

    def broken(invocation: MonitorInvocation) -> MonitorOutput:
        output = _grouped_answer(invocation.payload, THREE_GROUPS[:1])
        return output.model_copy(update={"message_results": output.message_results[:1]})

    agent = ScriptedMonitorAgent(responder=broken)
    runner = harness.run_monitor(agent)
    command = harness.monitor_command(locators)

    with pytest.raises(AgentContractViolationError):
        await runner.execute(command)

    operation_scope = OperationScope(namespace=harness.namespace, operation_id=command.operation_id)
    record = await harness.core.load_operation_agent_invocation(
        operation_scope, command.invocation_id
    )
    assert record is not None
    assert record.outcome.value == "FAILED"
    assert record.failure_code == "AGENT_CONTRACT_VIOLATION"
    assert record.output_hash is None
    assert record.case_id is None

    # A redelivery replays the recorded failure and calls no model. It is not re-raised as a
    # *retryable* failure either: the one licensed retry belonged to the original attempt.
    with pytest.raises(AgentError) as replayed:
        await runner.execute(command)
    assert replayed.value.retryable is False
    assert "AGENT_CONTRACT_VIOLATION" in replayed.value.reason_codes
    assert len(agent.invocations) == 1


# ---------------------------------------------------------------------------------------
# H1 -- bounded apply progress
# ---------------------------------------------------------------------------------------


async def test_every_apply_step_stays_under_the_transaction_limit(
    harness: MonitorHarness,
) -> None:
    locators = await _seeded(harness)
    agent = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS)
    )
    faulty = FaultInjectingDriver(inner=harness.driver, script=[], read_script=[])
    instrumented = MonitorHarness(driver=faulty, namespace=harness.namespace)

    await instrumented.run_monitor(agent).execute(instrumented.monitor_command(locators))

    assert faulty.transact_calls >= 3, "one bounded transaction per case at minimum"


async def test_a_failure_after_one_committed_step_resumes_the_rest(
    harness: MonitorHarness,
) -> None:
    """Partial durable progress is permitted; partial acceptance of an answer is not."""

    locators = await _seeded(harness)
    responder = lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS)  # noqa: E731
    faulty = FaultInjectingDriver(
        inner=harness.driver,
        # The first apply step commits, then the transport dies. The second is never attempted.
        script=[TransactBehaviour.SUCCEED, TransactBehaviour.DEFINITE_FAILURE],
        read_script=[],
        scripted=monitor_apply_steps,
    )
    partial = MonitorHarness(driver=faulty, namespace=harness.namespace)
    command = partial.monitor_command(locators)

    with pytest.raises(MonitorApplyInterruptedError):
        await partial.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(command)

    operation_scope = OperationScope(namespace=harness.namespace, operation_id=command.operation_id)
    progress = await harness.core.load_monitor_progress(operation_scope, command.invocation_id)
    assert progress is not None
    assert progress.completed_steps == 1
    assert progress.total_steps >= 3

    # A clean retry of the same invocation finishes only what is missing.
    resumed = MonitorHarness(driver=harness.driver, namespace=harness.namespace)
    result = await resumed.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(
        resumed.monitor_command(locators, operation_id=command.operation_id)
    )

    assert len(result.case_ids) == 3
    finished = await harness.core.load_monitor_progress(operation_scope, command.invocation_id)
    assert finished is not None
    assert finished.is_complete

    # And nothing was written twice.
    for case_id in result.case_ids:
        case = await harness.core.load_case(_scope(harness, case_id))
        assert len(case.report_ids) == len(set(case.report_ids)) == 2
        assert len(case.fact_ids) == len(set(case.fact_ids)) == 2
        events = await harness.audit.read_case_events(
            _scope(harness, case_id), PageRequest(limit=100)
        )
        assert len(events.items) == 1, "one decision, one audit row, however many attempts"


async def test_progress_counts_every_step_this_invocation_ever_committed(
    harness: MonitorHarness,
) -> None:
    """Cumulative across attempts, not per-attempt. A resumed run continues the count.

    The count includes the finalization step, which is why three planned case-steps make a
    four-step plan. That is the point of counting it: ``is_complete`` then means "the durable
    successful invocation record exists", not "the last data row landed".
    """

    locators = await _seeded(harness)
    responder = lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS)  # noqa: E731
    faulty = FaultInjectingDriver(
        inner=harness.driver,
        script=[TransactBehaviour.SUCCEED, TransactBehaviour.DEFINITE_FAILURE],
        read_script=[],
        scripted=monitor_apply_steps,
    )
    partial = MonitorHarness(driver=faulty, namespace=harness.namespace)
    command = partial.monitor_command(locators)
    operation_scope = OperationScope(namespace=harness.namespace, operation_id=command.operation_id)

    with pytest.raises(MonitorApplyInterruptedError):
        await partial.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(command)
    first = await harness.core.load_monitor_progress(operation_scope, command.invocation_id)
    assert first is not None
    assert (first.completed_steps, first.version) == (1, 1)

    resumed = MonitorHarness(driver=harness.driver, namespace=harness.namespace)
    await resumed.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(
        resumed.monitor_command(locators, operation_id=command.operation_id)
    )

    final = await harness.core.load_monitor_progress(operation_scope, command.invocation_id)
    assert final is not None
    assert (final.completed_steps, final.total_steps) == (4, 4), "three data steps, then finalize"
    assert final.version == 4, "the row advanced under its own version, once per step"
    assert final.is_complete
    record = await harness.core.load_operation_agent_invocation(
        operation_scope, command.invocation_id
    )
    assert record is not None and record.outcome is AgentInvocationOutcome.SUCCEEDED, (
        "complete progress means the successful invocation record is already durable"
    )


async def test_a_resumed_attempt_cannot_reach_a_different_answer_at_all(
    harness: MonitorHarness,
) -> None:
    """The mutation-sensitive half, strengthened: there is no second answer to disagree with.

    Progress says "this invocation has committed N steps", which is only meaningful against the
    answer those steps came from. The earlier design defended that by *detecting* a second,
    different answer under one invocation identity; this one makes a second answer impossible,
    because the validated plan is snapshotted before step one and a resumed attempt loads it
    instead of invoking anything.

    So the assertion is about the model, not about the hash: an agent that would have answered
    differently is never asked, and the work that completes is the work the first answer
    described.
    """

    locators = await _seeded(harness)
    faulty = FaultInjectingDriver(
        inner=harness.driver,
        script=[TransactBehaviour.SUCCEED, TransactBehaviour.DEFINITE_FAILURE],
        read_script=[],
        scripted=monitor_apply_steps,
    )
    partial = MonitorHarness(driver=faulty, namespace=harness.namespace)
    command = partial.monitor_command(locators)

    with pytest.raises(MonitorApplyInterruptedError):
        await partial.run_monitor(
            ScriptedMonitorAgent(
                responder=lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS)
            )
        ).execute(command)

    # A second attempt under the same invocation identity, wired to an agent that would answer
    # differently if it were ever asked.
    contradicting = ScriptedMonitorAgent(
        responder=lambda invocation: _grouped_answer(invocation.payload, THREE_GROUPS[1:])
    )
    resumed = MonitorHarness(driver=harness.driver, namespace=harness.namespace)
    result = await resumed.run_monitor(contradicting).execute(
        resumed.monitor_command(locators, operation_id=command.operation_id)
    )

    assert contradicting.invocations == [], "a frozen plan is applied, never re-answered"
    assert len(result.case_ids) == 3, "the first answer's plan is what finished"
    operation_scope = OperationScope(namespace=harness.namespace, operation_id=command.operation_id)
    progress = await harness.core.load_monitor_progress(operation_scope, command.invocation_id)
    assert progress is not None
    assert progress.is_complete


async def test_the_apply_item_budget_leaves_room_for_its_own_overhead() -> None:
    """A step is items plus case row, progress, proof, and -- once per case -- record and audit."""

    assert MONITOR_APPLY_ITEM_BUDGET + 5 < TRANSACTION_MAX_OPERATIONS


async def test_a_contract_maximum_answer_never_attempts_one_oversized_transaction(
    harness: MonitorHarness,
) -> None:
    """H-1's exact adversarial scenario: one answer at the frozen Monitor maxima.

    Twenty-five reports and a hundred facts, every one of them one new candidate case. A
    single transaction for that group would need roughly the reports, the signals, and the
    facts, plus the case row, progress advance, commit proof, and -- once -- the invocation
    record and audit event: comfortably past DynamoDB's hundred-operation limit. This drives
    the real planner and the real storage driver at that size and asserts every transaction it
    actually issues, not just the arithmetic that predicts them.
    """

    await harness.seed()
    anchor = harness.adapter.messages()[-1].sent_at

    batch = tuple(
        AmbientMessage(
            adapter="SYNTHETIC",
            channel_message_id=f"h1-max-{index:03d}",
            contributor_pseudonym="resident-a",
            sent_at=anchor + timedelta(minutes=200 + index),
            text=f"The elevator is stuck again, report number {index}.",
        )
        for index in range(MAX_PROPOSED_REPORTS)
    )
    ingested = await harness.ingest_messages(batch, idempotency_key="h1-max-key")
    locators = tuple(
        MessageFeedEntry(message_id=item.message_id, sent_at=message.sent_at)
        for item, message in zip(ingested.messages, batch, strict=True)
    )
    facts_per_report = MAX_PROPOSED_FACTS // MAX_PROPOSED_REPORTS
    assert facts_per_report * MAX_PROPOSED_REPORTS == MAX_PROPOSED_FACTS, "the probe must be exact"

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        reports: list[ProposedReport] = []
        facts: list[ProposedFact] = []
        links: list[CandidateLink] = []
        signals: set[UUID] = set()

        def _typed_values(message: MonitorMessage) -> tuple[MonitorFactValue, ...]:
            return (
                IncidentOccurrenceValue(
                    fact_type=FactType.INCIDENT_OCCURRENCE,
                    occurred_at=message.sent_at,
                    failure_mode=FailureMode.STUCK,
                ),
                ServiceImpactValue(
                    fact_type=FactType.SERVICE_IMPACT,
                    impact_code=ImpactCode.DELAY,
                    summary="Residents delayed getting to work.",
                ),
                LocationAreaValue(
                    fact_type=FactType.LOCATION_AREA, area=LocationAreaCode.ELEVATOR_CAB
                ),
                ManagementStatementValue(
                    fact_type=FactType.MANAGEMENT_STATEMENT,
                    statement="Management is aware and dispatched a technician.",
                    speaker_org="Building management",
                    stated_at=message.sent_at,
                ),
            )

        for index, message in enumerate(payload.messages):
            signals.add(message.message_id)
            report_ref = f"report-{index:03d}"
            reports.append(_report_for(message, report_ref, IssueType.ELEVATOR_FAILURE))
            span = _whole_span(message)
            for slot, typed_value in enumerate(_typed_values(message)[:facts_per_report]):
                facts.append(
                    ProposedFact(
                        client_ref=f"fact-{index:03d}-{slot}",
                        report_client_ref=report_ref,
                        fact_type=typed_value.fact_type,
                        typed_value=typed_value,
                        sensitivity=SensitivityCategory.GENERAL,
                        source_spans=(span,),
                    )
                )
            links.append(
                CandidateLink(
                    report_client_ref=report_ref,
                    candidate_group_ref="max-group",
                    proposed_case_title="Recurring lift failures at contract maximum",
                    similarity_reasons=("same equipment",),
                    confidence="0.9",
                )
            )
        return MonitorOutput(
            message_results=_classify_all(payload, signals),
            proposed_reports=tuple(reports),
            proposed_facts=tuple(facts),
            candidate_links=tuple(links),
        )

    faulty = FaultInjectingDriver(inner=harness.driver, script=[])
    instrumented = MonitorHarness(driver=faulty, namespace=harness.namespace)
    agent = ScriptedMonitorAgent(responder=responder)

    result = await instrumented.run_monitor(agent).execute(instrumented.monitor_command(locators))

    assert len(result.created_case_ids) == 1
    case = await harness.core.load_case(_scope(harness, result.created_case_ids[0]))
    assert len(case.report_ids) == MAX_PROPOSED_REPORTS
    assert len(case.fact_ids) == MAX_PROPOSED_FACTS

    assert len(faulty.transact_sizes) > 1, "the contract maximum must span more than one step"
    assert max(faulty.transact_sizes) <= TRANSACTION_MAX_OPERATIONS, (
        f"a single transaction of {max(faulty.transact_sizes)} operations exceeds the frozen "
        f"{TRANSACTION_MAX_OPERATIONS}-operation limit -- this is the exact defect H-1 reported"
    )


# ---------------------------------------------------------------------------------------
# M13 -- a below-threshold observation stays findable
# ---------------------------------------------------------------------------------------


async def test_a_lone_report_creates_nothing_but_is_reconsidered_later(
    harness: MonitorHarness,
) -> None:
    """Run one finds one relevant message and files nothing. Run two finds the pattern.

    The second run must see the first run's message, which it only can because the context
    window reaches back past the batch boundary. Nothing about the corpus is hard-coded: the
    messages are whichever two the scripted answer points at.
    """

    await harness.seed()
    corpus = harness.adapter.messages()
    sent_at = {message.channel_message_id: message.sent_at for message in corpus}

    first_batch = await harness.ingest_messages(corpus[:1], idempotency_key="lone-key-000001")
    first_locators = tuple(
        MessageFeedEntry(message_id=item.message_id, sent_at=sent_at[item.channel_message_id])
        for item in first_batch.messages
    )

    single = (("lift-group", IssueType.ELEVATOR_FAILURE, "Recurring lift failures", (0,)),)
    lone = await harness.run_monitor(
        ScriptedMonitorAgent(
            responder=lambda invocation: _grouped_answer(invocation.payload, single)
        )
    ).execute(harness.monitor_command(first_locators))

    assert lone.created_case_ids == ()
    assert lone.skipped_below_threshold == 1
    community = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    assert (await harness.core.read_feed_signals(community, PageRequest(limit=100))).items == ()

    # A later, corroborating message arrives. The window shows the Monitor both.
    second_batch = await harness.ingest_messages(corpus[1:2], idempotency_key="lone-key-000002")
    second_locators = tuple(
        MessageFeedEntry(message_id=item.message_id, sent_at=sent_at[item.channel_message_id])
        for item in second_batch.messages
    )

    seen: list[int] = []

    def pair(invocation: MonitorInvocation) -> MonitorOutput:
        seen.append(len(invocation.payload.messages))
        indices = tuple(range(len(invocation.payload.messages)))
        return _grouped_answer(
            invocation.payload,
            (("lift-group", IssueType.ELEVATOR_FAILURE, "Recurring lift failures", indices),),
        )

    formed = await harness.run_monitor(ScriptedMonitorAgent(responder=pair)).execute(
        harness.monitor_command(second_locators, invocation_id=uuid4())
    )

    assert seen == [2], "the earlier message is back in the context window"
    assert len(formed.created_case_ids) == 1
    case = await harness.core.load_case(_scope(harness, formed.created_case_ids[0]))
    assert len(case.report_ids) == 2


# ---------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------


def _scope(harness: MonitorHarness, case_id: CaseId) -> CaseScope:
    return CaseScope(
        namespace=harness.namespace, community_id=harness.community_id, case_id=case_id
    )


def _plan(operation: PutItem) -> TransactionPlan:
    return TransactionPlan(
        name="test-refresh-signal",
        operations=(operation,),
        audit_required=False,
    )


async def _write_case(
    harness: MonitorHarness, scope: CaseScope, case: CommunityCase, *, expected_version: int
) -> None:
    await harness.unit_of_work.commit(
        TransactionPlan(
            name="test-advance-case",
            operations=(
                harness.core.stage_update_case(scope, case, expected_version=expected_version),
            ),
            audit_required=False,
        )
    )


def _extend_existing_case(invocation: MonitorInvocation) -> MonitorOutput:
    """Report only the newest message, linking it to the first case summary offered.

    Deliberately conservative about the rest of the window. The context window also carries
    older messages that a previous run already reported, and re-proposing those would exercise
    the drift and relink gates rather than the version and state gates these tests are about.
    """

    payload = invocation.payload
    summary = payload.candidate_case_summaries[0] if payload.candidate_case_summaries else None
    newest = payload.messages[-1]
    reports: list[ProposedReport] = []
    facts: list[ProposedFact] = []
    links: list[CandidateLink] = []
    signals: set[UUID] = {newest.message_id}
    report_ref = "report-000"
    reports.append(_report_for(newest, report_ref, IssueType.ELEVATOR_FAILURE))
    facts.append(_fact_for(newest, "fact-000", report_ref))
    links.append(
        CandidateLink(
            report_client_ref=report_ref,
            existing_case_id=None if summary is None else summary.case_id,
            candidate_group_ref=None if summary is not None else "lift-group",
            proposed_case_title="Recurring lift failures" if summary is None else summary.title,
            similarity_reasons=("same equipment",),
            confidence="0.9",
        )
    )
    return MonitorOutput(
        message_results=_classify_all(payload, signals),
        proposed_reports=tuple(reports),
        proposed_facts=tuple(facts),
        candidate_links=tuple(links),
    )


async def _case_and_new_message(
    harness: MonitorHarness,
) -> tuple[CaseScope, MessageFeedEntry]:
    """A discovered case plus one freshly ingested message that could extend it."""

    locators = await _seeded(harness)
    result = await harness.run_monitor(LexicalFakeMonitorAgent()).execute(
        harness.monitor_command(locators)
    )
    scope = _scope(harness, result.created_case_ids[0])

    from chorus.ports.ambient import AmbientMessage

    extra = AmbientMessage(
        adapter="SYNTHETIC",
        channel_message_id="feed-900",
        contributor_pseudonym="resident-c",
        sent_at=harness.adapter.messages()[-1].sent_at,
        text="The lift is out of service again this morning.",
    )
    ingested = await harness.ingest_messages((extra,), idempotency_key="extend-key-000900")
    return scope, MessageFeedEntry(
        message_id=ingested.messages[0].message_id, sent_at=extra.sent_at
    )


def _fixture_uuid(label: str) -> UUID:
    return uuid5(FIXTURE_ID_NAMESPACE, label)
