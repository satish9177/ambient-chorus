"""The send: the order, the one call, and every way it must refuse to make a second.

The number these tests assert on is ``ScriptedSender.call_count``. That is the number the whole
phase is about -- **at most one deliberate SES attempt per approved execution** -- and it cannot
be inferred from a durable state, because a definite failure that reached SES and one that never
did leave the same ``FAILED`` row.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from tests.fixtures.action import FROM_IDENTITY_ID
from tests.fixtures.send import SendHarness, registry_for

from chorus.application.commands.send_action import (
    CLAIM_PARTICIPANTS,
    REPLAY_TABLE,
    RESULT_PARTICIPANTS,
    SendDeniedError,
    SendFailureReason,
    SendReplayOutcome,
)
from chorus.application.services.action_authorization import SEND_FENCE_LIFETIME
from chorus.application.services.action_renderer import render_preview
from chorus.application.services.ses_message import (
    EXECUTION_TAG_NAME,
    execution_tag_value,
    ses_request_token_hash,
)
from chorus.domain.entities import ActionExecutionState
from chorus.infrastructure.local.sender import ScriptedSender
from chorus.ports.errors import PersistenceConflictError, PersistenceError, PersistenceErrorCode
from chorus.ports.sender import (
    SendFailureCode,
    SendUnknownReason,
    SesAccepted,
    SesDefiniteFailure,
    SesUnknown,
)
from chorus.ports.storage import CheckItem, PutItem
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------------------
# The frozen order
# ---------------------------------------------------------------------------------------


async def test_render_precedes_claim_so_sending_can_carry_its_required_hashes(
    send_harness: SendHarness,
) -> None:
    """The presence table makes both hashes required at ``SENDING``, so they exist by then.

    The frozen pipeline ran *claim, fence, render, send*, which could not be executed against
    the entity the same freeze created: the first step had to write two values the third and
    fourth had not produced yet. Rendering is a pure function of immutable inputs, so moving it
    earlier costs nothing -- and it is what makes the approved-equals-sent comparison happen
    before anything is consumed.
    """

    await send_harness.prepare()
    await send_harness.approve()
    proposal = await send_harness.proposal()
    sender = ScriptedSender(default=SesAccepted(message_id="ses-1"))

    await send_harness.send(sender=sender)

    claim = send_harness.action.unit_of_work.plan("send-action-claim")
    assert len(claim.operations) == CLAIM_PARTICIPANTS == 3
    execution = await send_harness.execution()
    assert execution.rendered_message_hash == proposal.preview_hash
    assert execution.ses_request_token_hash is not None
    assert execution.started_at is not None


async def test_the_rendered_hash_equals_the_approved_preview_hash(
    send_harness: SendHarness,
) -> None:
    """Approved bytes are sent bytes, and the comparison is between two owners' digests.

    ``preview_hash`` was sealed by the proposal a human approved; ``rendered_message_hash`` is
    produced by the sender at send time. A correct system produces the same digest twice, which
    is what makes this a comparison rather than a tautology.
    """

    view = await send_harness.prepare()
    await send_harness.approve()
    proposal = await send_harness.proposal()
    sender = ScriptedSender(default=SesAccepted(message_id="ses-1"))

    await send_harness.send(sender=sender)

    regenerated = render_preview(proposal, view, from_identity_id=FROM_IDENTITY_ID)
    assert regenerated.preview_hash == proposal.preview_hash
    assert sender.calls[0].subject == regenerated.document.subject
    assert sender.calls[0].text_body == regenerated.text_body
    assert sender.calls[0].html_body == regenerated.html_body


async def test_rendered_hash_mismatch_fails_before_ses(send_harness: SendHarness) -> None:
    """No claim, no fence, no SES call.

    The mismatch is driven by moving ``from_identity_id``, which is inside ``preview_hash`` --
    so this is simultaneously the sender-identity-change row and the digest-mismatch row, and
    that is the point: an approval of one letterhead that could be sent under another would be
    an approval of the words and not of the correspondence.
    """

    await send_harness.prepare()
    await send_harness.approve()
    sender = ScriptedSender(default=SesAccepted(message_id="never"))

    result = await send_harness.send_action(
        sender=sender, from_identity_id="chorus-some-other-sender"
    ).execute(await send_harness.send_command())

    assert result.state is ActionExecutionState.FAILED
    assert SendFailureReason.RENDERED_HASH_MISMATCH.value in result.reason_codes
    assert result.ses_call_made is False
    assert sender.call_count == 0
    execution = await send_harness.execution()
    # Nothing was consumed: no rendered hash, no SES token, no claim.
    assert execution.rendered_message_hash is None
    assert execution.ses_request_token_hash is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        pytest.param(
            {"registry_version": 99},
            SendFailureReason.DESTINATION_REGISTRY_CHANGED,
            id="registry-version",
        ),
        pytest.param(
            {"display_label": "Somebody Else"},
            SendFailureReason.DESTINATION_REGISTRY_CHANGED,
            id="display-label",
        ),
    ],
)
async def test_a_destination_registry_change_after_approval_fails_before_ses(
    send_harness: SendHarness, overrides: dict[str, object], reason: SendFailureReason
) -> None:
    """Each late change gets its *specific* cause code, not the generic digest mismatch.

    Step 4 would catch every one of these structurally, because all of them are inside the
    preview digest. They are checked separately as well so the recorded ``failure_code`` names
    the repair an operator actually has to make.
    """

    await send_harness.prepare()
    await send_harness.approve()
    moved = send_harness.rotated_destination(**overrides)
    sender = ScriptedSender(default=SesAccepted(message_id="never"))

    result = await send_harness.send_action(
        sender=sender,
        destination=moved,
        registry=registry_for(moved, address="property-manager@chorus.invalid"),
    ).execute(await send_harness.send_command())

    assert result.state is ActionExecutionState.FAILED
    assert reason.value in result.reason_codes
    assert sender.call_count == 0


async def test_a_routing_token_change_after_approval_fails_before_ses(
    send_harness: SendHarness,
) -> None:
    """The routing token is one third of the triple the preview digest binds."""

    from uuid import UUID

    await send_harness.prepare()
    await send_harness.approve()
    moved = send_harness.rotated_destination(
        routing_token=UUID("99999999-9999-4999-8999-999999999999")
    )
    sender = ScriptedSender(default=SesAccepted(message_id="never"))

    result = await send_harness.send_action(
        sender=sender,
        destination=moved,
        registry=registry_for(moved, address="property-manager@chorus.invalid"),
    ).execute(await send_harness.send_command())

    assert result.state is ActionExecutionState.FAILED
    assert SendFailureReason.ROUTING_TOKEN_CHANGED.value in result.reason_codes
    assert sender.call_count == 0


async def test_an_expired_view_fails_before_ses(send_harness: SendHarness) -> None:
    """Equality at expiry means expired, and this is checked before anything is claimed."""

    view = await send_harness.prepare()
    await send_harness.approve()
    command = await send_harness.send_command()
    send_harness.action.compile.clock.instant = view.expires_at
    sender = ScriptedSender(default=SesAccepted(message_id="never"))

    result = await send_harness.send_action(sender=sender).execute(command)

    assert result.state is ActionExecutionState.FAILED
    assert SendFailureReason.VIEW_EXPIRED.value in result.reason_codes
    assert sender.call_count == 0


async def test_an_expired_approval_fails_before_ses(send_harness: SendHarness) -> None:
    """An approval never outlives the disclosure authority it was made against."""

    await send_harness.prepare()
    approval = await send_harness.approve()
    command = await send_harness.send_command()
    send_harness.action.compile.clock.instant = approval.expires_at
    sender = ScriptedSender(default=SesAccepted(message_id="never"))

    result = await send_harness.send_action(sender=sender).execute(command)

    assert result.state is ActionExecutionState.FAILED
    assert SendFailureReason.APPROVAL_EXPIRED.value in result.reason_codes
    assert sender.call_count == 0


async def test_a_superseded_current_action_fails_before_ses(send_harness: SendHarness) -> None:
    """The pointer decides which proposal is current; an execution it no longer names cannot go."""

    await send_harness.prepare()
    await send_harness.approve()
    command = await send_harness.send_command()
    # The human withdraws, which moves the pointer to INVALIDATED and the execution to FAILED.
    execution = await send_harness.execution()
    await send_harness.invalidate(expected_execution_version=execution.version)
    sender = ScriptedSender(default=SesAccepted(message_id="never"))

    with pytest.raises(SendDeniedError) as error:
        await send_harness.send_action(sender=sender).execute(command)

    assert error.value.outcome is SendReplayOutcome.TERMINAL_FAILED
    assert sender.call_count == 0


# ---------------------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------------------


async def test_the_ses_payload_is_simple_one_recipient_and_carries_the_execution_tag(
    send_harness: SendHarness,
) -> None:
    """Exactly one recipient, exactly one reply-to, UTF-8, and the recomputable tag.

    The tag is bare lowercase hex with no ``sha256:`` prefix, because SES email-tag values admit
    only ``[A-Za-z0-9_-]`` -- a detail worth freezing rather than discovering at the first live
    send. It is recomputed here from durable values, which is exactly what reconciliation does.
    """

    await send_harness.prepare()
    await send_harness.approve()
    pointer = await send_harness.pointer()
    sender = ScriptedSender(default=SesAccepted(message_id="ses-1"))

    await send_harness.send(sender=sender)

    request = sender.calls[0]
    assert len(request.to_addresses) == 1
    assert len(request.reply_to_addresses) == 1
    assert request.charset == "UTF-8"
    assert [tag.name for tag in request.email_tags] == [EXECUTION_TAG_NAME]
    expected = execution_tag_value(
        namespace=send_harness.action.compile.case.namespace,
        execution_id=pointer.execution_id,
    )
    assert request.email_tags[0].value == expected
    assert ":" not in request.email_tags[0].value
    assert len(request.email_tags[0].value) == 64


async def test_the_request_token_is_recomputable_from_durable_values(
    send_harness: SendHarness,
) -> None:
    """A recovery path recomputes it without having stored it."""

    await send_harness.prepare()
    await send_harness.approve()
    pointer = await send_harness.pointer()
    await send_harness.send(sender=ScriptedSender(default=SesAccepted(message_id="ses-1")))

    execution = await send_harness.execution()
    assert execution.idempotency_key is not None
    assert execution.ses_request_token_hash == ses_request_token_hash(
        namespace=send_harness.action.compile.case.namespace,
        action_id=pointer.action_id,
        execution_id=pointer.execution_id,
        idempotency_key=execution.idempotency_key,
    )


async def test_no_address_reaches_the_execution_row_or_the_audit_trail(
    send_harness: SendHarness,
) -> None:
    """No artifact a model, a human, an audit row, or a log line can see holds an address."""

    await send_harness.prepare()
    await send_harness.approve()
    sender = ScriptedSender(default=SesAccepted(message_id="ses-1"))
    await send_harness.send(sender=sender)

    execution = await send_harness.execution()
    rendered = repr(execution)
    for address in (
        sender.calls[0].to_addresses[0],
        sender.calls[0].from_email_address,
        sender.calls[0].reply_to_addresses[0],
    ):
        assert address not in rendered
    plan = send_harness.action.unit_of_work.plan("send-action-result")
    audit = [item for item in plan.operations if item.key.table.value == "AUDIT"]
    assert audit
    for operation in audit:
        text = repr(operation)
        assert "@" not in text.replace("@chorus", "")
        assert sender.calls[0].subject not in text


# ---------------------------------------------------------------------------------------
# The classification table
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "state", "code"),
    [
        pytest.param(
            SesAccepted(message_id="ses-ok"), ActionExecutionState.SENT, None, id="accepted"
        ),
        pytest.param(
            SesDefiniteFailure(failure_code=SendFailureCode.SES_REJECTED),
            ActionExecutionState.FAILED,
            SendFailureCode.SES_REJECTED.value,
            id="rejected",
        ),
        pytest.param(
            SesDefiniteFailure(failure_code=SendFailureCode.SES_DEFINITE_FAILURE),
            ActionExecutionState.FAILED,
            SendFailureCode.SES_DEFINITE_FAILURE.value,
            id="definite-failure",
        ),
        pytest.param(
            SesDefiniteFailure(failure_code=SendFailureCode.SES_UNREACHABLE),
            ActionExecutionState.FAILED,
            SendFailureCode.SES_UNREACHABLE.value,
            id="unreachable",
        ),
        pytest.param(
            SesUnknown(reason_code=SendUnknownReason.SES_TIMEOUT),
            ActionExecutionState.SEND_UNKNOWN,
            None,
            id="timeout",
        ),
    ],
)
async def test_every_classified_outcome_persists_its_frozen_state(
    send_harness: SendHarness,
    outcome: object,
    state: ActionExecutionState,
    code: str | None,
) -> None:
    """One shape, three terminal forms, and exactly one SES call in every branch."""

    await send_harness.prepare()
    await send_harness.approve()
    sender = ScriptedSender(default=outcome)  # type: ignore[arg-type]

    result = await send_harness.send(sender=sender)

    assert result.state is state
    assert result.failure_code == code
    assert sender.call_count == 1
    plan = send_harness.action.unit_of_work.plan("send-action-result")
    assert len(plan.operations) == RESULT_PARTICIPANTS == 3


async def test_an_exception_escaping_the_adapter_classifies_as_send_unknown(
    send_harness: SendHarness,
) -> None:
    """The fail-safe default, exercised where an adapter breaks its own contract.

    The port's contract is to classify rather than raise. This is the second layer: an
    exception class nobody anticipated must land on the safe side by construction rather than
    by somebody remembering to add it.
    """

    await send_harness.prepare()
    await send_harness.approve()

    class NobodyAnticipatedThis(Exception):
        pass

    sender = ScriptedSender()
    sender.raises.append(NobodyAnticipatedThis("a class this file has never heard of"))

    result = await send_harness.send(sender=sender)

    assert result.state is ActionExecutionState.SEND_UNKNOWN
    assert result.reason_codes == (SendUnknownReason.SES_TRANSPORT_AMBIGUOUS.value,)
    assert sender.call_count == 1


# ---------------------------------------------------------------------------------------
# The one-attempt boundary
# ---------------------------------------------------------------------------------------


async def test_claim_cas_admits_exactly_one_of_two_workers(send_harness: SendHarness) -> None:
    """Two concurrent send workers resolve to **one** SES call.

    The boundary is the conditional ``APPROVED@v -> SENDING@v+1`` write and nothing else. The
    fence is per case and admits a replay by the same ``execution_id``, so it cannot and does
    not prevent this -- and "the fence" is the intuitive wrong answer an implementer reaches
    for.
    """

    await send_harness.prepare()
    await send_harness.approve()
    sender = ScriptedSender(default=SesAccepted(message_id="ses-1"))
    first = await send_harness.send_command(idempotency_key="send-key-a")
    second = await send_harness.send_command(idempotency_key="send-key-b")

    outcomes = await asyncio.gather(
        send_harness.send_action(sender=sender).execute(first),
        send_harness.send_action(sender=sender).execute(second),
        return_exceptions=True,
    )

    assert sender.call_count == 1
    # Both may *report* SENT: the loser reads the durable row and replays its message
    # reference, which is exactly what the frozen replay table says a delivery finding ``SENT``
    # must do. What must be one is the number of deliveries that actually called SES.
    called = [
        item for item in outcomes if not isinstance(item, BaseException) and item.ses_call_made
    ]
    assert len(called) == 1
    assert all(
        isinstance(item, BaseException) or item.state is ActionExecutionState.SENT
        for item in outcomes
    )


async def test_no_ses_call_is_made_from_sending_on_redelivery(
    send_harness: SendHarness,
) -> None:
    """A redelivery reading ``SENDING`` returns 202-in-progress and calls nothing.

    The replay table is consulted before anything is rendered, resolved, or claimed, so this
    branch cannot reach SES by any path at all.
    """

    await send_harness.prepare()
    await send_harness.approve()
    send_harness.action.unit_of_work.fail_by_name["send-action-result"] = PersistenceError(
        PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME, "ACTION_EXECUTION"
    )
    first = ScriptedSender(default=SesAccepted(message_id="ses-1"))
    with pytest.raises(PersistenceError):
        await send_harness.send(sender=first)
    assert first.call_count == 1

    second = ScriptedSender(default=SesAccepted(message_id="ses-2"))
    result = await send_harness.send(sender=second)

    assert second.call_count == 0
    assert result.state is ActionExecutionState.SENDING
    assert result.reason_codes == (SendReplayOutcome.IN_PROGRESS.value,)


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        pytest.param(ActionExecutionState.DRAFT, SendReplayOutcome.CONFLICT, id="draft"),
        pytest.param(ActionExecutionState.FAILED, SendReplayOutcome.TERMINAL_FAILED, id="failed"),
        pytest.param(
            ActionExecutionState.SEND_UNKNOWN, SendReplayOutcome.QUARANTINED, id="unknown"
        ),
    ],
)
def test_the_replay_table_admits_exactly_one_state(
    state: ActionExecutionState, outcome: SendReplayOutcome
) -> None:
    """Exactly one entry is ``PROCEED``, and a state added to the enum has no default."""

    assert REPLAY_TABLE[state] is outcome
    proceeding = [key for key, value in REPLAY_TABLE.items() if value is SendReplayOutcome.PROCEED]
    assert proceeding == [ActionExecutionState.APPROVED]
    assert set(REPLAY_TABLE) == set(ActionExecutionState)


async def test_a_replay_after_sent_returns_the_same_message_reference(
    send_harness: SendHarness,
) -> None:
    """``SENT`` is terminal and has no edge out. A second delivery calls nothing."""

    await send_harness.prepare()
    await send_harness.approve()
    await send_harness.send(sender=ScriptedSender(default=SesAccepted(message_id="ses-1")))

    replay = ScriptedSender(default=SesAccepted(message_id="ses-2"))
    result = await send_harness.send(sender=replay)

    assert replay.call_count == 0
    assert result.state is ActionExecutionState.SENT
    assert result.ses_message_id == "ses-1"


async def test_a_retry_after_failed_is_refused_and_calls_nothing(
    send_harness: SendHarness,
) -> None:
    """``FAILED`` is terminal for the action; a fresh proposal and approval are required."""

    await send_harness.prepare()
    await send_harness.approve()
    await send_harness.send(
        sender=ScriptedSender(default=SesDefiniteFailure(failure_code=SendFailureCode.SES_REJECTED))
    )

    retry = ScriptedSender(default=SesAccepted(message_id="never"))
    with pytest.raises(SendDeniedError) as error:
        await send_harness.send(sender=retry)

    assert error.value.outcome is SendReplayOutcome.TERMINAL_FAILED
    assert retry.call_count == 0


async def test_a_retry_after_send_unknown_is_refused_and_calls_nothing(
    send_harness: SendHarness,
) -> None:
    """The quarantine. **No retry route exists**, and its absence is a design element."""

    await send_harness.prepare()
    await send_harness.approve()
    await send_harness.send(sender=ScriptedSender(default=SesUnknown()))

    retry = ScriptedSender(default=SesAccepted(message_id="never"))
    with pytest.raises(SendDeniedError) as error:
        await send_harness.send(sender=retry)

    assert error.value.outcome is SendReplayOutcome.QUARANTINED
    assert retry.call_count == 0


async def test_a_crash_before_ses_leaves_the_execution_claimable_or_claimed_never_sent(
    send_harness: SendHarness,
) -> None:
    """A crash between the claim and the call leaves ``SENDING`` and no message.

    The row cannot distinguish this from a call whose response was lost, and pretending
    otherwise would require a marker written non-atomically with an external call -- which
    moves the ambiguity one step earlier rather than removing it.
    """

    await send_harness.prepare()
    await send_harness.approve()
    sender = ScriptedSender(default=SesAccepted(message_id="never"))
    sender.raises.append(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        await send_harness.send(sender=sender)

    execution = await send_harness.execution()
    assert execution.state is ActionExecutionState.SENDING


# ---------------------------------------------------------------------------------------
# The fence
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(SesAccepted(message_id="ses-1"), id="sent"),
        pytest.param(SesDefiniteFailure(failure_code=SendFailureCode.SES_REJECTED), id="failed"),
        pytest.param(SesUnknown(), id="send-unknown"),
    ],
)
async def test_the_fence_is_released_on_every_terminal_outcome(
    send_harness: SendHarness, outcome: object
) -> None:
    """Including the ambiguous one, and that is the load-bearing choice.

    A fence retained to mark "something happened here" would permanently refuse every future
    mandate decision and revocation on this case, which inverts the guarantee the fence exists
    to provide: it is a sixty-second ordering window for contributors' authority, not a lien on
    it.
    """

    await send_harness.prepare()
    await send_harness.approve()

    await send_harness.send(sender=ScriptedSender(default=outcome))  # type: ignore[arg-type]

    fence = await send_harness.action.compile.core.load_send_fence(send_harness.action.scope)
    assert fence is None


async def test_the_fence_expiry_is_bounded_by_the_view_and_the_approval(
    send_harness: SendHarness,
) -> None:
    """``min(now + 60s, view.expires_at, approval.expires_at, earliest mandate expiry)``."""

    from chorus.application.services.send_authorization import SendAuthorizationGranted

    view = await send_harness.prepare()
    approval = await send_harness.approve()
    proposal = await send_harness.proposal()
    pointer = await send_harness.pointer()
    execution = await send_harness.execution()
    assert execution.approval_id is not None

    from chorus.application.services.send_authorization import SendAuthorizationRequest

    now = send_harness.action.compile.clock.now()
    granted = await send_harness.authorization().authorize(
        SendAuthorizationRequest(
            namespace=send_harness.action.compile.case.namespace,
            community_id=send_harness.action.compile.case.community_id,
            case_id=send_harness.action.case_id,
            action_id=pointer.action_id,
            execution_id=pointer.execution_id,
            approval_id=execution.approval_id,
            proposal_hash=proposal.proposal_hash,
            view_id=view.view_id,
            view_hash=view.view_hash,
            authorization_version=view.authorization_version,
            policy_version=view.policy_version,
            compiler_version=view.compiler_version,
            policy_build_hash=view.policy_build_hash,
            destination_id=view.destination.destination_id,
            destination_registry_version=view.destination.registry_version,
            routing_token=view.destination.routing_token,
            purpose=view.purpose,
            authorization_snapshot_hash=view.authorization_snapshot_hash,
            requested_at=now,
        )
    )

    assert isinstance(granted, SendAuthorizationGranted)
    assert granted.fence.expires_at <= now + SEND_FENCE_LIFETIME
    assert granted.fence.expires_at <= view.expires_at
    assert granted.fence.expires_at <= approval.expires_at


async def test_send_unknown_releases_the_fence_and_revocation_proceeds(
    send_harness: SendHarness,
) -> None:
    """An ambiguous send never becomes a lien on a contributor's consent.

    The no-live-fence condition every mandate decision stages is what would otherwise refuse
    forever, so this asserts the condition itself passes rather than merely that the row is
    gone.
    """

    await send_harness.prepare()
    await send_harness.approve()
    await send_harness.send(sender=ScriptedSender(default=SesUnknown()))

    # The participant a mandate decision, an investigation apply, and a compile all stage.
    now = send_harness.action.compile.clock.now() + timedelta(seconds=1)
    guard = send_harness.action.compile.core.stage_require_no_live_send_fence(
        send_harness.action.scope, now=now
    )
    await send_harness.action.compile.unit_of_work.commit(
        _plan_with(guard, name="revocation-guard")
    )


def _plan_with(operation: CheckItem | PutItem, *, name: str) -> TransactionPlan:
    """Stage one participant on its own, so it is what the transaction is testing."""

    return TransactionPlan(name=name, operations=(operation,), audit_required=False)


async def test_withdrawal_and_send_claim_race_has_exactly_one_winner(
    send_harness: SendHarness,
) -> None:
    """Two conditional writes to one row; exactly one commits.

    There is deliberately no attempt to make the human always win -- that would require holding
    a lock across an external call. What matters is that there is no window in which both
    believe they did.
    """

    await send_harness.prepare()
    await send_harness.approve()
    execution = await send_harness.execution()
    sender = ScriptedSender(default=SesAccepted(message_id="ses-1"))

    outcomes = await asyncio.gather(
        send_harness.send_action(sender=sender).execute(await send_harness.send_command()),
        send_harness.invalidate(expected_execution_version=execution.version),
        return_exceptions=True,
    )

    succeeded = [item for item in outcomes if not isinstance(item, BaseException)]
    assert len(succeeded) == 1
    final = await send_harness.execution()
    if sender.call_count == 1:
        assert final.state is ActionExecutionState.SENT
    else:
        assert final.state is ActionExecutionState.FAILED
    assert sender.call_count <= 1


async def test_send_fence_denies_after_authorization_version_moves_post_approval(
    send_harness: SendHarness,
) -> None:
    """The revocation-after-approval race, end to end.

    The old approval does not authorize the send, and it does not because the approval is not
    consulted for authority at fence time -- it is consulted for integrity. Authority is
    re-derived from live state every time.
    """

    from chorus.domain.state import bump_case_authorization

    await send_harness.prepare()
    await send_harness.approve()
    command = await send_harness.send_command()

    case = await send_harness.action.compile.core.load_case(send_harness.action.scope)
    bumped = bump_case_authorization(
        case,
        expected_version=case.version,
        reason_code="MANDATE_REVOKED",
        now=send_harness.action.compile.clock.now(),
    )
    await send_harness.action.compile.unit_of_work.commit(
        _plan_with(
            send_harness.action.compile.core.stage_update_case(
                send_harness.action.scope, bumped, expected_version=case.version
            ),
            name="revoke-authority",
        )
    )

    sender = ScriptedSender(default=SesAccepted(message_id="never"))
    result = await send_harness.send_action(sender=sender).execute(command)

    assert result.state is ActionExecutionState.FAILED
    assert SendFailureReason.STALE_AUTHORIZATION.value in result.reason_codes
    assert "AUTHORIZATION_VERSION_MOVED" in result.reason_codes
    assert sender.call_count == 0


async def test_the_send_never_requires_the_case_occ_version_to_match(
    send_harness: SendHarness,
) -> None:
    """``CommunityCase.version`` moved on purpose, and requiring it would fail every first send.

    The ``READY_FOR_ACTION -> ACTION_PROPOSED`` edge that created this proposal advanced the OCC
    version, so a send that compared it against the proposal's recorded ``case_version`` could
    never succeed. ADR-020 removed that deadlock and this asserts it was not reintroduced.
    """

    await send_harness.prepare()
    await send_harness.approve()
    proposal = await send_harness.proposal()
    case = await send_harness.action.compile.core.load_case(send_harness.action.scope)
    assert case.version != proposal.case_version

    result = await send_harness.send(sender=ScriptedSender(default=SesAccepted(message_id="ses-1")))

    assert result.state is ActionExecutionState.SENT


async def test_a_definite_pre_send_failure_never_holds_a_fence(
    send_harness: SendHarness,
) -> None:
    """Nothing is claimed and no fence is taken, so there is nothing to release."""

    await send_harness.prepare()
    await send_harness.approve()
    sender = ScriptedSender(default=SesAccepted(message_id="never"))

    await send_harness.send_action(
        sender=sender, from_identity_id="chorus-some-other-sender"
    ).execute(await send_harness.send_command())

    fence = await send_harness.action.compile.core.load_send_fence(send_harness.action.scope)
    assert fence is None
    assert sender.call_count == 0


async def test_a_send_claimed_by_one_worker_conflicts_for_a_second(
    send_harness: SendHarness,
) -> None:
    """A worker whose reads passed still loses the compare-and-swap, and never calls SES."""

    await send_harness.prepare()
    await send_harness.approve()
    loser = ScriptedSender(default=SesAccepted(message_id="never"))
    winner = ScriptedSender(default=SesAccepted(message_id="ses-1"))
    command = await send_harness.send_command(idempotency_key="send-key-loser")

    async def somebody_else_claims_first() -> None:
        await send_harness.send_action(sender=winner).execute(
            await send_harness.send_command(idempotency_key="send-key-winner")
        )

    send_harness.action.unit_of_work.before_commit.append(somebody_else_claims_first)

    with pytest.raises(PersistenceConflictError):
        await send_harness.send_action(sender=loser).execute(command)

    assert loser.call_count == 0
    assert winner.call_count == 1


async def test_the_send_proof_records_key_on_the_attempt_not_the_client_key(
    send_harness: SendHarness,
) -> None:
    """Domains 4, 5, and 6 are keyed on the execution's own send key (ADR-025 SS 12).

    That is what makes them replay-safe regardless of how the worker was invoked or how many
    times. Keying them on the client key would be a real defect: two workers arriving under
    different ``Idempotency-Key`` values for one execution would write two *different* result
    records for one attempt, and each would look like proof that its own outcome had been
    persisted.

    Asserted on the staged plan's actual item key, because that is the address a recovery path
    reads -- the constant it was derived from could be self-consistently wrong.
    """

    from chorus.application.services.action_authorization import (
        send_claim_key_hash,
        send_result_key_hash,
    )
    from chorus.infrastructure.dynamodb import keys

    await send_harness.prepare()
    await send_harness.approve()
    execution = await send_harness.execution()
    pointer = await send_harness.pointer()
    assert execution.idempotency_key is not None

    await send_harness.send_action(
        sender=ScriptedSender(default=SesAccepted(message_id="ses-1"))
    ).execute(await send_harness.send_command(idempotency_key="a-client-key-nobody-else-uses"))

    namespace = send_harness.action.compile.case.namespace
    expected_partition = keys.execution_partition(namespace, pointer.action_id)
    for name, domain in (
        ("send-action-claim", send_claim_key_hash),
        ("send-action-result", send_result_key_hash),
    ):
        plan = send_harness.action.unit_of_work.plan(name)
        record = plan.operations[2]
        assert record.key.partition_key == expected_partition
        assert domain(execution.idempotency_key).value in record.key.sort_key
        assert "a-client-key-nobody-else-uses" not in record.key.sort_key


async def test_send_transaction_participant_counts_are_three_three_and_four(
    send_harness: SendHarness,
) -> None:
    """Claim, outcome, and case projection, asserted arithmetically against the staged plans.

    The counts are read off the plans the commands actually built, not off the constants they
    are compared against -- an assertion against the constant alone would only prove each
    module is self-consistent with itself.

    None of these is ten. The proposal apply needed ten because it commits an artifact, an
    execution, two pointers, an invocation record, a case transition, and two guards at once; a
    send outcome commits one row and its proof.
    """

    from uuid import uuid4

    from chorus.application.commands.project_action_outcome import (
        PROJECTION_PARTICIPANTS,
        ProjectActionOutcome,
        ProjectActionOutcomeCommand,
    )
    from chorus.domain.ids import Sha256Digest

    await send_harness.prepare()
    await send_harness.approve()
    pointer = await send_harness.pointer()
    await send_harness.send(sender=ScriptedSender(default=SesAccepted(message_id="ses-1")))
    await send_harness.project().execute(
        ProjectActionOutcomeCommand(
            namespace=send_harness.action.compile.case.namespace,
            community_id=send_harness.action.compile.case.community_id,
            case_id=send_harness.action.case_id,
            action_id=pointer.action_id,
            execution_id=pointer.execution_id,
            actor_id_hash=Sha256Digest("sha256:" + "f" * 64),
            correlation_id=uuid4(),
        )
    )

    plans = send_harness.action.unit_of_work
    assert len(plans.plan("send-action-claim").operations) == CLAIM_PARTICIPANTS == 3
    assert len(plans.plan("send-action-result").operations) == RESULT_PARTICIPANTS == 3
    # Five since ADR-026 § 3 amended ADR-025 § 11: the projection also writes the immutable
    # outbound message locator, which is the only channel from a send back to a reply.
    assert len(plans.plan("project-action-outcome").operations) == PROJECTION_PARTICIPANTS == 5
    assert isinstance(ProjectActionOutcome, type)
