"""F09 -- a durable ``SUCCEEDED`` record is proof only if it is *this* operation's.

Codex found ``_finish_if_recorded`` and the in-process replay path accepting any loaded record
whose ``outcome`` was ``SUCCEEDED``. That is not provenance. It says an Action invocation
somewhere succeeded; it says nothing about which view, which prompt artifact, which case, or
which proposal -- so a record that reached that key by any route could finish an operation it
has nothing to do with, and the identifiers returned to the caller would be read out of it.

The repair verifies, on **both** paths, through one shared helper:

* the invocation identity;
* the namespace / community / case scope;
* ``agent == ACTION``;
* ``prompt_version == ACTION_PROMPT_VERSION``;
* the expected ``input_hash``, recomputed from the immutable bound view through the same
  canonical schema the invocation-time hash used -- so the comparison is between two values
  derived from two sources, never a value compared with itself;
* an exact result-reference set: one ``ACTION_PROPOSAL`` and one ``ACTION_EXECUTION``, no
  foreign or malformed reference.

Every failure below is fail-closed: no ``RUNNING -> SUCCEEDED``, no identifiers returned, and no
model call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from chorus.application.commands.propose_action import (
    InvocationExpectation,
    _view_hash_verifies,
    expected_action_input_hash,
    invocation_provenance_failures,
    to_action_input,
)
from chorus.domain.entities import (
    ActionExecution,
    ActionProposal,
    ApplicationOperationStatus,
)
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import ActionId, CaseId, ExecutionId, Sha256Digest
from chorus.infrastructure.dynamodb import codec_fence, codec_share
from chorus.ports.idempotency import EntityRef
from chorus.ports.operations import ProposeActionOperationJob
from chorus.ports.records import AgentInvocationOutcome, AgentInvocationResult
from chorus.ports.records import AgentName as StoredAgentName
from chorus.ports.scopes import ActionScope, CaseScope
from chorus.ports.storage import KeyAbsent, KeyPresent, PutItem
from chorus.ports.unit_of_work import TransactionPlan
from chorus.privacy.canonical import hash_value
from tests.contract.action.test_races_and_recovery import _invalidate, _return_to_ready
from tests.fixtures.action import ActionHarness

pytestmark = pytest.mark.anyio


def _scope(harness: ActionHarness) -> CaseScope:
    return CaseScope(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
    )


def _action_scope(harness: ActionHarness, action_id: ActionId) -> ActionScope:
    return ActionScope(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
        action_id=action_id,
    )


async def _real_apply(
    harness: ActionHarness,
) -> tuple[ProposeActionOperationJob, ActionProposal, ActionExecution, AgentInvocationResult]:
    await harness.prepare()
    started = await harness.start_operation()
    job = await harness.job(started)
    settled = await harness.worker().execute(job)
    assert settled.status is ApplicationOperationStatus.SUCCEEDED
    assert len(harness.agent.invocations) == 1

    scope = _scope(harness)
    record = await harness.compile.core.load_agent_invocation(scope, harness.invocation_id)
    assert record is not None

    action_scope = ActionScope(
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=scope.case_id,
        action_id=ActionId(record.result_refs[0].entity_id),
    )
    proposal = await harness.compile.shareable.load_proposal(action_scope)
    execution = await harness.compile.shareable.load_execution(
        action_scope, ExecutionId(record.result_refs[1].entity_id)
    )
    return job, proposal, execution, record


async def _reset_to_running(harness: ActionHarness, job: ProposeActionOperationJob) -> None:
    operations = harness.operations()
    operation = await operations.load(
        namespace=harness.scope.namespace, operation_id=job.operation_id
    )
    running_op = replace(
        operation,
        status=ApplicationOperationStatus.RUNNING,
        result_refs=(),
        error_code=None,
        version=operation.version + 1,
    )
    await harness.compile.unit_of_work.commit(
        TransactionPlan(
            name="reset-operation-running",
            operations=(
                harness.compile.core.stage_update_operation(
                    _scope(harness).namespace_scope, running_op, expected_version=operation.version
                ),
            ),
            audit_required=False,
        )
    )


async def _overwrite_record(harness: ActionHarness, record: AgentInvocationResult) -> None:
    scope = _scope(harness)
    await harness.compile.unit_of_work.commit(
        TransactionPlan(
            name="overwrite-record",
            operations=(
                PutItem(
                    key=codec_fence.agent_invocation_key(scope, record.invocation_id),
                    item=codec_fence.encode_agent_invocation(scope, record),
                    condition=KeyPresent(),
                ),
            ),
            audit_required=False,
        )
    )


async def _overwrite_proposal(
    harness: ActionHarness, action_scope: ActionScope, proposal: object
) -> None:
    await harness.compile.unit_of_work.commit(
        TransactionPlan(
            name="overwrite-proposal",
            operations=(
                PutItem(
                    key=codec_share.proposal_key(action_scope),
                    item=codec_share.encode_proposal(action_scope, proposal),  # type: ignore[arg-type]
                    condition=KeyPresent(),
                ),
            ),
            audit_required=False,
        )
    )


async def _overwrite_execution(
    harness: ActionHarness, action_scope: ActionScope, execution: object, *, is_new: bool = False
) -> None:
    await harness.compile.unit_of_work.commit(
        TransactionPlan(
            name="overwrite-execution",
            operations=(
                PutItem(
                    key=codec_share.execution_key(action_scope, execution.execution_id),  # type: ignore[attr-defined]
                    item=codec_share.encode_execution(action_scope, execution),  # type: ignore[arg-type]
                    condition=KeyAbsent() if is_new else KeyPresent(),
                ),
            ),
            audit_required=False,
        )
    )


async def _honest_record(harness: ActionHarness) -> AgentInvocationResult:
    """The record a genuine successful apply would have written for this invocation."""

    payload = to_action_input(harness.view)
    return AgentInvocationResult(
        invocation_id=harness.invocation_id,
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
        operation_id=None,
        agent_name=StoredAgentName.ACTION,
        prompt_version="action/v1",
        input_hash=hash_value(payload.model_dump(mode="json")),
        output_hash=hash_value({"stub": "output"}),
        outcome=AgentInvocationOutcome.SUCCEEDED,
        result_refs=(
            EntityRef(entity_type="ACTION_PROPOSAL", entity_id=uuid4()),
            EntityRef(entity_type="ACTION_EXECUTION", entity_id=uuid4()),
        ),
        created_at=harness.compile.clock.now(),
    )


async def _plant(harness: ActionHarness, record: AgentInvocationResult) -> None:
    """Write one invocation record directly, bypassing the apply that would have written it."""

    await harness.compile.unit_of_work.commit(
        TransactionPlan(
            name="repair-plant-record",
            operations=(
                harness.compile.core.stage_append_agent_invocation(_scope(harness), record),
            ),
            audit_required=False,
        )
    )


async def _running_job(harness: ActionHarness) -> object:
    """A claimed operation and its job, so ``_handle_running`` is the path under test."""

    started = await harness.start_operation()
    job = await harness.job(started)
    operations = harness.operations()
    operation = await operations.load(
        namespace=harness.scope.namespace, operation_id=job.operation_id
    )
    await operations.claim(operation)
    return job


# ---------------------------------------------------------------------------------------
# The honest record still works
# ---------------------------------------------------------------------------------------


async def test_a_matching_record_finishes_the_operation(harness: ActionHarness) -> None:
    """A real apply followed by resetting to RUNNING and recovering from durable proof."""

    job, proposal, execution, _record = await _real_apply(harness)
    assert len(harness.agent.invocations) == 1

    await _reset_to_running(harness, job)

    recovered = await harness.worker().execute(job)

    assert recovered.status is ApplicationOperationStatus.SUCCEEDED
    assert len(harness.agent.invocations) == 1
    assert recovered.result_refs == (proposal.action_id.value, execution.execution_id.value)


@pytest.mark.parametrize("entrance", ["worker", "replay", "unknown-outcome"])
async def test_corrupted_view_cannot_be_compensated_by_invocation_input_hash(
    harness: ActionHarness, entrance: str
) -> None:
    job, _proposal, _execution, record = await _real_apply(harness)
    await _reset_to_running(harness, job)
    view = await harness.compile.shareable.load_view(_scope(harness), job.view_id)
    corrupted = replace(view, community_public_label=view.community_public_label + " changed")
    assert corrupted.view_hash == view.view_hash
    assert not _view_hash_verifies(corrupted)
    await harness.compile.unit_of_work.commit(
        TransactionPlan(
            name="corrupt-historical-view",
            operations=(
                PutItem(
                    key=codec_share.view_key(_scope(harness), view.view_id),
                    item=codec_share.encode_view(_scope(harness), corrupted),
                    condition=KeyPresent(),
                ),
            ),
            audit_required=False,
        )
    )
    matching_input_hash = hash_value(to_action_input(corrupted).model_dump(mode="json"))
    assert matching_input_hash != record.input_hash
    await _overwrite_record(harness, replace(record, input_hash=matching_input_hash))
    worker = harness.worker()
    operation = await harness.operations().load(
        namespace=job.namespace, operation_id=job.operation_id
    )
    with pytest.raises(IntegrityError) as raised:
        if entrance == "worker":
            await worker.execute(job)
        elif entrance == "replay":
            await harness.propose_action().execute(worker._command(job))
        else:
            await worker._resolve_unknown_outcome(job, operation, worker._command(job))
    assert raised.value.entity_ref == "SHAREABLE_VIEW"
    unchanged = await harness.operations().load(
        namespace=job.namespace, operation_id=job.operation_id
    )
    assert unchanged.status is ApplicationOperationStatus.RUNNING
    assert unchanged.result_refs == ()
    assert len(harness.agent.invocations) == 1


async def test_historical_recovery_after_pointer_moves_and_view_expires(
    harness: ActionHarness,
) -> None:
    job, proposal, execution, _record = await _real_apply(harness)
    await _invalidate(harness, proposal.action_id, execution.execution_id)
    await _return_to_ready(harness)
    harness.invocation_id = uuid4()
    replacement = await harness.propose_action().execute(
        await harness.command(idempotency_key="historical-replacement")
    )
    assert replacement.action_id != proposal.action_id
    pointer = await harness.compile.shareable.load_current_action_pointer(_scope(harness))
    assert pointer is not None and pointer.action_id == replacement.action_id
    calls = len(harness.agent.invocations)
    harness.compile.clock.instant = harness.view.expires_at + timedelta(days=1)
    await _reset_to_running(harness, job)

    recovered = await harness.worker().execute(job)

    assert recovered.status is ApplicationOperationStatus.SUCCEEDED
    assert recovered.result_refs == (proposal.action_id.value, execution.execution_id.value)
    assert all(identifier.version == 4 for identifier in recovered.result_refs)
    assert len(harness.agent.invocations) == calls


async def test_canonical_view_hash_covers_community_public_label(harness: ActionHarness) -> None:
    view = await harness.prepare()
    assert _view_hash_verifies(view)
    changed = replace(view, community_public_label=view.community_public_label + " changed")
    assert changed.view_hash == view.view_hash
    assert not _view_hash_verifies(changed)


# ---------------------------------------------------------------------------------------
# Fourteen negative tests proving failure to recover artifacts (F09 provenance repair)
# ---------------------------------------------------------------------------------------


async def _assert_recovery_refused(harness: ActionHarness, job: ProposeActionOperationJob) -> None:
    with pytest.raises(IntegrityError):
        await harness.worker().execute(job)

    operation = await harness.operations().load(
        namespace=harness.scope.namespace, operation_id=job.operation_id
    )
    assert operation.status is ApplicationOperationStatus.RUNNING
    assert len(harness.agent.invocations) == 1


async def test_recovery_refused_when_proposal_ref_id_nonexistent(harness: ActionHarness) -> None:
    job, _proposal, _execution, record = await _real_apply(harness)
    await _reset_to_running(harness, job)

    mutated = replace(
        record,
        result_refs=(
            EntityRef(entity_type="ACTION_PROPOSAL", entity_id=uuid4()),
            record.result_refs[1],
        ),
    )
    await _overwrite_record(harness, mutated)
    await _assert_recovery_refused(harness, job)


async def test_recovery_refused_when_execution_ref_id_nonexistent(harness: ActionHarness) -> None:
    job, _proposal, _execution, record = await _real_apply(harness)
    await _reset_to_running(harness, job)

    mutated = replace(
        record,
        result_refs=(
            record.result_refs[0],
            EntityRef(entity_type="ACTION_EXECUTION", entity_id=uuid4()),
        ),
    )
    await _overwrite_record(harness, mutated)
    await _assert_recovery_refused(harness, job)


async def test_recovery_refused_when_valid_proposal_and_unrelated_execution(
    harness: ActionHarness,
) -> None:
    job, _proposal, execution, record = await _real_apply(harness)
    await _reset_to_running(harness, job)

    unrelated_action_id = ActionId(uuid4())
    unrelated_exec_id = ExecutionId(uuid4())
    unrelated_scope = _action_scope(harness, unrelated_action_id)
    unrelated_execution = replace(
        execution, action_id=unrelated_action_id, execution_id=unrelated_exec_id
    )
    await _overwrite_execution(harness, unrelated_scope, unrelated_execution, is_new=True)

    mutated = replace(
        record,
        result_refs=(
            record.result_refs[0],
            EntityRef(entity_type="ACTION_EXECUTION", entity_id=unrelated_exec_id.value),
        ),
    )
    await _overwrite_record(harness, mutated)
    await _assert_recovery_refused(harness, job)


async def test_recovery_refused_when_proposal_action_id_mismatches_execution(
    harness: ActionHarness,
) -> None:
    job, proposal, execution, _record = await _real_apply(harness)
    action_scope = _action_scope(harness, proposal.action_id)
    await _reset_to_running(harness, job)

    mutated_exec = replace(execution, action_id=ActionId(uuid4()))
    await _overwrite_execution(harness, action_scope, mutated_exec)
    await _assert_recovery_refused(harness, job)


async def test_recovery_refused_when_proposal_hash_mismatches_execution(
    harness: ActionHarness,
) -> None:
    job, proposal, execution, _record = await _real_apply(harness)
    action_scope = _action_scope(harness, proposal.action_id)
    await _reset_to_running(harness, job)

    mutated_exec = replace(execution, proposal_hash=Sha256Digest("sha256:" + "0" * 64))
    await _overwrite_execution(harness, action_scope, mutated_exec)
    await _assert_recovery_refused(harness, job)


async def test_recovery_refused_when_view_hash_mismatches_execution(
    harness: ActionHarness,
) -> None:
    job, proposal, execution, _record = await _real_apply(harness)
    action_scope = _action_scope(harness, proposal.action_id)
    await _reset_to_running(harness, job)

    mutated_exec = replace(execution, view_hash=Sha256Digest("sha256:" + "0" * 64))
    await _overwrite_execution(harness, action_scope, mutated_exec)
    await _assert_recovery_refused(harness, job)


async def test_recovery_refused_when_proposal_invocation_id_mismatches_record(
    harness: ActionHarness,
) -> None:
    job, proposal, _execution, _record = await _real_apply(harness)
    action_scope = _action_scope(harness, proposal.action_id)
    await _reset_to_running(harness, job)

    mutated_prop = replace(proposal, agent_invocation_id=uuid4())
    await _overwrite_proposal(harness, action_scope, mutated_prop)
    await _assert_recovery_refused(harness, job)


async def test_recovery_refused_when_proposal_case_id_mismatches_record(
    harness: ActionHarness,
) -> None:
    job, proposal, _execution, _record = await _real_apply(harness)
    action_scope = _action_scope(harness, proposal.action_id)
    await _reset_to_running(harness, job)

    mutated_prop = replace(proposal, case_id=CaseId(uuid4()))
    await _overwrite_proposal(harness, action_scope, mutated_prop)
    await _assert_recovery_refused(harness, job)


async def test_recovery_refused_when_proposal_prompt_version_mismatches_record(
    harness: ActionHarness,
) -> None:
    job, proposal, _execution, _record = await _real_apply(harness)
    action_scope = _action_scope(harness, proposal.action_id)
    await _reset_to_running(harness, job)

    mutated_prop = replace(proposal, prompt_version="action/v99")
    await _overwrite_proposal(harness, action_scope, mutated_prop)
    await _assert_recovery_refused(harness, job)


async def test_recovery_refused_when_proposal_tampered(
    harness: ActionHarness,
) -> None:
    job, proposal, _execution, _record = await _real_apply(harness)
    action_scope = _action_scope(harness, proposal.action_id)
    await _reset_to_running(harness, job)

    mutated_prop = replace(proposal, subject="tampered subject line")
    await _overwrite_proposal(harness, action_scope, mutated_prop)
    await _assert_recovery_refused(harness, job)


async def test_recovery_refused_when_result_refs_contains_extra_ref(
    harness: ActionHarness,
) -> None:
    job, _proposal, _execution, record = await _real_apply(harness)
    await _reset_to_running(harness, job)

    mutated = replace(
        record,
        result_refs=(
            record.result_refs[0],
            record.result_refs[1],
            EntityRef(entity_type="ACTION_PROPOSAL", entity_id=uuid4()),
        ),
    )
    await _overwrite_record(harness, mutated)
    await _assert_recovery_refused(harness, job)


async def test_recovery_refused_when_result_refs_missing_execution_ref(
    harness: ActionHarness,
) -> None:
    job, _proposal, _execution, record = await _real_apply(harness)
    await _reset_to_running(harness, job)

    mutated = replace(record, result_refs=(record.result_refs[0],))
    await _overwrite_record(harness, mutated)
    await _assert_recovery_refused(harness, job)


async def test_recovery_refused_when_result_refs_duplicate_proposal_refs(
    harness: ActionHarness,
) -> None:
    job, _proposal, _execution, record = await _real_apply(harness)
    await _reset_to_running(harness, job)

    mutated = replace(
        record,
        result_refs=(
            record.result_refs[0],
            EntityRef(entity_type="ACTION_PROPOSAL", entity_id=uuid4()),
        ),
    )
    await _overwrite_record(harness, mutated)
    await _assert_recovery_refused(harness, job)


async def test_recovery_refused_when_result_refs_duplicate_execution_refs(
    harness: ActionHarness,
) -> None:
    job, _proposal, _execution, record = await _real_apply(harness)
    await _reset_to_running(harness, job)

    mutated = replace(
        record,
        result_refs=(
            record.result_refs[1],
            EntityRef(entity_type="ACTION_EXECUTION", entity_id=uuid4()),
        ),
    )
    await _overwrite_record(harness, mutated)
    await _assert_recovery_refused(harness, job)


# ---------------------------------------------------------------------------------------
# Adversarial records: none of them may finish anything
# ---------------------------------------------------------------------------------------


async def _assert_refused(
    harness: ActionHarness,
    mutate: Callable[[AgentInvocationResult], AgentInvocationResult],
) -> None:
    """Claim the operation, plant one hostile record at its invocation key, and expect refusal.

    The record is built **after** the operation is started, because starting one mints the
    invocation identity the record has to sit under -- a record planted at a stale key would
    simply not be found, which proves nothing.
    """

    job = await _running_job(harness)
    await _plant(harness, mutate(await _honest_record(harness)))

    with pytest.raises(IntegrityError):
        await harness.worker().execute(job)  # type: ignore[arg-type]

    operation = await harness.operations().load(
        namespace=harness.scope.namespace,
        operation_id=job.operation_id,  # type: ignore[attr-defined]
    )
    assert operation.status is ApplicationOperationStatus.RUNNING
    assert harness.agent.invocations == []


async def test_a_wrong_input_hash_is_refused(harness: ActionHarness) -> None:
    """The load-bearing check: a record describing an invocation over a *different view*."""

    await harness.prepare()
    await _assert_refused(
        harness, lambda record: replace(record, input_hash=hash_value({"another": "view"}))
    )


async def test_a_wrong_agent_is_refused(harness: ActionHarness) -> None:
    await harness.prepare()
    await _assert_refused(
        harness, lambda record: replace(record, agent_name=StoredAgentName.INVESTIGATOR)
    )


async def test_a_wrong_prompt_version_is_refused(harness: ActionHarness) -> None:
    """A runtime serving an unreviewed artifact is not a partially usable answer."""

    await harness.prepare()
    await _assert_refused(harness, lambda record: replace(record, prompt_version="action/v0"))


async def test_a_foreign_result_reference_is_refused(harness: ActionHarness) -> None:
    await harness.prepare()
    await _assert_refused(
        harness,
        lambda record: replace(
            record,
            result_refs=(
                EntityRef(entity_type="ACTION_PROPOSAL", entity_id=uuid4()),
                EntityRef(entity_type="MANDATE_VERSION", entity_id=uuid4()),
            ),
        ),
    )


async def test_a_malformed_result_reference_set_is_refused(harness: ActionHarness) -> None:
    """One reference where two are required: the apply writes both or neither."""

    await harness.prepare()
    await _assert_refused(
        harness, lambda record: replace(record, result_refs=(record.result_refs[0],))
    )


async def test_a_succeeded_outcome_with_mismatched_provenance_is_refused(
    harness: ActionHarness,
) -> None:
    """``outcome == SUCCEEDED`` is the one thing that is *not* enough on its own."""

    def mutate(record: AgentInvocationResult) -> AgentInvocationResult:
        mismatched = replace(
            record,
            prompt_version="action/v9",
            input_hash=hash_value({"foreign": "payload"}),
        )
        assert mismatched.outcome is AgentInvocationOutcome.SUCCEEDED
        return mismatched

    await harness.prepare()
    await _assert_refused(harness, mutate)


# ---------------------------------------------------------------------------------------
# The in-process replay path runs the same check
# ---------------------------------------------------------------------------------------


async def test_the_replay_path_refuses_a_mismatched_record_and_calls_no_model(
    harness: ActionHarness,
) -> None:
    """``ProposeAction._replay`` answered from any record it found at the expected key."""

    await harness.prepare()
    honest = await _honest_record(harness)
    await _plant(harness, replace(honest, input_hash=hash_value({"another": "view"})))

    with pytest.raises(IntegrityError):
        await harness.propose_action().execute(await harness.command())

    assert harness.agent.invocations == []


async def test_the_replay_path_accepts_its_own_record(harness: ActionHarness) -> None:
    """A real apply followed by a redelivery: verified, replayed, and no second model call."""

    await harness.prepare()
    first = await harness.propose_action().execute(await harness.command())

    replayed = await harness.propose_action().execute(await harness.command())

    assert replayed.replayed is True
    assert replayed.action_id == first.action_id
    assert len(harness.agent.invocations) == 1


# ---------------------------------------------------------------------------------------
# The expected input hash is derived independently, not read back
# ---------------------------------------------------------------------------------------


async def test_the_expected_input_hash_comes_from_the_stored_view(
    harness: ActionHarness,
) -> None:
    """Derived from durable, view-bound data through the same canonical schema.

    It is never taken from the record it is compared against, which is the difference between
    a check and a tautology.
    """

    await harness.prepare()
    derived = await expected_action_input_hash(
        harness.compile.shareable,
        _scope(harness),
        view_id=harness.view.view_id,
        view_hash=harness.view.view_hash,
    )

    assert derived == hash_value(to_action_input(harness.view).model_dump(mode="json"))


async def test_a_view_hash_disagreement_is_an_integrity_failure(
    harness: ActionHarness,
) -> None:
    """The operation is bound to an exact view; a stored artifact that disagrees is not it."""

    from chorus.domain.ids import Sha256Digest

    await harness.prepare()
    with pytest.raises(IntegrityError):
        await expected_action_input_hash(
            harness.compile.shareable,
            _scope(harness),
            view_id=harness.view.view_id,
            view_hash=Sha256Digest("sha256:" + "b" * 64),
        )


async def test_every_provenance_dimension_is_reported_not_only_the_first(
    harness: ActionHarness,
) -> None:
    """An operator fixing a misrouted record wants the whole disagreement."""

    await harness.prepare()
    honest = await _honest_record(harness)
    expectation = InvocationExpectation(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
        invocation_id=harness.invocation_id,
        input_hash=honest.input_hash,
    )

    assert invocation_provenance_failures(honest, expectation) == ()

    broken = replace(
        honest,
        invocation_id=uuid4(),
        agent_name=StoredAgentName.MONITOR,
        prompt_version="action/v0",
        input_hash=hash_value({"foreign": "payload"}),
        result_refs=(),
    )
    failures = invocation_provenance_failures(broken, expectation)

    assert set(failures) == {
        "RECORD_INVOCATION_MISMATCH",
        "RECORD_AGENT_MISMATCH",
        "RECORD_PROMPT_VERSION_MISMATCH",
        "RECORD_INPUT_HASH_MISMATCH",
        "RECORD_RESULT_REFS_MISMATCH",
    }


async def test_a_record_from_another_case_is_refused(harness: ActionHarness) -> None:
    """Scope is part of provenance: one case's record may not finish another's operation."""

    from chorus.domain.ids import CaseId

    await harness.prepare()
    honest = await _honest_record(harness)
    expectation = InvocationExpectation(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=CaseId(uuid4()),
        invocation_id=harness.invocation_id,
        input_hash=honest.input_hash,
    )

    assert "RECORD_SCOPE_MISMATCH" in invocation_provenance_failures(honest, expectation)
