"""A commit proof says a transaction committed. It does not say *which rows*, and it never did.

The failure this module pins was reproduced on both drivers and is narrow enough to state in
five steps: the approval transaction commits; the domain-1 completion write is lost; the
domain-2 proof is corrupted so its ``ACTION_EXECUTION`` reference names a different execution
under the same action; the caller retries the identical approval; recovery replays the proof and
answers with the **other** execution -- then finishes domain 1 with the other execution's
reference, so the receipt permanently describes something nobody asked about.

What makes it interesting is that every check that existed passed. The requested execution was
intact, verified perfectly, and was never the problem. The proof's own provenance was, and
provenance is a comparison between the proof and the request rather than between a row and
itself. So the repair checks the reference set before any row is read
(:func:`chorus.application.commands.approve_action.approval_proof_failures`), and then checks
the artifacts the proof named against the request field by field
(:func:`chorus.application.commands.approve_action.approval_artifact_failures`).

Every test below asserts the same five refusals: an ``IntegrityError``, a domain-1 record still
``IN_PROGRESS``, the requested execution untouched, the foreign execution untouched, and exactly
one ``approve-action`` transaction ever committed -- no second approval and no second ``DRAFT``
compare-and-swap.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from tests.fixtures.send import APPROVER_HASH, SendHarness

from chorus.application.commands.approve_action import (
    ApprovalRecoveryFailure,
    ApproveActionCommand,
    RecoveredApproval,
    approval_artifact_failures,
    approval_proof_failures,
)
from chorus.application.services.action_authorization import (
    approval_key,
    approval_request_hash,
    approval_start_key_hash,
    approval_transaction_key_hash,
)
from chorus.domain.entities import (
    ActionExecution,
    ActionExecutionState,
    Approval,
    ApprovalDecision,
)
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import ApprovalId, CaseId, CommunityId, ExecutionId, Sha256Digest
from chorus.infrastructure.dynamodb import codec_idempotency
from chorus.ports.errors import PersistenceError, PersistenceErrorCode
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyKey,
    IdempotencyRecord,
    IdempotencyStatus,
)
from chorus.ports.storage import KeyPresent, PutItem, TableName
from chorus.ports.unit_of_work import TransactionPlan
from chorus.privacy.canonical import hash_approval

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------------------
# The reproduction
# ---------------------------------------------------------------------------------------


def _transaction_key(send_harness: SendHarness, command: ApproveActionCommand) -> IdempotencyKey:
    """Domain 2: the approval transaction's own commit proof."""

    return approval_key(
        namespace=command.namespace,
        action_id=command.action_id,
        actor_id_hash=command.approver_id_hash,
        key_hash=approval_transaction_key_hash(command.idempotency_key),
    )


def _start_key(send_harness: SendHarness, command: ApproveActionCommand) -> IdempotencyKey:
    """Domain 1: the caller's HTTP receipt."""

    return approval_key(
        namespace=command.namespace,
        action_id=command.action_id,
        actor_id_hash=command.approver_id_hash,
        key_hash=approval_start_key_hash(command.idempotency_key),
    )


async def _overwrite_record(send_harness: SendHarness, record: IdempotencyRecord) -> None:
    """Corrupt one durable idempotency item the way storage or a bad migration would."""

    await send_harness.action.compile.unit_of_work.commit(
        TransactionPlan(
            name="corrupt-proof",
            operations=(
                PutItem(
                    key=codec_idempotency.idempotency_item_key(
                        record.key, table=TableName.SHAREABLE
                    ),
                    item=codec_idempotency.encode_idempotency(record, table=TableName.SHAREABLE),
                    condition=KeyPresent(),
                ),
            ),
            audit_required=False,
        )
    )


async def _second_execution(send_harness: SendHarness) -> ActionExecution:
    """A second, entirely valid ``DRAFT`` execution in the same action partition.

    Not a fabricated row: it is the real ``DRAFT`` the proposal created, written again under a
    fresh identifier, so the foreign execution a corrupted proof could name is one that would
    load and verify like any other.
    """

    draft = await send_harness.execution()
    foreign = replace(draft, execution_id=ExecutionId(uuid4()))
    scope = await send_harness.action_scope()
    await send_harness.action.compile.unit_of_work.commit(
        TransactionPlan(
            name="second-execution",
            operations=(
                send_harness.action.compile.shareable.stage_create_execution(scope, foreign),
            ),
            audit_required=False,
        )
    )
    return foreign


async def _approve_losing_the_receipt(
    send_harness: SendHarness, command: ApproveActionCommand
) -> None:
    """Commit the decision and lose the domain-1 completion, exactly as a crash would."""

    send_harness.action.unit_of_work.fail_by_name["approve-action-complete"] = PersistenceError(
        PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME, "IDEMPOTENCY"
    )
    with pytest.raises(PersistenceError):
        await send_harness.approve_action().execute(command)


async def _assert_refused_and_unchanged(
    send_harness: SendHarness,
    command: ApproveActionCommand,
    *,
    foreign: ActionExecution,
    approved_version: int,
    approval_id: ApprovalId,
) -> None:
    """The five facts a refusal on this path owes, asserted together every time."""

    start = await send_harness.action.compile.idempotency.load(_start_key(send_harness, command))
    assert start is not None
    assert start.status is IdempotencyStatus.IN_PROGRESS
    assert start.result_entity_refs == ()

    scope = await send_harness.action_scope()
    requested = await send_harness.execution()
    assert requested.execution_id == command.execution_id
    assert requested.state is ActionExecutionState.APPROVED
    assert requested.version == approved_version
    assert requested.approval_id == approval_id

    still_draft = await send_harness.action.compile.shareable.load_execution(
        scope, foreign.execution_id
    )
    assert still_draft.state is ActionExecutionState.DRAFT
    assert still_draft.version == foreign.version
    assert still_draft.approval_id is None

    decisions = [
        plan for plan in send_harness.action.unit_of_work.plans if plan.name == "approve-action"
    ]
    assert len(decisions) == 1


async def test_a_proof_naming_another_execution_never_completes_the_receipt(
    send_harness: SendHarness,
) -> None:
    """The exact reproduction: only the domain-2 execution reference is corrupted.

    Nothing else is touched. The approval row is genuine and verifies, the requested execution
    is genuine and verifies, the request hash matches, and the key is the caller's own. The one
    thing that is wrong is the thing that used to be unchecked.
    """

    await send_harness.prepare()
    foreign = await _second_execution(send_harness)
    command = await send_harness.approval_command()
    await _approve_losing_the_receipt(send_harness, command)

    committed = await send_harness.execution()
    assert committed.state is ActionExecutionState.APPROVED
    assert committed.approval_id is not None

    key = _transaction_key(send_harness, command)
    proof = await send_harness.action.compile.idempotency.load(key)
    assert proof is not None
    approval_ref = next(ref for ref in proof.result_entity_refs if ref.entity_type == "APPROVAL")
    execution_ref = next(
        ref for ref in proof.result_entity_refs if ref.entity_type == "ACTION_EXECUTION"
    )
    await _overwrite_record(
        send_harness,
        replace(
            proof,
            result_entity_refs=(
                approval_ref,
                replace(execution_ref, entity_id=foreign.execution_id.value),
            ),
            version=proof.version + 1,
        ),
    )

    with pytest.raises(IntegrityError):
        await send_harness.approve_action().execute(command)

    await _assert_refused_and_unchanged(
        send_harness,
        command,
        foreign=foreign,
        approved_version=committed.version,
        approval_id=ApprovalId(committed.approval_id.value),
    )


async def test_a_proof_naming_another_approval_is_refused(
    send_harness: SendHarness,
) -> None:
    """The mirror image: the execution reference is honest and the approval reference is not.

    The foreign approval here is a *valid* one -- correctly sealed, in the right partition, for
    the second execution -- so nothing about it fails a hash check. It fails because it binds an
    execution this request never named.
    """

    await send_harness.prepare()
    foreign_execution = await _second_execution(send_harness)
    command = await send_harness.approval_command()
    await _approve_losing_the_receipt(send_harness, command)

    committed = await send_harness.execution()
    assert committed.approval_id is not None
    scope = await send_harness.action_scope()
    genuine = await send_harness.action.compile.shareable.load_approval(
        scope, ApprovalId(committed.approval_id.value)
    )
    draft = replace(
        genuine,
        approval_id=ApprovalId(uuid4()),
        execution_id=foreign_execution.execution_id,
    )
    foreign_approval = replace(draft, approval_hash=hash_approval(draft))
    await send_harness.action.compile.unit_of_work.commit(
        TransactionPlan(
            name="second-approval",
            operations=(
                send_harness.action.compile.shareable.stage_append_approval(
                    scope, foreign_approval
                ),
            ),
            audit_required=False,
        )
    )

    key = _transaction_key(send_harness, command)
    proof = await send_harness.action.compile.idempotency.load(key)
    assert proof is not None
    execution_ref = next(
        ref for ref in proof.result_entity_refs if ref.entity_type == "ACTION_EXECUTION"
    )
    await _overwrite_record(
        send_harness,
        replace(
            proof,
            result_entity_refs=(
                EntityRef(entity_type="APPROVAL", entity_id=foreign_approval.approval_id.value),
                execution_ref,
            ),
            version=proof.version + 1,
        ),
    )

    with pytest.raises(IntegrityError):
        await send_harness.approve_action().execute(command)

    await _assert_refused_and_unchanged(
        send_harness,
        command,
        foreign=foreign_execution,
        approved_version=committed.version,
        approval_id=ApprovalId(committed.approval_id.value),
    )


async def test_a_proof_carrying_a_foreign_reference_set_is_refused(
    send_harness: SendHarness,
) -> None:
    """A malformed proof is refused before a row is read, not interpreted around."""

    await send_harness.prepare()
    foreign = await _second_execution(send_harness)
    command = await send_harness.approval_command()
    await _approve_losing_the_receipt(send_harness, command)
    committed = await send_harness.execution()
    assert committed.approval_id is not None

    key = _transaction_key(send_harness, command)
    proof = await send_harness.action.compile.idempotency.load(key)
    assert proof is not None
    await _overwrite_record(
        send_harness,
        replace(
            proof,
            result_entity_refs=(
                *proof.result_entity_refs,
                EntityRef(entity_type="ACTION_PROPOSAL", entity_id=command.action_id.value),
            ),
            version=proof.version + 1,
        ),
    )

    with pytest.raises(IntegrityError):
        await send_harness.approve_action().execute(command)

    await _assert_refused_and_unchanged(
        send_harness,
        command,
        foreign=foreign,
        approved_version=committed.version,
        approval_id=ApprovalId(committed.approval_id.value),
    )


async def test_an_intact_proof_still_recovers_normally(send_harness: SendHarness) -> None:
    """The control. The repair must refuse corrupted provenance and nothing else.

    A recovery that refused honest proofs would be the same outage the recovery path was built
    to remove: a human told their approval failed while it sat committed one row away.
    """

    await send_harness.prepare()
    await _second_execution(send_harness)
    command = await send_harness.approval_command()
    await _approve_losing_the_receipt(send_harness, command)
    committed = await send_harness.execution()

    recovered = await send_harness.approve_action().execute(command)

    assert recovered.replayed is True
    assert recovered.execution_id == command.execution_id
    assert recovered.execution_version == committed.version
    assert recovered.execution_state is ActionExecutionState.APPROVED
    start = await send_harness.action.compile.idempotency.load(_start_key(send_harness, command))
    assert start is not None
    assert start.status is IdempotencyStatus.COMPLETED
    assert {ref.entity_id for ref in start.result_entity_refs} == {
        recovered.approval_id.value,
        command.execution_id.value,
    }
    decisions = [
        plan for plan in send_harness.action.unit_of_work.plans if plan.name == "approve-action"
    ]
    assert len(decisions) == 1


# ---------------------------------------------------------------------------------------
# The predicates themselves, over artifacts too awkward to persist
# ---------------------------------------------------------------------------------------


async def _recovered(send_harness: SendHarness, command: ApproveActionCommand) -> RecoveredApproval:
    """One honest recovery bundle, assembled from a real committed decision and its real replay."""

    await _approve_losing_the_receipt(send_harness, command)
    result = await send_harness.approve_action().execute(command)
    scope = await send_harness.action_scope()
    approval = await send_harness.action.compile.shareable.load_approval(scope, result.approval_id)
    execution = await send_harness.action.compile.shareable.load_execution(
        scope, result.execution_id
    )
    proposal = await send_harness.proposal()
    return RecoveredApproval(
        approval_ref=EntityRef(entity_type="APPROVAL", entity_id=approval.approval_id.value),
        execution_ref=EntityRef(
            entity_type="ACTION_EXECUTION",
            entity_id=execution.execution_id.value,
            version=execution.version,
        ),
        approval=approval,
        proposal=proposal,
        execution=execution,
        result=result,
    )


async def test_the_artifact_predicate_accepts_an_honest_recovery(
    send_harness: SendHarness,
) -> None:
    """The baseline the negative cases are measured against."""

    await send_harness.prepare()
    command = await send_harness.approval_command()

    assert approval_artifact_failures(command, await _recovered(send_harness, command)) == ()


@pytest.mark.parametrize(
    ("mutate", "failure"),
    [
        pytest.param(
            lambda approval: replace(approval, case_id=CaseId(uuid4())),
            ApprovalRecoveryFailure.SCOPE_MISMATCH,
            id="foreign-case",
        ),
        pytest.param(
            lambda approval: replace(approval, community_id=CommunityId(uuid4())),
            ApprovalRecoveryFailure.SCOPE_MISMATCH,
            id="foreign-community",
        ),
        pytest.param(
            lambda approval: replace(approval, decision=ApprovalDecision.REJECTED),
            ApprovalRecoveryFailure.DECISION_MISMATCH,
            id="other-decision",
        ),
        pytest.param(
            lambda approval: replace(approval, request_key_hash=Sha256Digest("sha256:" + "c" * 64)),
            ApprovalRecoveryFailure.REQUEST_KEY_MISMATCH,
            id="other-client-key",
        ),
        pytest.param(
            lambda approval: replace(approval, approver_id_hash=Sha256Digest("sha256:" + "f" * 64)),
            ApprovalRecoveryFailure.APPROVER_MISMATCH,
            id="other-approver",
        ),
        pytest.param(
            lambda approval: replace(approval, proposal_hash=Sha256Digest("sha256:" + "a" * 64)),
            ApprovalRecoveryFailure.PROPOSAL_BINDING_MISMATCH,
            id="other-proposal-digest",
        ),
        pytest.param(
            lambda approval: replace(approval, view_hash=Sha256Digest("sha256:" + "b" * 64)),
            ApprovalRecoveryFailure.PROPOSAL_BINDING_MISMATCH,
            id="other-view-digest",
        ),
        pytest.param(
            lambda approval: replace(approval, authorization_version=99),
            ApprovalRecoveryFailure.AUTHORIZATION_VERSION_MISMATCH,
            id="other-authorization-epoch",
        ),
    ],
)
async def test_the_artifact_predicate_refuses_every_broken_binding(
    send_harness: SendHarness,
    mutate: object,
    failure: ApprovalRecoveryFailure,
) -> None:
    """Each durable field the recovery now verifies, moved one at a time.

    The approval is re-sealed after each mutation, so none of these is caught by the digest
    check -- every one of them is caught by the binding it is about. A field that only failed
    because the hash moved would prove nothing about the field.
    """

    await send_harness.prepare()
    command = await send_harness.approval_command()
    honest = await _recovered(send_harness, command)
    draft: Approval = mutate(honest.approval)  # type: ignore[operator]
    broken = replace(honest, approval=replace(draft, approval_hash=hash_approval(draft)))

    assert failure.value in approval_artifact_failures(command, broken)


async def test_the_artifact_predicate_refuses_an_approval_that_no_longer_verifies(
    send_harness: SendHarness,
) -> None:
    """A tampered approval whose digest was *not* re-sealed is its own failure."""

    await send_harness.prepare()
    command = await send_harness.approval_command()
    honest = await _recovered(send_harness, command)
    broken = replace(
        honest, approval=replace(honest.approval, authorization_version=honest.approval.version + 7)
    )

    assert ApprovalRecoveryFailure.APPROVAL_HASH_MISMATCH.value in approval_artifact_failures(
        command, broken
    )


async def test_the_proof_predicate_refuses_a_foreign_execution_before_any_read(
    send_harness: SendHarness,
) -> None:
    """The cheap half: reference-set shape and the requested execution, over the record alone."""

    await send_harness.prepare()
    command = await send_harness.approval_command()
    request_hash = approval_request_hash(
        case_id=command.case_id,
        action_id=command.action_id,
        execution_id=command.execution_id,
        decision=command.decision,
        expected_execution_version=command.expected_execution_version,
        view_hash=command.view_hash,
        proposal_hash=command.proposal_hash,
        preview_hash=command.preview_hash,
    )
    await _approve_losing_the_receipt(send_harness, command)
    proof = await send_harness.action.compile.idempotency.load(
        _transaction_key(send_harness, command)
    )
    assert proof is not None
    assert approval_proof_failures(command, proof, request_hash) == ()

    execution_ref = next(
        ref for ref in proof.result_entity_refs if ref.entity_type == "ACTION_EXECUTION"
    )
    approval_ref = next(ref for ref in proof.result_entity_refs if ref.entity_type == "APPROVAL")

    foreign = replace(
        proof,
        result_entity_refs=(approval_ref, replace(execution_ref, entity_id=uuid4())),
    )
    assert ApprovalRecoveryFailure.EXECUTION_MISMATCH.value in approval_proof_failures(
        command, foreign, request_hash
    )

    duplicated = replace(
        proof,
        result_entity_refs=(
            approval_ref,
            execution_ref,
            EntityRef(entity_type="ACTION_EXECUTION", entity_id=uuid4()),
        ),
    )
    assert ApprovalRecoveryFailure.RESULT_REFS_MISMATCH.value in approval_proof_failures(
        command, duplicated, request_hash
    )

    assert ApprovalRecoveryFailure.REQUEST_HASH_MISMATCH.value in approval_proof_failures(
        command, proof, Sha256Digest("sha256:" + "9" * 64)
    )


async def test_a_rejection_recovers_without_requiring_the_callers_digests(
    send_harness: SendHarness,
) -> None:
    """A rejection is allowed to be stale, so its recovery must not demand fresh digests.

    Checks 3 and 5 never run for a ``REJECTED`` decision -- a human must always be able to say
    no, and rejecting a proposal that has gone stale is the correct response to one. Requiring
    the approval's bound digests to equal the caller's at recovery time would refuse a recovery
    the original decision was entitled to make. The same drift on an ``APPROVED`` decision is a
    refusal, which is what the second half asserts.
    """

    await send_harness.prepare()
    command = await send_harness.approval_command(decision=ApprovalDecision.REJECTED)
    honest = await _recovered(send_harness, command)
    drifted = replace(
        command,
        proposal_hash=Sha256Digest("sha256:" + "7" * 64),
        view_hash=Sha256Digest("sha256:" + "8" * 64),
    )

    assert approval_artifact_failures(drifted, honest) == ()
    assert honest.result.decision is ApprovalDecision.REJECTED
    assert honest.approval.approver_id_hash == APPROVER_HASH


async def test_an_approval_recovery_does_require_the_callers_digests(
    send_harness: SendHarness,
) -> None:
    """The contrast: an ``APPROVED`` recovery answering about other artifacts is refused."""

    await send_harness.prepare()
    command = await send_harness.approval_command()
    honest = await _recovered(send_harness, command)
    drifted = replace(command, proposal_hash=Sha256Digest("sha256:" + "7" * 64))

    assert ApprovalRecoveryFailure.REQUESTED_ARTIFACT_MISMATCH.value in approval_artifact_failures(
        drifted, honest
    )
