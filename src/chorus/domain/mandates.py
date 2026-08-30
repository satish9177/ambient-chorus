"""Immutable disclosure-mandate versions and current-pointer contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from chorus.domain.entities import DisclosureScope, MandateStatus, Purpose
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
