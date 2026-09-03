"""NEW-1: the validator may require nothing the reviewed prompt does not ask for.

A hidden validator requirement is not a stricter system, it is a broken one. Validation is
whole-output, so a rule the model was never told about does not reject one group -- it settles
the operation ``FAILED`` and writes nothing for the entire batch, including the reports, facts,
and classifications that had no connection to the rule at all.

So this file reads the two artifacts against each other. One direction: an answer written by
following ``MONITOR_SYSTEM_PROMPT``, supplying only what the prompt asks for, is accepted. The
other: the rule the prompt *does* state is genuinely enforced, and fails closed with its own
named code rather than being advisory.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.fixtures.monitor import MonitorHarness
from tests.fixtures.monitor_answers import classify_all, fact_for, report_for

from chorus.contracts.common import MONITOR_PROMPT_VERSION
from chorus.contracts.monitor import (
    CandidateLink,
    IssueType,
    MonitorOutput,
    ProposedReport,
)
from chorus.ports.agents import AgentContractViolationError, MonitorInvocation
from chorus.ports.ambient import AmbientMessage
from chorus.ports.records import MessageFeedEntry
from chorus.ports.scopes import CaseScope

pytestmark = pytest.mark.anyio


async def _ingest(
    harness: MonitorHarness, entries: tuple[tuple[str, str, str], ...], *, offset: int
) -> tuple[MessageFeedEntry, ...]:
    anchor = harness.adapter.messages()[-1].sent_at
    batch = tuple(
        AmbientMessage(
            adapter="SYNTHETIC",
            channel_message_id=entry[0],
            contributor_pseudonym=entry[1],
            sent_at=anchor + timedelta(minutes=offset + index),
            text=entry[2],
        )
        for index, entry in enumerate(entries)
    )
    result = await harness.ingest_messages(batch, idempotency_key=f"contract-key-{offset}")
    return tuple(
        MessageFeedEntry(message_id=item.message_id, sent_at=message.sent_at)
        for item, message in zip(result.messages, batch, strict=True)
    )


def _prompt_text() -> str:
    """Everything the reviewed runtime actually sends as instructions."""

    from runtimes.monitor import prompt as monitor_prompt

    return "".join(value for value in vars(monitor_prompt).values() if isinstance(value, str))


# ---------------------------------------------------------------------------------------
# The two artifacts, read against each other
# ---------------------------------------------------------------------------------------


def silently_optional_report_fields() -> frozenset[str]:
    """Report fields the model may omit and that the prompt never asks it to send.

    This is NEW-1 stated structurally rather than field by field. Any field in here must be
    safe to leave out: the model has no instruction to fill it, so requiring it is requiring
    something nobody asked for. A new optional field added without a prompt line joins this
    set automatically, and the acceptance test below then has to keep passing with it unset.
    """

    text = _prompt_text()
    return frozenset(
        name
        for name, field in ProposedReport.model_fields.items()
        if not field.is_required() and name not in text
    )


def test_location_area_is_one_of_the_fields_the_prompt_never_asks_for() -> None:
    """The finding, pinned: this is the field the withdrawn rule made mandatory."""

    assert "location_area" in silently_optional_report_fields()


def test_the_prompt_states_the_grouping_rule_the_validator_enforces() -> None:
    """The converse duty: a rule that *is* enforced must appear in the reviewed text."""

    text = _prompt_text()
    assert "OTHER" in text
    assert "candidate_group_ref no other report shares" in text
    assert "do not link it to an existing case" in text


def test_the_prompt_version_moved_with_the_rule() -> None:
    """A changed instruction is a changed prompt identity, or no runtime re-review happens."""

    from runtimes.monitor.prompt import MONITOR_PROMPT_VERSION as runtime_version

    assert MONITOR_PROMPT_VERSION == "monitor/v2"
    assert runtime_version == MONITOR_PROMPT_VERSION


# ---------------------------------------------------------------------------------------
# A prompt-conformant answer is accepted
# ---------------------------------------------------------------------------------------


async def test_an_answer_supplying_only_what_the_prompt_asks_for_is_accepted(
    harness: MonitorHarness,
) -> None:
    """The regression NEW-1 named: this exact answer used to fail the whole batch.

    Nothing here is omitted carelessly: every field the prompt never names is left at its
    contract default, enumerated rather than listed by hand. The grouping is exactly what the
    prompt does ask for -- one label, one title, one issue type, for two reports about one
    problem.
    """

    await harness.seed()
    locators = await _ingest(
        harness,
        (
            ("conformant-a", "resident-a", "The lift stopped between floors again this morning."),
            ("conformant-b", "resident-b", "Confirming the lift is stuck, same as reported."),
            ("conformant-c", "resident-c", "Reminder that the residents social is on Saturday."),
        ),
        offset=3300,
    )
    wanted = {locators[0].message_id.value, locators[1].message_id.value}

    # Every field the prompt never mentions is left at its default, so acceptance here proves
    # the validator asks for nothing the model was not told to send.
    defaults = {
        name: ProposedReport.model_fields[name].get_default(call_default_factory=True)
        for name in silently_optional_report_fields()
    }
    assert "location_area" in defaults

    def conformant(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        picked = [message for message in payload.messages if message.message_id in wanted]
        return MonitorOutput(
            message_results=classify_all(payload, wanted),
            proposed_reports=tuple(
                report_for(message, f"report-{index:03d}", IssueType.ELEVATOR_FAILURE).model_copy(
                    update=defaults
                )
                for index, message in enumerate(picked)
            ),
            proposed_facts=tuple(
                fact_for(message, f"fact-{index:03d}", f"report-{index:03d}")
                for index, message in enumerate(picked)
            ),
            candidate_links=tuple(
                CandidateLink(
                    report_client_ref=f"report-{index:03d}",
                    candidate_group_ref="lift-group",
                    proposed_case_title="Lift stopping between floors",
                    similarity_reasons=("same equipment, both residents describe it",),
                    confidence="0.8",
                )
                for index in range(len(picked))
            ),
        )

    from chorus.infrastructure.local.monitor_agent import ScriptedMonitorAgent

    result = await harness.run_monitor(ScriptedMonitorAgent(responder=conformant)).execute(
        harness.monitor_command(locators)
    )

    assert len(result.created_case_ids) == 1
    case = await harness.core.load_case(
        CaseScope(
            namespace=harness.namespace,
            community_id=harness.community_id,
            case_id=result.created_case_ids[0],
        )
    )
    assert len(case.report_ids) == 2
    assert all(report is not None for report in case.report_ids)


async def test_the_documented_rule_fails_closed_when_the_model_ignores_it(
    harness: MonitorHarness,
) -> None:
    """The rule the prompt states is enforced, with its own code, and refuses the whole batch.

    The batch deliberately also carries a perfectly good unrelated observation, so the blast
    radius is visible: whole-output refusal means that one too. That is the accepted cost of
    never half-accepting an answer, and it is only acceptable because the prompt now tells the
    model exactly how to avoid it.
    """

    from chorus.domain.entities import ApplicationOperationStatus
    from chorus.infrastructure.local.monitor_agent import ScriptedMonitorAgent
    from chorus.ports.scopes import CommunityScope

    await harness.seed()
    locators = await _ingest(
        harness,
        (
            ("ignored-a", "resident-a", "The bins by the side entrance are overflowing again."),
            ("ignored-b", "resident-b", "The parcel locker keypad has stopped accepting codes."),
            ("ignored-c", "resident-c", "The lift is stuck between floors again."),
        ),
        offset=3400,
    )
    merged = {locators[0].message_id.value, locators[1].message_id.value}

    def ignores_the_rule(invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        picked = [message for message in payload.messages if message.message_id in merged]
        return MonitorOutput(
            message_results=classify_all(payload, merged),
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
                    candidate_group_ref="one-group",
                    proposed_case_title="Building maintenance",
                    similarity_reasons=("ignoring the stated rule",),
                    confidence="0.9",
                )
                for index in range(len(picked))
            ),
        )

    agent = ScriptedMonitorAgent(responder=ignores_the_rule)

    with pytest.raises(AgentContractViolationError) as raised:
        await harness.run_monitor(agent).execute(harness.monitor_command(locators))
    assert raised.value.reason_codes == ("CANDIDATE_GROUP_UNPROVABLE",), (
        "the refusal must name exactly the documented rule, and nothing else"
    )

    # And measured at the worker, the whole operation settles terminal with nothing durable.
    _operation, job = await harness.dispatched(locators)
    settled = await harness.worker(ScriptedMonitorAgent(responder=ignores_the_rule)).execute(job)
    assert settled.status is ApplicationOperationStatus.FAILED
    assert settled.result_refs == ()
    signals = await harness.core.load_feed_signals(
        CommunityScope(namespace=harness.namespace, community_id=harness.community_id),
        tuple(item.message_id for item in locators),
    )
    assert signals == {}
