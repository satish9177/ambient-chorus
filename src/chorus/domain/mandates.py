"""Immutable disclosure-mandate versions and current-pointer contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

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
from chorus.domain.time import require_utc


@dataclass(frozen=True, slots=True, kw_only=True)
class FactGrant:
    """Maximum use of one exact fact under a mandate version."""

    fact_id: FactId
    max_scope: DisclosureScope
    allow_safe_transformation: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class IdentityGrant:
    """Identity permission evaluated independently from content grants."""

    externally_shareable: bool
    max_scope: DisclosureScope

    def __post_init__(self) -> None:
        allowed = {
            DisclosureScope.ANONYMOUS_CASE,
            DisclosureScope.NAMED_CASE,
            DisclosureScope.EXTERNAL_ACTION,
        }
        if self.max_scope not in allowed:
            raise ValueError("identity grant has an unsupported maximum scope")
        if not self.externally_shareable and self.max_scope is not DisclosureScope.ANONYMOUS_CASE:
            raise ValueError("non-shareable identity must remain anonymous")


@dataclass(frozen=True, slots=True, kw_only=True)
class DisclosureMandate:
    """One append-only authorization decision and exact terms version."""

    mandate_id: MandateId
    version: int
    case_id: CaseId
    community_id: CommunityId
    contributor_id: ContributorId
    namespace: Namespace
    status: MandateStatus
    fact_grants: tuple[FactGrant, ...]
    identity_grant: IdentityGrant
    allowed_destination_ids: tuple[DestinationId, ...]
    allowed_purposes: tuple[Purpose, ...]
    valid_from: datetime
    expires_at: datetime | None
    proposed_at: datetime
    decided_at: datetime | None
    revoked_at: datetime | None
    decision_actor_id: ContributorId | None
    supersedes_version: int | None
    terms_hash: Sha256Digest
    created_at: datetime
    updated_at: datetime
    schema_version: str = "disclosure-mandate/v1"

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("mandate version must be positive")
        if self.supersedes_version is not None and self.supersedes_version != self.version - 1:
            raise ValueError("mandate versions must supersede the immediately previous version")
        if len({grant.fact_id for grant in self.fact_grants}) != len(self.fact_grants):
            raise ValueError("fact grants must be unique")
        if not self.allowed_destination_ids or not self.allowed_purposes:
            raise ValueError("mandate destination and purpose sets cannot be empty")
        if len(set(self.allowed_destination_ids)) != len(self.allowed_destination_ids):
            raise ValueError("allowed destination IDs must be unique")
        if len(set(self.allowed_purposes)) != len(self.allowed_purposes):
            raise ValueError("allowed purposes must be unique")
        for instant in (
            self.valid_from,
            self.proposed_at,
            self.created_at,
            self.updated_at,
        ):
            require_utc(instant)
        optional_instants = (self.expires_at, self.decided_at, self.revoked_at)
        for optional_instant in optional_instants:
            if optional_instant is not None:
                require_utc(optional_instant)
        if self.expires_at is not None and self.expires_at <= self.valid_from:
            raise ValueError("mandate expiry must be after valid_from")
        if self.status is MandateStatus.PROPOSED:
            if self.decided_at is not None or self.decision_actor_id is not None:
                raise ValueError("proposed mandate cannot have a decision")
        else:
            if self.decided_at is None or self.decision_actor_id != self.contributor_id:
                raise ValueError("mandate decision must be made by its contributor")
        if self.status is MandateStatus.APPROVED and self.decided_at is not None:
            if self.decided_at < self.valid_from:
                raise ValueError("approval predates mandate validity")
            if self.expires_at is not None and self.decided_at >= self.expires_at:
                raise ValueError("approval occurred after mandate expiry")
        if self.status is MandateStatus.REVOKED:
            if self.revoked_at is None:
                raise ValueError("revoked mandate requires revoked_at")
        elif self.revoked_at is not None:
            raise ValueError("only revoked mandate versions may have revoked_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentMandatePointer:
    """Strongly read pointer used to prove which immutable version is current."""

    mandate_id: MandateId
    version: int
    case_id: CaseId
    contributor_id: ContributorId
    terms_hash: Sha256Digest

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("current mandate version must be positive")


class MandateDecision(StrEnum):
    """The four authorization decisions a contributor may take on their own mandate."""

    APPROVE = "APPROVE"
    ADJUST = "ADJUST"
    REFUSE = "REFUSE"
    REVOKE = "REVOKE"


MANDATE_DECISION_EDGES: dict[tuple[MandateStatus, MandateDecision], MandateStatus] = {
    (MandateStatus.PROPOSED, MandateDecision.APPROVE): MandateStatus.APPROVED,
    (MandateStatus.PROPOSED, MandateDecision.ADJUST): MandateStatus.APPROVED,
    (MandateStatus.PROPOSED, MandateDecision.REFUSE): MandateStatus.REFUSED,
    (MandateStatus.APPROVED, MandateDecision.ADJUST): MandateStatus.APPROVED,
    (MandateStatus.APPROVED, MandateDecision.REVOKE): MandateStatus.REVOKED,
}
"""Every legal decision edge, and by omission every refused one.

The table is exhaustive on purpose: a decision it does not name mutates nothing and raises
:class:`~chorus.domain.errors.StateTransitionError`. Four omissions are deliberate readings of
the frozen contract rather than oversights.

* ``PROPOSED`` + ``REVOKE`` -- nothing has been granted, so there is nothing to revoke.
  Accepting it would mint a ``REVOKED`` version that never authorized anything, and would make
  "was this ever approved?" unanswerable from the history alone.
* ``APPROVED`` + ``APPROVE`` -- a second approval of terms already approved changes no
  authorization. Appending a version for it would still move the current pointer and stale
  every view compiled against the previous one, while saying nothing new.
* ``APPROVED`` + ``REFUSE`` -- refusal is the answer to a proposal. Withdrawing authorization
  that already exists is ``REVOKE``, which is the edge that records ``revoked_at`` and the one
  the compiler's revocation gate reads.
* ``REFUSED`` and ``REVOKED`` are terminal. The frozen contract calls them terminal versions,
  and a contributor who wants to authorize afterwards needs a fresh proposal rather than a
  resurrection of the record of their refusal.

``EXPIRED`` never appears as a source because it is never stored: expiry is derived from the
injected clock against the current version, and :func:`decision_is_within_validity` refuses
every decision on an expired mandate before this table is consulted.
"""

NO_IDENTITY_GRANT = IdentityGrant(
    externally_shareable=False,
    max_scope=DisclosureScope.ANONYMOUS_CASE,
)
"""No identity permission at all: the only identity grant a terminal decision may carry."""


def next_mandate_status(status: MandateStatus, decision: MandateDecision) -> MandateStatus:
    """Return the status one decision produces, or refuse the edge."""

    target = MANDATE_DECISION_EDGES.get((status, decision))
    if target is None:
        raise StateTransitionError("DISCLOSURE_MANDATE")
    return target


def mandate_is_expired(mandate: DisclosureMandate, now: datetime) -> bool:
    """True when ``now`` is at or past expiry. Equality at ``expires_at`` is expired."""

    require_utc(now)
    return mandate.expires_at is not None and now >= mandate.expires_at


def decision_is_within_validity(mandate: DisclosureMandate, now: datetime) -> bool:
    """True when a decision at ``now`` falls inside the mandate's own validity window."""

    require_utc(now)
    return mandate.valid_from <= now and not mandate_is_expired(mandate, now)


def is_superseded(mandate: DisclosureMandate, *, current_version: int) -> bool:
    """True when a stored non-terminal version is no longer the one the pointer names.

    ``SUPERSEDED`` is derived rather than stored, because a mandate version row is create-only
    and the frozen contract says historical versions never mutate. The successor records the
    relationship in ``supersedes_version``; the pointer move is what makes it true.
    """

    return mandate.version < current_version and mandate.status in {
        MandateStatus.PROPOSED,
        MandateStatus.APPROVED,
    }


def derived_status(
    mandate: DisclosureMandate, *, current_version: int, now: datetime
) -> MandateStatus:
    """Return the status a reader should see, after applying the two derived rules."""

    if is_superseded(mandate, current_version=current_version):
        return MandateStatus.SUPERSEDED
    if mandate.status is MandateStatus.APPROVED and mandate_is_expired(mandate, now):
        return MandateStatus.EXPIRED
    return mandate.status


def terms_are_identical(
    mandate: DisclosureMandate,
    *,
    fact_grants: tuple[FactGrant, ...],
    identity_grant: IdentityGrant,
    expires_at: datetime | None,
) -> bool:
    """True when a submitted decision reproduces a version's authorization terms exactly.

    Fact grants are compared as a set, because a client orders a JSON array however it likes
    and the canonical terms payload sorts them anyway. Everything else is compared by value: an
    ``APPROVE`` that changed one scope, one transformation flag, or the expiry is a different
    authorization wearing the word "approve".
    """

    return (
        set(mandate.fact_grants) == set(fact_grants)
        and mandate.identity_grant == identity_grant
        and mandate.expires_at == expires_at
    )


_NARROWING_DECISIONS: frozenset[MandateDecision] = frozenset(
    {MandateDecision.REFUSE, MandateDecision.REVOKE}
)
"""Decisions that always take back authority somebody may already be relying on."""


def withdraws_authorization(
    *,
    decision: MandateDecision,
    previous: tuple[FactGrant, ...],
    requested: tuple[FactGrant, ...],
) -> bool:
    """True when this decision removes something the current version allowed.

    A refusal or a revocation always does. An adjustment does when any previously granted fact
    is dropped or its grant changed -- compared *per fact* rather than by counting, because
    swapping one grant for another leaves the count identical while changing what may be
    exported. Widening is not considered here; policy has already refused anything above the
    ceiling, and a legitimate widening is not a withdrawal.

    This is what readiness reconciliation reads. A case that was ready to act loses that
    readiness when a contributor takes authority back, and keeps it when they only add.
    """

    if decision in _NARROWING_DECISIONS:
        return True
    if decision is not MandateDecision.ADJUST:
        return False
    by_id = {grant.fact_id: grant for grant in requested}
    return any(by_id.get(grant.fact_id) != grant for grant in previous)


def decide_mandate(
    current: DisclosureMandate,
    *,
    decision: MandateDecision,
    fact_grants: tuple[FactGrant, ...],
    identity_grant: IdentityGrant,
    expires_at: datetime | None,
    actor_id: ContributorId,
    now: datetime,
    terms_hash: Sha256Digest,
) -> DisclosureMandate:
    """Build the next immutable mandate version, or refuse without mutating anything.

    This is the structural half of the decision. Whether the requested scopes stay inside the
    deterministic policy/v1 maximums is a *policy* question and lives in
    :mod:`chorus.privacy.policy`; the caller runs that first. What is decided here is the shape
    of the decision itself: who may take it, which edges exist, whether the mandate is still
    inside its own validity window, and what each decision word is allowed to carry.

    ``terms_hash`` is supplied rather than computed because canonicalization lives in the
    privacy package, which the domain may not import. The caller seals the returned version by
    recomputing the hash over it; the canonical terms payload deliberately excludes the hash
    field, so the two can never disagree about what was covered.
    """

    require_utc(now)
    if actor_id != current.contributor_id:
        # Identity authorization applies only to the contributor making the decision, and so
        # does content authorization. A mandate is never decided on someone else's behalf.
        raise StateTransitionError("DISCLOSURE_MANDATE")
    if not decision_is_within_validity(current, now):
        raise StateTransitionError("DISCLOSURE_MANDATE")
    status = next_mandate_status(current.status, decision)

    if decision is MandateDecision.APPROVE and not terms_are_identical(
        current,
        fact_grants=fact_grants,
        identity_grant=identity_grant,
        expires_at=expires_at,
    ):
        # Approve means "yes, exactly this". A caller who wants different terms has to say
        # ADJUST, which is validated against the policy maximums as a complete replacement.
        raise StateTransitionError("DISCLOSURE_MANDATE")

    if decision in {MandateDecision.REFUSE, MandateDecision.REVOKE}:
        if fact_grants or identity_grant != NO_IDENTITY_GRANT:
            # A refusal or revocation carrying a grant would be an authorization wearing the
            # word "no". The request is refused before any version is built.
            raise StateTransitionError("DISCLOSURE_MANDATE")
        granted: tuple[FactGrant, ...] = ()
        granted_identity = NO_IDENTITY_GRANT
        effective_expiry = current.expires_at
    else:
        granted = tuple(sorted(fact_grants, key=lambda grant: str(grant.fact_id)))
        granted_identity = identity_grant
        effective_expiry = expires_at

    try:
        return DisclosureMandate(
            mandate_id=current.mandate_id,
            version=current.version + 1,
            case_id=current.case_id,
            community_id=current.community_id,
            contributor_id=current.contributor_id,
            namespace=current.namespace,
            status=status,
            fact_grants=granted,
            identity_grant=granted_identity,
            allowed_destination_ids=current.allowed_destination_ids,
            allowed_purposes=current.allowed_purposes,
            valid_from=current.valid_from,
            expires_at=effective_expiry,
            proposed_at=current.proposed_at,
            decided_at=now,
            revoked_at=now if status is MandateStatus.REVOKED else None,
            decision_actor_id=actor_id,
            supersedes_version=current.version,
            terms_hash=terms_hash,
            created_at=now,
            updated_at=now,
        )
    except ValueError as error:
        # A duplicate fact grant, an expiry at or before validity, or any other closed
        # invariant. The rejected value never reaches the message.
        raise ValidationError("DISCLOSURE_MANDATE") from error
