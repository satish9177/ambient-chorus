"""Whole-output PERSISTENCE atomicity, attacked at every apply-step boundary.

Whole-output *validation* atomicity is proved elsewhere: an invalid answer is refused before
anything is written. The harder question is what a *valid* answer leaves behind when storage
fails halfway through applying it, because at the frozen contract maxima one answer cannot fit
in one transaction and partial durable progress is therefore the ordinary case, not an exotic
one.

So this file drives the real planner, the real transactions and the real worker, and fails at
every apply-step boundary in turn -- including finalization -- with both definite and ambiguous
storage outcomes. Two questions each time: what does an external reader see while the operation
is incomplete, and does a redelivery converge on exactly the state the uninterrupted run would
have produced.

Promoted from an independent reviewer's H-1 falsification probe, which failed to reproduce.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.fixtures.faults import (
    FaultInjectingDriver,
    TransactBehaviour,
    monitor_apply_steps,
)
from tests.fixtures.monitor import MonitorHarness
from tests.fixtures.monitor_answers import THREE_GROUPS, classify_all, fact_for, report_for

from chorus.application.commands.run_monitor import MonitorApplyInterruptedError
from chorus.contracts.monitor import (
    MAX_PROPOSED_FACTS,
    MAX_PROPOSED_REPORTS,
    CandidateLink,
    IncidentOccurrenceValue,
    IssueType,
    LocationAreaValue,
    ManagementStatementValue,
    MonitorFactValue,
    MonitorMessage,
    MonitorOutput,
    ProposedFact,
    ProposedReport,
    ServiceImpactValue,
)
from chorus.domain.entities import ApplicationOperationStatus, FactType, SensitivityCategory
from chorus.domain.facts import FailureMode, ImpactCode, LocationAreaCode
from chorus.domain.ids import CaseId
from chorus.infrastructure.local.monitor_agent import ScriptedMonitorAgent
from chorus.ports.agents import MonitorInvocation
from chorus.ports.ambient import AmbientMessage
from chorus.ports.limits import TRANSACTION_MAX_OPERATIONS
from chorus.ports.pagination import PageRequest
from chorus.ports.records import AgentInvocationOutcome, MessageFeedEntry
from chorus.ports.scopes import CaseScope, CommunityScope, OperationScope

pytestmark = pytest.mark.anyio


def _scope(harness: MonitorHarness, case_id: CaseId) -> CaseScope:
    return CaseScope(
        namespace=harness.namespace, community_id=harness.community_id, case_id=case_id
    )


async def _seeded(harness: MonitorHarness) -> tuple[MessageFeedEntry, ...]:
    await harness.seed()
    return await harness.ingest_feed()


def _responder(invocation: MonitorInvocation) -> MonitorOutput:
    from tests.fixtures.monitor_answers import grouped_answer

    return grouped_answer(invocation.payload, THREE_GROUPS)


# ---------------------------------------------------------------------------------------
# PROBE 1 -- failure at EVERY apply-step boundary, definite failure
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("fail_after", [0, 1, 2, 3])
async def test_definite_failure_at_each_boundary_resumes(
    harness: MonitorHarness, fail_after: int
) -> None:
    """step 1..N commit, step N+1 dies, the worker retries: what survives?

    ``fail_after=3`` puts the failure on the *finalization* transaction, which is the
    boundary the earlier design had outside progress entirely.
    """

    locators = await _seeded(harness)
    script = [TransactBehaviour.SUCCEED] * fail_after + [TransactBehaviour.DEFINITE_FAILURE]
    faulty = FaultInjectingDriver(
        inner=harness.driver,
        script=script,
        read_script=[],
        # Finalization writes no case row, so select every Monitor transaction that either
        # writes a case row or is the finalize transaction, by taking all transactions after
        # the two snapshot writes. Shape selection keeps step ordinals stable.
        scripted=lambda ops: monitor_apply_steps(ops) or _is_finalize(ops),
    )
    partial = MonitorHarness(driver=faulty, namespace=harness.namespace)
    command = partial.monitor_command(locators)
    operation_scope = OperationScope(namespace=harness.namespace, operation_id=command.operation_id)

    with pytest.raises(MonitorApplyInterruptedError):
        await partial.run_monitor(ScriptedMonitorAgent(responder=_responder)).execute(command)

    progress = await harness.core.load_monitor_progress(operation_scope, command.invocation_id)
    if fail_after == 0:
        # The progress row is written *inside* the first step's transaction, so a first-step
        # failure leaves no progress row at all. That is the correct shape, not a gap.
        assert progress is None
    else:
        assert progress is not None
        assert progress.completed_steps == fail_after, "progress equals exactly what committed"
        assert not progress.is_complete

    # An external reader must never see a case naming rows that are not there.
    await _readable_cases(harness, locators)

    # Redelivery: a different process, same invocation identity, an agent that would answer
    # differently if it were ever asked.
    contradicting = ScriptedMonitorAgent(
        responder=lambda inv: __import__(
            "tests.fixtures.monitor_answers", fromlist=["grouped_answer"]
        ).grouped_answer(inv.payload, THREE_GROUPS[1:])
    )
    resumed = MonitorHarness(driver=harness.driver, namespace=harness.namespace)
    result = await resumed.run_monitor(contradicting).execute(
        resumed.monitor_command(locators, operation_id=command.operation_id)
    )

    assert contradicting.invocations == [], "no second model pass over private text"
    assert len(result.case_ids) == 3
    final = await harness.core.load_monitor_progress(operation_scope, command.invocation_id)
    assert final is not None and final.is_complete
    record = await harness.core.load_operation_agent_invocation(
        operation_scope, command.invocation_id
    )
    assert record is not None and record.outcome is AgentInvocationOutcome.SUCCEEDED

    for case_id in result.case_ids:
        case = await harness.core.load_case(_scope(harness, case_id))
        assert len(case.report_ids) == len(set(case.report_ids)) == 2, "no duplicated report"
        assert len(case.fact_ids) == len(set(case.fact_ids)) == 2, "no duplicated fact"
        events = await harness.audit.read_case_events(
            _scope(harness, case_id), PageRequest(limit=100)
        )
        assert len(events.items) == 1, "one decision, one audit row"
        # every report the case names must actually exist
        for report_id in case.report_ids:
            await harness.core.load_report(_scope(harness, case_id), report_id)
        await harness.core.load_facts(_scope(harness, case_id), case.fact_ids)


def _is_finalize(ops: tuple[object, ...]) -> bool:
    """True for the finalization transaction: an operation-scoped invocation record write."""

    return any(
        getattr(getattr(op, "key", None), "sort_key", "").startswith("AGENT_INVOCATION#")
        and getattr(getattr(op, "key", None), "partition_key", "").find("OPERATION#") >= 0
        for op in ops
    )


async def _readable_cases(
    harness: MonitorHarness, locators: tuple[MessageFeedEntry, ...]
) -> set[CaseId]:
    """Assert every case any feed signal points at resolves all the rows it names.

    This is the externally visible coherence question: a reader who follows the feed to a
    case must never land on a case row naming a report or fact that is not there.
    """

    scope = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    signals = await harness.core.load_feed_signals(
        scope, tuple(item.message_id for item in locators)
    )
    case_ids = {signal.case_id for signal in signals.values()}
    for case_id in case_ids:
        case_scope = _scope(harness, case_id)
        case = await harness.core.load_case(case_scope)
        for report_id in case.report_ids:
            await harness.core.load_report(case_scope, report_id)  # raises NotFoundError
        await harness.core.load_facts(case_scope, case.fact_ids)  # raises NotFoundError
    return case_ids


# ---------------------------------------------------------------------------------------
# PROBE 2 -- ambiguous outcome (the write may or may not have landed) at each boundary
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "behaviour",
    [TransactBehaviour.AMBIGUOUS_AFTER_APPLY, TransactBehaviour.AMBIGUOUS_WITHOUT_APPLY],
)
@pytest.mark.parametrize("fail_after", [0, 1, 2])
async def test_ambiguous_outcome_at_each_boundary_resumes(
    harness: MonitorHarness, behaviour: TransactBehaviour, fail_after: int
) -> None:
    """The hard case: the worker cannot tell whether its transaction committed."""

    locators = await _seeded(harness)
    faulty = FaultInjectingDriver(
        inner=harness.driver,
        script=[TransactBehaviour.SUCCEED] * fail_after + [behaviour],
        read_script=[],
        scripted=lambda ops: monitor_apply_steps(ops) or _is_finalize(ops),
    )
    partial = MonitorHarness(driver=faulty, namespace=harness.namespace)
    command = partial.monitor_command(locators)
    operation_scope = OperationScope(namespace=harness.namespace, operation_id=command.operation_id)

    once = ScriptedMonitorAgent(responder=_responder)
    first = await partial.run_monitor(once).execute(command)

    # The ambiguity is RESOLVED in place by reading the plan's own commit proof, so the run
    # completes rather than raising. That is the property under test: an unknown outcome
    # neither duplicates a mutation nor abandons the apply.
    assert len(once.invocations) == 1, "one model pass, whatever the storage did"
    assert len(first.case_ids) == 3
    await _readable_cases(harness, locators)
    final = await harness.core.load_monitor_progress(operation_scope, command.invocation_id)
    assert final is not None and final.is_complete

    for case_id in first.case_ids:
        case = await harness.core.load_case(_scope(harness, case_id))
        assert len(case.report_ids) == len(set(case.report_ids)) == 2, "no double-applied report"
        assert len(case.fact_ids) == len(set(case.fact_ids)) == 2, "no double-applied fact"
        events = await harness.audit.read_case_events(
            _scope(harness, case_id), PageRequest(limit=100)
        )
        assert len(events.items) == 1, "an ambiguous retry never doubles the audit row"

    # A redelivery on top of a resolved-ambiguous run is still a pure no-op.
    resumed = MonitorHarness(driver=harness.driver, namespace=harness.namespace)
    never = ScriptedMonitorAgent(responder=_responder)
    again = await resumed.run_monitor(never).execute(
        resumed.monitor_command(locators, operation_id=command.operation_id)
    )
    assert never.invocations == [], "an ambiguous storage outcome never re-asks the model"
    assert set(again.case_ids) == set(first.case_ids)


# ---------------------------------------------------------------------------------------
# PROBE 3 -- the worker-level contract: interruption leaves the operation retryable
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("fail_after", [0, 1, 2, 3])
async def test_worker_returns_interrupted_operation_to_pending(
    harness: MonitorHarness, fail_after: int
) -> None:
    """Externally visible status after a mid-plan crash, and after redelivery."""

    locators = await _seeded(harness)
    _operation, job = await harness.dispatched(locators)

    faulty = FaultInjectingDriver(
        inner=harness.driver,
        script=[TransactBehaviour.SUCCEED] * fail_after + [TransactBehaviour.DEFINITE_FAILURE],
        read_script=[],
        scripted=lambda ops: monitor_apply_steps(ops) or _is_finalize(ops),
    )
    partial = MonitorHarness(driver=faulty, namespace=harness.namespace)
    settled = await partial.worker(ScriptedMonitorAgent(responder=_responder)).execute(job)

    assert settled.status is ApplicationOperationStatus.PENDING, (
        "an interrupted apply must be retryable, not FAILED and not stuck RUNNING"
    )

    resumed = MonitorHarness(driver=harness.driver, namespace=harness.namespace)
    never = ScriptedMonitorAgent(responder=_responder)
    finished = await resumed.worker(never).execute(job)

    assert finished.status is ApplicationOperationStatus.SUCCEEDED
    assert never.invocations == [], "redelivery of a frozen plan calls no model"
    assert len(finished.result_refs) == 3

    # A third redelivery must be a no-op.
    again = ScriptedMonitorAgent(responder=_responder)
    third = await resumed.worker(again).execute(job)
    assert third.status is ApplicationOperationStatus.SUCCEEDED
    assert again.invocations == []
    await _readable_cases(harness, locators)
    for case_id in await _readable_cases(harness, locators):
        case = await harness.core.load_case(_scope(harness, case_id))
        assert len(case.report_ids) == 2 and len(case.fact_ids) == 2


# ---------------------------------------------------------------------------------------
# PROBE 4 -- repeated failure: does the operation ever become permanently stuck?
# ---------------------------------------------------------------------------------------


async def test_repeated_interruption_never_poisons_the_operation(
    harness: MonitorHarness,
) -> None:
    """Five consecutive interrupted deliveries, then a clean one."""

    locators = await _seeded(harness)
    _operation, job = await harness.dispatched(locators)

    for _ in range(5):
        faulty = FaultInjectingDriver(
            inner=harness.driver,
            script=[TransactBehaviour.DEFINITE_FAILURE],
            read_script=[],
            scripted=lambda ops: monitor_apply_steps(ops) or _is_finalize(ops),
        )
        partial = MonitorHarness(driver=faulty, namespace=harness.namespace)
        settled = await partial.worker(ScriptedMonitorAgent(responder=_responder)).execute(job)
        assert settled.status is ApplicationOperationStatus.PENDING

    clean = MonitorHarness(driver=harness.driver, namespace=harness.namespace)
    never = ScriptedMonitorAgent(responder=_responder)
    finished = await clean.worker(never).execute(job)
    assert finished.status is ApplicationOperationStatus.SUCCEEDED
    assert never.invocations == [], "the model is asked exactly once across six deliveries"


# ---------------------------------------------------------------------------------------
# PROBE 5 -- transaction size at the contract maximum, on ONE case
# ---------------------------------------------------------------------------------------


async def test_contract_maximum_single_group_transaction_sizes(
    harness: MonitorHarness,
) -> None:
    """Every transaction issued at 25 reports / 100 facts, measured not predicted."""

    await harness.seed()
    anchor = harness.adapter.messages()[-1].sent_at
    batch = tuple(
        AmbientMessage(
            adapter="SYNTHETIC",
            channel_message_id=f"h1probe-max-{index:03d}",
            contributor_pseudonym="resident-a",
            sent_at=anchor + timedelta(minutes=500 + index),
            text=f"The elevator is stuck again, probe report number {index}.",
        )
        for index in range(MAX_PROPOSED_REPORTS)
    )
    ingested = await harness.ingest_messages(batch, idempotency_key="h1probe-max-key")
    locators = tuple(
        MessageFeedEntry(message_id=item.message_id, sent_at=message.sent_at)
        for item, message in zip(ingested.messages, batch, strict=True)
    )

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        reports: list[ProposedReport] = []
        facts: list[ProposedFact] = []
        links: list[CandidateLink] = []
        signals = set()

        def values(message: MonitorMessage) -> tuple[MonitorFactValue, ...]:
            return (
                IncidentOccurrenceValue(
                    fact_type=FactType.INCIDENT_OCCURRENCE,
                    occurred_at=message.sent_at,
                    failure_mode=FailureMode.STUCK,
                ),
                ServiceImpactValue(
                    fact_type=FactType.SERVICE_IMPACT,
                    impact_code=ImpactCode.DELAY,
                    summary="Residents delayed.",
                ),
                LocationAreaValue(
                    fact_type=FactType.LOCATION_AREA, area=LocationAreaCode.ELEVATOR_CAB
                ),
                ManagementStatementValue(
                    fact_type=FactType.MANAGEMENT_STATEMENT,
                    statement="Management dispatched a technician.",
                    speaker_org="Building management",
                    stated_at=message.sent_at,
                ),
            )

        for index, message in enumerate(payload.messages):
            signals.add(message.message_id)
            ref = f"report-{index:03d}"
            reports.append(report_for(message, ref, IssueType.ELEVATOR_FAILURE))
            from tests.fixtures.monitor_answers import whole_span

            span = whole_span(message)
            for slot, value in enumerate(values(message)):
                facts.append(
                    ProposedFact(
                        client_ref=f"fact-{index:03d}-{slot}",
                        report_client_ref=ref,
                        fact_type=value.fact_type,
                        typed_value=value,
                        sensitivity=SensitivityCategory.GENERAL,
                        source_spans=(span,),
                    )
                )
            links.append(
                CandidateLink(
                    report_client_ref=ref,
                    candidate_group_ref="max-group",
                    proposed_case_title="Recurring lift failures",
                    similarity_reasons=("same equipment",),
                    confidence="0.9",
                )
            )
        return MonitorOutput(
            message_results=classify_all(payload, signals),
            proposed_reports=tuple(reports),
            proposed_facts=tuple(facts),
            candidate_links=tuple(links),
        )

    faulty = FaultInjectingDriver(inner=harness.driver, script=[], read_script=[])
    instrumented = MonitorHarness(driver=faulty, namespace=harness.namespace)
    result = await instrumented.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(
        instrumented.monitor_command(locators)
    )

    assert len(result.created_case_ids) == 1
    case = await harness.core.load_case(_scope(harness, result.created_case_ids[0]))
    assert len(case.report_ids) == MAX_PROPOSED_REPORTS
    assert len(case.fact_ids) == MAX_PROPOSED_FACTS
    print(f"\nTRANSACTION SIZES: {faulty.transact_sizes}")
    assert max(faulty.transact_sizes) <= TRANSACTION_MAX_OPERATIONS


# ---------------------------------------------------------------------------------------
# PROBE 6 -- failure at every boundary of a LARGE single-case plan
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("fail_after", [0, 1, 2])
async def test_large_single_case_partial_apply_is_prefix_consistent(
    harness: MonitorHarness, fail_after: int
) -> None:
    """A many-step single case: after a mid-case crash, is the case row a valid prefix?"""

    await harness.seed()
    anchor = harness.adapter.messages()[-1].sent_at
    batch = tuple(
        AmbientMessage(
            adapter="SYNTHETIC",
            channel_message_id=f"h1probe-big-{fail_after}-{index:03d}",
            contributor_pseudonym="resident-a",
            sent_at=anchor + timedelta(minutes=800 + index),
            text=f"The elevator is stuck again, big probe report {index}.",
        )
        for index in range(MAX_PROPOSED_REPORTS)
    )
    ingested = await harness.ingest_messages(batch, idempotency_key=f"h1probe-big-key-{fail_after}")
    locators = tuple(
        MessageFeedEntry(message_id=item.message_id, sent_at=message.sent_at)
        for item, message in zip(ingested.messages, batch, strict=True)
    )

    def responder(invocation: MonitorInvocation) -> MonitorOutput:

        payload = invocation.payload
        reports, facts, links, signals = [], [], [], set()
        for index, message in enumerate(payload.messages):
            signals.add(message.message_id)
            ref = f"report-{index:03d}"
            reports.append(report_for(message, ref, IssueType.ELEVATOR_FAILURE))
            facts.append(fact_for(message, f"fact-{index:03d}", ref))
            links.append(
                CandidateLink(
                    report_client_ref=ref,
                    candidate_group_ref="big-group",
                    proposed_case_title="Recurring lift failures",
                    similarity_reasons=("same equipment",),
                    confidence="0.9",
                )
            )
        return MonitorOutput(
            message_results=classify_all(payload, signals),
            proposed_reports=tuple(reports),
            proposed_facts=tuple(facts),
            candidate_links=tuple(links),
        )

    faulty = FaultInjectingDriver(
        inner=harness.driver,
        script=[TransactBehaviour.SUCCEED] * fail_after + [TransactBehaviour.DEFINITE_FAILURE],
        read_script=[],
        scripted=lambda ops: monitor_apply_steps(ops) or _is_finalize(ops),
    )
    partial = MonitorHarness(driver=faulty, namespace=harness.namespace)
    command = partial.monitor_command(locators)

    with pytest.raises(MonitorApplyInterruptedError):
        await partial.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(command)

    # THE CENTRAL QUESTION: is the half-applied case externally coherent?
    await _readable_cases(harness, locators)

    resumed = MonitorHarness(driver=harness.driver, namespace=harness.namespace)
    never = ScriptedMonitorAgent(responder=responder)
    result = await resumed.run_monitor(never).execute(
        resumed.monitor_command(locators, operation_id=command.operation_id)
    )
    assert never.invocations == []
    case = await harness.core.load_case(_scope(harness, result.case_ids[0]))
    assert len(case.report_ids) == len(set(case.report_ids)) == MAX_PROPOSED_REPORTS
    assert len(case.fact_ids) == len(set(case.fact_ids)) == MAX_PROPOSED_REPORTS
    events = await harness.audit.read_case_events(
        _scope(harness, result.case_ids[0]), PageRequest(limit=100)
    )
    assert len(events.items) == 1, "one decision, one audit row, across a mid-case crash"
