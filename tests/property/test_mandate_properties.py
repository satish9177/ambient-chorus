"""Hypothesis properties for the two things a mandate must never get wrong.

Table-driven tests cover the cases somebody thought of. These cover the ones nobody did, over
every combination of fact type, sensitivity, scope, and identity permission the domain admits:

* **no escalation.** Nothing a contributor can send produces a grant above the policy ceiling
  for that exact fact, and nothing produces identity permission above the identity ceiling.
* **determinism.** The canonical terms hash depends on the authorization terms and on nothing
  else -- not on the order grants arrive in, not on which attempt computed it.

The generators deliberately produce *invalid* authorizations as often as valid ones. A strategy
that only built well-formed grants would be testing that the validator accepts what the fixture
already knows is fine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from chorus.application.services.mandate_terms import PLACEHOLDER_TERMS_HASH, seal
from chorus.domain.entities import (
    DisclosureScope,
    EvidenceStatus,
    FactType,
    MandateStatus,
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
    MandateId,
    MessageId,
    Namespace,
    ReportId,
)
from chorus.domain.mandates import DisclosureMandate, FactGrant, IdentityGrant
from chorus.privacy.canonical import canonical_bytes, hash_mandate_terms, mandate_terms_payload
from chorus.privacy.mandates import validate_requested_grants
from chorus.privacy.policy import (
    MandateDenialCode,
    identity_maximum_scope,
    policy_maximum_scope,
    proposed_scope,
    scope_permits,
)

NAMESPACE = Namespace("TEST_PROPERTY")
NOW = datetime(2030, 1, 20, 12, 0, 0, tzinfo=UTC)
CASE_ID = CaseId(UUID("11111111-1111-4111-8111-111111111111"))
COMMUNITY_ID = CommunityId(UUID("22222222-2222-4222-8222-222222222222"))
OWNER = ContributorId(UUID("33333333-3333-4333-8333-333333333333"))
REPORT_ID = ReportId(UUID("55555555-5555-4555-8555-555555555555"))
MESSAGE_ID = MessageId(UUID("66666666-6666-4666-8666-666666666666"))
MANDATE_ID = MandateId(UUID("77777777-7777-4777-8777-777777777777"))
DESTINATION = DestinationId("property_manager:demo")

VALUES: dict[FactType, FactValue] = {
    FactType.INCIDENT_OCCURRENCE: IncidentOccurrence(
        occurred_at=NOW - timedelta(days=1), failure_mode=FailureMode.STUCK
    ),
    FactType.SERVICE_IMPACT: ServiceImpact(impact_code=ImpactCode.DELAY, summary="delayed"),
    FactType.LOCATION_AREA: LocationArea(area=LocationAreaCode.LOBBY),
    FactType.IDENTITY_ATTRIBUTE: IdentityAttribute(display_name="Someone"),
    FactType.UNIT_LOCATION: UnitLocation(unit_label="4B"),
    FactType.HEALTH_DETAIL: HealthDetail(subject_relation=SubjectRelation.SELF, detail="detail"),
    FactType.MANAGEMENT_STATEMENT: ManagementStatement(
        statement="a statement", speaker_org="Management", stated_at=NOW
    ),
    FactType.EVIDENCE_DESCRIPTION: EvidenceDescription(
        description="a description", media_kind=EvidenceMediaKind.IMAGE
    ),
}
"""One valid value per fact type the union can build without extra citations.

``CONTRADICTION`` and ``COMMITMENT_TERM`` are absent because both require cited identifiers or
a due date the generator would have to invent; their ceilings are covered by the table tests.
"""

GRANTABLE_TYPES = tuple(VALUES)

REQUIRED_SENSITIVITY: dict[FactType, SensitivityCategory] = {
    FactType.IDENTITY_ATTRIBUTE: SensitivityCategory.IDENTITY,
    FactType.UNIT_LOCATION: SensitivityCategory.UNIT_LOCATION,
    FactType.HEALTH_DETAIL: SensitivityCategory.HEALTH,
}

FREE_SENSITIVITIES = tuple(
    category
    for category in SensitivityCategory
    if category not in set(REQUIRED_SENSITIVITY.values())
)


def build_fact(fact_type: FactType, sensitivity: SensitivityCategory, index: int) -> Fact:
    return Fact(
        fact_id=FactId(UUID(int=index, version=4)),
        case_id=CASE_ID,
        report_id=REPORT_ID,
        community_id=COMMUNITY_ID,
        contributor_id=OWNER,
        namespace=NAMESPACE,
        fact_type=fact_type,
        value=VALUES[fact_type],
        sensitivity=REQUIRED_SENSITIVITY.get(fact_type, sensitivity),
        evidence_ids=(),
        evidence_status=EvidenceStatus.REPORTED,
        source_message_ids=(MESSAGE_ID,),
        supersedes_fact_id=None,
        status=FactStatus.ACTIVE,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


facts_and_grants = st.lists(
    st.tuples(
        st.sampled_from(GRANTABLE_TYPES),
        st.sampled_from(FREE_SENSITIVITIES),
        st.sampled_from(tuple(DisclosureScope)),
        st.booleans(),
    ),
    min_size=1,
    max_size=6,
    unique_by=lambda item: item[0],
)

identity_grants = st.builds(
    lambda shareable, scope: IdentityGrant(
        externally_shareable=shareable,
        max_scope=scope if shareable else DisclosureScope.ANONYMOUS_CASE,
    ),
    st.booleans(),
    st.sampled_from(
        (
            DisclosureScope.ANONYMOUS_CASE,
            DisclosureScope.NAMED_CASE,
            DisclosureScope.EXTERNAL_ACTION,
        )
    ),
)


# -- no escalation ------------------------------------------------------------------------


@given(rows=facts_and_grants, identity=identity_grants)
def test_no_requested_grant_above_its_ceiling_is_ever_accepted(
    rows: list[tuple[FactType, SensitivityCategory, DisclosureScope, bool]],
    identity: IdentityGrant,
) -> None:
    """The whole point of the ceiling, stated as a property over every combination."""

    facts = tuple(
        build_fact(fact_type, sensitivity, index + 1)
        for index, (fact_type, sensitivity, _, _) in enumerate(rows)
    )
    grants = tuple(
        FactGrant(fact_id=fact.fact_id, max_scope=scope, allow_safe_transformation=transform)
        for fact, (_, _, scope, transform) in zip(facts, rows, strict=True)
    )
    denials = validate_requested_grants(
        fact_grants=grants,
        identity_grant=identity,
        expires_at=None,
        proposed_fact_ids=frozenset(fact.fact_id for fact in facts),
        facts_by_id={fact.fact_id: fact for fact in facts},
        contributor_id=OWNER,
        case_id=CASE_ID,
        community_id=COMMUNITY_ID,
        namespace=NAMESPACE,
        now=NOW,
    )

    escalates = any(
        not scope_permits(policy_maximum_scope(fact.fact_type, fact.sensitivity), grant.max_scope)
        for fact, grant in zip(facts, grants, strict=True)
    )
    assert escalates == (MandateDenialCode.SCOPE_EXCEEDS_POLICY_MAXIMUM in denials)


@given(identity=identity_grants)
def test_identity_permission_never_exceeds_its_own_ceiling(identity: IdentityGrant) -> None:
    denials = validate_requested_grants(
        fact_grants=(),
        identity_grant=identity,
        expires_at=None,
        proposed_fact_ids=frozenset(),
        facts_by_id={},
        contributor_id=OWNER,
        case_id=CASE_ID,
        community_id=COMMUNITY_ID,
        namespace=NAMESPACE,
        now=NOW,
    )

    over = not scope_permits(identity_maximum_scope(), identity.max_scope)
    assert over == (MandateDenialCode.IDENTITY_EXCEEDS_POLICY_MAXIMUM in denials)


@given(rows=facts_and_grants)
def test_content_permission_never_produces_identity_permission(
    rows: list[tuple[FactType, SensitivityCategory, DisclosureScope, bool]],
) -> None:
    """However much content is granted, an identity grant that gives nothing still gives nothing.

    The two are independent truth dimensions in the frozen model, and this is the direction that
    matters: a maximal content grant is exactly the input under which somebody might be tempted
    to infer that the owner "obviously" consented to being named.
    """

    facts = tuple(
        build_fact(fact_type, sensitivity, index + 1)
        for index, (fact_type, sensitivity, _, _) in enumerate(rows)
    )
    grants = tuple(
        FactGrant(
            fact_id=fact.fact_id,
            max_scope=policy_maximum_scope(fact.fact_type, fact.sensitivity),
            allow_safe_transformation=True,
        )
        for fact in facts
    )
    silent = IdentityGrant(externally_shareable=False, max_scope=DisclosureScope.ANONYMOUS_CASE)
    mandate = seal(_mandate(grants, silent))

    assert mandate.identity_grant.externally_shareable is False
    assert mandate.identity_grant.max_scope is DisclosureScope.ANONYMOUS_CASE


@given(
    fact_type=st.sampled_from(GRANTABLE_TYPES),
    sensitivity=st.sampled_from(FREE_SENSITIVITIES),
)
def test_a_proposed_scope_is_always_reachable_and_never_above_the_ceiling(
    fact_type: FactType, sensitivity: SensitivityCategory
) -> None:
    ceiling = policy_maximum_scope(fact_type, sensitivity)
    offered = proposed_scope(fact_type, sensitivity)
    assert scope_permits(ceiling, offered)


# -- canonical determinism ------------------------------------------------------------------


@given(rows=facts_and_grants, identity=identity_grants)
def test_the_terms_hash_is_insensitive_to_grant_order(
    rows: list[tuple[FactType, SensitivityCategory, DisclosureScope, bool]],
    identity: IdentityGrant,
) -> None:
    facts = tuple(
        build_fact(fact_type, sensitivity, index + 1)
        for index, (fact_type, sensitivity, _, _) in enumerate(rows)
    )
    grants = tuple(
        FactGrant(fact_id=fact.fact_id, max_scope=scope, allow_safe_transformation=transform)
        for fact, (_, _, scope, transform) in zip(facts, rows, strict=True)
    )

    forward = _mandate(grants, identity)
    reversed_order = _mandate(tuple(reversed(grants)), identity)

    assert hash_mandate_terms(forward) == hash_mandate_terms(reversed_order)
    assert canonical_bytes(mandate_terms_payload(forward)) == canonical_bytes(
        mandate_terms_payload(reversed_order)
    )


@given(rows=facts_and_grants, identity=identity_grants)
def test_sealing_is_stable_and_verifiable(
    rows: list[tuple[FactType, SensitivityCategory, DisclosureScope, bool]],
    identity: IdentityGrant,
) -> None:
    facts = tuple(
        build_fact(fact_type, sensitivity, index + 1)
        for index, (fact_type, sensitivity, _, _) in enumerate(rows)
    )
    grants = tuple(
        FactGrant(fact_id=fact.fact_id, max_scope=scope, allow_safe_transformation=transform)
        for fact, (_, _, scope, transform) in zip(facts, rows, strict=True)
    )

    once = seal(_mandate(grants, identity))
    twice = seal(once)

    assert once.terms_hash == twice.terms_hash
    assert once.terms_hash == hash_mandate_terms(once)


@given(rows=facts_and_grants, identity=identity_grants)
def test_any_change_to_the_terms_changes_the_hash(
    rows: list[tuple[FactType, SensitivityCategory, DisclosureScope, bool]],
    identity: IdentityGrant,
) -> None:
    """Narrowing one grant to nothing is a different authorization, and hashes differently."""

    facts = tuple(
        build_fact(fact_type, sensitivity, index + 1)
        for index, (fact_type, sensitivity, _, _) in enumerate(rows)
    )
    grants = tuple(
        FactGrant(fact_id=fact.fact_id, max_scope=scope, allow_safe_transformation=transform)
        for fact, (_, _, scope, transform) in zip(facts, rows, strict=True)
    )
    narrowed = (
        FactGrant(
            fact_id=grants[0].fact_id,
            max_scope=DisclosureScope.INTERNAL_ONLY,
            allow_safe_transformation=False,
        ),
        *grants[1:],
    )

    if grants[0] == narrowed[0]:
        return  # already at the narrowest terms; nothing changed, so nothing should move
    assert hash_mandate_terms(_mandate(grants, identity)) != hash_mandate_terms(
        _mandate(narrowed, identity)
    )


# -- cross-scope references always fail ---------------------------------------------------


@given(rows=facts_and_grants)
def test_a_fact_outside_the_caller_scope_always_answers_unknown(
    rows: list[tuple[FactType, SensitivityCategory, DisclosureScope, bool]],
) -> None:
    """Whatever the scope requested, a fact in another case is never grantable."""

    other_case = CaseId(UUID("1a111111-1111-4111-8111-111111111111"))
    facts = tuple(
        build_fact(fact_type, sensitivity, index + 1)
        for index, (fact_type, sensitivity, _, _) in enumerate(rows)
    )
    grants = tuple(
        FactGrant(fact_id=fact.fact_id, max_scope=scope, allow_safe_transformation=transform)
        for fact, (_, _, scope, transform) in zip(facts, rows, strict=True)
    )

    denials = validate_requested_grants(
        fact_grants=grants,
        identity_grant=IdentityGrant(
            externally_shareable=False, max_scope=DisclosureScope.ANONYMOUS_CASE
        ),
        expires_at=None,
        proposed_fact_ids=frozenset(fact.fact_id for fact in facts),
        facts_by_id={fact.fact_id: fact for fact in facts},
        contributor_id=OWNER,
        case_id=other_case,
        community_id=COMMUNITY_ID,
        namespace=NAMESPACE,
        now=NOW,
    )

    assert MandateDenialCode.UNKNOWN_FACT in denials
    assert MandateDenialCode.SCOPE_EXCEEDS_POLICY_MAXIMUM not in denials


def _mandate(grants: tuple[FactGrant, ...], identity: IdentityGrant) -> DisclosureMandate:
    return DisclosureMandate(
        mandate_id=MANDATE_ID,
        version=2,
        case_id=CASE_ID,
        community_id=COMMUNITY_ID,
        contributor_id=OWNER,
        namespace=NAMESPACE,
        status=MandateStatus.APPROVED,
        fact_grants=grants,
        identity_grant=identity,
        allowed_destination_ids=(DESTINATION,),
        allowed_purposes=(Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,),
        valid_from=NOW - timedelta(days=1),
        expires_at=None,
        proposed_at=NOW - timedelta(days=1),
        decided_at=NOW,
        revoked_at=None,
        decision_actor_id=OWNER,
        supersedes_version=1,
        terms_hash=PLACEHOLDER_TERMS_HASH,
        created_at=NOW,
        updated_at=NOW,
    )
