"""Recovery, reconciliation, and the case projection: proof, or nothing moves.

Every branch here obeys three rules without exception: verify immutable artifacts and their
hashes first; never invoke the Action model; and **never repeat an SES call whose previous
outcome is unknown**. The last one is asserted on the sender's own call count in each test that
can reach a send at all, because it is the property the phase is about.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.fixtures.send import SendHarness

from chorus.application.commands.project_action_outcome import (
    PROJECTION_PARTICIPANTS,
    ProjectActionOutcomeCommand,
)
from chorus.application.commands.reconcile_send_outcome import (
    QUARANTINE_PARTICIPANTS,
    TERMINAL_PARTICIPANTS,
    ReconcileSendOutcomeCommand,
    ReconciliationReason,
    ReconciliationRefusal,
    ReconciliationRefusedError,
)
from chorus.application.services.action_authorization import SEND_RECOVERY_WINDOW
from chorus.domain.entities import ActionExecutionState, CaseState
from chorus.domain.errors import IntegrityError
from chorus.infrastructure.local.sender import ScriptedSender
from chorus.ports.errors import PersistenceError, PersistenceErrorCode
from chorus.ports.sender import SendUnknownReason, SesAccepted, SesUnknown
from chorus.ports.storage import CheckItem

pytestmark = pytest.mark.anyio


async def _reconcile_command(
    send_harness: SendHarness, **overrides: object
) -> ReconcileSendOutcomeCommand:
    pointer = await send_harness.pointer()
    from uuid import uuid4

    from chorus.domain.ids import Sha256Digest

    defaults: dict[str, object] = {
        "namespace": send_harness.action.compile.case.namespace,
        "community_id": send_harness.action.compile.case.community_id,
        "case_id": send_harness.action.case_id,
        "action_id": pointer.action_id,
        "execution_id": pointer.execution_id,
        "actor_id_hash": Sha256Digest("sha256:" + "d" * 64),
        "correlation_id": uuid4(),
    }
    defaults.update(overrides)
    return ReconcileSendOutcomeCommand(**defaults)  # type: ignore[arg-type]


async def _strand_in_sending(send_harness: SendHarness) -> ScriptedSender:
    """Leave the row claimed and the result write lost, exactly as a crashed sender would."""

    sender = ScriptedSender(default=SesAccepted(message_id="ses-lost"))
    send_harness.action.unit_of_work.fail_by_name["send-action-result"] = PersistenceError(
        PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME, "ACTION_EXECUTION"
    )
    with pytest.raises(PersistenceError):
        await send_harness.send(sender=sender)
    return sender


# ---------------------------------------------------------------------------------------
# SENDING -> SEND_UNKNOWN
# ---------------------------------------------------------------------------------------


async def test_a_sending_row_is_not_reconcilable_inside_the_recovery_window(
    send_harness: SendHarness,
) -> None:
    """Elapsed time is not evidence about a transaction, and neither is impatience.

    Inside the window another process may still be working, and quarantining underneath it
    would record an outcome about an attempt that has not finished.
    """

    await send_harness.prepare()
    await send_harness.approve()
    await _strand_in_sending(send_harness)

    with pytest.raises(ReconciliationRefusedError) as error:
        await send_harness.reconcile().execute(await _reconcile_command(send_harness))
    assert error.value.refusal is ReconciliationRefusal.RECOVERY_WINDOW_OPEN


async def test_a_sending_row_past_the_window_is_quarantined_and_calls_nothing(
    send_harness: SendHarness,
) -> None:
    """``SENDING -> SEND_UNKNOWN``, three participants, and zero SES calls.

    This branch *is* the send result, persisted late by whoever found the row abandoned, so it
    writes the send-result proof the lost transaction would have written.
    """

    await send_harness.prepare()
    await send_harness.approve()
    sender = await _strand_in_sending(send_harness)
    assert sender.call_count == 1
    send_harness.advance(SEND_RECOVERY_WINDOW + timedelta(seconds=1))

    result = await send_harness.reconcile().execute(await _reconcile_command(send_harness))

    assert result.state is ActionExecutionState.SEND_UNKNOWN
    assert result.reason_code == ReconciliationReason.RECONCILED_UNKNOWN.value
    assert sender.call_count == 1
    plan = send_harness.action.unit_of_work.plan("reconcile-send-outcome")
    assert len(plan.operations) == QUARANTINE_PARTICIPANTS == 3


# ---------------------------------------------------------------------------------------
# SEND_UNKNOWN -> SENT / FAILED, on proof only
# ---------------------------------------------------------------------------------------


async def test_send_unknown_resolves_to_sent_on_a_matching_configuration_set_event(
    send_harness: SendHarness,
) -> None:
    """All three of set, tag, and message ID, because any two are satisfiable by an impostor."""

    await send_harness.prepare()
    await send_harness.approve()
    pointer = await send_harness.pointer()
    await send_harness.send(sender=ScriptedSender(default=SesUnknown()))

    result = await send_harness.reconcile().execute(
        await _reconcile_command(
            send_harness,
            evidence=await send_harness.attest(
                execution_tag=send_harness.execution_tag(pointer.execution_id),
                message_id="ses-proven",
            ),
        )
    )

    assert result.state is ActionExecutionState.SENT
    assert result.ses_message_id == "ses-proven"
    plan = send_harness.action.unit_of_work.plan("reconcile-send-outcome")
    assert len(plan.operations) == TERMINAL_PARTICIPANTS == 2


async def test_an_event_from_a_foreign_configuration_set_is_refused(
    send_harness: SendHarness,
) -> None:
    """Proof has to come from *this* deployment's configuration set."""

    await send_harness.prepare()
    await send_harness.approve()
    pointer = await send_harness.pointer()
    await send_harness.send(sender=ScriptedSender(default=SesUnknown()))

    with pytest.raises(ReconciliationRefusedError) as error:
        await send_harness.reconcile().execute(
            await _reconcile_command(
                send_harness,
                evidence=await send_harness.attest(
                    configuration_set="somebody-elses-set",
                    execution_tag=send_harness.execution_tag(pointer.execution_id),
                    message_id="ses-forged",
                ),
            )
        )
    assert error.value.refusal is ReconciliationRefusal.FOREIGN_CONFIGURATION_SET
    execution = await send_harness.execution()
    assert execution.state is ActionExecutionState.SEND_UNKNOWN


async def test_an_event_whose_tag_names_another_execution_is_refused(
    send_harness: SendHarness,
) -> None:
    """The tag is recomputed for *this exact* execution and compared, never trusted."""

    await send_harness.prepare()
    await send_harness.approve()
    await send_harness.send(sender=ScriptedSender(default=SesUnknown()))

    with pytest.raises(ReconciliationRefusedError) as error:
        await send_harness.reconcile().execute(
            await _reconcile_command(
                send_harness,
                evidence=await send_harness.attest(execution_tag="0" * 64, message_id="ses-forged"),
            )
        )
    assert error.value.refusal is ReconciliationRefusal.TAG_MISMATCH


async def test_reconciliation_rejects_a_disagreeing_message_id(
    send_harness: SendHarness,
) -> None:
    """A forged or tampered identifier is at worst a *rejected* reconciliation (T35).

    Monotonic presence forbids rewriting ``ses_message_id`` once it is set, so the disagreement
    is refused before anything is staged rather than silently replacing a recorded outcome.
    """

    await send_harness.prepare()
    await send_harness.approve()
    pointer = await send_harness.pointer()
    await send_harness.send(sender=ScriptedSender(default=SesUnknown(reason_code=_ambiguous())))
    tag = send_harness.execution_tag(pointer.execution_id)
    # First, a legitimate reconciliation records an identifier.
    await send_harness.reconcile().execute(
        await _reconcile_command(
            send_harness,
            evidence=await send_harness.attest(execution_tag=tag, message_id="ses-real"),
        )
    )
    execution = await send_harness.execution()
    assert execution.ses_message_id == "ses-real"

    # A second event claiming a different identifier for the same execution.
    with pytest.raises(Exception) as error:
        await send_harness.reconcile().execute(
            await _reconcile_command(
                send_harness,
                evidence=await send_harness.attest(execution_tag=tag, message_id="ses-forged"),
            )
        )
    assert isinstance(error.value, IntegrityError | ReconciliationRefusedError)
    final = await send_harness.execution()
    assert final.ses_message_id == "ses-real"


async def test_reconciliation_without_proof_leaves_the_quarantine_alone(
    send_harness: SendHarness,
) -> None:
    """Uncertainty remains unknown indefinitely rather than being resolved by a guess."""

    await send_harness.prepare()
    await send_harness.approve()
    await send_harness.send(sender=ScriptedSender(default=SesUnknown()))

    with pytest.raises(ReconciliationRefusedError) as error:
        await send_harness.reconcile().execute(await _reconcile_command(send_harness))
    assert error.value.refusal is ReconciliationRefusal.INSUFFICIENT_PROOF
    execution = await send_harness.execution()
    assert execution.state is ActionExecutionState.SEND_UNKNOWN


async def test_an_operator_attestation_can_only_resolve_to_failed(
    send_harness: SendHarness,
) -> None:
    """A person can responsibly say something did not happen; only SES can produce an ID."""

    await send_harness.prepare()
    await send_harness.approve()
    await send_harness.send(sender=ScriptedSender(default=SesUnknown()))

    result = await send_harness.reconcile().execute(
        await _reconcile_command(send_harness, operator_attestation_code="OPERATOR_VERIFIED")
    )

    assert result.state is ActionExecutionState.FAILED
    assert result.reason_code == ReconciliationReason.RECONCILED_FAILED.value
    assert result.ses_message_id is None


async def test_reconciliation_refuses_a_state_that_is_already_authoritative(
    send_harness: SendHarness,
) -> None:
    """It is a repair, not a second opinion."""

    await send_harness.prepare()
    await send_harness.approve()
    await send_harness.send(sender=ScriptedSender(default=SesAccepted(message_id="ses-1")))

    with pytest.raises(ReconciliationRefusedError) as error:
        await send_harness.reconcile().execute(await _reconcile_command(send_harness))
    assert error.value.refusal is ReconciliationRefusal.NOT_RECONCILABLE


# ---------------------------------------------------------------------------------------
# The case projection
# ---------------------------------------------------------------------------------------


async def _projection_command(send_harness: SendHarness) -> ProjectActionOutcomeCommand:
    from uuid import uuid4

    from chorus.domain.ids import Sha256Digest

    pointer = await send_harness.pointer()
    return ProjectActionOutcomeCommand(
        namespace=send_harness.action.compile.case.namespace,
        community_id=send_harness.action.compile.case.community_id,
        case_id=send_harness.action.case_id,
        action_id=pointer.action_id,
        execution_id=pointer.execution_id,
        actor_id_hash=Sha256Digest("sha256:" + "e" * 64),
        correlation_id=uuid4(),
    )


async def test_a_sent_execution_projects_the_case_to_actioned(
    send_harness: SendHarness,
) -> None:
    """Four participants, and ``authorization_version`` carried forward unchanged.

    Recording a send outcome changes no fact, status, mandate, or count, so an epoch that moved
    here would stale every view in the case for having successfully sent a message.
    """

    await send_harness.prepare()
    await send_harness.approve()
    await send_harness.send(sender=ScriptedSender(default=SesAccepted(message_id="ses-1")))
    before = await send_harness.action.compile.core.load_case(send_harness.action.scope)

    result = await send_harness.project().execute(await _projection_command(send_harness))

    assert result.projected is True
    assert result.case_state is CaseState.ACTIONED
    assert result.case_version == before.version + 1
    assert result.authorization_version == before.authorization_version
    plan = send_harness.action.unit_of_work.plan("project-action-outcome")
    assert len(plan.operations) == PROJECTION_PARTICIPANTS == 5
    # The execution is a read-only condition: it is the sender's row, and the worker has no
    # business changing it.
    assert isinstance(plan.operations[1], CheckItem)


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(SesUnknown(), id="send-unknown"),
    ],
)
async def test_a_non_sent_outcome_takes_no_case_edge_at_all(
    send_harness: SendHarness, outcome: object
) -> None:
    """``FAILED`` and ``SEND_UNKNOWN`` leave the case ``ACTION_PROPOSED`` with its banner."""

    await send_harness.prepare()
    await send_harness.approve()
    await send_harness.send(sender=ScriptedSender(default=outcome))  # type: ignore[arg-type]
    before = await send_harness.action.compile.core.load_case(send_harness.action.scope)

    result = await send_harness.project().execute(await _projection_command(send_harness))

    assert result.projected is False
    assert result.case_state is CaseState.ACTION_PROPOSED
    after = await send_harness.action.compile.core.load_case(send_harness.action.scope)
    assert after == before


async def test_the_projection_is_replay_safe_and_reports_rather_than_retrying(
    send_harness: SendHarness,
) -> None:
    """A second delivery reads ``ACTIONED`` and says so; the conditions would refuse anyway."""

    await send_harness.prepare()
    await send_harness.approve()
    await send_harness.send(sender=ScriptedSender(default=SesAccepted(message_id="ses-1")))
    await send_harness.project().execute(await _projection_command(send_harness))

    second = await send_harness.project().execute(await _projection_command(send_harness))

    assert second.projected is False
    assert second.case_state is CaseState.ACTIONED


# ---------------------------------------------------------------------------------------
# The worker's own recovery
# ---------------------------------------------------------------------------------------


async def test_the_worker_never_sends_again_after_an_ambiguous_result(
    send_harness: SendHarness,
) -> None:
    """The one recovery capability on this path: quarantine, and nothing else.

    The execution is left at ``SENDING`` with the SES call already made. A later delivery must
    read that row, decline to send, and -- once the window has passed -- record the honest
    answer.
    """

    await send_harness.prepare()
    await send_harness.approve()
    first = await _strand_in_sending(send_harness)
    assert first.call_count == 1
    send_harness.advance(SEND_RECOVERY_WINDOW + timedelta(seconds=1))

    second = ScriptedSender(default=SesAccepted(message_id="never"))
    await send_harness.reconcile().execute(await _reconcile_command(send_harness))

    assert second.call_count == 0
    execution = await send_harness.execution()
    assert execution.state is ActionExecutionState.SEND_UNKNOWN


def _ambiguous() -> SendUnknownReason:
    """The default classification, named so the disagreeing-ID test reads as its own scenario."""

    return SendUnknownReason.SES_TRANSPORT_AMBIGUOUS
