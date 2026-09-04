"""The happy path, end to end: one case in, one assessment and one readiness decision out."""

from __future__ import annotations

import pytest
from tests.fixtures.investigation import InvestigationHarness
from tests.fixtures.investigation_answers import cooperative

from chorus.domain.entities import ApplicationOperationStatus, CaseState, EvidenceStatus
from chorus.infrastructure.local.investigator_agent import ScriptedInvestigatorAgent

pytestmark = pytest.mark.anyio


async def test_a_corroborated_case_becomes_ready_and_records_its_assessment(
    harness: InvestigationHarness,
) -> None:
    await harness.seed()
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    result = await harness.run_investigation(agent).execute(harness.command())

    assert result.replayed is False
    assert result.independent_source_count == 2
    assert result.is_corroborated is True
    case = await harness.case()
    assert case.state is CaseState.READY_FOR_ACTION
    assert case.assessment_id == result.assessment_id
    assert case.corroboration_source_count == 2
    assert case.version == 2


async def test_the_identical_claim_corroborates_while_the_unique_one_stays_reported(
    harness: InvestigationHarness,
) -> None:
    """AC and AD together: a case can be corroborated and still hold ``REPORTED`` facts."""

    await harness.seed()
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    await harness.run_investigation(agent).execute(harness.command())

    # Both reporters asserted the byte-identical LOCATION_AREA value.
    assert await harness.status_of("location:resident-a") is EvidenceStatus.CORROBORATED
    assert await harness.status_of("location:resident-b") is EvidenceStatus.CORROBORATED
    # Each incident instant is one person's account and nobody else's.
    assert await harness.status_of("incident:resident-a") is EvidenceStatus.REPORTED
    assert await harness.status_of("incident:resident-b") is EvidenceStatus.REPORTED


async def test_the_worker_runs_the_operation_to_succeeded(
    harness: InvestigationHarness,
) -> None:
    await harness.seed()
    operation = await harness.bound_operation()
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    settled = await harness.worker(agent).execute(harness.job_for(operation))

    assert settled.status is ApplicationOperationStatus.SUCCEEDED
    assert len(agent.invocations) == 1
