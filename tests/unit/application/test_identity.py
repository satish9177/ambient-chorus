"""Deterministic identity: stable for the same lineage, different for different lineage.

Both halves matter. Stability is what makes a retry finish work instead of duplicating it;
distinctness is what keeps two genuinely different things from landing at one address.

A third property matters as much and is easier to lose: *nothing the model wrote may reach an
identifier*. A summary, a title, a confidence, or a model-chosen typed value in a derivation
payload would mean a re-answer worded differently resolved to a different address, and the
duplicate that produces is exactly what ADR-011 exists to prevent. The fact-slot tests below
are written to fail if that ever creeps back in.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from chorus.application.services.identity import (
    MONITOR_DERIVED_ID_ROOTS,
    derive_audit_event_id,
    derive_candidate_case_id,
    derive_evidence_root_id,
    derive_fact_slot_id,
    derive_report_id,
)
from chorus.domain.entities import FactType
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    MessageId,
    Namespace,
    ReportId,
    Sha256Digest,
)

NAMESPACE = Namespace("TEST_IDENTITY")
OTHER_NAMESPACE = Namespace("TEST_IDENTITY_ALT")
COMMUNITY = CommunityId(uuid4())
CONTRIBUTOR = ContributorId(uuid4())
MESSAGES = (MessageId(uuid4()), MessageId(uuid4()))
EVIDENCE = (EvidenceItemId(uuid4()), EvidenceItemId(uuid4()))


def _report(**changes: object) -> ReportId:
    values: dict[str, object] = {
        "namespace": NAMESPACE,
        "community_id": COMMUNITY,
        "contributor_id": CONTRIBUTOR,
        "issue_type": "ELEVATOR_FAILURE",
        "source_message_ids": MESSAGES,
    }
    values.update(changes)
    return derive_report_id(**values)  # type: ignore[arg-type]


def test_the_same_validated_report_always_derives_the_same_identifier() -> None:
    assert _report() == _report()


def test_message_order_does_not_change_report_identity() -> None:
    """The set of source messages is the identity, not the order they were listed in."""

    assert _report() == _report(source_message_ids=tuple(reversed(MESSAGES)))


def test_a_different_owner_derives_a_different_report() -> None:
    assert _report() != _report(contributor_id=ContributorId(uuid4()))


def test_a_different_message_set_derives_a_different_report() -> None:
    assert _report() != _report(source_message_ids=(MESSAGES[0],))


def test_a_different_issue_type_derives_a_different_report() -> None:
    assert _report() != _report(issue_type="OTHER")


def test_a_different_namespace_derives_a_different_report() -> None:
    """Namespace is an isolation boundary, so identity may never collide across it."""

    assert _report() != _report(namespace=OTHER_NAMESPACE)


def _slot(**changes: object) -> object:
    values: dict[str, object] = {
        "namespace": NAMESPACE,
        "community_id": COMMUNITY,
        "report_id": _report(),
        "fact_type": FactType.INCIDENT_OCCURRENCE,
        "source_message_ids": MESSAGES,
        "evidence_ids": EVIDENCE,
    }
    values.update(changes)
    return derive_fact_slot_id(**values)  # type: ignore[arg-type]


def test_a_fact_slot_is_stable_for_one_lineage() -> None:
    assert _slot() == _slot()


def test_lineage_order_does_not_change_the_slot() -> None:
    """Sets, not sequences. A reordered answer describes the same lineage."""

    assert _slot() == _slot(source_message_ids=tuple(reversed(MESSAGES)))
    assert _slot() == _slot(evidence_ids=tuple(reversed(EVIDENCE)))


def test_two_fact_types_over_one_report_occupy_different_slots() -> None:
    assert _slot() != _slot(fact_type=FactType.LOCATION_AREA)


def test_a_different_source_lineage_is_a_different_slot() -> None:
    assert _slot() != _slot(source_message_ids=(MESSAGES[0],))


def test_a_different_evidence_lineage_is_a_different_slot() -> None:
    assert _slot() != _slot(evidence_ids=(EVIDENCE[0],))


def test_a_different_report_owns_a_different_slot() -> None:
    assert _slot() != _slot(report_id=_report(issue_type="OTHER"))


def test_the_slot_derivation_takes_no_model_authored_value() -> None:
    """The signature is the proof, so a value can never be smuggled into an address.

    A re-answer that changes only what the model *said* -- a different failure mode, a
    reworded summary, a different confidence -- must resolve to the same slot, and the surest
    way to guarantee that is for the derivation to have nowhere to put such a value. The check
    is on the parameter names rather than on two derived identifiers, because a test that
    compared outputs would still pass if a new parameter were added and defaulted.
    """

    import inspect

    parameters = set(inspect.signature(derive_fact_slot_id).parameters)
    assert parameters == {
        "namespace",
        "community_id",
        "report_id",
        "fact_type",
        "source_message_ids",
        "evidence_ids",
    }
    assert not parameters & {"value", "typed_value", "summary", "confidence", "title"}


def test_the_derived_identity_families_are_exactly_the_approved_five() -> None:
    """ADR-011 accepts a narrow exception; this is the whole of it, in one place."""

    assert set(MONITOR_DERIVED_ID_ROOTS) == {
        "REPORT",
        "FACT_SLOT",
        "CANDIDATE_CASE",
        "EVIDENCE_ROOT",
        "MONITOR_APPLY_AUDIT_EVENT",
    }
    assert len(set(MONITOR_DERIVED_ID_ROOTS.values())) == len(MONITOR_DERIVED_ID_ROOTS)


def test_a_candidate_case_is_identified_by_the_reports_that_formed_it() -> None:
    reports = (_report(), _report(source_message_ids=(MESSAGES[0],)))
    first = derive_candidate_case_id(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        issue_type="ELEVATOR_FAILURE",
        report_ids=reports,
    )
    reordered = derive_candidate_case_id(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        issue_type="ELEVATOR_FAILURE",
        report_ids=tuple(reversed(reports)),
    )
    smaller = derive_candidate_case_id(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        issue_type="ELEVATOR_FAILURE",
        report_ids=reports[:1],
    )

    assert first == reordered
    assert first != smaller


def test_a_report_and_a_case_never_collide_on_one_identifier() -> None:
    """Separate derivation roots, so identical inputs cannot produce identical identity."""

    shared = derive_candidate_case_id(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        issue_type="ELEVATOR_FAILURE",
        report_ids=(),
    )
    assert str(shared) != str(_report())


def test_an_evidence_root_is_identified_by_its_content() -> None:
    digest = Sha256Digest("sha256:" + "b" * 64)
    other = Sha256Digest("sha256:" + "c" * 64)

    assert derive_evidence_root_id(
        namespace=NAMESPACE, community_id=COMMUNITY, root_sha256=digest
    ) == derive_evidence_root_id(namespace=NAMESPACE, community_id=COMMUNITY, root_sha256=digest)
    assert derive_evidence_root_id(
        namespace=NAMESPACE, community_id=COMMUNITY, root_sha256=digest
    ) != derive_evidence_root_id(namespace=NAMESPACE, community_id=COMMUNITY, root_sha256=other)


def _audit_event(
    *,
    namespace: Namespace = NAMESPACE,
    community_id: CommunityId = COMMUNITY,
    invocation_id: UUID,
    case_id: CaseId,
) -> UUID:
    return derive_audit_event_id(
        namespace=namespace,
        community_id=community_id,
        invocation_id=invocation_id,
        case_id=case_id,
    )


def test_an_audit_row_is_named_by_its_invocation_and_case() -> None:
    invocation = uuid4()
    case = CaseId(uuid4())

    assert _audit_event(invocation_id=invocation, case_id=case) == _audit_event(
        invocation_id=invocation, case_id=case
    )
    assert _audit_event(invocation_id=invocation, case_id=case) != _audit_event(
        invocation_id=invocation, case_id=CaseId(uuid4())
    )


def test_an_audit_row_identifier_is_scoped_to_its_namespace_and_community() -> None:
    """ADR-011 domain separation, applied to the one audit family that derives its ID.

    The invocation identity is unique in practice, which is exactly why leaving namespace and
    community out of the payload was easy to miss: the derived identifiers did not collide.
    They did not collide because of a property another component happens to have, though, and
    the rule the ADR states is that a derived identifier is never a cross-tenant address --
    which has to be true of the derivation itself.
    """

    invocation = uuid4()
    case = CaseId(uuid4())
    other_community = CommunityId(uuid4())

    same = _audit_event(invocation_id=invocation, case_id=case)

    assert same != _audit_event(namespace=OTHER_NAMESPACE, invocation_id=invocation, case_id=case)
    assert same != _audit_event(
        community_id=other_community, invocation_id=invocation, case_id=case
    )
