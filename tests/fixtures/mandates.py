"""A discovered case with four owners, built through the real discovery path.

The case these tests decide mandates about is *found*, not staged. Messages go in through the
ingestion route, a Monitor answer is validated, and the apply plan writes the reports, facts,
and candidate case. Only the agent is scripted, which is the same substitution every Phase-3
contract test makes: everything from the route down is the production path.

That matters more here than convenience would suggest. A mandate proposal is derived from the
facts a case actually holds, so a fixture that staged facts directly would be testing the
proposal builder against inputs the system cannot produce. Resident B's health detail, unit
label, name, and photo description reach the case exactly the way the demo corpus makes them
reach it: as typed facts the Monitor proposed and deterministic validation accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
from fastapi.testclient import TestClient

from chorus.application.commands.decide_mandate import DecideMandateCommand
from chorus.contracts.monitor import (
    CandidateLink,
    EvidenceDescriptionValue,
    HealthDetailValue,
    IdentityAttributeValue,
    IncidentOccurrenceValue,
    IssueType,
    MonitorInput,
    MonitorMessage,
    MonitorOutput,
    ProposedFact,
    ServiceImpactValue,
    UnitLocationValue,
)
from chorus.domain.entities import DisclosureScope, FactType, SensitivityCategory
from chorus.domain.facts import (
    EvidenceMediaKind,
    FailureMode,
    ImpactCode,
    SubjectRelation,
)
from chorus.domain.ids import CaseId, ContributorId, FactId, MandateId
from chorus.domain.mandates import FactGrant, IdentityGrant, MandateDecision
from chorus.domain.time import parse_utc
from chorus.infrastructure.local.dispatch import InProcessOperationDispatcher
from chorus.infrastructure.local.monitor_agent import ScriptedMonitorAgent
from chorus.ports.agents import MonitorInvocation
from chorus.ports.pagination import PageRequest
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import StorageDriver
from tests.contract.api.conftest import ApiHarness, build_harness
from tests.fixtures.monitor import DESTINATION_ID, resident_actor_hash
from tests.fixtures.monitor_answers import classify_all, report_for, whole_span

BASE = datetime(2030, 1, 14, 8, 0, tzinfo=UTC)

RESIDENTS: tuple[str, ...] = ("resident-a", "resident-b", "resident-c", "resident-d")
ACTOR_BY_PSEUDONYM: dict[str, str] = {
    "resident-a": "resident_a",
    "resident-b": "resident_b",
    "resident-c": "resident_c",
    "resident-d": "resident_d",
}
SENSITIVE_OWNER = "resident-b"
"""The resident whose messages carry the facts policy/v1 treats differently."""


def _incident(message: MonitorMessage, report_ref: str) -> ProposedFact:
    return ProposedFact(
        client_ref=f"{report_ref}-incident",
        report_client_ref=report_ref,
        fact_type=FactType.INCIDENT_OCCURRENCE,
        typed_value=IncidentOccurrenceValue(
            fact_type=FactType.INCIDENT_OCCURRENCE,
            occurred_at=message.sent_at,
            failure_mode=FailureMode.OUT_OF_SERVICE,
        ),
        sensitivity=SensitivityCategory.GENERAL,
        source_spans=(whole_span(message),),
    )


def _sensitive_facts(message: MonitorMessage, report_ref: str) -> tuple[ProposedFact, ...]:
    """The extra facts Resident B's messages carry in the frozen demo corpus.

    One of each interesting policy shape: an impact and a photo description that may be
    exported, a name that additionally needs a separate identity grant, and a unit label and a
    health detail that policy/v1 never lets leave the building whatever anybody grants.
    """

    return (
        ProposedFact(
            client_ref=f"{report_ref}-impact",
            report_client_ref=report_ref,
            fact_type=FactType.SERVICE_IMPACT,
            typed_value=ServiceImpactValue(
                fact_type=FactType.SERVICE_IMPACT,
                impact_code=ImpactCode.TRAPPED,
                summary="stuck between floors for five minutes",
            ),
            sensitivity=SensitivityCategory.GENERAL,
            source_spans=(whole_span(message),),
        ),
        ProposedFact(
            client_ref=f"{report_ref}-identity",
            report_client_ref=report_ref,
            fact_type=FactType.IDENTITY_ATTRIBUTE,
            typed_value=IdentityAttributeValue(
                fact_type=FactType.IDENTITY_ATTRIBUTE, display_name="Resident B"
            ),
            sensitivity=SensitivityCategory.IDENTITY,
            source_spans=(whole_span(message),),
        ),
        ProposedFact(
            client_ref=f"{report_ref}-unit",
            report_client_ref=report_ref,
            fact_type=FactType.UNIT_LOCATION,
            typed_value=UnitLocationValue(fact_type=FactType.UNIT_LOCATION, unit_label="4B"),
            sensitivity=SensitivityCategory.UNIT_LOCATION,
            source_spans=(whole_span(message),),
        ),
        ProposedFact(
            client_ref=f"{report_ref}-health",
            report_client_ref=report_ref,
            fact_type=FactType.HEALTH_DETAIL,
            typed_value=HealthDetailValue(
                fact_type=FactType.HEALTH_DETAIL,
                subject_relation=SubjectRelation.FAMILY,
                detail="asthma, and the enclosed space caused panic",
            ),
            sensitivity=SensitivityCategory.HEALTH,
            source_spans=(whole_span(message),),
        ),
        ProposedFact(
            client_ref=f"{report_ref}-evidence",
            report_client_ref=report_ref,
            fact_type=FactType.EVIDENCE_DESCRIPTION,
            typed_value=EvidenceDescriptionValue(
                fact_type=FactType.EVIDENCE_DESCRIPTION,
                description="a photograph of the E42 error on the lift panel",
                media_kind=EvidenceMediaKind.IMAGE,
            ),
            sensitivity=SensitivityCategory.GENERAL,
            source_spans=(whole_span(message),),
        ),
    )


def four_owner_answer(invocation: MonitorInvocation) -> MonitorOutput:
    """One candidate case over four residents, with Resident B carrying the sensitive facts.

    Ownership is read from each message's pseudonym rather than from its position, because the
    bounded projection orders messages by time and a fixture that assumed an index would attach
    the health detail to whoever happened to be first.
    """

    payload: MonitorInput = invocation.payload
    messages = payload.messages
    reports = tuple(
        report_for(message, f"report-{index:03d}", IssueType.ELEVATOR_FAILURE)
        for index, message in enumerate(messages)
    )
    facts: list[ProposedFact] = []
    for index, message in enumerate(messages):
        ref = f"report-{index:03d}"
        facts.append(_incident(message, ref))
        if message.contributor_pseudonym_id == SENSITIVE_OWNER:
            facts.extend(_sensitive_facts(message, ref))
    return MonitorOutput(
        message_results=classify_all(payload, {message.message_id for message in messages}),
        proposed_reports=reports,
        proposed_facts=tuple(facts),
        candidate_links=tuple(
            CandidateLink(
                report_client_ref=report.client_ref,
                candidate_group_ref="elevator",
                proposed_case_title="Recurring elevator failures",
                similarity_reasons=("four residents report the same lift",),
                confidence="0.9",
            )
            for report in reports
        ),
    )


@dataclass(slots=True)
class MandateWorld:
    """A wired API over a discovered case, plus the helpers a mandate test needs."""

    api: ApiHarness
    case_id: CaseId

    @property
    def client(self) -> TestClient:
        return self.api.client

    @property
    def case_scope(self) -> CaseScope:
        return CaseScope(
            namespace=self.api.harness.namespace,
            community_id=self.api.harness.community_id,
            case_id=self.case_id,
        )

    def contributor_id(self, pseudonym: str) -> ContributorId:
        return self.api.harness.contributor_id(pseudonym)

    def actor_for(self, pseudonym: str) -> str:
        return ACTOR_BY_PSEUDONYM[pseudonym]

    # -- HTTP ---------------------------------------------------------------------------

    def propose(
        self,
        *,
        expected_case_version: int,
        key: str = "propose-mandates-0001",
        actor: str | None = None,
    ) -> httpx.Response:
        return _sent(
            self.client.post(
                f"/v1/cases/{self.case_id}/mandates",
                json={"expected_case_version": expected_case_version},
                headers=self.api.actor_headers(
                    actor or "presenter_admin", **{"Idempotency-Key": key}
                ),
            )
        )

    def thread(self, pseudonym: str, *, actor: str | None = None) -> httpx.Response:
        return _sent(
            self.client.get(
                f"/v1/contributors/{self.contributor_id(pseudonym)}/mandates/current",
                params={"case_id": str(self.case_id)},
                headers=self.api.actor_headers(actor or self.actor_for(pseudonym)),
            )
        )

    def decide(
        self,
        pseudonym: str,
        mandate_id: str,
        body: dict[str, Any],
        *,
        key: str,
        actor: str | None = None,
    ) -> httpx.Response:
        return _sent(
            self.client.post(
                f"/v1/cases/{self.case_id}/mandates/{mandate_id}/decisions",
                json=body,
                headers=self.api.actor_headers(
                    actor or self.actor_for(pseudonym), **{"Idempotency-Key": key}
                ),
            )
        )

    # -- state --------------------------------------------------------------------------

    async def case_version(self) -> int:
        return (await self.api.harness.core.load_case(self.case_scope)).version

    async def accept_candidate(self) -> httpx.Response:
        """Move the discovered case to AWAITING_MANDATES with its proposals, once."""

        return self.propose(expected_case_version=await self.case_version())


async def build_mandate_world(
    storage: StorageDriver, *, prefix: str = "mandate", seed: bool = True
) -> MandateWorld:
    """Seed a community, ingest four messages, and let the real path discover one case.

    ``prefix`` distinguishes a *second* world over the same storage. Channel identifiers are
    unique per community, so two worlds sharing a prefix ingest the same messages, replay them,
    and derive the same case -- which would make a "foreign case" test quietly assert something
    about the case it already had. ``seed`` is off for that second world because the community
    and its contributors are create-only and already exist.
    """

    # ``ids_prefix`` is what keeps two worlds over one namespace apart. Each harness builds a
    # deterministic UUID5 generator from the namespace alone, so without it the second world
    # mints the same message identifiers as the first and its create-only writes collide.
    api = build_harness(
        storage,
        "in-process",
        agent=ScriptedMonitorAgent(responder=four_owner_answer),
        ids_prefix=prefix,
    )
    api.client.__enter__()
    if seed:
        await api.harness.seed()

    body = {
        "community_id": str(api.harness.community_id),
        "messages": [
            {
                "adapter": "SYNTHETIC",
                "channel_message_id": f"{prefix}-{index:03d}",
                "contributor_id": str(api.harness.contributor_id(pseudonym)),
                "sent_at": (BASE + timedelta(hours=index)).isoformat().replace("+00:00", "Z"),
                "text": f"The lift was out of service again this morning ({pseudonym}).",
                "attachments": [],
            }
            for index, pseudonym in enumerate(RESIDENTS)
        ],
    }
    response = api.client.post(
        "/v1/ingest/messages",
        json=body,
        headers=api.presenter_headers(**{"Idempotency-Key": f"{prefix}-world-ingest-0001"}),
    )
    assert response.status_code == 202, response.text
    # The dispatcher runs the worker as a background task, so the case does not exist the
    # instant the route answers 202. Draining is the deterministic point at which it does --
    # waiting on the job's own completion event rather than sleeping and hoping.
    assert isinstance(api.dispatcher, InProcessOperationDispatcher)
    await api.dispatcher.drain()
    return MandateWorld(api=api, case_id=await _discovered_case_id(api, prefix=prefix))


async def _discovered_case_id(api: ApiHarness, *, prefix: str) -> CaseId:
    """Find the case this batch created, by the exact messages it ingested.

    A strong ``BatchGetItem`` on the signals of *these* message identifiers, rather than a page
    over every signal in the community: a second world in the same community would otherwise
    see both cases and have no way to say which one it just made.
    """

    scope = api.harness.core_scope
    entries = await api.harness.core.read_message_feed(
        scope,
        start=BASE - timedelta(days=1),
        end=BASE + timedelta(days=1),
        request=PageRequest(limit=100),
    )
    mine = tuple(
        message.message_id
        for message in entries.items
        if message.channel_message_id.startswith(f"{prefix}-")
    )
    signals = await api.harness.core.load_feed_signals(scope, mine)
    case_ids = {signal.case_id for signal in signals.values()}
    assert len(case_ids) == 1, f"expected exactly one discovered case, saw {case_ids}"
    return next(iter(case_ids))


# -- request builders -------------------------------------------------------------------


def fact_ids_by_type(thread: dict[str, Any]) -> dict[str, str]:
    """Map each fact type in a mandate thread to its identifier, for building decisions."""

    return {row["fact_type"]: row["fact_id"] for row in thread["fact_permissions"]}


def grant(fact_id: str, scope: str, *, transform: bool | None = None) -> dict[str, Any]:
    return {
        "fact_id": fact_id,
        "max_scope": scope,
        "allow_safe_transformation": (scope != "INTERNAL_ONLY" if transform is None else transform),
    }


def approve_body(thread: dict[str, Any]) -> dict[str, Any]:
    """The body that approves exactly the proposed terms, reproduced from the thread."""

    return {
        "expected_version": thread["current_version"],
        "decision": "APPROVE",
        "fact_grants": [
            grant(row["fact_id"], row["proposed_scope"], transform=row["allow_safe_transformation"])
            for row in thread["fact_permissions"]
        ],
        "identity_grant": {
            "externally_shareable": thread["identity_permission"]["externally_shareable"],
            "max_scope": thread["identity_permission"]["max_scope"],
        },
        "expires_at": None,
    }


def adjust_body(
    thread: dict[str, Any],
    scopes: dict[str, str],
    *,
    identity: dict[str, Any] | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """A complete replacement grant set, choosing a scope per fact type."""

    return {
        "expected_version": thread["current_version"],
        "decision": "ADJUST",
        "fact_grants": [
            grant(row["fact_id"], scopes.get(row["fact_type"], "INTERNAL_ONLY"))
            for row in thread["fact_permissions"]
        ],
        "identity_grant": identity
        or {"externally_shareable": False, "max_scope": "ANONYMOUS_CASE"},
        "expires_at": expires_at,
    }


def terminal_body(thread: dict[str, Any], decision: str) -> dict[str, Any]:
    """A refusal or revocation, which may carry no grant of any kind."""

    return {
        "expected_version": thread["current_version"],
        "decision": decision,
        "fact_grants": [],
        "identity_grant": {"externally_shareable": False, "max_scope": "ANONYMOUS_CASE"},
        "expires_at": None,
    }


def _sent(response: object) -> httpx.Response:
    """Narrow the test client's loosely typed return to the response it actually is."""

    assert isinstance(response, httpx.Response)
    return response


def decision_command(
    world: MandateWorld,
    pseudonym: str,
    thread: dict[str, Any],
    body: dict[str, Any],
    *,
    key: str,
) -> DecideMandateCommand:
    """Build the exact command the route would build, for a caller that is not the route.

    Used where a test needs a *second concurrent caller* driving the real use case. The HTTP
    client cannot be re-entered from inside an in-flight request -- its portal refuses a call
    from the event loop thread -- so the twin goes in one layer below the transport and through
    everything else the transport would have reached.
    """

    actor = ACTOR_BY_PSEUDONYM[pseudonym]
    return DecideMandateCommand(
        namespace=world.api.harness.namespace,
        community_id=world.api.harness.community_id,
        case_id=world.case_id,
        mandate_id=MandateId(UUID(thread["mandate_id"])),
        actor_contributor_id=world.contributor_id(pseudonym),
        actor_id_hash=resident_actor_hash(actor),
        expected_version=body["expected_version"],
        decision=MandateDecision(body["decision"]),
        fact_grants=tuple(
            FactGrant(
                fact_id=FactId(UUID(item["fact_id"])),
                max_scope=DisclosureScope(item["max_scope"]),
                allow_safe_transformation=item["allow_safe_transformation"],
            )
            for item in body["fact_grants"]
        ),
        identity_grant=IdentityGrant(
            externally_shareable=body["identity_grant"]["externally_shareable"],
            max_scope=DisclosureScope(body["identity_grant"]["max_scope"]),
        ),
        expires_at=None if body["expires_at"] is None else parse_utc(body["expires_at"]),
        idempotency_key=key,
        destination_id=DESTINATION_ID,
    )


def json_of(response: httpx.Response) -> dict[str, Any]:
    return cast("dict[str, Any]", response.json())
