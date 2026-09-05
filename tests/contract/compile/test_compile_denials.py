"""The refusals, through the adapter, and what each one does and does not persist.

The pure compiler's own gate tests already prove *which* gate answers. What only becomes
checkable here is that a refusal is durable, that it leaves the Shareable zone untouched, and
that the reason a caller is given is the reason the private lineage recorded.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.fixtures.compile import CompileHarness, harness_uuid, photo_bytes
from tests.fixtures.elevator import NOW

from chorus.application.commands.compile_view import CompileView
from chorus.application.errors import PolicyDeniedError
from chorus.domain.entities import (
    CaseState,
    DestinationKind,
    DisclosureScope,
    MandateStatus,
    Purpose,
)
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import CaseId, DestinationId, EvidenceItemId, FactId
from chorus.ports.pagination import PageRequest
from chorus.ports.records import CompileDecisionOutcome, StoredSafeDestination
from chorus.privacy.canonical import hash_mandate_terms

pytestmark = pytest.mark.anyio


async def _seed(harness: CompileHarness, **kwargs: object) -> CompileView:
    raw = photo_bytes()
    kwargs.setdefault("evidence_items", harness.align_photo_digest(raw))
    kwargs.setdefault("photo", raw)
    await harness.seed(**kwargs)  # type: ignore[arg-type]
    return harness.compile_view()


async def _assert_nothing_shareable(harness: CompileHarness) -> None:
    """A denial writes no view, no history, and no pointer. Every time."""

    assert await harness.shareable.load_current_view_pointer(harness.scope) is None
    history = await harness.shareable.read_view_history(harness.scope, PageRequest(limit=10))
    assert history.items == ()


# -- whole-request denials --------------------------------------------------------------


async def test_a_stale_expected_case_version_denies_the_whole_compile(
    harness: CompileHarness,
) -> None:
    """Matrix H."""

    compile_view = await _seed(harness)

    with pytest.raises(PolicyDeniedError) as error:
        await compile_view.execute(harness.command(expected_case_version=harness.case.version + 1))

    assert error.value.reason_codes == ("STALE_CASE_VERSION",)
    await _assert_nothing_shareable(harness)


async def test_a_resolved_case_denies_the_whole_compile(harness: CompileHarness) -> None:
    """Matrix AW. A case nothing may act on cannot authorize an outbound artifact."""

    closed = replace(harness.case, state=CaseState.RESOLVED, resolved_at=NOW)
    compile_view = await _seed(harness, case=closed)

    with pytest.raises(PolicyDeniedError) as error:
        await compile_view.execute(harness.command())

    assert error.value.reason_codes == ("STALE_CASE_VERSION",)


async def test_a_cross_case_fact_denies_the_whole_compile(harness: CompileHarness) -> None:
    """Matrix K. A foreign identifier is refused, not quietly skipped."""

    compile_view = await _seed(harness)
    foreign = FactId(harness_uuid("foreign-fact"))

    with pytest.raises(PolicyDeniedError) as error:
        await compile_view.execute(
            harness.command(
                fact_ids=(harness.fixture.incident_fact_ids[0], foreign), evidence_ids=()
            )
        )

    assert error.value.reason_codes == ("FACT_NOT_FOUND",)
    await _assert_nothing_shareable(harness)


async def test_a_cross_case_evidence_reference_denies_the_whole_compile(
    harness: CompileHarness,
) -> None:
    """Matrix L. Nothing is salvaged from a request that named something foreign."""

    compile_view = await _seed(harness)
    foreign = EvidenceItemId(harness_uuid("foreign-evidence"))

    with pytest.raises(PolicyDeniedError) as error:
        await compile_view.execute(harness.command(evidence_ids=(foreign,)))

    assert error.value.reason_codes == ("EVIDENCE_NOT_FOUND",)
    await _assert_nothing_shareable(harness)


async def test_a_case_below_the_corroboration_minimum_is_denied(
    harness: CompileHarness,
) -> None:
    """Matrix M. Gate 17 takes the minimum of stored and computed.

    A stale stored value can therefore only ever deny.
    """

    understated = replace(harness.case, corroboration_source_count=1)
    compile_view = await _seed(harness, case=understated)

    with pytest.raises(PolicyDeniedError) as error:
        await compile_view.execute(harness.command())

    assert error.value.reason_codes == ("CORROBORATION_MIN_NOT_MET",)


async def test_a_missing_root_locator_fails_closed_rather_than_under_counting(
    harness: CompileHarness,
) -> None:
    """Matrix O and ADR-017. An absent locator is loud, because a short answer is invisible."""

    context = harness.fixture.context
    compile_view = await _seed(harness, roots=context.evidence_roots[:1])

    with pytest.raises(IntegrityError):
        await compile_view.execute(harness.command())


async def test_a_destination_the_mandate_does_not_allow_denies_a_required_fact(
    harness: CompileHarness,
) -> None:
    """Matrix F. The registry entry and the request must agree, and both must be granted."""

    compile_view = await _seed(harness)
    other = StoredSafeDestination(
        destination_id=DestinationId("property_manager:other"),
        kind=DestinationKind.PROPERTY_MANAGER,
        registry_version=1,
        routing_token=harness_uuid("other-routing"),
        display_label="Other Management",
    )

    with pytest.raises(PolicyDeniedError) as error:
        await compile_view.execute(
            harness.command(
                destination=other,
                fact_ids=(harness.fixture.incident_fact_ids[0],),
                evidence_ids=(),
                necessity_required=True,
            )
        )

    assert "DESTINATION_NOT_ALLOWED" in error.value.reason_codes


# -- item-level exclusions --------------------------------------------------------------


async def test_an_internal_only_fact_is_excluded_and_recorded_as_such(
    harness: CompileHarness,
) -> None:
    """Matrix B. An optional ineligible fact is omitted and audited, not silently dropped."""

    compile_view = await _seed(harness)
    command = harness.command()

    result = await compile_view.execute(command)

    excluded = {entry.fact_id for entry in result.excluded}
    assert harness.fixture.health_fact_id in excluded
    assert harness.fixture.unit_fact_id in excluded

    projection = await harness.audit.load_compile_projection(harness.scope, command.compile_id)
    assert projection is not None
    health = next(
        record for record in projection.facts if record.fact_id == harness.fixture.health_fact_id
    )
    assert health.granted_scope is DisclosureScope.INTERNAL_ONLY
    assert "INTERNAL_ONLY" in health.reason_codes


async def test_an_identity_fact_without_an_identity_grant_is_excluded(
    harness: CompileHarness,
) -> None:
    """Matrix C. Content permission is not identity permission, and never becomes it."""

    compile_view = await _seed(harness)

    result = await compile_view.execute(harness.command())

    excluded = {entry.fact_id for entry in result.excluded}
    assert harness.fixture.identity_fact_id in excluded


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("expired", "MANDATE_EXPIRED"),
        ("revoked", "MANDATE_REVOKED"),
        ("refused", "MANDATE_NOT_APPROVED"),
    ],
)
async def test_a_mandate_that_cannot_authorize_denies_a_required_fact(
    harness: CompileHarness, mutation: str, expected: str
) -> None:
    """Matrix D and E. Each ordered gate answers with its own reason, not a generic one."""

    context = harness.fixture.context
    target = context.mandates[0]
    if mutation == "expired":
        changed = replace(target, expires_at=NOW - timedelta(seconds=1))
    elif mutation == "revoked":
        changed = replace(
            target, status=MandateStatus.REVOKED, revoked_at=NOW - timedelta(minutes=1)
        )
    else:
        changed = replace(target, status=MandateStatus.REFUSED)
    # The terms hash covers the terms, so a mutated mandate has to be re-sealed. Leaving the
    # old digest would trip gate 8 and prove nothing about the gate under test.
    changed = replace(changed, terms_hash=hash_mandate_terms(changed))
    mandates = (changed, *context.mandates[1:])
    pointers = tuple(
        replace(pointer, terms_hash=changed.terms_hash)
        if pointer.mandate_id == changed.mandate_id
        else pointer
        for pointer in context.mandate_pointers
    )
    compile_view = await _seed(harness, mandates=mandates, pointers=pointers)
    owned = tuple(
        fact.fact_id
        for fact in context.facts
        if fact.contributor_id == changed.contributor_id
        and fact.fact_type.value == "INCIDENT_OCCURRENCE"
    )

    with pytest.raises(PolicyDeniedError) as error:
        await compile_view.execute(
            harness.command(fact_ids=owned, evidence_ids=(), necessity_required=True)
        )

    assert expected in error.value.reason_codes


# -- what a denial persists -------------------------------------------------------------


async def test_a_denial_persists_its_lineage_and_its_reason(harness: CompileHarness) -> None:
    """The refusal is durable, so a redelivery replays it rather than re-deciding it."""

    compile_view = await _seed(harness)
    command = harness.command(expected_case_version=harness.case.version + 1)

    with pytest.raises(PolicyDeniedError):
        await compile_view.execute(command)

    projection = await harness.audit.load_compile_projection(harness.scope, command.compile_id)
    assert projection is not None
    assert projection.decision is CompileDecisionOutcome.DENY
    assert projection.reason_codes == ("STALE_CASE_VERSION",)
    assert projection.view_id is None
    assert projection.view_hash is None


async def test_a_denial_leaves_an_existing_current_view_valid_and_current(
    harness: CompileHarness,
) -> None:
    """Matrix AF. A refusal is not an invalidation; the pointer is untouched."""

    compile_view = await _seed(harness)
    allowed = await compile_view.execute(harness.command())
    assert allowed.view is not None
    before = await harness.shareable.load_current_view_pointer(harness.scope)

    with pytest.raises(PolicyDeniedError):
        await compile_view.execute(
            harness.command(
                compile_id=harness_uuid("compile:denied-after"),
                idempotency_key="compile-key-denied-after",
                expected_case_version=harness.case.version + 3,
            )
        )

    after = await harness.shareable.load_current_view_pointer(harness.scope)
    assert after == before
    assert after is not None
    assert after.view_id == allowed.view.view_id


async def test_a_foreign_case_identifier_is_refused_without_enumerating_it(
    harness: CompileHarness,
) -> None:
    """A case that is not this caller's answers the same way an absent one does."""

    compile_view = await _seed(harness)

    with pytest.raises(Exception) as error:
        await compile_view.execute(harness.command(case_id=CaseId(harness_uuid("foreign-case"))))

    assert "NOT_FOUND" in str(error.value) or "CROSS_CASE" in str(error.value)


def test_the_purpose_gate_cannot_be_reached_through_a_legal_mandate() -> None:
    """Matrix G, answered honestly rather than staged.

    ``Purpose`` has exactly one member in policy/v1 and a mandate must name a non-empty purpose
    set, so no legally constructible mandate can exclude the only purpose a caller may request.
    The gate is real and is covered by the pure compiler's own table, which reaches it with a
    synthetic value; reproducing that here would mean building a mandate the domain refuses,
    which would test the test rather than the boundary.

    This assertion is the thing that would break if a second purpose were ever added -- at which
    point the contract-level case becomes constructible and should be written.
    """

    assert len(tuple(Purpose)) == 1
