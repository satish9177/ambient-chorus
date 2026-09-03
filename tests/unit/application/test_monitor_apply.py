"""The apply planner: the gates, the step boundaries, and what a retry stages.

The planner is where the deterministic decisions live, so these tests never touch storage.
They assert what the system *intends* to write and what it refuses to write, which is the
thing a reviewer needs to be able to read off one function.

Five gates are exercised here, because each one fails closed for a different reason: the
candidate threshold, the case state, the case version the agent actually saw, an existing
linkage that intake may not move, and a fact slot that is already settled.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import combinations, islice
from uuid import uuid4

import pytest

from chorus.application.services.identity import derive_fact_slot_id, derive_report_id
from chorus.application.services.monitor_apply import (
    MIN_REPORTS_FOR_NEW_CANDIDATE,
    MONITOR_APPLY_ITEM_BUDGET,
    CurrentApplyState,
    MonitorApplyDenial,
    MonitorApplyDeniedError,
    derive_identities,
    plan_monitor_application,
)
from chorus.application.services.monitor_validation import (
    ValidatedCandidateGroup,
    ValidatedFact,
    ValidatedMonitorOutput,
    ValidatedReport,
)
from chorus.contracts.monitor import MAX_PROPOSED_FACTS, MAX_PROPOSED_REPORTS
from chorus.domain.entities import (
    CaseState,
    CommunityCase,
    EvidenceStatus,
    FactType,
    SensitivityCategory,
)
from chorus.domain.facts import (
    Fact,
    FactStatus,
    FailureMode,
    IncidentOccurrence,
    LocationAreaCode,
)
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    FactId,
    MessageId,
    Namespace,
    ReportId,
)
from chorus.ports.agents import AgentContractViolationError, AgentOutputDriftError
from chorus.ports.limits import TRANSACTION_MAX_OPERATIONS
from chorus.ports.records import FeedSignalProjection

NAMESPACE = Namespace("TEST_APPLY")
COMMUNITY = CommunityId(uuid4())
NOW = datetime(2030, 1, 14, 9, 0, tzinfo=UTC)
GROUP = "lift-group"


def _report(ref: str, *, owner: ContributorId | None = None) -> ValidatedReport:
    return ValidatedReport(
        client_ref=ref,
        contributor_id=owner or ContributorId(uuid4()),
        issue_type="ELEVATOR_FAILURE",
        summary=f"summary for {ref}",
        occurred_at=NOW,
        location_area=LocationAreaCode.ELEVATOR_CAB,
        source_message_ids=(MessageId(uuid4()),),
        evidence_ids=(),
    )


def _fact(
    ref: str,
    report_ref: str,
    *,
    failure_mode: FailureMode = FailureMode.STUCK,
    source_message_ids: tuple[MessageId, ...] | None = None,
) -> ValidatedFact:
    return ValidatedFact(
        client_ref=ref,
        report_client_ref=report_ref,
        fact_type=FactType.INCIDENT_OCCURRENCE,
        value=IncidentOccurrence(occurred_at=NOW, failure_mode=failure_mode),
        sensitivity=SensitivityCategory.GENERAL,
        evidence_ids=(),
        source_message_ids=source_message_ids or (MessageId(uuid4()),),
    )


def _validated(
    reports: tuple[ValidatedReport, ...],
    facts: tuple[ValidatedFact, ...] = (),
    *,
    existing_case_id: CaseId | None = None,
    expected_case_version: int | None = None,
) -> ValidatedMonitorOutput:
    return ValidatedMonitorOutput(
        reports=reports,
        facts=facts,
        groups=(
            ValidatedCandidateGroup(
                existing_case_id=existing_case_id,
                expected_case_version=expected_case_version,
                group_ref=None if existing_case_id is not None else GROUP,
                issue_type="ELEVATOR_FAILURE",
                title="Recurring lift failures",
                report_client_refs=tuple(report.client_ref for report in reports),
            ),
        ),
        noise_message_ids=(),
        policy_like_message_ids=(),
    )


def _state(
    cases: dict[CaseId, CommunityCase] | None = None,
    signals: dict[MessageId, FeedSignalProjection] | None = None,
    facts: dict[FactId, Fact] | None = None,
) -> CurrentApplyState:
    return CurrentApplyState(cases=cases or {}, signals=signals or {}, facts=facts or {})


def _plan(validated: ValidatedMonitorOutput, current: CurrentApplyState | None = None):  # type: ignore[no-untyped-def]
    return plan_monitor_application(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        validated=validated,
        identities=derive_identities(
            namespace=NAMESPACE, community_id=COMMUNITY, validated=validated
        ),
        current=current or _state(),
        now=NOW,
    )


def _case(case_id: CaseId, **changes: object) -> CommunityCase:
    values: dict[str, object] = {
        "case_id": case_id,
        "community_id": COMMUNITY,
        "namespace": NAMESPACE,
        "title": "Recurring lift failures",
        "issue_type": "ELEVATOR_FAILURE",
        "state": CaseState.CANDIDATE,
        "report_ids": (),
        "fact_ids": (),
        "assessment_id": None,
        "current_view_id": None,
        "current_action_id": None,
        "corroboration_source_count": 0,
        "state_reason_code": "MONITOR_CANDIDATE_DETECTED",
        "version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return CommunityCase(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------
# Candidate threshold
# ---------------------------------------------------------------------------------------


def test_two_related_reports_create_one_candidate_case() -> None:
    plan = _plan(_validated((_report("a"), _report("b"))))

    assert len(plan.cases) == 1
    planned = plan.cases[0]
    assert planned.created is True
    assert planned.final_case.state is CaseState.CANDIDATE
    assert len(planned.new_reports) == 2
    assert plan.skipped_below_threshold == 0


def test_a_single_report_creates_no_case_and_no_durable_state() -> None:
    """The frozen guard needs at least two related proposals to call something a pattern."""

    plan = _plan(_validated((_report("a"),)))

    assert plan.cases == ()
    assert plan.skipped_below_threshold == 1
    assert plan.has_durable_effect is False
    assert MIN_REPORTS_FOR_NEW_CANDIDATE == 2


def test_a_below_threshold_group_names_the_messages_a_later_run_may_reconsider() -> None:
    """Provisional, not discarded. The messages stay eligible for a later context window."""

    report = _report("a")
    plan = _plan(_validated((report,)))

    assert plan.provisional_message_ids == report.source_message_ids


def test_a_facts_only_group_below_the_threshold_writes_no_fact_either() -> None:
    validated = _validated((_report("a"),), (_fact("f1", "a"),))

    plan = _plan(validated)

    assert plan.cases == ()
    assert plan.has_durable_effect is False


def test_a_new_candidate_starts_with_no_claimed_corroboration() -> None:
    plan = _plan(_validated((_report("a"), _report("b"))))

    assert plan.cases[0].final_case.corroboration_source_count == 0


def test_every_new_fact_starts_as_merely_reported() -> None:
    validated = _validated((_report("a"), _report("b")), (_fact("f1", "a"), _fact("f2", "b")))

    plan = _plan(validated)

    assert len(plan.cases[0].new_facts) == 2
    assert all(fact.evidence_status is EvidenceStatus.REPORTED for fact in plan.cases[0].new_facts)


# ---------------------------------------------------------------------------------------
# Replay and no-op
# ---------------------------------------------------------------------------------------


def test_a_replan_against_committed_state_stages_nothing_new() -> None:
    validated = _validated((_report("a"), _report("b")), (_fact("f1", "a"),))
    first = _plan(validated)
    committed = first.cases[0].final_case
    stored = first.cases[0].new_facts[0]
    signals = {write.signal.message_id: write.signal for write in first.cases[0].signal_writes}

    again = _plan(
        validated, _state({committed.case_id: committed}, signals, {stored.fact_id: stored})
    )

    assert again.cases[0].new_reports == ()
    assert again.cases[0].new_facts == ()
    assert again.cases[0].signal_writes == ()
    assert again.has_durable_effect is False
    assert again.steps == ()


def test_a_no_op_apply_leaves_the_case_version_and_reason_code_untouched() -> None:
    """A valid invocation that adds nothing must not stale everything bound to the case."""

    validated = _validated((_report("a"), _report("b")), (_fact("f1", "a"),))
    first = _plan(validated)
    committed = replace(
        first.cases[0].final_case, state_reason_code="SOMETHING_ELSE_DECIDED_THIS", version=9
    )
    stored = first.cases[0].new_facts[0]
    signals = {write.signal.message_id: write.signal for write in first.cases[0].signal_writes}

    again = _plan(
        validated, _state({committed.case_id: committed}, signals, {stored.fact_id: stored})
    )

    assert again.cases[0].final_case is committed
    assert again.cases[0].final_case.version == 9
    assert again.cases[0].final_case.state_reason_code == "SOMETHING_ELSE_DECIDED_THIS"


def test_a_partially_committed_case_stages_only_the_missing_reports() -> None:
    reports = (_report("a"), _report("b"))
    validated = _validated(reports)
    full = _plan(validated).cases[0].final_case
    partial = replace(full, report_ids=(full.report_ids[0],), fact_ids=(), version=1)

    completed = _plan(validated, _state({full.case_id: partial}))

    assert len(completed.cases[0].new_reports) == 1
    assert completed.cases[0].new_reports[0].report_id not in partial.report_ids


def test_extending_an_existing_case_needs_no_second_report() -> None:
    """The threshold guards *creation*; adding to a known case is a different decision."""

    existing_id = CaseId(uuid4())
    existing = _case(existing_id, version=4)
    validated = _validated((_report("a"),), existing_case_id=existing_id, expected_case_version=4)

    plan = _plan(validated, _state({existing_id: existing}))

    assert len(plan.cases) == 1
    assert plan.cases[0].created is False
    assert len(plan.cases[0].new_reports) == 1
    assert plan.cases[0].final_case.version == 5


# ---------------------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        CaseState.CANDIDATE,
        CaseState.AWAITING_MANDATES,
        CaseState.INVESTIGATING,
        CaseState.READY_FOR_ACTION,
        CaseState.ACTION_PROPOSED,
        CaseState.ACTIONED,
        CaseState.VERIFYING,
    ],
)
def test_every_live_case_state_accepts_a_monitor_linked_report(state: CaseState) -> None:
    existing_id = CaseId(uuid4())
    existing = _case(existing_id, state=state, version=2)
    validated = _validated((_report("a"),), existing_case_id=existing_id, expected_case_version=2)

    plan = _plan(validated, _state({existing_id: existing}))

    assert plan.cases[0].final_case.state is state


@pytest.mark.parametrize("state", [CaseState.RESOLVED, CaseState.CLOSED_UNRESOLVED])
def test_a_terminal_case_refuses_a_monitor_linked_report(state: CaseState) -> None:
    """Reopening a terminal case is a human command, and intake is not a human."""

    existing_id = CaseId(uuid4())
    existing = _case(existing_id, state=state, version=2, state_reason_code="HUMAN_CLOSED")
    validated = _validated((_report("a"),), existing_case_id=existing_id, expected_case_version=2)

    with pytest.raises(MonitorApplyDeniedError) as raised:
        _plan(validated, _state({existing_id: existing}))

    assert raised.value.denial is MonitorApplyDenial.CASE_STATE_INELIGIBLE
    # The gate ran before anything was composed, so the stored case is untouched by
    # construction: there is no plan in which it could have been written.
    assert existing.version == 2
    assert existing.state_reason_code == "HUMAN_CLOSED"


def test_a_case_that_moved_on_since_the_agent_saw_it_refuses_the_link() -> None:
    existing_id = CaseId(uuid4())
    current = _case(existing_id, version=2)
    # The agent was shown version 1; something else advanced the case in between.
    validated = _validated((_report("a"),), existing_case_id=existing_id, expected_case_version=1)

    with pytest.raises(MonitorApplyDeniedError) as raised:
        _plan(validated, _state({existing_id: current}))

    assert raised.value.denial is MonitorApplyDenial.CASE_VERSION_STALE


def test_a_case_that_vanished_since_the_agent_saw_it_refuses_the_link() -> None:
    existing_id = CaseId(uuid4())
    validated = _validated((_report("a"),), existing_case_id=existing_id, expected_case_version=1)

    with pytest.raises(MonitorApplyDeniedError) as raised:
        _plan(validated, _state())

    assert raised.value.denial is MonitorApplyDenial.CASE_VERSION_STALE


def test_a_message_already_linked_elsewhere_refuses_the_whole_apply() -> None:
    """Phase-3 Monitor cannot relink. That is a correction, and it has its own authority."""

    reports = (_report("a"), _report("b"))
    other_case = CaseId(uuid4())
    claimed = reports[0].source_message_ids[0]
    signals = {
        claimed: FeedSignalProjection(
            namespace=NAMESPACE,
            community_id=COMMUNITY,
            message_id=claimed,
            case_id=other_case,
            case_version=3,
            label="An older pattern",
            related_message_count=4,
            case_state=CaseState.INVESTIGATING,
            detected_at=NOW,
            version=1,
        )
    }

    with pytest.raises(MonitorApplyDeniedError) as raised:
        _plan(_validated(reports), _state(signals=signals))

    assert raised.value.denial is MonitorApplyDenial.REPORT_ALREADY_LINKED


# ---------------------------------------------------------------------------------------
# Fact slots
# ---------------------------------------------------------------------------------------


def test_a_re_answered_fact_slot_with_identical_content_writes_nothing() -> None:
    reports = (_report("a"), _report("b"))
    fact = _fact("f1", "a")
    validated = _validated(reports, (fact,))
    first = _plan(validated)
    stored = first.cases[0].new_facts[0]
    committed = first.cases[0].final_case
    signals = {write.signal.message_id: write.signal for write in first.cases[0].signal_writes}

    again = _plan(
        validated, _state({committed.case_id: committed}, signals, {stored.fact_id: stored})
    )

    assert again.cases[0].new_facts == ()
    assert again.has_durable_effect is False


def test_a_re_answered_fact_slot_with_a_changed_value_is_drift_rather_than_a_duplicate() -> None:
    """Same messages, same evidence, a different answer. One slot cannot hold both."""

    reports = (_report("a"), _report("b"))
    original = _fact("f1", "a", failure_mode=FailureMode.STUCK)
    first = _plan(_validated(reports, (original,)))
    stored = first.cases[0].new_facts[0]
    committed = first.cases[0].final_case
    signals = {write.signal.message_id: write.signal for write in first.cases[0].signal_writes}

    changed = _fact(
        "f1",
        "a",
        failure_mode=FailureMode.OUT_OF_SERVICE,
        source_message_ids=original.source_message_ids,
    )

    with pytest.raises(AgentOutputDriftError):
        _plan(
            _validated(reports, (changed,)),
            _state({committed.case_id: committed}, signals, {stored.fact_id: stored}),
        )


def test_an_investigator_corroborated_fact_still_reads_as_an_exact_replay() -> None:
    """Evidence status belongs to the Investigator, so it is not part of "did intake change?"."""

    reports = (_report("a"), _report("b"))
    fact = _fact("f1", "a")
    validated = _validated(reports, (fact,))
    first = _plan(validated)
    stored = replace(
        first.cases[0].new_facts[0],
        evidence_status=EvidenceStatus.CORROBORATED,
        version=3,
        status=FactStatus.ACTIVE,
    )
    committed = first.cases[0].final_case
    signals = {write.signal.message_id: write.signal for write in first.cases[0].signal_writes}

    again = _plan(
        validated, _state({committed.case_id: committed}, signals, {stored.fact_id: stored})
    )

    assert again.cases[0].new_facts == ()


def test_two_proposals_for_one_slot_in_one_answer_are_ambiguous() -> None:
    reports = (_report("a"), _report("b"))
    shared = (MessageId(uuid4()),)
    validated = _validated(
        reports,
        (
            _fact("f1", "a", source_message_ids=shared),
            _fact("f2", "a", failure_mode=FailureMode.ERRATIC, source_message_ids=shared),
        ),
    )

    with pytest.raises(AgentContractViolationError) as raised:
        _plan(validated)

    assert "AMBIGUOUS_FACT_SLOT" in raised.value.reason_codes


def test_fact_identity_matches_the_documented_slot_derivation() -> None:
    reports = (_report("a"), _report("b"))
    fact = _fact("f1", "a")
    plan = _plan(_validated(reports, (fact,)))

    report_id = derive_report_id(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        contributor_id=reports[0].contributor_id,
        issue_type=reports[0].issue_type,
        source_message_ids=reports[0].source_message_ids,
    )
    assert plan.cases[0].new_facts[0].fact_id == derive_fact_slot_id(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        report_id=report_id,
        fact_type=fact.fact_type,
        source_message_ids=fact.source_message_ids,
        evidence_ids=fact.evidence_ids,
    )


# ---------------------------------------------------------------------------------------
# Signals and identity
# ---------------------------------------------------------------------------------------


def test_a_feed_signal_accompanies_every_newly_linked_message() -> None:
    reports = (_report("a"), _report("b"))
    plan = _plan(_validated(reports))

    writes = plan.cases[0].signal_writes
    assert {write.signal.message_id for write in writes} == {
        message_id for report in reports for message_id in report.source_message_ids
    }
    assert all(write.expected_version is None for write in writes)
    assert all(write.signal.case_id == plan.cases[0].case_id for write in writes)
    assert all(write.signal.related_message_count == 2 for write in writes)


def test_a_stale_signal_for_this_case_is_refreshed_under_its_own_version() -> None:
    """A display row must be able to catch up. Create-only made that impossible."""

    reports = (_report("a"), _report("b"))
    validated = _validated(reports)
    first = _plan(validated)
    committed = first.cases[0].final_case
    stale = {
        write.signal.message_id: replace(write.signal, label="An out-of-date label", version=2)
        for write in first.cases[0].signal_writes
    }

    again = _plan(validated, _state({committed.case_id: committed}, stale))

    writes = again.cases[0].signal_writes
    assert len(writes) == 2
    assert all(write.expected_version == 2 for write in writes)
    assert all(write.signal.version == 3 for write in writes)
    assert all(write.signal.label == committed.title for write in writes)


def test_report_identity_matches_the_documented_derivation() -> None:
    reports = (_report("a"), _report("b"))
    plan = _plan(_validated(reports))

    expected = {
        derive_report_id(
            namespace=NAMESPACE,
            community_id=COMMUNITY,
            contributor_id=report.contributor_id,
            issue_type=report.issue_type,
            source_message_ids=report.source_message_ids,
        )
        for report in reports
    }
    assert {report.report_id for report in plan.cases[0].new_reports} == expected


# ---------------------------------------------------------------------------------------
# Step boundaries at the contract maxima
# ---------------------------------------------------------------------------------------


def _maximal_output() -> ValidatedMonitorOutput:
    """One answer at the exact frozen bounds: 25 reports, 50 messages, 100 facts.

    Deliberately built from the contract constants rather than from numbers that happen to be
    large, because the bound that matters is the one the contract permits -- not the one the
    demo fixture produces.
    """

    owner = ContributorId(uuid4())
    messages = tuple(MessageId(uuid4()) for _ in range(50))
    reports = tuple(
        ValidatedReport(
            client_ref=f"report-{index:03d}",
            contributor_id=owner,
            issue_type="ELEVATOR_FAILURE",
            summary=f"summary {index}",
            occurred_at=NOW,
            location_area=LocationAreaCode.ELEVATOR_CAB,
            # Two messages each, so 25 reports consume the whole 50-message batch.
            source_message_ids=(messages[index * 2], messages[index * 2 + 1]),
            evidence_ids=(),
        )
        for index in range(MAX_PROPOSED_REPORTS)
    )
    # A distinct pair of cited messages per fact, so 100 proposals occupy 100 distinct slots.
    # Two facts of one type on one report from one lineage would be genuinely ambiguous, and
    # the planner refuses that rather than picking one -- which is a different test.
    lineages = tuple(islice(combinations(messages, 2), MAX_PROPOSED_FACTS))
    facts = tuple(
        ValidatedFact(
            client_ref=f"fact-{index:03d}",
            report_client_ref=reports[index % MAX_PROPOSED_REPORTS].client_ref,
            fact_type=FactType.INCIDENT_OCCURRENCE,
            value=IncidentOccurrence(occurred_at=NOW, failure_mode=FailureMode.STUCK),
            sensitivity=SensitivityCategory.GENERAL,
            evidence_ids=(),
            source_message_ids=lineages[index],
        )
        for index in range(MAX_PROPOSED_FACTS)
    )
    return ValidatedMonitorOutput(
        reports=reports,
        facts=facts,
        groups=(
            ValidatedCandidateGroup(
                existing_case_id=None,
                expected_case_version=None,
                group_ref=GROUP,
                issue_type="ELEVATOR_FAILURE",
                title="Recurring lift failures",
                report_client_refs=tuple(report.client_ref for report in reports),
            ),
        ),
        noise_message_ids=(),
        policy_like_message_ids=(),
    )


def test_no_step_exceeds_the_transaction_limit_at_the_exact_contract_maxima() -> None:
    """The bound that has to hold is the contract's, not the demo fixture's."""

    plan = _plan(_maximal_output())

    assert plan.steps
    assert plan.max_operation_count < TRANSACTION_MAX_OPERATIONS
    for step in plan.steps:
        assert step.operation_count <= MONITOR_APPLY_ITEM_BUDGET + 5


def test_the_maximal_answer_needs_more_than_one_step() -> None:
    """If it fitted in one transaction the whole bounded-progress design would be dead code."""

    plan = _plan(_maximal_output())

    assert len(plan.steps) > 1


def test_a_new_case_and_all_of_its_initial_reports_land_in_one_atomic_step() -> None:
    plan = _plan(_maximal_output())
    first = plan.steps[0]

    assert first.first_for_case is True
    assert first.case_expected_version is None
    assert len(first.reports) == MAX_PROPOSED_REPORTS


def test_every_intermediate_case_row_lists_only_rows_that_step_has_written() -> None:
    """A partial apply leaves a case that knows less, never one naming absent rows."""

    plan = _plan(_maximal_output())
    written_reports: set[ReportId] = set()
    written_facts: set[FactId] = set()
    for step in plan.steps:
        written_reports.update(report.report_id for report in step.reports)
        written_facts.update(fact.fact_id for fact in step.facts)
        assert set(step.case.report_ids) == written_reports
        assert set(step.case.fact_ids) == written_facts


def test_case_versions_advance_by_exactly_one_per_step() -> None:
    plan = _plan(_maximal_output())

    for index, step in enumerate(plan.steps):
        assert step.case.version == index + 1
        assert step.case_expected_version == (None if index == 0 else index)


def test_the_plan_hash_changes_when_the_ordered_plan_changes() -> None:
    """Progress is only meaningful against the plan it was recorded for."""

    reports = (_report("a"), _report("b"))
    first = _plan(_validated(reports))
    second = _plan(_validated((*reports, _report("c"))))

    assert first.plan_hash != second.plan_hash
    assert first.plan_hash == _plan(_validated(reports)).plan_hash


def test_planning_is_free_of_wall_clock_drift() -> None:
    """Two plans built at different instants over the same state agree on their steps."""

    validated = _validated((_report("a"), _report("b")))
    later = plan_monitor_application(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        validated=validated,
        identities=derive_identities(
            namespace=NAMESPACE, community_id=COMMUNITY, validated=validated
        ),
        current=_state(),
        now=NOW + timedelta(hours=3),
    )

    assert _plan(validated).plan_hash == later.plan_hash
