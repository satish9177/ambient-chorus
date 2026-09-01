"""Post-deserialization scope revalidation and staged-write helpers.

A partition key is not an authorization boundary. Every loaded item is checked four ways: its
stored key must equal the key the repository asked for, its stored namespace, community, and
case attributes must equal the requested scope, the decoded entity's own identifiers must
agree with that same scope, and the address the decoded entity claims for itself must be the
address it was actually found at. Any disagreement fails the whole operation with
``CrossCaseViolationError`` -- foreign records are never skipped, filtered, or partially
returned.

The fourth check matters because a query names a partition, not an item. A direct get proves
address agreement for free: the repository built the key from the caller's own identifier, so
a body claiming to be a different entity lands on a key mismatch. A page has no such
identifier to build from, so the expected address is rebuilt from the decoded entity through
the same key builder the writer used, and compared with where the row really is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from chorus.domain.ids import CaseId, CommunityId, Namespace
from chorus.infrastructure.dynamodb.codec import ATTR_VERSION, DecodedScope
from chorus.ports.errors import CrossCaseViolationError
from chorus.ports.storage import (
    AttributeEqualsNumber,
    ItemKey,
    KeyAbsent,
    PutItem,
    StoredItem,
)


class Unchecked:
    """Marker meaning an attribute is data rather than part of the requested scope."""

    __slots__ = ()


UNCHECKED: Final = Unchecked()


@dataclass(frozen=True, slots=True, kw_only=True)
class EntityIdentity:
    """The scope a decoded entity claims for itself, independent of where it was stored.

    A repository builds this from the entity's *own* fields, so a record whose envelope was
    forged to match the requested address is still rejected on its body. Fields the entity
    does not carry stay ``UNCHECKED``; they are never silently treated as agreeing.
    """

    namespace: Namespace | Unchecked = UNCHECKED
    community_id: CommunityId | Unchecked | None = UNCHECKED
    case_id: CaseId | Unchecked | None = UNCHECKED


def _deny_unless(agrees: bool, entity_ref: str) -> None:
    if not agrees:
        raise CrossCaseViolationError(entity_ref)


def validate_identity(
    identity: EntityIdentity,
    *,
    entity_ref: str,
    namespace: Namespace,
    community_id: CommunityId | Unchecked | None = UNCHECKED,
    case_id: CaseId | Unchecked | None = UNCHECKED,
) -> None:
    """Fail the whole operation when a decoded entity disowns the requested scope."""

    if not isinstance(identity.namespace, Unchecked):
        _deny_unless(identity.namespace == namespace, entity_ref)
    if not isinstance(identity.community_id, Unchecked) and not isinstance(community_id, Unchecked):
        _deny_unless(identity.community_id == community_id, entity_ref)
    if not isinstance(identity.case_id, Unchecked) and not isinstance(case_id, Unchecked):
        _deny_unless(identity.case_id == case_id, entity_ref)


def validate_page_scope(
    decoded: DecodedScope,
    identity: EntityIdentity,
    *,
    expected_key: ItemKey,
    entity_ref: str,
    namespace: Namespace,
    community_id: CommunityId | Unchecked | None = UNCHECKED,
    case_id: CaseId | Unchecked | None = UNCHECKED,
) -> None:
    """Revalidate one queried row exactly as strictly as a direct get is revalidated.

    ``expected_key`` is rebuilt by the caller from the decoded entity's *own* identity, so
    comparing it with the stored key proves the row is where the entity it contains says it
    belongs. A row whose sort key names one fact and whose body names another satisfies every
    scope check and is still rejected here.

    The stored envelope and the decoded entity's own identifiers are then checked against the
    requested scope, and a single disagreement fails the entire page rather than dropping the
    row.
    """

    _deny_unless(decoded.partition_key == expected_key.partition_key, entity_ref)
    _deny_unless(decoded.sort_key == expected_key.sort_key, entity_ref)
    _deny_unless(decoded.namespace == namespace, entity_ref)
    if not isinstance(community_id, Unchecked):
        _deny_unless(decoded.community_id == community_id, entity_ref)
    if not isinstance(case_id, Unchecked):
        _deny_unless(decoded.case_id == case_id, entity_ref)
    validate_identity(
        identity,
        entity_ref=entity_ref,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
    )


def validate_scope(
    decoded: DecodedScope,
    *,
    key: ItemKey,
    entity_ref: str,
    namespace: Namespace,
    community_id: CommunityId | Unchecked | None = UNCHECKED,
    case_id: CaseId | Unchecked | None = UNCHECKED,
) -> None:
    """Fail the whole operation when a stored item does not belong to the requested scope."""

    if decoded.partition_key != key.partition_key or decoded.sort_key != key.sort_key:
        raise CrossCaseViolationError(entity_ref)
    if decoded.namespace != namespace:
        raise CrossCaseViolationError(entity_ref)
    if not isinstance(community_id, Unchecked) and decoded.community_id != community_id:
        raise CrossCaseViolationError(entity_ref)
    if not isinstance(case_id, Unchecked) and decoded.case_id != case_id:
        raise CrossCaseViolationError(entity_ref)


def require_same(expected: object, actual: object, entity_ref: str) -> None:
    """Assert one decoded entity field agrees with the requested scope."""

    if expected != actual:
        raise CrossCaseViolationError(entity_ref)


def create_operation(key: ItemKey, item: StoredItem) -> PutItem:
    """Stage a create-only write; an existing item is a conflict, never an overwrite."""

    return PutItem(key=key, item=item, condition=KeyAbsent())


def replace_operation(
    key: ItemKey, item: StoredItem, *, expected_version: int, new_version: int
) -> PutItem:
    """Stage an optimistic replace guarded on the exact expected version."""

    if expected_version < 1:
        raise ValueError("expected version must be positive")
    if new_version != expected_version + 1:
        raise ValueError("an optimistic write must increment the entity version by one")
    return PutItem(
        key=key,
        item=item,
        condition=AttributeEqualsNumber(name=ATTR_VERSION, value=expected_version),
    )
