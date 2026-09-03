"""Corrupting a frozen plan, one field at a time, and proving every version fails closed.

A validated-plan snapshot is the one artefact in the Monitor lifecycle that authorises writes
without re-asking the model. Whatever it says will be applied. So the question these tests ask
is not "does it round-trip" but "what would an attacker, or a bad merge, or a half-finished
migration have to change to get a *different* answer applied under this invocation's identity",
and the answer has to be "nothing that works".

The corruptions come in three strengths, and the earlier loader only survived the first:

* **naive** -- edit a manifest field. Caught, because the manifest and the document restate the
  same provenance and now have to agree.
* **consistent** -- edit the manifest field *and* its twin inside the document *and* the
  document digest *and* the provenance digest, so every stored copy agrees with every other.
  Caught anyway, because the loader recomputes the output hash from the stored answer and the
  plan hash from the stored steps, and checks the prompt version, operation, and invocation
  against what this process actually holds. Metadata cannot describe content it does not match.
* **substitution** -- keep every hash internally consistent and move the whole snapshot: to
  another operation, to another invocation, or beside a different frozen input. Caught, because
  the identity being proved is not "is this a well-formed plan" but "is this *the* plan this
  operation's own invocation produced from this operation's own frozen input".

Every failure is :class:`IntegrityError`, before any apply, with no model retry and no repair.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.fixtures.faults import (
    FaultInjectingDriver,
    TransactBehaviour,
    monitor_apply_steps,
)
from tests.fixtures.monitor import MonitorHarness
from tests.fixtures.monitor_answers import THREE_GROUPS, grouped_answer

from chorus.application.services.monitor_snapshots import (
    FrozenMonitorPlan,
    MonitorPlanProvenance,
    MonitorSnapshots,
    chunk_canonical_bytes,
)
from chorus.domain.entities import ApplicationOperationStatus
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import OperationId, SensitiveStr, Sha256Digest
from chorus.domain.time import epoch_seconds_ceiling
from chorus.infrastructure.dynamodb import codec_core
from chorus.infrastructure.local.monitor_agent import ScriptedMonitorAgent
from chorus.ports.ambient import AmbientMessage
from chorus.ports.errors import CrossCaseViolationError
from chorus.ports.operations import MonitorOperationJob
from chorus.ports.records import (
    MAX_SNAPSHOT_CHUNK_BYTES,
    MessageFeedEntry,
    MonitorSnapshotChunk,
    MonitorSnapshotKind,
    MonitorSnapshotManifest,
)
from chorus.ports.scopes import CommunityScope, OperationScope
from chorus.ports.storage import DeleteItem, ItemKey, KeyPresent, PutItem
from chorus.ports.unit_of_work import TransactionPlan
from chorus.privacy.canonical import canonical_bytes, hash_value

pytestmark = pytest.mark.anyio

FORGED_DIGEST = Sha256Digest("sha256:" + "f" * 64)
"""The exact value Codex used to prove the old loader trusted its metadata."""


# ---------------------------------------------------------------------------------------
# Building one real, healthy plan snapshot
# ---------------------------------------------------------------------------------------


def _responder() -> ScriptedMonitorAgent:
    return ScriptedMonitorAgent(
        responder=lambda invocation: grouped_answer(invocation.payload, THREE_GROUPS)
    )


def _snapshots(harness: MonitorHarness) -> MonitorSnapshots:
    return MonitorSnapshots(core=harness.core, unit_of_work=harness.unit_of_work)


def _scope(harness: MonitorHarness, job: MonitorOperationJob) -> OperationScope:
    return OperationScope(namespace=harness.namespace, operation_id=job.operation_id)


async def _applied(harness: MonitorHarness) -> MonitorOperationJob:
    """Run one Monitor operation to completion, leaving both snapshots healthy behind it."""

    await harness.seed()
    locators = await harness.ingest_feed()
    _, job = await harness.dispatched(locators)
    finished = await harness.worker(_responder()).execute(job)
    assert finished.status is ApplicationOperationStatus.SUCCEEDED
    return job


async def _load(harness: MonitorHarness, job: MonitorOperationJob) -> FrozenMonitorPlan | None:
    """Load the plan exactly as the use case does: proved against its own frozen input."""

    snapshots = _snapshots(harness)
    scope = _scope(harness, job)
    frozen_input = await snapshots.load_input(scope, job.invocation_id)
    assert frozen_input is not None
    return await snapshots.load_plan(scope, job.invocation_id, frozen_input=frozen_input)


# ---------------------------------------------------------------------------------------
# Raw storage surgery -- the only way to write a snapshot the writer would never produce
# ---------------------------------------------------------------------------------------


def _manifest_key(
    harness: MonitorHarness,
    job: MonitorOperationJob,
    kind: MonitorSnapshotKind = MonitorSnapshotKind.MONITOR_PLAN,
) -> ItemKey:
    return codec_core.monitor_snapshot_manifest_key(_scope(harness, job), kind, job.invocation_id)


async def _read_item(harness: MonitorHarness, key: ItemKey) -> dict[str, Any]:
    item = await harness.driver.get_item(key, consistent=True)
    assert item is not None
    return dict(item)


async def _overwrite(harness: MonitorHarness, key: ItemKey, item: dict[str, Any]) -> None:
    await harness.driver.write_item(PutItem(key=key, item=item, condition=KeyPresent()))


async def _edit_manifest(harness: MonitorHarness, job: MonitorOperationJob, **changes: Any) -> None:
    """Rewrite manifest attributes and nothing else -- the naive corruption."""

    key = _manifest_key(harness, job)
    item = await _read_item(harness, key)
    item.update(changes)
    await _overwrite(harness, key, item)


async def _read_document(harness: MonitorHarness, job: MonitorOperationJob) -> dict[str, Any]:
    scope = _scope(harness, job)
    manifest = await harness.core.load_monitor_snapshot_manifest(
        scope, kind=MonitorSnapshotKind.MONITOR_PLAN, invocation_id=job.invocation_id
    )
    assert manifest is not None
    chunks = await harness.core.load_monitor_snapshot_chunks(scope, manifest)
    raw = "".join(chunk.content.reveal() for chunk in chunks).encode("utf-8")
    decoded: dict[str, Any] = json.loads(raw.decode("utf-8"))
    return decoded


async def _rewrite_document(
    harness: MonitorHarness,
    job: MonitorOperationJob,
    document: dict[str, Any],
    *,
    mirror_manifest: bool = True,
) -> None:
    """Store a document as the writer would have, with a manifest that fully agrees with it.

    This is the *consistent* corruption. Byte length, chunk count, content digest, every
    manifest scalar, and the provenance digest are all recomputed from the edited document, so
    nothing in the stored state disagrees with anything else. What remains is the only thing
    that can still catch it: recomputation from content.
    """

    scope = _scope(harness, job)
    manifest_key = _manifest_key(harness, job)
    stored = await _read_item(harness, manifest_key)
    raw = canonical_bytes(document)
    chunks = chunk_canonical_bytes(raw)

    stored["content_sha256"] = hash_value(document).value
    stored["byte_length"] = len(raw)
    stored["chunk_count"] = len(chunks)
    if mirror_manifest:
        provenance = MonitorPlanProvenance(
            operation_id=OperationId(UUID(str(document["operation_id"]))),
            invocation_id=UUID(str(document["invocation_id"])),
            input_hash=Sha256Digest(str(document["input_hash"])),
            output_hash=Sha256Digest(str(document["output_hash"])),
            plan_hash=Sha256Digest(str(document["plan_hash"])),
            prompt_version=str(document["prompt_version"]),
            model_profile_hash=Sha256Digest(str(document["model_profile_hash"])),
        )
        stored["input_hash"] = provenance.input_hash.value
        stored["output_hash"] = provenance.output_hash.value
        stored["plan_hash"] = provenance.plan_hash.value
        stored["prompt_version"] = provenance.prompt_version
        stored["model_profile_hash"] = provenance.model_profile_hash.value
        stored["provenance_hash"] = provenance.digest.value
    await _overwrite(harness, manifest_key, stored)

    expires = int(stored["expires_at_epoch"])
    operations = tuple(
        harness.core.stage_create_monitor_snapshot_chunk(
            scope,
            MonitorSnapshotChunk(
                invocation_id=job.invocation_id,
                operation_id=job.operation_id,
                namespace=harness.namespace,
                community_id=harness.community_id,
                kind=MonitorSnapshotKind.MONITOR_PLAN,
                index=index,
                content=SensitiveStr(content),
                expires_at_epoch=expires,
            ),
        )
        for index, content in enumerate(chunks)
    )
    for operation in operations:
        await harness.driver.write_item(
            PutItem(key=operation.key, item=operation.item, condition=KeyPresent())
        )


# ---------------------------------------------------------------------------------------
# The healthy snapshot loads, so every refusal below is about the corruption
# ---------------------------------------------------------------------------------------


async def test_an_untouched_plan_snapshot_loads_and_proves_itself(
    harness: MonitorHarness,
) -> None:
    plan = await _load(harness, await _applied(harness))
    assert plan is not None
    assert plan.steps


# ---------------------------------------------------------------------------------------
# Naive corruption -- one manifest field
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attribute",
    [
        "input_hash",
        "output_hash",
        "plan_hash",
        "model_profile_hash",
        "provenance_hash",
    ],
)
async def test_a_forged_manifest_digest_is_refused(harness: MonitorHarness, attribute: str) -> None:
    """Codex's exact probe, generalised across every digest the manifest carries."""

    job = await _applied(harness)
    await _edit_manifest(harness, job, **{attribute: FORGED_DIGEST.value})

    with pytest.raises(IntegrityError):
        await _load(harness, job)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        pytest.param("prompt_version", "monitor/v0", id="prompt_version"),
        pytest.param("operation_id", "00000000-0000-4000-8000-000000000001", id="operation_id"),
        pytest.param("invocation_id", "00000000-0000-4000-8000-000000000002", id="invocation_id"),
    ],
)
async def test_a_forged_manifest_identity_field_is_refused(
    harness: MonitorHarness, attribute: str, value: str
) -> None:
    """Two closed refusals share this case, and either is a correct fail-closed answer.

    An identity moved on the manifest is caught by the repository's own address guard before
    the snapshot service ever sees it -- a row whose body claims an operation or invocation
    other than the one it was found under is a cross-scope violation, which is the older and
    broader rule. A prompt version has no such guard and reaches the provenance check. Both
    end the load with nothing applied, which is the property under test.
    """

    job = await _applied(harness)
    await _edit_manifest(harness, job, **{attribute: value})

    with pytest.raises((IntegrityError, CrossCaseViolationError)):
        await _load(harness, job)


# ---------------------------------------------------------------------------------------
# Consistent corruption -- every stored copy agrees, and it still fails
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("output_hash", FORGED_DIGEST.value, id="output_hash"),
        pytest.param("plan_hash", FORGED_DIGEST.value, id="plan_hash"),
        pytest.param("input_hash", FORGED_DIGEST.value, id="input_hash"),
        pytest.param("prompt_version", "monitor/v0", id="prompt_version"),
        pytest.param("model_profile_hash", FORGED_DIGEST.value, id="model_profile_hash"),
        pytest.param("operation_id", "00000000-0000-4000-8000-000000000003", id="operation_id"),
        pytest.param("invocation_id", "00000000-0000-4000-8000-000000000004", id="invocation_id"),
    ],
)
async def test_a_wholly_self_consistent_forgery_is_still_refused(
    harness: MonitorHarness, field: str, value: str
) -> None:
    """The attack the old loader could not see: change the value *everywhere* it is stored.

    Manifest, document, document digest, and provenance digest all agree afterwards. The
    refusal comes from the two things no amount of internal consistency can supply -- the hash
    recomputed from the content itself, and the identity this process independently holds.
    """

    job = await _applied(harness)
    document = await _read_document(harness, job)
    document[field] = value
    await _rewrite_document(harness, job, document)

    with pytest.raises(IntegrityError):
        await _load(harness, job)


async def test_an_edited_answer_whose_hashes_were_left_alone_is_refused(
    harness: MonitorHarness,
) -> None:
    """Content moved and metadata did not, which is the plainest tampering there is."""

    job = await _applied(harness)
    document = await _read_document(harness, job)
    result = json.loads(str(document["result"]))
    reports = result["output"]["proposed_reports"]
    assert reports, "the scenario needs an answer with something in it to edit"
    reports[0]["summary"] = "A different summary than the model actually produced."
    document["result"] = json.dumps(result)
    await _rewrite_document(harness, job, document, mirror_manifest=False)

    with pytest.raises(IntegrityError):
        await _load(harness, job)


async def test_edited_plan_steps_are_refused_even_with_a_matching_document_digest(
    harness: MonitorHarness,
) -> None:
    """Dropping a step would silently narrow what a resumed apply believes it still owes."""

    job = await _applied(harness)
    document = await _read_document(harness, job)
    steps = list(document["steps"])
    assert len(steps) > 1
    document["steps"] = steps[:-1]
    await _rewrite_document(harness, job, document, mirror_manifest=False)

    with pytest.raises(IntegrityError):
        await _load(harness, job)


# ---------------------------------------------------------------------------------------
# Substitution -- a perfectly valid snapshot, in the wrong place
# ---------------------------------------------------------------------------------------


async def _second_operation(harness: MonitorHarness) -> MonitorOperationJob:
    """A second completed Monitor operation over its own batch, in the same community."""

    scope = CommunityScope(namespace=harness.namespace, community_id=harness.community_id)
    newest = await harness.core.read_recent_messages(
        scope, before=harness.adapter.messages()[-1].sent_at + timedelta(days=365), limit=1
    )
    last = newest[0].sent_at if newest else harness.adapter.messages()[-1].sent_at
    pseudonyms = [seed.pseudonym for seed in harness.adapter.contributor_seeds]
    batch = tuple(
        AmbientMessage(
            adapter="SYNTHETIC",
            channel_message_id=f"second-{index:03d}",
            contributor_pseudonym=pseudonyms[index % len(pseudonyms)],
            sent_at=last + timedelta(minutes=index + 1),
            text=f"The lift stopped between floors again (second {index}).",
        )
        for index in range(4)
    )
    result = await harness.ingest_messages(batch, idempotency_key="second-operation-0001")
    sent_at = {message.channel_message_id: message.sent_at for message in batch}
    locators = tuple(
        MessageFeedEntry(message_id=item.message_id, sent_at=sent_at[item.channel_message_id])
        for item in result.messages
    )
    _, job = await harness.dispatched(locators)
    await harness.worker(_responder()).execute(job)
    return job


async def test_a_plan_snapshot_moved_to_another_operation_is_refused(
    harness: MonitorHarness,
) -> None:
    """A whole, valid, self-consistent snapshot -- belonging to a different operation."""

    first = await _applied(harness)
    second = await _second_operation(harness)

    donor = await _read_item(harness, _manifest_key(harness, first))
    target_key = _manifest_key(harness, second)
    stored = await _read_item(harness, target_key)
    for field in ("content_sha256", "byte_length", "chunk_count", "provenance_hash"):
        stored[field] = donor[field]
    for field in ("input_hash", "output_hash", "plan_hash", "model_profile_hash"):
        stored[field] = donor[field]
    await _overwrite(harness, target_key, stored)

    with pytest.raises(IntegrityError):
        await _load(harness, second)


async def test_a_plan_whose_input_hash_names_another_frozen_input_is_refused(
    harness: MonitorHarness,
) -> None:
    """The check that makes swapping a plan between two invocations detectable.

    Both snapshots here are real. The plan is internally flawless -- its digests all agree, its
    steps hash correctly, its answer hashes correctly. It simply says it was reasoned from an
    input that is not the input actually sitting beside it, and applying it would be applying
    an answer to a question this operation never asked.
    """

    first = await _applied(harness)
    second = await _second_operation(harness)

    donor_manifest = await harness.core.load_monitor_snapshot_manifest(
        _scope(harness, first),
        kind=MonitorSnapshotKind.MONITOR_PLAN,
        invocation_id=first.invocation_id,
    )
    assert donor_manifest is not None

    document = await _read_document(harness, second)
    document["input_hash"] = donor_manifest.input_hash.value
    await _rewrite_document(harness, second, document)

    with pytest.raises(IntegrityError):
        await _load(harness, second)


async def test_a_refused_plan_is_refused_before_anything_is_applied(
    harness: MonitorHarness,
) -> None:
    """A corrupt plan never becomes a partially applied one, and never becomes a model call."""

    await harness.seed()
    locators = await harness.ingest_feed()
    _, job = await harness.dispatched(locators)

    # Freeze the plan without applying it, by failing the first apply step outright.
    faulty = FaultInjectingDriver(
        inner=harness.driver,
        script=[TransactBehaviour.DEFINITE_FAILURE],
        scripted=monitor_apply_steps,
    )
    interrupted = await (
        MonitorHarness(driver=faulty, namespace=harness.namespace, clock=harness.clock)
        .worker(_responder())
        .execute(job)
    )
    assert interrupted.status is ApplicationOperationStatus.PENDING

    await _edit_manifest(harness, job, output_hash=FORGED_DIGEST.value)

    agent = _responder()
    settled = await harness.worker(agent).execute(job)

    assert settled.status is ApplicationOperationStatus.FAILED
    assert agent.invocations == [], "a corrupt plan is never repaired by asking the model again"
    progress = await harness.core.load_monitor_progress(_scope(harness, job), job.invocation_id)
    assert progress is None, "nothing was applied"


# ---------------------------------------------------------------------------------------
# Reassembly integrity -- the checks that were already there, kept and widened
# ---------------------------------------------------------------------------------------


async def _chunk_key(harness: MonitorHarness, job: MonitorOperationJob, index: int) -> ItemKey:
    return codec_core.monitor_snapshot_chunk_key(
        _scope(harness, job), MonitorSnapshotKind.MONITOR_PLAN, job.invocation_id, index
    )


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param("missing_chunk", id="missing_chunk"),
        pytest.param("altered_chunk", id="altered_chunk"),
        pytest.param("chunk_count", id="manifest_count_mismatch"),
        pytest.param("byte_length", id="manifest_byte_length_mismatch"),
        pytest.param("content_sha256", id="manifest_content_hash_mismatch"),
    ],
)
async def test_reassembly_failures_are_integrity_failures(
    harness: MonitorHarness, corrupt: str
) -> None:
    job = await _applied(harness)
    key = await _chunk_key(harness, job, 0)

    match corrupt:
        case "missing_chunk":
            await harness.driver.write_item(DeleteItem(key=key, condition=KeyPresent()))
        case "altered_chunk":
            item = await _read_item(harness, key)
            content = str(item["content"])
            item["content"] = content[:-1] + ("x" if content[-1] != "x" else "y")
            await _overwrite(harness, key, item)
        case "chunk_count":
            await _edit_manifest(harness, job, chunk_count=2)
        case "byte_length":
            await _edit_manifest(harness, job, byte_length=17)
        case "content_sha256":
            await _edit_manifest(harness, job, content_sha256=FORGED_DIGEST.value)

    with pytest.raises(IntegrityError):
        await _load(harness, job)


async def test_a_snapshot_near_the_payload_bound_is_chunked_and_proved(
    harness: MonitorHarness,
) -> None:
    """The large end of the range, written and read back through the real repository.

    The frozen payload bound is 1 MiB and one DynamoDB item is 400 KiB, so a snapshot at the
    contract maxima *must* span chunks. This writes one deliberately close to the bound so the
    multi-chunk path is exercised by a real value rather than by a unit-level fake -- and so
    the observed item size stays visibly inside the margin Codex measured.
    """

    await harness.seed()
    scope = OperationScope(namespace=harness.namespace, operation_id=harness.operation_id("large"))
    snapshots = _snapshots(harness)

    invocation_id = uuid4()
    filler = "e" * (2 * MAX_SNAPSHOT_CHUNK_BYTES + 1_000)
    document = {"schema": "test-large/v1", "filler": filler}
    raw = canonical_bytes(document)
    chunks = chunk_canonical_bytes(raw)

    assert len(chunks) == 3, "the payload spans several items, which is the point"
    assert all(len(chunk.encode("utf-8")) <= MAX_SNAPSHOT_CHUNK_BYTES for chunk in chunks)
    assert max(len(chunk.encode("utf-8")) for chunk in chunks) <= 300_000, (
        "the observed item stays well inside the 400 KiB DynamoDB limit"
    )

    now = harness.clock.now()
    expires = epoch_seconds_ceiling(now + timedelta(days=7))
    manifest = MonitorSnapshotManifest(
        invocation_id=invocation_id,
        operation_id=scope.operation_id,
        namespace=harness.namespace,
        community_id=harness.community_id,
        kind=MonitorSnapshotKind.MONITOR_INPUT,
        content_sha256=hash_value(document),
        byte_length=len(raw),
        chunk_count=len(chunks),
        input_hash=hash_value(document),
        prompt_version="monitor/v1",
        created_at=now,
        expires_at_epoch=expires,
    )
    await harness.unit_of_work.commit(
        TransactionPlan(
            name="large-snapshot",
            operations=(
                harness.core.stage_create_monitor_snapshot_manifest(scope, manifest),
                *(
                    harness.core.stage_create_monitor_snapshot_chunk(
                        scope,
                        MonitorSnapshotChunk(
                            invocation_id=invocation_id,
                            operation_id=scope.operation_id,
                            namespace=harness.namespace,
                            community_id=harness.community_id,
                            kind=MonitorSnapshotKind.MONITOR_INPUT,
                            index=index,
                            content=SensitiveStr(content),
                            expires_at_epoch=expires,
                        ),
                    )
                    for index, content in enumerate(chunks)
                ),
            ),
            audit_required=False,
        )
    )

    stored = await harness.core.load_monitor_snapshot_chunks(scope, manifest)
    rebuilt = "".join(chunk.content.reveal() for chunk in stored).encode("utf-8")
    assert rebuilt == raw
    assert snapshots is not None


def test_the_corruption_matrix_covers_every_provenance_field() -> None:
    """A field added to the provenance without a corruption case would go unproved."""

    covered = {
        "operation_id",
        "invocation_id",
        "input_hash",
        "output_hash",
        "plan_hash",
        "prompt_version",
        "model_profile_hash",
    }
    assert {item.name for item in fields(MonitorPlanProvenance)} == covered
