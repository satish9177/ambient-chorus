"""What the apply transaction guarantees: atomicity, audit, and isolation.

Persistence is where a validated proposal stops being a suggestion. These tests hold the
transaction to its three promises -- it is one atomic unit, it is never unaudited, and it
cannot reach another namespace -- using the real repositories rather than a stub of them.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.fixtures.faults import (
    FaultInjectingDriver,
    TransactBehaviour,
    monitor_apply_steps,
)
from tests.fixtures.monitor import OTHER_NAMESPACE, MonitorHarness

from chorus.application.commands.run_monitor import MonitorApplyInterruptedError
from chorus.application.services.monitor_apply import (
    CANDIDATE_EXTENDED_REASON_CODE,
    CANDIDATE_REASON_CODE,
)
from chorus.domain.entities import ActorType, AuditDecision, CaseState, CommunityCase
from chorus.domain.ids import CaseId
from chorus.infrastructure.local.monitor_agent import (
    LexicalFakeMonitorAgent,
)
from chorus.ports.ambient import AmbientMessage
from chorus.ports.errors import (
    NotFoundError,
    UnauditedMutationError,
)
from chorus.ports.pagination import PageRequest
from chorus.ports.records import MessageFeedEntry
from chorus.ports.scopes import CaseScope, CommunityScope
from chorus.ports.storage import TableName
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio


async def _discovered(harness: MonitorHarness) -> CaseScope:
    await harness.seed()
    locators = await harness.ingest_feed()
    result = await harness.run_monitor(LexicalFakeMonitorAgent()).execute(
        harness.monitor_command(locators)
    )
    return CaseScope(
        namespace=harness.namespace,
        community_id=harness.community_id,
        case_id=result.created_case_ids[0],
    )


async def test_the_apply_transaction_writes_its_audit_row_atomically(
    harness: MonitorHarness,
) -> None:
    scope = await _discovered(harness)

    events = await harness.audit.read_case_events(scope, PageRequest(limit=100))

    assert len(events.items) == 1
    event = events.items[0]
    assert event.event_type == "candidate.detected"
    assert event.decision is AuditDecision.ALLOW
    assert event.actor_type is ActorType.SYSTEM
    assert event.reason_codes == (CANDIDATE_REASON_CODE,)
    assert event.case_id == scope.case_id


async def test_the_audit_row_carries_hashes_and_counts_but_no_content(
    harness: MonitorHarness,
) -> None:
    scope = await _discovered(harness)
    event = (await harness.audit.read_case_events(scope, PageRequest(limit=100))).items[0]

    assert event.input_hash is not None and event.input_hash.value.startswith("sha256:")
    assert event.output_hash is not None and event.output_hash.value.startswith("sha256:")
    assert event.safe_details.count is not None
    rendered = repr(event)
    for message in harness.adapter.messages():
        assert message.text not in rendered


async def test_an_apply_plan_without_its_audit_row_is_refused_before_storage(
    harness: MonitorHarness,
) -> None:
    """A plan that mutates a case and forgot its audit write never reaches storage."""

    await harness.seed()
    case_scope = CaseScope(
        namespace=harness.namespace,
        community_id=harness.community_id,
        case_id=CaseId(uuid4()),
    )
    case = _draft_case(harness, case_scope)

    with pytest.raises(UnauditedMutationError):
        TransactionPlan(
            name="apply-without-audit",
            operations=(harness.core.stage_create_case(case_scope, case),),
            audit_required=True,
        )


async def test_an_audit_write_failure_aborts_the_whole_mutation(
    harness: MonitorHarness,
) -> None:
    await harness.seed()
    locators = await harness.ingest_feed()
    faulty = FaultInjectingDriver(
        inner=harness.driver,
        script=[TransactBehaviour.DEFINITE_FAILURE],
        scripted=monitor_apply_steps,
    )
    poisoned = MonitorHarness(driver=faulty, namespace=harness.namespace)

    # The plan is frozen by the time the first step runs, so a storage failure here is an
    # interruption of work that can still finish -- not a verdict on the answer.
    with pytest.raises(MonitorApplyInterruptedError):
        await poisoned.run_monitor(LexicalFakeMonitorAgent()).execute(
            poisoned.monitor_command(locators)
        )

    # Nothing from the aborted transaction survives, including the feed projection.
    signals = await harness.core.read_feed_signals(
        CommunityScope(namespace=harness.namespace, community_id=harness.community_id),
        PageRequest(limit=100),
    )
    assert signals.items == ()


async def test_an_ambiguous_apply_outcome_is_resolved_by_its_own_commit_proof(
    harness: MonitorHarness,
) -> None:
    """The transaction carries proof, so an ambiguous failure never re-applies blindly."""

    await harness.seed()
    locators = await harness.ingest_feed()
    faulty = FaultInjectingDriver(
        inner=harness.driver,
        script=[TransactBehaviour.AMBIGUOUS_AFTER_APPLY],
        scripted=monitor_apply_steps,
    )
    resilient = MonitorHarness(driver=faulty, namespace=harness.namespace)

    result = await resilient.run_monitor(LexicalFakeMonitorAgent()).execute(
        resilient.monitor_command(locators)
    )

    assert len(result.created_case_ids) == 1
    case = await harness.core.load_case(
        CaseScope(
            namespace=harness.namespace,
            community_id=harness.community_id,
            case_id=result.created_case_ids[0],
        )
    )
    assert case.state is CaseState.CANDIDATE
    assert case.version == 1


async def test_an_unprovable_outcome_is_never_retried(harness: MonitorHarness) -> None:
    await harness.seed()
    locators = await harness.ingest_feed()
    faulty = FaultInjectingDriver(
        inner=harness.driver,
        script=[
            TransactBehaviour.AMBIGUOUS_AFTER_APPLY,
            TransactBehaviour.AMBIGUOUS_AFTER_APPLY,
        ],
        read_script=[],
        scripted=monitor_apply_steps,
    )
    harness_with_fault = MonitorHarness(driver=faulty, namespace=harness.namespace)

    # The first ambiguous write resolves through its proof, so the command still succeeds.
    result = await harness_with_fault.run_monitor(LexicalFakeMonitorAgent()).execute(
        harness_with_fault.monitor_command(locators)
    )

    assert faulty.scripted_calls == 1, "the ambiguous step was never sent a second time"
    assert len(result.created_case_ids) == 1


async def test_a_case_is_addressable_only_inside_its_own_namespace(
    harness: MonitorHarness,
) -> None:
    scope = await _discovered(harness)
    foreign = MonitorHarness(driver=harness.driver, namespace=OTHER_NAMESPACE)

    with pytest.raises(NotFoundError):
        await foreign.core.load_case(
            CaseScope(
                namespace=OTHER_NAMESPACE,
                community_id=foreign.community_id,
                case_id=scope.case_id,
            )
        )


async def test_a_feed_signal_is_addressable_only_inside_its_own_community(
    harness: MonitorHarness,
) -> None:
    await _discovered(harness)
    foreign = MonitorHarness(driver=harness.driver, namespace=OTHER_NAMESPACE)

    signals = await foreign.core.read_feed_signals(
        CommunityScope(namespace=OTHER_NAMESPACE, community_id=foreign.community_id),
        PageRequest(limit=100),
    )

    assert signals.items == ()


async def test_extending_a_case_bumps_its_version_under_an_optimistic_condition(
    harness: MonitorHarness,
) -> None:
    scope = await _discovered(harness)
    case = await harness.core.load_case(scope)

    extra = AmbientMessage(
        adapter="SYNTHETIC",
        channel_message_id="feed-025",
        contributor_pseudonym="resident-c",
        sent_at=harness.adapter.messages()[-1].sent_at,
        text="The lift is out of service again this morning.",
    )
    result = await harness.ingest_messages((extra,), idempotency_key="extend-key-000001")
    locator = MessageFeedEntry(message_id=result.messages[0].message_id, sent_at=extra.sent_at)

    # No candidate case is named by the caller. The run finds the existing case itself, from
    # the feed signals of the recent messages its context window already includes.
    extended = await harness.run_monitor(LexicalFakeMonitorAgent()).execute(
        harness.monitor_command((locator,), invocation_id=uuid4())
    )

    assert extended.created_case_ids == ()
    updated = await harness.core.load_case(scope)
    assert updated.version == case.version + 1
    assert updated.state_reason_code == CANDIDATE_EXTENDED_REASON_CODE
    assert len(updated.report_ids) == len(case.report_ids) + 1


async def test_the_idempotency_record_lives_in_the_private_core_table(
    harness: MonitorHarness,
) -> None:
    """Monitor output is private, so its command record never touches the shareable table."""

    await _discovered(harness)

    assert harness.idempotency.table is TableName.CORE


def _draft_case(harness: MonitorHarness, scope: CaseScope) -> CommunityCase:
    now = harness.clock.now()
    return CommunityCase(
        case_id=scope.case_id,
        community_id=scope.community_id,
        namespace=scope.namespace,
        title="Recurring lift failures",
        issue_type="ELEVATOR_FAILURE",
        state=CaseState.CANDIDATE,
        report_ids=(),
        fact_ids=(),
        assessment_id=None,
        current_view_id=None,
        current_action_id=None,
        corroboration_source_count=0,
        state_reason_code=CANDIDATE_REASON_CODE,
        version=1,
        created_at=now,
        updated_at=now,
    )
