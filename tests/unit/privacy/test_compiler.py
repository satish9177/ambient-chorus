from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import cast

import pytest
from tests.fixtures.elevator import (
    FIXTURE_NAMESPACE_UUID,
    NOW,
    ElevatorFixture,
    _uuid,
    build_elevator_fixture,
)

from chorus.domain.entities import (
    DisclosureScope,
    FactType,
    MandateStatus,
    Purpose,
    SensitivityCategory,
)
from chorus.domain.facts import ImpactCode, ServiceImpact
from chorus.domain.ids import CaseId, EvidenceItemId, FactId, Sha256Digest, Uuid5Generator
from chorus.domain.mandates import CurrentMandatePointer, FactGrant, IdentityGrant
from chorus.privacy.canonical import hash_mandate_terms, to_canonical_primitive, verify_hash
from chorus.privacy.compiler import (
    CompileAllow,
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
    return PrivacyCompiler(Uuid5Generator(FIXTURE_NAMESPACE_UUID, prefix="compile-test"))


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
        "sha256:21c30df22f8b545bda103f0645b93898f5fccbf6b4aefac21066e92c56160120"
    )
    assert result.view.view_hash == Sha256Digest(
        "sha256:cf1dac40eec5cc4fb6193a41a9b27a5ab12a4f2c2a60d081d6047c074512a34d"
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


def _aggregate_fixture(count: int) -> tuple[ElevatorFixture, tuple[FactId, ...]]:
    fixture = build_elevator_fixture()
    selected = fixture.incident_fact_ids[:count]
    changed_facts = tuple(
        replace(
            fact,
            fact_type=FactType.SERVICE_IMPACT,
            value=ServiceImpact(impact_code=ImpactCode.ACCESS_BLOCKED, summary="private"),
            version=fact.version + 1,
            updated_at=NOW,
        )
        if fact.fact_id in selected
        else fact
        for fact in fixture.context.facts
    )
    fixture = replace(
        fixture,
        context=replace(
            fixture.context,
            facts=changed_facts,
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


def test_minimum_necessary_excludes_permitted_but_unnecessary_fact() -> None:
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


def test_permuted_unordered_inputs_produce_identical_view_hash() -> None:
    fixture = build_elevator_fixture()
    forward = _compiler().compile(_command(fixture, fixture.incident_fact_ids), fixture.context)
    reversed_context = replace(
        fixture.context,
        facts=tuple(reversed(fixture.context.facts)),
        reports=tuple(reversed(fixture.context.reports)),
        mandates=tuple(reversed(fixture.context.mandates)),
        mandate_pointers=tuple(reversed(fixture.context.mandate_pointers)),
    )
    reverse = _compiler().compile(
        _command(fixture, tuple(reversed(fixture.incident_fact_ids))), reversed_context
    )

    assert isinstance(forward, CompileAllow)
    assert isinstance(reverse, CompileAllow)
    assert forward.view.view_hash == reverse.view.view_hash


def test_shareable_view_has_no_public_constructor() -> None:
    with pytest.raises(TypeError):
        ShareableCaseView()  # type: ignore[call-arg]
