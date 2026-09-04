"""ADR-015: what a status is computed from, and what a model may do to it.

Two halves, and the whole ADR turns on keeping them apart.

*Computation* answers "what does the case establish about this fact": a validated contradiction
first, then exact-canonical corroboration, then ``REPORTED``. ``VERIFIED`` is never produced,
because the allowed verification source set is empty; ``UNKNOWN`` is never produced either.

*Resolution* answers "what may the model do to that": lower it, never raise it. One rule does
two jobs -- "preserve ``UNKNOWN``" and "the model cannot grant ``VERIFIED``" are the same
sentence read in two directions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

import pytest

from chorus.application.services.evidence_status import (
    ALLOWED_VERIFICATION_SOURCES,
    StatusReason,
    compute_statuses,
    fact_support_key,
    resolve_status,
)
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
    Fact,
    FactStatus,
    FailureMode,
    IncidentOccurrence,
    LocationArea,
    LocationAreaCode,
    Report,
    ReportStatus,
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

NAMESPACE = Namespace("TEST_STATUS")
SEED = UUID("2c9a3e40-1f9e-5f00-9c3a-6b7f0a1d2e3f")
NOW = datetime(2030, 2, 1, 9, 0, 0, tzinfo=UTC)


def uuid(name: str) -> UUID:
    return uuid5(SEED, name)


COMMUNITY = CommunityId(uuid("community"))
CASE = CaseId(uuid("case"))


def digest(value: str) -> Sha256Digest:
    from hashlib import sha256

    return Sha256Digest(f"sha256:{sha256(value.encode()).hexdigest()}")


def report(
    label: str,
    *,
    status: ReportStatus = ReportStatus.ACTIVE,
    duplicate_of: str | None = None,
) -> Report:
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
        status=status,
        duplicate_of_report_id=(
            None if duplicate_of is None else ReportId(uuid(f"report:{duplicate_of}"))
        ),
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def fact(
    label: str,
    reporter: str,
    value: object,
    *,
    fact_type: FactType = FactType.LOCATION_AREA,
    evidence_labels: tuple[str, ...] = (),
    status: FactStatus = FactStatus.ACTIVE,
) -> Fact:
    return Fact(
        fact_id=FactId(uuid(f"fact:{label}")),
        case_id=CASE,
        report_id=ReportId(uuid(f"report:{reporter}")),
        community_id=COMMUNITY,
        contributor_id=ContributorId(uuid(f"contributor:{reporter}")),
        namespace=NAMESPACE,
        fact_type=fact_type,
        value=value,  # type: ignore[arg-type]
        sensitivity=SensitivityCategory.GENERAL,
        evidence_ids=tuple(EvidenceItemId(uuid(f"evidence:{item}")) for item in evidence_labels),
        evidence_status=EvidenceStatus.REPORTED,
        source_message_ids=(MessageId(uuid(f"message:{reporter}")),),
        supersedes_fact_id=None,
        status=status,
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
        derivation_kind=(DerivationKind.ORIGINAL if parent is None else DerivationKind.FORWARDED),
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


LOBBY = LocationArea(area=LocationAreaCode.LOBBY)
CAB = LocationArea(area=LocationAreaCode.ELEVATOR_CAB)


# -- the verified-source rule ------------------------------------------------------------


def test_the_allowed_verification_source_set_is_empty_in_policy_v1() -> None:
    """The rule is honoured in its strongest form: there is no source to mis-evaluate."""

    assert frozenset() == ALLOWED_VERIFICATION_SOURCES


def test_a_clean_malware_scan_does_not_verify_a_fact() -> None:
    """A statement about bytes is not a statement about the world.

    The fact below cites evidence whose scan is ``CLEAN`` -- the strongest per-evidence signal
    V1 has -- and it is exactly as unverified as one citing nothing.
    """

    facts = (fact("only", "a", CAB, evidence_labels=("photo",)),)
    statuses = compute_statuses(
        facts=facts,
        reports=(report("a"),),
        evidence_items=(item("photo", "a", "one"),),
        roots=(root("one"),),
        contradicted_fact_ids=frozenset(),
    )
    assert statuses[facts[0].fact_id] is EvidenceStatus.REPORTED


@pytest.mark.parametrize("proposed", [EvidenceStatus.VERIFIED])
def test_model_verified_is_always_downgraded_in_v1(proposed: EvidenceStatus) -> None:
    """Named test 16. Every model-proposed ``VERIFIED`` loses, and is audited as an overclaim."""

    fact_id = FactId(uuid("fact:any"))
    for computed in (EvidenceStatus.REPORTED, EvidenceStatus.CORROBORATED):
        resolved = resolve_status(fact_id, computed, proposed)
        assert resolved.resolved is computed
        assert resolved.overclaimed is True


def test_deterministic_computation_never_produces_verified_or_unknown() -> None:
    """Only three outcomes exist, and a fourth would need a superseding ADR to appear."""

    facts = (fact("a", "a", CAB), fact("b", "b", CAB), fact("c", "c", LOBBY))
    statuses = compute_statuses(
        facts=facts,
        reports=(report("a"), report("b"), report("c")),
        evidence_items=(),
        roots=(),
        contradicted_fact_ids=frozenset({facts[2].fact_id}),
    )
    assert set(statuses.values()) <= {
        EvidenceStatus.REPORTED,
        EvidenceStatus.CORROBORATED,
        EvidenceStatus.CONTRADICTED,
    }


# -- the ladder ---------------------------------------------------------------------------


def test_model_may_lower_but_never_raise_evidence_status() -> None:
    """Named test 17, in both directions from every rung of the ladder."""

    fact_id = FactId(uuid("fact:any"))
    ladder = (
        EvidenceStatus.VERIFIED,
        EvidenceStatus.CORROBORATED,
        EvidenceStatus.REPORTED,
        EvidenceStatus.UNKNOWN,
    )
    for computed in (EvidenceStatus.CORROBORATED, EvidenceStatus.REPORTED):
        for proposed in ladder:
            resolved = resolve_status(fact_id, computed, proposed)
            weaker = ladder.index(proposed) > ladder.index(computed)
            assert resolved.resolved is (proposed if weaker else computed)
            assert resolved.overclaimed is (ladder.index(proposed) < ladder.index(computed))


def test_unknown_is_reachable_only_by_the_model_lowering_a_computed_status() -> None:
    fact_id = FactId(uuid("fact:any"))
    resolved = resolve_status(fact_id, EvidenceStatus.CORROBORATED, EvidenceStatus.UNKNOWN)
    assert resolved.resolved is EvidenceStatus.UNKNOWN
    assert resolved.reason_code == StatusReason.MODEL_LOWERED_STATUS
    assert resolved.overclaimed is False


def test_a_fact_with_no_finding_keeps_its_computed_status() -> None:
    fact_id = FactId(uuid("fact:any"))
    resolved = resolve_status(fact_id, EvidenceStatus.CORROBORATED, None)
    assert resolved.resolved is EvidenceStatus.CORROBORATED
    assert resolved.reason_code == StatusReason.MULTIPLE_INDEPENDENT_SOURCES


# -- the one authority path to CONTRADICTED ------------------------------------------------


def test_proposed_status_contradicted_without_a_contradiction_entry_has_no_effect() -> None:
    """Named test 18. The field names no cited facts, so it asserts a conflict with nothing."""

    fact_id = FactId(uuid("fact:any"))
    for computed in (EvidenceStatus.REPORTED, EvidenceStatus.CORROBORATED):
        resolved = resolve_status(fact_id, computed, EvidenceStatus.CONTRADICTED)
        assert resolved.resolved is computed
        assert resolved.overclaimed is False


@pytest.mark.parametrize(
    "proposed",
    [
        EvidenceStatus.REPORTED,
        EvidenceStatus.UNKNOWN,
        EvidenceStatus.CORROBORATED,
        EvidenceStatus.VERIFIED,
        EvidenceStatus.CONTRADICTED,
        None,
    ],
)
def test_validated_contradiction_overrides_any_proposed_status(
    proposed: EvidenceStatus | None,
) -> None:
    """Named test 19. ``CONTRADICTED`` is off the ladder in both directions."""

    fact_id = FactId(uuid("fact:any"))
    resolved = resolve_status(fact_id, EvidenceStatus.CONTRADICTED, proposed)
    assert resolved.resolved is EvidenceStatus.CONTRADICTED
    assert resolved.reason_code == StatusReason.CONTRADICTION_CITED
    assert resolved.overclaimed is False


def test_a_cited_contradiction_outranks_corroboration() -> None:
    """Evaluated first, so a corroborated claim the case contradicts is never labelled so."""

    facts = (fact("a", "a", CAB), fact("b", "b", CAB))
    statuses = compute_statuses(
        facts=facts,
        reports=(report("a"), report("b")),
        evidence_items=(),
        roots=(),
        contradicted_fact_ids=frozenset({facts[0].fact_id}),
    )
    assert statuses[facts[0].fact_id] is EvidenceStatus.CONTRADICTED
    assert statuses[facts[1].fact_id] is EvidenceStatus.CORROBORATED


# -- exact canonical grouping ---------------------------------------------------------------


def test_two_independent_reporters_of_identical_canonical_fact_corroborate() -> None:
    """Named test 21, and case AD."""

    facts = (fact("a", "a", CAB), fact("b", "b", CAB))
    statuses = compute_statuses(
        facts=facts,
        reports=(report("a"), report("b")),
        evidence_items=(),
        roots=(),
        contradicted_fact_ids=frozenset(),
    )
    assert set(statuses.values()) == {EvidenceStatus.CORROBORATED}


def test_one_reporter_repeating_an_exact_fact_stays_reported() -> None:
    """Case AE: a contributor never corroborates themselves, at fact level either."""

    facts = (fact("first", "a", CAB), fact("second", "a", CAB))
    statuses = compute_statuses(
        facts=facts,
        reports=(report("a"),),
        evidence_items=(),
        roots=(),
        contradicted_fact_ids=frozenset(),
    )
    assert set(statuses.values()) == {EvidenceStatus.REPORTED}


def test_two_reporters_relying_on_one_forwarded_root_stay_reported() -> None:
    """Case AE2: a forwarded copy collapses to its origin, so the second adds no source."""

    facts = (
        fact("a", "a", CAB, evidence_labels=("original",)),
        fact("b", "b", CAB, evidence_labels=("forward",)),
    )
    statuses = compute_statuses(
        facts=facts,
        reports=(report("a"), report("b")),
        evidence_items=(item("original", "a", "one"), item("forward", "b", "two")),
        roots=(root("one"), root("two", parent="one")),
        contradicted_fact_ids=frozenset(),
    )
    assert set(statuses.values()) == {EvidenceStatus.REPORTED}


def test_different_canonical_values_are_different_claims() -> None:
    """Case AF: no similarity, no window, no judgement. Byte equality or nothing."""

    facts = (fact("a", "a", CAB), fact("b", "b", LOBBY))
    statuses = compute_statuses(
        facts=facts,
        reports=(report("a"), report("b")),
        evidence_items=(),
        roots=(),
        contradicted_fact_ids=frozenset(),
    )
    assert set(statuses.values()) == {EvidenceStatus.REPORTED}


def test_two_reporters_of_one_incident_a_minute_apart_do_not_group() -> None:
    """The accepted cost of exact equality, asserted so nobody 'fixes' it into a window."""

    first = IncidentOccurrence(occurred_at=NOW, failure_mode=FailureMode.STUCK)
    second = IncidentOccurrence(
        occurred_at=NOW + timedelta(minutes=1), failure_mode=FailureMode.STUCK
    )
    facts = (
        fact("a", "a", first, fact_type=FactType.INCIDENT_OCCURRENCE),
        fact("b", "b", second, fact_type=FactType.INCIDENT_OCCURRENCE),
    )
    statuses = compute_statuses(
        facts=facts,
        reports=(report("a"), report("b")),
        evidence_items=(),
        roots=(),
        contradicted_fact_ids=frozenset(),
    )
    assert set(statuses.values()) == {EvidenceStatus.REPORTED}


def test_the_support_key_is_the_type_and_the_canonical_value_digest() -> None:
    same = fact_support_key(fact("a", "a", CAB))
    also_same = fact_support_key(fact("b", "b", CAB))
    different_value = fact_support_key(fact("c", "c", LOBBY))
    assert same == also_same
    assert same != different_value
    assert same[0] is FactType.LOCATION_AREA


def test_the_same_value_under_a_different_type_is_a_different_claim() -> None:
    """The key leads with the type, so two types can never share a group by hash collision."""

    location = fact_support_key(fact("a", "a", CAB))
    assert location[0] is FactType.LOCATION_AREA


# -- what the computation ignores ------------------------------------------------------------


def test_a_withdrawn_fact_is_not_classified_at_all() -> None:
    facts = (fact("live", "a", CAB), fact("gone", "b", CAB, status=FactStatus.WITHDRAWN))
    statuses = compute_statuses(
        facts=facts,
        reports=(report("a"), report("b")),
        evidence_items=(),
        roots=(),
        contradicted_fact_ids=frozenset(),
    )
    assert set(statuses) == {facts[0].fact_id}
    # And the withdrawn fact cannot corroborate the live one either.
    assert statuses[facts[0].fact_id] is EvidenceStatus.REPORTED


def test_a_duplicate_reporters_fact_does_not_corroborate() -> None:
    """Case B: a duplicate report contributes no independent source at fact level."""

    facts = (fact("a", "a", CAB), fact("dupe", "dupe", CAB))
    duplicate = report("dupe", status=ReportStatus.DUPLICATE, duplicate_of="a")
    statuses = compute_statuses(
        facts=facts,
        reports=(report("a"), duplicate),
        evidence_items=(),
        roots=(),
        contradicted_fact_ids=frozenset(),
    )
    assert set(statuses.values()) == {EvidenceStatus.REPORTED}
