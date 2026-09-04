"""Deterministic policy/v1 rules for what a disclosure mandate may say.

This module answers one question the domain deliberately cannot: *is this authorization inside
what policy/v1 permits?* The domain owns the shape of a decision -- who may take it, which
edges exist, what each decision word may carry -- and it owns none of the disclosure semantics,
because those belong to the policy version and change with it.

Two ceilings are enforced here and they are independent:

* the **content** ceiling, derived per fact from its type and its sensitivity, which decides
  the widest scope a contributor may grant for that exact fact;
* the **identity** ceiling, which decides how far a contributor may let their own identity
  travel and is never inferred from any content grant.

Neither ceiling is a courtesy check that duplicates the compiler. The compiler re-derives all of
it at gates 14, 15, 18 and 19 and would exclude an over-broad fact regardless -- authorization
is necessary but never sufficient. What this module adds is that an over-broad grant is never
*written down*: the contributor is never shown a permission the system would refuse to honour,
and no stored mandate ever claims an authority policy overrides.

Every failure is reported as a bounded :class:`~chorus.privacy.policy.MandateDenialCode`. No
function here returns, logs, or embeds a fact value, a scope the caller asked for, or an
identifier belonging to somebody else.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from chorus.domain.entities import DisclosureScope, Purpose
from chorus.domain.facts import Fact, FactStatus
from chorus.domain.ids import CaseId, CommunityId, ContributorId, DestinationId, FactId, Namespace
from chorus.domain.mandates import FactGrant, IdentityGrant
from chorus.privacy.policy import (
    ALLOWED_PURPOSES,
    MandateDenialCode,
    identity_maximum_scope,
    policy_maximum_scope,
    proposed_scope,
    scope_permits,
)

PROPOSAL_IDENTITY_GRANT = IdentityGrant(
    externally_shareable=False,
    max_scope=DisclosureScope.ANONYMOUS_CASE,
)
"""What a proposal offers for identity, which is nothing.

A proposal that arrived pre-set to "share my name" would make the least-permissive default the
one a contributor has to notice and undo. Identity starts off and stays off until somebody
turns it on deliberately, which is the whole point of it being a separate control.
"""


def grantable_facts(
    facts: Iterable[Fact],
    *,
    contributor_id: ContributorId,
    case_id: CaseId,
    community_id: CommunityId,
    namespace: Namespace,
) -> tuple[Fact, ...]:
    """Return exactly the facts this contributor may be offered a grant over, in stable order.

    Scope is re-verified here rather than assumed from the caller's query. A repository returns
    what its key addressed; this returns what the *policy* is willing to talk about, and the two
    disagreeing is precisely the case worth failing closed on.
    """

    return tuple(
        sorted(
            (
                fact
                for fact in facts
                if fact.contributor_id == contributor_id
                and fact.case_id == case_id
                and fact.community_id == community_id
                and fact.namespace == namespace
                and fact.status is FactStatus.ACTIVE
            ),
            key=lambda fact: str(fact.fact_id),
        )
    )


def build_proposed_grants(facts: Iterable[Fact]) -> tuple[FactGrant, ...]:
    """Build the least-permissive useful grant for each fact, in canonical order.

    Every fact the contributor owns appears, including the ones policy keeps internal. That is
    deliberate: the mandate thread is where a contributor sees *what was collected about them*,
    and silently omitting the health detail and the apartment number would hide the two facts
    they most need to see are locked. They appear at ``INTERNAL_ONLY`` with a ceiling of
    ``INTERNAL_ONLY``, so seeing them is all anyone can do with them.
    """

    return tuple(
        FactGrant(
            fact_id=fact.fact_id,
            max_scope=proposed_scope(fact.fact_type, fact.sensitivity),
            # A fact proposed at INTERNAL_ONLY carries no transformation permission either.
            # Permission to transform something nobody may export is a permission with no
            # subject, and it would read as consent in the mandate thread.
            allow_safe_transformation=proposed_scope(fact.fact_type, fact.sensitivity)
            is not DisclosureScope.INTERNAL_ONLY,
        )
        for fact in sorted(facts, key=lambda fact: str(fact.fact_id))
    )


def validate_destinations_and_purposes(
    *,
    destination_ids: tuple[DestinationId, ...],
    purposes: tuple[Purpose, ...],
    allowed_destination_id: DestinationId,
) -> tuple[MandateDenialCode, ...]:
    """Check a mandate's destination and purpose sets against the policy/v1 allowlists.

    Run when a proposal is built *and* again on every decision, against the values carried
    forward from the stored proposal. The second run is not redundant: the decision request
    cannot express a destination or a purpose, so the only way a bad one reaches a new version
    is a stored row that was already wrong, and that is exactly the case where re-deriving from
    policy rather than trusting storage matters.
    """

    denials: list[MandateDenialCode] = []
    if not destination_ids or any(item != allowed_destination_id for item in destination_ids):
        denials.append(MandateDenialCode.DESTINATION_NOT_ALLOWED)
    if not purposes or any(item not in ALLOWED_PURPOSES for item in purposes):
        denials.append(MandateDenialCode.PURPOSE_NOT_ALLOWED)
    return tuple(denials)


def validate_requested_grants(
    *,
    fact_grants: tuple[FactGrant, ...],
    identity_grant: IdentityGrant,
    expires_at: datetime | None,
    proposed_fact_ids: frozenset[FactId],
    facts_by_id: Mapping[FactId, Fact],
    contributor_id: ContributorId,
    case_id: CaseId,
    community_id: CommunityId,
    namespace: Namespace,
    now: datetime,
) -> tuple[MandateDenialCode, ...]:
    """Return every deterministic reason these authorization terms are refused.

    An empty result means the terms are within policy; it does not mean the resulting export
    would be allowed, which remains the compiler's decision at compile time.

    The result is the *complete* set of distinct reasons rather than the first one found, in a
    stable order, so a contributor correcting a mandate is told everything wrong with it at
    once. Nothing about which specific grant failed is returned -- only that some did, and why.
    """

    denials: set[MandateDenialCode] = set()

    seen: set[FactId] = set()
    for grant in fact_grants:
        if grant.fact_id in seen:
            # Two grants for one fact are two answers to one question. Silently keeping either
            # would let a request mean whichever one the reader happened to iterate to last.
            denials.add(MandateDenialCode.DUPLICATE_FACT_GRANT)
            continue
        seen.add(grant.fact_id)

        fact = facts_by_id.get(grant.fact_id)
        if (
            fact is None
            or fact.contributor_id != contributor_id
            or fact.case_id != case_id
            or fact.community_id != community_id
            or fact.namespace != namespace
            or fact.status is not FactStatus.ACTIVE
        ):
            # Absent, foreign case, foreign community, foreign owner, withdrawn: one answer.
            denials.add(MandateDenialCode.UNKNOWN_FACT)
            continue

        if grant.fact_id not in proposed_fact_ids:
            # The fact is real and owned, but this mandate never offered it. A fact that
            # appeared after the proposal was made belongs to a fresh proposal, not to a
            # decision the contributor took before it existed.
            denials.add(MandateDenialCode.GRANT_NOT_PROPOSED)
            continue

        ceiling = policy_maximum_scope(fact.fact_type, fact.sensitivity)
        if not scope_permits(ceiling, grant.max_scope):
            denials.add(MandateDenialCode.SCOPE_EXCEEDS_POLICY_MAXIMUM)

    if not scope_permits(identity_maximum_scope(), identity_grant.max_scope):
        denials.add(MandateDenialCode.IDENTITY_EXCEEDS_POLICY_MAXIMUM)

    if expires_at is not None and expires_at <= now:
        # An authorization that is already expired at the instant it is written is not a
        # narrower grant, it is an unusable record that reads like consent.
        denials.add(MandateDenialCode.EXPIRY_ALREADY_PASSED)

    return tuple(sorted(denials, key=str))
