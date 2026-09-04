"""policy/v1 ceilings: what a contributor may be offered, and what they may never grant.

The two ceilings are proved separately and proved to stay separate. A content grant on an
identity fact never produces identity permission, and an identity grant never widens what may
be said about an incident.

The scope table is exercised exhaustively rather than by example, because it is written out by
hand precisely so that nobody computes it from enum order -- and an exhaustive check is the only
thing that notices if somebody later does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from chorus.domain.entities import (
    DisclosureScope,
    EvidenceStatus,
    FactType,
    Purpose,
    SensitivityCategory,
)
from chorus.domain.facts import (
    EvidenceDescription,
    EvidenceMediaKind,
    Fact,
    FactStatus,
    FactValue,
    FailureMode,
    HealthDetail,
    IdentityAttribute,
    ImpactCode,
    IncidentOccurrence,
    LocationArea,
    LocationAreaCode,
    ManagementStatement,
    ServiceImpact,
    SubjectRelation,
    UnitLocation,
)
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    DestinationId,
    FactId,
    MessageId,
    Namespace,
    ReportId,
)
from chorus.domain.mandates import FactGrant, IdentityGrant
from chorus.privacy.mandates import (
    PROPOSAL_IDENTITY_GRANT,
    build_proposed_grants,
    grantable_facts,
    validate_destinations_and_purposes,
    validate_requested_grants,
)
from chorus.privacy.policy import (
    SCOPE_PERMITS,
    MandateDenialCode,
    identity_maximum_scope,
    policy_maximum_scope,
    proposed_scope,
    scope_permits,
)

NAMESPACE = Namespace("TEST_POLICY")
NOW = datetime(2030, 1, 20, 12, 0, 0, tzinfo=UTC)
CASE_ID = CaseId(UUID("11111111-1111-4111-8111-111111111111"))
OTHER_CASE_ID = CaseId(UUID("1a111111-1111-4111-8111-111111111111"))
COMMUNITY_ID = CommunityId(UUID("22222222-2222-4222-8222-222222222222"))
OTHER_COMMUNITY_ID = CommunityId(UUID("2a222222-2222-4222-8222-222222222222"))
OWNER = ContributorId(UUID("33333333-3333-4333-8333-333333333333"))
NEIGHBOUR = ContributorId(UUID("44444444-4444-4444-8444-444444444444"))
REPORT_ID = ReportId(UUID("55555555-5555-4555-8555-555555555555"))
MESSAGE_ID = MessageId(UUID("66666666-6666-4666-8666-666666666666"))
DESTINATION = DestinationId("property_manager:demo")
OTHER_DESTINATION = DestinationId("property_manager:other")

VALUES: dict[FactType, FactValue] = {
    FactType.INCIDENT_OCCURRENCE: IncidentOccurrence(
        occurred_at=NOW - timedelta(days=1), failure_mode=FailureMode.STUCK
    ),
    FactType.SERVICE_IMPACT: ServiceImpact(impact_code=ImpactCode.DELAY, summary="delayed"),
    FactType.LOCATION_AREA: LocationArea(area=LocationAreaCode.ELEVATOR_CAB),
    FactType.IDENTITY_ATTRIBUTE: IdentityAttribute(display_name="Resident B"),
    FactType.UNIT_LOCATION: UnitLocation(unit_label="4B"),
    FactType.HEALTH_DETAIL: HealthDetail(subject_relation=SubjectRelation.FAMILY, detail="asthma"),
    FactType.MANAGEMENT_STATEMENT: ManagementStatement(
        statement="nobody else reported it", speaker_org="Management", stated_at=NOW
    ),
    FactType.EVIDENCE_DESCRIPTION: EvidenceDescription(
        description="an elevator error photo", media_kind=EvidenceMediaKind.IMAGE
    ),
}

DEFAULT_SENSITIVITY: dict[FactType, SensitivityCategory] = {
    FactType.IDENTITY_ATTRIBUTE: SensitivityCategory.IDENTITY,
    FactType.UNIT_LOCATION: SensitivityCategory.UNIT_LOCATION,
    FactType.HEALTH_DETAIL: SensitivityCategory.HEALTH,
}


def fact(
    fact_type: FactType,
    *,
    fact_id: str = "77777777-7777-4777-8777-777777777777",
    contributor_id: ContributorId = OWNER,
    case_id: CaseId = CASE_ID,
    community_id: CommunityId = COMMUNITY_ID,
    sensitivity: SensitivityCategory | None = None,
    status: FactStatus = FactStatus.ACTIVE,
) -> Fact:
    return Fact(
        fact_id=FactId(UUID(fact_id)),
        case_id=case_id,
        report_id=REPORT_ID,
        community_id=community_id,
        contributor_id=contributor_id,
        namespace=NAMESPACE,
        fact_type=fact_type,
        value=VALUES[fact_type],
        sensitivity=sensitivity or DEFAULT_SENSITIVITY.get(fact_type, SensitivityCategory.GENERAL),
        evidence_ids=(),
        evidence_status=EvidenceStatus.REPORTED,
        source_message_ids=(MESSAGE_ID,),
        supersedes_fact_id=None,
        status=status,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


# -- the scope table --------------------------------------------------------------------


@pytest.mark.parametrize("ceiling", list(DisclosureScope))
@pytest.mark.parametrize("requested", list(DisclosureScope))
def test_scope_permits_matches_the_explicit_table_in_every_cell(
    ceiling: DisclosureScope, requested: DisclosureScope
) -> None:
    assert scope_permits(ceiling, requested) is (requested in SCOPE_PERMITS[ceiling])


@pytest.mark.parametrize("ceiling", list(DisclosureScope))
def test_every_ceiling_permits_itself_and_internal_only(ceiling: DisclosureScope) -> None:
    """Narrowing to nothing is always available, and a ceiling is always reachable."""

    assert scope_permits(ceiling, ceiling)
    assert scope_permits(ceiling, DisclosureScope.INTERNAL_ONLY)


def test_internal_only_permits_nothing_else() -> None:
    assert SCOPE_PERMITS[DisclosureScope.INTERNAL_ONLY] == frozenset(
        {DisclosureScope.INTERNAL_ONLY}
    )


def test_the_table_covers_every_scope_and_is_not_derived_from_enum_order() -> None:
    assert set(SCOPE_PERMITS) == set(DisclosureScope)
    # NAMED_CASE does not permit EXTERNAL_ACTION even though it appears later in the enum.
    assert not scope_permits(DisclosureScope.NAMED_CASE, DisclosureScope.EXTERNAL_ACTION)


# -- ceilings per fact type -------------------------------------------------------------


@pytest.mark.parametrize(
    ("fact_type", "expected"),
    [
        (FactType.INCIDENT_OCCURRENCE, DisclosureScope.EXTERNAL_ACTION),
        (FactType.SERVICE_IMPACT, DisclosureScope.EXTERNAL_ACTION),
        (FactType.LOCATION_AREA, DisclosureScope.EXTERNAL_ACTION),
        (FactType.CONTRADICTION, DisclosureScope.EXTERNAL_ACTION),
        (FactType.EVIDENCE_DESCRIPTION, DisclosureScope.EXTERNAL_ACTION),
        (FactType.IDENTITY_ATTRIBUTE, DisclosureScope.NAMED_CASE),
        (FactType.UNIT_LOCATION, DisclosureScope.INTERNAL_ONLY),
        (FactType.HEALTH_DETAIL, DisclosureScope.INTERNAL_ONLY),
        (FactType.MANAGEMENT_STATEMENT, DisclosureScope.INTERNAL_ONLY),
        (FactType.COMMITMENT_TERM, DisclosureScope.INTERNAL_ONLY),
    ],
)
def test_the_policy_ceiling_is_declared_for_every_fact_type(
    fact_type: FactType, expected: DisclosureScope
) -> None:
    assert policy_maximum_scope(fact_type, SensitivityCategory.GENERAL) is expected


@pytest.mark.parametrize("fact_type", list(FactType))
def test_every_fact_type_has_a_ceiling(fact_type: FactType) -> None:
    """A fact type added without a ceiling would raise here rather than default to open."""

    assert policy_maximum_scope(fact_type, SensitivityCategory.GENERAL) in set(DisclosureScope)


@pytest.mark.parametrize(
    "sensitivity",
    [
        SensitivityCategory.CONTACT,
        SensitivityCategory.UNIT_LOCATION,
        SensitivityCategory.HEALTH,
        SensitivityCategory.MINOR,
        SensitivityCategory.PRIVATE_QUOTE,
        SensitivityCategory.PRIVATE_EVIDENCE_URI,
    ],
)
def test_a_hard_internal_sensitivity_lowers_even_an_exportable_fact_type(
    sensitivity: SensitivityCategory,
) -> None:
    """Sensitivity narrows; it never widens. An incident carrying health detail is internal."""

    assert (
        policy_maximum_scope(FactType.INCIDENT_OCCURRENCE, sensitivity)
        is DisclosureScope.INTERNAL_ONLY
    )


# -- what a proposal offers -------------------------------------------------------------


@pytest.mark.parametrize("fact_type", list(FactType))
def test_a_proposed_scope_never_exceeds_its_own_ceiling(fact_type: FactType) -> None:
    ceiling = policy_maximum_scope(fact_type, SensitivityCategory.GENERAL)
    assert scope_permits(ceiling, proposed_scope(fact_type, SensitivityCategory.GENERAL))


@pytest.mark.parametrize(
    "fact_type",
    [
        FactType.IDENTITY_ATTRIBUTE,
        FactType.UNIT_LOCATION,
        FactType.HEALTH_DETAIL,
        FactType.MANAGEMENT_STATEMENT,
        FactType.COMMITMENT_TERM,
        FactType.EVIDENCE_DESCRIPTION,
    ],
)
def test_nothing_sensitive_or_evidential_is_offered_by_default(fact_type: FactType) -> None:
    """Identity, unit, health, quotes and evidence export are opt-in, never a default."""

    assert proposed_scope(fact_type, SensitivityCategory.GENERAL) is DisclosureScope.INTERNAL_ONLY


def test_a_proposal_offers_no_identity_permission_at_all() -> None:
    assert PROPOSAL_IDENTITY_GRANT.externally_shareable is False
    assert PROPOSAL_IDENTITY_GRANT.max_scope is DisclosureScope.ANONYMOUS_CASE


def test_a_proposal_grants_every_owned_fact_including_the_locked_ones() -> None:
    """A locked fact is shown as locked rather than omitted, so its owner can see it exists."""

    facts = (
        fact(FactType.INCIDENT_OCCURRENCE, fact_id="88888888-8888-4888-8888-888888888881"),
        fact(FactType.HEALTH_DETAIL, fact_id="88888888-8888-4888-8888-888888888882"),
        fact(FactType.UNIT_LOCATION, fact_id="88888888-8888-4888-8888-888888888883"),
    )
    grants = build_proposed_grants(facts)
    assert len(grants) == 3
    by_id = {grant.fact_id: grant for grant in grants}
    assert by_id[facts[0].fact_id].max_scope is DisclosureScope.ANONYMOUS_CASE
    assert by_id[facts[1].fact_id].max_scope is DisclosureScope.INTERNAL_ONLY
    assert by_id[facts[2].fact_id].max_scope is DisclosureScope.INTERNAL_ONLY


def test_an_internal_only_proposal_grants_no_transformation_permission() -> None:
    grants = build_proposed_grants((fact(FactType.HEALTH_DETAIL),))
    assert grants[0].allow_safe_transformation is False


def test_proposed_grants_are_ordered_canonically_regardless_of_input_order() -> None:
    first = fact(FactType.INCIDENT_OCCURRENCE, fact_id="88888888-8888-4888-8888-888888888881")
    second = fact(FactType.LOCATION_AREA, fact_id="88888888-8888-4888-8888-888888888882")
    assert build_proposed_grants((first, second)) == build_proposed_grants((second, first))


# -- which facts are grantable ----------------------------------------------------------


def test_grantable_facts_excludes_every_kind_of_foreign_or_inactive_fact() -> None:
    mine = fact(FactType.INCIDENT_OCCURRENCE, fact_id="88888888-8888-4888-8888-888888888881")
    candidates = (
        mine,
        fact(
            FactType.INCIDENT_OCCURRENCE,
            fact_id="88888888-8888-4888-8888-888888888882",
            contributor_id=NEIGHBOUR,
        ),
        fact(
            FactType.INCIDENT_OCCURRENCE,
            fact_id="88888888-8888-4888-8888-888888888883",
            case_id=OTHER_CASE_ID,
        ),
        fact(
            FactType.INCIDENT_OCCURRENCE,
            fact_id="88888888-8888-4888-8888-888888888884",
            community_id=OTHER_COMMUNITY_ID,
        ),
        fact(
            FactType.INCIDENT_OCCURRENCE,
            fact_id="88888888-8888-4888-8888-888888888885",
            status=FactStatus.WITHDRAWN,
        ),
    )
    assert grantable_facts(
        candidates,
        contributor_id=OWNER,
        case_id=CASE_ID,
        community_id=COMMUNITY_ID,
        namespace=NAMESPACE,
    ) == (mine,)


# -- validating a requested set ---------------------------------------------------------


def validate(
    grants: tuple[FactGrant, ...],
    facts: tuple[Fact, ...],
    *,
    identity: IdentityGrant = PROPOSAL_IDENTITY_GRANT,
    expires_at: datetime | None = None,
    proposed: frozenset[FactId] | None = None,
    now: datetime = NOW,
) -> tuple[MandateDenialCode, ...]:
    return validate_requested_grants(
        fact_grants=grants,
        identity_grant=identity,
        expires_at=expires_at,
        proposed_fact_ids=(
            frozenset(item.fact_id for item in facts) if proposed is None else proposed
        ),
        facts_by_id={item.fact_id: item for item in facts},
        contributor_id=OWNER,
        case_id=CASE_ID,
        community_id=COMMUNITY_ID,
        namespace=NAMESPACE,
        now=now,
    )


def test_a_grant_within_the_ceiling_is_accepted() -> None:
    incident = fact(FactType.INCIDENT_OCCURRENCE)
    grant = FactGrant(
        fact_id=incident.fact_id,
        max_scope=DisclosureScope.EXTERNAL_ACTION,
        allow_safe_transformation=True,
    )
    assert validate((grant,), (incident,)) == ()


@pytest.mark.parametrize(
    "scope",
    [
        DisclosureScope.AGGREGATE_ONLY,
        DisclosureScope.ANONYMOUS_CASE,
        DisclosureScope.NAMED_CASE,
        DisclosureScope.EXTERNAL_ACTION,
    ],
)
def test_a_health_fact_may_never_be_granted_above_internal_only(
    scope: DisclosureScope,
) -> None:
    health = fact(FactType.HEALTH_DETAIL)
    grant = FactGrant(fact_id=health.fact_id, max_scope=scope, allow_safe_transformation=True)
    assert MandateDenialCode.SCOPE_EXCEEDS_POLICY_MAXIMUM in validate((grant,), (health,))


@pytest.mark.parametrize(
    "scope",
    [
        DisclosureScope.AGGREGATE_ONLY,
        DisclosureScope.ANONYMOUS_CASE,
        DisclosureScope.NAMED_CASE,
        DisclosureScope.EXTERNAL_ACTION,
    ],
)
def test_a_unit_location_fact_may_never_be_granted_above_internal_only(
    scope: DisclosureScope,
) -> None:
    unit = fact(FactType.UNIT_LOCATION)
    grant = FactGrant(fact_id=unit.fact_id, max_scope=scope, allow_safe_transformation=True)
    assert MandateDenialCode.SCOPE_EXCEEDS_POLICY_MAXIMUM in validate((grant,), (unit,))


def test_an_identity_fact_may_not_be_granted_at_external_action() -> None:
    identity = fact(FactType.IDENTITY_ATTRIBUTE)
    grant = FactGrant(
        fact_id=identity.fact_id,
        max_scope=DisclosureScope.EXTERNAL_ACTION,
        allow_safe_transformation=True,
    )
    assert MandateDenialCode.SCOPE_EXCEEDS_POLICY_MAXIMUM in validate((grant,), (identity,))


def test_an_identity_fact_may_be_granted_at_named_case() -> None:
    identity = fact(FactType.IDENTITY_ATTRIBUTE)
    grant = FactGrant(
        fact_id=identity.fact_id,
        max_scope=DisclosureScope.NAMED_CASE,
        allow_safe_transformation=True,
    )
    assert validate((grant,), (identity,)) == ()


@pytest.mark.parametrize(
    ("contributor_id", "case_id", "community_id", "status"),
    [
        (NEIGHBOUR, CASE_ID, COMMUNITY_ID, FactStatus.ACTIVE),
        (OWNER, OTHER_CASE_ID, COMMUNITY_ID, FactStatus.ACTIVE),
        (OWNER, CASE_ID, OTHER_COMMUNITY_ID, FactStatus.ACTIVE),
        (OWNER, CASE_ID, COMMUNITY_ID, FactStatus.WITHDRAWN),
    ],
)
def test_foreign_and_withdrawn_facts_all_answer_unknown_fact(
    contributor_id: ContributorId,
    case_id: CaseId,
    community_id: CommunityId,
    status: FactStatus,
) -> None:
    """One code for four situations, so a caller cannot tell which one they hit."""

    foreign = fact(
        FactType.INCIDENT_OCCURRENCE,
        contributor_id=contributor_id,
        case_id=case_id,
        community_id=community_id,
        status=status,
    )
    grant = FactGrant(
        fact_id=foreign.fact_id,
        max_scope=DisclosureScope.ANONYMOUS_CASE,
        allow_safe_transformation=True,
    )
    assert validate((grant,), (foreign,)) == (MandateDenialCode.UNKNOWN_FACT,)


def test_a_nonexistent_fact_answers_unknown_fact_identically() -> None:
    grant = FactGrant(
        fact_id=FactId(UUID("99999999-9999-4999-8999-999999999999")),
        max_scope=DisclosureScope.ANONYMOUS_CASE,
        allow_safe_transformation=True,
    )
    assert validate((grant,), ()) == (MandateDenialCode.UNKNOWN_FACT,)


def test_a_duplicate_fact_grant_is_refused() -> None:
    incident = fact(FactType.INCIDENT_OCCURRENCE)
    grant = FactGrant(
        fact_id=incident.fact_id,
        max_scope=DisclosureScope.ANONYMOUS_CASE,
        allow_safe_transformation=True,
    )
    assert MandateDenialCode.DUPLICATE_FACT_GRANT in validate((grant, grant), (incident,))


def test_granting_a_real_owned_fact_the_proposal_never_named_is_refused() -> None:
    incident = fact(FactType.INCIDENT_OCCURRENCE)
    grant = FactGrant(
        fact_id=incident.fact_id,
        max_scope=DisclosureScope.ANONYMOUS_CASE,
        allow_safe_transformation=True,
    )
    assert validate((grant,), (incident,), proposed=frozenset()) == (
        MandateDenialCode.GRANT_NOT_PROPOSED,
    )


def test_every_distinct_reason_is_reported_at_once() -> None:
    health = fact(FactType.HEALTH_DETAIL, fact_id="88888888-8888-4888-8888-888888888881")
    missing = FactGrant(
        fact_id=FactId(UUID("99999999-9999-4999-8999-999999999999")),
        max_scope=DisclosureScope.ANONYMOUS_CASE,
        allow_safe_transformation=True,
    )
    overbroad = FactGrant(
        fact_id=health.fact_id,
        max_scope=DisclosureScope.EXTERNAL_ACTION,
        allow_safe_transformation=True,
    )
    codes = validate((missing, overbroad), (health,))
    assert set(codes) == {
        MandateDenialCode.UNKNOWN_FACT,
        MandateDenialCode.SCOPE_EXCEEDS_POLICY_MAXIMUM,
    }
    assert list(codes) == sorted(codes, key=str)


# -- identity is capped on its own ------------------------------------------------------


def test_identity_permission_is_capped_at_named_case() -> None:
    assert identity_maximum_scope() is DisclosureScope.NAMED_CASE
    over = IdentityGrant(externally_shareable=True, max_scope=DisclosureScope.EXTERNAL_ACTION)
    assert validate((), (), identity=over) == (MandateDenialCode.IDENTITY_EXCEEDS_POLICY_MAXIMUM,)


def test_a_content_grant_at_the_ceiling_confers_no_identity_permission() -> None:
    """The separation, stated directly: granting content leaves identity exactly as it was."""

    incident = fact(FactType.INCIDENT_OCCURRENCE)
    grant = FactGrant(
        fact_id=incident.fact_id,
        max_scope=DisclosureScope.EXTERNAL_ACTION,
        allow_safe_transformation=True,
    )
    assert validate((grant,), (incident,), identity=PROPOSAL_IDENTITY_GRANT) == ()
    assert PROPOSAL_IDENTITY_GRANT.externally_shareable is False


def test_identity_permission_confers_no_content_permission() -> None:
    """The converse: a shared identity does not raise a health fact's ceiling."""

    health = fact(FactType.HEALTH_DETAIL)
    grant = FactGrant(
        fact_id=health.fact_id,
        max_scope=DisclosureScope.NAMED_CASE,
        allow_safe_transformation=True,
    )
    identity = IdentityGrant(externally_shareable=True, max_scope=DisclosureScope.NAMED_CASE)
    assert MandateDenialCode.SCOPE_EXCEEDS_POLICY_MAXIMUM in validate(
        (grant,), (health,), identity=identity
    )


# -- expiry, destination, purpose -------------------------------------------------------


def test_an_expiry_at_the_decision_instant_is_refused() -> None:
    assert validate((), (), expires_at=NOW) == (MandateDenialCode.EXPIRY_ALREADY_PASSED,)


def test_an_expiry_one_microsecond_in_the_future_is_accepted() -> None:
    assert validate((), (), expires_at=NOW + timedelta(microseconds=1)) == ()


def test_an_unknown_destination_is_refused() -> None:
    assert validate_destinations_and_purposes(
        destination_ids=(OTHER_DESTINATION,),
        purposes=(Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,),
        allowed_destination_id=DESTINATION,
    ) == (MandateDenialCode.DESTINATION_NOT_ALLOWED,)


def test_an_empty_destination_or_purpose_set_is_refused() -> None:
    codes = validate_destinations_and_purposes(
        destination_ids=(), purposes=(), allowed_destination_id=DESTINATION
    )
    assert set(codes) == {
        MandateDenialCode.DESTINATION_NOT_ALLOWED,
        MandateDenialCode.PURPOSE_NOT_ALLOWED,
    }


def test_the_only_policy_purpose_is_accepted() -> None:
    assert (
        validate_destinations_and_purposes(
            destination_ids=(DESTINATION,),
            purposes=(Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,),
            allowed_destination_id=DESTINATION,
        )
        == ()
    )
