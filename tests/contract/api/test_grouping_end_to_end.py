"""NEW-2 through the whole path: HTTP -> dispatcher -> worker -> persistence.

The grouping invariant is enforced in the validator and again at the apply gate, and both are
covered directly elsewhere. What this file answers is the question a unit of either cannot: an
HTTP client posts messages, an operation is dispatched, a worker runs the real Monitor use case
against real storage, and *what is in the database afterwards*.

Every case here is seeded into storage directly rather than discovered. Under ADR-012 intake
can no longer produce an ``OTHER`` case at all, and the gate has to answer for one that exists
anyway -- so the fixture puts one there and lets the production path meet it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from tests.contract.api.conftest import ApiHarness, build_harness
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver
from tests.fixtures.monitor import MonitorHarness
from tests.fixtures.monitor_answers import classify_all, fact_for, report_for

from chorus.application.services.identity import derive_candidate_case_id
from chorus.contracts.monitor import CandidateLink, IssueType, MonitorOutput
from chorus.domain.entities import CaseState, CommunityCase
from chorus.domain.ids import CaseId, MessageId
from chorus.infrastructure.local.monitor_agent import ScriptedMonitorAgent
from chorus.ports.agents import MonitorInvocation
from chorus.ports.records import FeedSignalProjection
from chorus.ports.scopes import CaseScope, CommunityScope
from chorus.ports.storage import StorageDriver
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio

BASE = datetime(2030, 2, 1, 8, 0, tzinfo=UTC)


class _LinkToCase:
    """A Monitor that proposes every *new* message it is given for one existing case.

    Deliberately a well-formed answer -- classified, owned, anchored -- so grouping is the only
    thing under test. It is a class rather than a closure because the case identifier is only
    known after the fixture has staged it.
    """

    def __init__(self, *, issue: IssueType) -> None:
        self.issue = issue
        self.case_id: CaseId | None = None
        self.seen: list[MonitorInvocation] = []

    async def invoke_monitor(self, invocation: MonitorInvocation):  # type: ignore[no-untyped-def]
        self.seen.append(invocation)
        return await ScriptedMonitorAgent(responder=self._answer).invoke_monitor(invocation)

    def _answer(self, invocation: MonitorInvocation) -> MonitorOutput:
        payload = invocation.payload
        assert self.case_id is not None
        summary = next(
            item for item in payload.candidate_case_summaries if item.case_id == self.case_id.value
        )
        # Only the messages not already bound to the case may be proposed; the anchor is
        # already linked, and re-proposing it would be refused as a relink rather than for the
        # grouping reason this test is about.
        anchored = {invocation.payload.messages[0].message_id}
        fresh = [item for item in payload.messages if item.message_id not in anchored]
        return MonitorOutput(
            message_results=classify_all(payload, {item.message_id for item in fresh}),
            proposed_reports=tuple(
                report_for(message, f"report-{index:03d}", self.issue)
                for index, message in enumerate(fresh)
            ),
            proposed_facts=tuple(
                fact_for(message, f"fact-{index:03d}", f"report-{index:03d}")
                for index, message in enumerate(fresh)
            ),
            candidate_links=tuple(
                CandidateLink(
                    report_client_ref=f"report-{index:03d}",
                    existing_case_id=self.case_id.value,
                    proposed_case_title=summary.title,
                    similarity_reasons=("the model is sure these belong together",),
                    confidence="0.95",
                )
                for index in range(len(fresh))
            ),
        )


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    yield from storage_driver(str(request.param), prefix="grouping-e2e")


def _message(pseudonym: str, channel_id: str, minute: int, text: str) -> dict[str, Any]:
    return {
        "adapter": "SYNTHETIC",
        "channel_message_id": channel_id,
        "pseudonym": pseudonym,
        "sent_at": (BASE + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
        "text": text,
    }


def _post(api: ApiHarness, messages: list[dict[str, Any]], *, key: str) -> dict[str, Any]:
    body = {
        "community_id": str(api.harness.community_id),
        "messages": [
            {
                "adapter": item["adapter"],
                "channel_message_id": item["channel_message_id"],
                "contributor_id": str(api.harness.contributor_id(item["pseudonym"])),
                "sent_at": item["sent_at"],
                "text": item["text"],
                "attachments": [],
            }
            for item in messages
        ],
    }
    response = api.client.post(
        "/v1/ingest/messages",
        json=body,
        headers=api.presenter_headers(**{"Idempotency-Key": key}),
    )
    assert response.status_code == 202, response.text
    return cast("dict[str, Any]", response.json())


def _operation(api: ApiHarness, body: dict[str, Any]) -> dict[str, Any]:
    response = api.client.get(
        f"/v1/operations/{body['operation']['operation_id']}",
        headers=api.presenter_headers(),
    )
    return cast("dict[str, Any]", response.json())


async def _seed_case(
    harness: MonitorHarness, *, issue_type: str, title: str, anchor: MessageId
) -> CaseId:
    """Stage a case and point ``anchor``'s feed signal at it, so a later run is offered it."""

    now = harness.clock.now()
    case_id = CaseId(
        derive_candidate_case_id(
            namespace=harness.namespace,
            community_id=harness.community_id,
            issue_type=issue_type,
            report_ids=(),
        ).value
    )
    case_scope = CaseScope(
        namespace=harness.namespace, community_id=harness.community_id, case_id=case_id
    )
    community = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    await harness.unit_of_work.commit(
        TransactionPlan(
            name="seed-case-for-e2e",
            operations=(
                harness.core.stage_create_case(
                    case_scope,
                    CommunityCase(
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
                    ),
                ),
                harness.core.stage_create_feed_signal(
                    community,
                    FeedSignalProjection(
                        namespace=harness.namespace,
                        community_id=harness.community_id,
                        message_id=anchor,
                        case_id=case_id,
                        case_version=1,
                        label=title,
                        related_message_count=1,
                        case_state=CaseState.CANDIDATE,
                        detected_at=now,
                    ),
                ),
            ),
            audit_required=False,
        )
    )
    return case_id


async def _extend_over_http(
    storage: StorageDriver,
    *,
    case_issue_type: str,
    case_title: str,
    link_issue: IssueType,
    second_text: str,
) -> tuple[CommunityCase, dict[str, Any]]:
    """Drive the full path and return the case as stored afterwards, plus the operation."""

    agent = _LinkToCase(issue=link_issue)
    api = build_harness(storage, "in-process", agent=agent)
    with api.client:
        await api.harness.seed()
        first = _post(
            api,
            [
                _message(
                    "resident-a",
                    "e2e-anchor",
                    0,
                    "Water is leaking from the ceiling in Building A and pooling badly.",
                )
            ],
            key="e2e-anchor-key",
        )
        anchor = MessageId(UUID(first["messages"][0]["message_id"]))
        case_id = await _seed_case(
            api.harness, issue_type=case_issue_type, title=case_title, anchor=anchor
        )
        agent.case_id = case_id

        second = _post(
            api,
            [_message("resident-c", "e2e-second", 30, second_text)],
            key="e2e-second-key",
        )
        await api.dispatcher.drain()  # type: ignore[union-attr]
        operation = _operation(api, second)
        assert agent.seen, "the worker must actually have reached the Monitor"

        case = await api.harness.core.load_case(
            CaseScope(
                namespace=api.harness.namespace,
                community_id=api.harness.community_id,
                case_id=case_id,
            )
        )
        return case, operation


async def test_an_unrelated_report_cannot_extend_an_other_case_over_http(
    storage: StorageDriver,
) -> None:
    """NEW-2's exact scenario. Building A plumbing case, then an unrelated lift complaint."""

    case, operation = await _extend_over_http(
        storage,
        case_issue_type=IssueType.OTHER.value,
        case_title="Water leaking in Building A",
        link_issue=IssueType.OTHER,
        second_text="Completely unrelated: the lift in Building A has a heavy vibration now.",
    )

    assert operation["status"] == "FAILED"
    assert operation["error_code"] == "AGENT_CONTRACT_VIOLATION", (
        "the refusal must be the grouping invariant, not an unrelated failure the probe "
        "would otherwise pass on"
    )
    assert operation["result_refs"] == []
    assert case.report_ids == (), "an unrelated report reached the case through the real path"
    assert case.version == 1, "a refused extension still bumped the case"


async def test_even_the_same_incident_cannot_extend_an_other_case_over_http(
    storage: StorageDriver,
) -> None:
    """The positive-sounding counterpart, refused for the invariant rather than a judgement.

    This second report genuinely is the same leak. Deterministic code cannot tell it apart from
    the unrelated one above, so it is refused identically -- false separation, deliberately
    preferred to false merging.
    """

    case, operation = await _extend_over_http(
        storage,
        case_issue_type=IssueType.OTHER.value,
        case_title="Water leaking in Building A",
        link_issue=IssueType.OTHER,
        second_text="The ceiling leak in Building A is still going, the puddle is bigger today.",
    )

    assert operation["status"] == "FAILED"
    assert operation["error_code"] == "AGENT_CONTRACT_VIOLATION"
    assert case.report_ids == ()


async def test_a_named_case_still_extends_over_http(storage: StorageDriver) -> None:
    """The path is not broken, only narrowed: a case whose issue type names a subject grows."""

    case, operation = await _extend_over_http(
        storage,
        case_issue_type=IssueType.ELEVATOR_FAILURE.value,
        case_title="Recurring lift failures",
        link_issue=IssueType.ELEVATOR_FAILURE,
        second_text="The lift is stuck between floors again, same fault as the other reports.",
    )

    assert operation["status"] == "SUCCEEDED"
    assert operation["result_refs"] == [str(case.case_id.value)]
    assert len(case.report_ids) == 1, "the extending report landed through the real path"
    assert case.version > 1
