"""Explicit transaction composition.

A ``TransactionPlan`` is built by naming the exact operations a bounded use case needs; there
is no generic transaction DSL and no "save everything" entry point. Plans are validated
locally before any storage call so an oversized, duplicate-target, or unaudited mutation is
rejected without touching DynamoDB.

Unknown outcomes are not retried blindly. A plan that mutates durable state carries a
``CommitProof``: the idempotency item the transaction itself writes. After an ambiguous
transport failure the runner reads that item strongly and only then decides whether the
transaction committed, definitely did not commit, or remains unproven.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from chorus.domain.ids import Sha256Digest
from chorus.ports.errors import (
    TransactionLimitExceededError,
    UnauditedMutationError,
)
from chorus.ports.idempotency import REQUEST_HASH_ATTRIBUTE
from chorus.ports.limits import TRANSACTION_MAX_OPERATIONS
from chorus.ports.storage import (
    AllOf,
    AnyOf,
    AttributeAtMostNumber,
    AttributeEqualsNumber,
    AttributeEqualsString,
    CheckItem,
    ItemCondition,
    ItemKey,
    KeyAbsent,
    KeyPresent,
    PutItem,
    StoredValue,
    TableName,
    WriteOperation,
)

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from hashlib import _Hash


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitProof:
    """The idempotency item whose presence proves this exact transaction committed.

    The proof binds four things at once: the physical table and the exact partition/sort key
    the item lives at (all three carried by ``key``), the request hash the item records, and
    -- enforced by the plan -- create-only write semantics. A proof that does not match the
    item its own plan persists would let a *different* command's record be read as evidence
    that this one committed, so the plan refuses to be built at all.
    """

    key: ItemKey
    request_hash: Sha256Digest


@dataclass(frozen=True, slots=True, kw_only=True)
class TransactionPlan:
    """A validated, bounded, atomic set of conditional writes."""

    name: str
    operations: tuple[WriteOperation, ...]
    audit_required: bool
    commit_proof: CommitProof | None = None
    client_request_token: str = field(init=False)

    def __post_init__(self) -> None:
        if not 1 <= len(self.name) <= 64:
            raise ValueError("transaction name length is invalid")
        if not self.operations:
            raise ValueError("a transaction requires at least one operation")
        if len(self.operations) > TRANSACTION_MAX_OPERATIONS:
            raise TransactionLimitExceededError(self.name)
        targets = tuple(
            (operation.key.table, operation.key.partition_key, operation.key.sort_key)
            for operation in self.operations
        )
        if len(set(targets)) != len(targets):
            raise ValueError("a transaction cannot address the same item twice")
        if self.audit_required and not self._has_append_only_audit_write():
            raise UnauditedMutationError(self.name)
        if not self.audit_required and self._audit_writes():
            raise UnauditedMutationError(self.name)
        self._validate_commit_proof()
        object.__setattr__(self, "client_request_token", _client_request_token(self))

    def _validate_commit_proof(self) -> None:
        """Reject a proof this plan does not actually persist, before touching storage.

        The proof is only evidence if this plan itself creates that exact item *with that
        exact request hash*. A plan whose proof named the right address but a different
        request would resolve an ambiguous outcome against someone else's record, so the
        disagreement is a local composition error and never reaches DynamoDB.
        """

        proof = self.commit_proof
        if proof is None:
            return
        writes = tuple(
            operation
            for operation in self.operations
            if isinstance(operation, PutItem)
            and isinstance(operation.condition, KeyAbsent)
            and operation.key == proof.key
        )
        if not writes:
            raise ValueError("commit proof must name a create-only write inside this plan")
        for operation in writes:
            if operation.item.get(REQUEST_HASH_ATTRIBUTE) != proof.request_hash.value:
                raise ValueError("commit proof must bind the request hash its plan persists")

    def _audit_writes(self) -> tuple[WriteOperation, ...]:
        return tuple(
            operation
            for operation in self.operations
            if operation.key.table is TableName.AUDIT and not isinstance(operation, CheckItem)
        )

    def _has_append_only_audit_write(self) -> bool:
        writes = self._audit_writes()
        return bool(writes) and all(
            isinstance(operation, PutItem) and isinstance(operation.condition, KeyAbsent)
            for operation in writes
        )


def _tag(digest: _Hash, label: bytes, payload: bytes = b"") -> None:
    """Absorb a length-prefixed, tagged field so two different plans cannot collide."""

    digest.update(label)
    digest.update(str(len(payload)).encode("ascii"))
    digest.update(b":")
    digest.update(payload)


def _absorb_value(digest: _Hash, value: StoredValue) -> None:
    """Absorb one stored value with an unambiguous type tag."""

    if value is None:
        _tag(digest, b"n")
    elif isinstance(value, bool):
        _tag(digest, b"b", b"1" if value else b"0")
    elif isinstance(value, int):
        _tag(digest, b"i", str(value).encode("ascii"))
    elif isinstance(value, str):
        _tag(digest, b"s", value.encode("utf-8"))
    elif isinstance(value, tuple):
        _tag(digest, b"l", str(len(value)).encode("ascii"))
        for item in value:
            _absorb_value(digest, item)
    else:
        _tag(digest, b"m", str(len(value)).encode("ascii"))
        for name in sorted(value):
            _tag(digest, b"k", name.encode("utf-8"))
            _absorb_value(digest, value[name])


def _absorb_condition(digest: _Hash, condition: ItemCondition) -> None:
    _tag(digest, b"c", type(condition).__name__.encode("ascii"))
    match condition:
        case KeyAbsent() | KeyPresent():
            return
        case AttributeEqualsString(name=name, value=text):
            _tag(digest, b"k", name.encode("utf-8"))
            _absorb_value(digest, text)
        case (
            AttributeEqualsNumber(name=name, value=number)
            | AttributeAtMostNumber(name=name, value=number)
        ):
            _tag(digest, b"k", name.encode("utf-8"))
            _absorb_value(digest, number)
        case AllOf(inner) | AnyOf(inner):
            _tag(digest, b"g", str(len(inner)).encode("ascii"))
            for nested in inner:
                _absorb_condition(digest, nested)
        case _:  # pragma: no cover - the condition union is closed
            raise AssertionError("unreachable item condition")


def _client_request_token(plan: TransactionPlan) -> str:
    """Derive a deterministic 36-character token from the plan's entire content.

    DynamoDB treats a request arriving under a previously seen token as idempotent for ten
    minutes, so the token must cover everything that can differ between two requests.
    Deriving it from the addressed keys alone would let a second, genuinely different
    mutation of the same items be discarded as a replay of the first.

    This is defence in depth only; commit proof, not the token, is what authorises a retry.
    """

    digest = sha256()
    _tag(digest, b"t", plan.name.encode("utf-8"))
    _tag(digest, b"#", str(len(plan.operations)).encode("ascii"))
    for operation in plan.operations:
        _tag(digest, b"o", type(operation).__name__.encode("ascii"))
        _tag(digest, b"T", operation.key.table.value.encode("ascii"))
        _tag(digest, b"p", operation.key.partition_key.encode("utf-8"))
        _tag(digest, b"q", operation.key.sort_key.encode("utf-8"))
        _absorb_condition(digest, operation.condition)
        if isinstance(operation, PutItem):
            _absorb_value(digest, operation.item)
    return str(UUID(bytes=digest.digest()[:16], version=4))


@dataclass(frozen=True, slots=True)
class TransactionCommitted:
    """Proof was found that the ambiguous transaction did commit."""


@dataclass(frozen=True, slots=True)
class TransactionNotCommitted:
    """Proof was found that the ambiguous transaction did not commit."""


@dataclass(frozen=True, slots=True)
class TransactionOutcomeUnproven:
    """Neither commit nor non-commit could be established; never retry."""


type TransactionOutcome = (
    TransactionCommitted | TransactionNotCommitted | TransactionOutcomeUnproven
)


class UnitOfWork(Protocol):
    """Commits one explicitly composed transaction plan."""

    async def commit(self, plan: TransactionPlan) -> None:
        """Apply the plan atomically, resolving ambiguous outcomes before any retry."""

    async def resolve_outcome(self, plan: TransactionPlan) -> TransactionOutcome:
        """Read the plan's commit proof strongly and classify an ambiguous outcome."""
