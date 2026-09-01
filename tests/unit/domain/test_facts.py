from __future__ import annotations

from dataclasses import replace
from uuid import uuid5

import pytest
from tests.fixtures.elevator import FIXTURE_NAMESPACE_UUID, build_elevator_fixture

from chorus.domain.facts import ReportStatus, independent_source_count
from chorus.domain.ids import CommunityId, FactId, MessageId, Namespace, ReportId


def test_forwarded_photo_counts_as_one_root() -> None:
    fixture = build_elevator_fixture()
    photo_fact = next(
        fact for fact in fixture.context.facts if fact.fact_id == fixture.photo_fact_id
    )
    photo_report = next(
        report for report in fixture.context.reports if report.report_id == photo_fact.report_id
    )
    forwarded_report_id = ReportId(uuid5(FIXTURE_NAMESPACE_UUID, "report:forwarded-photo-copy"))
    forwarded_message_id = MessageId(uuid5(FIXTURE_NAMESPACE_UUID, "message:forwarded-photo-copy"))
    forwarded_report = replace(
        photo_report,
        report_id=forwarded_report_id,
        contributor_id=fixture.contributor_ids[3],
        source_message_ids=(forwarded_message_id,),
        evidence_ids=(fixture.forwarded_evidence_id,),
    )
    forwarded_fact = replace(
        photo_fact,
        fact_id=FactId(uuid5(FIXTURE_NAMESPACE_UUID, "fact:forwarded-photo-copy")),
        report_id=forwarded_report_id,
        contributor_id=fixture.contributor_ids[3],
        evidence_ids=(fixture.forwarded_evidence_id,),
        source_message_ids=(forwarded_message_id,),
    )

    count = independent_source_count(
        (photo_fact, forwarded_fact),
        (photo_report, forwarded_report),
        fixture.context.evidence_items,
        fixture.context.evidence_roots,
    )

    assert count == 1


def test_duplicate_reporter_does_not_inflate_independent_count() -> None:
    fixture = build_elevator_fixture()
    original_fact, duplicate_fact = tuple(
        fact for fact in fixture.context.facts if fact.fact_id in fixture.incident_fact_ids[:2]
    )
    original_report = next(
        report for report in fixture.context.reports if report.report_id == original_fact.report_id
    )
    duplicate_report = next(
        report for report in fixture.context.reports if report.report_id == duplicate_fact.report_id
    )
    duplicate_report = replace(
        duplicate_report,
        status=ReportStatus.DUPLICATE,
        duplicate_of_report_id=original_report.report_id,
    )

    count = independent_source_count(
        (original_fact, duplicate_fact),
        (original_report, duplicate_report),
        fixture.context.evidence_items,
        fixture.context.evidence_roots,
    )

    assert count == 1


def test_retracted_report_does_not_add_an_independent_source() -> None:
    fixture = build_elevator_fixture()
    facts = tuple(
        fact for fact in fixture.context.facts if fact.fact_id in fixture.incident_fact_ids[:2]
    )
    reports = tuple(
        report
        for report in fixture.context.reports
        if report.report_id in {f.report_id for f in facts}
    )
    reports = (reports[0], replace(reports[1], status=ReportStatus.RETRACTED))

    count = independent_source_count(
        facts,
        reports,
        fixture.context.evidence_items,
        fixture.context.evidence_roots,
    )

    assert count == 1


def test_two_genuinely_independent_reporters_count_as_two_sources() -> None:
    fixture = build_elevator_fixture()
    facts = tuple(
        fact for fact in fixture.context.facts if fact.fact_id in fixture.incident_fact_ids[:2]
    )
    reports = tuple(
        report
        for report in fixture.context.reports
        if report.report_id in {f.report_id for f in facts}
    )

    count = independent_source_count(
        facts,
        reports,
        fixture.context.evidence_items,
        fixture.context.evidence_roots,
    )

    assert count == 2


def test_forwarded_root_ancestry_cannot_cross_community() -> None:
    fixture = build_elevator_fixture()
    photo_fact = next(
        fact for fact in fixture.context.facts if fact.fact_id == fixture.photo_fact_id
    )
    photo_report = next(
        report for report in fixture.context.reports if report.report_id == photo_fact.report_id
    )
    forwarded_report_id = ReportId(uuid5(FIXTURE_NAMESPACE_UUID, "report:foreign-root-copy"))
    forwarded_message_id = MessageId(uuid5(FIXTURE_NAMESPACE_UUID, "message:foreign-root-copy"))
    forwarded_report = replace(
        photo_report,
        report_id=forwarded_report_id,
        contributor_id=fixture.contributor_ids[3],
        source_message_ids=(forwarded_message_id,),
        evidence_ids=(fixture.forwarded_evidence_id,),
    )
    forwarded_fact = replace(
        photo_fact,
        fact_id=FactId(uuid5(FIXTURE_NAMESPACE_UUID, "fact:foreign-root-copy")),
        report_id=forwarded_report_id,
        contributor_id=fixture.contributor_ids[3],
        source_message_ids=(forwarded_message_id,),
        evidence_ids=(fixture.forwarded_evidence_id,),
    )
    original_root_id = next(
        item.root_id
        for item in fixture.context.evidence_items
        if item.evidence_id == fixture.photo_evidence_id
    )
    roots = tuple(
        replace(
            root,
            community_id=CommunityId(uuid5(FIXTURE_NAMESPACE_UUID, "community:foreign")),
        )
        if root.root_id == original_root_id
        else root
        for root in fixture.context.evidence_roots
    )

    with pytest.raises(ValueError, match="ancestry crosses community"):
        independent_source_count(
            (forwarded_fact,),
            (forwarded_report,),
            fixture.context.evidence_items,
            roots,
        )


def test_evidence_item_namespace_must_match_fact_namespace() -> None:
    fixture = build_elevator_fixture()
    photo_fact = next(
        fact for fact in fixture.context.facts if fact.fact_id == fixture.photo_fact_id
    )
    photo_report = next(
        report for report in fixture.context.reports if report.report_id == photo_fact.report_id
    )
    evidence_items = tuple(
        replace(item, namespace=Namespace("TEST_FOREIGN"))
        if item.evidence_id == fixture.photo_evidence_id
        else item
        for item in fixture.context.evidence_items
    )

    with pytest.raises(ValueError, match="namespace"):
        independent_source_count(
            (photo_fact,),
            (photo_report,),
            evidence_items,
            fixture.context.evidence_roots,
        )
