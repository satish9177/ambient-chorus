"""The Private Mandate Thread: what a contributor is allowed to see about their own mandate.

This is a read model, and its job is as much about omission as about content. The mandate rows
it reads live in the private zone beside raw messages, health details, and apartment numbers,
and the projection is the boundary that decides which of that a contributor's browser ever
holds.

What crosses:

* the proposed and current terms, per fact, with the policy ceiling beside each one so the UI
  can render a locked row as locked rather than as a choice somebody declined to make;
* **contributor-facing wording** built from closed typed fields, never the fact's own value;
* the content grants and the identity grant, kept as separate structures at every level,
  because collapsing them into one "sharing" object is how identity permission gets inferred
  from content permission by a reader who was not being careful;
* destination, purpose, validity, status, version, and the immutable history.

What never crosses: another contributor's mandate, contact fields, private object keys, agent
prompts or output, and any raw fact value. There is no parameter through which one could.

A contributor may read only their own thread. A presenter may read any thread in the namespace
because the frozen access model gives them the private surface -- but a presenter can never
*decide* one, which is enforced at the route rather than here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from chorus.application.services.mandate_terms import contributor_wording, locked_reason
from chorus.domain.entities import (
    CaseState,
    DisclosureScope,
    FactType,
    MandateStatus,
    Purpose,
)
from chorus.domain.facts import Fact
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
from chorus.domain.mandates import DisclosureMandate, IdentityGrant, derived_status
from chorus.domain.time import Clock
from chorus.ports.errors import NotFoundError
from chorus.ports.pagination import PageRequest
from chorus.ports.repositories import CoreRepositoryPort
from chorus.ports.scopes import CaseScope
from chorus.privacy.policy import identity_maximum_scope, policy_maximum_scope

MAX_MANDATES_PER_CASE_PAGE = 100
"""Bound on the strong pointer page. A case is capped at 100 active facts, so the number of
contributors owning one cannot exceed that either."""


@dataclass(frozen=True, slots=True, kw_only=True)
class FactPermission:
    """One fact as its owner sees it: what it is, what is allowed, what was chosen."""

    fact_id: FactId
    fact_type: FactType
    wording: str
    policy_maximum_scope: DisclosureScope
    proposed_scope: DisclosureScope
    current_scope: DisclosureScope
    allow_safe_transformation: bool
    requires_identity_grant: bool
    locked_reason: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class IdentityPermission:
    """Identity permission, always its own structure and never folded into the grants above."""

    externally_shareable: bool
    max_scope: DisclosureScope
    policy_maximum_scope: DisclosureScope


@dataclass(frozen=True, slots=True, kw_only=True)
class MandateVersionSummary:
    """One immutable version in the history, described without its terms."""

    version: int
    status: MandateStatus
    terms_hash: Sha256Digest
    decided_at: datetime | None
    revoked_at: datetime | None
    supersedes_version: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class MandateThread:
    """Everything the Private Mandate Thread surface is entitled to render."""

    mandate_id: MandateId
    case_id: CaseId
    case_state: CaseState
    contributor_id: ContributorId
    current_version: int
    status: MandateStatus
    terms_hash: Sha256Digest
    fact_permissions: tuple[FactPermission, ...]
    identity_permission: IdentityPermission
    allowed_destination_ids: tuple[DestinationId, ...]
    allowed_purposes: tuple[Purpose, ...]
    valid_from: datetime
    expires_at: datetime | None
    proposed_at: datetime
    decided_at: datetime | None
    revoked_at: datetime | None
    history: tuple[MandateVersionSummary, ...]


@dataclass(slots=True)
class ReadMandateThread:
    """Read one contributor's mandate for one case, and nothing else."""

    core: CoreRepositoryPort
    clock: Clock

    async def execute(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        contributor_id: ContributorId,
    ) -> MandateThread:
        """Return the thread, or ``NOT_FOUND`` for anything this caller may not see.

        Resolution goes contributor to mandate, never the other way around. A caller supplies a
        contributor and a case; the pointer that matches both is the only one that can be
        returned, so there is no identifier a caller can supply to reach somebody else's row.
        """

        scope = CaseScope(namespace=namespace, community_id=community_id, case_id=case_id)
        case = await self.core.load_case(scope)
        pointers = await self.core.load_current_mandate_pointers(
            scope, PageRequest(limit=MAX_MANDATES_PER_CASE_PAGE)
        )
        stored = next(
            (
                item
                for item in pointers.items
                if item.pointer.contributor_id == contributor_id
                and item.pointer.case_id == case_id
                and item.namespace == namespace
                and item.community_id == community_id
            ),
            None,
        )
        if stored is None:
            raise NotFoundError("DISCLOSURE_MANDATE")

        current = await self.core.load_mandate_version(
            scope, stored.pointer.mandate_id, stored.pointer.version
        )
        proposal = (
            current
            if current.version == 1
            else await self.core.load_mandate_version(scope, stored.pointer.mandate_id, 1)
        )
        history = await self._history(scope, current)
        facts = await self._facts(scope, case_fact_ids=case.fact_ids, mandate=proposal)
        now = self.clock.now()

        return MandateThread(
            mandate_id=current.mandate_id,
            case_id=current.case_id,
            case_state=case.state,
            contributor_id=current.contributor_id,
            current_version=current.version,
            status=derived_status(current, current_version=current.version, now=now),
            terms_hash=current.terms_hash,
            fact_permissions=self._permissions(facts, proposal=proposal, current=current),
            identity_permission=_identity(current.identity_grant),
            allowed_destination_ids=current.allowed_destination_ids,
            allowed_purposes=current.allowed_purposes,
            valid_from=current.valid_from,
            expires_at=current.expires_at,
            proposed_at=current.proposed_at,
            decided_at=current.decided_at,
            revoked_at=current.revoked_at,
            history=tuple(
                MandateVersionSummary(
                    version=version.version,
                    status=derived_status(version, current_version=current.version, now=now),
                    terms_hash=version.terms_hash,
                    decided_at=version.decided_at,
                    revoked_at=version.revoked_at,
                    supersedes_version=version.supersedes_version,
                )
                for version in history
            ),
        )

    async def _history(
        self, scope: CaseScope, current: DisclosureMandate
    ) -> tuple[DisclosureMandate, ...]:
        """Load every version from one to current, in order.

        A direct get per version rather than a prefix query, because the version numbers are
        already known: they are exactly ``1..current``, with no gaps, since a version is only
        ever created by incrementing the current one inside a guarded transaction. A gap would
        be an integrity failure, and the repository's own scope checks would raise it.
        """

        versions: list[DisclosureMandate] = []
        for number in range(1, current.version + 1):
            versions.append(
                current
                if number == current.version
                else await self.core.load_mandate_version(scope, current.mandate_id, number)
            )
        return tuple(versions)

    async def _facts(
        self,
        scope: CaseScope,
        *,
        case_fact_ids: tuple[FactId, ...],
        mandate: DisclosureMandate,
    ) -> dict[FactId, Fact]:
        """Load exactly the facts this mandate is about, and no others."""

        granted = {grant.fact_id for grant in mandate.fact_grants}
        wanted = tuple(fact_id for fact_id in case_fact_ids if fact_id in granted)
        if not wanted:
            return {}
        loaded = await self.core.load_facts(scope, wanted)
        return {fact.fact_id: fact for fact in loaded}

    @staticmethod
    def _permissions(
        facts: dict[FactId, Fact],
        *,
        proposal: DisclosureMandate,
        current: DisclosureMandate,
    ) -> tuple[FactPermission, ...]:
        """Pair each proposed fact with what policy allows and what is currently granted.

        The row exists for every fact the *proposal* named, even one the current version grants
        nothing for. A refused or narrowed fact that vanished from the list would read as "we
        never collected that", when the truth is "we collected it and you said no" -- and the
        second is the thing a mandate thread exists to show.
        """

        by_id = {grant.fact_id: grant for grant in current.fact_grants}
        rows: list[FactPermission] = []
        for grant in sorted(proposal.fact_grants, key=lambda item: str(item.fact_id)):
            fact = facts.get(grant.fact_id)
            if fact is None:
                # Withdrawn or superseded since the proposal was made. It is no longer a
                # decision anyone can take, so it is not shown as one.
                continue
            ceiling = policy_maximum_scope(fact.fact_type, fact.sensitivity)
            live = by_id.get(grant.fact_id)
            rows.append(
                FactPermission(
                    fact_id=fact.fact_id,
                    fact_type=fact.fact_type,
                    wording=contributor_wording(fact),
                    policy_maximum_scope=ceiling,
                    proposed_scope=grant.max_scope,
                    current_scope=(
                        live.max_scope if live is not None else DisclosureScope.INTERNAL_ONLY
                    ),
                    allow_safe_transformation=(
                        live.allow_safe_transformation if live is not None else False
                    ),
                    requires_identity_grant=fact.fact_type is FactType.IDENTITY_ATTRIBUTE,
                    locked_reason=locked_reason(ceiling),
                )
            )
        return tuple(rows)


def _identity(grant: IdentityGrant) -> IdentityPermission:
    return IdentityPermission(
        externally_shareable=grant.externally_shareable,
        max_scope=grant.max_scope,
        policy_maximum_scope=identity_maximum_scope(),
    )
