"""Nominal identifiers, digests, and deterministic ID generation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, TypeVar
from uuid import UUID, uuid4, uuid5


@dataclass(frozen=True, slots=True)
class UUIDIdentifier:
    """Base for identifiers that must never be mixed by type."""

    value: UUID

    @classmethod
    def parse(cls, value: str) -> UUIDIdentifier:
        """Parse a lowercase, hyphenated UUID into this nominal type."""

        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError("identifier must be a lowercase hyphenated UUID")
        return cls(parsed)

    def __str__(self) -> str:
        return str(self.value)


class CommunityId(UUIDIdentifier):
    __slots__ = ()


class ContributorId(UUIDIdentifier):
    __slots__ = ()


class MessageId(UUIDIdentifier):
    __slots__ = ()


class ReportId(UUIDIdentifier):
    __slots__ = ()


class FactId(UUIDIdentifier):
    __slots__ = ()


class EvidenceRootId(UUIDIdentifier):
    __slots__ = ()


class EvidenceItemId(UUIDIdentifier):
    __slots__ = ()


class MandateId(UUIDIdentifier):
    __slots__ = ()


class CaseId(UUIDIdentifier):
    __slots__ = ()


class AssessmentId(UUIDIdentifier):
    __slots__ = ()


class ViewId(UUIDIdentifier):
    __slots__ = ()


class ExportFactId(UUIDIdentifier):
    __slots__ = ()


class SafeEvidenceRefId(UUIDIdentifier):
    __slots__ = ()


class ActionId(UUIDIdentifier):
    __slots__ = ()


class ApprovalId(UUIDIdentifier):
    __slots__ = ()


class ExecutionId(UUIDIdentifier):
    __slots__ = ()


class CommitmentId(UUIDIdentifier):
    __slots__ = ()


class OperationId(UUIDIdentifier):
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Namespace:
    """Validated isolation namespace."""

    value: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"(?:LOCAL_[A-Za-z0-9_-]+|DEMO|TEST_[A-Za-z0-9_-]+)", self.value) is None:
            raise ValueError("invalid namespace")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class DestinationId:
    """Opaque destination-registry identity; never an address."""

    value: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_-]{1,63}:[a-z0-9_-]{1,64}", self.value) is None:
            raise ValueError("invalid destination ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Sha256Digest:
    """Canonical prefixed SHA-256 digest."""

    value: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.value) is None:
            raise ValueError("invalid SHA-256 digest")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SensitiveStr:
    """Private text that redacts accidental string/repr rendering."""

    _value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self._value:
            raise ValueError("sensitive text cannot be empty")

    def reveal(self) -> str:
        """Reveal only at an explicitly private boundary."""

        return self._value

    def __repr__(self) -> str:
        return "SensitiveStr('***')"

    def __str__(self) -> str:
        return "***"


IdentifierT = TypeVar("IdentifierT", bound=UUIDIdentifier)


class IdGenerator(Protocol):
    """Injected source for normal or deterministic identifiers."""

    def new(self, identifier_type: type[IdentifierT]) -> IdentifierT:
        """Create one identifier of the requested nominal type."""


@dataclass(slots=True)
class Uuid4Generator:
    """Production identifier generator."""

    def new(self, identifier_type: type[IdentifierT]) -> IdentifierT:
        return identifier_type(uuid4())


@dataclass(slots=True)
class Uuid5Generator:
    """Namespace-scoped deterministic generator for synthetic fixtures."""

    namespace: UUID
    prefix: str = "fixture"
    _counter: int = field(default=0, init=False, repr=False)

    def new(self, identifier_type: type[IdentifierT]) -> IdentifierT:
        self._counter += 1
        value = uuid5(self.namespace, f"{self.prefix}:{identifier_type.__name__}:{self._counter}")
        return identifier_type(value)
