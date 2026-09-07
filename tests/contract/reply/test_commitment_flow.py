"""From an attested reply to a scheduled commitment, a due event, and a human's decision.

The exit criteria of Phase 9, written as tests: an attested fixture reply produces **exactly
one** commitment, **exactly one** schedule request under the deterministic name and client
token, **exactly one** ``PENDING -> DUE`` transition across duplicate and early invocations,
**exactly one** verification request, and the two human outcomes -- ``FULFILLED`` alone
resolving, ``MISSED`` returning the case to ``READY_FOR_ACTION`` with the action pointer
invalidated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tests.fixtures.reply import ReplyHarness, scripted_extraction

from chorus.application.commands.apply_commitment import (
    APPLY_PARTICIPANTS,
    REJECT_PARTICIPANTS,
    ApplyCommitmentCommand,
)
from chorus.application.commands.create_due_schedule import (
    SCHEDULE_PARTICIPANTS,
    CreateDueScheduleCommand,
)
from chorus.application.commands.extract_commitment_operation import (
    ExtractCommitmentJob,
    extraction_output_hash,
)
from chorus.application.commands.ingest_external_reply import IngestExternalReplyResult
from chorus.application.commands.record_commitment_due import (
    DUE_PARTICIPANTS,
    TRIGGER_DEMO_CLOCK,
    WatcherOutcome,
)
from chorus.application.commands.verify_commitment import (
    VERIFY_PARTICIPANTS,
    VerificationOutcome,
    VerificationRefusal,
    VerificationRefusedError,
)
from chorus.application.services.commitment_schedule import (
    due_event_id,
    schedule_client_token,
    schedule_name,
)
from chorus.application.services.commitment_validation import (
    VERIFICATION_METHOD,
    CommitmentRejection,
)
from chorus.contracts.commitment import (
    CommitmentExtractionOutput,
    ProposedCommitmentDraft,
    SourceSpan,
)
from chorus.domain.entities import (
    ActionProposalStatus,
    CaseState,
    Commitment,
    CommitmentStatus,
)
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import ContributorId, Sha256Digest, Uuid4Generator
from chorus.infrastructure.fixtures.inbound_replies import MANAGER_HEDGE, MANAGER_WEEKDAY
from chorus.infrastructure.local.commitment_agent import LiteralSpanCommitmentExtractor
from chorus.ports.errors import IdempotencyConflictError, PersistenceConflictError
from chorus.ports.records import CommitmentScheduleStatus
from chorus.ports.scheduler import ScheduleCreateFailed, ScheduleFailureCode
from chorus.ports.storage import TableName

pytestmark = pytest.mark.anyio


async def _commitment(harness: ReplyHarness, **overrides: object) -> Commitment:
    """Ingest one reply, extract from it, and return the commitment that resulted."""

    ingested = await harness.ingest_reply(**overrides)
    job = await harness.extraction_job(ingested)
    result = await harness.extract().execute(job)
    assert result.commitment_id is not None, result.rejection_codes
    return await harness.commitment(result.commitment_id)


async def test_a_grounded_reply_produces_one_pending_commitment_and_a_verifying_case(
    reply_harness: ReplyHarness,
) -> None:
    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness, received_at=_near(reply_harness))

    plan = reply_harness.send.action.unit_of_work.plan("apply-commitment")
    assert len(plan.operations) == APPLY_PARTICIPANTS

    case = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)
    assert case.state is CaseState.VERIFYING
    assert commitment.status is CommitmentStatus.PENDING
    assert commitment.verification_method == VERIFICATION_METHOD
    assert commitment.obligor == "property management"


async def test_the_deadline_is_the_cited_date_at_end_of_day_and_never_the_model_value(
    reply_harness: ReplyHarness,
) -> None:
    """The model's ``due_at`` is midnight; the derived value is end of day. They differ."""

    await reply_harness.prepare_sent()
    reply_harness.send.action.compile.clock.instant = reply_harness.send.action.compile.clock.now()
    commitment = await _commitment(reply_harness, received_at=_near(reply_harness))

    assert commitment.due_at.isoformat() == "2030-01-14T23:59:59.999999+00:00"


async def test_all_three_schedule_fields_are_derived_at_creation(
    reply_harness: ReplyHarness,
) -> None:
    """Nothing is attached afterwards: the entity's required fields are satisfiable at once."""

    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness, received_at=_near(reply_harness))

    assert commitment.schedule_generation == 1
    assert commitment.scheduler_name == schedule_name(
        environment="test",
        namespace=reply_harness.scope.namespace,
        commitment_id=commitment.commitment_id,
        generation=1,
    )
    assert commitment.due_event_id == due_event_id(
        commitment_id=commitment.commitment_id, generation=1
    )


async def test_a_weekday_reply_yields_no_commitment(reply_harness: ReplyHarness) -> None:
    """ "Technician scheduled Wednesday 10-12." has no ISO date, so nothing is possible."""

    await reply_harness.prepare_sent()
    ingested = await reply_harness.ingest_reply(fixture_id=MANAGER_WEEKDAY)
    result = await reply_harness.extract().execute(await reply_harness.extraction_job(ingested))

    assert result.commitment_id is None
    case = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)
    assert case.state is CaseState.ACTIONED


async def test_a_hedged_reply_is_not_a_commitment(reply_harness: ReplyHarness) -> None:
    """ "We will look into elevator B and may have an update by ..." fails unconditionality."""

    await reply_harness.prepare_sent()
    ingested = await reply_harness.ingest_reply(
        fixture_id=MANAGER_HEDGE, received_at=_near(reply_harness)
    )
    result = await reply_harness.extract().execute(await reply_harness.extraction_job(ingested))

    assert result.commitment_id is None
    assert CommitmentRejection.COMMITMENT_NOT_UNCONDITIONAL.value in result.rejection_codes
    plan = reply_harness.send.action.unit_of_work.plan("reject-commitment")
    assert len(plan.operations) == REJECT_PARTICIPANTS


async def test_a_rejected_extraction_takes_no_case_edge(reply_harness: ReplyHarness) -> None:
    """Compared against the case *after* ingestion, which legitimately moved both counters.

    Ingestion is authorization-sensitive and the extraction is not, so the interesting
    assertion is that the second step adds nothing on top of the first.
    """

    await reply_harness.prepare_sent()
    ingested = await reply_harness.ingest_reply(fixture_id=MANAGER_WEEKDAY)
    before = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)
    await reply_harness.extract().execute(await reply_harness.extraction_job(ingested))

    after = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)
    assert after.state is before.state
    assert after.version == before.version
    assert after.authorization_version == before.authorization_version


async def test_a_redelivered_rejected_extraction_costs_no_second_model_call(
    reply_harness: ReplyHarness,
) -> None:
    """Astra P2-4.A/B: a durably rejected extraction, redelivered, never re-reads the model.

    Before this repair, ``_reject`` wrote no agent-invocation record, so
    ``ExtractCommitment._recovered`` found no proof for a rejected outcome and fell through to a
    second ``invoke_commitment_extraction`` call over the same stranger's email -- indistinguish
    -able, at this layer, from "the transaction committed and the acknowledgement never arrived"
    (B), since both replay the identical job under the identical invocation identity.
    """

    await reply_harness.prepare_sent()
    ingested = await reply_harness.ingest_reply(
        fixture_id=MANAGER_HEDGE, received_at=_near(reply_harness)
    )
    job = await reply_harness.extraction_job(ingested)

    first = await reply_harness.extract().execute(job)
    assert first.commitment_id is None
    assert first.replayed is False
    extractor = reply_harness.extractor
    assert extractor is not None
    assert len(extractor.invocations) == 1

    second = await reply_harness.extract().execute(job)

    assert len(extractor.invocations) == 1
    assert second.commitment_id is None
    assert second.replayed is True
    # Exactly one "reject-commitment" transaction ever committed -- .plan() itself asserts that.
    reply_harness.send.action.unit_of_work.plan("reject-commitment")


async def test_a_replayed_rejection_carries_the_exact_original_codes(
    reply_harness: ReplyHarness,
) -> None:
    """Astra P2-4.E: the replay must not silently answer with an empty code tuple."""

    await reply_harness.prepare_sent()
    ingested = await reply_harness.ingest_reply(
        fixture_id=MANAGER_HEDGE, received_at=_near(reply_harness)
    )
    job = await reply_harness.extraction_job(ingested)

    first = await reply_harness.extract().execute(job)
    second = await reply_harness.extract().execute(job)

    assert second.replayed is True
    assert second.rejection_codes == first.rejection_codes
    assert CommitmentRejection.COMMITMENT_NOT_UNCONDITIONAL.value in second.rejection_codes


async def test_a_rejection_proof_for_different_content_is_never_reused(
    reply_harness: ReplyHarness,
) -> None:
    """Astra P2-4.C: a durable record naming a different artifact is not proof of this one.

    In production, ``ExtractCommitmentOperationWorker`` refuses a job whose evidence disagrees
    with the ``agent_binding_hash`` its operation record froze, before this layer is ever
    reached -- one invocation identity is permanently bound to one artifact. This test reaches
    under that guard, at the ``ExtractCommitment``/``ApplyCommitment`` layer directly, to prove
    the deeper property still holds: the recomputed input hash disagrees with the durable
    record's, ``_recovered`` refuses to treat that record as this run's proof, extraction runs
    fresh (one genuine model call, not a recovered zero), and the reused invocation identity's
    own create-only durable record then fails closed with a storage conflict rather than
    silently overwriting the first artifact's proof with the second's.
    """

    await reply_harness.prepare_sent()
    first_ingested = await reply_harness.ingest_reply(
        fixture_id=MANAGER_HEDGE, received_at=_near(reply_harness)
    )
    shared_invocation_id = uuid4()
    first_job = await reply_harness.extraction_job(
        first_ingested, invocation_id=shared_invocation_id
    )
    first = await reply_harness.extract().execute(first_job)
    assert first.commitment_id is None

    reply_harness.extractor = LiteralSpanCommitmentExtractor(
        destination_label=reply_harness.destination_label
    )
    second_ingested = await reply_harness.ingest_reply(received_at=_near(reply_harness))
    second_job = await reply_harness.extraction_job(
        second_ingested, invocation_id=shared_invocation_id
    )

    with pytest.raises(PersistenceConflictError):
        await reply_harness.extract().execute(second_job)

    # The one call that happened was a genuine model pass over the *second* artifact -- proof
    # ``_recovered`` did not answer from the first artifact's unrelated durable record.
    assert len(reply_harness.extractor.invocations) == 1


async def test_a_replay_with_a_mismatched_recorded_proof_fails_closed(
    reply_harness: ReplyHarness,
) -> None:
    """Astra P2-4.D: a proof that does not verify is refused, never guessed at.

    Calling ``ApplyCommitment`` directly with the completed idempotency record's key, its exact
    recorded ``output_hash`` (so the request-hash gate is satisfied), but an ``input_hash`` that
    disagrees with the durable invocation record simulates a corrupted or unrelated proof -- the
    replay must fail closed rather than answer with an empty-but-plausible result.
    """

    await reply_harness.prepare_sent()
    ingested = await reply_harness.ingest_reply(
        fixture_id=MANAGER_HEDGE, received_at=_near(reply_harness)
    )
    job = await reply_harness.extraction_job(ingested)
    first = await reply_harness.extract().execute(job)
    assert first.commitment_id is None

    invocation = await reply_harness.send.action.compile.core.load_agent_invocation(
        reply_harness.scope, job.invocation_id
    )
    assert invocation is not None
    assert invocation.output_hash is not None

    apply_command = ApplyCommitmentCommand(
        namespace=job.namespace,
        community_id=job.community_id,
        case_id=job.case_id,
        action_id=job.action_id,
        evidence_id=job.evidence_id,
        invocation_id=job.invocation_id,
        correlation_id=job.correlation_id,
        actor_id_hash=job.actor_id_hash,
        output=CommitmentExtractionOutput(
            case_id=job.case_id.value, source_evidence_id=job.evidence_id.value, commitments=()
        ),
        input_hash=Sha256Digest(f"sha256:{'0' * 64}"),
        output_hash=invocation.output_hash,
    )

    with pytest.raises(IntegrityError):
        await reply_harness.apply_commitment().execute(apply_command)


async def test_a_replay_bound_to_a_different_output_hash_fails_closed(
    reply_harness: ReplyHarness,
) -> None:
    """The remaining P2: a completed record for output A must not answer for a retry over B.

    Four independent retries under the exact same completed idempotency domain/key -- an empty
    proposal set, a sibling proposal, an altered clause, and a rejection's retry with an altered
    output -- each compute a different ``output_hash`` and each must fail closed rather than
    replay ``A``'s commitment, ``A``'s rejection, or any mutation at all.
    """

    await reply_harness.prepare_sent()
    ingested = await reply_harness.ingest_reply(received_at=_near(reply_harness))
    job = await reply_harness.extraction_job(ingested)

    original = _draft(ingested)
    first_command = _apply_command(job, original)
    first = await reply_harness.apply_commitment().execute(first_command)
    assert first.commitment_id is not None
    assert first.replayed is False

    case_before = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)

    # A. the exact same command replays cleanly.
    identical = await reply_harness.apply_commitment().execute(first_command)
    assert identical.replayed is True
    assert identical.commitment_id == first.commitment_id

    # B. an empty proposal set under the same key.
    empty_output = CommitmentExtractionOutput(
        case_id=ingested.case_id.value,
        source_evidence_id=ingested.evidence_id.value,
        commitments=(),
    )
    with pytest.raises(IdempotencyConflictError):
        await reply_harness.apply_commitment().execute(_apply_command(job, empty_output))

    # C. a sibling proposal under the same key.
    sibling = _draft(ingested, action_text="Restore elevator C to service by 2030-01-14")
    with pytest.raises(IdempotencyConflictError):
        await reply_harness.apply_commitment().execute(_apply_command(job, sibling))

    # D. an altered clause (a different due date) under the same key.
    altered = _draft(ingested, due_at=datetime(2030, 1, 15, tzinfo=UTC))
    with pytest.raises(IdempotencyConflictError):
        await reply_harness.apply_commitment().execute(_apply_command(job, altered))

    case_after = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)
    assert case_after.version == case_before.version
    assert case_after.state == case_before.state
    commitment = await reply_harness.commitment(first.commitment_id)
    assert commitment.version == 1


async def test_a_completed_rejections_retry_with_an_altered_output_fails_closed(
    reply_harness: ReplyHarness,
) -> None:
    """Scenario E: a completed rejection is not proof for a retry that answers differently."""

    await reply_harness.prepare_sent()
    ingested = await reply_harness.ingest_reply(
        fixture_id=MANAGER_HEDGE, received_at=_near(reply_harness)
    )
    job = await reply_harness.extraction_job(ingested)
    hedge_output = CommitmentExtractionOutput(
        case_id=ingested.case_id.value,
        source_evidence_id=ingested.evidence_id.value,
        commitments=(),
    )
    first = await reply_harness.apply_commitment().execute(_apply_command(job, hedge_output))
    assert first.commitment_id is None
    assert first.replayed is False

    altered = _draft(ingested)
    with pytest.raises(IdempotencyConflictError):
        await reply_harness.apply_commitment().execute(_apply_command(job, altered))


async def test_a_wrong_obligor_is_rejected(reply_harness: ReplyHarness) -> None:
    """Check 4: the obligor is compared with the correlated destination's safe label."""

    await reply_harness.prepare_sent()
    ingested = await reply_harness.ingest_reply(received_at=_near(reply_harness))
    reply_harness.extractor = scripted_extraction(
        _draft(
            ingested,
            obligor="Acme Elevator Services",
            action_text="Restore elevator B to service by 2030-01-14",
        )
    )
    result = await reply_harness.extract().execute(await reply_harness.extraction_job(ingested))

    assert result.commitment_id is None
    assert CommitmentRejection.COMMITMENT_OBLIGOR_MISMATCH.value in result.rejection_codes


async def test_an_ungrounded_action_text_is_rejected(reply_harness: ReplyHarness) -> None:
    """Check 3: every risk token in the restatement must be one the reply itself contains."""

    await reply_harness.prepare_sent()
    ingested = await reply_harness.ingest_reply(received_at=_near(reply_harness))
    reply_harness.extractor = scripted_extraction(
        _draft(ingested, action_text="Restore elevator B to service by 2030-02-28")
    )
    result = await reply_harness.extract().execute(await reply_harness.extraction_job(ingested))

    assert result.commitment_id is None
    assert CommitmentRejection.COMMITMENT_UNGROUNDED.value in result.rejection_codes


async def test_a_span_outside_the_stored_text_is_rejected(reply_harness: ReplyHarness) -> None:
    """Check 1: an offset pair either indexes the stored text or it does not."""

    await reply_harness.prepare_sent()
    ingested = await reply_harness.ingest_reply(received_at=_near(reply_harness))
    reply_harness.extractor = scripted_extraction(
        _draft(ingested, due_date_span=SourceSpan(start=9000, end=9010))
    )
    result = await reply_harness.extract().execute(await reply_harness.extraction_job(ingested))

    assert result.commitment_id is None
    assert CommitmentRejection.SPAN_OUT_OF_RANGE.value in result.rejection_codes


async def test_one_bad_proposal_does_not_discard_a_valid_sibling(
    reply_harness: ReplyHarness,
) -> None:
    """The Action precedent does not transfer: a dropped valid commitment costs a follow-up."""

    await reply_harness.prepare_sent()
    ingested = await reply_harness.ingest_reply(received_at=_near(reply_harness))
    bad = _draft(ingested, due_date_span=SourceSpan(start=9000, end=9010))
    good = _draft(ingested)
    reply_harness.extractor = scripted_extraction(
        CommitmentExtractionOutput(
            case_id=ingested.case_id.value,
            source_evidence_id=ingested.evidence_id.value,
            commitments=(bad.commitments[0], good.commitments[0]),
        )
    )
    result = await reply_harness.extract().execute(await reply_harness.extraction_job(ingested))

    assert result.commitment_id is not None
    assert CommitmentRejection.SPAN_OUT_OF_RANGE.value in result.rejection_codes


async def test_a_second_reply_returns_the_existing_commitment(
    reply_harness: ReplyHarness,
) -> None:
    """Check 9: at most one ``PENDING`` or ``DUE`` commitment per action."""

    await reply_harness.prepare_sent()
    first = await _commitment(reply_harness, received_at=_near(reply_harness))

    second = await reply_harness.ingest_reply(
        received_at=_near(reply_harness), object_key="inbound/chorus-test/reply-0002"
    )
    result = await reply_harness.extract().execute(await reply_harness.extraction_job(second))

    assert result.commitment_id == first.commitment_id
    assert CommitmentRejection.COMMITMENT_ALREADY_ACTIVE.value in result.rejection_codes


async def test_a_committed_apply_replays_without_a_second_model_call(
    reply_harness: ReplyHarness,
) -> None:
    """The durable invocation record settles it, and the model is not asked again."""

    from chorus.infrastructure.local.commitment_agent import LiteralSpanCommitmentExtractor

    await reply_harness.prepare_sent()
    ingested = await reply_harness.ingest_reply(received_at=_near(reply_harness))
    job = await reply_harness.extraction_job(ingested)
    first = await reply_harness.extract().execute(job)
    assert first.commitment_id is not None

    extractor = reply_harness.extractor
    assert isinstance(extractor, LiteralSpanCommitmentExtractor)
    calls_before = len(extractor.invocations)
    second = await reply_harness.extract().execute(job)

    assert second.commitment_id == first.commitment_id
    assert len(extractor.invocations) == calls_before


# ------------------------------------------------------------------------------------------
# The schedule
# ------------------------------------------------------------------------------------------


async def test_one_schedule_request_under_the_derived_name_and_client_token(
    reply_harness: ReplyHarness,
) -> None:
    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness, received_at=_near(reply_harness))

    outcome = await reply_harness.create_schedule().execute(
        _schedule_command(reply_harness, commitment)
    )

    assert outcome.status is CommitmentScheduleStatus.CREATED
    assert reply_harness.scheduler.create_count == 1
    request = reply_harness.scheduler.created[0]
    assert request.schedule_name == commitment.scheduler_name
    assert request.client_token == schedule_client_token(
        commitment_id=commitment.commitment_id, generation=1
    )
    plan = reply_harness.send.action.unit_of_work.plan("record-schedule-created")
    assert len(plan.operations) == SCHEDULE_PARTICIPANTS


async def test_a_failed_schedule_leaves_the_commitment_pending_and_visibly_unscheduled(
    reply_harness: ReplyHarness,
) -> None:
    """What failed is the alarm clock, not the promise."""

    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness, received_at=_near(reply_harness))
    reply_harness.scheduler.outcomes.append(
        ScheduleCreateFailed(reason_code=ScheduleFailureCode.SCHEDULER_UNAVAILABLE)
    )

    outcome = await reply_harness.create_schedule().execute(
        _schedule_command(reply_harness, commitment)
    )

    assert outcome.status is CommitmentScheduleStatus.PENDING_SCHEDULE
    assert outcome.failure_code == ScheduleFailureCode.SCHEDULER_UNAVAILABLE.value
    still = await reply_harness.commitment(commitment.commitment_id)
    assert still.status is CommitmentStatus.PENDING


async def test_a_lost_create_response_is_reconciled_by_name(
    reply_harness: ReplyHarness,
) -> None:
    """Never a second differently named schedule: the exact name is asked about instead."""

    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness, received_at=_near(reply_harness))
    reply_harness.scheduler.store_on_failure = True
    reply_harness.scheduler.outcomes.append(
        ScheduleCreateFailed(reason_code=ScheduleFailureCode.SCHEDULER_UNKNOWN)
    )

    outcome = await reply_harness.create_schedule().execute(
        _schedule_command(reply_harness, commitment)
    )

    assert outcome.status is CommitmentScheduleStatus.CREATED
    assert reply_harness.scheduler.describe_calls == [commitment.scheduler_name]
    assert reply_harness.scheduler.create_count == 1


# ------------------------------------------------------------------------------------------
# The watcher
# ------------------------------------------------------------------------------------------


async def test_the_watcher_moves_pending_to_due_exactly_once(
    reply_harness: ReplyHarness,
) -> None:
    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness, received_at=_near(reply_harness))
    reply_harness.past_due(commitment)

    first = await reply_harness.watcher().execute(await reply_harness.due_command(commitment))
    assert first.outcome is WatcherOutcome.DUE
    plan = reply_harness.send.action.unit_of_work.plan("record-commitment-due")
    assert len(plan.operations) == DUE_PARTICIPANTS

    reloaded = await reply_harness.commitment(commitment.commitment_id)
    second = await reply_harness.watcher().execute(await reply_harness.due_command(reloaded))
    assert second.outcome is WatcherOutcome.WATCHER_REPLAY

    request = await reply_harness.send.action.compile.shareable.load_verification_request(
        reply_harness.scope, commitment.commitment_id, generation=1
    )
    assert request is not None


async def test_the_watcher_takes_no_case_edge(reply_harness: ReplyHarness) -> None:
    """Asserted over the staged plan, in both tables."""

    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness, received_at=_near(reply_harness))
    reply_harness.past_due(commitment)
    await reply_harness.watcher().execute(await reply_harness.due_command(commitment))

    plan = reply_harness.send.action.unit_of_work.plan("record-commitment-due")
    assert all(operation.key.table is not TableName.CORE for operation in plan.operations)
    assert all(operation.key.sort_key != "CASE" for operation in plan.operations)


async def test_the_watcher_fires_early_and_changes_nothing(
    reply_harness: ReplyHarness,
) -> None:
    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness, received_at=_near(reply_harness))

    result = await reply_harness.watcher().execute(await reply_harness.due_command(commitment))

    assert result.outcome is WatcherOutcome.WATCHER_EARLY
    still = await reply_harness.commitment(commitment.commitment_id)
    assert still.status is CommitmentStatus.PENDING
    assert still.version == commitment.version
    assert reply_harness.scheduler.create_count == 0


async def test_the_watcher_refuses_a_stale_generation(reply_harness: ReplyHarness) -> None:
    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness, received_at=_near(reply_harness))
    reply_harness.past_due(commitment)

    result = await reply_harness.watcher().execute(
        await reply_harness.due_command(commitment, generation=2)
    )

    assert result.outcome is WatcherOutcome.WATCHER_STALE_GENERATION
    still = await reply_harness.commitment(commitment.commitment_id)
    assert still.status is CommitmentStatus.PENDING


async def test_the_watcher_refuses_a_due_time_the_row_does_not_hold(
    reply_harness: ReplyHarness,
) -> None:
    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness, received_at=_near(reply_harness))
    reply_harness.past_due(commitment)

    result = await reply_harness.watcher().execute(
        await reply_harness.due_command(commitment, due_at=commitment.due_at + timedelta(days=1))
    )

    assert result.outcome is WatcherOutcome.WATCHER_STALE_GENERATION


async def test_the_demo_clock_reaches_the_same_watcher(reply_harness: ReplyHarness) -> None:
    """The same function, the same event, and a ``trigger`` audit field. Not a second path."""

    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness, received_at=_near(reply_harness))
    reply_harness.past_due(commitment)

    result = await reply_harness.watcher().execute(
        await reply_harness.due_command(commitment, trigger=TRIGGER_DEMO_CLOCK)
    )

    assert result.outcome is WatcherOutcome.DUE


# ------------------------------------------------------------------------------------------
# Verification
# ------------------------------------------------------------------------------------------


async def _due_commitment(harness: ReplyHarness) -> Commitment:
    commitment = await _commitment(harness, received_at=_near(harness))
    harness.past_due(commitment)
    await harness.watcher().execute(await harness.due_command(commitment))
    return await harness.commitment(commitment.commitment_id)


async def test_fulfilled_alone_resolves_the_case(reply_harness: ReplyHarness) -> None:
    await reply_harness.prepare_sent()
    commitment = await _due_commitment(reply_harness)
    contributor = await reply_harness.affected_contributor()

    result = await reply_harness.verify().execute(
        await reply_harness.verification_command(commitment, contributor_id=contributor)
    )

    assert result.case_state is CaseState.RESOLVED
    assert result.commitment_status is CommitmentStatus.FULFILLED
    assert not result.action_pointer_invalidated
    plan = reply_harness.send.action.unit_of_work.plan("verify-commitment")
    assert len(plan.operations) == VERIFY_PARTICIPANTS


async def test_missed_returns_the_case_to_ready_and_invalidates_the_pointer(
    reply_harness: ReplyHarness,
) -> None:
    await reply_harness.prepare_sent()
    commitment = await _due_commitment(reply_harness)
    contributor = await reply_harness.affected_contributor()

    result = await reply_harness.verify().execute(
        await reply_harness.verification_command(
            commitment, contributor_id=contributor, outcome=VerificationOutcome.MISSED
        )
    )

    assert result.case_state is CaseState.READY_FOR_ACTION
    assert result.commitment_status is CommitmentStatus.MISSED
    assert result.action_pointer_invalidated
    pointer = await reply_harness.send.action.compile.shareable.load_current_action_pointer(
        reply_harness.scope
    )
    assert pointer is not None
    assert pointer.status is ActionProposalStatus.INVALIDATED
    plan = reply_harness.send.action.unit_of_work.plan("verify-commitment")
    assert len(plan.operations) == VERIFY_PARTICIPANTS


async def test_a_contributor_who_owns_no_active_fact_cannot_verify(
    reply_harness: ReplyHarness,
) -> None:
    """Checked against loaded facts, never against a claim in the request body."""

    await reply_harness.prepare_sent()
    commitment = await _due_commitment(reply_harness)
    stranger = ContributorId(Uuid4Generator().new_uuid())

    with pytest.raises(VerificationRefusedError) as raised:
        await reply_harness.verify().execute(
            await reply_harness.verification_command(commitment, contributor_id=stranger)
        )
    assert raised.value.refusal is VerificationRefusal.ACTOR_NOT_AFFECTED
    still = await reply_harness.commitment(commitment.commitment_id)
    assert still.status is CommitmentStatus.DUE


async def test_a_pending_commitment_cannot_be_verified(reply_harness: ReplyHarness) -> None:
    """Time passage produces a verification request; it never produces an outcome."""

    await reply_harness.prepare_sent()
    commitment = await _commitment(reply_harness, received_at=_near(reply_harness))
    contributor = await reply_harness.affected_contributor()

    with pytest.raises(VerificationRefusedError) as raised:
        await reply_harness.verify().execute(
            await reply_harness.verification_command(commitment, contributor_id=contributor)
        )
    assert raised.value.refusal is VerificationRefusal.COMMITMENT_NOT_DUE


async def test_a_repeated_verification_replays_its_own_answer(
    reply_harness: ReplyHarness,
) -> None:
    await reply_harness.prepare_sent()
    commitment = await _due_commitment(reply_harness)
    contributor = await reply_harness.affected_contributor()
    command = await reply_harness.verification_command(commitment, contributor_id=contributor)

    first = await reply_harness.verify().execute(command)
    second = await reply_harness.verify().execute(command)

    assert not first.replayed
    assert second.replayed
    assert second.case_state is CaseState.RESOLVED


# ------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------


def _near(harness: ReplyHarness) -> datetime:
    """A received instant close enough to 2030-01-14 for the 30-day range to admit it.

    The fixture corpus states a fixed date so the reviewed text is stable; the clock is what
    moves. Setting the harness clock as well keeps ``uploaded_at`` and the range check reading
    the same world.
    """

    from datetime import UTC, datetime

    instant = datetime(2030, 1, 2, 9, 0, tzinfo=UTC)
    harness.send.action.compile.clock.instant = instant
    return instant


def _schedule_command(harness: ReplyHarness, commitment: Commitment) -> CreateDueScheduleCommand:
    from tests.fixtures.send import APPROVER_HASH

    return CreateDueScheduleCommand(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        commitment=commitment,
        actor_id_hash=APPROVER_HASH,
        correlation_id=commitment.commitment_id.value,
    )


def _apply_command(
    job: ExtractCommitmentJob, output: CommitmentExtractionOutput
) -> ApplyCommitmentCommand:
    """One retry of ``job`` answering with ``output``, its ``output_hash`` computed the way
    ``ExtractCommitment`` computes it -- so two calls with a differently shaped ``output`` are,
    by construction, two different logical requests under the exact same idempotency key.
    """

    return ApplyCommitmentCommand(
        namespace=job.namespace,
        community_id=job.community_id,
        case_id=job.case_id,
        action_id=job.action_id,
        evidence_id=job.evidence_id,
        invocation_id=job.invocation_id,
        correlation_id=job.correlation_id,
        actor_id_hash=job.actor_id_hash,
        output=output,
        input_hash=job.request_hash,
        output_hash=extraction_output_hash(output),
    )


def _draft(ingested: IngestExternalReplyResult, **overrides: object) -> CommitmentExtractionOutput:
    """The narrowest extraction the fixture reply admits, grounded by construction."""

    text = (
        "thank you for the report.\n"
        "we will restore elevator b to service by 2030-01-14.\n"
        "please contact the office with any further concerns."
    )
    date_start = text.index("2030-01-14")
    action_start = text.index("we will restore")
    fields: dict[str, object] = {
        "obligor_span": SourceSpan(start=action_start, end=action_start + 10),
        "action_span": SourceSpan(start=action_start, end=date_start + 10),
        "due_date_span": SourceSpan(start=date_start, end=date_start + 10),
        "obligor": "Property Management",
        "action_text": "Restore elevator B to service by 2030-01-14",
        "due_at": datetime(2030, 1, 14, tzinfo=UTC),
        "refusal_detected": False,
    }
    fields.update(overrides)
    return CommitmentExtractionOutput(
        case_id=ingested.case_id.value,
        source_evidence_id=ingested.evidence_id.value,
        commitments=(ProposedCommitmentDraft(**fields),),  # type: ignore[arg-type]
    )
