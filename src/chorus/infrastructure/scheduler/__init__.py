"""The EventBridge Scheduler boundary: a narrow client and one adapter over it."""

from chorus.infrastructure.scheduler.client import SchedulerClient, create_scheduler_client
from chorus.infrastructure.scheduler.eventbridge import (
    ACTION_AFTER_COMPLETION,
    FLEXIBLE_WINDOW,
    MAXIMUM_EVENT_AGE_SECONDS,
    MAXIMUM_RETRY_ATTEMPTS,
    EventBridgeDeadlineScheduler,
)

__all__ = [
    "ACTION_AFTER_COMPLETION",
    "FLEXIBLE_WINDOW",
    "MAXIMUM_EVENT_AGE_SECONDS",
    "MAXIMUM_RETRY_ATTEMPTS",
    "EventBridgeDeadlineScheduler",
    "SchedulerClient",
    "create_scheduler_client",
]
