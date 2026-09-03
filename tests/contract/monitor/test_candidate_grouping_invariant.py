"""ADR-012, attacked from every direction a merge can be reached.

Two reports share a case only under an issue type that names a subject. This file is the
permanent home of the adversarial cases that closed H-3, NEW-1 and NEW-2, promoted from the
independent reviewer's gate probes and extended with attacks those probes did not make.

The organising question in every test is the same: *what did deterministic code actually
prove?* An answer in which issue type agrees, title agrees, location agrees, and the messages
arrived a minute apart has proved nothing at all -- the model chose all four. So the tests come
in pairs. A refusal is only interesting next to the accepted answer it is nearly identical to,
and an acceptance is only interesting next to the refusal it is one field away from.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from tests.fixtures.monitor import MonitorHarness
from tests.fixtures.monitor_answers import classify_all, fact_for, report_for

from chorus.application.services.identity import derive_candidate_case_id
from chorus.application.services.monitor_apply import (
    MonitorApplyDenial,
    MonitorApplyDeniedError,
)
from chorus.contracts.monitor import (
    CandidateLink,
    IssueType,
    MonitorInput,
    MonitorOutput,
)
from chorus.domain.entities import CaseState, CommunityCase
from chorus.domain.facts import LocationAreaCode
from chorus.domain.ids import CaseId
from chorus.infrastructure.local.monitor_agent import ScriptedMonitorAgent
from chorus.ports.agents import AgentContractViolationError, MonitorInvocation
from chorus.ports.ambient import AmbientMessage
from chorus.ports.records import FeedSignalProjection, MessageFeedEntry
from chorus.ports.scopes import CaseScope, CommunityScope
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio

type Responder = Callable[[MonitorInvocation], MonitorOutput]

UNPROVABLE = "CANDIDATE_GROUP_UNPROVABLE"


def _case_scope(harness: MonitorHarness, case_id: CaseId) -> CaseScope:
    return CaseScope(
        namespace=harness.namespace, community_id=harness.community_id, case_id=case_id
    )


def _community_scope(harness: MonitorHarness) -> CommunityScope:
    return CommunityScope(namespace=harness.namespace, community_id=harness.community_id)


async def _ingest(
    harness: MonitorHarness,
    entries: tuple[tuple[str, str, str], ...],
    *,
    offset: int,
    apart: int = 1,
) -> tuple[MessageFeedEntry, ...]:
    """Ingest ``(channel_id, pseudonym, text)`` messages ``apart`` minutes from each other."""

    anchor = harness.adapter.messages()[-1].sent_at
    batch = tuple(
        AmbientMessage(
            adapter="SYNTHETIC",
            channel_message_id=entry[0],
            contributor_pseudonym=entry[1],
            sent_at=anchor + timedelta(minutes=offset + index * apart),
            text=entry[2],
        )
        for index, entry in enumerate(entries)
    )
    result = await harness.ingest_messages(batch, idempotency_key=f"grouping-key-{offset}")
    return tuple(
        MessageFeedEntry(message_id=item.message_id, sent_at=message.sent_at)
        for item, message in zip(result.messages, batch, strict=True)
    )


def _merge_responder(
    locators: tuple[MessageFeedEntry, ...],
    *,
    issue: IssueType,
    title: str,
    group_ref: str = "merge-group",
    areas: tuple[LocationAreaCode | None, ...] | None = None,
) -> Responder:
    """An answer that files every named message into ONE group under ``issue``.

    Deliberately a *well-formed* answer: every message classified, every report owned by its
    sender, every fact anchored. What is under test is grouping, so nothing else may be the
    reason it is refused.
    """

    wanted = tuple(item.message_id.value for item in locators)

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        payload: MonitorInput = invocation.payload
        by_id = {message.message_id: message for message in payload.messages}
        picked = [by_id[message_id] for message_id in wanted]
        reports = []
        for index, message in enumerate(picked):
            report = report_for(message, f"report-{index:03d}", issue)
            area = None if areas is None else areas[index]
            reports.append(report.model_copy(update={"location_area": area}))
        return MonitorOutput(
            message_results=classify_all(payload, {message.message_id for message in picked}),
            proposed_reports=tuple(reports),
            proposed_facts=tuple(
                fact_for(message, f"fact-{index:03d}", f"report-{index:03d}")
                for index, message in enumerate(picked)
            ),
            candidate_links=tuple(
                CandidateLink(
                    report_client_ref=f"report-{index:03d}",
                    candidate_group_ref=group_ref,
                    proposed_case_title=title,
                    similarity_reasons=("the model says these belong together",),
                    confidence="0.95",
                )
                for index in range(len(picked))
            ),
        )

    return responder


async def _refuses(
    harness: MonitorHarness,
    responder: Responder,
    locators: tuple[MessageFeedEntry, ...],
    *,
    invocation_id: UUID | None = None,
) -> AgentContractViolationError:
    with pytest.raises(AgentContractViolationError) as raised:
        await harness.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(
            harness.monitor_command(locators, invocation_id=invocation_id)
        )
    return raised.value


# =======================================================================================
# 1. Same location, unrelated OTHER incidents
# =======================================================================================


@pytest.mark.parametrize("area", list(LocationAreaCode))
async def test_unrelated_other_incidents_sharing_an_area_never_merge(
    harness: MonitorHarness, area: LocationAreaCode
) -> None:
    """H-3 itself, at every value of the enum the withdrawn rule compared.

    ``LocationAreaCode`` is an *area kind*, not a place identity: two different buildings in
    one community share ``BUILDING``. Agreement on it was never evidence, which is why the
    refusal must not depend on whether the two reports agree.
    """

    await harness.seed()
    locators = await _ingest(
        harness,
        (
            (
                f"unrelated-leak-{area.value}",
                "resident-a",
                "Water is leaking from the ceiling in the shared area and pooling badly.",
            ),
            (
                f"unrelated-locker-{area.value}",
                "resident-b",
                "Unrelated: the parcel locker keypad has stopped accepting any code at all.",
            ),
        ),
        offset=2000 + list(LocationAreaCode).index(area) * 10,
    )
    responder = _merge_responder(
        locators, issue=IssueType.OTHER, title="Maintenance issue", areas=(area, area)
    )

    error = await _refuses(harness, responder, locators)

    assert UNPROVABLE in error.reason_codes
    signals = await harness.core.load_feed_signals(
        _community_scope(harness), tuple(item.message_id for item in locators)
    )
    assert signals == {}, "a refused merge must leave no durable trace"


# =======================================================================================
# 2. Same location, same vague title, unrelated systems
# =======================================================================================


async def test_one_vague_title_over_two_unrelated_systems_never_merges(
    harness: MonitorHarness,
) -> None:
    """The reviewer's example verbatim: elevator vibration and water pressure, one building.

    Every signal the old rule and its predecessor consulted is satisfied here on purpose --
    same issue type, same title, same area, one group ref -- because the model wrote all four.
    """

    await harness.seed()
    locators = await _ingest(
        harness,
        (
            (
                "vague-vibration",
                "resident-a",
                "The elevator has developed a heavy vibration whenever it passes floor three.",
            ),
            (
                "vague-pressure",
                "resident-b",
                "Water pressure on the upper floors has been almost nothing since Tuesday.",
            ),
        ),
        offset=2100,
    )
    responder = _merge_responder(
        locators,
        issue=IssueType.OTHER,
        title="Building A issue",
        areas=(LocationAreaCode.BUILDING, LocationAreaCode.BUILDING),
    )

    error = await _refuses(harness, responder, locators)

    assert UNPROVABLE in error.reason_codes


# =======================================================================================
# 3. Same location, same time window, unrelated incidents
# =======================================================================================


async def test_two_unrelated_other_reports_seconds_apart_never_merge(
    harness: MonitorHarness,
) -> None:
    """Time proximity is not relatedness. Two complaints a minute apart is a busy channel."""

    await harness.seed()
    locators = await _ingest(
        harness,
        (
            (
                "burst-bins",
                "resident-a",
                "The recycling bins by the side entrance are overflowing again.",
            ),
            (
                "burst-dog",
                "resident-b",
                "A stray dog got into the car park through the open gate.",
            ),
        ),
        offset=2200,
        apart=1,
    )
    first, second = locators
    assert second.sent_at - first.sent_at == timedelta(minutes=1), "the probe must be tight"

    responder = _merge_responder(
        locators,
        issue=IssueType.OTHER,
        title="Two things happening right now",
        areas=(LocationAreaCode.COMMON_AREA, LocationAreaCode.COMMON_AREA),
    )

    error = await _refuses(harness, responder, locators)

    assert UNPROVABLE in error.reason_codes


# =======================================================================================
# 4. Same location, genuinely related reports
# =======================================================================================


@pytest.mark.parametrize("area", list(LocationAreaCode))
async def test_genuinely_related_reports_under_a_named_issue_type_group(
    harness: MonitorHarness, area: LocationAreaCode
) -> None:
    """The invariant restricts the *vocabulary*, not the grouping mechanism.

    Same two-report shape as every refusal above, at every area value, and it is accepted --
    because ``ELEVATOR_FAILURE`` is a word that says what went wrong.
    """

    await harness.seed()
    locators = await _ingest(
        harness,
        (
            (
                f"related-lift-a-{area.value}",
                "resident-a",
                "The lift stopped between floors again and the doors would not open.",
            ),
            (
                f"related-lift-b-{area.value}",
                "resident-b",
                "Confirming the lift is stuck, exactly the fault my neighbour described.",
            ),
        ),
        offset=2300 + list(LocationAreaCode).index(area) * 10,
    )
    responder = _merge_responder(
        locators,
        issue=IssueType.ELEVATOR_FAILURE,
        title="Lift stopping between floors",
        areas=(area, area),
    )

    result = await harness.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(
        harness.monitor_command(locators)
    )

    assert len(result.created_case_ids) == 1
    case = await harness.core.load_case(_case_scope(harness, result.created_case_ids[0]))
    assert len(case.report_ids) == 2
    assert case.issue_type == IssueType.ELEVATOR_FAILURE.value


# =======================================================================================
# 5. Ambiguous or missing semantic grouping information
# =======================================================================================


async def test_an_other_merge_with_no_location_at_all_is_refused_the_same_way(
    harness: MonitorHarness,
) -> None:
    """Omitting the field is refused identically to supplying it, and for the same reason.

    Under the withdrawn rule these two cases produced different outcomes, which is what made
    the rule dodgeable in one direction and a hidden requirement in the other.
    """

    await harness.seed()
    locators = await _ingest(
        harness,
        (
            ("nolocation-a", "resident-a", "Something in the building has been making a noise."),
            ("nolocation-b", "resident-b", "There is an issue with one of the shared facilities."),
        ),
        offset=2400,
    )
    responder = _merge_responder(
        locators, issue=IssueType.OTHER, title="Unspecified issue", areas=(None, None)
    )

    error = await _refuses(harness, responder, locators)

    assert UNPROVABLE in error.reason_codes


async def test_a_lone_other_report_is_accepted_and_produces_nothing_durable(
    harness: MonitorHarness,
) -> None:
    """The fail-closed shape, stated exactly.

    A single ``OTHER`` report is not a merge, so the answer is *accepted* -- the batch's other
    classifications and observations survive, which is precisely what NEW-1's whole-batch
    failure destroyed. It simply never reaches a case, because a case needs two reports and
    ``OTHER`` may never reach two.
    """

    await harness.seed()
    locators = await _ingest(
        harness,
        (("lone-other", "resident-a", "The parcel locker keypad has stopped accepting codes."),),
        offset=2500,
    )
    responder = _merge_responder(
        locators, issue=IssueType.OTHER, title="Parcel locker keypad", areas=(None,)
    )

    result = await harness.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(
        harness.monitor_command(locators)
    )

    assert result.created_case_ids == ()
    assert result.skipped_below_threshold == 1
    signals = await harness.core.load_feed_signals(
        _community_scope(harness), tuple(item.message_id for item in locators)
    )
    assert signals == {}


# =======================================================================================
# 6. A malicious or careless candidate_group_ref
# =======================================================================================


async def test_two_other_reports_under_two_group_refs_stay_two_provisional_reports(
    harness: MonitorHarness,
) -> None:
    """The compliant answer for two unrelated ``OTHER`` reports: two labels, no case."""

    await harness.seed()
    locators = await _ingest(
        harness,
        (
            ("split-a", "resident-a", "The recycling bins by the side entrance are overflowing."),
            ("split-b", "resident-b", "The parcel locker keypad has stopped accepting codes."),
        ),
        offset=2600,
    )
    first, second = locators

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        by_id = {message.message_id: message for message in payload.messages}
        picked = [by_id[first.message_id.value], by_id[second.message_id.value]]
        return MonitorOutput(
            message_results=classify_all(payload, {message.message_id for message in picked}),
            proposed_reports=tuple(
                report_for(message, f"report-{index:03d}", IssueType.OTHER)
                for index, message in enumerate(picked)
            ),
            proposed_facts=tuple(
                fact_for(message, f"fact-{index:03d}", f"report-{index:03d}")
                for index, message in enumerate(picked)
            ),
            candidate_links=tuple(
                CandidateLink(
                    report_client_ref=f"report-{index:03d}",
                    candidate_group_ref=f"group-{index:03d}",
                    proposed_case_title=f"Separate problem {index}",
                    similarity_reasons=("kept apart on purpose",),
                    confidence="0.9",
                )
                for index in range(2)
            ),
        )

    result = await harness.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(
        harness.monitor_command(locators)
    )

    assert result.created_case_ids == ()
    assert result.skipped_below_threshold == 2


async def test_a_group_ref_that_imitates_a_case_reference_still_cannot_merge_other(
    harness: MonitorHarness,
) -> None:
    """A label chosen to look authoritative buys the model nothing.

    The label is not the mechanism -- it separates groups, it never licenses one -- so a
    plausible-sounding ref is refused exactly like ``merge-group``. (A ref that *parses* as a
    UUID is refused earlier still, by the contract itself; that is asserted in the B-1 suite.)
    """

    await harness.seed()
    locators = await _ingest(
        harness,
        (
            ("imitate-a", "resident-a", "The gate to the car park will not close properly."),
            ("imitate-b", "resident-b", "The laundry room dryer has stopped heating up."),
        ),
        offset=2700,
    )
    responder = _merge_responder(
        locators,
        issue=IssueType.OTHER,
        title="Confirmed related by the system",
        group_ref="verified-existing-case",
        areas=(LocationAreaCode.BUILDING, LocationAreaCode.BUILDING),
    )

    error = await _refuses(harness, responder, locators)

    assert UNPROVABLE in error.reason_codes


# =======================================================================================
# 7. Extension into an existing case -- NEW-2
# =======================================================================================


async def _store_case(
    harness: MonitorHarness, *, issue_type: str, title: str, anchor: MessageFeedEntry
) -> CaseId:
    """Put a case into storage directly, and point one message's feed signal at it.

    Direct staging, not a Monitor run, because under ADR-012 a Monitor run can no longer
    produce an ``OTHER`` case -- and the gate under test has to answer for a case that exists
    anyway. Signalling ``anchor`` is what makes the case reachable as an extension candidate,
    which is how a later run is offered it in the first place.
    """

    now = harness.clock.now()
    case_id = CaseId(
        derive_candidate_case_id(
            namespace=harness.namespace,
            community_id=harness.community_id,
            issue_type=issue_type,
            report_ids=(),
        ).value
    )
    case = CommunityCase(
        case_id=case_id,
        community_id=harness.community_id,
        namespace=harness.namespace,
        title=title,
        issue_type=issue_type,
        state=CaseState.CANDIDATE,
        report_ids=(),
        fact_ids=(),
        assessment_id=None,
        current_view_id=None,
        current_action_id=None,
        corroboration_source_count=0,
        state_reason_code="SEEDED_FOR_TEST",
        version=1,
        created_at=now,
        updated_at=now,
    )
    signal = FeedSignalProjection(
        namespace=harness.namespace,
        community_id=harness.community_id,
        message_id=anchor.message_id,
        case_id=case_id,
        case_version=1,
        label=title,
        related_message_count=1,
        case_state=CaseState.CANDIDATE,
        detected_at=now,
    )
    await harness.unit_of_work.commit(
        TransactionPlan(
            name="seed-case-for-grouping-test",
            operations=(
                harness.core.stage_create_case(_case_scope(harness, case_id), case),
                harness.core.stage_create_feed_signal(_community_scope(harness), signal),
            ),
            audit_required=False,
        )
    )
    return case_id


def _extend_responder(
    target: MessageFeedEntry, case_id: CaseId, title: str, issue: IssueType
) -> Responder:
    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        message = next(
            item for item in payload.messages if item.message_id == target.message_id.value
        )
        return MonitorOutput(
            message_results=classify_all(payload, {message.message_id}),
            proposed_reports=(report_for(message, "report-000", issue),),
            proposed_facts=(fact_for(message, "fact-000", "report-000"),),
            candidate_links=(
                CandidateLink(
                    report_client_ref="report-000",
                    existing_case_id=case_id.value,
                    proposed_case_title=title,
                    similarity_reasons=("the model says this belongs here",),
                    confidence="0.95",
                ),
            ),
        )

    return responder


async def test_an_unrelated_report_cannot_extend_a_stored_other_case(
    harness: MonitorHarness,
) -> None:
    """NEW-2. The extension path is governed by the same invariant as creation.

    Under the withdrawn rule this path was not checked at all, so an unrelated ``OTHER``
    report -- with no location whatsoever, which the creation rule would have refused --
    joined an existing ``OTHER`` case one batch later.
    """

    await harness.seed()
    anchor, target = await _ingest(
        harness,
        (
            ("extend-anchor", "resident-a", "The lobby intercom has been broken for a week."),
            ("extend-unrelated", "resident-c", "The parcel locker keypad has stopped working."),
        ),
        offset=2800,
    )
    case_id = await _store_case(
        harness,
        issue_type=IssueType.OTHER.value,
        title="Broken lobby intercom",
        anchor=anchor,
    )

    error = await _refuses(
        harness,
        _extend_responder(target, case_id, "Broken lobby intercom", IssueType.OTHER),
        (anchor, target),
        invocation_id=uuid4(),
    )

    assert UNPROVABLE in error.reason_codes
    after = await harness.core.load_case(_case_scope(harness, case_id))
    assert after.report_ids == (), "NEW-2: a refused extension still wrote a report"
    assert after.version == 1, "NEW-2: a refused extension still bumped the case"


async def test_even_a_related_sounding_report_cannot_extend_a_stored_other_case(
    harness: MonitorHarness,
) -> None:
    """The refusal is the invariant, not a relatedness judgement that happened to say no.

    This report genuinely is about the same intercom, and it is refused all the same. If the
    gate accepted this one it would be reading the model's prose, which is the thing that
    cannot be trusted.
    """

    await harness.seed()
    anchor, target = await _ingest(
        harness,
        (
            ("related-anchor", "resident-a", "The lobby intercom has been broken for a week."),
            ("related-extend", "resident-c", "The lobby intercom is still completely dead today."),
        ),
        offset=2900,
    )
    case_id = await _store_case(
        harness,
        issue_type=IssueType.OTHER.value,
        title="Broken lobby intercom",
        anchor=anchor,
    )

    error = await _refuses(
        harness,
        _extend_responder(target, case_id, "Broken lobby intercom", IssueType.OTHER),
        (anchor, target),
        invocation_id=uuid4(),
    )

    assert UNPROVABLE in error.reason_codes


async def test_a_named_case_still_extends(harness: MonitorHarness) -> None:
    """The positive counterpart, on the very same path: a named case grows normally."""

    await harness.seed()
    anchor, target = await _ingest(
        harness,
        (
            ("named-anchor", "resident-a", "The lift stopped between floors this morning."),
            ("named-extend", "resident-c", "The lift is stuck again, same as earlier today."),
        ),
        offset=3000,
    )
    case_id = await _store_case(
        harness,
        issue_type=IssueType.ELEVATOR_FAILURE.value,
        title="Recurring lift failures",
        anchor=anchor,
    )

    result = await harness.run_monitor(
        ScriptedMonitorAgent(
            responder=_extend_responder(
                target, case_id, "Recurring lift failures", IssueType.ELEVATOR_FAILURE
            )
        )
    ).execute(harness.monitor_command((anchor, target), invocation_id=uuid4()))

    assert result.case_ids == (case_id,)
    after = await harness.core.load_case(_case_scope(harness, case_id))
    assert len(after.report_ids) == 1, "the extending report landed"
    assert after.version > 1


async def test_the_apply_gate_refuses_a_stored_other_case_independently(
    harness: MonitorHarness,
) -> None:
    """Defence in depth: the gate asks the case row, not the summary the agent was shown.

    The candidate summary here says ``ELEVATOR_FAILURE`` while the stored case says ``OTHER``,
    which is what a summary built before a case changed underneath it would look like. The
    validator has nothing to object to -- it can only see the summary -- so the refusal has to
    come from the apply gate reading durable state.
    """

    from chorus.application.services.monitor_apply import _check_case_eligibility
    from chorus.application.services.monitor_validation import ValidatedCandidateGroup

    await harness.seed()
    (anchor,) = await _ingest(
        harness,
        (("apply-gate-anchor", "resident-a", "The lobby intercom has been broken for a week."),),
        offset=3100,
    )
    case_id = await _store_case(
        harness, issue_type=IssueType.OTHER.value, title="Broken lobby intercom", anchor=anchor
    )
    stored = await harness.core.load_case(_case_scope(harness, case_id))

    group = ValidatedCandidateGroup(
        existing_case_id=case_id,
        expected_case_version=stored.version,
        group_ref=None,
        issue_type=IssueType.ELEVATOR_FAILURE.value,
        title=stored.title,
        report_client_refs=("report-000",),
    )

    with pytest.raises(MonitorApplyDeniedError) as raised:
        _check_case_eligibility(group=group, existing=stored)
    assert raised.value.denial is MonitorApplyDenial.CASE_SUBJECT_UNNAMED

    # And the same gate lets a named case through, so the denial is about the subject and not
    # about anything else this fixture happens to have staged.
    named = replace(stored, issue_type=IssueType.ELEVATOR_FAILURE.value)
    _check_case_eligibility(group=group, existing=named)


# =======================================================================================
# Blast radius, and what breadth cannot buy
# =======================================================================================


async def test_a_lone_other_report_does_not_cost_the_rest_of_the_batch(
    harness: MonitorHarness,
) -> None:
    """The half of NEW-1 that mattered most: an unnamed problem is not a batch-killer.

    Two lift reports and one unnamed complaint, in one answer. The lift case is created and
    the unnamed report is simply provisional. Under the withdrawn rule a batch like this could
    lose everything to one optional field, which is why "fail closed" had to mean *this* shape
    -- refuse the merge, not the batch -- and why the model is now told how to produce it.
    """

    await harness.seed()
    locators = await _ingest(
        harness,
        (
            ("mixed-lift-a", "resident-a", "The lift stopped between floors this morning."),
            ("mixed-lift-b", "resident-b", "Confirming the lift is stuck, same fault."),
            ("mixed-misc", "resident-c", "Separately, the bins by the side entrance overflow."),
        ),
        offset=3500,
    )

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        by_id = {message.message_id: message for message in payload.messages}
        picked = [by_id[item.message_id.value] for item in locators]
        issues = (IssueType.ELEVATOR_FAILURE, IssueType.ELEVATOR_FAILURE, IssueType.OTHER)
        labels = (
            ("lift", "Lift stopping between floors"),
            ("lift", "Lift stopping between floors"),
            ("misc", "Overflowing bins"),
        )
        return MonitorOutput(
            message_results=classify_all(payload, {item.message_id for item in picked}),
            proposed_reports=tuple(
                report_for(message, f"report-{index:03d}", issues[index])
                for index, message in enumerate(picked)
            ),
            proposed_facts=tuple(
                fact_for(message, f"fact-{index:03d}", f"report-{index:03d}")
                for index, message in enumerate(picked)
            ),
            candidate_links=tuple(
                CandidateLink(
                    report_client_ref=f"report-{index:03d}",
                    candidate_group_ref=labels[index][0],
                    proposed_case_title=labels[index][1],
                    similarity_reasons=("scripted",),
                    confidence="0.9",
                )
                for index in range(len(picked))
            ),
        )

    result = await harness.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(
        harness.monitor_command(locators)
    )

    assert len(result.created_case_ids) == 1, "the named problem still became a case"
    assert result.skipped_below_threshold == 1, "the unnamed one is provisional, not fatal"
    case = await harness.core.load_case(_case_scope(harness, result.created_case_ids[0]))
    assert case.issue_type == IssueType.ELEVATOR_FAILURE.value
    assert len(case.report_ids) == 2


async def test_one_other_report_citing_many_messages_is_still_not_a_case(
    harness: MonitorHarness,
) -> None:
    """Breadth cannot substitute for the count the threshold asks for.

    A single report may cite up to ten messages, so an answer could try to make one ``OTHER``
    report look like a pattern by piling citations into it. The candidate guard counts distinct
    *reports*, not messages, so this is one report and no case -- and the grouping rule never
    has to be the thing that catches it.
    """

    await harness.seed()
    locators = await _ingest(
        harness,
        tuple(
            (f"wide-{index}", "resident-a", f"An unnamed problem in the building, note {index}.")
            for index in range(6)
        ),
        offset=3600,
    )
    wanted = tuple(item.message_id.value for item in locators)

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        by_id = {message.message_id: message for message in payload.messages}
        picked = [by_id[message_id] for message_id in wanted]
        report = report_for(picked[0], "report-000", IssueType.OTHER).model_copy(
            update={"message_ids": tuple(message.message_id for message in picked)}
        )
        return MonitorOutput(
            message_results=classify_all(payload, {message.message_id for message in picked}),
            proposed_reports=(report,),
            proposed_facts=(fact_for(picked[0], "fact-000", "report-000"),),
            candidate_links=(
                CandidateLink(
                    report_client_ref="report-000",
                    candidate_group_ref="wide",
                    proposed_case_title="One big unnamed problem",
                    similarity_reasons=("many messages, one report",),
                    confidence="0.99",
                ),
            ),
        )

    result = await harness.run_monitor(ScriptedMonitorAgent(responder=responder)).execute(
        harness.monitor_command(locators)
    )

    assert result.created_case_ids == ()
    assert result.skipped_below_threshold == 1


async def test_mixing_a_named_and_an_unnamed_report_in_one_group_is_inconsistent(
    harness: MonitorHarness,
) -> None:
    """The nearest bypass: hide an ``OTHER`` report inside a group that names a subject.

    Refused as an inconsistent group rather than as an unprovable one, because the members
    disagree about what the group *is* before the question of relatedness even arises. Both
    codes are correct refusals; what matters is that the merge never happens.
    """

    await harness.seed()
    locators = await _ingest(
        harness,
        (
            ("hide-lift", "resident-a", "The lift is stuck between floors again."),
            ("hide-misc", "resident-b", "The bins by the side entrance are overflowing."),
        ),
        offset=3700,
    )

    def responder(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        by_id = {message.message_id: message for message in payload.messages}
        picked = [by_id[item.message_id.value] for item in locators]
        issues = (IssueType.ELEVATOR_FAILURE, IssueType.OTHER)
        return MonitorOutput(
            message_results=classify_all(payload, {item.message_id for item in picked}),
            proposed_reports=tuple(
                report_for(message, f"report-{index:03d}", issues[index])
                for index, message in enumerate(picked)
            ),
            proposed_facts=tuple(
                fact_for(message, f"fact-{index:03d}", f"report-{index:03d}")
                for index, message in enumerate(picked)
            ),
            candidate_links=tuple(
                CandidateLink(
                    report_client_ref=f"report-{index:03d}",
                    candidate_group_ref="one-group",
                    proposed_case_title="Lift and everything else",
                    similarity_reasons=("hiding one inside the other",),
                    confidence="0.9",
                )
                for index in range(len(picked))
            ),
        )

    error = await _refuses(harness, responder, locators)

    assert "CANDIDATE_GROUP_INCONSISTENT" in error.reason_codes


def test_no_spelling_of_the_unnamed_issue_type_grants_grouping() -> None:
    """A stored ``issue_type`` is an ordinary string, so its spelling must not be a bypass."""

    from chorus.domain.entities import issue_type_names_a_subject

    for spelling in ("OTHER", "other", "Other", " OTHER ", "oThEr", "OTHER\t"):
        assert not issue_type_names_a_subject(spelling), spelling
    for named in ("ELEVATOR_FAILURE", "OTHERS", "NOT_OTHER"):
        assert issue_type_names_a_subject(named), named


# =======================================================================================
# Why the withdrawn signals could never have worked -- kept as standing facts
# =======================================================================================


def test_the_location_vocabulary_is_an_area_kind_not_a_place_identity() -> None:
    """The whole discriminating power of the withdrawn rule, stated numerically.

    Four values for an entire community. ``BUILDING`` in particular is an area *kind*: two
    different buildings in one community are both ``BUILDING``, so agreement on it says
    nothing about whether two reports describe one place, let alone one incident.
    """

    assert len(tuple(LocationAreaCode)) == 4, tuple(LocationAreaCode)
    assert "BUILDING" in {code.value for code in LocationAreaCode}


async def test_the_candidate_summary_never_carries_a_location(harness: MonitorHarness) -> None:
    """And the extension path could not have compared one even if it wanted to.

    ``MonitorCandidateSummary`` has the field; the application always supplies ``None``. So a
    location rule was structurally incapable of reaching the extension path -- which is the
    mechanical reason NEW-2 existed, underneath the design reason.
    """

    await harness.seed()
    (anchor,) = await _ingest(
        harness,
        (("summary-anchor", "resident-a", "The lift stopped between floors this morning."),),
        offset=3200,
    )
    case_id = await _store_case(
        harness,
        issue_type=IssueType.ELEVATOR_FAILURE.value,
        title="Recurring lift failures",
        anchor=anchor,
    )
    (later,) = await _ingest(
        harness,
        (("summary-later", "resident-c", "The lift is stuck again this afternoon."),),
        offset=3210,
    )
    seen: list[object] = []

    def observe(invocation: MonitorInvocation) -> MonitorOutput:
        seen.extend(invocation.payload.candidate_case_summaries)
        return MonitorOutput(
            message_results=classify_all(invocation.payload, set()),
            proposed_reports=(),
            proposed_facts=(),
            candidate_links=(),
        )

    await harness.run_monitor(ScriptedMonitorAgent(responder=observe)).execute(
        harness.monitor_command((anchor, later), invocation_id=uuid4())
    )

    assert any(summary.case_id == case_id.value for summary in seen), (  # type: ignore[attr-defined]
        "the case must have been offered as an extension candidate"
    )
    assert all(summary.location_area is None for summary in seen)  # type: ignore[attr-defined]
