"""The extraction operation: binding before claiming, and recovery without a second model call.

A second pass here is a second reading of somebody's private email, so the number that matters
is the model call count -- and a test that inferred it from a durable state could not tell a run
that recovered from one that ran again and happened to agree.
"""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest
from tests.fixtures.action import ACTOR_HASH
from tests.fixtures.reply import ReplyHarness

from chorus.application.commands.extract_commitment_operation import (
    ExtractCommitmentJob,
    ExtractCommitmentJobBinding,
    ExtractCommitmentOperationWorker,
    extraction_input_hash,
)
from chorus.application.operations import (
    ApplicationOperations,
    extract_commitment_binding_hash,
)
from chorus.contracts.commitment import CommitmentExtractionInput
from chorus.domain.entities import (
    ApplicationOperationKind,
    ApplicationOperationStatus,
    CaseState,
)
from chorus.domain.ids import Sha256Digest, Uuid4Generator
from chorus.infrastructure.local.commitment_agent import LiteralSpanCommitmentExtractor
from chorus.ports.idempotency import IdempotentCommand
from chorus.ports.scopes import CaseScope

pytestmark = pytest.mark.anyio


def _operations(harness: ReplyHarness) -> ApplicationOperations:
    return ApplicationOperations(
        core=harness.send.action.compile.core,
        idempotency=harness.send.action.compile.idempotency,
        unit_of_work=harness.send.action.compile.unit_of_work,
        clock=harness.send.action.compile.clock,
        ids=Uuid4Generator(),
    )


async def _started_job(harness: ReplyHarness) -> tuple[ExtractCommitmentJob, ApplicationOperations]:
    """Create the durable operation exactly as the route does, then its job.

    The worker's first act is to load the operation the job names, so a fabricated job would
    exercise the binding check and nothing else.
    """

    from datetime import UTC, datetime

    harness.send.action.compile.clock.instant = datetime(2030, 1, 2, 9, 0, tzinfo=UTC)
    ingested = await harness.ingest_reply()
    items = await harness.send.action.compile.core.load_evidence_items(
        harness.scope, (ingested.evidence_id,)
    )
    evidence_sha256 = items[0].sha256
    binding = extract_commitment_binding_hash(
        case_id=ingested.case_id,
        evidence_id=ingested.evidence_id,
        evidence_sha256=evidence_sha256,
    )
    operations = _operations(harness)
    reserved = await operations.reserve_start(
        namespace=harness.scope.namespace,
        command=IdempotentCommand.EXTRACT_COMMITMENT,
        actor_id_hash=ACTOR_HASH,
        key_hash=evidence_sha256,
        request_hash=binding,
    )
    started = await operations.complete_start(
        reserved,  # type: ignore[arg-type]
        namespace=harness.scope.namespace,
        kind=ApplicationOperationKind.EXTRACT_COMMITMENT,
        actor_id_hash=ACTOR_HASH,
        case_id=ingested.case_id,
        agent_binding_hash=binding,
    )
    job = ExtractCommitmentJob(
        operation_id=started.operation.operation_id,
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=ingested.case_id,
        action_id=ingested.action_id,
        evidence_id=ingested.evidence_id,
        invocation_id=started.invocation_id,
        correlation_id=uuid4(),
        actor_id_hash=ACTOR_HASH,
        request_hash=binding,
        evidence_sha256=evidence_sha256,
    )
    return job, operations


def _worker(
    harness: ReplyHarness, operations: ApplicationOperations
) -> ExtractCommitmentOperationWorker:
    return ExtractCommitmentOperationWorker(operations=operations, extract=harness.extract())


async def test_a_bound_job_runs_once_and_succeeds(reply_harness: ReplyHarness) -> None:
    await reply_harness.prepare_sent()
    job, operations = await _started_job(reply_harness)

    operation = await _worker(reply_harness, operations).execute(job)

    assert operation.status is ApplicationOperationStatus.SUCCEEDED
    case = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)
    assert case.state is CaseState.VERIFYING
    extractor = reply_harness.extractor
    assert isinstance(extractor, LiteralSpanCommitmentExtractor)
    assert len(extractor.invocations) == 1


async def test_the_model_is_given_the_reply_text_and_no_case_data(
    reply_harness: ReplyHarness,
) -> None:
    """Not the case, not other evidence, not facts, not mandates, not contributor data.

    ``destination_display_label`` is the one value beyond the reply, and it is configuration
    rather than case data: the safe organization label the deployment already publishes as a
    non-secret environment variable, present because check 4 compares the model's ``obligor``
    with it (ADR-027 § 1 amendment). It names no mailbox and carries no address.
    """

    await reply_harness.prepare_sent()
    job, operations = await _started_job(reply_harness)
    await _worker(reply_harness, operations).execute(job)

    extractor = reply_harness.extractor
    assert isinstance(extractor, LiteralSpanCommitmentExtractor)
    payload = extractor.invocations[0].payload
    assert isinstance(payload, CommitmentExtractionInput)
    assert set(payload.model_dump()) == {
        "schema_version",
        "case_id",
        "source_evidence_id",
        "destination_display_label",
        "reply_text",
    }
    assert payload.destination_display_label == reply_harness.destination_label
    # No case on the envelope either: an extraction is bound to one immutable artifact rather
    # than to a version of a case.
    assert extractor.invocations[0].case_id is None


async def test_a_redelivered_job_recovers_without_a_second_model_call(
    reply_harness: ReplyHarness,
) -> None:
    """The durable agent-invocation record settles it. A committed apply costs zero calls."""

    await reply_harness.prepare_sent()
    job, operations = await _started_job(reply_harness)
    await _worker(reply_harness, operations).execute(job)

    extractor = reply_harness.extractor
    assert isinstance(extractor, LiteralSpanCommitmentExtractor)
    calls = len(extractor.invocations)

    replayed = await _worker(reply_harness, operations).execute(job)

    assert replayed.status is ApplicationOperationStatus.SUCCEEDED
    assert len(extractor.invocations) == calls


async def test_the_invocation_record_is_proof_only_for_the_exact_input(
    reply_harness: ReplyHarness,
) -> None:
    """A record that agreed about the invocation but not the input read something else."""

    await reply_harness.prepare_sent()
    job, _operations = await _started_job(reply_harness)
    items = await reply_harness.send.action.compile.core.load_evidence_items(
        reply_harness.scope, (job.evidence_id,)
    )
    text = items[0].extracted_text
    assert text is not None
    payload = CommitmentExtractionInput(
        case_id=job.case_id.value,
        source_evidence_id=job.evidence_id.value,
        destination_display_label=reply_harness.destination_label,
        reply_text=text.reveal(),
    )
    other = CommitmentExtractionInput(
        case_id=job.case_id.value,
        source_evidence_id=job.evidence_id.value,
        destination_display_label=reply_harness.destination_label,
        reply_text=text.reveal() + " and something else",
    )

    assert extraction_input_hash(payload) != extraction_input_hash(other)


@pytest.mark.parametrize(
    "field",
    [
        pytest.param("case_id", id=ExtractCommitmentJobBinding.CASE),
        pytest.param("actor_id_hash", id=ExtractCommitmentJobBinding.ACTOR),
        pytest.param("request_hash", id=ExtractCommitmentJobBinding.REQUEST),
        pytest.param("invocation_id", id=ExtractCommitmentJobBinding.INVOCATION),
        pytest.param("evidence_sha256", id=ExtractCommitmentJobBinding.BINDING),
    ],
)
async def test_a_misrouted_job_claims_nothing_and_invokes_nothing(
    reply_harness: ReplyHarness, field: str
) -> None:
    """A job is data on a queue, and data on a queue can be wrong."""

    await reply_harness.prepare_sent()
    job, operations = await _started_job(reply_harness)
    substitute: object
    if field == "case_id":
        from chorus.domain.ids import CaseId

        substitute = CaseId(uuid4())
    elif field == "actor_id_hash":
        substitute = Sha256Digest("sha256:" + "e" * 64)
    elif field in {"request_hash", "evidence_sha256"}:
        substitute = Sha256Digest("sha256:" + "d" * 64)
    else:
        substitute = uuid4()
    misrouted = dataclasses.replace(job, **{field: substitute})  # type: ignore[arg-type]

    operation = await _worker(reply_harness, operations).execute(misrouted)

    assert operation.status is ApplicationOperationStatus.PENDING
    extractor = reply_harness.extractor
    assert isinstance(extractor, LiteralSpanCommitmentExtractor)
    assert extractor.invocations == []


async def test_an_extraction_naming_another_case_is_refused_whole(
    reply_harness: ReplyHarness,
) -> None:
    """Five envelope refusals, and this is the one a misrouted run would hit."""

    from tests.fixtures.reply import scripted_extraction

    from chorus.contracts.commitment import CommitmentExtractionOutput

    await reply_harness.prepare_sent()
    reply_harness.extractor = scripted_extraction(
        CommitmentExtractionOutput(case_id=uuid4(), source_evidence_id=uuid4())
    )
    job, operations = await _started_job(reply_harness)

    operation = await _worker(reply_harness, operations).execute(job)

    assert operation.status is ApplicationOperationStatus.FAILED
    assert operation.error_code == "AGENT_CONTRACT_VIOLATION"
    case = await reply_harness.send.action.compile.core.load_case(
        CaseScope(
            namespace=reply_harness.scope.namespace,
            community_id=reply_harness.scope.community_id,
            case_id=reply_harness.scope.case_id,
        )
    )
    assert case.state is CaseState.ACTIONED
