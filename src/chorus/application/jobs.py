"""The internal asynchronous work boundary, written down as one versioned wire contract.

``POST`` a route that starts an agent-invoking operation and the request path returns ``202``
having written a durable operation and handed one **job** to the worker. Deployed, that handover
is an ``InvocationType="Event"`` Lambda invocation, and this module is the only thing that
decides what crosses it.

What crosses, and what deliberately does not
---------------------------------------------
Identifiers, versions, digests, and instants. **No message text, no mandate, no projected
payload, no agent output, and no request object** -- the jobs in :mod:`chorus.ports.operations`
are already shaped that way ("it names the work precisely enough that the worker can reload
everything it needs from the durable state that already exists"), and this contract is a
one-for-one serialization of them rather than a second, looser shape beside them.

There is no pickle, no dynamic class lookup, and no "decode whatever is in the envelope". The
envelope names a schema version and a kind; both are checked against closed sets, and an unknown
value fails closed rather than being guessed from the payload's shape.

Why the encoder is here and not in the adapter
-----------------------------------------------
Four of the five job types live in ``chorus.ports.operations`` and the fifth beside its command
in the application layer, and infrastructure may import neither. So the codec lives here -- pure
functions over frozen dataclasses, no SDK, no framework -- and the Lambda adapter below it deals
only in ``{"operation": ..., "payload": ...}``.

Versioning
-----------
``worker-job/v1``. It is checked on the way in, so a worker of one version handed a job of
another refuses it instead of reading fields that have moved.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from chorus.application.commands.extract_commitment_operation import ExtractCommitmentJob
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CaseId,
    CommunityId,
    EvidenceItemId,
    ExecutionId,
    MessageId,
    Namespace,
    OperationId,
    Sha256Digest,
    ViewId,
)
from chorus.domain.time import format_utc, parse_utc
from chorus.ports.operations import (
    InvestigationOperationJob,
    MonitorOperationJob,
    ProposeActionOperationJob,
    SendActionOperationJob,
)
from chorus.ports.records import MessageFeedEntry

WORKER_JOB_SCHEMA: Final = "worker-job/v1"
WORKER_JOB_SCHEMA_VERSIONS: Final = frozenset({WORKER_JOB_SCHEMA})

MAX_MESSAGE_LOCATORS: Final = 500
"""A bound on the one repeated field, so a job stays a bounded value rather than a payload.

A Monitor batch names the messages just ingested; the ingest route already bounds a batch, so
this is a second, independent refusal at the boundary a redelivery crosses.
"""


class WorkerJobKind(StrEnum):
    """The closed set of work the operation worker will accept. There is no default branch."""

    MONITOR = "MONITOR"
    INVESTIGATE = "INVESTIGATE"
    PROPOSE_ACTION = "PROPOSE_ACTION"
    EXTRACT_COMMITMENT = "EXTRACT_COMMITMENT"
    SEND_ACTION = "SEND_ACTION"


class WorkerJobError(ValueError):
    """A delivered event is not a job this worker can run, and no side effect has happened.

    Raised before anything is loaded, claimed, or written. A malformed handover must fail where
    it is parsed, because every later place it could fail is a place something has already
    moved.
    """


type OperationJob = (
    MonitorOperationJob
    | InvestigationOperationJob
    | ProposeActionOperationJob
    | ExtractCommitmentJob
    | SendActionOperationJob
)
"""The five, and there is no sixth. A worker asked for anything else fails closed."""


def _digest(value: str) -> Sha256Digest:
    try:
        return Sha256Digest(value)
    except (TypeError, ValueError) as error:
        raise WorkerJobError("a job field is not a canonical digest") from error


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise WorkerJobError("a job identifier is not a string")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise WorkerJobError("a job identifier is not a UUID") from error
    if str(parsed) != value:
        raise WorkerJobError("a job identifier is not canonical")
    return parsed


def _text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise WorkerJobError(f"a job is missing {name}")
    return value


def _number(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkerJobError(f"a job is missing {name}")
    return value


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise WorkerJobError("a job instant is not a string")
    try:
        return parse_utc(value)
    except ValueError as error:
        raise WorkerJobError("a job instant is not canonical UTC") from error


def _namespace(payload: dict[str, object]) -> Namespace:
    try:
        return Namespace(_text(payload, "namespace"))
    except ValueError as error:
        raise WorkerJobError("a job names no valid namespace") from error


def _identity(payload: dict[str, object], name: str) -> UUID:
    return _uuid(payload.get(name))


def encode_locators(locators: tuple[MessageFeedEntry, ...]) -> list[dict[str, str]]:
    return [
        {"message_id": str(entry.message_id), "sent_at": format_utc(entry.sent_at)}
        for entry in locators
    ]


def decode_locators(raw: object) -> tuple[MessageFeedEntry, ...]:
    if not isinstance(raw, list) or not raw:
        raise WorkerJobError("a Monitor job names at least one message")
    if len(raw) > MAX_MESSAGE_LOCATORS:
        raise WorkerJobError("a Monitor job names too many messages")
    entries: list[MessageFeedEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            raise WorkerJobError("a Monitor job locator is not an object")
        entries.append(
            MessageFeedEntry(
                message_id=MessageId(_uuid(item.get("message_id"))),
                sent_at=_instant(item.get("sent_at")),
            )
        )
    return tuple(entries)


def encode_job(job: object) -> dict[str, object]:
    """Serialize one job into its exact wire shape, or refuse to.

    Field for field and nothing derived. A job whose type is not one of the five is refused
    here rather than being encoded generically, because a generic encoder is how a payload that
    is not a job ends up on a queue that will be trusted to name work.
    """

    match job:
        case MonitorOperationJob():
            body: dict[str, object] = {
                "operation_id": str(job.operation_id),
                "namespace": job.namespace.value,
                "community_id": str(job.community_id),
                "invocation_id": str(job.invocation_id),
                "correlation_id": str(job.correlation_id),
                "actor_id_hash": job.actor_id_hash.value,
                "request_hash": job.request_hash.value,
                "message_locators": encode_locators(job.message_locators),
            }
            kind = WorkerJobKind.MONITOR
        case InvestigationOperationJob():
            body = {
                "operation_id": str(job.operation_id),
                "namespace": job.namespace.value,
                "community_id": str(job.community_id),
                "case_id": str(job.case_id),
                "invocation_id": str(job.invocation_id),
                "correlation_id": str(job.correlation_id),
                "actor_id_hash": job.actor_id_hash.value,
                "request_hash": job.request_hash.value,
                "expected_case_version": job.expected_case_version,
                "reason": job.reason,
                "idempotency_key": job.idempotency_key,
            }
            kind = WorkerJobKind.INVESTIGATE
        case ProposeActionOperationJob():
            body = {
                "operation_id": str(job.operation_id),
                "namespace": job.namespace.value,
                "community_id": str(job.community_id),
                "case_id": str(job.case_id),
                "invocation_id": str(job.invocation_id),
                "correlation_id": str(job.correlation_id),
                "actor_id_hash": job.actor_id_hash.value,
                "request_hash": job.request_hash.value,
                "expected_case_version": job.expected_case_version,
                "view_id": str(job.view_id),
                "view_hash": job.view_hash.value,
                "idempotency_key": job.idempotency_key,
            }
            kind = WorkerJobKind.PROPOSE_ACTION
        case ExtractCommitmentJob():
            body = {
                "operation_id": str(job.operation_id),
                "namespace": job.namespace.value,
                "community_id": str(job.community_id),
                "case_id": str(job.case_id),
                "action_id": str(job.action_id),
                "evidence_id": str(job.evidence_id),
                "invocation_id": str(job.invocation_id),
                "correlation_id": str(job.correlation_id),
                "actor_id_hash": job.actor_id_hash.value,
                "request_hash": job.request_hash.value,
                "evidence_sha256": job.evidence_sha256.value,
            }
            kind = WorkerJobKind.EXTRACT_COMMITMENT
        case SendActionOperationJob():
            body = {
                "operation_id": str(job.operation_id),
                "namespace": job.namespace.value,
                "community_id": str(job.community_id),
                "case_id": str(job.case_id),
                "action_id": str(job.action_id),
                "execution_id": str(job.execution_id),
                "approval_id": str(job.approval_id),
                "correlation_id": str(job.correlation_id),
                "actor_id_hash": job.actor_id_hash.value,
                "request_hash": job.request_hash.value,
                "expected_execution_version": job.expected_execution_version,
                "idempotency_key": job.idempotency_key,
            }
            kind = WorkerJobKind.SEND_ACTION
        case _:
            raise WorkerJobError("that is not an operation job")
    return {"schema": WORKER_JOB_SCHEMA, "kind": kind.value, "job": body}


def decode_job(payload: object) -> tuple[WorkerJobKind, OperationJob]:
    """Parse one delivered event into its exact job, or refuse before anything happens.

    The kind is read from the envelope's **declared** field and from nowhere else. A worker that
    inferred the kind from which fields were present would be a worker steerable by adding one.
    """

    if not isinstance(payload, dict):
        raise WorkerJobError("a worker event is not an object")
    schema = payload.get("schema")
    if schema not in WORKER_JOB_SCHEMA_VERSIONS:
        raise WorkerJobError("a worker event names an unknown schema version")
    raw_kind = payload.get("kind")
    if not isinstance(raw_kind, str):
        raise WorkerJobError("a worker event names no kind")
    try:
        kind = WorkerJobKind(raw_kind)
    except ValueError as error:
        raise WorkerJobError("a worker event names an unknown kind") from error
    body = payload.get("job")
    if not isinstance(body, dict):
        raise WorkerJobError("a worker event carries no job")
    return kind, _decode_body(kind, body)


def _decode_body(kind: WorkerJobKind, body: dict[str, object]) -> OperationJob:
    try:
        match kind:
            case WorkerJobKind.MONITOR:
                return MonitorOperationJob(
                    operation_id=OperationId(_identity(body, "operation_id")),
                    namespace=_namespace(body),
                    community_id=CommunityId(_identity(body, "community_id")),
                    invocation_id=_identity(body, "invocation_id"),
                    correlation_id=_identity(body, "correlation_id"),
                    actor_id_hash=_digest(_text(body, "actor_id_hash")),
                    request_hash=_digest(_text(body, "request_hash")),
                    message_locators=decode_locators(body.get("message_locators")),
                )
            case WorkerJobKind.INVESTIGATE:
                return InvestigationOperationJob(
                    operation_id=OperationId(_identity(body, "operation_id")),
                    namespace=_namespace(body),
                    community_id=CommunityId(_identity(body, "community_id")),
                    case_id=CaseId(_identity(body, "case_id")),
                    invocation_id=_identity(body, "invocation_id"),
                    correlation_id=_identity(body, "correlation_id"),
                    actor_id_hash=_digest(_text(body, "actor_id_hash")),
                    request_hash=_digest(_text(body, "request_hash")),
                    expected_case_version=_number(body, "expected_case_version"),
                    reason=_text(body, "reason"),
                    idempotency_key=_text(body, "idempotency_key"),
                )
            case WorkerJobKind.PROPOSE_ACTION:
                return ProposeActionOperationJob(
                    operation_id=OperationId(_identity(body, "operation_id")),
                    namespace=_namespace(body),
                    community_id=CommunityId(_identity(body, "community_id")),
                    case_id=CaseId(_identity(body, "case_id")),
                    invocation_id=_identity(body, "invocation_id"),
                    correlation_id=_identity(body, "correlation_id"),
                    actor_id_hash=_digest(_text(body, "actor_id_hash")),
                    request_hash=_digest(_text(body, "request_hash")),
                    expected_case_version=_number(body, "expected_case_version"),
                    view_id=ViewId(_identity(body, "view_id")),
                    view_hash=_digest(_text(body, "view_hash")),
                    idempotency_key=_text(body, "idempotency_key"),
                )
            case WorkerJobKind.EXTRACT_COMMITMENT:
                return ExtractCommitmentJob(
                    operation_id=OperationId(_identity(body, "operation_id")),
                    namespace=_namespace(body),
                    community_id=CommunityId(_identity(body, "community_id")),
                    case_id=CaseId(_identity(body, "case_id")),
                    action_id=ActionId(_identity(body, "action_id")),
                    evidence_id=EvidenceItemId(_identity(body, "evidence_id")),
                    invocation_id=_identity(body, "invocation_id"),
                    correlation_id=_identity(body, "correlation_id"),
                    actor_id_hash=_digest(_text(body, "actor_id_hash")),
                    request_hash=_digest(_text(body, "request_hash")),
                    evidence_sha256=_digest(_text(body, "evidence_sha256")),
                )
            case WorkerJobKind.SEND_ACTION:
                return SendActionOperationJob(
                    operation_id=OperationId(_identity(body, "operation_id")),
                    namespace=_namespace(body),
                    community_id=CommunityId(_identity(body, "community_id")),
                    case_id=CaseId(_identity(body, "case_id")),
                    action_id=ActionId(_identity(body, "action_id")),
                    execution_id=ExecutionId(_identity(body, "execution_id")),
                    approval_id=ApprovalId(_identity(body, "approval_id")),
                    correlation_id=_identity(body, "correlation_id"),
                    actor_id_hash=_digest(_text(body, "actor_id_hash")),
                    request_hash=_digest(_text(body, "request_hash")),
                    expected_execution_version=_number(body, "expected_execution_version"),
                    idempotency_key=_text(body, "idempotency_key"),
                )
    except (TypeError, ValueError) as error:
        if isinstance(error, WorkerJobError):
            raise
        # A job whose own constructor invariant refuses it -- an empty locator list, a
        # non-positive version -- is a malformed handover, not an internal error.
        raise WorkerJobError("a worker event is not a well-formed job") from error
    raise WorkerJobError("a worker event names an unknown kind")  # pragma: no cover - closed set


__all__ = [
    "MAX_MESSAGE_LOCATORS",
    "WORKER_JOB_SCHEMA",
    "WORKER_JOB_SCHEMA_VERSIONS",
    "OperationJob",
    "WorkerJobError",
    "WorkerJobKind",
    "decode_job",
    "decode_locators",
    "encode_job",
    "encode_locators",
]
