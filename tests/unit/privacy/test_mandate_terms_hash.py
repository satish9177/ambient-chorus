"""``terms_hash`` binds the exact authorization terms, and nothing looser.

The hash is what a compiled view carries instead of the mandate itself, so it is the only thing
standing between "this view was authorized" and "some mandate with this identifier existed". It
has to move when authorization moves and stay still when nothing authorization-relevant did.

Everything here goes through the frozen ``chorus.privacy.canonical`` primitives. No test builds
its own JSON: an ad-hoc serialization that agreed with the real one would prove nothing, and one
that disagreed would be testing itself.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from chorus.application.services.mandate_terms import PLACEHOLDER_TERMS_HASH, seal
from chorus.domain.entities import DisclosureScope, MandateStatus, Purpose
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    DestinationId,
    FactId,
    MandateId,
    Namespace,
)
from chorus.domain.mandates import DisclosureMandate, FactGrant, IdentityGrant
from chorus.privacy.canonical import (
    canonical_bytes,
    hash_mandate_terms,
    mandate_terms_payload,
    verify_hash,
)

NAMESPACE = Namespace("TEST_TERMS")
NOW = datetime(2030, 1, 20, 12, 0, 0, tzinfo=UTC)
CASE_ID = CaseId(UUID("11111111-1111-4111-8111-111111111111"))
COMMUNITY_ID = CommunityId(UUID("22222222-2222-4222-8222-222222222222"))
OWNER = ContributorId(UUID("33333333-3333-4333-8333-333333333333"))
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
    max_scope=DisclosureScope.EXTERNAL_ACTION,
    allow_safe_transformation=True,
)


def mandate(**overrides: object) -> DisclosureMandate:
    base = {
        "mandate_id": MANDATE_ID,
        "version": 2,
        "case_id": CASE_ID,
        "community_id": COMMUNITY_ID,
        "contributor_id": OWNER,
        "namespace": NAMESPACE,
        "status": MandateStatus.APPROVED,
        "fact_grants": (GRANT_ONE, GRANT_TWO),
        "identity_grant": IdentityGrant(
            externally_shareable=False, max_scope=DisclosureScope.ANONYMOUS_CASE
        ),
        "allowed_destination_ids": (DESTINATION,),
        "allowed_purposes": (Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,),
        "valid_from": NOW - timedelta(days=1),
        "expires_at": None,
        "proposed_at": NOW - timedelta(days=1),
        "decided_at": NOW,
        "revoked_at": None,
        "decision_actor_id": OWNER,
        "supersedes_version": 1,
        "terms_hash": PLACEHOLDER_TERMS_HASH,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return DisclosureMandate(**base)  # type: ignore[arg-type]


# -- determinism ------------------------------------------------------------------------


def test_the_same_terms_hash_identically() -> None:
    assert hash_mandate_terms(mandate()) == hash_mandate_terms(mandate())


def test_reordered_fact_grants_produce_the_same_hash() -> None:
    """The payload sorts grants, so array order carries no authorization meaning."""

    assert hash_mandate_terms(mandate(fact_grants=(GRANT_ONE, GRANT_TWO))) == hash_mandate_terms(
        mandate(fact_grants=(GRANT_TWO, GRANT_ONE))
    )


def test_reordered_grants_produce_identical_canonical_bytes() -> None:
    left = canonical_bytes(mandate_terms_payload(mandate(fact_grants=(GRANT_ONE, GRANT_TWO))))
    right = canonical_bytes(mandate_terms_payload(mandate(fact_grants=(GRANT_TWO, GRANT_ONE))))
    assert left == right


def test_the_hash_has_the_frozen_digest_shape() -> None:
    value = hash_mandate_terms(mandate()).value
    assert value.startswith("sha256:")
    assert len(value) == len("sha256:") + 64
    assert value[7:] == value[7:].lower()


# -- what the hash does not cover -------------------------------------------------------


@pytest.mark.parametrize("field", ["status", "decided_at", "revoked_at", "decision_actor_id"])
def test_decision_metadata_is_outside_the_terms_hash(field: str) -> None:
    """The hash covers the *terms*, and a status is an outcome rather than a term.

    This is the frozen payload's own shape, restated as a test so a well-meaning change that
    folded status into the hash would break loudly: every previously compiled view carries a
    terms hash, and moving what it covers would invalidate all of them at once.
    """

    assert field not in mandate_terms_payload(mandate())


def test_timestamps_that_are_not_authorization_terms_are_outside_the_hash() -> None:
    payload = mandate_terms_payload(mandate())
    assert "created_at" not in payload
    assert "updated_at" not in payload
    assert "proposed_at" not in payload


# -- what the hash must cover -----------------------------------------------------------


def test_a_changed_fact_grant_scope_changes_the_hash() -> None:
    widened = replace(GRANT_ONE, max_scope=DisclosureScope.EXTERNAL_ACTION)
    assert hash_mandate_terms(mandate()) != hash_mandate_terms(
        mandate(fact_grants=(widened, GRANT_TWO))
    )


def test_a_changed_transformation_flag_changes_the_hash() -> None:
    flipped = replace(GRANT_ONE, allow_safe_transformation=False)
    assert hash_mandate_terms(mandate()) != hash_mandate_terms(
        mandate(fact_grants=(flipped, GRANT_TWO))
    )


def test_a_dropped_fact_grant_changes_the_hash() -> None:
    assert hash_mandate_terms(mandate()) != hash_mandate_terms(mandate(fact_grants=(GRANT_ONE,)))


def test_a_changed_identity_grant_changes_the_hash() -> None:
    shared = IdentityGrant(externally_shareable=True, max_scope=DisclosureScope.NAMED_CASE)
    assert hash_mandate_terms(mandate()) != hash_mandate_terms(mandate(identity_grant=shared))


def test_identity_and_content_move_the_hash_independently() -> None:
    """Four distinct authorizations, four distinct hashes."""

    shared = IdentityGrant(externally_shareable=True, max_scope=DisclosureScope.NAMED_CASE)
    narrow = (replace(GRANT_ONE, max_scope=DisclosureScope.INTERNAL_ONLY), GRANT_TWO)
    hashes = {
        hash_mandate_terms(mandate()),
        hash_mandate_terms(mandate(identity_grant=shared)),
        hash_mandate_terms(mandate(fact_grants=narrow)),
        hash_mandate_terms(mandate(fact_grants=narrow, identity_grant=shared)),
    }
    assert len(hashes) == 4


def test_a_changed_expiry_changes_the_hash() -> None:
    assert hash_mandate_terms(mandate()) != hash_mandate_terms(
        mandate(expires_at=NOW + timedelta(days=1))
    )


def test_a_changed_destination_changes_the_hash() -> None:
    other = DestinationId("property_manager:other")
    assert hash_mandate_terms(mandate()) != hash_mandate_terms(
        mandate(allowed_destination_ids=(other,))
    )


def test_a_changed_version_changes_the_hash() -> None:
    """Two versions with identical terms are still two authorizations, and hash differently."""

    assert hash_mandate_terms(mandate(version=2)) != hash_mandate_terms(
        mandate(version=3, supersedes_version=2)
    )


def test_a_changed_owner_or_case_changes_the_hash() -> None:
    other_owner = ContributorId(UUID("44444444-4444-4444-8444-444444444444"))
    other_case = CaseId(UUID("1a111111-1111-4111-8111-111111111111"))
    base = hash_mandate_terms(mandate())
    assert base != hash_mandate_terms(
        mandate(contributor_id=other_owner, decision_actor_id=other_owner)
    )
    assert base != hash_mandate_terms(mandate(case_id=other_case))


def test_a_changed_valid_from_changes_the_hash() -> None:
    assert hash_mandate_terms(mandate()) != hash_mandate_terms(
        mandate(valid_from=NOW - timedelta(days=2), proposed_at=NOW - timedelta(days=2))
    )


# -- sealing and verification -----------------------------------------------------------


def test_sealing_produces_a_hash_that_verifies_against_its_own_terms() -> None:
    sealed = seal(mandate())
    assert sealed.terms_hash == hash_mandate_terms(sealed)
    assert verify_hash(mandate_terms_payload(sealed), sealed.terms_hash)


def test_sealing_is_idempotent() -> None:
    """Because the payload excludes the hash field, re-sealing cannot change the answer."""

    once = seal(mandate())
    assert seal(once).terms_hash == once.terms_hash


def test_a_tampered_hash_fails_verification() -> None:
    sealed = seal(mandate())
    tampered = replace(sealed, terms_hash=PLACEHOLDER_TERMS_HASH)
    assert not verify_hash(mandate_terms_payload(tampered), tampered.terms_hash)


def test_terms_tampered_after_sealing_fail_verification() -> None:
    """The other direction: the hash stands and the terms beneath it moved."""

    sealed = seal(mandate())
    widened = replace(GRANT_ONE, max_scope=DisclosureScope.EXTERNAL_ACTION)
    tampered = replace(sealed, fact_grants=(widened, GRANT_TWO))
    assert not verify_hash(mandate_terms_payload(tampered), tampered.terms_hash)
