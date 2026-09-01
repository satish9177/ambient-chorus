"""Typed access scopes required by every repository method.

A scope is the authorization boundary of a repository call. Keys are derived from a scope and
every loaded record is revalidated against the same scope after deserialization, because a
partition key alone is not an authorization boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.domain.ids import ActionId, CaseId, CommunityId, Namespace


@dataclass(frozen=True, slots=True, kw_only=True)
class NamespaceScope:
    """Namespace isolation boundary shared by every table."""

    namespace: Namespace


@dataclass(frozen=True, slots=True, kw_only=True)
class CommunityScope:
    """Community item-collection boundary inside one namespace."""

    namespace: Namespace
    community_id: CommunityId

    @property
    def namespace_scope(self) -> NamespaceScope:
        return NamespaceScope(namespace=self.namespace)


@dataclass(frozen=True, slots=True, kw_only=True)
class CaseScope:
    """Case boundary required by every case-scoped repository operation."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId

    @property
    def community_scope(self) -> CommunityScope:
        return CommunityScope(namespace=self.namespace, community_id=self.community_id)

    @property
    def namespace_scope(self) -> NamespaceScope:
        return NamespaceScope(namespace=self.namespace)


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionScope:
    """Shareable action partition bound to its owning case."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId

    @property
    def case_scope(self) -> CaseScope:
        return CaseScope(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
        )
