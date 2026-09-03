"""The Phase 3 observability events, emitted from the real application paths.

Every event here is a fact about a *decision*: a message was accepted, a candidate was
detected, a link was denied, an agent answered, a key replayed. None of them describes the
content the decision was about, and none of them can: the emitters below accept identifiers,
digests, versions, counts, and closed reason codes, and there is no parameter through which a
message, a summary, a quotation, a prompt, a completion, or an exception representation could
be passed even by mistake.

That is the point of putting them in one module rather than scattering ``logger.info`` calls
through the use cases. A log line is written by whoever holds the private value, so the safe
thing is to make the only available call sites incapable of taking one. The formatter in
``chorus.infrastructure.observability.logging`` is the second gate and drops anything not on
its allowlist; this module is the first, and it is the one that decides what an event *means*.

Reason codes are closed enum values -- ``AgentRejection``, ``MonitorApplyDenial``, a
persistence code -- so a denial can be counted, alarmed on, and explained to an operator
without the offending identifier, quotation, or field ever being written down.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final
from uuid import UUID

from chorus.domain.ids import CaseId, CommunityId, Namespace, OperationId, Sha256Digest

LOGGER_NAME: Final = "chorus.application"

_logger = logging.getLogger(LOGGER_NAME)

SERVICE_API: Final = "chorus-api"
SERVICE_WORKER: Final = "worker"
SERVICE: Final = SERVICE_API
"""The service names the frozen observability table permits for Phase 3.

``service`` has to name the process that actually emitted the record. These emitters are
called from both sides of the asynchronous handover -- an HTTP request and an operation
worker -- and labelling a worker's agent invocation ``chorus-api`` makes "which process
invoked the model" unanswerable from the logs, which is the one question the agent events
exist to answer.
"""

_service: ContextVar[str] = ContextVar("chorus_observability_service", default=SERVICE_API)
"""Which process the current task is emitting as.

A context variable rather than a constructor argument, because the emitters are module
functions that use cases call directly and threading a service name through every call site
would put a formatting concern into the signature of every decision this module records. It
is task-local, so a worker running as a background task cannot relabel the request that
started it.
"""


@contextmanager
def emitting_as(service: str) -> Iterator[None]:
    """Attribute every event raised inside this block to ``service``."""

    token = _service.set(service)
    try:
        yield
    finally:
        _service.reset(token)


class EventName:
    """The stable dotted names the frozen observability table requires of Phase 3."""

    MESSAGE_ACCEPTED: Final = "message.accepted"
    MESSAGE_REPLAYED: Final = "message.replayed"
    MESSAGE_CONFLICT: Final = "message.conflict"

    CANDIDATE_DETECTED: Final = "candidate.detected"
    REPORT_LINKED: Final = "report.linked"
    REPORT_LINK_DENIED: Final = "report.link.denied"

    AGENT_INVOCATION_STARTED: Final = "agent.invocation.started"
    AGENT_INVOCATION_COMPLETED: Final = "agent.invocation.completed"
    AGENT_INVOCATION_FAILED: Final = "agent.invocation.failed"
    AGENT_CONTRACT_DENIED: Final = "agent.contract.denied"

    IDEMPOTENCY_REPLAY: Final = "idempotency.replay"
    IDEMPOTENCY_CONFLICT: Final = "idempotency.conflict"
    LAMBDA_REPLAY: Final = "lambda.replay"

    WORKER_JOB_MISMATCH: Final = "worker.job.mismatch"
    OPERATION_RESUME_SCHEDULED: Final = "operation.resume.scheduled"
    OPERATION_RESUMED: Final = "operation.resumed"
    MONITOR_BATCH_NOOP: Final = "monitor.batch.noop"

    PROMPT_INJECTION_OBSERVED: Final = "prompt_injection.observed"


def _emit(
    event_name: str,
    *,
    level: int = logging.INFO,
    namespace: Namespace | None = None,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
    community_id: CommunityId | None = None,
    case_id: CaseId | None = None,
    case_version: int | None = None,
    operation_id: OperationId | None = None,
    invocation_id: UUID | None = None,
    actor_id_hash: Sha256Digest | None = None,
    input_hash: Sha256Digest | None = None,
    output_hash: Sha256Digest | None = None,
    prompt_version: str | None = None,
    outcome: str | None = None,
    reason_codes: tuple[str, ...] = (),
    counts: Mapping[str, int] | None = None,
    attempt: int | None = None,
    retryable: bool | None = None,
) -> None:
    """Write one allowlisted record.

    Absent fields are omitted rather than written as ``None``: a formatter that renders every
    declared field would make the shape of an event depend on which optional values happened
    to be available, and an operator reading two events of the same name should see the same
    keys mean the same things.
    """

    extra: dict[str, object] = {"service": _service.get(), "event_name": event_name}
    optional: dict[str, object | None] = {
        "namespace": None if namespace is None else namespace.value,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "community_id": community_id,
        "case_id": case_id,
        "case_version": case_version,
        "operation_id": operation_id,
        "invocation_id": invocation_id,
        "actor_id_hash": None if actor_id_hash is None else actor_id_hash.value,
        "input_hash": None if input_hash is None else input_hash.value,
        "output_hash": None if output_hash is None else output_hash.value,
        "prompt_version": prompt_version,
        "outcome": outcome,
        "attempt": attempt,
        "retryable": retryable,
    }
    for name, value in optional.items():
        if value is not None:
            extra[name] = value
    if reason_codes:
        extra["reason_codes"] = list(reason_codes)
    if counts is not None:
        extra["counts"] = dict(counts)
    _logger.log(level, event_name, extra=extra)


def message_ingested(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    correlation_id: UUID | None,
    actor_id_hash: Sha256Digest,
    accepted: int,
    replayed: int,
) -> None:
    """Record how one ingestion batch resolved, in counts only.

    Accepted and replayed are separate events rather than one event with a flag, because an
    operator watching a redelivery storm is asking a different question from one watching
    ingestion volume, and a metric filtered on a flag is harder to alarm on than a name.
    """

    if accepted:
        _emit(
            EventName.MESSAGE_ACCEPTED,
            namespace=namespace,
            community_id=community_id,
            correlation_id=correlation_id,
            actor_id_hash=actor_id_hash,
            outcome="SUCCEEDED",
            counts={"messages": accepted},
        )
    if replayed:
        _emit(
            EventName.MESSAGE_REPLAYED,
            namespace=namespace,
            community_id=community_id,
            correlation_id=correlation_id,
            actor_id_hash=actor_id_hash,
            outcome="REPLAYED",
            counts={"messages": replayed},
        )


def message_conflict(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    correlation_id: UUID | None,
    actor_id_hash: Sha256Digest,
    reason_code: str,
) -> None:
    """Record that one channel identifier was re-ingested with different content."""

    _emit(
        EventName.MESSAGE_CONFLICT,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        correlation_id=correlation_id,
        actor_id_hash=actor_id_hash,
        outcome="FAILED",
        reason_codes=(reason_code,),
    )


def agent_invocation_started(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    operation_id: OperationId,
    invocation_id: UUID,
    correlation_id: UUID,
    input_hash: Sha256Digest,
    prompt_version: str,
    attempt: int,
    message_count: int,
    candidate_summary_count: int,
) -> None:
    """Record that a model is about to be asked, and exactly what it was asked about."""

    _emit(
        EventName.AGENT_INVOCATION_STARTED,
        namespace=namespace,
        community_id=community_id,
        operation_id=operation_id,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        input_hash=input_hash,
        prompt_version=prompt_version,
        attempt=attempt,
        counts={
            "messages": message_count,
            "candidate_summaries": candidate_summary_count,
        },
    )


def agent_invocation_completed(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    operation_id: OperationId,
    invocation_id: UUID,
    correlation_id: UUID,
    input_hash: Sha256Digest,
    output_hash: Sha256Digest,
    prompt_version: str,
    outcome: str,
    counts: Mapping[str, int],
) -> None:
    """Record that an answer arrived and what deterministic code made of it."""

    _emit(
        EventName.AGENT_INVOCATION_COMPLETED,
        namespace=namespace,
        community_id=community_id,
        operation_id=operation_id,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        input_hash=input_hash,
        output_hash=output_hash,
        prompt_version=prompt_version,
        outcome=outcome,
        counts=counts,
    )


def agent_invocation_failed(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    operation_id: OperationId,
    invocation_id: UUID,
    correlation_id: UUID,
    input_hash: Sha256Digest,
    prompt_version: str,
    reason_codes: tuple[str, ...],
    retryable: bool,
) -> None:
    """Record that an invocation produced nothing durable, and why.

    ``reason_codes`` are closed enum values. The exception that carried them is never
    formatted: a provider error message can quote the payload that provoked it.
    """

    _emit(
        EventName.AGENT_INVOCATION_FAILED,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        operation_id=operation_id,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        input_hash=input_hash,
        prompt_version=prompt_version,
        outcome="FAILED",
        reason_codes=reason_codes,
        retryable=retryable,
    )


def agent_contract_denied(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    operation_id: OperationId,
    invocation_id: UUID,
    correlation_id: UUID,
    input_hash: Sha256Digest,
    reason_codes: tuple[str, ...],
) -> None:
    """Record that a well-formed answer failed deterministic validation and was refused."""

    _emit(
        EventName.AGENT_CONTRACT_DENIED,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        operation_id=operation_id,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        input_hash=input_hash,
        outcome="DENIED",
        reason_codes=reason_codes,
    )


def candidate_detected(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    case_version: int,
    correlation_id: UUID,
    invocation_id: UUID,
    report_count: int,
    fact_count: int,
) -> None:
    """Record that a new candidate case became durable."""

    _emit(
        EventName.CANDIDATE_DETECTED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        case_version=case_version,
        correlation_id=correlation_id,
        invocation_id=invocation_id,
        outcome="SUCCEEDED",
        counts={"reports": report_count, "facts": fact_count},
    )


def report_linked(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    case_version: int,
    correlation_id: UUID,
    invocation_id: UUID,
    report_count: int,
    fact_count: int,
) -> None:
    """Record that reports were appended to an existing case."""

    _emit(
        EventName.REPORT_LINKED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        case_version=case_version,
        correlation_id=correlation_id,
        invocation_id=invocation_id,
        outcome="SUCCEEDED",
        counts={"reports": report_count, "facts": fact_count},
    )


def report_link_denied(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    correlation_id: UUID,
    invocation_id: UUID,
    reason_code: str,
    case_id: CaseId | None = None,
) -> None:
    """Record that an apply gate refused a linkage and wrote nothing."""

    _emit(
        EventName.REPORT_LINK_DENIED,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        invocation_id=invocation_id,
        outcome="DENIED",
        reason_codes=(reason_code,),
    )


def prompt_injection_observed(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    correlation_id: UUID,
    invocation_id: UUID,
    observed_count: int,
) -> None:
    """Record that messages read as addressed to a system rather than to neighbours.

    Only the count. Writing the attempt down would copy the attack into the log group, and
    the classification changes nothing anyway: such a message stays ordinary untrusted data,
    and the runtime it was aimed at has no tool it could have invoked.
    """

    if not observed_count:
        return
    _emit(
        EventName.PROMPT_INJECTION_OBSERVED,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        correlation_id=correlation_id,
        invocation_id=invocation_id,
        outcome="DENIED",
        counts={"messages": observed_count},
    )


def idempotency_replay(
    *,
    namespace: Namespace,
    community_id: CommunityId | None,
    correlation_id: UUID | None,
    actor_id_hash: Sha256Digest,
    operation_id: OperationId | None = None,
    input_hash: Sha256Digest | None = None,
) -> None:
    """Record that a command key returned a previously recorded outcome."""

    _emit(
        EventName.IDEMPOTENCY_REPLAY,
        namespace=namespace,
        community_id=community_id,
        correlation_id=correlation_id,
        actor_id_hash=actor_id_hash,
        operation_id=operation_id,
        input_hash=input_hash,
        outcome="REPLAYED",
    )


def idempotency_conflict(
    *,
    namespace: Namespace,
    community_id: CommunityId | None,
    correlation_id: UUID | None,
    actor_id_hash: Sha256Digest,
    operation_id: OperationId | None = None,
) -> None:
    """Record that one key was reused for a materially different request."""

    _emit(
        EventName.IDEMPOTENCY_CONFLICT,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        correlation_id=correlation_id,
        actor_id_hash=actor_id_hash,
        operation_id=operation_id,
        outcome="FAILED",
        reason_codes=("IDEMPOTENCY_CONFLICT",),
    )


__all__ = [
    "LOGGER_NAME",
    "EventName",
    "agent_contract_denied",
    "agent_invocation_completed",
    "agent_invocation_failed",
    "agent_invocation_started",
    "candidate_detected",
    "idempotency_conflict",
    "idempotency_replay",
    "message_conflict",
    "message_ingested",
    "prompt_injection_observed",
    "report_link_denied",
    "report_linked",
]


def lambda_replay(
    *,
    namespace: Namespace,
    community_id: CommunityId | None,
    operation_id: OperationId,
    invocation_id: UUID | None,
    correlation_id: UUID | None,
    outcome: str,
) -> None:
    """One asynchronous delivery arrived for work that is already accounted for.

    At-least-once delivery is the contract, so a repeat is normal rather than alarming. It is
    still recorded, because "the worker ran twice and the model ran once" is the property the
    claim exists to provide and an operator has to be able to see it holding.
    """

    _emit(
        EventName.LAMBDA_REPLAY,
        namespace=namespace,
        community_id=community_id,
        operation_id=operation_id,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        outcome=outcome,
    )


def worker_job_mismatch(
    *,
    namespace: Namespace,
    operation_id: OperationId,
    invocation_id: UUID | None,
    correlation_id: UUID | None,
    reason_codes: tuple[str, ...],
) -> None:
    """A job was handed to a worker the durable operation does not agree it belongs to."""

    _emit(
        EventName.WORKER_JOB_MISMATCH,
        level=logging.WARNING,
        namespace=namespace,
        operation_id=operation_id,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        reason_codes=reason_codes,
    )


def operation_resume_scheduled(
    *,
    namespace: Namespace,
    community_id: CommunityId | None,
    operation_id: OperationId,
    invocation_id: UUID | None,
    correlation_id: UUID | None,
    reason_code: str,
) -> None:
    """A frozen apply plan was interrupted, and the operation is eligible to resume."""

    _emit(
        EventName.OPERATION_RESUME_SCHEDULED,
        namespace=namespace,
        community_id=community_id,
        operation_id=operation_id,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        reason_codes=(reason_code,),
    )


def operation_resumed(
    *,
    namespace: Namespace,
    community_id: CommunityId | None,
    operation_id: OperationId,
    invocation_id: UUID | None,
    correlation_id: UUID | None,
    completed_steps: int,
    total_steps: int,
) -> None:
    """A redelivery picked a frozen plan back up, and called no model to do it."""

    _emit(
        EventName.OPERATION_RESUMED,
        namespace=namespace,
        community_id=community_id,
        operation_id=operation_id,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        counts={"completed_steps": completed_steps, "total_steps": total_steps},
    )


def monitor_batch_noop(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    operation_id: OperationId,
    invocation_id: UUID | None,
    correlation_id: UUID | None,
    reason_code: str,
) -> None:
    """The frozen batch held nothing to reason about, so no model was invoked."""

    _emit(
        EventName.MONITOR_BATCH_NOOP,
        namespace=namespace,
        community_id=community_id,
        operation_id=operation_id,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        outcome="NOOP",
        reason_codes=(reason_code,),
    )
