"""``independent_sources``: the same frozen function, two different fact sets.

Additive by construction -- ``independent_source_count`` is a thin wrapper over ``count`` -- so
these tests exist to pin the *result object* the private investigation surface and the
``evidence.independence.computed`` event read, and to prove the wrapper still agrees with it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid5

from chorus.domain.entities import (
    DerivationKind,
    EvidenceItem,
    EvidenceRoot,
    EvidenceStatus,
    ExtractionStatus,
    FactType,
    MalwareScanStatus,
    SensitivityCategory,
)
from chorus.domain.facts import (
    EvidenceRootSource,
    Fact,
    FactStatus,
    LocationArea,
    LocationAreaCode,
    Report,
    ReporterSource,
    ReportStatus,
    independent_source_count,
    independent_sources,
)
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    EvidenceRootId,
    FactId,
    MessageId,
    Namespace,
    ReportId,
    SensitiveStr,
    Sha256Digest,
)

NAMESPACE = Namespace("TEST_INDEPENDENCE")
SEED = UUID("4e5f6071-8293-54a5-b6c7-d8e9f0a1b2c3")
NOW = datetime(2030, 7, 1, 9, 0, 0, tzinfo=UTC)
CAB = LocationArea(area=LocationAreaCode.ELEVATOR_CAB)


def uuid(name: str) -> UUID:
    return uuid5(SEED, name)


COMMUNITY = CommunityId(uuid("community"))
CASE = CaseId(uuid("case"))


def digest(value: str) -> Sha256Digest:
    return Sha256Digest(f"sha256:{sha256(value.encode()).hexdigest()}")


def report(label: str) -> Report:
    return Report(
        report_id=ReportId(uuid(f"report:{label}")),
        case_id=CASE,
        community_id=COMMUNITY,
        contributor_id=ContributorId(uuid(f"contributor:{label}")),
        namespace=NAMESPACE,
        source_message_ids=(MessageId(uuid(f"message:{label}")),),
        issue_type="ELEVATOR_FAILURE",
        private_summary=SensitiveStr("A summary."),
        occurred_at=NOW,
        location_area=LocationAreaCode.ELEVATOR_CAB,
        evidence_ids=(),
        status=ReportStatus.ACTIVE,
        duplicate_of_report_id=None,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def fact(label: str, reporter: str, *, evidence_labels: tuple[str, ...] = ()) -> Fact:
    return Fact(
        fact_id=FactId(uuid(f"fact:{label}")),
        case_id=CASE,
        report_id=ReportId(uuid(f"report:{reporter}")),
        community_id=COMMUNITY,
        contributor_id=ContributorId(uuid(f"contributor:{reporter}")),
        namespace=NAMESPACE,
        fact_type=FactType.LOCATION_AREA,
        value=CAB,
        sensitivity=SensitivityCategory.GENERAL,
        evidence_ids=tuple(EvidenceItemId(uuid(f"evidence:{item}")) for item in evidence_labels),
        evidence_status=EvidenceStatus.REPORTED,
        source_message_ids=(MessageId(uuid(f"message:{reporter}")),),
        supersedes_fact_id=None,
        status=FactStatus.ACTIVE,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def root(label: str, *, parent: str | None = None) -> EvidenceRoot:
    return EvidenceRoot(
        root_id=EvidenceRootId(uuid(f"root:{label}")),
        community_id=COMMUNITY,
        namespace=NAMESPACE,
        root_sha256=digest(f"root:{label}"),
        media_type="image/jpeg",
        first_observed_at=NOW,
        derivation_kind=DerivationKind.ORIGINAL if parent is None else DerivationKind.FORWARDED,
        parent_root_id=None if parent is None else EvidenceRootId(uuid(f"root:{parent}")),
        created_at=NOW,
        updated_at=NOW,
    )


def item(label: str, reporter: str, root_label: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=EvidenceItemId(uuid(f"evidence:{label}")),
        root_id=EvidenceRootId(uuid(f"root:{root_label}")),
        community_id=COMMUNITY,
        case_id=CASE,
        namespace=NAMESPACE,
        submitted_by_contributor_id=ContributorId(uuid(f"contributor:{reporter}")),
        source_message_id=None,
        private_object_key=SensitiveStr(f"private/{label}"),
        media_type="image/jpeg",
        byte_length=10,
        sha256=digest(f"evidence:{label}"),
        captured_at=None,
        uploaded_at=NOW,
        derived_from_evidence_id=None,
        malware_scan_status=MalwareScanStatus.CLEAN,
        extraction_status=ExtractionStatus.NOT_NEEDED,
        extracted_text=None,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def test_the_wrapper_returns_exactly_the_results_count() -> None:
    """Additive: the existing function is now one attribute access away from the new one."""

    facts = (fact("a", "a"), fact("b", "b"))
    reports = (report("a"), report("b"))
    assert independent_source_count(facts, reports, (), ()) == 2
    assert independent_sources(facts, reports, (), ()).count == 2


def test_the_result_shows_which_contributor_contributed_which_origin() -> None:
    facts = (fact("a", "a", evidence_labels=("photo",)), fact("b", "b"))
    result = independent_sources(
        facts, (report("a"), report("b")), (item("photo", "a", "one"),), (root("one"),)
    )
    by_contributor = result.sources_by_contributor
    assert by_contributor[ContributorId(uuid("contributor:a"))] == (
        EvidenceRootSource(EvidenceRootId(uuid("root:one"))),
    )
    assert by_contributor[ContributorId(uuid("contributor:b"))] == (
        ReporterSource(ContributorId(uuid("contributor:b"))),
    )


def test_two_contributors_collapsing_onto_one_root_are_visible_in_the_working() -> None:
    """The count says one; the working says why, without anyone recomputing it."""

    facts = (
        fact("a", "a", evidence_labels=("original",)),
        fact("b", "b", evidence_labels=("forward",)),
    )
    result = independent_sources(
        facts,
        (report("a"), report("b")),
        (item("original", "a", "one"), item("forward", "b", "two")),
        (root("one"), root("two", parent="one")),
    )
    assert result.count == 1
    origins = {source for sources in result.sources_by_contributor.values() for source in sources}
    assert origins == {EvidenceRootSource(EvidenceRootId(uuid("root:one")))}


def test_the_working_is_not_an_input_to_the_count() -> None:
    """Recomputing from the mapping would be a second implementation of an authorization value."""

    facts = (fact("a", "a"), fact("b", "b"))
    result = independent_sources(facts, (report("a"), report("b")), (), ())
    assert result.count == 2
    assert len(result.sources_by_contributor) == 2
