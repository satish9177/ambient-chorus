"""The trusted reconciliation caller: what it accepts, and what it structurally cannot.

``SEND_UNKNOWN -> SENT`` needs an SES message identifier, and only SES can produce one. So the
entry point that resolves a quarantine takes an **authenticated delivery**, not a set of fields a
caller chose: there is no ``message_id`` parameter, no ``execution_tag`` parameter, no
``configuration_set`` parameter, and no ``accepted`` flag anywhere on its command.

These tests assert both halves of that, and the second half is the repair. Structural decoding
was never provenance: an envelope a caller typed decoded cleanly, correlated cleanly, and moved a
quarantined row to ``SENT`` under an invented identifier. Everything below the happy path is
therefore about *origin* -- a forged envelope, a hand-built evidence object, a hand-built attested
wrapper, another deployment's boundary, an unauthenticated transport, and a deployment with no
boundary wired at all. Every one of them leaves the row exactly where it was, writes no
identifier, and makes no SES call.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from tests.fixtures.send import (
    CONFIGURATION_SET,
    EVENT_SOURCE_ARN,
    EVENT_TRANSPORT,
    FOREIGN_SOURCE_ARN,
    ScriptedTransportAuthenticator,
    SendHarness,
    ses_event_envelope,
)

from chorus.application.commands.reconcile_from_ses_event import (
    ReconcileFromSesEvent,
    ReconcileFromSesEventCommand,
)
from chorus.application.commands.reconcile_send_outcome import (
    ReconcileSendOutcomeCommand,
    ReconciliationRefusal,
    ReconciliationRefusedError,
)
from chorus.application.services.ses_events import (
    CONFIGURATION_SET_TAG,
    EXECUTION_TAG_NAME,
    AttestedSesEventEvidence,
    SesEventEvidence,
    SesEventRejected,
    SesEventRejection,
    SesEventTrustFailure,
    SesEventUntrusted,
    decode_configuration_set_event,
    ses_event_trust_boundary,
)
from chorus.domain.entities import EXECUTION_FIELD_PRESENCE, ActionExecutionState, FieldPresence
from chorus.domain.ids import ExecutionId, Sha256Digest
from chorus.domain.state import ACTION_EXECUTION_EDGES
from chorus.infrastructure.local.sender import ScriptedSender
from chorus.ports.sender import SesUnknown
from chorus.ports.ses_events import SesEventTransportContext

pytestmark = pytest.mark.anyio

ACTOR = Sha256Digest("sha256:" + "e" * 64)


async def _quarantined(send_harness: SendHarness) -> tuple[ExecutionId, ScriptedSender]:
    """Drive a real send to an ambiguous outcome, so the row under test is genuinely unknown."""

    await send_harness.prepare()
    await send_harness.approve()
    pointer = await send_harness.pointer()
    sender = ScriptedSender(default=SesUnknown())
    await send_harness.send(sender=sender)
    assert (await send_harness.execution()).state is ActionExecutionState.SEND_UNKNOWN
    assert sender.call_count == 1
    return pointer.execution_id, sender


async def _command(
    send_harness: SendHarness,
    execution_id: ExecutionId,
    envelope: dict[str, object],
    *,
    source_arn: str = EVENT_SOURCE_ARN,
    transport: str = EVENT_TRANSPORT,
) -> ReconcileFromSesEventCommand:
    return await send_harness.ses_event_command(
        execution_id,
        send_harness.delivery(envelope=envelope, transport=transport, source_arn=source_arn),
        actor_id_hash=ACTOR,
    )


def _caller(send_harness: SendHarness) -> ReconcileFromSesEvent:
    return send_harness.ses_event_caller()


async def _assert_untouched(send_harness: SendHarness, sender: ScriptedSender) -> None:
    """The three facts every refusal on this boundary owes: no move, no identifier, no call."""

    execution = await send_harness.execution()
    assert execution.state is ActionExecutionState.SEND_UNKNOWN
    assert execution.ses_message_id is None
    assert sender.call_count == 1


# ---------------------------------------------------------------------------------------
# R15 and the happy path -- C: evidence the trusted adapter produced
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", ["Send", "Delivery"])
async def test_r15_send_unknown_resolves_to_sent_with_failure_detail_absent(
    send_harness: SendHarness, event_type: str
) -> None:
    """R15. The frozen ``SEND_UNKNOWN -> SENT`` edge remains legal and reachable.

    ``failure_detail_safe`` is ``ABSENT`` at both ends, which is the correction this repair
    makes to the presence table. It used to be ``OPTIONAL`` at ``SEND_UNKNOWN`` and ``ABSENT`` at
    ``SENT``, and presence is monotonic -- so a quarantined row that had exercised the option
    could never take this edge. An option only one value is reachable from is not an option.

    It is also finding C: evidence that came through the trusted adapter, and only that, still
    resolves a quarantine. A boundary that refused everything would be safe and useless.
    """

    execution_id, _ = await _quarantined(send_harness)
    quarantined = await send_harness.execution()
    assert quarantined.failure_detail_safe is None

    result = await _caller(send_harness).execute(
        await _command(
            send_harness,
            execution_id,
            ses_event_envelope(
                event_type=event_type,
                execution_tag=send_harness.execution_tag(execution_id),
                message_id="0100016a-proof",
            ),
        )
    )

    assert result.state is ActionExecutionState.SENT
    assert result.ses_message_id == "0100016a-proof"
    sent = await send_harness.execution()
    assert sent.failure_detail_safe is None
    assert sent.reconciled_at is not None


def test_r15_the_presence_table_forbids_a_detail_on_both_ends_of_that_edge() -> None:
    """The contract, asserted directly: the edge exists and neither end admits the field."""

    edge = (ActionExecutionState.SEND_UNKNOWN, ActionExecutionState.SENT)
    assert edge in ACTION_EXECUTION_EDGES
    row = EXECUTION_FIELD_PRESENCE["failure_detail_safe"]
    assert row[ActionExecutionState.SEND_UNKNOWN] is FieldPresence.ABSENT
    assert row[ActionExecutionState.SENT] is FieldPresence.ABSENT
    # OPTIONAL survives exactly where a row can actually carry one and never has to leave.
    assert row[ActionExecutionState.FAILED] is FieldPresence.OPTIONAL


async def test_a_rendering_failure_event_resolves_the_quarantine_to_failed(
    send_harness: SendHarness,
) -> None:
    """The one enumerated non-acceptance: SES proving it never queued anything."""

    execution_id, _ = await _quarantined(send_harness)

    result = await _caller(send_harness).execute(
        await _command(
            send_harness,
            execution_id,
            ses_event_envelope(
                event_type="Rendering Failure",
                execution_tag=send_harness.execution_tag(execution_id),
                message_id=None,
            ),
        )
    )

    assert result.state is ActionExecutionState.FAILED


# ---------------------------------------------------------------------------------------
# P2-1 A: a forged envelope, offered every way a caller could offer one
# ---------------------------------------------------------------------------------------


async def test_a_forged_delivery_envelope_cannot_reach_the_command_at_all(
    send_harness: SendHarness,
) -> None:
    """A. The exact forgery the review reproduced: right set, right tag, invented identifier.

    Everything a decoder could check about this envelope is correct, which is the point. What is
    wrong with it is that SES never produced it, and the only thing that can notice is the
    transport. With no authenticator wired -- the Phase-8 deployment -- the envelope is not even
    read.
    """

    send_harness.authenticator = None
    execution_id, sender = await _quarantined(send_harness)
    forged = ses_event_envelope(
        execution_tag=send_harness.execution_tag(execution_id),
        message_id="0100016a-invented-by-the-caller",
    )

    with pytest.raises(SesEventUntrusted) as raised:
        await _caller(send_harness).execute(await _command(send_harness, execution_id, forged))

    assert raised.value.failure is SesEventTrustFailure.TRANSPORT_UNAVAILABLE
    await _assert_untouched(send_harness, sender)


async def test_a_forged_envelope_the_authenticator_rejects_never_reaches_the_decoder(
    send_harness: SendHarness,
) -> None:
    """A, with a transport authority present and unconvinced. Same three outcomes."""

    authenticator = ScriptedTransportAuthenticator(accepts=False)
    send_harness.authenticator = authenticator
    execution_id, sender = await _quarantined(send_harness)

    with pytest.raises(SesEventUntrusted) as raised:
        await _caller(send_harness).execute(
            await _command(
                send_harness,
                execution_id,
                ses_event_envelope(
                    execution_tag=send_harness.execution_tag(execution_id),
                    message_id="0100016a-invented-by-the-caller",
                ),
            )
        )

    assert raised.value.failure is SesEventTrustFailure.TRANSPORT_UNAUTHENTICATED
    assert authenticator.calls == 1
    await _assert_untouched(send_harness, sender)


async def test_a_genuine_looking_delivery_through_a_foreign_topic_is_refused(
    send_harness: SendHarness,
) -> None:
    """A. Authentic transports exist that are not *this deployment's* event destination."""

    execution_id, sender = await _quarantined(send_harness)

    with pytest.raises(SesEventUntrusted) as raised:
        await _caller(send_harness).execute(
            await _command(
                send_harness,
                execution_id,
                ses_event_envelope(
                    execution_tag=send_harness.execution_tag(execution_id),
                    message_id="0100016a-foreign-topic",
                ),
                source_arn=FOREIGN_SOURCE_ARN,
            )
        )

    assert raised.value.failure is SesEventTrustFailure.FOREIGN_TRANSPORT_SOURCE
    await _assert_untouched(send_harness, sender)


# ---------------------------------------------------------------------------------------
# P2-1 B: evidence objects a caller builds, rather than obtains
# ---------------------------------------------------------------------------------------


async def test_a_caller_built_evidence_object_is_not_evidence(
    send_harness: SendHarness,
) -> None:
    """B. The decoder is public, the derivation is public, and neither of them is provenance.

    This test does exactly what an attacker with repository access would: decode the forged
    envelope with the application's own function, wrap the result in the application's own
    types, and hand it to the command. The wrapper is well-formed. It carries an attestation
    string of the right shape. It is refused, because the boundary compares a MAC rather than a
    type name.
    """

    execution_id, sender = await _quarantined(send_harness)
    configuration_set, execution_tag, message_id, accepted = decode_configuration_set_event(
        ses_event_envelope(
            execution_tag=send_harness.execution_tag(execution_id),
            message_id="0100016a-hand-built",
        )
    )
    hand_built = AttestedSesEventEvidence(
        evidence=SesEventEvidence(
            configuration_set=configuration_set,
            execution_tag=execution_tag,
            message_id=message_id,
            accepted=accepted,
        ),
        source_arn=EVENT_SOURCE_ARN,
        attestation="0" * 64,
    )

    with pytest.raises(ReconciliationRefusedError) as raised:
        await send_harness.reconcile().execute(
            await _reconcile_command(send_harness, execution_id, hand_built)
        )

    assert raised.value.refusal is ReconciliationRefusal.UNATTESTED_EVIDENCE
    await _assert_untouched(send_harness, sender)


async def test_evidence_minted_by_another_deployments_boundary_is_refused(
    send_harness: SendHarness,
) -> None:
    """B. A real attester, a real authenticator, a real envelope -- and the wrong key.

    The strongest form of the forgery: somebody who can build the whole boundary still cannot
    build *this* deployment's, because the attestation key exists only inside the pair the
    reconciling process made.
    """

    execution_id, sender = await _quarantined(send_harness)
    attester, _ = ses_event_trust_boundary(
        transport=EVENT_TRANSPORT,
        source_arn=EVENT_SOURCE_ARN,
        authenticator=ScriptedTransportAuthenticator(),
    )
    foreign = await attester.attest(
        SesEventTransportContext(
            transport=EVENT_TRANSPORT,
            source_arn=EVENT_SOURCE_ARN,
            envelope=ses_event_envelope(
                execution_tag=send_harness.execution_tag(execution_id),
                message_id="0100016a-other-boundary",
            ),
        )
    )

    with pytest.raises(ReconciliationRefusedError) as raised:
        await send_harness.reconcile().execute(
            await _reconcile_command(send_harness, execution_id, foreign)
        )

    assert raised.value.refusal is ReconciliationRefusal.UNATTESTED_EVIDENCE
    await _assert_untouched(send_harness, sender)


async def test_attested_evidence_edited_after_minting_no_longer_verifies(
    send_harness: SendHarness,
) -> None:
    """B. The attestation covers the fields, so swapping the identifier invalidates it."""

    execution_id, sender = await _quarantined(send_harness)
    genuine = await send_harness.attest(
        execution_tag=send_harness.execution_tag(execution_id), message_id="0100016a-real"
    )
    tampered = replace(
        genuine, evidence=replace(genuine.evidence, message_id="0100016a-substituted")
    )

    with pytest.raises(ReconciliationRefusedError) as raised:
        await send_harness.reconcile().execute(
            await _reconcile_command(send_harness, execution_id, tampered)
        )

    assert raised.value.refusal is ReconciliationRefusal.UNATTESTED_EVIDENCE
    await _assert_untouched(send_harness, sender)


# ---------------------------------------------------------------------------------------
# P2-1 D and E: authentic evidence, pointed at the wrong thing
# ---------------------------------------------------------------------------------------


async def test_r14_a_genuine_event_about_another_execution_is_refused(
    send_harness: SendHarness,
) -> None:
    """R14 / D. Cross-execution evidence is refused by the recomputed tag, not the transport.

    A real SES event about execution X, replayed against execution Y, is still a real event --
    which is precisely why the delivery path is not what decides. Authentication and correlation
    are different questions, and this is the second one.
    """

    execution_id, sender = await _quarantined(send_harness)
    foreign = ExecutionId(uuid4())

    with pytest.raises(ReconciliationRefusedError) as raised:
        await _caller(send_harness).execute(
            await _command(
                send_harness,
                execution_id,
                ses_event_envelope(
                    execution_tag=send_harness.execution_tag(foreign),
                    message_id="0100016a-somebody-elses",
                ),
            )
        )

    assert raised.value.refusal is ReconciliationRefusal.TAG_MISMATCH
    await _assert_untouched(send_harness, sender)


async def test_r14_an_event_from_a_foreign_configuration_set_is_refused(
    send_harness: SendHarness,
) -> None:
    """E. The configuration set is read out of the envelope, and it has to be this deployment's."""

    execution_id, sender = await _quarantined(send_harness)

    with pytest.raises(ReconciliationRefusedError) as raised:
        await _caller(send_harness).execute(
            await _command(
                send_harness,
                execution_id,
                ses_event_envelope(
                    configuration_set="somebody-elses-set",
                    execution_tag=send_harness.execution_tag(execution_id),
                    message_id="0100016a-forged",
                ),
            )
        )

    assert raised.value.refusal is ReconciliationRefusal.FOREIGN_CONFIGURATION_SET
    await _assert_untouched(send_harness, sender)


# ---------------------------------------------------------------------------------------
# P2-1 G: the boundary itself missing
# ---------------------------------------------------------------------------------------


async def test_a_command_with_no_verifier_wired_refuses_even_genuine_evidence(
    send_harness: SendHarness,
) -> None:
    """G. Quarantine is the correct answer when nothing can authenticate anything.

    The evidence here is genuine -- minted by this harness's own attester. The command is the
    one that has no boundary, which is the Phase-8 deployment shape until Phase 11 supplies a
    transport authenticator. It refuses rather than degrading to a decode-and-believe.
    """

    execution_id, sender = await _quarantined(send_harness)
    genuine = await send_harness.attest(
        execution_tag=send_harness.execution_tag(execution_id), message_id="0100016a-real"
    )

    with pytest.raises(ReconciliationRefusedError) as raised:
        await send_harness.reconcile(trusted=False).execute(
            await _reconcile_command(send_harness, execution_id, genuine)
        )

    assert raised.value.refusal is ReconciliationRefusal.TRUST_BOUNDARY_UNAVAILABLE
    await _assert_untouched(send_harness, sender)


async def test_the_worker_can_still_quarantine_with_no_boundary_wired(
    send_harness: SendHarness,
) -> None:
    """G's other half: the absence of a boundary must not disable the evidence-free path.

    ``SENDING -> SEND_UNKNOWN`` needs no evidence and must keep working in a deployment that has
    no event transport at all, or an abandoned row could never be quarantined in the first place.
    """

    send_harness.authenticator = None
    execution_id, _ = await _quarantined(send_harness)

    with pytest.raises(ReconciliationRefusedError) as raised:
        await send_harness.reconcile(trusted=False).execute(
            await _reconcile_command(send_harness, execution_id, None)
        )

    # No evidence offered at all is the ordinary "nothing to go on" refusal, not a trust failure.
    assert raised.value.refusal is ReconciliationRefusal.INSUFFICIENT_PROOF


# ---------------------------------------------------------------------------------------
# P2-1 F: malformed and unsupported events, now behind the authenticator
# ---------------------------------------------------------------------------------------


async def test_the_entry_point_has_no_parameter_for_self_asserted_proof() -> None:
    """The absence is the design, so it is asserted rather than left to review.

    An entry point taking ``{configuration_set, execution_tag, message_id}`` from its caller
    would let anybody who could reach it resolve a quarantine to ``SENT`` by typing three
    strings. There is no such field on either command, and this fails if one is ever added.
    """

    adapter_fields = set(ReconcileFromSesEventCommand.__dataclass_fields__)
    command_fields = set(ReconcileSendOutcomeCommand.__dataclass_fields__)
    forgeable = {"configuration_set", "execution_tag", "message_id", "accepted"}

    assert not adapter_fields & forgeable
    assert not command_fields & forgeable
    assert "delivery" in adapter_fields
    # And the command's own evidence field takes the attested wrapper, never the decoded payload.
    assert ReconcileSendOutcomeCommand.__annotations__["evidence"] == (
        "AttestedSesEventEvidence | None"
    )


@pytest.mark.parametrize(
    ("envelope", "rejection"),
    [
        pytest.param({}, SesEventRejection.MALFORMED_ENVELOPE, id="empty"),
        pytest.param({"eventType": "Delivery"}, SesEventRejection.MALFORMED_ENVELOPE, id="no-mail"),
        pytest.param(
            {"eventType": "Delivery", "mail": {}},
            SesEventRejection.MALFORMED_ENVELOPE,
            id="no-tags",
        ),
        pytest.param(
            ses_event_envelope(event_type="Bounce", execution_tag="0" * 64, message_id="x"),
            SesEventRejection.UNINTERPRETED_EVENT_TYPE,
            id="unenumerated-type",
        ),
        pytest.param(
            {
                "eventType": "Delivery",
                "mail": {"messageId": "x", "tags": {EXECUTION_TAG_NAME: ["0" * 64]}},
            },
            SesEventRejection.MISSING_CONFIGURATION_SET,
            id="no-configuration-set",
        ),
        pytest.param(
            {
                "eventType": "Delivery",
                "mail": {"messageId": "x", "tags": {CONFIGURATION_SET_TAG: [CONFIGURATION_SET]}},
            },
            SesEventRejection.MISSING_EXECUTION_TAG,
            id="no-execution-tag",
        ),
        pytest.param(
            ses_event_envelope(execution_tag="0" * 64, message_id=None),
            SesEventRejection.MISSING_MESSAGE_ID,
            id="acceptance-without-identifier",
        ),
    ],
)
async def test_an_envelope_this_boundary_would_have_to_guess_about_is_refused(
    send_harness: SendHarness, envelope: dict[str, object], rejection: SesEventRejection
) -> None:
    """F. Nothing is defaulted and nothing is inferred, so nothing here moves a row.

    An envelope the decoder had to guess about is an envelope somebody could have constructed,
    and a guess in the permissive direction resolves a quarantine on no evidence at all. These
    now run *after* the transport is authenticated, which is why they are reached at all.
    """

    execution_id, sender = await _quarantined(send_harness)

    with pytest.raises(SesEventRejected) as raised:
        await _caller(send_harness).execute(await _command(send_harness, execution_id, envelope))

    assert raised.value.rejection is rejection
    await _assert_untouched(send_harness, sender)


async def _reconcile_command(
    send_harness: SendHarness,
    execution_id: ExecutionId,
    evidence: AttestedSesEventEvidence | None,
) -> ReconcileSendOutcomeCommand:
    """The command as an attacker reaching past the adapter would have to build it."""

    pointer = await send_harness.pointer()
    return ReconcileSendOutcomeCommand(
        namespace=send_harness.action.compile.case.namespace,
        community_id=send_harness.action.compile.case.community_id,
        case_id=send_harness.action.case_id,
        action_id=pointer.action_id,
        execution_id=execution_id,
        actor_id_hash=ACTOR,
        correlation_id=uuid4(),
        evidence=evidence,
    )
