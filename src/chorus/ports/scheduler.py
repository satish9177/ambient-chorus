"""The deadline scheduler boundary: two methods, four derived values, and no way to reschedule.

``DeadlineSchedulerPort`` has **no ``delete_schedule`` and no ``update_schedule``**
([ADR-028](../../../docs/adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) SS 1).
``ActionAfterCompletion=DELETE`` handles cleanup, and V1 has no reschedule verb, so a method
to change a deadline would be a method with no legitimate caller and one obvious illegitimate
one. The application's IAM grant is narrowed to match -- ``scheduler:CreateSchedule`` and
``scheduler:GetSchedule`` and nothing else -- because a grant wider than its caller is a grant
waiting for a second caller.

Every field of :class:`DueScheduleRequest` is **derived** from the commitment row and none is
passed in by a caller. The schedule name and the client token are deterministic functions of
``{commitment_id, generation}``, which is what makes a retry after a lost create response a
repeat of the same request rather than a second differently named schedule.

:class:`CommitmentDueEvent` is the payload, and it is **not signed and not trusted**. It grants
the watcher exactly one power: naming which commitment to load. Every other field is
re-verified against the strongly loaded row before anything moves, which is strictly stronger
than verifying a signature over values the row already holds (ADR-028 SS 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from chorus.domain.ids import CaseId, CommitmentId, Namespace
from chorus.domain.time import require_utc

COMMITMENT_DUE_EVENT_SCHEMA = "commitment-due/v1"
"""The frozen payload identity, exactly as 07-action-ses-and-commitments.md prints it."""

MAX_SCHEDULE_NAME_LENGTH = 64
"""EventBridge Scheduler's own bound on a schedule name, asserted before the call."""


class ScheduleFailureCode(StrEnum):
    """Why a schedule was not created. Closed codes; never a provider message.

    ``SCHEDULER_UNKNOWN`` is the default for anything unenumerated, for the same reason
    :class:`chorus.ports.sender.SesUnknown` is: the safe side has to be the default rather
    than the remembered case. A commitment whose schedule outcome is unknown stays visibly
    ``PENDING_SCHEDULE`` and is retried under the same name and the same client token.
    """

    SCHEDULER_REJECTED = "SCHEDULER_REJECTED"
    SCHEDULER_UNAVAILABLE = "SCHEDULER_UNAVAILABLE"
    SCHEDULER_UNKNOWN = "SCHEDULER_UNKNOWN"


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitmentDueEvent:
    """The frozen one-time payload the schedule carries to the watcher.

    It names a commitment and restates what the row should say about it. The watcher trusts
    the name and re-verifies everything else, so a forged or replayed event can at most cause
    a strongly consistent read that changes nothing.
    """

    schema_version: str = COMMITMENT_DUE_EVENT_SCHEMA
    event_id: UUID
    namespace: Namespace
    case_id: CaseId
    commitment_id: CommitmentId
    expected_generation: int
    logical_due_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != COMMITMENT_DUE_EVENT_SCHEMA:
            raise ValueError("unsupported commitment due event schema")
        if self.expected_generation < 1:
            raise ValueError("expected_generation must be positive")
        require_utc(self.logical_due_at)

    def as_payload(self) -> dict[str, Any]:
        """The canonical JSON-safe body the scheduler adapter transmits."""

        from chorus.domain.time import format_utc

        return {
            "schema_version": self.schema_version,
            "event_id": str(self.event_id),
            "namespace": self.namespace.value,
            "case_id": str(self.case_id),
            "commitment_id": str(self.commitment_id),
            "expected_generation": self.expected_generation,
            "logical_due_at": format_utc(self.logical_due_at),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DueScheduleRequest:
    """``{schedule_name, client_token, at_utc, payload}`` and nothing else.

    All four are derived by :mod:`chorus.application.services.commitment_schedule` from the
    commitment row. A caller that could name its own schedule would be a caller that could
    move a deadline.
    """

    schedule_name: str
    client_token: UUID
    at_utc: datetime
    payload: CommitmentDueEvent

    def __post_init__(self) -> None:
        if not 1 <= len(self.schedule_name) <= MAX_SCHEDULE_NAME_LENGTH:
            raise ValueError("schedule name length is invalid")
        require_utc(self.at_utc)


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleDescription:
    """What ``describe_schedule`` returns: the identity of a schedule, never its target role."""

    schedule_name: str
    at_utc: datetime
    event_id: UUID

    def __post_init__(self) -> None:
        require_utc(self.at_utc)


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleCreated:
    """The schedule now exists under exactly this name."""

    schedule_name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleAlreadyExists:
    """A schedule of this name is already there, which under a derived name is a replay."""

    schedule_name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleCreateFailed:
    """No schedule was created, under one closed code and no provider text."""

    reason_code: ScheduleFailureCode = ScheduleFailureCode.SCHEDULER_UNKNOWN


type ScheduleOutcome = ScheduleCreated | ScheduleAlreadyExists | ScheduleCreateFailed


class DeadlineSchedulerPort(Protocol):
    """Create one deterministic one-time schedule, and describe one by exact name."""

    async def create_due_schedule(self, request: DueScheduleRequest) -> ScheduleOutcome:
        """Create the schedule once, and classify what happened.

        Implementations **must not raise for a transport condition**: an unrecognised failure
        is :class:`ScheduleCreateFailed` with ``SCHEDULER_UNKNOWN``, because the safe side has
        to be the default. They must never invent a second name: a retry uses the same name
        and the same client token, and a lost response is reconciled by
        :meth:`describe_schedule` on that exact name.
        """

    async def describe_schedule(self, name: str) -> ScheduleDescription | None:
        """Describe the schedule at this exact name, or ``None`` when there is none."""


__all__ = [
    "COMMITMENT_DUE_EVENT_SCHEMA",
    "MAX_SCHEDULE_NAME_LENGTH",
    "CommitmentDueEvent",
    "DeadlineSchedulerPort",
    "DueScheduleRequest",
    "ScheduleAlreadyExists",
    "ScheduleCreateFailed",
    "ScheduleCreated",
    "ScheduleDescription",
    "ScheduleFailureCode",
    "ScheduleOutcome",
]
