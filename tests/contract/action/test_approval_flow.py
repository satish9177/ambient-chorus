"""The human decision: what it binds, what refuses it, and what exactly one of them commits.

Every test here runs against a proposal a real compile and a real Action invocation produced,
so the hashes a decision binds are the hashes the production authorities sealed. A fabricated
proposal would make each assertion a statement about the fixture.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest
from tests.fixtures.send import APPROVER_HASH, SendHarness

from chorus.application.commands.approve_action import (
    APPROVE_PARTICIPANTS,
    REJECT_PARTICIPANTS,
    ApprovalDenial,
    ApprovalDeniedError,
)
from chorus.application.commands.invalidate_action import (
    CLEARED_CODE,
    INVALIDATE_PARTICIPANTS,
    WITHDRAWN_CODE,
    InvalidationDenial,
    InvalidationDeniedError,
)
from chorus.application.services.action_authorization import APPROVAL_LIFETIME
from chorus.domain.entities import (
    ActionExecutionState,
    ActionProposalStatus,
    ApprovalDecision,
    ApproverAssurance,
    CaseState,
)
from chorus.domain.ids import ActionId, ExecutionId, Sha256Digest
from chorus.ports.errors import PersistenceConflictError
from chorus.privacy.canonical import APPROVAL_HASH_OMITTED_FIELDS, hash_approval, verify_hash

pytestmark = pytest.mark.anyio

_OTHER_DIGEST = Sha256Digest("sha256:" + "c" * 64)
_OTHER_UUID = UUID("11111111-2222-4333-8444-555555555555")
"""An identifier belonging to nothing in this world, for the cross-binding refusals."""


async def test_an_approval_binds_the_proposal_execution_and_epoch(
    send_harness: SendHarness,
) -> None:
    """The decision stores five bindings directly and reaches the rest through the chain.

    ``case_id``, ``action_id``, ``execution_id``, ``proposal_hash``, ``view_hash``, and
    ``authorization_version`` are on the row. ``view_id``, ``preview_hash``,
    ``template_version``, ``from_identity_id``, and the whole destination routing triple are
    bound **transitively** through ``proposal_hash`` -- and none of them is copied here,
    because a second copy of a fact is a thing that can disagree with the first (ADR-023 SS 3).
    """

    await send_harness.prepare()
    proposal = await send_harness.proposal()
    pointer = await send_harness.pointer()

    result = await send_harness.approve()

    approval = await send_harness.action.compile.shareable.load_approval(
        await send_harness.action_scope(), result.approval_id
    )
    assert approval.case_id == send_harness.action.case_id
    assert approval.action_id == pointer.action_id
    assert approval.execution_id == pointer.execution_id
    assert approval.proposal_hash == proposal.proposal_hash
    assert approval.view_hash == proposal.view_hash
    assert approval.authorization_version == proposal.authorization_version
    assert approval.approver_id_hash == APPROVER_HASH
    assert approval.approver_assurance is ApproverAssurance.DEMO_SHARED_TOKEN
    assert approval.schema_version == "approval/v2"
    # Nothing transitively bound is duplicated onto the row.
    assert not hasattr(approval, "preview_hash")
    assert not hasattr(approval, "view_id")
    assert not hasattr(approval, "destination_id")
    assert not hasattr(approval, "routing_token")
    assert not hasattr(approval, "from_identity_id")


async def test_the_approval_digest_verifies_against_the_stored_row(
    send_harness: SendHarness,
) -> None:
    """Recomputation is meaningful at any later instant, because nothing here ever moves."""

    await send_harness.prepare()
    result = await send_harness.approve()

    approval = await send_harness.action.compile.shareable.load_approval(
        await send_harness.action_scope(), result.approval_id
    )
    assert hash_approval(approval) == approval.approval_hash
    assert verify_hash(approval, approval.approval_hash, omit_fields=APPROVAL_HASH_OMITTED_FIELDS)


async def test_approval_expiry_is_the_earlier_of_fifteen_minutes_and_the_view(
    send_harness: SendHarness,
) -> None:
    """``min(approved_at + 15 minutes, view.expires_at)``.

    An approval never outlives the disclosure authority it was made against, so the view's own
    expiry is a ceiling on it rather than a separate deadline somebody has to remember.
    """

    view = await send_harness.prepare()
    result = await send_harness.approve()

    assert result.expires_at <= view.expires_at
    assert result.expires_at <= send_harness.action.compile.clock.now() + APPROVAL_LIFETIME


async def test_the_approval_moves_the_draft_execution_and_takes_no_case_edge(
    send_harness: SendHarness,
) -> None:
    """An approval is not a lifecycle transition, so the case appears only as a condition."""

    await send_harness.prepare()
    case_before = await send_harness.action.compile.core.load_case(send_harness.action.scope)

    result = await send_harness.approve()

    execution = await send_harness.execution()
    assert execution.state is ActionExecutionState.APPROVED
    assert execution.version == 2
    assert execution.approval_id == result.approval_id
    # The send key is defined *over* the approval, which is why a DRAFT cannot carry one.
    assert execution.idempotency_key is not None
    case_after = await send_harness.action.compile.core.load_case(send_harness.action.scope)
    assert case_after == case_before


async def test_approve_participant_count_is_exactly_five(send_harness: SendHarness) -> None:
    """Asserted arithmetically against the staged plan, not against the constant."""

    await send_harness.prepare()
    await send_harness.approve()

    plan = send_harness.action.unit_of_work.plan("approve-action")
    assert len(plan.operations) == APPROVE_PARTICIPANTS == 5


async def test_second_decision_on_one_draft_conflicts(send_harness: SendHarness) -> None:
    """Two approvals, or an approval and a rejection, resolve to exactly one commit.

    Run concurrently rather than in sequence, because a sequential pair only ever exercises the
    cheap read-time refusal. Whichever way the loser is refused -- it read a moved execution, or
    it lost the compare-and-swap -- **exactly one** of any number of concurrent decisions
    commits, and that is the whole guarantee.
    """

    await send_harness.prepare()
    approve = await send_harness.approval_command(idempotency_key="approve-key-first")
    reject = await send_harness.approval_command(
        decision=ApprovalDecision.REJECTED, idempotency_key="approve-key-second"
    )

    outcomes = await asyncio.gather(
        send_harness.approve_action().execute(approve),
        send_harness.approve_action().execute(reject),
        return_exceptions=True,
    )

    committed = [item for item in outcomes if not isinstance(item, BaseException)]
    refused = [item for item in outcomes if isinstance(item, BaseException)]
    assert len(committed) == 1
    assert len(refused) == 1
    assert isinstance(refused[0], ApprovalDeniedError | PersistenceConflictError)
    execution = await send_harness.execution()
    assert execution.version == 2


async def test_a_decision_that_wins_the_reads_still_loses_the_compare_and_swap(
    send_harness: SendHarness,
) -> None:
    """The transaction repeats the reads as conditions, so a stale tab commits nothing.

    A decision whose checks all passed, made against a world that then moved before its write
    landed, is the race the read-time checks structurally cannot see. The ``DRAFT@1``
    compare-and-swap is what refuses it, and this is the only shape of test that reaches it.
    """

    await send_harness.prepare()
    command = await send_harness.approval_command(idempotency_key="approve-key-loser")

    async def somebody_else_decides_first() -> None:
        await send_harness.reject(idempotency_key="approve-key-winner")

    send_harness.action.unit_of_work.before_commit.append(somebody_else_decides_first)

    with pytest.raises(PersistenceConflictError):
        await send_harness.approve_action().execute(command)

    execution = await send_harness.execution()
    assert execution.state is ActionExecutionState.FAILED
    assert execution.version == 2


async def test_a_replay_under_the_same_key_writes_nothing_and_answers_from_the_record(
    send_harness: SendHarness,
) -> None:
    """A duplicate approval creates no new execution and changes no approved bytes."""

    await send_harness.prepare()
    command = await send_harness.approval_command()

    first = await send_harness.approve_action().execute(command)
    second = await send_harness.approve_action().execute(command)

    assert second.replayed is True
    assert second.approval_id == first.approval_id
    assert second.execution_version == first.execution_version


@pytest.mark.parametrize(
    ("overrides", "denial"),
    [
        pytest.param(
            {"proposal_hash": _OTHER_DIGEST},
            ApprovalDenial.PROPOSAL_HASH_MISMATCH,
            id="proposal-hash-mismatch",
        ),
        pytest.param(
            {"preview_hash": _OTHER_DIGEST},
            ApprovalDenial.PREVIEW_HASH_MISMATCH,
            id="preview-hash-mismatch",
        ),
        pytest.param(
            {"view_hash": _OTHER_DIGEST},
            ApprovalDenial.VIEW_HASH_MISMATCH,
            id="view-hash-mismatch",
        ),
        pytest.param(
            {"expected_execution_version": 7},
            ApprovalDenial.EXECUTION_VERSION_MISMATCH,
            id="stale-execution-version",
        ),
    ],
)
async def test_stale_tab_cannot_approve_a_replaced_proposal(
    send_harness: SendHarness, overrides: dict[str, object], denial: ApprovalDenial
) -> None:
    """Refused three ways over, and each way is checked on its own.

    A browser holding an old view of the world carries an old ``proposal_hash``, an old
    ``expected_execution_version``, and an ``action_id`` the pointer may no longer name. Each
    is parameterized separately, because a test that only moved one of them could pass while
    the other two checks were absent.
    """

    await send_harness.prepare()

    with pytest.raises(ApprovalDeniedError) as error:
        await send_harness.approve(**overrides)
    assert error.value.denial is denial

    execution = await send_harness.execution()
    assert execution.state is ActionExecutionState.DRAFT
    assert execution.version == 1


async def test_an_approval_naming_a_foreign_execution_is_refused(
    send_harness: SendHarness,
) -> None:
    """The pointer names the execution; a body that names another is not about this proposal."""

    await send_harness.prepare()

    with pytest.raises(ApprovalDeniedError) as error:
        await send_harness.approve(execution_id=ExecutionId(_OTHER_UUID))
    assert error.value.denial is ApprovalDenial.EXECUTION_NOT_CURRENT


async def test_an_approval_naming_a_foreign_action_is_refused(
    send_harness: SendHarness,
) -> None:
    """A superseded ``action_id`` in a stale URL never reaches a decision."""

    await send_harness.prepare()

    with pytest.raises(Exception) as error:
        await send_harness.approve(action_id=ActionId(_OTHER_UUID))
    assert type(error.value).__name__ in {"ApprovalDeniedError", "NotFoundError"}


async def test_approval_refuses_when_deployment_configuration_moved(
    send_harness: SendHarness,
) -> None:
    """Refusing here is strictly kinder than letting a human approve what the fence will refuse.

    The destination registry version, the routing token, the policy build, and the compiler
    version are deployment-owned rather than case-owned, so they sit outside
    ``authorization_version`` and a verified ``proposal_hash`` proves only that the old artifact
    is internally coherent (ADR-020 SS 3).
    """

    await send_harness.prepare()
    moved = send_harness.rotated_destination(registry_version=99)

    with pytest.raises(ApprovalDeniedError) as error:
        await send_harness.approve_action(destination=moved).execute(
            await send_harness.approval_command()
        )
    assert error.value.denial is ApprovalDenial.DEPLOYMENT_CONFIGURATION_MOVED


async def test_approval_refuses_an_expired_view(send_harness: SendHarness) -> None:
    """Equality at expiry means expired, and no storage condition can express it."""

    view = await send_harness.prepare()
    send_harness.action.compile.clock.instant = view.expires_at

    with pytest.raises(ApprovalDeniedError) as error:
        await send_harness.approve()
    assert error.value.denial is ApprovalDenial.VIEW_EXPIRED


# ---------------------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------------------


async def test_rejection_of_a_stale_proposal_succeeds(send_harness: SendHarness) -> None:
    """A proposal that can never be approved must still be clearable.

    Checks 3 to 7 do not apply to a rejection. A reject path that staleness could block would
    strand the case with a proposal nobody can approve and nobody can clear -- which is exactly
    the state this test constructs, by expiring the view first.
    """

    view = await send_harness.prepare()
    send_harness.action.compile.clock.instant = view.expires_at

    result = await send_harness.reject()

    assert result.decision is ApprovalDecision.REJECTED
    execution = await send_harness.execution()
    assert execution.state is ActionExecutionState.FAILED
    assert execution.failure_code == "PROPOSAL_REJECTED"
    pointer = await send_harness.pointer()
    assert pointer.status is ActionProposalStatus.INVALIDATED


async def test_reject_participant_count_is_exactly_six(send_harness: SendHarness) -> None:
    """Shape A plus the pointer. The count is identical on both readiness branches."""

    await send_harness.prepare()
    await send_harness.reject()

    plan = send_harness.action.unit_of_work.plan("reject-action")
    assert len(plan.operations) == REJECT_PARTICIPANTS == 6


async def test_rejection_returns_the_case_to_ready_when_readiness_remains(
    send_harness: SendHarness,
) -> None:
    """Lifecycle only: ``version N -> N+1``, ``authorization_version A -> A``."""

    await send_harness.prepare()
    before = await send_harness.action.compile.core.load_case(send_harness.action.scope)

    result = await send_harness.reject()

    assert result.case_state is CaseState.READY_FOR_ACTION
    assert result.case_version == before.version + 1
    # A human clearing a draft message is not an authorization event.
    assert result.authorization_version == before.authorization_version


async def test_a_rejected_approval_row_is_written_and_is_immutable(
    send_harness: SendHarness,
) -> None:
    """The decision is recorded whichever way it went; only the case effect differs."""

    await send_harness.prepare()
    result = await send_harness.reject()

    approval = await send_harness.action.compile.shareable.load_approval(
        await send_harness.action_scope(), result.approval_id
    )
    assert approval.decision is ApprovalDecision.REJECTED
    assert hash_approval(approval) == approval.approval_hash


# ---------------------------------------------------------------------------------------
# Invalidation: withdrawal and clearing
# ---------------------------------------------------------------------------------------


async def test_withdrawal_moves_an_approved_execution_to_failed(
    send_harness: SendHarness,
) -> None:
    """An approval can be taken back before anything external has happened."""

    await send_harness.prepare()
    await send_harness.approve()

    result = await send_harness.invalidate()

    assert result.reason_code == WITHDRAWN_CODE
    assert result.execution_state is ActionExecutionState.FAILED
    assert result.pointer_status is ActionProposalStatus.INVALIDATED


async def test_invalidation_after_definite_send_failure_frees_the_case(
    send_harness: SendHarness,
) -> None:
    """The failure matrix's stated remedy is reachable.

    A definite send failure leaves the execution ``FAILED`` and the pointer still ``DRAFT``, so
    before the invalidation route existed the "create and approve a fresh proposal" remedy had
    no path at all: a new proposal needs an ``INVALIDATED`` pointer whose execution is terminal
    ``FAILED``, and only rejection set a pointer to ``INVALIDATED``.
    """

    await send_harness.prepare()
    await send_harness.approve()
    moved = send_harness.rotated_destination(registry_version=99)
    await send_harness.send_action(destination=moved).execute(await send_harness.send_command())

    execution = await send_harness.execution()
    assert execution.state is ActionExecutionState.FAILED
    pointer = await send_harness.pointer()
    assert pointer.status is ActionProposalStatus.DRAFT

    result = await send_harness.invalidate(expected_execution_version=execution.version)

    assert result.reason_code == CLEARED_CODE
    assert result.pointer_status is ActionProposalStatus.INVALIDATED
    assert result.case_state is CaseState.READY_FOR_ACTION


async def test_clearing_a_terminal_failure_writes_no_execution_row(
    send_harness: SendHarness,
) -> None:
    """The execution participates as a read-only condition, never as a second write.

    A ``PutItem`` here would rewrite a row that records something that already happened, which
    is what monotonic presence exists to refuse.
    """

    from chorus.ports.storage import CheckItem

    await send_harness.prepare()
    await send_harness.reject()
    execution = await send_harness.execution()

    await send_harness.invalidate(expected_execution_version=execution.version)

    plan = send_harness.action.unit_of_work.plan("invalidate-action")
    assert len(plan.operations) == INVALIDATE_PARTICIPANTS == 5
    assert isinstance(plan.operations[0], CheckItem)


@pytest.mark.parametrize(
    ("state", "denial"),
    [
        pytest.param(ActionExecutionState.SENDING, InvalidationDenial.SEND_IN_FLIGHT, id="sending"),
        pytest.param(ActionExecutionState.SENT, InvalidationDenial.ALREADY_SENT, id="sent"),
        pytest.param(
            ActionExecutionState.SEND_UNKNOWN,
            InvalidationDenial.SEND_OUTCOME_UNKNOWN,
            id="send-unknown",
        ),
    ],
)
async def test_invalidation_refuses_sending_sent_and_unknown(
    send_harness: SendHarness, state: ActionExecutionState, denial: InvalidationDenial
) -> None:
    """Three refusals, three different reasons, and none of them is negotiable.

    A send in flight cannot be taken back by anybody; a sent message cannot be recalled; and an
    ambiguous outcome is a quarantine that only reconciliation resolves.
    """

    from chorus.infrastructure.local.sender import ScriptedSender
    from chorus.ports.sender import SesAccepted, SesUnknown

    await send_harness.prepare()
    await send_harness.approve()
    if state is ActionExecutionState.SENT:
        await send_harness.send(sender=ScriptedSender(default=SesAccepted(message_id="m-1")))
    elif state is ActionExecutionState.SEND_UNKNOWN:
        await send_harness.send(sender=ScriptedSender(default=SesUnknown()))
    else:
        await _strand_in_sending(send_harness)

    execution = await send_harness.execution()
    assert execution.state is state

    with pytest.raises(InvalidationDeniedError) as error:
        await send_harness.invalidate(expected_execution_version=execution.version)
    assert error.value.denial is denial


async def _strand_in_sending(send_harness: SendHarness) -> None:
    """Leave the execution claimed but unfinished, the way a crashed sender would.

    The claim commits and the result write never happens, which is exactly the ``SENDING`` row
    a lost process leaves behind -- and the row a redelivery must never send from.
    """

    from chorus.ports.errors import PersistenceError, PersistenceErrorCode

    # The claim commits and the result write does not. Named rather than positional, because
    # what matters is which transaction is lost -- the claim landing is the whole point.
    send_harness.action.unit_of_work.fail_by_name["send-action-result"] = PersistenceError(
        PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME, "ACTION_EXECUTION"
    )
    with pytest.raises(PersistenceError):
        await send_harness.send()


async def test_an_approval_against_a_foreign_case_is_refused(
    send_harness: SendHarness,
) -> None:
    """The action partition is addressed by ``action_id`` alone, so scope is proved by the row.

    A caller who guessed a real ``action_id`` from another case would otherwise reach that
    case's proposal through this case's URL. The loaded proposal's own ``case_id`` is what
    refuses it -- not the path, which the caller wrote.
    """

    from chorus.domain.ids import CaseId
    from chorus.ports.errors import CrossCaseViolationError

    await send_harness.prepare()
    command = await send_harness.approval_command()
    foreign = replace(command, case_id=CaseId(_OTHER_UUID))

    # Refused at the persistence boundary, which is earlier and stronger than the command's own
    # scope check: every loaded record is revalidated against the scope it was asked for, so a
    # proposal belonging to another case never reaches the decision at all.
    with pytest.raises(CrossCaseViolationError):
        await send_harness.approve_action().execute(foreign)
    execution = await send_harness.execution()
    assert execution.state is ActionExecutionState.DRAFT


async def test_an_approval_against_a_foreign_community_is_refused(
    send_harness: SendHarness,
) -> None:
    """Namespace and community are resolved server-side, and the loaded row is revalidated.

    A body naming another community is refused by the same revalidation, before any decision
    is proved and with nothing written. Community isolation is not a filter applied to a
    result; it is a property every loaded record is checked against.
    """

    from chorus.domain.ids import CommunityId
    from chorus.ports.errors import CrossCaseViolationError

    await send_harness.prepare()
    command = await send_harness.approval_command()
    foreign = replace(command, community_id=CommunityId(_OTHER_UUID))

    with pytest.raises(CrossCaseViolationError):
        await send_harness.approve_action().execute(foreign)
    execution = await send_harness.execution()
    assert execution.state is ActionExecutionState.DRAFT


async def test_an_approval_after_invalidation_is_refused(send_harness: SendHarness) -> None:
    """A cleared proposal is not approvable, and the pointer is what says so.

    Reached here through rejection, which is the ordinary way a pointer becomes
    ``INVALIDATED``. The execution is terminal as well, so this is refused twice over -- and
    both refusals are asserted because either alone would leave the other unexercised.
    """

    await send_harness.prepare()
    await send_harness.reject(idempotency_key="approve-key-reject")

    with pytest.raises(ApprovalDeniedError) as error:
        await send_harness.approve(idempotency_key="approve-key-after")
    assert error.value.denial is ApprovalDenial.POINTER_NOT_DRAFT
    pointer = await send_harness.pointer()
    assert pointer.status is ActionProposalStatus.INVALIDATED
    execution = await send_harness.execution()
    assert execution.state is ActionExecutionState.FAILED


async def test_approver_identity_is_a_hash_and_names_no_contributor(
    send_harness: SendHarness,
) -> None:
    """No contributor is minted to represent the approver (ADR-023 SS 4).

    A contributor is a counted thing: ``corroboration_source_count``, independence grouping,
    and mandate ownership are all defined over contributors. Minting one to satisfy a type
    would put a non-participant into the population that decides whether a case may act at all.
    """

    await send_harness.prepare()
    result = await send_harness.approve()

    approval = await send_harness.action.compile.shareable.load_approval(
        await send_harness.action_scope(), result.approval_id
    )
    assert approval.approver_id_hash.value.startswith("sha256:")
    assert not hasattr(approval, "approver_id")
    case = await send_harness.action.compile.core.load_case(send_harness.action.scope)
    # The approver has not become a source of corroboration for anything.
    assert case.corroboration_source_count >= 2
    assert approval.approver_assurance is ApproverAssurance.DEMO_SHARED_TOKEN
