"""Durable command idempotency contract.

An idempotency key is scoped to ``{namespace, command_type, actor_id}`` and binds a SHA-256
request hash. The same key with the same hash replays the recorded outcome; the same key with
a different hash is a conflict. Record expiry is cleanup only: an intrinsically unique side
effect such as an accepted SES send stays authoritative through its own execution state long
after the idempotency record's TTL has passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from chorus.domain.entities import ActionExecutionState
from chorus.domain.ids import ActionId, CaseId, CommunityId, Namespace, Sha256Digest
from chorus.domain.time import require_utc
from chorus.ports.limits import (
    ORDINARY_IDEMPOTENCY_TTL_SECONDS,
    SEND_IDEMPOTENCY_TTL_SECONDS,
)

REQUEST_HASH_ATTRIBUTE: Final = "request_hash"
"""Persisted attribute name a commit proof binds to.

The name lives here rather than in the codec because a ``TransactionPlan`` -- which may not
import infrastructure -- has to verify that the proof it carries matches the request hash the
plan actually persists.
"""

STATUS_ATTRIBUTE: Final = "status"
VERSION_ATTRIBUTE: Final = "version"
"""The two further attributes a *completion* proof binds to.

A commit proof is ordinarily the create-only record its own transaction writes, so the item's
mere presence settles the outcome. A transaction that instead **completes a reservation** has
no such luxury: the record was already there, in ``IN_PROGRESS``, before the transaction ran.
Its proof therefore names the exact status and version the completing write moves the record
to, and presence alone proves nothing.
"""


class IdempotentCommand(StrEnum):
    """Closed set of replayable commands; the value is an uppercase key segment."""

    INGEST_MESSAGE = "INGEST_MESSAGE"
    START_MONITOR_OPERATION = "START_MONITOR_OPERATION"
    APPLY_MONITOR_OUTPUT = "APPLY_MONITOR_OUTPUT"
    DECIDE_MANDATE = "DECIDE_MANDATE"
    APPLY_INVESTIGATION = "APPLY_INVESTIGATION"
    COMPILE_VIEW = "COMPILE_VIEW"
    PROPOSE_ACTION = "PROPOSE_ACTION"
    APPROVE_ACTION = "APPROVE_ACTION"
    SEND_ACTION = "SEND_ACTION"
    CREATE_COMMITMENT = "CREATE_COMMITMENT"
    VERIFY_COMMITMENT = "VERIFY_COMMITMENT"


SEND_COMMANDS: frozenset[IdempotentCommand] = frozenset({IdempotentCommand.SEND_ACTION})

SEND_ATTEMPT_AUTHORITATIVE_STATES: frozenset[ActionExecutionState] = frozenset(
    {ActionExecutionState.SENT, ActionExecutionState.SEND_UNKNOWN}
)


def retention_seconds(command: IdempotentCommand) -> int:
    """Return the frozen retention for a command family."""

    if command in SEND_COMMANDS:
        return SEND_IDEMPOTENCY_TTL_SECONDS
    return ORDINARY_IDEMPOTENCY_TTL_SECONDS


def send_attempt_is_authoritative(state: ActionExecutionState) -> bool:
    """True when the recorded execution state forbids another attempt regardless of TTL."""

    return state in SEND_ATTEMPT_AUTHORITATIVE_STATES


class IdempotencyStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED_FINAL = "FAILED_FINAL"


class IdempotencyPartitionKind(StrEnum):
    """Contextual partition that owns an idempotency record.

    ``VIEW_CURRENT`` is the case-scoped *Shareable* partition, and it exists because the
    compiler cannot write anywhere else in that table. The frozen trust matrix restricts
    compiler Shareable writes by ``dynamodb:LeadingKeys`` to the two view prefixes, so a
    compile record in ``NS#n#CASE#k`` would be a row the principal that must write it is
    denied. It is still the contextual case partition the persistence document names -- for
    a compile, the case partition of the Shareable table *is* the view-current one.
    """

    NAMESPACE = "NAMESPACE"
    COMMUNITY = "COMMUNITY"
    CASE = "CASE"
    VIEW_CURRENT = "VIEW_CURRENT"
    ACTION = "ACTION"


@dataclass(frozen=True, slots=True, kw_only=True)
class IdempotencyPartition:
    """Explicit contextual partition; the caller never guesses a partition."""

    kind: IdempotencyPartitionKind
    namespace: Namespace
    community_id: CommunityId | None = None
    case_id: CaseId | None = None
    action_id: ActionId | None = None

    def __post_init__(self) -> None:
        required = {
            IdempotencyPartitionKind.NAMESPACE: (),
            IdempotencyPartitionKind.COMMUNITY: ("community_id",),
            IdempotencyPartitionKind.CASE: ("case_id",),
            IdempotencyPartitionKind.VIEW_CURRENT: ("case_id",),
            IdempotencyPartitionKind.ACTION: ("action_id",),
        }[self.kind]
        for name in ("community_id", "case_id", "action_id"):
            present = getattr(self, name) is not None
            if present != (name in required):
                raise ValueError("idempotency partition fields do not match its kind")


@dataclass(frozen=True, slots=True, kw_only=True)
class IdempotencyKey:
    """Command identity: namespace, command type, actor, and the client-supplied key."""

    partition: IdempotencyPartition
    command: IdempotentCommand
    actor_id_hash: Sha256Digest
    key_hash: Sha256Digest


@dataclass(frozen=True, slots=True, kw_only=True)
class EntityRef:
    """Opaque reference to a durable result entity; never a value or private string."""

    entity_type: str
    entity_id: UUID
    version: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.entity_type) <= 64:
            raise ValueError("entity_type length is invalid")
        if self.version is not None and self.version < 1:
            raise ValueError("entity version must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class IdempotencyRecord:
    """The authoritative durable record for one command key."""

    key: IdempotencyKey
    request_hash: Sha256Digest
    status: IdempotencyStatus
    result_entity_refs: tuple[EntityRef, ...]
    response_status: int | None
    created_at: datetime
    updated_at: datetime
    expires_at_epoch: int
    version: int
    schema_version: str = "idempotency-record/v1"

    def __post_init__(self) -> None:
        require_utc(self.created_at)
        require_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        if self.version < 1:
            raise ValueError("version must be positive")
        if self.expires_at_epoch < 0:
            raise ValueError("expires_at_epoch cannot be negative")
        if self.response_status is not None and not 100 <= self.response_status <= 599:
            raise ValueError("response_status is not an HTTP status code")
        refs = tuple((ref.entity_type, ref.entity_id) for ref in self.result_entity_refs)
        if len(set(refs)) != len(refs):
            raise ValueError("result entity references must be unique")
        if self.status is IdempotencyStatus.IN_PROGRESS and self.result_entity_refs:
            raise ValueError("an in-progress record cannot carry result references")


@dataclass(frozen=True, slots=True, kw_only=True)
class IdempotencyStarted:
    """This caller created the record and owns the command attempt."""

    record: IdempotencyRecord


@dataclass(frozen=True, slots=True, kw_only=True)
class IdempotencyReplay:
    """A previous attempt under the same key and request hash already completed."""

    record: IdempotencyRecord


@dataclass(frozen=True, slots=True, kw_only=True)
class IdempotencyInProgress:
    """A concurrent attempt under the same key and request hash is still running."""

    record: IdempotencyRecord


@dataclass(frozen=True, slots=True, kw_only=True)
class IdempotencyFailedFinal:
    """A previous attempt failed terminally; the command is not retryable under this key."""

    record: IdempotencyRecord


type IdempotencyOutcome = (
    IdempotencyStarted | IdempotencyReplay | IdempotencyInProgress | IdempotencyFailedFinal
)
