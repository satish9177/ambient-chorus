"""The boto3 EventBridge Scheduler adapter: one one-time schedule, classified rather than raised.

The schedule configuration is frozen and none of it is a parameter
([ADR-028](../../../../docs/adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) § 1, restating
07-action-ses-and-commitments.md unchanged): a one-time ``at(...)`` expression in UTC, flexible
time window ``OFF``, ``ActionAfterCompletion=DELETE``, maximum event age one hour, maximum retry
attempts three, and an encrypted standard SQS dead-letter queue. A caller that could choose any
of them could change what "the deadline fired" means.

Outcomes are **classified, never raised**, exactly as the SES sender's are. A definite rejection
and an ambiguous transport failure produce very different obligations, and an adapter that let
an exception escape would push that classification into the command, where it would be done a
second time. ``SCHEDULER_UNKNOWN`` is what this adapter returns when it does not recognise what
happened, rather than what somebody remembered to add.

``ConflictException`` -- a schedule already at that name -- is **not** a failure. Under a derived
name it is a replay of the same request, which is the whole reason the name is derived.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError

from chorus.domain.time import format_utc
from chorus.infrastructure.scheduler.client import SchedulerClient
from chorus.ports.scheduler import (
    DueScheduleRequest,
    ScheduleAlreadyExists,
    ScheduleCreated,
    ScheduleCreateFailed,
    ScheduleDescription,
    ScheduleFailureCode,
    ScheduleOutcome,
)

FLEXIBLE_WINDOW: Final = {"Mode": "OFF"}
"""No jitter. A deadline this system asked for is the instant it asked for."""

ACTION_AFTER_COMPLETION: Final = "DELETE"
"""How a fired one-time schedule is cleaned up, and why the port needs no delete method."""

MAXIMUM_EVENT_AGE_SECONDS: Final = 3600
MAXIMUM_RETRY_ATTEMPTS: Final = 3
"""Frozen delivery bounds. Beyond them the invocation lands in the DLQ, which alarms."""

SCHEDULE_TIMEZONE: Final = "UTC"

_CONFLICT_CODES = frozenset({"ConflictException"})
_DEFINITE_CODES = frozenset(
    {
        "ValidationException",
        "AccessDeniedException",
        "ResourceNotFoundException",
        "ServiceQuotaExceededException",
    }
)
_ABSENT_CODES = frozenset({"ResourceNotFoundException"})


def _error_code(error: ClientError) -> str:
    response = getattr(error, "response", {})
    return str(response.get("Error", {}).get("Code", ""))


@dataclass(slots=True)
class EventBridgeDeadlineScheduler:
    """Create one deterministic one-time schedule, and describe one by exact name."""

    client: SchedulerClient
    group_name: str
    target_arn: str
    role_arn: str
    dead_letter_arn: str | None = None

    async def create_due_schedule(self, request: DueScheduleRequest) -> ScheduleOutcome:
        target: dict[str, Any] = {
            "Arn": self.target_arn,
            "RoleArn": self.role_arn,
            "Input": _json_payload(request),
            "RetryPolicy": {
                "MaximumEventAgeInSeconds": MAXIMUM_EVENT_AGE_SECONDS,
                "MaximumRetryAttempts": MAXIMUM_RETRY_ATTEMPTS,
            },
        }
        if self.dead_letter_arn:
            target["DeadLetterConfig"] = {"Arn": self.dead_letter_arn}
        try:
            self.client.create_schedule(
                Name=request.schedule_name,
                GroupName=self.group_name,
                ScheduleExpression=_at_expression(request),
                ScheduleExpressionTimezone=SCHEDULE_TIMEZONE,
                FlexibleTimeWindow=FLEXIBLE_WINDOW,
                ActionAfterCompletion=ACTION_AFTER_COMPLETION,
                ClientToken=str(request.client_token),
                Target=target,
            )
        except ClientError as error:
            code = _error_code(error)
            if code in _CONFLICT_CODES:
                # Under a derived name this is the same request arriving twice, which is what
                # the derivation exists to make safe.
                return ScheduleAlreadyExists(schedule_name=request.schedule_name)
            if code in _DEFINITE_CODES:
                return ScheduleCreateFailed(reason_code=ScheduleFailureCode.SCHEDULER_REJECTED)
            return ScheduleCreateFailed(reason_code=ScheduleFailureCode.SCHEDULER_UNAVAILABLE)
        except BotoCoreError:
            # A connection or timeout failure. The request may or may not have reached the
            # service, so the safe answer is the unknown one and the caller reconciles by name.
            return ScheduleCreateFailed(reason_code=ScheduleFailureCode.SCHEDULER_UNKNOWN)
        except Exception:  # pragma: no cover - the port forbids raising for a transport
            return ScheduleCreateFailed(reason_code=ScheduleFailureCode.SCHEDULER_UNKNOWN)
        return ScheduleCreated(schedule_name=request.schedule_name)

    async def describe_schedule(self, name: str) -> ScheduleDescription | None:
        try:
            response = self.client.get_schedule(Name=name, GroupName=self.group_name)
        except ClientError as error:
            if _error_code(error) in _ABSENT_CODES:
                return None
            return None
        except BotoCoreError:
            # Unknown, and ``None`` here would be read as "no schedule exists", which would
            # invite a second create. The caller treats an unresolved describe as unresolved.
            raise
        expression = str(response.get("ScheduleExpression", ""))
        target = response.get("Target") or {}
        payload = _decode_payload(target.get("Input"))
        event_id = payload.get("event_id")
        return ScheduleDescription(
            schedule_name=str(response.get("Name", name)),
            at_utc=_parse_at_expression(expression),
            event_id=UUID(str(event_id)) if event_id else UUID(int=0),
        )


def _at_expression(request: DueScheduleRequest) -> str:
    """``at(YYYY-MM-DDTHH:MM:SS)`` -- the one-time form, seconds precision, no offset.

    EventBridge Scheduler's ``at()`` takes no fractional seconds and no offset; the timezone is
    stated separately and is always ``UTC`` here, because every instant this system schedules is
    already UTC and there is nothing to convert.
    """

    return f"at({format_utc(request.at_utc)[:19]})"


def _parse_at_expression(expression: str) -> Any:
    from datetime import UTC, datetime

    inner = expression.removeprefix("at(").removesuffix(")")
    return datetime.fromisoformat(inner).replace(tzinfo=UTC)


def _json_payload(request: DueScheduleRequest) -> str:
    import json

    return json.dumps(request.payload.as_payload(), sort_keys=True, separators=(",", ":"))


def _decode_payload(value: object) -> dict[str, Any]:
    import json

    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


__all__ = [
    "ACTION_AFTER_COMPLETION",
    "FLEXIBLE_WINDOW",
    "MAXIMUM_EVENT_AGE_SECONDS",
    "MAXIMUM_RETRY_ATTEMPTS",
    "SCHEDULE_TIMEZONE",
    "EventBridgeDeadlineScheduler",
]
