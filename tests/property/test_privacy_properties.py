from __future__ import annotations

import string
from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fixtures.elevator import FIXTURE_NAMESPACE_UUID, build_elevator_fixture
from tests.unit.privacy.test_compiler import _command

from chorus.domain.entities import FactType
from chorus.domain.facts import HealthDetail, SubjectRelation
from chorus.domain.ids import Uuid5Generator
from chorus.privacy.canonical import to_canonical_primitive
from chorus.privacy.compiler import CompileAllow, PrivacyCompiler


@given(st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=32))
@settings(max_examples=30)
def test_generated_internal_health_value_never_appears_in_view(secret_suffix: str) -> None:
    fixture = build_elevator_fixture()
    sentinel = f"PRIVATEHEALTH_{secret_suffix}"
    facts = tuple(
        replace(
            fact,
            value=HealthDetail(subject_relation=SubjectRelation.FAMILY, detail=sentinel),
        )
        if fact.fact_type is FactType.HEALTH_DETAIL
        else fact
        for fact in fixture.context.facts
    )
    context = replace(fixture.context, facts=facts)
    command = _command(
        fixture,
        (fixture.incident_fact_ids[0], fixture.health_fact_id),
        optional_ids=frozenset({fixture.health_fact_id}),
    )

    result = PrivacyCompiler(
        Uuid5Generator(FIXTURE_NAMESPACE_UUID, prefix="property-health")
    ).compile(command, context)

    assert isinstance(result, CompileAllow)
    assert sentinel not in str(to_canonical_primitive(result.view))


@given(st.permutations(tuple(range(6))))
@settings(max_examples=20)
def test_requested_fact_permutation_preserves_canonical_view_hash(order: list[int]) -> None:
    fixture = build_elevator_fixture()
    baseline = PrivacyCompiler(
        Uuid5Generator(FIXTURE_NAMESPACE_UUID, prefix="property-order")
    ).compile(_command(fixture, fixture.incident_fact_ids), fixture.context)
    permuted_ids = tuple(fixture.incident_fact_ids[index] for index in order)
    permuted = PrivacyCompiler(
        Uuid5Generator(FIXTURE_NAMESPACE_UUID, prefix="property-order")
    ).compile(_command(fixture, permuted_ids), fixture.context)

    assert isinstance(baseline, CompileAllow)
    assert isinstance(permuted, CompileAllow)
    assert baseline.view.view_hash == permuted.view.view_hash
