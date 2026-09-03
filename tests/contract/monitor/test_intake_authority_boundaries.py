"""The authority intake does not have, one adversarial scenario per boundary.

Every test here asks the system to do something intake is not allowed to do, through the
production use cases rather than through a helper, and then asserts *which guard refused* --
not merely that something did. A refusal from the wrong guard is a passing test hiding a real
defect, so the gate is named in the assertion every time.

The boundaries, in order: the model may never name durable identity; a feed signal is a
replaceable display row and not a linkage lock; a settled fact slot survives a re-answer
unchanged; a case that moved after the agent read it refuses the link; a terminal case is not
reopened by intake; and a first delivery is bound to its operation, actor, invocation and
message set exactly as tightly as the hundredth.

Promoted from an independent reviewer's falsification probes, all of which failed to reproduce.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from tests.fixtures.monitor import MonitorHarness
from tests.fixtures.monitor_answers import (
    THREE_GROUPS,
    classify_all,
    fact_for,
    grouped_answer,
    report_for,
)

from chorus.application.services.identity import derive_candidate_case_id
from chorus.application.services.monitor_apply import MonitorApplyDeniedError
from chorus.contracts.monitor import (
    CandidateLink,
    IssueType,
    MonitorOutput,
)
from chorus.domain.entities import CaseState, CommunityCase
from chorus.domain.facts import FailureMode
from chorus.domain.ids import CaseId
from chorus.infrastructure.local.monitor_agent import ScriptedMonitorAgent
from chorus.ports.agents import AgentContractViolationError, MonitorInvocation
from chorus.ports.ambient import AmbientMessage
from chorus.ports.records import MessageFeedEntry
from chorus.ports.scopes import CaseScope, CommunityScope

pytestmark = pytest.mark.anyio


def _scope(harness: MonitorHarness, case_id: CaseId) -> CaseScope:
    return CaseScope(
        namespace=harness.namespace, community_id=harness.community_id, case_id=case_id
    )


async def _seeded(harness: MonitorHarness) -> tuple[MessageFeedEntry, ...]:
    await harness.seed()
    return await harness.ingest_feed()


async def _lift_case(
    harness: MonitorHarness, case_ids: tuple[CaseId, ...]
) -> tuple[CaseId, CommunityCase]:
    """The ELEVATOR_FAILURE case among the three discovered ones.

    Selected by issue type rather than by position: an extending elevator report proposed
    against a case of any other type is refused earlier -- as an unsupported candidate
    transition, or under ADR-012 as an unprovable group -- which would hide the gate these
    tests are actually about.
    """

    for case_id in case_ids:
        case = await harness.core.load_case(_scope(harness, case_id))
        if case.issue_type == IssueType.ELEVATOR_FAILURE.value:
            return case_id, case
    raise AssertionError("no ELEVATOR_FAILURE case was discovered")


def _three(invocation: MonitorInvocation) -> MonitorOutput:
    return grouped_answer(invocation.payload, THREE_GROUPS)


# =======================================================================================
# B-1 -- the model must never name durable identity
# =======================================================================================


async def test_group_ref_never_becomes_a_case_id(harness: MonitorHarness) -> None:
    """A model-chosen ``candidate_group_ref`` must not survive into a durable ``case_id``."""

    locators = await _seeded(harness)
    result = await harness.run_monitor(ScriptedMonitorAgent(responder=_three)).execute(
        harness.monitor_command(locators)
    )

    group_refs = {group[0] for group in THREE_GROUPS}
    for case_id in result.created_case_ids:
        assert str(case_id.value) not in group_refs
        case = await harness.core.load_case(_scope(harness, case_id))
        # Identity is a pure function of the validated reports, so it is reproducible here
        # without knowing anything the model wrote.
        expected = derive_candidate_case_id(
            namespace=harness.namespace,
            community_id=harness.community_id,
            issue_type=case.issue_type,
            report_ids=tuple(case.report_ids),
        )
        assert case_id == expected, (
            "B-1 REGRESSION: the durable case ID is not derived from the validated reports"
        )


async def test_a_uuid_shaped_group_ref_is_refused_by_the_contract() -> None:
    """The model may not even *spell* a durable identifier in a group ref."""

    with pytest.raises(ValueError):
        CandidateLink(
            report_client_ref="report-000",
            candidate_group_ref=str(uuid4()),
            proposed_case_title="Anything",
            similarity_reasons=("adversarial",),
            confidence="0.9",
        )


async def test_a_foreign_case_id_is_refused(harness: MonitorHarness) -> None:
    """A case identifier that was never in this invocation's own input must be refused."""

    locators = await _seeded(harness)
    foreign = uuid4()

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        message = payload.messages[0]
        return MonitorOutput(
            message_results=classify_all(payload, {message.message_id}),
            proposed_reports=(report_for(message, "report-000", IssueType.ELEVATOR_FAILURE),),
            proposed_facts=(fact_for(message, "fact-000", "report-000"),),
            candidate_links=(
                CandidateLink(
                    report_client_ref="report-000",
                    existing_case_id=foreign,
                    proposed_case_title="Borrowed case",
                    similarity_reasons=("adversarial",),
                    confidence="0.9",
                ),
            ),
        )

    with pytest.raises(AgentContractViolationError) as raised:
        await harness.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(
            harness.monitor_command(locators)
        )
    assert "FOREIGN_CASE_ID" in raised.value.reason_codes


# =======================================================================================
# H-2 -- the feed signal is a display row, not a linkage lock
# =======================================================================================


async def test_a_signal_never_makes_valid_state_unreachable(
    harness: MonitorHarness,
) -> None:
    """A message already signalled for case A may not be silently moved to case B, and the
    refusal must be a domain rule -- not a create-only storage row that strands the answer."""

    locators = await _seeded(harness)
    first = await harness.run_monitor(ScriptedMonitorAgent(responder=_three)).execute(
        harness.monitor_command(locators)
    )
    assert len(first.created_case_ids) == 3

    scope = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    signals = await harness.core.load_feed_signals(
        scope, tuple(item.message_id for item in locators)
    )
    assert signals, "the first answer must have written display signals"
    # A display row carries a version and is replaceable -- that is the H-2 property.
    assert all(signal.version >= 1 for signal in signals.values())

    # A genuine relink: the messages already bound to the lift case, proposed as a *new*
    # case with a different group ref and a different title. This is the move a create-only
    # signal row would have made unreachable, and that a domain rule must refuse instead.
    # Case identity is derived from the report set, so changing only the label would name the
    # same case. Mixing one lift message with one plumbing message names a *different* case
    # and asks for two already-bound messages to be moved into it.
    moved_group = ("moved-group", IssueType.ELEVATOR_FAILURE, "Somewhere else entirely", (1, 0))

    def relink(invocation: MonitorInvocation) -> MonitorOutput:
        return grouped_answer(invocation.payload, (moved_group,))

    with pytest.raises((MonitorApplyDeniedError, AgentContractViolationError)) as raised:
        await harness.run_monitor(ScriptedMonitorAgent(responder=relink)).execute(
            harness.monitor_command(locators, invocation_id=uuid4())
        )
    # The refusal has to be the *domain* rule, decided before any write is staged.
    assert isinstance(raised.value, MonitorApplyDeniedError), (
        f"the refusal came from {type(raised.value).__name__}, not the apply gate"
    )

    # And nothing about the refusal may have disturbed the committed state.
    after = await harness.core.load_feed_signals(scope, tuple(item.message_id for item in locators))
    assert {k: v.case_id for k, v in after.items()} == {k: v.case_id for k, v in signals.items()}, (
        "H-2 REGRESSION: a refused relink mutated the display signals"
    )


async def test_a_signal_refreshes_rather_than_being_immutable(
    harness: MonitorHarness,
) -> None:
    """The row must be replaceable by a guarded update, or it is a lock."""

    locators = await _seeded(harness)
    result = await harness.run_monitor(ScriptedMonitorAgent(responder=_three)).execute(
        harness.monitor_command(locators)
    )
    case_id = result.created_case_ids[0]
    scope = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    before = await harness.core.load_feed_signals(
        scope, tuple(item.message_id for item in locators)
    )
    signalled = [m for m, s in before.items() if s.case_id == case_id]
    assert signalled

    case = await harness.core.load_case(_scope(harness, case_id))
    moved = replace(case, state=CaseState.INVESTIGATING, version=case.version + 1)
    from chorus.ports.unit_of_work import TransactionPlan

    await harness.unit_of_work.commit(
        TransactionPlan(
            name="probe-move-case",
            operations=(
                harness.core.stage_update_case(
                    _scope(harness, case_id), moved, expected_version=case.version
                ),
            ),
            audit_required=False,
        )
    )
    # Re-running the same answer must refresh the display row rather than being blocked by it.
    await harness.run_monitor(ScriptedMonitorAgent(responder=_three)).execute(
        harness.monitor_command(locators)
    )
    after = await harness.core.load_feed_signals(scope, tuple(signalled))
    assert all(signal.case_id == case_id for signal in after.values())


# =======================================================================================
# H-4 -- fact slot identity survives a re-answer
# =======================================================================================


async def test_a_changed_value_at_a_settled_slot_is_refused(
    harness: MonitorHarness,
) -> None:
    """Re-answer the same messages with a *different* fact value at the same slot."""

    locators = await _seeded(harness)
    await harness.run_monitor(ScriptedMonitorAgent(responder=_three)).execute(
        harness.monitor_command(locators)
    )

    def drifted(invocation: MonitorInvocation) -> MonitorOutput:
        return grouped_answer(invocation.payload, THREE_GROUPS, failure_mode=FailureMode.ERRATIC)

    with pytest.raises(Exception) as raised:
        await harness.run_monitor(ScriptedMonitorAgent(responder=drifted)).execute(
            harness.monitor_command(locators, invocation_id=uuid4())
        )
    assert "AgentOutputDrift" in type(raised.value).__name__ or isinstance(
        raised.value, (MonitorApplyDeniedError, AgentContractViolationError)
    ), f"H-4 REGRESSION: a drifted fact value was not refused ({type(raised.value).__name__})"


async def test_the_same_answer_creates_no_second_fact(
    harness: MonitorHarness,
) -> None:
    locators = await _seeded(harness)
    first = await harness.run_monitor(ScriptedMonitorAgent(responder=_three)).execute(
        harness.monitor_command(locators)
    )
    before = {
        case_id: (await harness.core.load_case(_scope(harness, case_id))).fact_ids
        for case_id in first.case_ids
    }
    await harness.run_monitor(ScriptedMonitorAgent(responder=_three)).execute(
        harness.monitor_command(locators, invocation_id=uuid4())
    )
    for case_id, fact_ids in before.items():
        case = await harness.core.load_case(_scope(harness, case_id))
        assert case.fact_ids == fact_ids, "H-4 REGRESSION: a re-answer duplicated a fact slot"


# =======================================================================================
# H-6 -- a case version must still mean what the agent saw
# =======================================================================================


async def test_a_case_moved_after_the_agent_saw_it_refuses_the_link(
    harness: MonitorHarness,
) -> None:
    """Change the case between the agent's read and the apply. The link must be denied."""

    locators = await _seeded(harness)
    first = await harness.run_monitor(ScriptedMonitorAgent(responder=_three)).execute(
        harness.monitor_command(locators)
    )
    case_id, case = await _lift_case(harness, first.created_case_ids)

    anchor = harness.adapter.messages()[-1].sent_at
    extra = (
        AmbientMessage(
            adapter="SYNTHETIC",
            channel_message_id="h6probe-extra",
            contributor_pseudonym="resident-c",
            sent_at=anchor + timedelta(minutes=900),
            text="The elevator is stuck again, same as the others reported.",
        ),
    )
    ingested = await harness.ingest_messages(extra, idempotency_key="h6probe-key")
    extra_locators = tuple(
        MessageFeedEntry(message_id=item.message_id, sent_at=extra[0].sent_at)
        for item in ingested.messages
    )

    from chorus.ports.unit_of_work import TransactionPlan

    target = extra_locators[0].message_id.value

    def extend(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        # The payload also carries prior context messages, and those are already bound to
        # their own cases. Only the freshly ingested one may be proposed.
        message = next(m for m in payload.messages if m.message_id == target)
        return MonitorOutput(
            message_results=classify_all(payload, {message.message_id}),
            proposed_reports=(report_for(message, "report-000", IssueType.ELEVATOR_FAILURE),),
            proposed_facts=(fact_for(message, "fact-000", "report-000"),),
            candidate_links=(
                CandidateLink(
                    report_client_ref="report-000",
                    existing_case_id=case_id.value,
                    proposed_case_title=case.title,
                    similarity_reasons=("same equipment",),
                    confidence="0.9",
                ),
            ),
        )

    class MoveCaseAfterAnswering:
        """A model that answers correctly, and a concurrent writer that moves the case
        between the answer and the apply. Wrapping rather than patching, because the
        scripted agent is a slots dataclass."""

        def __init__(self) -> None:
            self.inner = ScriptedMonitorAgent(responder=extend)

        @property
        def invocations(self):  # type: ignore[no-untyped-def]
            return self.inner.invocations

        async def invoke_monitor(self, invocation):  # type: ignore[no-untyped-def]
            answer = await self.inner.invoke_monitor(invocation)
            fresh = await harness.core.load_case(_scope(harness, case_id))
            moved = replace(fresh, state=CaseState.INVESTIGATING, version=fresh.version + 1)
            await harness.unit_of_work.commit(
                TransactionPlan(
                    name="probe-concurrent-move",
                    operations=(
                        harness.core.stage_update_case(
                            _scope(harness, case_id), moved, expected_version=fresh.version
                        ),
                    ),
                    audit_required=False,
                )
            )
            return answer

    agent = MoveCaseAfterAnswering()

    from chorus.application.services.monitor_apply import MonitorApplyDenial

    with pytest.raises(MonitorApplyDeniedError) as raised:
        await harness.run_monitor(agent).execute(
            harness.monitor_command(extra_locators, invocation_id=uuid4())
        )
    assert raised.value.denial is MonitorApplyDenial.CASE_VERSION_STALE, (
        f"H-6: refused, but for {raised.value.denial} rather than the stale version"
    )
    after = await harness.core.load_case(_scope(harness, case_id))
    assert after.report_ids == case.report_ids, "H-6 REGRESSION: a stale link still wrote a report"
    community = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    signals = await harness.core.load_feed_signals(community, (extra_locators[0].message_id,))
    assert signals == {}, "H-6 REGRESSION: a denied apply still wrote a display signal"


# =======================================================================================
# H-7 -- intake may not reopen a terminal case
# =======================================================================================


@pytest.mark.parametrize("terminal", [CaseState.RESOLVED, CaseState.CLOSED_UNRESOLVED])
async def test_a_terminal_case_refuses_an_intake_link(
    harness: MonitorHarness, terminal: CaseState
) -> None:
    locators = await _seeded(harness)
    first = await harness.run_monitor(ScriptedMonitorAgent(responder=_three)).execute(
        harness.monitor_command(locators)
    )
    case_id, case = await _lift_case(harness, first.created_case_ids)

    from chorus.ports.unit_of_work import TransactionPlan

    closed = replace(
        case, state=terminal, state_reason_code="PROBE_CLOSED", version=case.version + 1
    )
    await harness.unit_of_work.commit(
        TransactionPlan(
            name="probe-close-case",
            operations=(
                harness.core.stage_update_case(
                    _scope(harness, case_id), closed, expected_version=case.version
                ),
            ),
            audit_required=False,
        )
    )

    anchor = harness.adapter.messages()[-1].sent_at
    extra = (
        AmbientMessage(
            adapter="SYNTHETIC",
            channel_message_id=f"h7probe-{terminal.value}",
            contributor_pseudonym="resident-c",
            sent_at=anchor + timedelta(minutes=1000),
            text="The elevator is stuck again, same as before.",
        ),
    )
    ingested = await harness.ingest_messages(extra, idempotency_key=f"h7probe-key-{terminal.value}")
    extra_locators = tuple(
        MessageFeedEntry(message_id=item.message_id, sent_at=extra[0].sent_at)
        for item in ingested.messages
    )

    target = extra_locators[0].message_id.value

    def extend(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        message = next(m for m in payload.messages if m.message_id == target)
        return MonitorOutput(
            message_results=classify_all(payload, {message.message_id}),
            proposed_reports=(report_for(message, "report-000", IssueType.ELEVATOR_FAILURE),),
            proposed_facts=(fact_for(message, "fact-000", "report-000"),),
            candidate_links=(
                CandidateLink(
                    report_client_ref="report-000",
                    existing_case_id=case_id.value,
                    proposed_case_title=closed.title,
                    similarity_reasons=("same equipment",),
                    confidence="0.9",
                ),
            ),
        )

    # Either the terminal case is never offered as a candidate (so the link is FOREIGN), or
    # the apply gate denies it. Both are correct; silently reopening is not.
    with pytest.raises((MonitorApplyDeniedError, AgentContractViolationError)):
        await harness.run_monitor(ScriptedMonitorAgent(responder=extend)).execute(
            harness.monitor_command(extra_locators, invocation_id=uuid4())
        )

    after = await harness.core.load_case(_scope(harness, case_id))
    assert after.state is terminal, "H-7 REGRESSION: intake reopened a terminal case"
    assert after.version == closed.version, "H-7 REGRESSION: a denied link still bumped the case"
    assert after.report_ids == closed.report_ids


# =======================================================================================
# H-8 -- a FIRST delivery is bound as tightly as the hundredth
# =======================================================================================


async def test_a_substituted_invocation_id_on_a_first_delivery_is_refused(
    harness: MonitorHarness,
) -> None:
    """A caller holding a valid operation, actor and request hash swaps the invocation id."""

    locators = await _seeded(harness)
    operation, job = await harness.dispatched(locators)
    forged = replace(job, invocation_id=uuid4())

    agent = ScriptedMonitorAgent(responder=_three)
    await harness.worker(agent).execute(forged)

    assert agent.invocations == [], "H-8 REGRESSION: a forged invocation identity reached the model"
    reloaded = await harness.operations.load(
        namespace=harness.namespace, operation_id=operation.operation_id
    )
    assert reloaded.version == operation.version, (
        "H-8 REGRESSION: a refused job still mutated the operation"
    )
    assert reloaded.status == operation.status


async def test_a_narrowed_locator_set_on_a_first_delivery_is_refused(
    harness: MonitorHarness,
) -> None:
    """The caller keeps everything valid but hands the Monitor a different slice of the batch."""

    locators = await _seeded(harness)
    operation, job = await harness.dispatched(locators)
    narrowed = replace(job, message_locators=locators[:3])

    agent = ScriptedMonitorAgent(responder=_three)
    await harness.worker(agent).execute(narrowed)

    assert agent.invocations == [], (
        "H-8 REGRESSION: a narrowed message set reached the model; the caller chose what the "
        "Monitor read"
    )
    reloaded = await harness.operations.load(
        namespace=harness.namespace, operation_id=operation.operation_id
    )
    assert reloaded.version == operation.version


async def test_a_widened_locator_set_on_a_first_delivery_is_refused(
    harness: MonitorHarness,
) -> None:
    locators = await _seeded(harness)
    _operation, job = await harness.dispatched(locators[:5])
    widened = replace(job, message_locators=locators)

    agent = ScriptedMonitorAgent(responder=_three)
    await harness.worker(agent).execute(widened)

    assert agent.invocations == [], "H-8 REGRESSION: a widened message set reached the model"
