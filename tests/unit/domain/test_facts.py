from __future__ import annotations

from dataclasses import replace
from uuid import uuid5

from tests.fixtures.elevator import FIXTURE_NAMESPACE_UUID, build_elevator_fixture

from chorus.domain.facts import independent_source_count
from chorus.domain.ids import FactId


def test_forwarded_photo_counts_as_one_root() -> None:
    fixture = build_elevator_fixture()
    photo_fact = next(
        fact for fact in fixture.context.facts if fact.fact_id == fixture.photo_fact_id
    )
    forwarded_fact = replace(
        photo_fact,
        fact_id=FactId(uuid5(FIXTURE_NAMESPACE_UUID, "fact:forwarded-photo-copy")),
        contributor_id=fixture.contributor_ids[3],
        evidence_ids=(fixture.forwarded_evidence_id,),
    )

    count = independent_source_count(
        (photo_fact, forwarded_fact),
        fixture.context.evidence_items,
        fixture.context.evidence_roots,
    )

    assert count == 1


def test_duplicate_reporter_does_not_inflate_independent_count() -> None:
    fixture = build_elevator_fixture()
    facts = tuple(
        fact
        for fact in fixture.context.facts
        if fact.fact_id in {fixture.incident_fact_ids[0], fixture.incident_fact_ids[4]}
    )

    count = independent_source_count(
        facts,
        fixture.context.evidence_items,
        fixture.context.evidence_roots,
    )

    assert count == 1
