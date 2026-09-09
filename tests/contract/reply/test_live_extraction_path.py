"""The real extraction path, from the accepted application envelope to the runtime dispatcher.

Two defects lived in the gap between the application and the deployed runtime, and both were
invisible to tests that stopped at either side of it.

* The application sends an extraction envelope with **no case on it** -- an extraction is bound
  to one immutable artifact, not to a version of a case (ADR-027 § 1). A runtime guard that
  required an envelope case rejected the only envelope the system ever sends.
* Check 4 compares the model's ``obligor`` with the safe destination label, and the frozen demo
  reply does not contain that label. A model given only the reply could satisfy the check only
  by accident.

So this file drives the actual chain -- ``ExtractCommitment`` → ``AgentCoreCommitmentExtraction
Agent`` → serialized ``investigator-request/v1`` → the Investigator runtime's dispatcher → a
stubbed model → back through the deterministic validator -- with no shape shortened and nothing
reconstructed by hand. The only fake is the model.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from runtimes.investigator import entrypoint
from runtimes.investigator.commitment_prompt import (
    derive_commitment_fence,
    render_commitment_user_message,
)
from tests.fixtures.reply import ReplyHarness

from chorus.application.commands.apply_commitment import (
    ApplyCommitmentCommand,
    ApplyCommitmentResult,
)
from chorus.application.commands.extract_commitment_operation import (
    ExtractCommitment,
    ExtractCommitmentJob,
)
from chorus.contracts.agentcore import (
    ExtractCommitmentRequest,
    InvestigateRequest,
    InvestigatorOperation,
)
from chorus.contracts.commitment import (
    CommitmentExtractionInput,
    CommitmentExtractionOutput,
    ProposedCommitmentDraft,
    SourceSpan,
)
from chorus.contracts.common import AgentName
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import Sha256Digest
from chorus.infrastructure.agentcore.commitment import AgentCoreCommitmentExtractionAgent
from chorus.infrastructure.fixtures.inbound_replies import MANAGER_HEDGE
from chorus.ports.agents import CommitmentExtractionInvocation, CommitmentExtractionResult

pytestmark = pytest.mark.anyio

PROFILE_HASH = "sha256:" + "cd" * 32

SPEAKER = "we will restore"
"""The frozen demo reply's own words for the party who will act, as they are stored.

Normalized, because the extraction is given the evidence item's ``extracted_text`` and the spans
index exactly those characters."""

DUE_DATE = "2030-01-14"


class RuntimeBackedInvoker:
    """An invoker that hands the payload to the **real** Investigator runtime dispatcher.

    Not a recording stub. The bytes the adapter produced are parsed by ``entrypoint.handle``,
    which is the code the deployed runtime runs, so a disagreement between what the application
    sends and what the runtime accepts fails here instead of on AWS.
    """

    def __init__(self, runner: StubModel) -> None:
        self.runner = runner
        self.payloads: list[bytes] = []

    def invoke(self, *, runtime_arn: str, session_id: str, payload: bytes) -> bytes:
        import anyio

        self.payloads.append(payload)
        return anyio.from_thread.run_sync(lambda: payload) and _dispatch(payload, self.runner)


def _dispatch(payload: bytes, runner: StubModel) -> bytes:
    """Run the runtime's own handler synchronously, from inside the invoker's worker thread."""

    import asyncio

    return asyncio.run(entrypoint.handle(payload, extraction_runner=runner, budget_seconds=5))


class StubModel:
    """Stands in for Nova. Records what it was shown and answers the way the prompt asks.

    ``obligor`` is the authoritative label the payload carried -- which is what the live prompt
    instructs -- while ``obligor_span`` still cites the words in the reply where the
    correspondent refers to themselves. That combination is what check 4 and check 3 together
    require, and getting it wrong in either direction is what the tests below detect.
    """

    model_id = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/investigator"

    def __init__(self, *, obligor: str | None = None) -> None:
        self.obligor = obligor
        self.payloads: list[CommitmentExtractionInput] = []
        self.messages: list[str] = []
        self.outputs: list[CommitmentExtractionOutput] = []
        self.calls = 0

    async def extract(
        self, payload: CommitmentExtractionInput, *, fence: str
    ) -> CommitmentExtractionOutput:
        self.calls += 1
        self.payloads.append(payload)
        self.messages.append(render_commitment_user_message(payload, fence=fence))
        text = payload.reply_text
        # The stored ``extracted_text`` is the normalized body, so the words the spans index are
        # the normalized ones. The model cites what it was actually shown.
        sentence_start = text.index(SPEAKER)
        sentence_end = text.index(".", sentence_start)
        date_start = text.index(DUE_DATE)
        answer = CommitmentExtractionOutput(
            case_id=payload.case_id,
            source_evidence_id=payload.source_evidence_id,
            commitments=(
                ProposedCommitmentDraft(
                    # The span cites the reply's own word for the speaker, "We".
                    obligor_span=SourceSpan(start=sentence_start, end=sentence_start + 2),
                    # start .. start + 2 is "we": the reply's own word for the speaker.
                    action_span=SourceSpan(start=sentence_start, end=sentence_end),
                    due_date_span=SourceSpan(start=date_start, end=date_start + 10),
                    # The field carries the authoritative label the payload supplied.
                    obligor=(
                        self.obligor
                        if self.obligor is not None
                        else payload.destination_display_label
                    ),
                    action_text=text[sentence_start:sentence_end],
                    due_at=_midnight("2030-01-14"),
                    refusal_detected=False,
                ),
            ),
        )
        self.outputs.append(answer)
        return answer


def _midnight(iso_date: str) -> datetime:
    return datetime.fromisoformat(iso_date).replace(tzinfo=UTC)


class RuntimeBackedExtractor:
    """The deployed adapter, pointed at the deployed dispatcher, over a stubbed model."""

    def __init__(self, model: StubModel) -> None:
        self.model = model
        self.invoker = RuntimeBackedInvoker(model)
        self.agent = AgentCoreCommitmentExtractionAgent(
            invoker=self.invoker,
            runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/chorus_investigator",
        )
        self.invocations: list[CommitmentExtractionInvocation] = []

    async def invoke_commitment_extraction(
        self, invocation: CommitmentExtractionInvocation
    ) -> CommitmentExtractionResult:
        self.invocations.append(invocation)
        return await self.agent.invoke_commitment_extraction(invocation)


async def _run(harness: ReplyHarness, extractor: RuntimeBackedExtractor) -> ApplyCommitmentResult:
    await harness.prepare_sent()
    ingested = await harness.ingest_reply(received_at=_near(harness))
    harness.extractor = extractor  # type: ignore[assignment]
    job = await harness.extraction_job(ingested)
    return await harness.extract().execute(job)


def _near(harness: ReplyHarness) -> datetime:
    """A received instant close enough to 2030-01-14 for check 8's 30-day range to admit it.

    The fixture corpus states a fixed date so the reviewed text is stable; the clock is what
    moves. Setting the harness clock too keeps ``uploaded_at`` and the range check reading the
    same world -- the same arrangement ``test_commitment_flow`` uses.
    """

    instant = datetime(2030, 1, 2, 9, 0, tzinfo=UTC)
    harness.send.action.compile.clock.instant = instant
    return instant


# -- P1-2: the accepted envelope reaches the runtime -------------------------------------------


async def test_the_accepted_extraction_envelope_reaches_the_model_exactly_once(
    reply_harness: ReplyHarness,
) -> None:
    """The envelope the application actually builds carries no case, and must be accepted."""

    model = StubModel()
    extractor = RuntimeBackedExtractor(model)

    result = await _run(reply_harness, extractor)

    sent = extractor.invocations[0]
    assert sent.case_id is None, "the accepted envelope names no case; the payload does"
    assert sent.case_version is None
    assert sent.agent_name is AgentName.INVESTIGATOR
    assert model.calls == 1, "exactly one model pass over one stranger's email"
    assert result.commitment_id is not None


async def test_the_serialized_request_declares_the_extraction_operation(
    reply_harness: ReplyHarness,
) -> None:
    """What went on the wire, read back from the bytes the adapter handed the invoker."""

    model = StubModel()
    extractor = RuntimeBackedExtractor(model)

    await _run(reply_harness, extractor)

    body = json.loads(extractor.invoker.payloads[0].decode("utf-8"))
    assert body["schema_version"] == "investigator-request/v1"
    assert body["operation"] == InvestigatorOperation.EXTRACT_COMMITMENT.value
    assert body["invocation"]["case_id"] is None
    assert body["invocation"]["payload"]["case_id"] is not None
    assert body["invocation"]["payload"]["source_evidence_id"] is not None


def test_the_extraction_arm_accepts_an_envelope_with_no_case(
    reply_harness: ReplyHarness,
) -> None:
    """Stated directly against the parser, so the guard cannot regress quietly."""

    from uuid import uuid4

    from chorus.contracts.common import AgentInputEnvelope

    request = ExtractCommitmentRequest(
        operation=InvestigatorOperation.EXTRACT_COMMITMENT,
        invocation=AgentInputEnvelope[CommitmentExtractionInput](
            invocation_id=uuid4(),
            namespace="DEMO",
            agent_name=AgentName.INVESTIGATOR,
            case_id=None,
            case_version=None,
            requested_at=reply_harness.send.action.compile.clock.now(),
            policy_version="policy/v1",
            payload=CommitmentExtractionInput(
                case_id=uuid4(),
                source_evidence_id=uuid4(),
                destination_display_label="Property Management",
                reply_text="we will restore elevator b to service by 2030-01-14.",
            ),
        ),
    )

    parsed = entrypoint.parse_request(request.model_dump_json().encode("utf-8"))

    assert isinstance(parsed, ExtractCommitmentRequest)
    assert parsed.invocation.case_id is None


def test_the_investigation_arm_still_requires_an_envelope_case(
    reply_harness: ReplyHarness,
) -> None:
    """The other operation is not weakened. An assessment of no case applies to nothing."""

    from tests.unit.runtime.test_investigator_extraction import investigation_request

    body = json.loads(investigation_request().model_dump_json())
    body["invocation"]["case_id"] = None
    body["invocation"]["case_version"] = None

    with pytest.raises(entrypoint.RuntimeContractError, match="exactly one case"):
        entrypoint.parse_request(json.dumps(body).encode("utf-8"))


def test_the_declared_operation_is_not_changed_by_the_payload(
    reply_harness: ReplyHarness,
) -> None:
    """A commitment payload labelled INVESTIGATE is refused, never rerouted to the arm that fits."""

    from uuid import uuid4

    from chorus.contracts.common import AgentInputEnvelope

    request = ExtractCommitmentRequest(
        operation=InvestigatorOperation.EXTRACT_COMMITMENT,
        invocation=AgentInputEnvelope[CommitmentExtractionInput](
            invocation_id=uuid4(),
            namespace="DEMO",
            agent_name=AgentName.INVESTIGATOR,
            case_id=None,
            case_version=None,
            requested_at=reply_harness.send.action.compile.clock.now(),
            policy_version="policy/v1",
            payload=CommitmentExtractionInput(
                case_id=uuid4(),
                source_evidence_id=uuid4(),
                destination_display_label="Property Management",
                reply_text="we will restore elevator b to service by 2030-01-14.",
            ),
        ),
    )
    body = json.loads(request.model_dump_json())
    body["operation"] = InvestigatorOperation.INVESTIGATE.value

    with pytest.raises(entrypoint.RuntimeContractError):
        entrypoint.parse_request(json.dumps(body).encode("utf-8"))

    assert InvestigateRequest.model_fields["operation"].annotation is not None


# -- P1-3: the demo reply, the safe label, and check 4 -----------------------------------------


async def test_the_frozen_demo_reply_produces_a_commitment_the_validator_accepts(
    reply_harness: ReplyHarness,
) -> None:
    """The end the whole repair is for.

    "we will restore elevator b to service by 2030-01-14." carries no organization name, so the
    only way check 4 can pass is for the authoritative label to reach the model as input. It
    does; the model restates it; the span still cites "We"; and a commitment is created.
    """

    model = StubModel()
    extractor = RuntimeBackedExtractor(model)

    result = await _run(reply_harness, extractor)

    payload = model.payloads[0]
    assert payload.destination_display_label == reply_harness.destination_label
    assert payload.destination_display_label not in payload.reply_text, (
        "the label is absent from the reply, which is why it has to be supplied"
    )
    proposal_start = payload.reply_text.index(SPEAKER)
    assert payload.reply_text[proposal_start : proposal_start + 2] == "we"
    assert result.commitment_id is not None
    assert result.rejection_codes == (), result.rejection_codes


async def test_a_model_that_invents_a_different_obligor_is_rejected(
    reply_harness: ReplyHarness,
) -> None:
    """Supplying the label does not make the model authoritative; check 4 still decides."""

    model = StubModel(obligor="Riverside Elevator Services")
    extractor = RuntimeBackedExtractor(model)

    result = await _run(reply_harness, extractor)

    assert result.commitment_id is None
    codes = set(result.rejection_codes)
    assert codes == {"COMMITMENT_OBLIGOR_MISMATCH"}, codes


async def test_the_rendered_prompt_states_the_label_outside_the_fence(
    reply_harness: ReplyHarness,
) -> None:
    """It is configuration the correlation established, not something the reply said."""

    model = StubModel()
    extractor = RuntimeBackedExtractor(model)

    await _run(reply_harness, extractor)

    payload = model.payloads[0]
    fence = derive_commitment_fence(payload, extractor.invocations[0].invocation_id)
    rendered = model.messages[0]

    assert f"CORRESPONDENT: {payload.destination_display_label}" in rendered
    # The fenced block is everything between the markers around the reply. The label must sit
    # outside it: inside would tell the model to read a fact as a quotation.
    opening, closing = f"<<<{fence}", f"{fence}>>>"
    quoted = rendered[rendered.rindex(opening) + len(opening) : rendered.rindex(closing)]
    assert quoted == payload.reply_text
    assert "CORRESPONDENT" not in quoted
    assert payload.destination_display_label not in quoted


# -- P1-3: the label is part of the deterministic binding ---------------------------------------


async def test_the_label_is_bound_into_the_input_hash(reply_harness: ReplyHarness) -> None:
    """An extraction produced under one correspondent is not proof for another."""

    from chorus.application.commands.extract_commitment_operation import extraction_input_hash

    model = StubModel()
    extractor = RuntimeBackedExtractor(model)
    await _run(reply_harness, extractor)
    payload = model.payloads[0]

    relabelled = payload.model_copy(update={"destination_display_label": "Another Manager"})

    assert extraction_input_hash(payload) != extraction_input_hash(relabelled)


# -- P2-A: a completed replay must still be about the request that is being made ---------------


LABEL_A = "Property Management"
LABEL_B = "Another Manager"


async def _prepared(harness: ReplyHarness) -> ExtractCommitmentJob:
    """One sent case, one ingested reply, one durable extraction job -- prepared once.

    The job carries the ``invocation_id`` and the request binding, so re-running the operation
    with this same object is the real redelivery path rather than a second, unrelated request.
    """

    await harness.prepare_sent()
    ingested = await harness.ingest_reply(received_at=_near(harness))
    return await harness.extraction_job(ingested)


def _extraction_under(
    harness: ReplyHarness, extractor: RuntimeBackedExtractor, label: str
) -> ExtractCommitment:
    """The harness's own composition, re-pointed at one safe destination label.

    Both halves move together -- the label the model is *shown* and the label check 4 judges it
    against are one deployment value, and a test that changed only one of them would be testing
    a configuration this system cannot have.
    """

    harness.extractor = extractor  # type: ignore[assignment]
    return replace(
        harness.extract(),
        destination_label=label,
        apply=replace(harness.apply_commitment(), destination_label=label),
    )


async def test_a_completed_commitment_is_not_replayed_under_a_changed_label(
    reply_harness: ReplyHarness,
) -> None:
    """Astra P2-A, reproduced as two real executions rather than as a hash comparison.

    The model here is pinned to the *first* label, so its answer is byte-identical across both
    runs. That is the dangerous shape: ``output_hash`` does not move, the request-hash gate is
    satisfied, and before the repair the second run was handed the first run's commitment with
    ``replayed=True`` under an input the model had never been shown.

    The extraction genuinely runs a second time -- ``_recovered`` refuses the durable record
    because the recomputed input hash now includes ``Another Manager`` -- and the apply must then
    refuse the completed result for the same reason, rather than serving it.
    """

    model = StubModel(obligor=LABEL_A)
    extractor = RuntimeBackedExtractor(model)
    job = await _prepared(reply_harness)

    first = await _extraction_under(reply_harness, extractor, LABEL_A).execute(job)

    assert first.commitment_id is not None
    assert first.replayed is False
    assert model.calls == 1

    with pytest.raises(IntegrityError):
        await _extraction_under(reply_harness, extractor, LABEL_B).execute(job)

    # The second run did reach the model -- the pre-model recovery correctly refused the record --
    # so the refusal above is the *apply* path's, which is the one that was missing.
    assert model.calls == 2
    assert model.payloads[0].destination_display_label == LABEL_A
    assert model.payloads[1].destination_display_label == LABEL_B


async def test_the_stale_commitment_survives_the_refusal_untouched(
    reply_harness: ReplyHarness,
) -> None:
    """A refused replay changes nothing: no second commitment, no altered case."""

    model = StubModel(obligor=LABEL_A)
    extractor = RuntimeBackedExtractor(model)
    job = await _prepared(reply_harness)

    first = await _extraction_under(reply_harness, extractor, LABEL_A).execute(job)
    assert first.commitment_id is not None
    case_before = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)

    with pytest.raises(IntegrityError):
        await _extraction_under(reply_harness, extractor, LABEL_B).execute(job)

    live = await reply_harness.send.action.compile.shareable.load_live_commitment(
        reply_harness.scope, job.action_id
    )
    case_after = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)

    assert live is not None
    assert live.commitment_id == first.commitment_id, "no second commitment for this action"
    assert case_after.version == case_before.version
    assert case_after.state is case_before.state


async def test_an_unchanged_input_still_replays_the_same_commitment(
    reply_harness: ReplyHarness,
) -> None:
    """The control. Idempotency is not weakened -- only the right to claim it is checked.

    Same job, same label, same everything: the durable record is proof, the model is not asked a
    second time about a stranger's email, and the identical commitment comes back.
    """

    model = StubModel(obligor=LABEL_A)
    extractor = RuntimeBackedExtractor(model)
    job = await _prepared(reply_harness)

    first = await _extraction_under(reply_harness, extractor, LABEL_A).execute(job)
    second = await _extraction_under(reply_harness, extractor, LABEL_A).execute(job)

    assert first.commitment_id is not None
    assert second.commitment_id == first.commitment_id
    assert second.replayed is True
    assert second.rejection_codes == ()
    assert model.calls == 1, "a proven replay costs no second model pass"
    assert second.case_state is first.case_state
    assert second.case_version == first.case_version


async def test_a_completed_commitment_is_not_replayed_under_any_other_changed_input(
    reply_harness: ReplyHarness,
) -> None:
    """The same invariant, reached without going through the label.

    ``ApplyCommitment`` is called directly with the completed record's exact ``output_hash`` --
    so the request-hash gate is satisfied -- and an ``input_hash`` that disagrees with the
    durable invocation record. This is the commitment-branch twin of the accepted
    no-commitment test in ``test_commitment_flow``, which was the only branch guarded before.
    """

    model = StubModel(obligor=LABEL_A)
    extractor = RuntimeBackedExtractor(model)
    job = await _prepared(reply_harness)
    first = await _extraction_under(reply_harness, extractor, LABEL_A).execute(job)
    assert first.commitment_id is not None

    invocation = await reply_harness.send.action.compile.core.load_agent_invocation(
        reply_harness.scope, job.invocation_id
    )
    assert invocation is not None
    assert invocation.output_hash is not None

    foreign = ApplyCommitmentCommand(
        namespace=job.namespace,
        community_id=job.community_id,
        case_id=job.case_id,
        action_id=job.action_id,
        evidence_id=job.evidence_id,
        invocation_id=job.invocation_id,
        correlation_id=job.correlation_id,
        actor_id_hash=job.actor_id_hash,
        output=model.outputs[0],
        input_hash=Sha256Digest(f"sha256:{'0' * 64}"),
        output_hash=invocation.output_hash,
    )

    with pytest.raises(IntegrityError):
        await replace(reply_harness.apply_commitment(), destination_label=LABEL_A).execute(foreign)


async def test_the_no_commitment_replay_binding_is_unchanged(
    reply_harness: ReplyHarness,
) -> None:
    """The branch that was already correct still is -- both now go through one helper."""

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

    foreign = ApplyCommitmentCommand(
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
        await reply_harness.apply_commitment().execute(foreign)
