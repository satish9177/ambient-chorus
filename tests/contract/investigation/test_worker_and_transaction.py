"""The investigation worker's binding, and the arithmetic behind its transaction bound.

Two things that must be facts rather than intentions: a job is bound to the operation it names
before anything is claimed (ADR-016), and ``MAX_INVESTIGATION_FACT_UPDATES`` is derived from the
plan the implementation actually stages rather than from a number somebody once counted.
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
    SeededFact,
)
from tests.fixtures.investigation_answers import cooperative

from chorus.application.commands.run_investigation import (
    INVESTIGATION_APPLY_TRANSACTION,
    INVESTIGATION_FIXED_TRANSACTION_PARTICIPANTS,
    MAX_INVESTIGATION_FACT_UPDATES,
)
from chorus.application.commands.run_investigation_operation import (
    InvestigationJobBinding,
)
from chorus.application.operations import investigate_binding_hash
from chorus.domain.entities import (
    ApplicationOperationKind,
    ApplicationOperationStatus,
    EvidenceStatus,
    FactType,
)
from chorus.domain.facts import FailureMode, IncidentOccurrence
from chorus.domain.ids import Sha256Digest
from chorus.infrastructure.local.investigator_agent import ScriptedInvestigatorAgent
from chorus.ports.limits import TRANSACTION_MAX_OPERATIONS
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork

pytestmark = pytest.mark.anyio


# -- the derived transaction bound -------------------------------------------------------------


class RecordingUnitOfWork:
    """A unit of work that records the plan it was handed and then commits it for real."""

    def __init__(self, inner: UnitOfWork) -> None:
        self._inner = inner
        self.plans: list[TransactionPlan] = []

    async def commit(self, plan: TransactionPlan) -> None:
        self.plans.append(plan)
        await self._inner.commit(plan)

    async def resolve_outcome(self, plan: TransactionPlan):  # type: ignore[no-untyped-def]
        return await self._inner.resolve_outcome(plan)


async def test_the_derived_bound_matches_the_real_staged_transaction(
    harness: InvestigationHarness,
) -> None:
    """Case V's arithmetic, asserted against the plan rather than against a comment.

    The largest legal apply is built and its operation count is compared with DynamoDB's
    maximum. If a seventh fixed participant is ever added, this fails before anything reaches
    storage -- which is the whole reason the bound is derived rather than written down.
    """

    facts = tuple(
        SeededFact(
            label=f"incident:{index}",
            reporter="resident-a" if index % 2 == 0 else "resident-b",
            fact_type=FactType.INCIDENT_OCCURRENCE,
            value=IncidentOccurrence(
                occurred_at=NOW - timedelta(minutes=index + 1),
                failure_mode=FailureMode.STUCK,
            ),
        )
        for index in range(MAX_INVESTIGATION_FACT_UPDATES)
    )
    await harness.seed(facts=facts, approved_facts=())
    recorder = RecordingUnitOfWork(harness.unit_of_work)
    run = replace(
        harness.run_investigation(
            ScriptedInvestigatorAgent(
                responder=cooperative(
                    proposed={
                        harness.fact_id(fact.label).value: EvidenceStatus.UNKNOWN for fact in facts
                    }
                )
            )
        ),
        unit_of_work=recorder,
    )

    result = await run.execute(harness.command())

    assert result.fact_updates == MAX_INVESTIGATION_FACT_UPDATES
    apply_plan = next(
        plan for plan in recorder.plans if plan.name == INVESTIGATION_APPLY_TRANSACTION
    )
    assert len(apply_plan.operations) == TRANSACTION_MAX_OPERATIONS
    assert (
        len(apply_plan.operations) - result.fact_updates
        == INVESTIGATION_FIXED_TRANSACTION_PARTICIPANTS
    )


# -- the ADR-016 binding ---------------------------------------------------------------------------


async def test_a_bound_job_claims_and_settles_its_operation(
    harness: InvestigationHarness,
) -> None:
    await harness.seed()
    operation = await harness.bound_operation()
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    settled = await harness.worker(agent).execute(harness.job_for(operation))

    assert settled.status is ApplicationOperationStatus.SUCCEEDED


@pytest.mark.parametrize(
    "mutation",
    [
        "invocation",
        "actor",
        "request",
        "case",
        "version",
        "reason",
    ],
)
async def test_a_job_that_disagrees_with_its_operation_claims_nothing(
    harness: InvestigationHarness, mutation: str
) -> None:
    """Every field of the handover, one mismatch at a time.

    A mismatch claims nothing, invokes nothing, and mutates nothing: the operation's status and
    version are untouched and no model call happens.
    """

    await harness.seed()
    operation = await harness.bound_operation()
    job = harness.job_for(operation)
    if mutation == "invocation":
        job = replace(job, invocation_id=uuid4())
    elif mutation == "actor":
        job = replace(job, actor_id_hash=Sha256Digest("sha256:" + "11" * 32))
    elif mutation == "request":
        job = replace(job, request_hash=Sha256Digest("sha256:" + "22" * 32))
    elif mutation == "case":
        job = replace(job, case_id=harness.case_id.__class__(uuid4()))
    elif mutation == "version":
        job = replace(job, expected_case_version=7)
    else:
        job = replace(job, reason="REOPEN")

    agent = ScriptedInvestigatorAgent(responder=cooperative())
    settled = await harness.worker(agent).execute(job)

    assert settled.status is ApplicationOperationStatus.PENDING
    assert settled.version == operation.version
    assert len(agent.invocations) == 0
    case = await harness.case()
    assert case.version == 1


async def test_a_monitor_operation_is_refused_by_the_investigation_worker(
    harness: InvestigationHarness,
) -> None:
    """A misrouted delivery must not record a MONITOR command as having failed here."""

    await harness.seed()
    operation = await harness.bound_operation(kind=ApplicationOperationKind.MONITOR)
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    settled = await harness.worker(agent).execute(harness.job_for(operation))

    assert settled.status is ApplicationOperationStatus.PENDING
    assert len(agent.invocations) == 0


async def test_an_unbound_operation_is_refused_rather_than_trusted(
    harness: InvestigationHarness,
) -> None:
    """The gap ADR-016 closed: a first delivery with no durable record to disagree with."""

    await harness.seed()
    binding = investigate_binding_hash(
        case_id=harness.case_id, expected_case_version=1, reason="INITIAL"
    )
    now = harness.clock.now()
    from chorus.domain.entities import ApplicationOperation

    unbound = ApplicationOperation(
        operation_id=harness.operation_id("unbound"),
        kind=ApplicationOperationKind.INVESTIGATE,
        namespace=harness.namespace,
        actor_id_hash=harness.job_for(await harness.bound_operation(label="ignored")).actor_id_hash,
        case_id=harness.case_id,
        request_hash=binding,
        status=ApplicationOperationStatus.PENDING,
        result_refs=(),
        error_code=None,
        expires_at_epoch=int((now + timedelta(days=7)).timestamp()),
        version=1,
        created_at=now,
        updated_at=now,
        agent_invocation_id=None,
        agent_binding_hash=None,
    )
    await harness.unit_of_work.commit(
        TransactionPlan(
            name="seed-unbound-operation",
            operations=(
                harness.core.stage_create_operation(
                    harness.community_scope.namespace_scope, unbound
                ),
            ),
            audit_required=False,
        )
    )
    agent = ScriptedInvestigatorAgent(responder=cooperative())

    settled = await harness.worker(agent).execute(harness.job_for(unbound, label="unbound"))

    assert settled.status is ApplicationOperationStatus.PENDING
    assert len(agent.invocations) == 0


def test_the_binding_names_the_work_and_not_merely_the_request() -> None:
    """An initial run and a later reopen of one case at one version are different work."""

    from chorus.domain.ids import CaseId

    case_id = CaseId(uuid4())
    initial = investigate_binding_hash(case_id=case_id, expected_case_version=3, reason="INITIAL")
    reopened = investigate_binding_hash(case_id=case_id, expected_case_version=3, reason="REOPEN")
    later = investigate_binding_hash(case_id=case_id, expected_case_version=4, reason="INITIAL")
    assert len({initial, reopened, later}) == 3


def test_every_binding_failure_has_a_named_reason_code() -> None:
    """The worker reports the whole disagreement, so each cause needs its own code."""

    codes = {
        InvestigationJobBinding.KIND,
        InvestigationJobBinding.NAMESPACE,
        InvestigationJobBinding.CASE,
        InvestigationJobBinding.ACTOR,
        InvestigationJobBinding.REQUEST,
        InvestigationJobBinding.INVOCATION,
        InvestigationJobBinding.BINDING,
        InvestigationJobBinding.UNBOUND,
    }
    assert len(codes) == 8


# -- unknown outcome -------------------------------------------------------------------------------


async def test_a_lost_success_response_is_transcribed_rather_than_re_run(
    harness: InvestigationHarness,
) -> None:
    """Case N. The durable invocation record makes the status write a transcription."""

    await harness.seed()
    operation = await harness.bound_operation()
    agent = ScriptedInvestigatorAgent(responder=cooperative())
    await harness.worker(agent).execute(harness.job_for(operation))

    # A redelivery of the same job finds a terminal operation and does nothing at all.
    reloaded = await harness.operations.load(
        namespace=harness.namespace, operation_id=operation.operation_id
    )
    settled = await harness.worker(agent).execute(harness.job_for(operation))

    assert settled.status is ApplicationOperationStatus.SUCCEEDED
    assert settled.version == reloaded.version
    assert len(agent.invocations) == 1


async def test_a_case_moved_after_invocation_settles_the_operation_as_a_conflict(
    harness: InvestigationHarness,
) -> None:
    """The worker half of the mid-flight race: the operation settles, it does not strand.

    The command-level test proves nothing is applied. This proves the *operation* reaches a
    terminal status carrying the safe persistence code, because a race that raised out of the
    worker into an at-least-once dispatcher would leave the operation in ``RUNNING`` forever --
    which is the failure the settle path exists to prevent, and it is only reachable when the
    apply conflicts rather than when the request was stale to begin with.
    """

    await harness.seed()
    operation = await harness.bound_operation()
    inner = ScriptedInvestigatorAgent(responder=cooperative())
    agent = BumpsTheCaseMidFlight(inner=inner, harness=harness)

    settled = await harness.worker(agent).execute(harness.job_for(operation))

    assert settled.status is ApplicationOperationStatus.FAILED
    assert settled.error_code == "PERSISTENCE_CONFLICT"
    assert settled.result_refs == ()
    assert len(inner.invocations) == 1
    assert agent.moved_to == 2

    case = await harness.case()
    assert case.version == agent.moved_to
    assert case.assessment_id is None
