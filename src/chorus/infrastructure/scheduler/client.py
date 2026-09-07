"""Narrow EventBridge Scheduler client protocol and its single boto3 construction point.

The protocol names exactly two calls: ``create_schedule`` and ``get_schedule``. There is no
``update_schedule``, no ``delete_schedule``, and no ``list_schedules`` member, so no adapter can
reach one by mistake and a static scan of this package finds no access path to them
([ADR-028](../../../../docs/adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) § 1). The
IAM grant is narrowed to match, which is the point: a grant wider than its caller is a grant
waiting for a second caller.

SDK retrying is switched off for the same reason it is on the DynamoDB and S3 clients. A retry
performed inside botocore hands the caller one exception describing the last attempt, so an
earlier attempt that reached the service -- and may have created the schedule -- disappears from
the record the resolution logic sees. Here the resolution is ``get_schedule`` on the exact
derived name, and it can only be right if this process knows how many requests it made.
"""

from __future__ import annotations

from typing import Any, Final, Protocol, TypedDict, cast

from botocore.config import Config

SCHEDULER_SERVICE_NAME: Final = "scheduler"

SINGLE_ATTEMPT_RETRIES: Final = {"mode": "standard", "total_max_attempts": 1}
"""One adapter operation is exactly one request attempt. ``1`` counts the initial request."""


class GetScheduleOutput(TypedDict, total=False):
    Name: str
    ScheduleExpression: str
    Target: dict[str, Any]


class SchedulerClient(Protocol):
    """The exact EventBridge Scheduler surface CHORUS is permitted to call."""

    def create_schedule(self, **kwargs: object) -> object: ...

    def get_schedule(self, **kwargs: object) -> GetScheduleOutput: ...


def create_scheduler_client(
    *, region_name: str, endpoint_url: str | None = None
) -> SchedulerClient:
    """Build the boto3 Scheduler client with SDK retrying switched off.

    boto3's generated clients carry no type information, so this function is the one place a
    cast is required; everything above it is statically typed against :class:`SchedulerClient`.
    The retry policy is passed explicitly so it wins over ``AWS_MAX_ATTEMPTS``,
    ``AWS_RETRY_MODE``, and the shared config file.
    """

    import boto3

    client: Any = boto3.client(
        SCHEDULER_SERVICE_NAME,
        region_name=region_name,
        endpoint_url=endpoint_url,
        config=Config(retries=SINGLE_ATTEMPT_RETRIES),
    )
    return cast(SchedulerClient, client)


__all__ = [
    "SCHEDULER_SERVICE_NAME",
    "SINGLE_ATTEMPT_RETRIES",
    "GetScheduleOutput",
    "SchedulerClient",
    "create_scheduler_client",
]
