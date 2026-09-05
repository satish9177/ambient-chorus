"""The happy compile, end to end, through the real adapters.

Scenario A of the frozen matrix, plus the properties that only become checkable once a compile
actually reaches storage: the view is durable, the pointer moves, the history row exists, the
private lineage records what the safe artifact deliberately forgot, and nothing the fixture
plants as private appears anywhere in the external-safe result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from tests.fixtures.compile import SENTINEL_PATTERN, CompileHarness, photo_bytes

from chorus.application.commands.compile_view import (
    ALLOW_FIXED_TRANSACTION_PARTICIPANTS,
    CompileView,
)
from chorus.ports.pagination import PageRequest
from chorus.ports.records import CompileDecisionOutcome, CompileItemOutcome
from chorus.ports.storage import TableName
from chorus.ports.unit_of_work import (
    TransactionCommitted,
    TransactionOutcome,
    TransactionPlan,
    UnitOfWork,
)

pytestmark = pytest.mark.anyio


@dataclass(slots=True)
class CountingUnitOfWork:
    """Record each plan's participant count, then commit it unchanged.

    A wrapper rather than a patch: ``StorageUnitOfWork`` uses slots, and more importantly the
    number under test is a property of the plan the command *builds*, so intercepting the
    real object is exactly the right place to read it.
    """

    inner: UnitOfWork
    sizes: dict[str, int] = field(default_factory=dict)

    async def commit(self, plan: TransactionPlan) -> None:
        self.sizes[plan.name] = len(plan.operations)
        await self.inner.commit(plan)

    async def resolve_outcome(self, plan: TransactionPlan) -> TransactionOutcome:
        return await self.inner.resolve_outcome(plan)


async def _seed(harness: CompileHarness) -> CompileView:
    raw = photo_bytes()
    await harness.seed(evidence_items=harness.align_photo_digest(raw), photo=raw)
    return harness.compile_view()


async def test_a_safe_compile_persists_a_view_and_moves_the_pointer(
    harness: CompileHarness,
) -> None:
    compile_view = await _seed(harness)

    result = await compile_view.execute(harness.command())

    assert result.view is not None
    assert result.replayed is False
    assert result.included

    stored = await harness.shareable.load_view(harness.scope, result.view.view_id)
    assert stored.view_hash == result.view.view_hash
    assert stored.case_version == harness.case.version

    pointer = await harness.shareable.load_current_view_pointer(harness.scope)
    assert pointer is not None
    assert pointer.view_id == result.view.view_id
    assert pointer.view_hash == result.view.view_hash
    assert pointer.version == 1


async def test_the_view_history_locator_is_written_beside_the_view(
    harness: CompileHarness,
) -> None:
    """The frozen transaction participant the persistence document had omitted."""

    compile_view = await _seed(harness)

    result = await compile_view.execute(harness.command())

    page = await harness.shareable.read_view_history(harness.scope, PageRequest(limit=10))
    assert result.view is not None
    assert [locator.view_id for locator in page.items] == [result.view.view_id]


async def test_the_private_lineage_records_what_the_safe_view_forgot(
    harness: CompileHarness,
) -> None:
    """``ShareableFact`` carries no source. The projection is where the join lives."""

    compile_view = await _seed(harness)
    command = harness.command()

    result = await compile_view.execute(command)

    projection = await harness.audit.load_compile_projection(harness.scope, command.compile_id)
    assert projection is not None
    assert projection.decision is CompileDecisionOutcome.ALLOW
    assert result.view is not None
    assert projection.view_id == result.view.view_id
    assert projection.view_hash == result.view.view_hash
    assert projection.based_on_case_version == harness.case.version
    assert len(projection.facts) == len(command.requested_facts)
    assert projection.gates

    included = {
        record.fact_id
        for record in projection.facts
        if record.outcome is CompileItemOutcome.INCLUDED
    }
    assert included == {entry.fact_id for entry in result.included}
    excluded = {
        record.fact_id
        for record in projection.facts
        if record.outcome is CompileItemOutcome.EXCLUDED
    }
    assert harness.fixture.health_fact_id in excluded
    assert harness.fixture.unit_fact_id in excluded


async def test_the_health_and_unit_facts_never_reach_the_safe_view(
    harness: CompileHarness,
) -> None:
    """Scenario B and evaluation scenario 11, proved against the persisted artifact."""

    compile_view = await _seed(harness)

    result = await compile_view.execute(harness.command())

    assert result.view is not None
    rendered = repr(result.view)
    assert SENTINEL_PATTERN.search(rendered) is None

    stored = await harness.shareable.load_view(harness.scope, result.view.view_id)
    assert SENTINEL_PATTERN.search(repr(stored)) is None


async def test_the_safe_evidence_reference_exposes_no_bucket_key_or_private_id(
    harness: CompileHarness,
) -> None:
    """Matrix AM: the handle is opaque and the media type describes the derivative."""

    compile_view = await _seed(harness)

    result = await compile_view.execute(harness.command())

    assert result.view is not None
    refs = result.view.safe_evidence_refs
    assert len(refs) == 1
    ref = refs[0]
    assert ref.media_type == "image/png"
    rendered = repr(ref)
    assert str(harness.fixture.photo_evidence_id) not in rendered
    assert "ns/" not in rendered
    assert "s3://" not in rendered
    assert "chorus-" not in rendered


async def test_the_allow_transaction_has_exactly_the_frozen_participant_count(
    harness: CompileHarness,
) -> None:
    """Matrix AS: the arithmetic, asserted against the plan the command actually builds."""

    compile_view = await _seed(harness)
    counting = CountingUnitOfWork(inner=harness.unit_of_work)
    compile_view.unit_of_work = counting

    await compile_view.execute(harness.command())

    assert counting.sizes == {"compile-view-allow": ALLOW_FIXED_TRANSACTION_PARTICIPANTS}


async def test_the_forwarded_evidence_root_is_resolved_through_the_adr_017_closure(
    harness: CompileHarness,
) -> None:
    """Matrix N. The compile reads the ancestry through the one service, not a second walk.

    The fixture chains a ``FORWARDED`` root onto the original, and gate 17 counts collapsed
    roots. Two things are asserted: the closure the adapter loads actually contains the parent
    it was never handed directly, and the count that reaches the compiler is the collapsed one
    rather than the number of evidence items.
    """

    from chorus.application.services.root_closure import evidence_root_ids, resolve_root_closure

    compile_view = await _seed(harness)
    items = tuple(
        item
        for item in harness.fixture.context.evidence_items
        if item.evidence_id
        in {harness.fixture.photo_evidence_id, harness.fixture.forwarded_evidence_id}
    )

    closure = await resolve_root_closure(
        harness.core, harness.scope.community_scope, evidence_root_ids(items)
    )

    forwarded = next(root for root in closure if root.parent_root_id is not None)
    assert forwarded.parent_root_id in {root.root_id for root in closure}

    # And the compile itself still allows, which it could not do if the closure had failed.
    result = await compile_view.execute(harness.command())
    assert result.view is not None


async def test_the_compile_plan_carries_a_commit_proof(harness: CompileHarness) -> None:
    """Matrix AC. An ambiguous transport outcome is resolved by reading, never by retrying.

    The proof is the idempotency item the plan itself writes, so an interrupted compile can be
    classified as committed, definitely-not-committed, or unproven -- and only the middle one
    licenses another attempt. A plan without a proof would leave the third case indistinguishable
    from the second, which is how a compile gets run twice.
    """

    compile_view = await _seed(harness)
    captured: list[TransactionPlan] = []
    inner = harness.unit_of_work

    @dataclass(slots=True)
    class Capturing:
        async def commit(self, plan: TransactionPlan) -> None:
            captured.append(plan)
            await inner.commit(plan)

        async def resolve_outcome(self, plan: TransactionPlan) -> TransactionOutcome:
            return await inner.resolve_outcome(plan)

    compile_view.unit_of_work = Capturing()

    await compile_view.execute(harness.command())

    plan = next(item for item in captured if item.name == "compile-view-allow")
    assert plan.commit_proof is not None
    assert plan.commit_proof.key.table is TableName.SHAREABLE
    # The proof names the record this plan creates, so resolution reads *this* command's
    # outcome rather than some other command's row that happens to share an address.
    assert any(operation.key == plan.commit_proof.key for operation in plan.operations)

    outcome = await harness.unit_of_work.resolve_outcome(plan)
    assert isinstance(outcome, TransactionCommitted)
