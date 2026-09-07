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

from chorus.domain.ids import (
    CaseId,
    CommunityId,
    FactId,
    Namespace,
    OperationId,
    Sha256Digest,
)

LOGGER_NAME: Final = "chorus.application"

_logger = logging.getLogger(LOGGER_NAME)

SERVICE_API: Final = "chorus-api"
SERVICE_WORKER: Final = "worker"
SERVICE_SENDER: Final = "sender"
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

    MANDATE_REQUESTED: Final = "mandate.requested"
    MANDATE_DECIDED: Final = "mandate.decided"
    MANDATE_DENIED: Final = "mandate.denied"

    INVESTIGATION_APPLIED: Final = "investigation.applied"
    EVIDENCE_INDEPENDENCE_COMPUTED: Final = "evidence.independence.computed"
    CONTRADICTION_RECORDED: Final = "contradiction.recorded"
    EVIDENCE_STATUS_DOWNGRADED: Final = "evidence.status.downgraded"

    PROMPT_INJECTION_OBSERVED: Final = "prompt_injection.observed"

    COMPILE_STARTED: Final = "compile.started"
    COMPILE_ALLOWED: Final = "compile.allowed"
    COMPILE_DENIED: Final = "compile.denied"
    VIEW_PERSISTED: Final = "view.persisted"
    PRIVATE_URI_DENIED: Final = "private_uri.denied"

    PROPOSAL_REQUESTED: Final = "proposal.requested"
    PROPOSAL_VALIDATED: Final = "proposal.validated"
    PROPOSAL_DENIED: Final = "proposal.denied"
    PROPOSAL_STALE_REJECTED: Final = "proposal.stale_rejected"
    PROPOSAL_PERSISTED: Final = "proposal.persisted"
    PROPOSAL_REPLAYED: Final = "proposal.replayed"

    SEND_FENCE_ACQUIRED: Final = "send.fence.acquired"
    SEND_FENCE_DENIED: Final = "send.fence.denied"
    SEND_FENCE_RELEASED: Final = "send.fence.released"

    APPROVAL_RECORDED: Final = "approval.recorded"
    APPROVAL_CONFLICT: Final = "approval.conflict"

    EXECUTION_SENDING: Final = "execution.sending"
    EXECUTION_CLAIM_NOT_OWNED: Final = "execution.claim.not_owned"
    EXECUTION_SENT: Final = "execution.sent"
    EXECUTION_FAILED: Final = "execution.failed"
    EXECUTION_UNKNOWN: Final = "execution.unknown"
    EXECUTION_RECONCILED: Final = "execution.reconciled"

    REPLY_RECEIVED: Final = "reply.received"
    REPLY_REJECTED: Final = "reply.rejected"

    COMMITMENT_EXTRACTED: Final = "commitment.extracted"
    COMMITMENT_CREATED: Final = "commitment.created"
    COMMITMENT_REJECTED: Final = "commitment.rejected"
    COMMITMENT_DUE: Final = "commitment.due"
    COMMITMENT_REPLAYED: Final = "commitment.replayed"
    COMMITMENT_FULFILLED: Final = "commitment.fulfilled"
    COMMITMENT_MISSED: Final = "commitment.missed"

    SCHEDULE_CREATED: Final = "schedule.created"
    SCHEDULE_FAILED: Final = "schedule.failed"


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
    authorization_version: int | None = None,
    operation_id: OperationId | None = None,
    invocation_id: UUID | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    actor_id_hash: Sha256Digest | None = None,
    input_hash: Sha256Digest | None = None,
    output_hash: Sha256Digest | None = None,
    view_hash: Sha256Digest | None = None,
    proposal_hash: Sha256Digest | None = None,
    preview_hash: Sha256Digest | None = None,
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
        "authorization_version": authorization_version,
        "operation_id": operation_id,
        "invocation_id": invocation_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "actor_id_hash": None if actor_id_hash is None else actor_id_hash.value,
        "input_hash": None if input_hash is None else input_hash.value,
        "output_hash": None if output_hash is None else output_hash.value,
        "view_hash": None if view_hash is None else view_hash.value,
        "proposal_hash": None if proposal_hash is None else proposal_hash.value,
        "preview_hash": None if preview_hash is None else preview_hash.value,
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
    operation_id: OperationId | None = None,
    case_id: CaseId | None = None,
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
    operation_id: OperationId | None = None,
    case_id: CaseId | None = None,
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
    operation_id: OperationId | None = None,
    case_id: CaseId | None = None,
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


def investigation_applied(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    case_version: int,
    correlation_id: UUID,
    reason_code: str,
    status_counts: Mapping[str, int],
) -> None:
    """Record that one validated assessment was applied, and what it resolved to.

    ``status_counts`` is per-status and nothing else: how many facts ended ``REPORTED``,
    ``CORROBORATED``, ``CONTRADICTED``, ``UNKNOWN``, and -- always zero in policy/v1 --
    ``VERIFIED``. A non-zero ``VERIFIED`` count is a defect rather than a quality miss, which
    is exactly why it is counted rather than assumed.
    """

    _emit(
        EventName.INVESTIGATION_APPLIED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        case_version=case_version,
        correlation_id=correlation_id,
        outcome="SUCCEEDED",
        reason_codes=(reason_code,),
        counts=status_counts,
    )


def evidence_independence_computed(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID,
    independent_source_count: int,
) -> None:
    """Record the authoritative case-level count, so a corroboration claim is auditable."""

    _emit(
        EventName.EVIDENCE_INDEPENDENCE_COMPUTED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        counts={"independent_sources": independent_source_count},
    )


def contradiction_recorded(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID,
    materiality: str,
    cited_fact_count: int,
) -> None:
    """Record that a validated contradiction was accepted, at what cost and over how many facts.

    The description the model wrote is deliberately absent. It is private reasoning about
    private facts, it lives on the assessment row in the private zone, and an audit trail with
    a wider read audience is not where it belongs.
    """

    _emit(
        EventName.CONTRADICTION_RECORDED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        reason_codes=(materiality,),
        counts={"cited_facts": cited_fact_count},
    )


def evidence_status_downgraded(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID,
    fact_id: FactId,
    computed_status: str,
    proposed_status: str,
) -> None:
    """Record that a model proposed a status stronger than the computed one, and lost.

    Identifiers and codes only: the fact, the status deterministic code computed, and the
    status the model asked for. Never the rationale that argued for it -- SEC-21 is about which
    value won, and the argument is not evidence for anything a log needs to hold.
    """

    _emit(
        EventName.EVIDENCE_STATUS_DOWNGRADED,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        entity_type="FACT",
        entity_id=fact_id.value,
        outcome="DENIED",
        reason_codes=(f"COMPUTED_{computed_status}", f"PROPOSED_{proposed_status}"),
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


def mandate_requested(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    case_version: int,
    correlation_id: UUID | None,
    actor_id_hash: Sha256Digest,
    proposal_count: int,
    fact_count: int,
) -> None:
    """Record that a candidate was accepted and its mandate proposals became durable.

    Counts, never contents. How many contributors were asked and how many facts were described
    to them is an operational fact; which facts, and whose, is not.
    """

    _emit(
        EventName.MANDATE_REQUESTED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        case_version=case_version,
        correlation_id=correlation_id,
        actor_id_hash=actor_id_hash,
        outcome="SUCCEEDED",
        counts={"proposals": proposal_count, "facts": fact_count},
    )


def mandate_decided(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    case_version: int,
    correlation_id: UUID | None,
    actor_id_hash: Sha256Digest,
    decision: str,
    mandate_version: int,
    granted_fact_count: int,
    identity_shared: bool,
    replayed: bool = False,
) -> None:
    """Record one immutable authorization decision.

    ``decision`` is a closed enum value and ``identity_shared`` is the single bit that says
    whether identity permission was given -- the one fact about a mandate that has to be
    auditable separately from content, because the two authorizations are independent and a
    count of granted facts can never reveal which way identity went.
    """

    _emit(
        EventName.MANDATE_DECIDED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        case_version=case_version,
        correlation_id=correlation_id,
        actor_id_hash=actor_id_hash,
        outcome="REPLAYED" if replayed else "SUCCEEDED",
        reason_codes=(decision,),
        counts={
            "mandate_version": mandate_version,
            "granted_facts": granted_fact_count,
            "identity_shared": int(identity_shared),
        },
    )


def mandate_denied(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    actor_id_hash: Sha256Digest,
    reason_codes: tuple[str, ...],
) -> None:
    """Record that a mandate command was refused, and by which deterministic rules."""

    _emit(
        EventName.MANDATE_DENIED,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        actor_id_hash=actor_id_hash,
        outcome="DENIED",
        reason_codes=reason_codes,
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


def compile_started(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    case_version: int,
    correlation_id: UUID | None,
    actor_id_hash: Sha256Digest,
    requested_facts: int,
    requested_evidence: int,
) -> None:
    """Record that a compile was asked for, by whom, and how much it named.

    Counts, never contents. Which facts were requested is private lineage and lives in the
    compiler audit projection, where a reader has to be authorized to see a fact identifier at
    all; a log line that named them would put that lineage in a second place with weaker access
    control and a shorter memory.
    """

    _emit(
        EventName.COMPILE_STARTED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        case_version=case_version,
        correlation_id=correlation_id,
        actor_id_hash=actor_id_hash,
        counts={"requested_facts": requested_facts, "requested_evidence": requested_evidence},
    )


def compile_allowed(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    case_version: int,
    correlation_id: UUID | None,
    actor_id_hash: Sha256Digest,
    view_id: UUID,
    view_hash: Sha256Digest,
    included: int,
    excluded: int,
    safe_evidence: int,
    replayed: bool = False,
) -> None:
    """Record one persisted safe view by identifier and hash."""

    _emit(
        EventName.COMPILE_ALLOWED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        case_version=case_version,
        correlation_id=correlation_id,
        actor_id_hash=actor_id_hash,
        entity_type="SHAREABLE_VIEW",
        entity_id=view_id,
        view_hash=view_hash,
        outcome="REPLAYED" if replayed else "SUCCEEDED",
        counts={
            "included_facts": included,
            "excluded_facts": excluded,
            "safe_evidence_refs": safe_evidence,
        },
    )


def compile_denied(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    case_version: int,
    correlation_id: UUID | None,
    actor_id_hash: Sha256Digest,
    reason_codes: tuple[str, ...],
) -> None:
    """Record a deterministic refusal by its reason codes.

    ``INFO``, not ``WARNING``: a policy denial is the compiler working, and logging it as a
    problem would train an operator to treat the system's most important answer as noise.
    """

    _emit(
        EventName.COMPILE_DENIED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        case_version=case_version,
        correlation_id=correlation_id,
        actor_id_hash=actor_id_hash,
        outcome="DENIED",
        reason_codes=reason_codes,
    )


def proposal_requested(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    case_version: int,
    authorization_version: int,
    correlation_id: UUID | None,
    actor_id_hash: Sha256Digest,
    view_id: UUID,
    view_hash: Sha256Digest,
    fact_count: int,
) -> None:
    """Record that a proposal was asked for, against which exact view, and how large it was.

    ``authorization_version`` travels as a *count* rather than as prose, alongside the OCC
    version, because the two answer different questions and an operator reading a staleness
    incident needs to see which one moved.
    """

    _emit(
        EventName.PROPOSAL_REQUESTED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        case_version=case_version,
        correlation_id=correlation_id,
        actor_id_hash=actor_id_hash,
        authorization_version=authorization_version,
        entity_type="SHAREABLE_VIEW",
        entity_id=view_id,
        view_hash=view_hash,
        counts={"view_facts": fact_count},
    )


def proposal_validated(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    invocation_id: UUID,
    view_hash: Sha256Digest,
    claim_count: int,
    caveat_count: int,
    citation_count: int,
) -> None:
    """Record that one answer survived every deterministic check, in counts only.

    No claim text, no request text, no caveat text, no subject, and no rendered body. The
    counts are what an operator can act on; the prose is the thing the observability rules
    forbid a log line from carrying.
    """

    _emit(
        EventName.PROPOSAL_VALIDATED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        invocation_id=invocation_id,
        view_hash=view_hash,
        outcome="SUCCEEDED",
        counts={
            "claims": claim_count,
            "caveats": caveat_count,
            "citations": citation_count,
        },
    )


def proposal_denied(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    invocation_id: UUID,
    reason_codes: tuple[str, ...],
) -> None:
    """Record a whole-proposal refusal by its bounded ``ActionRejection`` codes.

    ``INFO`` for the same reason a compile denial is: a refused proposal is the validator
    working, and logging it as a problem would train an operator to treat the system's most
    important answer as noise.
    """

    _emit(
        EventName.PROPOSAL_DENIED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        invocation_id=invocation_id,
        outcome="DENIED",
        reason_codes=reason_codes,
    )


def proposal_stale_rejected(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    reason_codes: tuple[str, ...],
    invoked_model: bool,
) -> None:
    """Record a staleness refusal, and say whether it cost a model call.

    ``invoked_model`` is the field that distinguishes the two cases the failure matrix names
    separately: a stale-before-invocation refusal spends nothing, while a current-view pointer
    that moved *during* the invocation has already spent one pass and must not spend a second.
    """

    _emit(
        EventName.PROPOSAL_STALE_REJECTED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        outcome="DENIED",
        reason_codes=reason_codes,
        counts={"model_invocations": 1 if invoked_model else 0},
    )


def proposal_persisted(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    case_version: int,
    correlation_id: UUID | None,
    action_id: UUID,
    proposal_hash: Sha256Digest,
    preview_hash: Sha256Digest,
    participants: int,
) -> None:
    """Record one committed proposal by identifier and hashes.

    ``proposal_hash`` and ``preview_hash`` are named fields rather than borrowed
    ``input_hash``/``output_hash`` slots, because the frozen record vocabulary names them and an
    operator correlating an approval to a send needs the two digests to mean the same thing in
    every event that carries them. The bytes ``preview_hash`` covers are never persisted or
    logged anywhere -- only the digest travels.
    """

    _emit(
        EventName.PROPOSAL_PERSISTED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        case_version=case_version,
        correlation_id=correlation_id,
        entity_type="ACTION_PROPOSAL",
        entity_id=action_id,
        proposal_hash=proposal_hash,
        preview_hash=preview_hash,
        outcome="SUCCEEDED",
        counts={"transaction_participants": participants},
    )


def proposal_replayed(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    invocation_id: UUID,
) -> None:
    """Record that a redelivery answered from the durable record and called no model."""

    _emit(
        EventName.PROPOSAL_REPLAYED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        invocation_id=invocation_id,
        outcome="REPLAYED",
        counts={"model_invocations": 0},
    )


def send_fence_acquired(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    execution_id: UUID,
    replayed: bool = False,
) -> None:
    """Record that one execution holds the case's send authorization."""

    _emit(
        EventName.SEND_FENCE_ACQUIRED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        entity_type="SEND_FENCE",
        entity_id=execution_id,
        outcome="REPLAYED" if replayed else "SUCCEEDED",
    )


def send_fence_release_denied(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    execution_id: UUID,
) -> None:
    """Record a release refused because the caller is not the holder.

    ``WARNING``, because a process trying to clear somebody else's fence is either a stale
    attempt that should have given up or a bug, and both are worth seeing.
    """

    _emit(
        EventName.SEND_FENCE_DENIED,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        entity_type="SEND_FENCE",
        entity_id=execution_id,
        outcome="DENIED",
        reason_codes=("FENCE_NOT_HELD",),
    )


def send_fence_released(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    execution_id: UUID,
) -> None:
    """Record that the case's send authorization was returned by its holder."""

    _emit(
        EventName.SEND_FENCE_RELEASED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        entity_type="SEND_FENCE",
        entity_id=execution_id,
        outcome="SUCCEEDED",
    )


def approval_recorded(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    actor_id_hash: Sha256Digest,
    execution_id: UUID,
    decision: str,
    proposal_hash: Sha256Digest,
    view_hash: Sha256Digest,
    preview_hash: Sha256Digest,
) -> None:
    """Record one human decision, by hash and identifier only.

    The approver appears as ``actor_id_hash`` and can never be read as naming a person: the
    demo mechanism identifies that somebody holding the shared token asserted the approver
    persona, and a log line that implied more would be a claim the system cannot support.
    """

    _emit(
        EventName.APPROVAL_RECORDED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        actor_id_hash=actor_id_hash,
        entity_type="ACTION_EXECUTION",
        entity_id=execution_id,
        outcome=decision,
        proposal_hash=proposal_hash,
        view_hash=view_hash,
        preview_hash=preview_hash,
    )


def approval_conflict(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    actor_id_hash: Sha256Digest,
    reason_codes: tuple[str, ...],
) -> None:
    """Record a decision that lost the one-decision compare-and-swap, or arrived stale."""

    _emit(
        EventName.APPROVAL_CONFLICT,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        actor_id_hash=actor_id_hash,
        outcome="CONFLICT",
        reason_codes=reason_codes,
    )


def send_fence_denied(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    execution_id: UUID,
    reason_codes: tuple[str, ...],
) -> None:
    """Record that send authorization was refused before any SES call could be made.

    ``WARNING`` for the same reason a refused release is: an approved message that cannot be
    sent is a state a human is waiting on, and the reason codes are what tell them which
    repair -- recompile, re-propose, reapprove -- is the one they need.
    """

    _emit(
        EventName.SEND_FENCE_DENIED,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        entity_type="ACTION_EXECUTION",
        entity_id=execution_id,
        outcome="DENIED",
        reason_codes=reason_codes,
    )


def execution_sending(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    execution_id: UUID,
    proposal_hash: Sha256Digest,
    preview_hash: Sha256Digest,
) -> None:
    """Record the claim: this execution, and no other process, may now call SES once.

    ``preview_hash`` is emitted because the claim happens *after* the rendered digest was
    compared against it, so this line is the durable trace that the approved-equals-sent
    comparison passed before anything was consumed.
    """

    _emit(
        EventName.EXECUTION_SENDING,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        entity_type="ACTION_EXECUTION",
        entity_id=execution_id,
        outcome="CLAIMED",
        proposal_hash=proposal_hash,
        preview_hash=preview_hash,
    )


def execution_claim_not_owned(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    execution_id: UUID,
) -> None:
    """Record a delivery that lost an ambiguous claim and therefore made **no** SES call.

    Emitted at ``WARNING`` because it is rare and because it is the line an operator needs when
    two workers raced one approved message: it says, in one place, that a second sender read
    durable state, found the claim held by another attempt, and stopped.
    """

    _emit(
        EventName.EXECUTION_CLAIM_NOT_OWNED,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        entity_type="ACTION_EXECUTION",
        entity_id=execution_id,
        outcome="CLAIM_NOT_OWNED",
    )


def execution_sent(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    execution_id: UUID,
    preview_hash: Sha256Digest,
) -> None:
    """Record an accepted send. The SES message identifier is never logged beside the body.

    There is no body to log: neither rendered body is persisted anywhere, and the frozen
    observability table forbids the subject, either body, a claim, a caveat, a recipient
    address, or a reply-to address from reaching a log line.
    """

    _emit(
        EventName.EXECUTION_SENT,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        entity_type="ACTION_EXECUTION",
        entity_id=execution_id,
        outcome="SENT",
        preview_hash=preview_hash,
    )


def execution_failed(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    execution_id: UUID,
    reason_codes: tuple[str, ...],
) -> None:
    """Record a definite failure: proof exists that this message was not queued."""

    _emit(
        EventName.EXECUTION_FAILED,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        entity_type="ACTION_EXECUTION",
        entity_id=execution_id,
        outcome="FAILED",
        reason_codes=reason_codes,
        retryable=False,
    )


def execution_unknown(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    execution_id: UUID,
    reason_codes: tuple[str, ...],
) -> None:
    """Record an ambiguous outcome. ``ERROR``, because a human has to look at this one.

    It is the alarmed residual the whole phase is honest about: the message may have been
    delivered, and nothing will resend it. A quieter level would make the one state that
    needs a person look like the two that do not.
    """

    _emit(
        EventName.EXECUTION_UNKNOWN,
        level=logging.ERROR,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        entity_type="ACTION_EXECUTION",
        entity_id=execution_id,
        outcome="SEND_UNKNOWN",
        reason_codes=reason_codes,
        retryable=False,
    )


def execution_reconciled(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    execution_id: UUID,
    outcome: str,
    reason_codes: tuple[str, ...],
) -> None:
    """Record that durable classification moved on proof, without any SES call."""

    _emit(
        EventName.EXECUTION_RECONCILED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        entity_type="ACTION_EXECUTION",
        entity_id=execution_id,
        outcome=outcome,
        reason_codes=reason_codes,
    )


# ---------------------------------------------------------------------------------------
# Phase 9: the inbound reply, the commitment, and the deadline watcher
# ---------------------------------------------------------------------------------------


def reply_received(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    evidence_id: UUID,
    execution_id: UUID,
    inbound_message_id_hash: Sha256Digest,
) -> None:
    """Record that one authenticated, correlated reply became a private artifact.

    The frozen observability table admits the closed refusal code, the inbound message-ID
    **hash**, and the correlated execution -- and **never** the sender, the recipient, the
    subject, the body, or any part of the raw MIME. There is no parameter here through which
    one of those could be passed even by mistake.
    """

    _emit(
        EventName.REPLY_RECEIVED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        entity_type="EVIDENCE_ITEM",
        entity_id=evidence_id,
        input_hash=inbound_message_id_hash,
        output_hash=None,
        outcome="RECEIVED",
        causation_id=execution_id,
    )


def reply_rejected(
    *,
    namespace: Namespace,
    community_id: CommunityId | None,
    case_id: CaseId | None,
    correlation_id: UUID | None,
    reason_codes: tuple[str, ...],
    inbound_message_id_hash: Sha256Digest | None = None,
    execution_id: UUID | None = None,
) -> None:
    """Record one closed refusal. ``WARNING``, because somebody may be probing the boundary.

    ``case_id`` and ``execution_id`` are present only when correlation got that far; a delivery
    refused at the transport has no case to name, and inventing one would attribute a stranger's
    probe to a real case.
    """

    _emit(
        EventName.REPLY_REJECTED,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        entity_type="ACTION_EXECUTION" if execution_id is not None else None,
        entity_id=execution_id,
        input_hash=inbound_message_id_hash,
        outcome="REJECTED",
        reason_codes=reason_codes,
        retryable=False,
    )


def commitment_extracted(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    invocation_id: UUID,
    prompt_version: str,
    proposed: int,
) -> None:
    """Record that the extraction answered, and how many proposals it made. Never their text."""

    _emit(
        EventName.COMMITMENT_EXTRACTED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        invocation_id=invocation_id,
        prompt_version=prompt_version,
        counts={"proposed": proposed},
    )


def commitment_created(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    commitment_id: UUID,
    case_version: int,
) -> None:
    """Record one created commitment. No obligor label, no action text, no cited span."""

    _emit(
        EventName.COMMITMENT_CREATED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        case_version=case_version,
        correlation_id=correlation_id,
        entity_type="COMMITMENT",
        entity_id=commitment_id,
        outcome="PENDING",
    )


def commitment_rejected(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    reason_codes: tuple[str, ...],
    proposed: int,
) -> None:
    """Record an extraction that produced no valid commitment, with its per-proposal codes."""

    _emit(
        EventName.COMMITMENT_REJECTED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        outcome="REJECTED",
        reason_codes=reason_codes,
        counts={"proposed": proposed},
    )


def schedule_created(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    commitment_id: UUID,
    generation: int,
) -> None:
    """Record that the deterministic one-time schedule now exists under its derived name."""

    _emit(
        EventName.SCHEDULE_CREATED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        entity_type="COMMITMENT",
        entity_id=commitment_id,
        outcome="CREATED",
        counts={"generation": generation},
    )


def schedule_failed(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    commitment_id: UUID,
    attempts: int,
    reason_codes: tuple[str, ...],
) -> None:
    """Record that the alarm clock is not set. The promise is still ``PENDING``.

    ``WARNING`` rather than ``ERROR``: nothing is wrong with the commitment, and the case shows
    a visibly unscheduled banner. Retry uses the same name and the same client token.
    """

    _emit(
        EventName.SCHEDULE_FAILED,
        level=logging.WARNING,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        entity_type="COMMITMENT",
        entity_id=commitment_id,
        outcome="PENDING_SCHEDULE",
        reason_codes=reason_codes,
        counts={"attempts": attempts},
        retryable=True,
    )


def commitment_due(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    commitment_id: UUID,
    generation: int,
) -> None:
    """Record the watcher's one edge, and the verification request it created with it."""

    _emit(
        EventName.COMMITMENT_DUE,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        entity_type="COMMITMENT",
        entity_id=commitment_id,
        outcome="DUE",
        counts={"generation": generation},
    )


def commitment_replayed(
    *,
    namespace: Namespace,
    community_id: CommunityId | None,
    case_id: CaseId | None,
    correlation_id: UUID | None,
    commitment_id: UUID,
    reason_codes: tuple[str, ...],
    trigger: str,
) -> None:
    """Record a watcher invocation that changed nothing, and why.

    ``INFO``, because every branch it covers is an ordinary outcome: an early firing, a stale
    generation, an unknown commitment, and a duplicate delivery are all things a one-time
    schedule and an at-least-once transport produce in normal operation. ``trigger`` separates
    the real schedule from the demo clock.
    """

    _emit(
        EventName.COMMITMENT_REPLAYED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        correlation_id=correlation_id,
        entity_type="COMMITMENT",
        entity_id=commitment_id,
        outcome=trigger,
        reason_codes=reason_codes,
    )


def commitment_verified(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    correlation_id: UUID | None,
    commitment_id: UUID,
    actor_id_hash: Sha256Digest,
    fulfilled: bool,
    case_version: int,
) -> None:
    """Record the one human decision that can satisfy or miss a promise.

    The actor is a hash, and the note the contributor may have written is **not** a parameter:
    it lives on the commitment row and has no business in a log line.
    """

    _emit(
        EventName.COMMITMENT_FULFILLED if fulfilled else EventName.COMMITMENT_MISSED,
        namespace=namespace,
        community_id=community_id,
        case_id=case_id,
        case_version=case_version,
        correlation_id=correlation_id,
        actor_id_hash=actor_id_hash,
        entity_type="COMMITMENT",
        entity_id=commitment_id,
        outcome="FULFILLED" if fulfilled else "MISSED",
    )
