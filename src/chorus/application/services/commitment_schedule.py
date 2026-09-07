"""Every derived value the deadline path uses, derived in exactly one place.

``scheduler_name``, ``schedule_generation``, and ``due_event_id`` are **derived at creation**,
not attached afterwards (ADR-028 § 4). All three are deterministic functions of
``commitment_id`` and ``generation = 1``, both known before any AWS call, so the entity's
non-optional fields are satisfiable by the commitment
transaction itself and 06-persistence-and-evidence.md's "attach scheduler name/generation" is
corrected to describe what actually varies.

What varies is whether the schedule *exists*, and that is the ``COMMITMENT_SCHEDULE#c``
projection -- an operational value and never a ``CommitmentStatus``. A commitment whose schedule
failed is genuinely ``PENDING``: what failed is the alarm clock, not the promise.

There is no schedule-ARN field anywhere. The name is deterministic, so an ARN would be a second
copy of a derivable value that can disagree with the first.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid5

from chorus.domain.ids import CaseId, CommitmentId, Namespace
from chorus.domain.time import require_utc
from chorus.ports.scheduler import (
    MAX_SCHEDULE_NAME_LENGTH,
    CommitmentDueEvent,
    DueScheduleRequest,
)

SCHEDULE_TOKEN_NAMESPACE = UUID("2b0f6a2e-6a1b-5a52-9f6f-1f1a2c4d6e80")
DUE_EVENT_NAMESPACE = UUID("9c3d5f11-4b28-5c73-8a10-7d5e2f9b4c31")
"""Two fixed UUIDv5 namespaces, distinct so one derivation can never produce the other.

They are constants in source rather than configuration for the reason every derived identity in
this system is: a value an operator could change is a value that makes yesterday's schedule name
and today's disagree about the same commitment.
"""

NAMESPACE_HASH_LENGTH = 8
"""How much of the namespace digest goes into a schedule name.

Eight hex characters, because the name also carries a full UUID and EventBridge Scheduler bounds
a name at 64 characters. It is a *disambiguator between deployments*, not a security boundary:
what actually prevents a cross-namespace schedule is that the watcher re-verifies the namespace
against the strongly loaded commitment row.
"""

MAX_SCHEDULE_ENVIRONMENT_LENGTH = 8
"""How long the ``{env}`` segment of the frozen schedule name may be, and why there is a bound.

The frozen format is ``chorus-{env}-{namespace_hash8}-{commitment_id}-{generation}``. Its fixed
parts -- ``chorus``, three hyphens, eight hex characters, a 36-character UUID, and a one-digit
generation -- already spend 56 of the 64 characters EventBridge Scheduler allows in a schedule
name, which leaves exactly eight for the environment word. ``demo`` and ``test`` fit;
``development`` does not, by two characters.

So ``{env}`` is a short deployment token of its own (``CHORUS_SCHEDULER_ENVIRONMENT``) rather
than the full environment name. It is deployment configuration, it changes no format, and it is
asserted here rather than discovered at the first live ``CreateSchedule`` -- which is the point
of deriving the name in one place at all.
"""

DEMO_MINIMUM_DELAY = timedelta(minutes=10)
"""The floor in the demo clock mapping: ``actual_now + max(10 minutes, logical_due - logical_now)``.

A **real** one-time schedule is created -- the demo does not fake the resource -- and the later
real invocation is a harmless replay under watcher step 3, because the logical clock has by then
long passed ``due_at`` and the commitment is no longer ``PENDING`` (ADR-028 § 5).
"""


def namespace_hash8(namespace: Namespace) -> str:
    """The first eight hex characters of the namespace digest."""

    return sha256(namespace.value.encode("utf-8")).hexdigest()[:NAMESPACE_HASH_LENGTH]


def schedule_name(
    *, environment: str, namespace: Namespace, commitment_id: CommitmentId, generation: int
) -> str:
    """``chorus-{env}-{namespace_hash8}-{commitment_id}-{generation}``.

    Deterministic, so a retry after a lost create response uses the *same* name, and a lost
    response is reconciled by ``GetSchedule`` on that exact name rather than by creating a second
    differently named schedule.
    """

    if generation < 1:
        raise ValueError("schedule generation must be positive")
    if not environment:
        raise ValueError("a schedule name requires the deployment environment")
    name = f"chorus-{environment}-{namespace_hash8(namespace)}-{commitment_id}-{generation}"
    if len(name) > MAX_SCHEDULE_NAME_LENGTH:
        # Asserted before the call rather than discovered at the first live create. The two
        # variable parts are an environment word and a decimal generation, so exceeding the
        # bound means a deployment chose a name longer than the derivation leaves room for.
        raise ValueError("derived schedule name exceeds the scheduler's own bound")
    return name


def schedule_client_token(*, commitment_id: CommitmentId, generation: int) -> UUID:
    """``uuidv5(SCHEDULE_TOKEN_NAMESPACE, commitment_id | generation)``."""

    if generation < 1:
        raise ValueError("schedule generation must be positive")
    return uuid5(SCHEDULE_TOKEN_NAMESPACE, f"{commitment_id}|{generation}")


def due_event_id(*, commitment_id: CommitmentId, generation: int) -> UUID:
    """``uuidv5(DUE_EVENT_NAMESPACE, commitment_id | generation)``.

    Written onto the commitment row at creation and re-derived by nobody at firing time: the
    watcher compares the delivered event's identifier with the **stored** one, which is what
    makes a stale generation a success no-op rather than a transition.
    """

    if generation < 1:
        raise ValueError("schedule generation must be positive")
    return uuid5(DUE_EVENT_NAMESPACE, f"{commitment_id}|{generation}")


def due_event(
    *,
    namespace: Namespace,
    case_id: CaseId,
    commitment_id: CommitmentId,
    generation: int,
    due_at: datetime,
) -> CommitmentDueEvent:
    """The frozen ``commitment-due/v1`` payload for one commitment generation."""

    require_utc(due_at)
    return CommitmentDueEvent(
        event_id=due_event_id(commitment_id=commitment_id, generation=generation),
        namespace=namespace,
        case_id=case_id,
        commitment_id=commitment_id,
        expected_generation=generation,
        logical_due_at=due_at,
    )


def demo_schedule_instant(
    *, actual_now: datetime, logical_now: datetime, logical_due_at: datetime
) -> datetime:
    """``actual_now + max(10 minutes, logical_due - logical_now)``, unchanged from Phase 7.

    Both values are audited by the caller. The mapping exists so a presenter's demo does not
    depend on a precise firing window while a **real** schedule is still created.
    """

    require_utc(actual_now)
    require_utc(logical_now)
    require_utc(logical_due_at)
    return actual_now + max(DEMO_MINIMUM_DELAY, logical_due_at - logical_now)


def due_schedule_request(
    *,
    environment: str,
    namespace: Namespace,
    case_id: CaseId,
    commitment_id: CommitmentId,
    generation: int,
    due_at: datetime,
    at_utc: datetime,
) -> DueScheduleRequest:
    """Assemble the whole request from derived values. Nothing here is passed in by a caller.

    ``at_utc`` is the only argument that is not a pure function of the commitment, because it is
    the demo mapping's output or ``due_at`` itself -- which deployment decides, and this module
    does not.
    """

    return DueScheduleRequest(
        schedule_name=schedule_name(
            environment=environment,
            namespace=namespace,
            commitment_id=commitment_id,
            generation=generation,
        ),
        client_token=schedule_client_token(commitment_id=commitment_id, generation=generation),
        at_utc=at_utc,
        payload=due_event(
            namespace=namespace,
            case_id=case_id,
            commitment_id=commitment_id,
            generation=generation,
            due_at=due_at,
        ),
    )


__all__ = [
    "DEMO_MINIMUM_DELAY",
    "DUE_EVENT_NAMESPACE",
    "NAMESPACE_HASH_LENGTH",
    "SCHEDULE_TOKEN_NAMESPACE",
    "demo_schedule_instant",
    "due_event",
    "due_event_id",
    "due_schedule_request",
    "namespace_hash8",
    "schedule_client_token",
    "schedule_name",
]
