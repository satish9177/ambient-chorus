"""The Phase 5 adversarial matrix, run against the real use case over a real driver.

Each test is one row of the accepted matrix. The agent is the only substitution: the
projection, the validator, the independence recomputation, the status classification, the
readiness predicate, the compile preflight, and the single apply transaction are all the code
that runs in production.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from tests.fixtures.investigation import (
    NOW,
    BumpsTheCaseMidFlight,
    InvestigationHarness,
    SeededEvidence,
    SeededFact,
    SeededRoot,
)
from tests.fixtures.investigation_answers import (
    contradiction_over,
    cooperative,
    with_foreign_fact_citation,
    with_invented_finding,
)

from chorus.application.commands.run_investigation import (
    MAX_INVESTIGATION_FACT_UPDATES,
    InvestigationApplyDenial,
    InvestigationApplyDeniedError,
)
from chorus.contracts.investigation import (
    DuplicateEvidenceGroup,
    LinkageDecision,
    ProposedCommitment,
    RecommendedCaseDisposition,
)
from chorus.domain.entities import (
    CaseState,
    ContradictionMateriality,
    EvidenceStatus,
    FactType,
)
from chorus.domain.errors import IntegrityError
from chorus.domain.facts import (
    FactStatus,
    FailureMode,
    IncidentOccurrence,
    LocationArea,
    LocationAreaCode,
)
from chorus.domain.ids import ActionId, ApprovalId, ExecutionId, Sha256Digest, ViewId
from chorus.infrastructure.local.investigator_agent import ScriptedInvestigatorAgent
from chorus.ports.agents import (
    AgentContractViolationError,
    AgentDependencyError,
    AgentError,
    AgentErrorCode,
    AgentTimeoutError,
)
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.pagination import PageRequest
from chorus.ports.records import SendFence
from chorus.ports.scopes import CaseScope
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio

CAB = LocationArea(area=LocationAreaCode.ELEVATOR_CAB)
LOBBY = LocationArea(area=LocationAreaCode.LOBBY)


def location_fact(label: str, reporter: str, area: LocationAreaCode) -> SeededFact:
    return SeededFact(
        label=label,
        reporter=reporter,
        fact_type=FactType.LOCATION_AREA,
        value=LocationArea(area=area),
    )


# -- A / A2 / A3: forwarded root collapse, missing locator, cycle -----------------------------


async def test_a_forwarded_root_chain_resolves_through_the_locator(
    harness: InvestigationHarness,
) -> None:
    """Case A, and named test 22. Two reporters, one origin, one independent source."""

    await harness.seed(
        facts=(
            SeededFact(
                label="loc:a",
                reporter="resident-a",
                fact_type=FactType.LOCATION_AREA,
                value=CAB,
                evidence_labels=("original",),
            ),
            SeededFact(
                label="loc:b",
                reporter="resident-b",
                fact_type=FactType.LOCATION_AREA,
                value=CAB,
                evidence_labels=("forward",),
            ),
        ),
        roots=(SeededRoot(label="one"), SeededRoot(label="two", parent_label="one")),
        evidence=(
            SeededEvidence(label="original", reporter="resident-a", root_label="one"),
            SeededEvidence(label="forward", reporter="resident-b", root_label="two"),
        ),
    )
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    result = await harness.run_investigation(agent).execute(harness.command())

    assert result.independent_source_count == 1
    assert result.is_corroborated is False
    assert await harness.status_of("loc:a") is EvidenceStatus.REPORTED
    case = await harness.case()
    assert case.state is CaseState.INVESTIGATING


async def test_a_missing_root_locator_fails_closed_rather_than_under_counting(
    harness: InvestigationHarness,
) -> None:
    """Case A2. The backfill case: a root written before ADR-017 has no address.

    Failing loudly is the correct direction for a corroboration input. A silent under-count
    would look exactly like an honest 'these are the same file' answer.
    """

    await harness.seed(
        facts=(
            SeededFact(
                label="loc:a",
                reporter="resident-a",
                fact_type=FactType.LOCATION_AREA,
                value=CAB,
                evidence_labels=("original",),
            ),
        ),
        roots=(SeededRoot(label="one", write_locator=False),),
        evidence=(SeededEvidence(label="original", reporter="resident-a", root_label="one"),),
    )
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    with pytest.raises(IntegrityError):
        await harness.run_investigation(agent).execute(harness.command())

    assert len(agent.invocations) == 0, "no model call before the lineage resolves"


async def test_a_root_cycle_fails_closed(harness: InvestigationHarness) -> None:
    """Case A3. ``collapse_evidence_root`` rejects the cycle; nothing is counted from it."""

    await harness.seed(
        facts=(
            SeededFact(
                label="loc:a",
                reporter="resident-a",
                fact_type=FactType.LOCATION_AREA,
                value=CAB,
                evidence_labels=("original",),
            ),
        ),
        roots=(
            SeededRoot(label="one", parent_label="two"),
            SeededRoot(label="two", parent_label="one"),
        ),
        evidence=(SeededEvidence(label="original", reporter="resident-a", root_label="one"),),
    )
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    with pytest.raises(ValueError, match="cycle"):
        await harness.run_investigation(agent).execute(harness.command())


# -- B / B2: duplicate reporter, duplicate claims ---------------------------------------------


async def test_one_contributor_with_many_reports_is_never_ready(
    harness: InvestigationHarness,
) -> None:
    """Case Z, and evaluation scenario 7."""

    await harness.seed(
        reporters=("resident-a",),
        facts=(
            location_fact("loc:1", "resident-a", LocationAreaCode.ELEVATOR_CAB),
            location_fact("loc:2", "resident-a", LocationAreaCode.ELEVATOR_CAB),
            location_fact("loc:3", "resident-a", LocationAreaCode.ELEVATOR_CAB),
        ),
    )
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    result = await harness.run_investigation(agent).execute(harness.command())

    assert result.independent_source_count == 1
    assert result.state_reason_code == "CORROBORATION_MIN_NOT_MET"
    assert (await harness.case()).state is CaseState.INVESTIGATING
    # And no fact of that reporter's is corroborated on the strength of their own repetition.
    assert await harness.status_of("loc:1") is EvidenceStatus.REPORTED


# -- D / D2 / D3: contradictions at each materiality --------------------------------------------


@pytest.mark.parametrize(
    ("materiality", "expected_state"),
    [
        (ContradictionMateriality.LOW, CaseState.READY_FOR_ACTION),
        (ContradictionMateriality.MEDIUM, CaseState.INVESTIGATING),
        (ContradictionMateriality.HIGH, CaseState.INVESTIGATING),
    ],
)
async def test_contradiction_materiality_decides_readiness_and_never_the_status(
    harness: InvestigationHarness,
    materiality: ContradictionMateriality,
    expected_state: CaseState,
) -> None:
    """Cases D, D2, D3. Cited facts are ``CONTRADICTED`` at every materiality."""

    await harness.seed()
    cited = (
        harness.fact_id("incident:resident-a").value,
        harness.fact_id("incident:resident-b").value,
    )
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(contradictions=(contradiction_over(cited, materiality=materiality),))
    )

    result = await harness.run_investigation(agent).execute(harness.command())

    assert await harness.status_of("incident:resident-a") is EvidenceStatus.CONTRADICTED
    assert await harness.status_of("incident:resident-b") is EvidenceStatus.CONTRADICTED
    assert (await harness.case()).state is expected_state
    assert result.contradiction_count == 1


async def test_a_contradiction_overrides_a_weaker_proposed_status_on_the_same_fact(
    harness: InvestigationHarness,
) -> None:
    """The two contradiction-authority rows of the matrix, in one apply."""

    await harness.seed()
    a = harness.fact_id("incident:resident-a").value
    b = harness.fact_id("incident:resident-b").value
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(
            proposed={a: EvidenceStatus.REPORTED, b: EvidenceStatus.UNKNOWN},
            contradictions=(contradiction_over((a, b), materiality=ContradictionMateriality.LOW),),
        )
    )

    await harness.run_investigation(agent).execute(harness.command())

    assert await harness.status_of("incident:resident-a") is EvidenceStatus.CONTRADICTED
    assert await harness.status_of("incident:resident-b") is EvidenceStatus.CONTRADICTED


async def test_proposed_contradicted_alone_changes_nothing(
    harness: InvestigationHarness,
) -> None:
    """It names no cited facts, so there is nothing for a validator to check."""

    await harness.seed()
    a = harness.fact_id("incident:resident-a").value
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(proposed={a: EvidenceStatus.CONTRADICTED})
    )

    result = await harness.run_investigation(agent).execute(harness.command())

    assert await harness.status_of("incident:resident-a") is EvidenceStatus.REPORTED
    assert result.contradiction_count == 0
    assert (await harness.case()).state is CaseState.READY_FOR_ACTION


async def test_a_fabricated_high_contradiction_blocks_availability_and_grants_nothing(
    harness: InvestigationHarness,
) -> None:
    """Case AJ. The direction of effect is the invariant: it costs availability, never safety."""

    await harness.seed()
    cited = (
        harness.fact_id("location:resident-a").value,
        harness.fact_id("location:resident-b").value,
    )
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(
            contradictions=(contradiction_over(cited, materiality=ContradictionMateriality.HIGH),),
            disposition=RecommendedCaseDisposition.READY_FOR_ACTION,
        )
    )

    result = await harness.run_investigation(agent).execute(harness.command())

    case = await harness.case()
    assert case.state is CaseState.INVESTIGATING
    assert case.current_view_id is None
    assert case.current_action_id is None
    # No status was raised, no scope widened, no identity granted: the only movement is downward.
    assert await harness.status_of("location:resident-a") is EvidenceStatus.CONTRADICTED
    assert result.state_reason_code == "CONTRADICTION_UNRESOLVED"


# -- H group: the ladder, end to end ------------------------------------------------------------


async def test_model_verified_is_downgraded_and_audited(
    harness: InvestigationHarness,
) -> None:
    """Cases H and H4. The assessment persists in full; only the one field loses."""

    await harness.seed()
    a = harness.fact_id("location:resident-a").value
    agent = ScriptedInvestigatorAgent(responder=cooperative(proposed={a: EvidenceStatus.VERIFIED}))

    result = await harness.run_investigation(agent).execute(harness.command())

    assert await harness.status_of("location:resident-a") is EvidenceStatus.CORROBORATED
    assert result.downgraded_count == 1
    page = await harness.audit.read_case_events(harness.case_scope, PageRequest(limit=10))
    codes = {code for event in page.items for code in event.reason_codes}
    assert "EVIDENCE_STATUS_OVERCLAIM_DOWNGRADED" in codes


async def test_no_fact_ever_reaches_verified(harness: InvestigationHarness) -> None:
    """Case H5. Every proposal is ``VERIFIED`` and the count of verified facts stays zero."""

    await harness.seed()
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(
            proposed={
                harness.fact_id(label).value: EvidenceStatus.VERIFIED
                for label in (
                    "location:resident-a",
                    "location:resident-b",
                    "incident:resident-a",
                    "incident:resident-b",
                )
            }
        )
    )

    await harness.run_investigation(agent).execute(harness.command())

    facts = await harness.facts()
    assert all(fact.evidence_status is not EvidenceStatus.VERIFIED for fact in facts)


async def test_the_model_may_lower_a_corroborated_fact_to_unknown(
    harness: InvestigationHarness,
) -> None:
    """Case H6. Skepticism is the one status influence the model is granted."""

    await harness.seed()
    a = harness.fact_id("location:resident-a").value
    agent = ScriptedInvestigatorAgent(responder=cooperative(proposed={a: EvidenceStatus.UNKNOWN}))

    await harness.run_investigation(agent).execute(harness.command())

    assert await harness.status_of("location:resident-a") is EvidenceStatus.UNKNOWN
    assert await harness.status_of("location:resident-b") is EvidenceStatus.CORROBORATED


async def test_restating_a_stale_current_status_is_honoured_as_a_downgrade(
    harness: InvestigationHarness,
) -> None:
    """A model echoing the stored status suppresses corroboration it has just earned.

    That is the ladder working, not failing: the answer said ``REPORTED`` and ``REPORTED`` is
    weaker than the recomputation. It is asserted so the behaviour is a decision on the record
    rather than a surprise the first time an agent parrots its input.
    """

    await harness.seed()
    agent = ScriptedInvestigatorAgent(responder=cooperative(restate_current_status=True))

    await harness.run_investigation(agent).execute(harness.command())

    assert await harness.status_of("location:resident-a") is EvidenceStatus.REPORTED


# -- I / J: the model's own numbers are ignored ---------------------------------------------------


async def test_the_recomputed_source_count_replaces_the_model_number(
    harness: InvestigationHarness,
) -> None:
    """Cases I and J."""

    await harness.seed(reporters=("resident-a",))
    agent = ScriptedInvestigatorAgent(responder=cooperative(model_source_count=99))

    result = await harness.run_investigation(agent).execute(harness.command())

    assert result.independent_source_count == 1
    assert (await harness.case()).corroboration_source_count == 1


async def test_a_model_duplicate_group_changes_no_count(
    harness: InvestigationHarness,
) -> None:
    """A belief about which copies share an origin cannot add or remove a source."""

    await harness.seed(
        roots=(SeededRoot(label="one"), SeededRoot(label="two")),
        evidence=(
            SeededEvidence(label="one", reporter="resident-a", root_label="one"),
            SeededEvidence(label="two", reporter="resident-b", root_label="two"),
        ),
        facts=(
            SeededFact(
                label="loc:a",
                reporter="resident-a",
                fact_type=FactType.LOCATION_AREA,
                value=CAB,
                evidence_labels=("one",),
            ),
            SeededFact(
                label="loc:b",
                reporter="resident-b",
                fact_type=FactType.LOCATION_AREA,
                value=CAB,
                evidence_labels=("two",),
            ),
        ),
    )

    def answer(invocation):  # type: ignore[no-untyped-def]
        base = cooperative()(invocation)
        return base.model_copy(
            update={
                "duplicate_evidence_groups": (
                    DuplicateEvidenceGroup(
                        root_id=invocation.payload.evidence[0].root_id,
                        evidence_ids=tuple(
                            item.evidence_id for item in invocation.payload.evidence
                        ),
                        reason="These are obviously the same photograph.",
                    ),
                )
            }
        )

    result = await harness.run_investigation(ScriptedInvestigatorAgent(responder=answer)).execute(
        harness.command()
    )

    # The roots are genuinely distinct, so the two remain two however the model groups them.
    assert result.independent_source_count == 2


# -- C / F / G: invented and foreign identifiers ---------------------------------------------------


async def test_an_invented_fact_identifier_rejects_the_whole_answer(
    harness: InvestigationHarness,
) -> None:
    await harness.seed()
    agent = ScriptedInvestigatorAgent(responder=with_invented_finding())

    with pytest.raises(AgentContractViolationError):
        await harness.run_investigation(agent).execute(harness.command())

    case = await harness.case()
    assert case.version == 1
    assert case.assessment_id is None


async def test_a_foreign_citation_rejects_the_whole_answer(
    harness: InvestigationHarness,
) -> None:
    await harness.seed()
    agent = ScriptedInvestigatorAgent(responder=with_foreign_fact_citation())

    with pytest.raises(AgentContractViolationError):
        await harness.run_investigation(agent).execute(harness.command())

    assert (await harness.case()).version == 1


# -- K / L: concurrency and timeout ----------------------------------------------------------------


async def test_a_case_already_moved_before_invocation_applies_nothing(
    harness: InvestigationHarness,
) -> None:
    """Case K, first half. The request-time check refuses before a model is ever asked.

    The case moves *before* the command runs, so the staleness is visible to the strong read at
    step 1 and the refusal costs no invocation. This is the cheap check, not the guarantee: it
    says nothing about a case that moves after the read, which is the other half.
    """

    await harness.seed()
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    moved_to = await harness.bump_case_version()
    assert moved_to == 2

    with pytest.raises(InvestigationApplyDeniedError) as caught:
        await harness.run_investigation(agent).execute(harness.command(expected_case_version=1))
    assert caught.value.denial is InvestigationApplyDenial.STALE_CASE_VERSION
    assert agent.invocations == [], "the model is never asked about a case that has already moved"

    case = await harness.case()
    assert case.version == moved_to
    assert case.state is CaseState.INVESTIGATING
    assert case.assessment_id is None
    assert case.corroboration_source_count == 0
    assert (
        await harness.core.read_case_assessments(harness.case_scope, PageRequest(limit=10))
    ).items == ()
    for fact in await harness.facts():
        assert fact.evidence_status is EvidenceStatus.REPORTED
        assert fact.version == 1


async def test_a_case_version_change_after_invocation_starts_applies_nothing(
    harness: InvestigationHarness,
) -> None:
    """Case K, second half, named test 25, and the only test reaching the third check.

    The model is asked about version 1 and answers about version 1. The case becomes version 2
    while that answer is in flight, so both earlier checks -- the request-time expectation and
    the envelope's case version, which the answer restates correctly -- pass. Only the apply
    transaction's condition on the case row can refuse this, and refusing it is the whole
    reason the condition is there: a reading of an older world must not attach to the current
    one.
    """

    await harness.seed()
    inner = ScriptedInvestigatorAgent(responder=cooperative())
    agent = BumpsTheCaseMidFlight(inner=inner, harness=harness)

    with pytest.raises(PersistenceConflictError):
        await harness.run_investigation(agent).execute(harness.command(expected_case_version=1))

    # The model was asked, and it was asked about version 1. Anything else would mean this
    # test had reproduced the cheap request-time refusal again rather than the race.
    assert len(inner.invocations) == 1, "no automatic second model call follows an apply conflict"
    assert inner.invocations[0].case_version == 1
    assert inner.invocations[0].payload.case.version == 1
    assert agent.moved_to == 2

    # The answer is not applied as current truth: no assessment row, no pointer, no recomputed
    # source count, no readiness transition.
    case = await harness.case()
    assert case.version == 2, "the case is left at the state the independent writer moved it to"
    assert case.state is CaseState.INVESTIGATING
    assert case.assessment_id is None
    assert case.corroboration_source_count == 0
    assert (
        await harness.core.read_case_assessments(harness.case_scope, PageRequest(limit=10))
    ).items == ()

    # No fact carries a status the discarded answer implied, and none was rewritten at all.
    for fact in await harness.facts():
        assert fact.evidence_status is EvidenceStatus.REPORTED
        assert fact.version == 1

    # The operation settles on the frozen safe-persistence behaviour: the failure is durable,
    # so a redelivery re-reads it rather than re-asking the model.
    record = await harness.core.load_agent_invocation(harness.case_scope, harness.invocation_id())
    assert record is not None
    assert record.failure_code == "PERSISTENCE_CONFLICT"


async def test_a_timeout_leaves_no_partial_state_and_records_the_failure(
    harness: InvestigationHarness,
) -> None:
    """Case L. The failure is durable so a redelivery does not re-ask the same question."""

    await harness.seed()
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(), failures=[AgentTimeoutError(), AgentTimeoutError()]
    )

    with pytest.raises(AgentTimeoutError):
        await harness.run_investigation(agent).execute(harness.command())

    case = await harness.case()
    assert case.version == 1
    assert case.assessment_id is None
    record = await harness.core.load_agent_invocation(harness.case_scope, harness.invocation_id())
    assert record is not None
    assert record.failure_code == "AGENT_TIMEOUT"


async def test_exactly_one_licensed_retry_happens_for_a_transient_failure(
    harness: InvestigationHarness,
) -> None:
    await harness.seed()
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(), failures=[AgentDependencyError(retryable=True)]
    )

    await harness.run_investigation(agent).execute(harness.command())

    assert len(agent.invocations) == 2


# -- M / M2 / N: replay and binding ----------------------------------------------------------------


async def test_a_completed_invocation_replays_without_calling_the_model(
    harness: InvestigationHarness,
) -> None:
    """Case M.

    The redelivery carries the *same* command the first delivery did -- a job's
    ``expected_case_version`` never changes -- so it arrives naming version 1 at a case that the
    apply has already moved to version 2. That is precisely the shape the record exists to
    answer, and refusing it as stale would refuse the redelivery for having succeeded.
    """

    await harness.seed()
    agent = ScriptedInvestigatorAgent(responder=cooperative())
    first = await harness.run_investigation(agent).execute(harness.command())
    assert (await harness.case()).version == 2

    replayed = await harness.run_investigation(agent).execute(harness.command())

    assert replayed.replayed is True
    assert replayed.assessment_id == first.assessment_id
    assert len(agent.invocations) == 1


async def test_the_same_invocation_identity_under_a_different_question_is_a_conflict(
    harness: InvestigationHarness,
) -> None:
    """The other half of case M: a record is replayed only for the question that produced it."""

    await harness.seed()
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(), failures=[AgentTimeoutError(), AgentTimeoutError()]
    )
    with pytest.raises(AgentTimeoutError):
        await harness.run_investigation(agent).execute(harness.command())

    # Withdraw a fact so the case's projected payload genuinely differs, while leaving the
    # case at the version the redelivered command still names.
    facts = await harness.facts()
    target = next(fact for fact in facts if fact.fact_id == harness.fact_id("location:resident-b"))
    await harness.unit_of_work.commit(
        TransactionPlan(
            name="withdraw-a-fact",
            operations=(
                harness.core.stage_update_fact(
                    harness.case_scope,
                    replace(
                        target,
                        status=FactStatus.WITHDRAWN,
                        version=target.version + 1,
                        updated_at=target.updated_at + timedelta(seconds=1),
                    ),
                    expected_version=target.version,
                ),
            ),
            audit_required=False,
        )
    )

    fresh = ScriptedInvestigatorAgent(responder=cooperative())
    with pytest.raises(AgentContractViolationError):
        await harness.run_investigation(fresh).execute(harness.command())
    assert len(fresh.invocations) == 0


async def test_a_recorded_failure_replays_as_a_failure(
    harness: InvestigationHarness,
) -> None:
    """Case M2. 'This invocation is over' survives the failure that made it so."""

    await harness.seed()
    failing = ScriptedInvestigatorAgent(
        responder=cooperative(), failures=[AgentTimeoutError(), AgentTimeoutError()]
    )
    with pytest.raises(AgentTimeoutError):
        await harness.run_investigation(failing).execute(harness.command())

    fresh = ScriptedInvestigatorAgent(responder=cooperative())
    with pytest.raises(AgentError) as caught:
        await harness.run_investigation(fresh).execute(harness.command())

    assert len(fresh.invocations) == 0
    assert caught.value.code is AgentErrorCode.AGENT_TIMEOUT
    # The replayed failure is deliberately *not* retryable. The original timeout was, and
    # honouring that a second time would re-ask a question already recorded as answered.
    assert caught.value.retryable is False


# -- V: the derived transaction bound --------------------------------------------------------------


async def test_an_oversized_validated_output_is_refused_before_any_mutation(
    harness: InvestigationHarness,
) -> None:
    """Case V. The bound is derived from the real staged transaction, never guessed."""

    facts = tuple(
        SeededFact(
            label=f"loc:{index}",
            reporter="resident-a" if index % 2 == 0 else "resident-b",
            fact_type=FactType.INCIDENT_OCCURRENCE,
            value=IncidentOccurrence(
                occurred_at=NOW - timedelta(minutes=index + 1),
                failure_mode=FailureMode.STUCK,
            ),
        )
        for index in range(MAX_INVESTIGATION_FACT_UPDATES + 1)
    )
    await harness.seed(facts=facts, approved_facts=())
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(
            proposed={harness.fact_id(fact.label).value: EvidenceStatus.UNKNOWN for fact in facts}
        )
    )

    with pytest.raises(InvestigationApplyDeniedError) as caught:
        await harness.run_investigation(agent).execute(harness.command())
    assert caught.value.denial is InvestigationApplyDenial.TRANSACTION_BOUND_EXCEEDED

    case = await harness.case()
    assert case.version == 1
    assert case.assessment_id is None


# -- W: the live send fence ------------------------------------------------------------------------


async def test_a_live_send_fence_blocks_the_investigation_apply(
    harness: InvestigationHarness,
) -> None:
    """Case W. An authorized send must not have the evidence move underneath it."""

    await harness.seed()
    now = harness.clock.now()
    await harness.core.acquire_send_fence(
        harness.case_scope,
        SendFence(
            namespace=harness.namespace,
            community_id=harness.community_id,
            case_id=harness.case_id,
            execution_id=ExecutionId(uuid4()),
            action_id=ActionId(uuid4()),
            approval_id=ApprovalId(uuid4()),
            view_id=ViewId(uuid4()),
            authorization_snapshot_hash=Sha256Digest("sha256:" + "ff" * 32),
            acquired_at=now,
            expires_at=now + timedelta(seconds=60),
        ),
    )
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    with pytest.raises(PersistenceConflictError):
        await harness.run_investigation(agent).execute(harness.command())

    case = await harness.case()
    assert case.version == 1
    assert case.assessment_id is None


# -- AA / AB: what the apply does not do -----------------------------------------------------------


async def test_the_compile_preflight_persists_nothing(
    harness: InvestigationHarness,
) -> None:
    """Case AA, and named test 24."""

    await harness.seed()
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    await harness.run_investigation(agent).execute(harness.command())

    case = await harness.case()
    assert case.state is CaseState.READY_FOR_ACTION
    assert case.current_view_id is None
    pointer = await harness.core.load_send_fence(harness.case_scope)
    assert pointer is None
    # No compile audit row: the only audit event this apply writes is its own.
    page = await harness.audit.read_case_events(harness.case_scope, PageRequest(limit=25))
    assert {event.event_type for event in page.items} == {"investigation.applied"}


async def test_phase_five_creates_no_fact(harness: InvestigationHarness) -> None:
    """Case AB. Contradictions live in the assessment; no ``CONTRADICTION`` fact is produced."""

    await harness.seed()
    before = await harness.case()
    cited = (
        harness.fact_id("incident:resident-a").value,
        harness.fact_id("incident:resident-b").value,
    )
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(
            contradictions=(contradiction_over(cited, materiality=ContradictionMateriality.LOW),)
        )
    )

    await harness.run_investigation(agent).execute(harness.command())

    after = await harness.case()
    assert set(after.fact_ids) == set(before.fact_ids)
    facts = await harness.facts()
    assert all(fact.fact_type is not FactType.CONTRADICTION for fact in facts)


async def test_a_proposed_commitment_is_validated_and_then_discarded(
    harness: InvestigationHarness,
) -> None:
    """Case AI. Valid shape, valid citation, and nothing persisted."""

    await harness.seed(
        roots=(SeededRoot(label="reply"),),
        evidence=(SeededEvidence(label="reply", reporter="resident-a", root_label="reply"),),
        facts=(
            SeededFact(
                label="loc:a",
                reporter="resident-a",
                fact_type=FactType.LOCATION_AREA,
                value=CAB,
                evidence_labels=("reply",),
            ),
            SeededFact(
                label="loc:b",
                reporter="resident-b",
                fact_type=FactType.LOCATION_AREA,
                value=CAB,
            ),
        ),
    )

    def answer(invocation):  # type: ignore[no-untyped-def]
        base = cooperative()(invocation)
        return base.model_copy(
            update={
                "proposed_commitments": (
                    ProposedCommitment(
                        source_evidence_id=invocation.payload.evidence[0].evidence_id,
                        obligor="Property Management",
                        action_text="Repair the lift by Friday.",
                        due_at=NOW + timedelta(days=5),
                        verification_method="A resident confirms the lift runs.",
                    ),
                )
            }
        )

    await harness.run_investigation(ScriptedInvestigatorAgent(responder=answer)).execute(
        harness.command()
    )

    case = await harness.case()
    assert case.state is not CaseState.VERIFYING
    assert case.current_action_id is None
    facts = await harness.facts()
    assert all(fact.fact_type is not FactType.COMMITMENT_TERM for fact in facts)


# -- AG: ready while individual facts remain REPORTED ----------------------------------------------


async def test_a_case_may_be_ready_while_facts_remain_reported(
    harness: InvestigationHarness,
) -> None:
    """Case AG. Case corroboration carries the authority; the fact label travels outward."""

    await harness.seed()
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    await harness.run_investigation(agent).execute(harness.command())

    assert (await harness.case()).state is CaseState.READY_FOR_ACTION
    assert await harness.status_of("incident:resident-a") is EvidenceStatus.REPORTED


# -- O: the model's disposition is never a term ----------------------------------------------------


async def test_readiness_ignores_recommended_disposition(
    harness: InvestigationHarness,
) -> None:
    """Named test 23, in both directions."""

    await harness.seed()
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(disposition=RecommendedCaseDisposition.CONTINUE_INVESTIGATION)
    )
    await harness.run_investigation(agent).execute(harness.command())
    assert (await harness.case()).state is CaseState.READY_FOR_ACTION


async def test_a_ready_recommendation_cannot_make_an_uncorroborated_case_ready(
    harness: InvestigationHarness,
) -> None:
    await harness.seed(reporters=("resident-a",))
    agent = ScriptedInvestigatorAgent(
        responder=cooperative(disposition=RecommendedCaseDisposition.READY_FOR_ACTION)
    )
    await harness.run_investigation(agent).execute(harness.command())
    assert (await harness.case()).state is CaseState.INVESTIGATING


# -- P: a different-issue finding blocks -----------------------------------------------------------


@pytest.mark.parametrize("linkage", [LinkageDecision.DIFFERENT_ISSUES, LinkageDecision.UNCERTAIN])
async def test_a_non_same_issue_linkage_blocks_readiness(
    harness: InvestigationHarness, linkage: LinkageDecision
) -> None:
    await harness.seed()
    agent = ScriptedInvestigatorAgent(responder=cooperative(linkage=linkage))

    result = await harness.run_investigation(agent).execute(harness.command())

    assert (await harness.case()).state is CaseState.INVESTIGATING
    assert result.state_reason_code == "DIFFERENT_ISSUE_UNRESOLVED"


# -- readiness lost --------------------------------------------------------------------------------


async def test_a_ready_case_returns_to_investigating_when_readiness_is_lost(
    harness: InvestigationHarness,
) -> None:
    await harness.seed(state=CaseState.READY_FOR_ACTION)
    agent = ScriptedInvestigatorAgent(responder=cooperative(linkage=LinkageDecision.UNCERTAIN))

    await harness.run_investigation(agent).execute(harness.command())

    assert (await harness.case()).state is CaseState.INVESTIGATING


def test_the_scope_helper_is_the_case_scope(harness: InvestigationHarness) -> None:
    assert isinstance(harness.case_scope, CaseScope)
