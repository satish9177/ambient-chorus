"""Matrix AQ: the compiler's frozen properties, re-run through the persistence adapter.

The pure compiler already has these as Hypothesis properties over in-memory state. Re-running
them here answers a different question: whether the *adapter* preserves them. A composition
layer that reconstructed the context slightly differently -- a fact set built from a stale read,
a root closure resolved from the wrong scope, a candidate assembled with a caption nobody
reviewed -- would leave every pure test green and still export the wrong thing.

Each property below is therefore stated over the whole path: seed real rows, run the real use
case, and look at what actually reached storage.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.fixtures.compile import CompileHarness, harness_uuid, photo_bytes

from chorus.application.commands.compile_view import CompileViewResult
from chorus.application.errors import PolicyDeniedError
from chorus.domain.entities import DisclosureScope
from chorus.domain.facts import Fact, FactStatus, HealthDetail
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.privacy.canonical import to_canonical_primitive

pytestmark = pytest.mark.anyio

SLOW = settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


async def _compiled(harness: CompileHarness, **command: object) -> CompileViewResult:
    raw = photo_bytes()
    await harness.seed(evidence_items=harness.align_photo_digest(raw), photo=raw)
    return await harness.compile_view().execute(harness.command(**command))  # type: ignore[arg-type]


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for entry in value for item in _strings(entry)]
    if isinstance(value, dict):
        found: list[str] = []
        for key, entry in value.items():
            found.append(str(key))
            found.extend(_strings(entry))
        return found
    return []


@given(secret=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=6, max_size=24))
@SLOW
async def test_no_internal_only_value_reaches_the_persisted_view(secret: str) -> None:
    """The frozen property, over the artifact that actually landed in the Shareable table.

    A generated sentinel is planted inside the health fact's own value, so the only way it can
    reach the view is if the adapter widened something the compiler excluded.
    """

    harness = CompileHarness(driver=InMemoryStorageDriver())
    fixture = harness.fixture

    def _mark(fact: Fact) -> Fact:
        if fact.fact_id != fixture.health_fact_id:
            return fact
        value = fact.value
        assert isinstance(value, HealthDetail)
        return replace(fact, value=replace(value, detail=f"{secret}_HEALTH"))

    marked = tuple(_mark(fact) for fact in fixture.context.facts)
    raw = photo_bytes()
    await harness.seed(facts=marked, evidence_items=harness.align_photo_digest(raw), photo=raw)

    result = await harness.compile_view().execute(harness.command())

    assert result.view is not None
    rendered = " ".join(_strings(to_canonical_primitive(result.view)))
    assert secret not in rendered

    stored = await harness.shareable.load_view(harness.scope, result.view.view_id)
    assert secret not in " ".join(_strings(to_canonical_primitive(stored)))


@given(order=st.permutations(list(range(6))))
@SLOW
async def test_permuting_the_requested_order_does_not_change_the_view_hash(
    order: list[int],
) -> None:
    """The compiler canonicalizes order, so a caller's list order is not part of the request.

    Proved through the adapter because the request hash is computed here, not in the compiler:
    a composition layer that hashed the caller's literal order would make two identical requests
    look like a conflict under one idempotency key.
    """

    baseline = CompileHarness(driver=InMemoryStorageDriver())
    incidents = baseline.fixture.incident_fact_ids
    first = await _compiled(baseline, fact_ids=incidents, evidence_ids=())

    permuted = CompileHarness(driver=InMemoryStorageDriver())
    reordered = tuple(permuted.fixture.incident_fact_ids[index] for index in order)
    second = await _compiled(permuted, fact_ids=reordered, evidence_ids=())

    assert first.view is not None
    assert second.view is not None
    assert first.view.view_hash == second.view.view_hash


@given(
    extra=st.integers(min_value=1, max_value=8),
)
@SLOW
async def test_a_stale_stored_corroboration_count_can_only_deny(extra: int) -> None:
    """Gate 17 takes the minimum of stored and computed, in both directions.

    Understating the stored count denies; overstating it cannot allow, because the computed
    value is recomputed from the rows the adapter actually loaded.
    """

    understated = CompileHarness(driver=InMemoryStorageDriver())
    raw = photo_bytes()
    await understated.seed(
        case=replace(understated.case, corroboration_source_count=1),
        evidence_items=understated.align_photo_digest(raw),
        photo=raw,
    )

    with pytest.raises(PolicyDeniedError) as error:
        await understated.compile_view().execute(understated.command())
    assert error.value.reason_codes == ("CORROBORATION_MIN_NOT_MET",)

    overstated = CompileHarness(driver=InMemoryStorageDriver())
    await overstated.seed(
        case=replace(
            overstated.case, corroboration_source_count=overstated.case.version + extra + 50
        ),
        evidence_items=overstated.align_photo_digest(raw),
        photo=raw,
    )

    # Overstating cannot manufacture eligibility: the computed count still governs.
    result = await overstated.compile_view().execute(overstated.command())
    assert result.view is not None


@given(scope=st.sampled_from([DisclosureScope.INTERNAL_ONLY, DisclosureScope.AGGREGATE_ONLY]))
@SLOW
async def test_narrowing_a_grant_never_widens_what_is_exported(
    scope: DisclosureScope,
) -> None:
    """Revocation, narrowing, and expiry can only ever remove facts from a view.

    Stated as a property because the interesting failure is monotonicity: a narrowed grant that
    happened to change which transformation rule ran could otherwise export *more*.
    """

    from chorus.privacy.canonical import hash_mandate_terms

    baseline = CompileHarness(driver=InMemoryStorageDriver())
    raw = photo_bytes()
    await baseline.seed(evidence_items=baseline.align_photo_digest(raw), photo=raw)
    before = await baseline.compile_view().execute(baseline.command())
    assert before.view is not None

    narrowed_harness = CompileHarness(driver=InMemoryStorageDriver())
    context = narrowed_harness.fixture.context
    target = context.mandates[0]
    narrowed = replace(
        target,
        fact_grants=tuple(replace(grant, max_scope=scope) for grant in target.fact_grants),
    )
    narrowed = replace(narrowed, terms_hash=hash_mandate_terms(narrowed))
    pointers = tuple(
        replace(pointer, terms_hash=narrowed.terms_hash)
        if pointer.mandate_id == narrowed.mandate_id
        else pointer
        for pointer in context.mandate_pointers
    )
    await narrowed_harness.seed(
        mandates=(narrowed, *context.mandates[1:]),
        pointers=pointers,
        evidence_items=narrowed_harness.align_photo_digest(raw),
        photo=raw,
    )

    try:
        after = await narrowed_harness.compile_view().execute(narrowed_harness.command())
    except PolicyDeniedError:
        return

    assert after.view is not None
    assert len(after.included) <= len(before.included)


@given(withdrawn=st.integers(min_value=0, max_value=3))
@SLOW
async def test_a_withdrawn_fact_is_never_exported(withdrawn: int) -> None:
    """Only active facts travel, and the adapter is what decides which rows are loaded."""

    harness = CompileHarness(driver=InMemoryStorageDriver())
    fixture = harness.fixture
    target = fixture.incident_fact_ids[withdrawn]
    facts = tuple(
        replace(fact, status=FactStatus.WITHDRAWN) if fact.fact_id == target else fact
        for fact in fixture.context.facts
    )
    raw = photo_bytes()
    await harness.seed(facts=facts, evidence_items=harness.align_photo_digest(raw), photo=raw)

    try:
        result = await harness.compile_view().execute(
            harness.command(compile_id=harness_uuid(f"compile:withdrawn:{withdrawn}"))
        )
    except PolicyDeniedError:
        return

    assert target not in {entry.fact_id for entry in result.included}
