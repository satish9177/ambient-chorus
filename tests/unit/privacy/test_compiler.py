from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import cast

import pytest
from tests.fixtures.elevator import (
    NOW,
    ElevatorFixture,
    _uuid,
    build_elevator_fixture,
)

from chorus.domain.entities import (
    DerivationKind,
    DisclosureScope,
    FactType,
    MandateStatus,
    Purpose,
    SensitivityCategory,
)
from chorus.domain.facts import (
    Fact,
    FactStatus,
    ImpactCode,
    IncidentOccurrence,
    ReportStatus,
    ServiceImpact,
)
from chorus.domain.ids import (
    CaseId,
    EvidenceItemId,
    FactId,
    MessageId,
    Namespace,
    ReportId,
    Sha256Digest,
    Uuid4Generator,
    Uuid5Generator,
)
from chorus.domain.mandates import CurrentMandatePointer, FactGrant, IdentityGrant
from chorus.privacy.canonical import hash_mandate_terms, to_canonical_primitive, verify_hash
from chorus.privacy.compiler import (
    CompileAllow,
    CompileContext,
    CompileDeny,
    PrivacyCompiler,
    ShareableCaseView,
)
from chorus.privacy.policy import (
    CompileCommand,
    CompileDecision,
    CompileReasonCode,
    CompilerGate,
    IntendedUsage,
    Necessity,
    RequestedFact,
)

ZERO_DIGEST = Sha256Digest("sha256:" + "0" * 64)


def _compiler() -> PrivacyCompiler:
    return PrivacyCompiler(
        id_generator_factory=lambda compile_id: Uuid5Generator(
            namespace=compile_id,
            prefix="compiler-test",
        )
    )


def _command(
    fixture: ElevatorFixture,
    fact_ids: tuple[FactId, ...],
    *,
    optional_ids: frozenset[FactId] = frozenset(),
    usage: IntendedUsage = IntendedUsage.CLAIM,
    evidence_ids: tuple[EvidenceItemId, ...] = (),
) -> CompileCommand:
    return CompileCommand(
        compile_id=_uuid("compile:command"),
        namespace=fixture.context.case.namespace,
        case_id=fixture.context.case.case_id,
        expected_case_version=fixture.context.case.version,
        requested_facts=tuple(
            RequestedFact(
                fact_id=fact_id,
                necessity=(Necessity.OPTIONAL if fact_id in optional_ids else Necessity.REQUIRED),
                intended_usage=usage,
            )
            for fact_id in fact_ids
        ),
        requested_evidence_ids=evidence_ids,
        destination=fixture.context.destination_registry_entry,
        purpose=fixture.context.mandates[0].allowed_purposes[0],
        requested_at=NOW,
    )


def _replace_current_mandate(
    fixture: ElevatorFixture,
    contributor_index: int,
    *,
    fact_grants: tuple[FactGrant, ...] | None = None,
    identity_grant: IdentityGrant | None = None,
    status: MandateStatus | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    bump_case: bool = True,
) -> ElevatorFixture:
    contributor = fixture.contributor_ids[contributor_index]
    old = next(item for item in fixture.context.mandates if item.contributor_id == contributor)
    new = replace(
        old,
        version=old.version + 1,
        supersedes_version=old.version,
        fact_grants=fact_grants if fact_grants is not None else old.fact_grants,
        identity_grant=identity_grant if identity_grant is not None else old.identity_grant,
        status=status if status is not None else old.status,
        expires_at=old.expires_at if expires_at is None else expires_at,
        revoked_at=old.revoked_at if revoked_at is None else revoked_at,
        terms_hash=ZERO_DIGEST,
        updated_at=NOW,
    )
    new = replace(new, terms_hash=hash_mandate_terms(new))
    mandates = tuple(
        new if item.contributor_id == contributor else item for item in fixture.context.mandates
    )
    pointers = tuple(
        CurrentMandatePointer(
            mandate_id=new.mandate_id,
            version=new.version,
            case_id=new.case_id,
            contributor_id=new.contributor_id,
            terms_hash=new.terms_hash,
        )
        if pointer.contributor_id == contributor
        else pointer
        for pointer in fixture.context.mandate_pointers
    )
    case = (
        replace(
            fixture.context.case,
            version=fixture.context.case.version + 1,
            updated_at=NOW,
        )
        if bump_case
        else fixture.context.case
    )
    return replace(
        fixture,
        context=replace(fixture.context, case=case, mandates=mandates, mandate_pointers=pointers),
    )


def _grant_scope(
    fixture: ElevatorFixture, fact_ids: tuple[FactId, ...], scope: DisclosureScope
) -> ElevatorFixture:
    result = fixture
    for contributor_index, contributor in enumerate(fixture.contributor_ids):
        mandate = next(
            item for item in result.context.mandates if item.contributor_id == contributor
        )
        changed = False
        grants: list[FactGrant] = []
        for grant in mandate.fact_grants:
            if grant.fact_id in fact_ids:
                grants.append(replace(grant, max_scope=scope))
                changed = True
            else:
                grants.append(grant)
        if changed:
            result = _replace_current_mandate(
                result,
                contributor_index,
                fact_grants=tuple(grants),
                bump_case=True,
            )
    return result


def _reason_codes(result: CompileDeny) -> set[CompileReasonCode]:
    return {reason.code for reason in result.reasons}


def test_compile_safe_example_runs_all_22_gates_and_hashes_view() -> None:
    fixture = build_elevator_fixture()
    command = _command(fixture, fixture.incident_fact_ids)

    result = _compiler().compile(command, fixture.context)

    assert isinstance(result, CompileAllow)
    assert result.decision is CompileDecision.ALLOW
    assert tuple(item.gate for item in result.audit_decisions) == tuple(CompilerGate)
    assert result.view.shareable_facts
    assert result.view.authorization_snapshot_hash == Sha256Digest(
        "sha256:7310233202afdbdf7ad8a94acf825e2c8956413fd32936127226e5b0f50b2ef7"
    )
    assert result.view.view_hash == Sha256Digest(
        "sha256:3e6db66c924482c300670489aba1dc688d8f072f1b569710af9a3a7377f63b8c"
    )
    assert verify_hash(result.view, result.view.view_hash, omit_fields=frozenset({"view_hash"}))


def test_compile_internal_fact_never_serializes() -> None:
    fixture = build_elevator_fixture()
    ids = (fixture.incident_fact_ids[0], fixture.health_fact_id, fixture.unit_fact_id)
    command = _command(
        fixture,
        ids,
        optional_ids=frozenset({fixture.health_fact_id, fixture.unit_fact_id}),
    )

    result = _compiler().compile(command, fixture.context)

    assert isinstance(result, CompileAllow)
    rendered = str(to_canonical_primitive(result.view))
    assert "SECRET_SENTINEL_MOTHER_HEALTH" not in rendered
    assert "Apartment 4B" not in rendered
    assert {item.fact_id for item in result.excluded} == {
        fixture.health_fact_id,
        fixture.unit_fact_id,
    }


def test_compile_required_internal_fact_denies_whole_request() -> None:
    fixture = build_elevator_fixture()
    command = _command(fixture, (fixture.health_fact_id,))

    result = _compiler().compile(command, fixture.context)

    assert isinstance(result, CompileDeny)
    assert CompileReasonCode.INTERNAL_ONLY in _reason_codes(result)


@pytest.mark.parametrize(
    "mutation",
    ["destination", "purpose", "refused", "stale_case"],
)
def test_policy_restrictions_fail_closed(mutation: str) -> None:
    fixture = build_elevator_fixture()
    command = _command(fixture, (fixture.incident_fact_ids[0],))
    expected = CompileReasonCode.DESTINATION_NOT_ALLOWED
    if mutation == "destination":
        command = replace(
            command,
            destination=replace(command.destination, registry_version=2),
        )
    elif mutation == "purpose":
        command = replace(command, purpose=cast(Purpose, "UNSUPPORTED_PURPOSE"))
        expected = CompileReasonCode.PURPOSE_NOT_ALLOWED
    elif mutation == "refused":
        fixture = _replace_current_mandate(fixture, 0, status=MandateStatus.REFUSED)
        command = _command(fixture, (fixture.incident_fact_ids[0],))
        expected = CompileReasonCode.MANDATE_NOT_APPROVED
    else:
        command = replace(command, expected_case_version=command.expected_case_version + 1)
        expected = CompileReasonCode.STALE_CASE_VERSION

    result = _compiler().compile(command, fixture.context)

    assert isinstance(result, CompileDeny)
    assert expected in _reason_codes(result)


def test_compile_foreign_optional_fact_denies_whole_request() -> None:
    fixture = build_elevator_fixture()
    source = fixture.context.facts[0]
    foreign = replace(
        source,
        fact_id=replace(source.fact_id, value=_uuid("fact:foreign")),
        case_id=CaseId(_uuid("case:foreign")),
    )
    context = replace(fixture.context, facts=(*fixture.context.facts, foreign))
    command = _command(
        fixture,
        (fixture.incident_fact_ids[0], foreign.fact_id),
        optional_ids=frozenset({foreign.fact_id}),
    )

    result = _compiler().compile(command, context)

    assert isinstance(result, CompileDeny)
    assert _reason_codes(result) == {CompileReasonCode.CROSS_CASE_REFERENCE}


def test_same_case_orphan_fact_denies_as_ownership_integrity_error() -> None:
    fixture = build_elevator_fixture()
    source = fixture.context.facts[0]
    orphan = replace(source, fact_id=replace(source.fact_id, value=_uuid("fact:orphan")))
    context = replace(fixture.context, facts=(*fixture.context.facts, orphan))
    command = _command(fixture, (orphan.fact_id,))

    result = _compiler().compile(command, context)

    assert isinstance(result, CompileDeny)
    assert _reason_codes(result) == {CompileReasonCode.OWNERSHIP_INTEGRITY_ERROR}


@pytest.mark.parametrize("terminal_status", [MandateStatus.REVOKED, MandateStatus.EXPIRED])
def test_revoked_or_expired_mandate_cannot_authorize(
    terminal_status: MandateStatus,
) -> None:
    fixture = build_elevator_fixture()
    if terminal_status is MandateStatus.REVOKED:
        fixture = _replace_current_mandate(
            fixture,
            0,
            status=terminal_status,
            revoked_at=NOW - timedelta(seconds=1),
        )
        expected = CompileReasonCode.MANDATE_REVOKED
    else:
        fixture = _replace_current_mandate(
            fixture,
            0,
            status=terminal_status,
            expires_at=NOW,
        )
        expected = CompileReasonCode.MANDATE_EXPIRED
    command = _command(fixture, (fixture.incident_fact_ids[0],))

    result = _compiler().compile(command, fixture.context)

    assert isinstance(result, CompileDeny)
    assert expected in _reason_codes(result)


def test_identity_requires_content_and_identity_grants() -> None:
    fixture = build_elevator_fixture()
    ids = (fixture.incident_fact_ids[1], fixture.identity_fact_id)
    command = _command(fixture, ids, optional_ids=frozenset({fixture.identity_fact_id}))

    anonymous = _compiler().compile(command, fixture.context)

    assert isinstance(anonymous, CompileAllow)
    assert fixture.identity_fact_id in {item.fact_id for item in anonymous.excluded}
    assert "Resident B" not in str(to_canonical_primitive(anonymous.view))

    fixture = _replace_current_mandate(
        fixture,
        1,
        identity_grant=IdentityGrant(
            externally_shareable=True,
            max_scope=DisclosureScope.NAMED_CASE,
        ),
    )
    command = _command(fixture, ids, optional_ids=frozenset({fixture.identity_fact_id}))
    named = _compiler().compile(command, fixture.context)

    assert isinstance(named, CompileAllow)
    assert "Resident B" in str(to_canonical_primitive(named.view))


def test_external_action_identity_requires_external_action_identity_scope() -> None:
    fixture = build_elevator_fixture()
    fixture = _grant_scope(
        fixture,
        (fixture.identity_fact_id,),
        DisclosureScope.EXTERNAL_ACTION,
    )
    fixture = _replace_current_mandate(
        fixture,
        1,
        identity_grant=IdentityGrant(
            externally_shareable=True,
            max_scope=DisclosureScope.NAMED_CASE,
        ),
    )
    ids = (fixture.incident_fact_ids[1], fixture.identity_fact_id)
    command = _command(fixture, ids, optional_ids=frozenset({fixture.identity_fact_id}))

    named_only = _compiler().compile(command, fixture.context)

    assert isinstance(named_only, CompileAllow)
    excluded = next(
        item for item in named_only.excluded if item.fact_id == fixture.identity_fact_id
    )
    assert excluded.reason_codes == (CompileReasonCode.IDENTITY_NOT_ALLOWED,)

    fixture = _replace_current_mandate(
        fixture,
        1,
        identity_grant=IdentityGrant(
            externally_shareable=True,
            max_scope=DisclosureScope.EXTERNAL_ACTION,
        ),
    )
    externally_named = _compiler().compile(
        _command(fixture, ids, optional_ids=frozenset({fixture.identity_fact_id})),
        fixture.context,
    )

    assert isinstance(externally_named, CompileAllow)
    assert "Resident B" in str(to_canonical_primitive(externally_named.view))


def _aggregate_fixture(count: int) -> tuple[ElevatorFixture, tuple[FactId, ...]]:
    fixture = build_elevator_fixture()
    selected = fixture.incident_fact_ids[:count]
    changed_facts: list[Fact] = []
    for fact in fixture.context.facts:
        if fact.fact_id not in selected:
            changed_facts.append(fact)
            continue
        selected_index = selected.index(fact.fact_id)
        changed_facts.append(
            replace(
                fact,
                fact_type=FactType.SERVICE_IMPACT,
                value=ServiceImpact(
                    impact_code=(
                        ImpactCode.DELAY if selected_index % 2 else ImpactCode.ACCESS_BLOCKED
                    ),
                    summary="private relationship and health narrative",
                ),
                version=fact.version + 1,
                updated_at=NOW,
            )
        )
    fixture = replace(
        fixture,
        context=replace(
            fixture.context,
            facts=tuple(changed_facts),
            case=replace(
                fixture.context.case,
                version=fixture.context.case.version + 1,
                updated_at=NOW,
            ),
        ),
    )
    return _grant_scope(fixture, selected, DisclosureScope.AGGREGATE_ONLY), selected


def test_aggregate_three_contributors_is_not_corroboration_two() -> None:
    below, below_ids = _aggregate_fixture(2)
    below_result = _compiler().compile(
        _command(below, below_ids, usage=IntendedUsage.AGGREGATION_INPUT), below.context
    )

    assert isinstance(below_result, CompileDeny)
    assert CompileReasonCode.AGGREGATE_PRIVACY_MIN_NOT_MET in _reason_codes(below_result)
    assert below.context.case.corroboration_source_count >= 2

    valid, valid_ids = _aggregate_fixture(3)
    valid_result = _compiler().compile(
        _command(valid, valid_ids, usage=IntendedUsage.AGGREGATION_INPUT), valid.context
    )

    assert isinstance(valid_result, CompileAllow)
    assert valid_result.view.shareable_facts[0].contributor_count == 3


def test_gate18_rejects_aggregate_with_one_category_bucket() -> None:
    fixture, selected = _aggregate_fixture(3)
    facts = tuple(
        replace(
            fact,
            value=ServiceImpact(
                impact_code=ImpactCode.ACCESS_BLOCKED,
                summary="private relationship and health narrative",
            ),
        )
        if fact.fact_id in selected
        else fact
        for fact in fixture.context.facts
    )
    fixture = replace(fixture, context=replace(fixture.context, facts=facts))

    result = _compiler().compile(
        _command(fixture, selected, usage=IntendedUsage.AGGREGATION_INPUT), fixture.context
    )

    assert isinstance(result, CompileDeny)
    assert _reason_codes(result) == {CompileReasonCode.REIDENTIFICATION_RISK}
    assert result.audit_decisions[-1].gate is CompilerGate.REIDENTIFICATION


def test_gate18_incident_transform_generalizes_precise_time_to_day() -> None:
    fixture = build_elevator_fixture()
    source = next(
        fact for fact in fixture.context.facts if fact.fact_id == fixture.incident_fact_ids[0]
    )
    assert isinstance(source.value, IncidentOccurrence)

    result = _compiler().compile(_command(fixture, (source.fact_id,)), fixture.context)

    assert isinstance(result, CompileAllow)
    output = result.view.shareable_facts[0]
    assert source.value.occurred_at.date().isoformat() in output.safe_text
    assert source.value.occurred_at.time().isoformat() not in output.safe_text
    assert output.transformation_rule_id == "p1.incident.anonymous.v1"


def test_gate18_typed_impact_transform_drops_relationship_and_health_narrative() -> None:
    fixture, selected = _aggregate_fixture(3)

    result = _compiler().compile(
        _command(fixture, selected, usage=IntendedUsage.AGGREGATION_INPUT), fixture.context
    )

    assert isinstance(result, CompileAllow)
    rendered = str(to_canonical_primitive(result.view))
    assert "relationship" not in rendered
    assert "health narrative" not in rendered


def test_gate18_rejects_direct_management_quote_even_if_mislabeled_general() -> None:
    fixture = build_elevator_fixture()
    facts = tuple(
        replace(fact, sensitivity=SensitivityCategory.GENERAL, version=fact.version + 1)
        if fact.fact_id == fixture.management_fact_id
        else fact
        for fact in fixture.context.facts
    )
    fixture = replace(
        fixture,
        context=replace(
            fixture.context,
            facts=facts,
            case=replace(fixture.context.case, version=2, updated_at=NOW),
        ),
    )
    command = _command(
        fixture,
        (fixture.incident_fact_ids[0], fixture.management_fact_id),
        optional_ids=frozenset({fixture.management_fact_id}),
    )

    result = _compiler().compile(command, fixture.context)

    assert isinstance(result, CompileAllow)
    excluded = next(item for item in result.excluded if item.fact_id == fixture.management_fact_id)
    assert excluded.reason_codes == (CompileReasonCode.REIDENTIFICATION_RISK,)


def test_prompt_injection_evidence_is_data_and_has_no_policy_authority() -> None:
    fixture = build_elevator_fixture()
    facts = tuple(
        replace(fact, sensitivity=SensitivityCategory.GENERAL, version=fact.version + 1)
        if fact.fact_id == fixture.prompt_fact_id
        else fact
        for fact in fixture.context.facts
    )
    fixture = replace(
        fixture,
        context=replace(
            fixture.context,
            facts=facts,
            case=replace(fixture.context.case, version=2, updated_at=NOW),
        ),
    )
    command = _command(
        fixture,
        (fixture.prompt_fact_id,),
        usage=IntendedUsage.EVIDENCE,
        evidence_ids=(fixture.prompt_evidence_id,),
    )

    result = _compiler().compile(command, fixture.context)

    assert isinstance(result, CompileDeny)
    assert CompileReasonCode.UNSAFE_EVIDENCE in _reason_codes(result)


def test_reviewed_photo_exports_only_opaque_safe_reference() -> None:
    fixture = build_elevator_fixture()
    command = _command(
        fixture,
        (fixture.photo_fact_id,),
        usage=IntendedUsage.EVIDENCE,
        evidence_ids=(fixture.photo_evidence_id,),
    )

    result = _compiler().compile(command, fixture.context)

    assert isinstance(result, CompileAllow)
    assert len(result.view.safe_evidence_refs) == 1
    rendered = str(to_canonical_primitive(result.view))
    assert str(fixture.photo_evidence_id) not in rendered
    assert "private/" not in rendered


def test_deterministic_fixture_generator_repeats_safe_artifact_ids() -> None:
    fixture = build_elevator_fixture()
    command = _command(
        fixture,
        (fixture.photo_fact_id,),
        usage=IntendedUsage.EVIDENCE,
        evidence_ids=(fixture.photo_evidence_id,),
    )

    first = _compiler().compile(command, fixture.context)
    replay = _compiler().compile(command, fixture.context)

    assert isinstance(first, CompileAllow)
    assert isinstance(replay, CompileAllow)
    assert first == replay


def test_one_safe_evidence_source_produces_one_ref_when_shared_by_two_facts() -> None:
    fixture = build_elevator_fixture()
    source_fact = next(
        fact for fact in fixture.context.facts if fact.fact_id == fixture.photo_fact_id
    )
    source_report = next(
        report for report in fixture.context.reports if report.report_id == source_fact.report_id
    )
    second_fact_id = FactId(_uuid("fact:photo-description-two"))
    second_report_id = ReportId(_uuid("report:photo-description-two"))
    second_message_id = MessageId(_uuid("message:photo-description-two"))
    second_report = replace(
        source_report,
        report_id=second_report_id,
        source_message_ids=(second_message_id,),
    )
    second_fact = replace(
        source_fact,
        fact_id=second_fact_id,
        report_id=second_report_id,
        source_message_ids=(second_message_id,),
    )
    fixture = replace(
        fixture,
        context=replace(
            fixture.context,
            facts=(*fixture.context.facts, second_fact),
            reports=(*fixture.context.reports, second_report),
            case=replace(
                fixture.context.case,
                fact_ids=(*fixture.context.case.fact_ids, second_fact_id),
                report_ids=(*fixture.context.case.report_ids, second_report_id),
                version=fixture.context.case.version + 1,
                updated_at=NOW,
            ),
        ),
    )
    mandate = next(
        item
        for item in fixture.context.mandates
        if item.contributor_id == source_fact.contributor_id
    )
    source_grant = next(
        grant for grant in mandate.fact_grants if grant.fact_id == source_fact.fact_id
    )
    fixture = _replace_current_mandate(
        fixture,
        2,
        fact_grants=(
            *mandate.fact_grants,
            replace(source_grant, fact_id=second_fact_id),
        ),
    )
    command = _command(
        fixture,
        (source_fact.fact_id, second_fact_id),
        usage=IntendedUsage.EVIDENCE,
        evidence_ids=(fixture.photo_evidence_id,),
    )

    result = _compiler().compile(command, fixture.context)

    assert isinstance(result, CompileAllow)
    assert len(result.view.safe_evidence_refs) == 1
    safe_ref_id = result.view.safe_evidence_refs[0].safe_evidence_ref_id
    assert all(fact.safe_evidence_ref_ids == (safe_ref_id,) for fact in result.view.shareable_facts)


def test_minimum_necessary_excludes_permitted_but_unnecessary_evidence_fact() -> None:
    fixture = build_elevator_fixture()
    command = _command(
        fixture,
        (fixture.incident_fact_ids[0], fixture.photo_fact_id),
        optional_ids=frozenset({fixture.photo_fact_id}),
    )

    result = _compiler().compile(command, fixture.context)

    assert isinstance(result, CompileAllow)
    excluded = next(item for item in result.excluded if item.fact_id == fixture.photo_fact_id)
    assert excluded.reason_codes == (CompileReasonCode.NOT_MINIMUM_NECESSARY,)


def test_authorization_change_alters_snapshot_and_immutable_view_hash() -> None:
    fixture = build_elevator_fixture()
    ids = (fixture.incident_fact_ids[0], fixture.health_fact_id)
    first = _compiler().compile(
        _command(fixture, ids, optional_ids=frozenset({fixture.health_fact_id})), fixture.context
    )
    assert isinstance(first, CompileAllow)
    health_mandate = next(
        item
        for item in fixture.context.mandates
        if item.contributor_id == fixture.contributor_ids[1]
    )
    changed_grants = tuple(
        replace(grant, allow_safe_transformation=False)
        if grant.fact_id == fixture.health_fact_id
        else grant
        for grant in health_mandate.fact_grants
    )
    changed = _replace_current_mandate(fixture, 1, fact_grants=changed_grants)
    second = _compiler().compile(
        _command(changed, ids, optional_ids=frozenset({fixture.health_fact_id})), changed.context
    )

    assert isinstance(second, CompileAllow)
    assert first.view.authorization_snapshot_hash != second.view.authorization_snapshot_hash
    assert first.view.view_hash != second.view.view_hash


def test_report_status_change_alters_authorization_snapshot_without_case_bump() -> None:
    fixture = build_elevator_fixture()
    command = _command(fixture, fixture.incident_fact_ids)
    first = _compiler().compile(command, fixture.context)
    changed_report_id = next(
        fact.report_id
        for fact in fixture.context.facts
        if fact.fact_id == fixture.incident_fact_ids[0]
    )
    changed_reports = tuple(
        replace(report, status=ReportStatus.RETRACTED, version=report.version + 1)
        if report.report_id == changed_report_id
        else report
        for report in fixture.context.reports
    )
    changed_context = replace(fixture.context, reports=changed_reports)

    second = _compiler().compile(command, changed_context)

    assert isinstance(first, CompileAllow)
    assert isinstance(second, CompileAllow)
    assert first.view.authorization_snapshot_hash != second.view.authorization_snapshot_hash
    assert first.view.view_hash != second.view.view_hash


def test_duplicate_reports_cannot_satisfy_compiler_corroboration_gate() -> None:
    fixture = build_elevator_fixture()
    original_report = next(
        report
        for report in fixture.context.reports
        if report.contributor_id == fixture.contributor_ids[0]
    )
    reports = tuple(
        replace(
            report,
            status=ReportStatus.DUPLICATE,
            duplicate_of_report_id=original_report.report_id,
        )
        if report.contributor_id != fixture.contributor_ids[0]
        else report
        for report in fixture.context.reports
    )
    context = replace(fixture.context, reports=reports)

    result = _compiler().compile(
        _command(fixture, fixture.incident_fact_ids),
        context,
    )

    assert isinstance(result, CompileDeny)
    assert _reason_codes(result) == {CompileReasonCode.CORROBORATION_MIN_NOT_MET}
    assert result.audit_decisions[-1].gate is CompilerGate.INDEPENDENCE


def _reverse_context(context: CompileContext) -> CompileContext:
    return replace(
        context,
        facts=tuple(reversed(context.facts)),
        reports=tuple(reversed(context.reports)),
        evidence_items=tuple(reversed(context.evidence_items)),
        evidence_roots=tuple(reversed(context.evidence_roots)),
        mandates=tuple(reversed(context.mandates)),
        mandate_pointers=tuple(reversed(context.mandate_pointers)),
        safe_evidence_candidates=tuple(reversed(context.safe_evidence_candidates)),
    )


def _context_without_photo_evidence(
    fixture: ElevatorFixture,
    *,
    fact_status: FactStatus,
    report_status: ReportStatus = ReportStatus.ACTIVE,
) -> CompileContext:
    photo_fact = next(
        fact for fact in fixture.context.facts if fact.fact_id == fixture.photo_fact_id
    )
    return replace(
        fixture.context,
        facts=tuple(
            replace(fact, status=fact_status) if fact.fact_id == photo_fact.fact_id else fact
            for fact in fixture.context.facts
        ),
        reports=tuple(
            replace(report, status=report_status)
            if report.report_id == photo_fact.report_id
            else report
            for report in fixture.context.reports
        ),
        evidence_items=tuple(
            item
            for item in fixture.context.evidence_items
            if item.evidence_id != fixture.photo_evidence_id
        ),
    )


def test_foreign_corroboration_evidence_namespace_denies_as_integrity_failure() -> None:
    fixture = build_elevator_fixture()
    context = replace(
        fixture.context,
        evidence_items=tuple(
            replace(item, namespace=Namespace("TEST_FOREIGN"))
            if item.evidence_id == fixture.photo_evidence_id
            else item
            for item in fixture.context.evidence_items
        ),
    )

    result = _compiler().compile(
        _command(fixture, fixture.incident_fact_ids),
        context,
    )

    assert isinstance(result, CompileDeny)
    assert _reason_codes(result) == {CompileReasonCode.OWNERSHIP_INTEGRITY_ERROR}
    assert result.audit_decisions[-1].gate is CompilerGate.INDEPENDENCE


def test_foreign_corroboration_evidence_namespace_denial_is_order_independent() -> None:
    fixture = build_elevator_fixture()
    context = replace(
        fixture.context,
        evidence_items=tuple(
            replace(item, namespace=Namespace("TEST_FOREIGN"))
            if item.evidence_id == fixture.photo_evidence_id
            else item
            for item in fixture.context.evidence_items
        ),
    )
    command = _command(fixture, fixture.incident_fact_ids)

    forward = _compiler().compile(command, context)
    reverse = _compiler().compile(command, _reverse_context(context))

    assert isinstance(forward, CompileDeny)
    assert forward == reverse


def test_requested_evidence_root_ancestry_namespace_mismatch_denies_ownership() -> None:
    fixture = build_elevator_fixture()
    photo_item = next(
        item
        for item in fixture.context.evidence_items
        if item.evidence_id == fixture.photo_evidence_id
    )
    parent_root = next(
        root
        for root in fixture.context.evidence_roots
        if root.root_id
        == next(
            item.root_id
            for item in fixture.context.evidence_items
            if item.evidence_id == fixture.commitment_evidence_id
        )
    )
    context = replace(
        fixture.context,
        facts=tuple(
            replace(fact, status=FactStatus.WITHDRAWN)
            if fact.fact_id == fixture.photo_fact_id
            else fact
            for fact in fixture.context.facts
        ),
        evidence_roots=tuple(
            replace(
                root,
                derivation_kind=DerivationKind.FORWARDED,
                parent_root_id=parent_root.root_id,
            )
            if root.root_id == photo_item.root_id
            else replace(root, namespace=Namespace("TEST_FOREIGN"))
            if root.root_id == parent_root.root_id
            else root
            for root in fixture.context.evidence_roots
        ),
    )

    result = _compiler().compile(
        _command(
            fixture,
            (fixture.photo_fact_id,),
            usage=IntendedUsage.EVIDENCE,
            evidence_ids=(fixture.photo_evidence_id,),
        ),
        context,
    )

    assert isinstance(result, CompileDeny)
    assert _reason_codes(result) == {CompileReasonCode.OWNERSHIP_INTEGRITY_ERROR}
    assert result.audit_decisions[-1].gate is CompilerGate.OWNERSHIP


def test_withdrawn_fact_missing_evidence_is_ignored_without_snapshot_exception() -> None:
    fixture = build_elevator_fixture()
    context = _context_without_photo_evidence(fixture, fact_status=FactStatus.WITHDRAWN)
    command = _command(fixture, fixture.incident_fact_ids)

    forward = _compiler().compile(command, context)
    reverse = _compiler().compile(command, _reverse_context(context))

    assert isinstance(forward, CompileAllow)
    assert forward == reverse


@pytest.mark.parametrize("report_status", [ReportStatus.ACTIVE, ReportStatus.RETRACTED])
def test_active_fact_missing_evidence_denies_without_snapshot_exception(
    report_status: ReportStatus,
) -> None:
    fixture = build_elevator_fixture()
    context = _context_without_photo_evidence(
        fixture,
        fact_status=FactStatus.ACTIVE,
        report_status=report_status,
    )
    command = _command(fixture, fixture.incident_fact_ids)

    forward = _compiler().compile(command, context)
    reverse = _compiler().compile(command, _reverse_context(context))

    assert isinstance(forward, CompileDeny)
    assert _reason_codes(forward) == {CompileReasonCode.OWNERSHIP_INTEGRITY_ERROR}
    assert forward.audit_decisions[-1].gate is CompilerGate.INDEPENDENCE
    assert forward == reverse


def test_uuid4_generator_factory_produces_normal_operation_artifact_ids() -> None:
    fixture = build_elevator_fixture()

    result = PrivacyCompiler(id_generator_factory=lambda _compile_id: Uuid4Generator()).compile(
        _command(fixture, fixture.incident_fact_ids),
        fixture.context,
    )

    assert isinstance(result, CompileAllow)
    generated_ids = (
        result.audit_event_id,
        result.view.view_id.value,
        *(fact.export_fact_id.value for fact in result.view.shareable_facts),
    )
    assert all(identifier.version == 4 for identifier in generated_ids)


def test_deterministic_fixture_generator_repeated_compile_is_reproducible() -> None:
    fixture = build_elevator_fixture()
    compiler = _compiler()
    command = _command(fixture, fixture.incident_fact_ids)

    first = compiler.compile(command, fixture.context)
    replay = compiler.compile(command, fixture.context)

    assert isinstance(first, CompileAllow)
    assert isinstance(replay, CompileAllow)
    assert first == replay


def test_permuted_unordered_inputs_produce_identical_complete_view() -> None:
    fixture = build_elevator_fixture()
    forward = _compiler().compile(_command(fixture, fixture.incident_fact_ids), fixture.context)
    reversed_context = replace(
        fixture.context,
        facts=tuple(reversed(fixture.context.facts)),
        reports=tuple(reversed(fixture.context.reports)),
        evidence_items=tuple(reversed(fixture.context.evidence_items)),
        evidence_roots=tuple(reversed(fixture.context.evidence_roots)),
        mandates=tuple(reversed(fixture.context.mandates)),
        mandate_pointers=tuple(reversed(fixture.context.mandate_pointers)),
        safe_evidence_candidates=tuple(reversed(fixture.context.safe_evidence_candidates)),
    )
    reverse = _compiler().compile(
        _command(fixture, tuple(reversed(fixture.incident_fact_ids))), reversed_context
    )

    assert isinstance(forward, CompileAllow)
    assert isinstance(reverse, CompileAllow)
    assert forward.view == reverse.view


def test_deterministic_fixture_generator_scopes_artifacts_to_compile_id() -> None:
    fixture = build_elevator_fixture()
    command = _command(fixture, fixture.incident_fact_ids)

    first = _compiler().compile(command, fixture.context)
    second = _compiler().compile(
        replace(command, compile_id=_uuid("compile:new-command")), fixture.context
    )

    assert isinstance(first, CompileAllow)
    assert isinstance(second, CompileAllow)
    assert first.view.authorization_snapshot_hash == second.view.authorization_snapshot_hash
    assert first.view.view_id != second.view.view_id
    assert first.view.view_hash != second.view.view_hash


def _context_with_conflicting_duplicate(
    fixture: ElevatorFixture, collection: str, *, reverse_order: bool
) -> CompileContext:
    context = fixture.context
    if collection == "facts":
        source_fact = context.facts[0]
        fact_records = (*context.facts, replace(source_fact, status=FactStatus.WITHDRAWN))
        return replace(
            context,
            facts=tuple(reversed(fact_records)) if reverse_order else fact_records,
        )
    if collection == "reports":
        source_report = context.reports[0]
        report_records = (
            *context.reports,
            replace(source_report, status=ReportStatus.RETRACTED),
        )
        return replace(
            context,
            reports=tuple(reversed(report_records)) if reverse_order else report_records,
        )
    if collection == "evidence_items":
        source_evidence = context.evidence_items[0]
        evidence_records = (
            *context.evidence_items,
            replace(source_evidence, byte_length=source_evidence.byte_length + 1),
        )
        return replace(
            context,
            evidence_items=(
                tuple(reversed(evidence_records)) if reverse_order else evidence_records
            ),
        )
    if collection == "evidence_roots":
        source_root = context.evidence_roots[0]
        root_records = (*context.evidence_roots, replace(source_root, media_type="image/png"))
        return replace(
            context,
            evidence_roots=tuple(reversed(root_records)) if reverse_order else root_records,
        )
    if collection == "mandate_pointers":
        source_pointer = context.mandate_pointers[0]
        pointer_records = (
            *context.mandate_pointers,
            replace(source_pointer, terms_hash=ZERO_DIGEST),
        )
        return replace(
            context,
            mandate_pointers=(
                tuple(reversed(pointer_records)) if reverse_order else pointer_records
            ),
        )
    if collection == "mandates":
        source_mandate = context.mandates[0]
        mandate_records = (
            *context.mandates,
            replace(source_mandate, terms_hash=ZERO_DIGEST),
        )
        return replace(
            context,
            mandates=tuple(reversed(mandate_records)) if reverse_order else mandate_records,
        )
    if collection == "safe_evidence_candidates":
        source_safe_evidence = context.safe_evidence_candidates[0]
        safe_evidence_records = (
            *context.safe_evidence_candidates,
            replace(source_safe_evidence, caption="Conflicting review"),
        )
        return replace(
            context,
            safe_evidence_candidates=(
                tuple(reversed(safe_evidence_records)) if reverse_order else safe_evidence_records
            ),
        )
    raise AssertionError("unsupported duplicate collection")


@pytest.mark.parametrize(
    ("collection", "expected_gate", "expected_reason"),
    [
        (
            "facts",
            CompilerGate.OWNERSHIP,
            CompileReasonCode.OWNERSHIP_INTEGRITY_ERROR,
        ),
        (
            "reports",
            CompilerGate.OWNERSHIP,
            CompileReasonCode.OWNERSHIP_INTEGRITY_ERROR,
        ),
        (
            "evidence_items",
            CompilerGate.OWNERSHIP,
            CompileReasonCode.OWNERSHIP_INTEGRITY_ERROR,
        ),
        (
            "evidence_roots",
            CompilerGate.OWNERSHIP,
            CompileReasonCode.OWNERSHIP_INTEGRITY_ERROR,
        ),
        (
            "mandate_pointers",
            CompilerGate.CURRENT_MANDATE_SELECTION,
            CompileReasonCode.MANDATE_INTEGRITY_ERROR,
        ),
        (
            "mandates",
            CompilerGate.MANDATE_VERSION_INTEGRITY,
            CompileReasonCode.MANDATE_INTEGRITY_ERROR,
        ),
        (
            "safe_evidence_candidates",
            CompilerGate.EVIDENCE_SAFETY,
            CompileReasonCode.UNSAFE_EVIDENCE,
        ),
    ],
)
def test_duplicate_structural_records_deny_independent_of_input_order(
    collection: str,
    expected_gate: CompilerGate,
    expected_reason: CompileReasonCode,
) -> None:
    fixture = build_elevator_fixture()
    command = _command(fixture, fixture.incident_fact_ids)

    forward = _compiler().compile(
        command,
        _context_with_conflicting_duplicate(fixture, collection, reverse_order=False),
    )
    reverse = _compiler().compile(
        command,
        _context_with_conflicting_duplicate(fixture, collection, reverse_order=True),
    )

    assert isinstance(forward, CompileDeny)
    assert isinstance(reverse, CompileDeny)
    assert forward == reverse
    assert _reason_codes(forward) == {expected_reason}
    assert forward.audit_decisions[-1].gate is expected_gate


def test_shareable_view_prevents_accidental_direct_construction() -> None:
    with pytest.raises(TypeError):
        ShareableCaseView()
