"""Case Y: what an investigation writes down, and everything it must never write down.

The Investigator sees the widest private payload in the system -- health details, unit labels,
private quotes, extracted evidence text -- so this is where "logs omit private content" is
either true or the whole invariant is decorative.

The test plants sentinels in every private field a case can carry, runs a real investigation
with real emitters through the real content-safe formatter, and asserts that no sentinel
appears anywhere in the captured output. It also asserts the *positive* half: the events the
frozen observability table requires are actually emitted, because a formatter that dropped
everything would pass a leak test and answer no operational question at all.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import timedelta

import pytest
from tests.fixtures.investigation import InvestigationHarness, SeededFact
from tests.fixtures.investigation_answers import contradiction_over, cooperative

from chorus.application import observability
from chorus.domain.entities import (
    ContradictionMateriality,
    EvidenceStatus,
    FactType,
    SensitivityCategory,
)
from chorus.domain.facts import (
    FailureMode,
    HealthDetail,
    ImpactCode,
    IncidentOccurrence,
    ServiceImpact,
    SubjectRelation,
    UnitLocation,
)
from chorus.infrastructure.local.investigator_agent import ScriptedInvestigatorAgent
from chorus.infrastructure.observability.logging import ContentSafeJsonFormatter

pytestmark = pytest.mark.anyio

SENTINELS = (
    "SENTINEL_HEALTH_ASTHMA",
    "SENTINEL_UNIT_4B",
    "SENTINEL_PRIVATE_QUOTE",
    "SENTINEL_CASE_TITLE",
)


class CapturingHandler(logging.Handler):
    """Renders every record through the production formatter and keeps the JSON."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(ContentSafeJsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def captured() -> Iterator[CapturingHandler]:
    handler = CapturingHandler()
    logger = logging.getLogger(observability.LOGGER_NAME)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


async def _seed_private_case(harness: InvestigationHarness) -> None:
    await harness.seed(
        facts=(
            SeededFact(
                label="impact:a",
                reporter="resident-a",
                fact_type=FactType.SERVICE_IMPACT,
                value=ServiceImpact(
                    impact_code=ImpactCode.TRAPPED,
                    summary=f"They were trapped and said {SENTINELS[2]}.",
                ),
            ),
            SeededFact(
                label="health:a",
                reporter="resident-a",
                fact_type=FactType.HEALTH_DETAIL,
                sensitivity=SensitivityCategory.HEALTH,
                value=HealthDetail(
                    subject_relation=SubjectRelation.FAMILY,
                    detail=f"Her mother has {SENTINELS[0]}.",
                ),
            ),
            SeededFact(
                label="unit:a",
                reporter="resident-a",
                fact_type=FactType.UNIT_LOCATION,
                sensitivity=SensitivityCategory.UNIT_LOCATION,
                value=UnitLocation(unit_label=SENTINELS[1]),
            ),
            SeededFact(
                label="incident:b",
                reporter="resident-b",
                fact_type=FactType.INCIDENT_OCCURRENCE,
                value=IncidentOccurrence(
                    occurred_at=harness.clock.now() - timedelta(days=1),
                    failure_mode=FailureMode.STUCK,
                ),
            ),
            SeededFact(
                label="incident:a",
                reporter="resident-a",
                fact_type=FactType.INCIDENT_OCCURRENCE,
                value=IncidentOccurrence(
                    occurred_at=harness.clock.now() - timedelta(days=2),
                    failure_mode=FailureMode.STUCK,
                ),
            ),
        ),
    )


async def test_no_private_sentinel_reaches_the_log(
    harness: InvestigationHarness, captured: CapturingHandler
) -> None:
    await _seed_private_case(harness)
    cited = (
        harness.fact_id("incident:a").value,
        harness.fact_id("incident:b").value,
    )
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(
            proposed={harness.fact_id("impact:a").value: EvidenceStatus.VERIFIED},
            contradictions=(contradiction_over(cited, materiality=ContradictionMateriality.LOW),),
        )
    )

    await harness.run_investigation(agent).execute(harness.command())

    blob = "\n".join(captured.lines)
    assert blob, "the investigation emitted no events at all"
    for sentinel in SENTINELS:
        assert sentinel not in blob


async def test_the_required_investigation_events_are_emitted(
    harness: InvestigationHarness, captured: CapturingHandler
) -> None:
    """The positive half: a formatter that dropped everything would pass a leak test."""

    await _seed_private_case(harness)
    cited = (
        harness.fact_id("incident:a").value,
        harness.fact_id("incident:b").value,
    )
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(
            proposed={harness.fact_id("impact:a").value: EvidenceStatus.VERIFIED},
            contradictions=(contradiction_over(cited, materiality=ContradictionMateriality.LOW),),
        )
    )

    await harness.run_investigation(agent).execute(harness.command())

    names = {json.loads(line)["event_name"] for line in captured.lines}
    assert {
        observability.EventName.AGENT_INVOCATION_STARTED,
        observability.EventName.INVESTIGATION_APPLIED,
        observability.EventName.EVIDENCE_INDEPENDENCE_COMPUTED,
        observability.EventName.CONTRADICTION_RECORDED,
        observability.EventName.EVIDENCE_STATUS_DOWNGRADED,
    } <= names


async def test_the_downgrade_event_carries_codes_and_identifiers_only(
    harness: InvestigationHarness, captured: CapturingHandler
) -> None:
    await _seed_private_case(harness)
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(proposed={harness.fact_id("impact:a").value: EvidenceStatus.VERIFIED})
    )

    await harness.run_investigation(agent).execute(harness.command())

    downgrades = [
        json.loads(line)
        for line in captured.lines
        if json.loads(line)["event_name"] == observability.EventName.EVIDENCE_STATUS_DOWNGRADED
    ]
    assert len(downgrades) == 1
    event = downgrades[0]
    assert event["entity_type"] == "FACT"
    assert event["entity_id"] == str(harness.fact_id("impact:a"))
    assert set(event["reason_codes"]) == {"COMPUTED_REPORTED", "PROPOSED_VERIFIED"}
    assert "rationale" not in event


async def test_the_applied_event_counts_every_resolved_status(
    harness: InvestigationHarness, captured: CapturingHandler
) -> None:
    """``VERIFIED`` is counted rather than assumed, so a non-zero count is a visible defect."""

    await _seed_private_case(harness)
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    await harness.run_investigation(agent).execute(harness.command())

    applied = next(
        json.loads(line)
        for line in captured.lines
        if json.loads(line)["event_name"] == observability.EventName.INVESTIGATION_APPLIED
    )
    assert applied["counts"]["VERIFIED"] == 0
    assert sum(applied["counts"].values()) == 5
