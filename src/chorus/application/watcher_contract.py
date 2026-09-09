"""The commitment watcher's invocation contract, and the remote half of the demo-clock route.

The watcher is reached from **two** places -- an EventBridge Scheduler one-time delivery, and
``POST /v1/demo/clock/advance``, which invokes it synchronously and returns its outcome
([ADR-028](../../../docs/adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) SS 5). Both must
reach the **same** watcher application logic; a second implementation for the demo path is
exactly the defect ADR-028 SS 5 exists to prevent, because the early-firing comparison would
then mean two different things on the two paths.

So there is one wire contract, ``commitment-watcher-request/v1``, and both paths speak it. It
carries the frozen ``commitment-due/v1`` event plus the two values that are *deployment context
rather than event fields* -- the community and the invoking actor -- and the trigger token that
is recorded as an audit field and never as an authority.

Nothing here decides anything
------------------------------
:class:`RemoteRecordCommitmentDue` serializes a command, invokes, and parses one of a closed set
of outcomes. The due check, its order, the re-verification of every restated field, and the
single compare-and-swap all live in
:mod:`chorus.application.commands.record_commitment_due` and run inside the watcher. A branch
here on a status, a generation, or a clock reading would be a second implementation of the
authority -- the same argument
:class:`chorus.infrastructure.compiler.send_authorization.CompilerSendAuthorization` makes for
the fence.

It also never falls back. An invocation that fails, an answer it cannot parse, or an outcome
naming nothing in :class:`~chorus.application.commands.record_commitment_due.WatcherOutcome`
raises, because "the watcher could not be reached" and "the watcher changed nothing" are
different facts and a presenter shown the second when the first happened has been told a
commitment is fine when nobody looked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID

from chorus.application.commands.record_commitment_due import (
    TRIGGER_DEMO_CLOCK,
    TRIGGER_SCHEDULE,
    RecordCommitmentDueCommand,
    RecordCommitmentDueResult,
    WatcherOutcome,
)
from chorus.domain.entities import CommitmentStatus
from chorus.domain.ids import CaseId, CommitmentId, CommunityId, Namespace, Sha256Digest
from chorus.domain.time import parse_utc
from chorus.ports.errors import ExternalDependencyError, PersistenceErrorCode
from chorus.ports.invocation import SynchronousInvokerPort
from chorus.ports.scheduler import COMMITMENT_DUE_EVENT_SCHEMA, CommitmentDueEvent

WATCHER_OPERATION: Final = "RecordCommitmentDue"
WATCHER_REQUEST_SCHEMA: Final = "commitment-watcher-request/v1"
WATCHER_RESULT_SCHEMA: Final = "commitment-watcher-result/v1"

ACCEPTED_TRIGGERS: Final = frozenset({TRIGGER_SCHEDULE, TRIGGER_DEMO_CLOCK})
"""Which path invoked the watcher, and there are exactly two. An unknown token is refused
rather than recorded: the trigger becomes an audit reason code, and an audit trail that can be
told an arbitrary string about who woke it is not a record of anything."""


class CommitmentWatcher(Protocol):
    """Run one delivered due event to its outcome, wherever the watcher happens to live.

    Declared so a caller -- the demo-clock route -- can hold *the watcher* rather than one of
    the two ways of reaching it. The in-process
    :class:`~chorus.application.commands.record_commitment_due.RecordCommitmentDue` and the
    remote :class:`RemoteRecordCommitmentDue` both satisfy it, and no caller branches on which.
    """

    async def execute(self, command: RecordCommitmentDueCommand) -> RecordCommitmentDueResult:
        """Return what the watcher decided. Every outcome but ``DUE`` changed nothing."""


class WatcherRequestError(ValueError):
    """A delivered watcher event is not a request this watcher can run.

    Raised during parsing, before the commitment is loaded, so a malformed event changes
    nothing and leaves nothing to undo.
    """


def _unusable() -> ExternalDependencyError:
    return ExternalDependencyError(
        WATCHER_OPERATION, code=PersistenceErrorCode.DEPENDENCY_REJECTED, retryable=False
    )


def encode_watcher_request(command: RecordCommitmentDueCommand) -> dict[str, object]:
    """The frozen wire shape, field for field.

    Every value is an identifier, a generation, a digest, or an instant. No commitment text, no
    case title, and no evidence crosses this boundary, because none of them is an input to the
    question being asked.
    """

    event = command.event
    return {
        "schema": WATCHER_REQUEST_SCHEMA,
        "event": event.as_payload(),
        "community_id": str(command.community_id),
        "actor_id_hash": command.actor_id_hash.value,
        "correlation_id": str(command.correlation_id),
        "trigger": command.trigger,
    }


def _require_text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise WatcherRequestError(f"a watcher request is missing {name}")
    return value


def _require_uuid(payload: dict[str, object], name: str) -> UUID:
    raw = _require_text(payload, name)
    try:
        parsed = UUID(raw)
    except ValueError as error:
        raise WatcherRequestError(f"{name} is not a UUID") from error
    if str(parsed) != raw:
        raise WatcherRequestError(f"{name} is not canonical")
    return parsed


def decode_watcher_request(payload: object) -> RecordCommitmentDueCommand:
    """Parse one delivered event into the exact command, or refuse before anything happens.

    The event's own schema token is checked here as well as by ``CommitmentDueEvent``, so an
    event of a future shape is refused at the boundary rather than partially read.
    """

    if not isinstance(payload, dict):
        raise WatcherRequestError("a watcher request is not an object")
    if payload.get("schema") != WATCHER_REQUEST_SCHEMA:
        raise WatcherRequestError("a watcher request names an unknown schema version")
    raw_event = payload.get("event")
    if not isinstance(raw_event, dict):
        raise WatcherRequestError("a watcher request carries no due event")
    if raw_event.get("schema_version") != COMMITMENT_DUE_EVENT_SCHEMA:
        raise WatcherRequestError("a due event names an unknown schema version")
    generation = raw_event.get("expected_generation")
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise WatcherRequestError("a due event names no generation")
    trigger = _require_text(payload, "trigger")
    if trigger not in ACCEPTED_TRIGGERS:
        raise WatcherRequestError("a watcher request names an unknown trigger")
    try:
        event = CommitmentDueEvent(
            event_id=_require_uuid(raw_event, "event_id"),
            namespace=Namespace(_require_text(raw_event, "namespace")),
            case_id=CaseId(_require_uuid(raw_event, "case_id")),
            commitment_id=CommitmentId(_require_uuid(raw_event, "commitment_id")),
            expected_generation=generation,
            logical_due_at=parse_utc(_require_text(raw_event, "logical_due_at")),
        )
        return RecordCommitmentDueCommand(
            event=event,
            community_id=CommunityId(_require_uuid(payload, "community_id")),
            actor_id_hash=Sha256Digest(_require_text(payload, "actor_id_hash")),
            correlation_id=_require_uuid(payload, "correlation_id"),
            trigger=trigger,
        )
    except WatcherRequestError:
        raise
    except (TypeError, ValueError) as error:
        raise WatcherRequestError("a watcher request is not well formed") from error


def encode_watcher_result(result: RecordCommitmentDueResult) -> dict[str, object]:
    """The small, closed answer a watcher invocation returns.

    Three fields and no diagnostics: the outcome, and the commitment's status and version where
    one was loaded. No reason text, no traceback, no row.
    """

    return {
        "schema": WATCHER_RESULT_SCHEMA,
        "outcome": result.outcome.value,
        "commitment_status": (
            None if result.commitment_status is None else result.commitment_status.value
        ),
        "commitment_version": result.commitment_version,
    }


def decode_watcher_result(body: dict[str, object]) -> RecordCommitmentDueResult:
    """Parse the watcher's answer, or raise. It never invents an outcome."""

    if body.get("schema") != WATCHER_RESULT_SCHEMA:
        raise _unusable()
    raw_outcome = body.get("outcome")
    if not isinstance(raw_outcome, str):
        raise _unusable()
    try:
        outcome = WatcherOutcome(raw_outcome)
    except ValueError as error:
        raise _unusable() from error
    raw_status = body.get("commitment_status")
    status: CommitmentStatus | None
    if raw_status is None:
        status = None
    elif isinstance(raw_status, str):
        try:
            status = CommitmentStatus(raw_status)
        except ValueError as error:
            raise _unusable() from error
    else:
        raise _unusable()
    version = body.get("commitment_version")
    if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
        raise _unusable()
    return RecordCommitmentDueResult(
        outcome=outcome, commitment_status=status, commitment_version=version
    )


@dataclass(frozen=True, slots=True)
class RemoteRecordCommitmentDue:
    """The demo-clock route's whole relationship with the watcher: one synchronous invocation.

    It satisfies the same call shape the in-process
    :class:`~chorus.application.commands.record_commitment_due.RecordCommitmentDue` does, so the
    route holds one thing and never branches on which deployment it is in. What differs is
    everything the deployed API is not permitted to hold: no Shareable case write path, no unit
    of work, no audit repository -- only ``lambda:InvokeFunction`` on the watcher's ``live``
    alias.
    """

    invoker: SynchronousInvokerPort

    async def execute(self, command: RecordCommitmentDueCommand) -> RecordCommitmentDueResult:
        body = await self.invoker.invoke(
            operation=WATCHER_OPERATION, payload=encode_watcher_request(command)
        )
        return decode_watcher_result(body)


__all__ = [
    "ACCEPTED_TRIGGERS",
    "WATCHER_OPERATION",
    "WATCHER_REQUEST_SCHEMA",
    "WATCHER_RESULT_SCHEMA",
    "CommitmentWatcher",
    "RemoteRecordCommitmentDue",
    "WatcherRequestError",
    "decode_watcher_request",
    "decode_watcher_result",
    "encode_watcher_request",
    "encode_watcher_result",
]
