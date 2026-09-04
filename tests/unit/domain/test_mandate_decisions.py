"""The structural half of a mandate decision: edges, ownership, carriage, and time.

Policy lives elsewhere and is tested elsewhere. What is proved here is that the shape of a
decision cannot be abused: no edge exists that the frozen contract does not state, no decision
is taken by somebody other than the owner, no refusal smuggles a grant, and the expiry boundary
is exact at the microsecond rather than approximately right.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from chorus.domain.entities import DisclosureScope, MandateStatus, Purpose
from chorus.domain.errors import StateTransitionError, ValidationError
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    DestinationId,
    FactId,
    MandateId,
    Namespace,
    Sha256Digest,
)
from chorus.domain.mandates import (
    MANDATE_DECISION_EDGES,
    NO_IDENTITY_GRANT,
    DisclosureMandate,
    FactGrant,
    IdentityGrant,
    MandateDecision,
    decide_mandate,
    decision_is_within_validity,
    derived_status,
    is_superseded,
    mandate_is_expired,
    next_mandate_status,
    terms_are_identical,
    withdraws_authorization,
)

NAMESPACE = Namespace("TEST_MANDATE")
NOW = datetime(2030, 1, 20, 12, 0, 0, tzinfo=UTC)
VALID_FROM = NOW - timedelta(days=1)
PLACEHOLDER = Sha256Digest("sha256:" + "0" * 64)

CASE_ID = CaseId(UUID("11111111-1111-4111-8111-111111111111"))
COMMUNITY_ID = CommunityId(UUID("22222222-2222-4222-8222-222222222222"))
OWNER = ContributorId(UUID("33333333-3333-4333-8333-333333333333"))
STRANGER = ContributorId(UUID("44444444-4444-4444-8444-444444444444"))
MANDATE_ID = MandateId(UUID("55555555-5555-4555-8555-555555555555"))
FACT_ONE = FactId(UUID("66666666-6666-4666-8666-666666666666"))
FACT_TWO = FactId(UUID("77777777-7777-4777-8777-777777777777"))
DESTINATION = DestinationId("property_manager:demo")

GRANT_ONE = FactGrant(
    fact_id=FACT_ONE,
    max_scope=DisclosureScope.ANONYMOUS_CASE,
    allow_safe_transformation=True,
)
GRANT_TWO = FactGrant(
    fact_id=FACT_TWO,
    max_scope=DisclosureScope.INTERNAL_ONLY,
    allow_safe_transformation=False,
)


def mandate(
    *,
    version: int = 1,
    status: MandateStatus = MandateStatus.PROPOSED,
    grants: tuple[FactGrant, ...] = (GRANT_ONE, GRANT_TWO),
    identity: IdentityGrant = NO_IDENTITY_GRANT,
    expires_at: datetime | None = None,
    valid_from: datetime = VALID_FROM,
) -> DisclosureMandate:
    decided = None if status is MandateStatus.PROPOSED else valid_from + timedelta(minutes=1)
    return DisclosureMandate(
        mandate_id=MANDATE_ID,
        version=version,
        case_id=CASE_ID,
        community_id=COMMUNITY_ID,
        contributor_id=OWNER,
        namespace=NAMESPACE,
        status=status,
        fact_grants=grants,
        identity_grant=identity,
        allowed_destination_ids=(DESTINATION,),
        allowed_purposes=(Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,),
        valid_from=valid_from,
        expires_at=expires_at,
        proposed_at=valid_from,
        decided_at=decided,
        revoked_at=valid_from + timedelta(minutes=2) if status is MandateStatus.REVOKED else None,
        decision_actor_id=None if status is MandateStatus.PROPOSED else OWNER,
        supersedes_version=None if version == 1 else version - 1,
        terms_hash=PLACEHOLDER,
        created_at=valid_from,
        updated_at=valid_from,
    )


def decide(
    current: DisclosureMandate,
    decision: MandateDecision,
    *,
    grants: tuple[FactGrant, ...] | None = None,
    identity: IdentityGrant | None = None,
    expires_at: datetime | None = None,
    actor: ContributorId = OWNER,
    now: datetime = NOW,
) -> DisclosureMandate:
    return decide_mandate(
        current,
        decision=decision,
        fact_grants=current.fact_grants if grants is None else grants,
        identity_grant=current.identity_grant if identity is None else identity,
        expires_at=current.expires_at if expires_at is None else expires_at,
        actor_id=actor,
        now=now,
        terms_hash=PLACEHOLDER,
    )


# -- the edge table ---------------------------------------------------------------------

ALL_STATUSES = tuple(MandateStatus)
ALL_DECISIONS = tuple(MandateDecision)


@pytest.mark.parametrize("status", ALL_STATUSES)
@pytest.mark.parametrize("decision", ALL_DECISIONS)
def test_next_mandate_status_admits_only_the_five_tabulated_edges(
    status: MandateStatus, decision: MandateDecision
) -> None:
    """The complete 24-cell matrix, so a new enum member cannot quietly become legal."""

    expected = MANDATE_DECISION_EDGES.get((status, decision))
    if expected is None:
        with pytest.raises(StateTransitionError):
            next_mandate_status(status, decision)
    else:
        assert next_mandate_status(status, decision) is expected


def test_the_edge_table_contains_exactly_the_documented_edges() -> None:
    assert set(MANDATE_DECISION_EDGES) == {
        (MandateStatus.PROPOSED, MandateDecision.APPROVE),
        (MandateStatus.PROPOSED, MandateDecision.ADJUST),
        (MandateStatus.PROPOSED, MandateDecision.REFUSE),
        (MandateStatus.APPROVED, MandateDecision.ADJUST),
        (MandateStatus.APPROVED, MandateDecision.REVOKE),
    }


def test_revoking_a_proposal_is_refused_because_nothing_was_ever_granted() -> None:
    with pytest.raises(StateTransitionError):
        decide(mandate(), MandateDecision.REVOKE, grants=(), identity=NO_IDENTITY_GRANT)


def test_approving_an_already_approved_mandate_is_refused() -> None:
    approved = mandate(version=2, status=MandateStatus.APPROVED)
    with pytest.raises(StateTransitionError):
        decide(approved, MandateDecision.APPROVE)


@pytest.mark.parametrize("terminal", [MandateStatus.REFUSED, MandateStatus.REVOKED])
@pytest.mark.parametrize("decision", ALL_DECISIONS)
def test_a_terminal_version_accepts_no_further_decision(
    terminal: MandateStatus, decision: MandateDecision
) -> None:
    current = mandate(version=2, status=terminal, grants=())
    with pytest.raises(StateTransitionError):
        decide(current, decision, grants=(), identity=NO_IDENTITY_GRANT)


# -- ownership --------------------------------------------------------------------------


@pytest.mark.parametrize("decision", ALL_DECISIONS)
def test_a_stranger_can_take_no_decision_on_someone_elses_mandate(
    decision: MandateDecision,
) -> None:
    """Ownership is checked before the edge table, so even a legal edge is refused."""

    with pytest.raises(StateTransitionError):
        decide(mandate(), decision, actor=STRANGER)


def test_the_decision_actor_is_recorded_as_the_owner() -> None:
    approved = decide(mandate(), MandateDecision.APPROVE)
    assert approved.decision_actor_id == OWNER
    assert approved.contributor_id == OWNER


# -- what each decision may carry -------------------------------------------------------


def test_approve_must_reproduce_the_proposed_terms_exactly() -> None:
    approved = decide(mandate(), MandateDecision.APPROVE)
    assert approved.status is MandateStatus.APPROVED
    assert approved.version == 2
    assert approved.supersedes_version == 1
    assert set(approved.fact_grants) == {GRANT_ONE, GRANT_TWO}


def test_approve_is_insensitive_to_the_order_grants_arrive_in() -> None:
    approved = decide(mandate(), MandateDecision.APPROVE, grants=(GRANT_TWO, GRANT_ONE))
    assert approved.fact_grants == tuple(
        sorted((GRANT_ONE, GRANT_TWO), key=lambda grant: str(grant.fact_id))
    )


def test_approve_with_one_altered_scope_is_refused() -> None:
    altered = replace(GRANT_ONE, max_scope=DisclosureScope.EXTERNAL_ACTION)
    with pytest.raises(StateTransitionError):
        decide(mandate(), MandateDecision.APPROVE, grants=(altered, GRANT_TWO))


def test_approve_with_one_altered_transformation_flag_is_refused() -> None:
    altered = replace(GRANT_TWO, allow_safe_transformation=True)
    with pytest.raises(StateTransitionError):
        decide(mandate(), MandateDecision.APPROVE, grants=(GRANT_ONE, altered))


def test_approve_that_drops_a_grant_is_refused() -> None:
    with pytest.raises(StateTransitionError):
        decide(mandate(), MandateDecision.APPROVE, grants=(GRANT_ONE,))


def test_approve_that_changes_the_identity_grant_is_refused() -> None:
    with pytest.raises(StateTransitionError):
        decide(
            mandate(),
            MandateDecision.APPROVE,
            identity=IdentityGrant(externally_shareable=True, max_scope=DisclosureScope.NAMED_CASE),
        )


def test_approve_that_changes_the_expiry_is_refused() -> None:
    with pytest.raises(StateTransitionError):
        decide(mandate(), MandateDecision.APPROVE, expires_at=NOW + timedelta(days=1))


def test_adjust_replaces_the_complete_grant_set_rather_than_patching_it() -> None:
    """A one-fact ADJUST means "only this fact", not "change this and keep the rest"."""

    adjusted = decide(mandate(), MandateDecision.ADJUST, grants=(GRANT_ONE,))
    assert adjusted.fact_grants == (GRANT_ONE,)
    assert adjusted.status is MandateStatus.APPROVED
    assert adjusted.version == 2


def test_adjust_may_narrow_a_scope() -> None:
    narrowed = replace(GRANT_ONE, max_scope=DisclosureScope.INTERNAL_ONLY)
    adjusted = decide(mandate(), MandateDecision.ADJUST, grants=(narrowed, GRANT_TWO))
    assert narrowed in adjusted.fact_grants


@pytest.mark.parametrize("decision", [MandateDecision.REFUSE, MandateDecision.REVOKE])
def test_a_terminal_decision_carrying_a_fact_grant_is_refused(
    decision: MandateDecision,
) -> None:
    current = (
        mandate() if decision is MandateDecision.REFUSE else mandate(status=MandateStatus.APPROVED)
    )
    with pytest.raises(StateTransitionError):
        decide(current, decision, grants=(GRANT_ONE,), identity=NO_IDENTITY_GRANT)


@pytest.mark.parametrize("decision", [MandateDecision.REFUSE, MandateDecision.REVOKE])
def test_a_terminal_decision_carrying_identity_permission_is_refused(
    decision: MandateDecision,
) -> None:
    current = (
        mandate() if decision is MandateDecision.REFUSE else mandate(status=MandateStatus.APPROVED)
    )
    with pytest.raises(StateTransitionError):
        decide(
            current,
            decision,
            grants=(),
            identity=IdentityGrant(externally_shareable=True, max_scope=DisclosureScope.NAMED_CASE),
        )


def test_refuse_produces_a_version_that_grants_nothing() -> None:
    refused = decide(mandate(), MandateDecision.REFUSE, grants=(), identity=NO_IDENTITY_GRANT)
    assert refused.status is MandateStatus.REFUSED
    assert refused.fact_grants == ()
    assert refused.identity_grant.externally_shareable is False
    assert refused.revoked_at is None


def test_revoke_produces_a_version_that_grants_nothing_and_records_the_instant() -> None:
    approved = mandate(status=MandateStatus.APPROVED)
    revoked = decide(approved, MandateDecision.REVOKE, grants=(), identity=NO_IDENTITY_GRANT)
    assert revoked.status is MandateStatus.REVOKED
    assert revoked.fact_grants == ()
    assert revoked.revoked_at == NOW


# -- immutability -----------------------------------------------------------------------


def test_a_decision_leaves_the_version_it_supersedes_untouched() -> None:
    current = mandate()
    before = replace(current)
    decide(current, MandateDecision.APPROVE)
    assert current == before


def test_carried_forward_terms_are_not_decided_by_the_contributor() -> None:
    """Destination, purpose, and validity start belong to the proposal, not to the answer."""

    approved = decide(mandate(), MandateDecision.APPROVE)
    assert approved.allowed_destination_ids == (DESTINATION,)
    assert approved.allowed_purposes == (Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,)
    assert approved.valid_from == VALID_FROM
    assert approved.proposed_at == VALID_FROM


# -- time -------------------------------------------------------------------------------


def test_a_decision_before_valid_from_is_refused() -> None:
    future = mandate(valid_from=NOW + timedelta(hours=1))
    assert decision_is_within_validity(future, NOW) is False
    with pytest.raises(StateTransitionError):
        decide(future, MandateDecision.APPROVE)


def test_a_decision_exactly_at_valid_from_is_permitted() -> None:
    current = mandate(valid_from=NOW)
    assert decision_is_within_validity(current, NOW) is True
    assert decide(current, MandateDecision.APPROVE, now=NOW).decided_at == NOW


def test_a_decision_one_microsecond_before_expiry_is_permitted() -> None:
    expiry = NOW + timedelta(microseconds=1)
    current = mandate(expires_at=expiry)
    assert mandate_is_expired(current, NOW) is False
    assert decide(current, MandateDecision.APPROVE, now=NOW).status is MandateStatus.APPROVED


def test_equality_at_expiry_is_expired() -> None:
    current = mandate(expires_at=NOW)
    assert mandate_is_expired(current, NOW) is True
    with pytest.raises(StateTransitionError):
        decide(current, MandateDecision.APPROVE, now=NOW)


def test_a_decision_after_expiry_is_refused() -> None:
    current = mandate(expires_at=NOW - timedelta(microseconds=1))
    assert mandate_is_expired(current, NOW) is True
    with pytest.raises(StateTransitionError):
        decide(current, MandateDecision.APPROVE, now=NOW)


def test_a_mandate_with_no_expiry_never_expires() -> None:
    assert mandate_is_expired(mandate(expires_at=None), NOW + timedelta(days=3650)) is False


def test_an_expiry_at_or_before_validity_is_a_validation_error() -> None:
    """The closed domain invariant, surfaced as a request problem rather than a crash."""

    with pytest.raises(ValidationError):
        decide(mandate(), MandateDecision.ADJUST, grants=(GRANT_ONE,), expires_at=VALID_FROM)


def test_a_duplicate_fact_grant_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        decide(mandate(), MandateDecision.ADJUST, grants=(GRANT_ONE, GRANT_ONE))


# -- derived statuses -------------------------------------------------------------------


def test_a_non_current_proposed_or_approved_version_reads_as_superseded() -> None:
    assert is_superseded(mandate(version=1), current_version=2) is True
    approved = mandate(version=2, status=MandateStatus.APPROVED)
    assert is_superseded(approved, current_version=3) is True
    assert derived_status(approved, current_version=3, now=NOW) is MandateStatus.SUPERSEDED


def test_a_terminal_version_is_never_reported_as_superseded() -> None:
    """A refusal that a later version replaced is still a refusal, and reads as one."""

    refused = mandate(version=1, status=MandateStatus.REFUSED, grants=())
    assert is_superseded(refused, current_version=5) is False
    assert derived_status(refused, current_version=5, now=NOW) is MandateStatus.REFUSED


def test_an_expired_current_approval_reads_as_expired() -> None:
    approved = mandate(version=2, status=MandateStatus.APPROVED, expires_at=NOW)
    assert derived_status(approved, current_version=2, now=NOW) is MandateStatus.EXPIRED


def test_a_live_current_approval_reads_as_approved() -> None:
    approved = mandate(
        version=2, status=MandateStatus.APPROVED, expires_at=NOW + timedelta(seconds=1)
    )
    assert derived_status(approved, current_version=2, now=NOW) is MandateStatus.APPROVED


# -- terms comparison -------------------------------------------------------------------


def test_terms_are_identical_ignores_grant_order_only() -> None:
    current = mandate()
    assert terms_are_identical(
        current,
        fact_grants=(GRANT_TWO, GRANT_ONE),
        identity_grant=current.identity_grant,
        expires_at=current.expires_at,
    )
    assert not terms_are_identical(
        current,
        fact_grants=(GRANT_ONE,),
        identity_grant=current.identity_grant,
        expires_at=current.expires_at,
    )


# -- withdrawal, which is what readiness reconciliation reads ------------------------------


@pytest.mark.parametrize("decision", [MandateDecision.REFUSE, MandateDecision.REVOKE])
def test_a_terminal_decision_always_withdraws_authorization(
    decision: MandateDecision,
) -> None:
    assert withdraws_authorization(decision=decision, previous=(GRANT_ONE, GRANT_TWO), requested=())


def test_approve_never_withdraws_authorization() -> None:
    assert not withdraws_authorization(
        decision=MandateDecision.APPROVE,
        previous=(GRANT_ONE, GRANT_TWO),
        requested=(GRANT_ONE, GRANT_TWO),
    )


def test_an_adjustment_that_drops_a_fact_withdraws_authorization() -> None:
    assert withdraws_authorization(
        decision=MandateDecision.ADJUST, previous=(GRANT_ONE, GRANT_TWO), requested=(GRANT_ONE,)
    )


def test_an_adjustment_that_narrows_a_scope_withdraws_authorization() -> None:
    narrowed = replace(GRANT_ONE, max_scope=DisclosureScope.INTERNAL_ONLY)
    assert withdraws_authorization(
        decision=MandateDecision.ADJUST,
        previous=(GRANT_ONE, GRANT_TWO),
        requested=(narrowed, GRANT_TWO),
    )


def test_an_adjustment_that_swaps_one_grant_for_another_still_withdraws() -> None:
    """The count is unchanged and what may be exported is not, which is the point of per-fact."""

    replacement = FactGrant(
        fact_id=FactId(UUID("88888888-8888-4888-8888-888888888888")),
        max_scope=DisclosureScope.ANONYMOUS_CASE,
        allow_safe_transformation=True,
    )
    assert withdraws_authorization(
        decision=MandateDecision.ADJUST,
        previous=(GRANT_ONE, GRANT_TWO),
        requested=(replacement, GRANT_TWO),
    )


def test_an_adjustment_that_only_widens_does_not_withdraw() -> None:
    widened = replace(GRANT_TWO, max_scope=DisclosureScope.ANONYMOUS_CASE)
    assert not withdraws_authorization(
        decision=MandateDecision.ADJUST,
        previous=(GRANT_ONE,),
        requested=(GRANT_ONE, widened),
    )


def test_an_adjustment_that_changes_nothing_does_not_withdraw() -> None:
    assert not withdraws_authorization(
        decision=MandateDecision.ADJUST,
        previous=(GRANT_ONE, GRANT_TWO),
        requested=(GRANT_TWO, GRANT_ONE),
    )
