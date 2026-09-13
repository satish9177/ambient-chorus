"""The operation worker's Lambda entry point: parse, bind, run, and report a little.

The request path writes a durable ``ApplicationOperation``, hands one job over asynchronously,
and returns ``202``. This is the other end of that handover
([the deployment contract](../../docs/plans/phase-11-deployment-contract.md) § 14).

The order of one invocation
----------------------------
1. read the envelope and decode the job against ``worker-job/v1``. An unknown schema version, an
   unknown kind, or a malformed body is refused **before** a client exists, an operation is
   loaded, or anything is claimed;
2. select the runner from the job's **declared** kind, on the closed
   :class:`~chorus.application.jobs.WorkerJobKind` set. There is no default branch and nothing
   here reads the payload's shape to guess what was meant;
3. strongly read the authoritative logical clock and bind it for the invocation, so every
   timestamp this operation writes names the same instant. Fail closed if it cannot be read;
4. run the operation worker, which owns every claim, binding, and replay rule.

Duplicate delivery, and why nothing here handles it
-----------------------------------------------------
AWS asynchronous invocation may deliver the same job twice. This handler holds **no cache, no
seen-set, and no process-local deduplication**, because a process-local answer to a
cross-process question stops being true the moment a second execution environment exists.

The durable machinery is the boundary, and it already exists. Every operation worker loads the
operation, checks the job's binding against it -- kind, namespace, case, actor, request hash,
invocation identity -- and then makes a *single conditional write* to claim ``PENDING ->
RUNNING``. Two deliveries racing on one operation both evaluate that condition and exactly one
wins; the loser finds ``RUNNING`` and takes the resume path rather than invoking a model. A
delivery that arrives after the operation is terminal does at most an idempotent projection
repair. And for ``SEND_ACTION`` the guarantee is stronger still and does not depend on the
operation record at all: the execution's own ``APPROVED@v -> SENDING@v+1`` compare-and-swap
permits an SES call from exactly one state, so a duplicate makes **zero** SES calls.

What this returns
------------------
A small, safe diagnostic: the operation's identity, kind, status, and error code -- values the
operation record already stores and the polling surface already shows. **No agent output, no
message text, no payload echo, and no traceback.** A Lambda's return value reaches CloudWatch,
and a refusal carries a reason code and nothing else.

Nothing here speaks HTTP, and nothing here calls Bedrock. Agent invocation goes through the
AgentCore adapters, the sender through its own function, the schedule through EventBridge --
each one an object the composition root built and this module only uses.

**Cold start touches no network.** The graph is built lazily on the first invocation.
"""

from __future__ import annotations

import secrets
from typing import Any, Final, Protocol

import anyio

from chorus.application.jobs import WorkerJobError, WorkerJobKind, decode_job
from chorus.domain.entities import ApplicationOperation, DestinationKind
from chorus.domain.ids import DestinationId
from chorus.ports.demo_clock import DemoClockError
from chorus.ports.records import StoredSafeDestination
from chorus.settings import Settings
from functions.envelope import EnvelopeError, InvocationFailedError, failure, read_envelope
from functions.worker.composition import WorkerComposition, WorkerSettings, build_worker

ACCEPTED_OPERATIONS: Final = frozenset(kind.value for kind in WorkerJobKind)
"""The worker's complete invocation surface: one operation name per job kind."""

MALFORMED_EVENT: Final = "MALFORMED_EVENT"
CLOCK_UNAVAILABLE: Final = "CLOCK_UNAVAILABLE"
UNSUPPORTED_OPERATION: Final = "UNSUPPORTED_OPERATION"

COMMUNITY_PUBLIC_LABEL: Final = "Community"

_composition: WorkerComposition | None = None


class OperationRunner(Protocol):
    """Run one decoded job to a terminal operation status.

    Deliberately typed by what the five workers already are, rather than by a new base class:
    each of them records failure *on the operation* instead of raising, because an exception
    escaping into an at-least-once dispatcher reads as "retry me".
    """

    async def execute(self, job: Any) -> ApplicationOperation:
        """Return the operation as it now stands."""


def _require(value: str | None, what: str) -> str:
    """Refuse a missing worker deployment value at the mapper, not deep in a claimed operation.

    The worker is the one function that genuinely invokes agents (review P2-8), so *its*
    settings mapper is where "no runtime endpoint ARN" fails -- ``build_worker`` still re-checks,
    but the earliest, clearest failure is here.
    """

    if not value:
        raise ValueError(f"a deployed worker needs {what}")
    return value


def worker_settings(settings: Settings) -> WorkerSettings:
    """Map process configuration onto the worker's own settings, and nothing wider.

    Requires exactly what the worker's object graph consumes: the three AgentCore **runtime
    endpoint** ARNs (no model-profile ARN -- no worker adapter reads one), the compiler and
    sender function ARNs, the watcher ``:live`` alias, and the scheduler identity. No secret ARN
    of any kind -- the worker holds no Secrets Manager grant.
    """

    return WorkerSettings(
        region=settings.aws_region,
        namespace=settings.namespace,
        core_table=settings.core_table,
        shareable_table=settings.shareable_table,
        audit_table=settings.audit_table,
        monitor_runtime_arn=_require(settings.monitor_runtime_arn, "the Monitor runtime ARN"),
        investigator_runtime_arn=_require(
            settings.investigator_runtime_arn, "the Investigator runtime ARN"
        ),
        action_runtime_arn=_require(settings.action_runtime_arn, "the Action runtime ARN"),
        agent_timeout_seconds=settings.agent_timeout_seconds,
        sender_function_arn=_require(settings.sender_function_arn, "the sender function ARN"),
        scheduler_group=settings.scheduler_group,
        scheduler_environment=settings.scheduler_environment,
        scheduler_role_arn=_require(
            settings.scheduler_role_arn, "the scheduler execution role ARN"
        ),
        watcher_function_arn=_require(settings.watcher_function_arn, "the watcher live alias ARN"),
        destination=StoredSafeDestination(
            destination_id=DestinationId(settings.destination_id),
            kind=DestinationKind.PROPERTY_MANAGER,
            registry_version=settings.destination_registry_version,
            routing_token=settings.destination_routing_token,
            display_label=settings.destination_display_label,
        ),
        from_identity_id=settings.ses_from_identity_id,
        ses_configuration_set=settings.ses_configuration_set,
        policy_version=settings.policy_version,
        community_public_label=COMMUNITY_PUBLIC_LABEL,
        cursor_secret=secrets.token_bytes(32),
    )


def composition() -> WorkerComposition:
    """Build the object graph once per execution environment, on first use."""

    global _composition
    if _composition is None:
        _composition = build_worker(worker_settings(Settings.load()))
    return _composition


async def run(event: object, *, built: WorkerComposition | None = None) -> dict[str, Any]:
    """Run one delivered job. The async body the handler drives."""

    try:
        _, payload = read_envelope(event, accepted=ACCEPTED_OPERATIONS)
        kind, job = decode_job(payload)
    except (EnvelopeError, WorkerJobError):
        # Refused at the parse. Nothing is loaded, nothing is claimed, nothing is written.
        return failure(MALFORMED_EVENT)
    graph = built or composition()
    runner: OperationRunner | None = graph.runners.get(kind)  # type: ignore[assignment]
    if runner is None:
        # Unreachable while the runner map covers the closed kind set, and checked anyway: a
        # worker that fell through to a default branch would be a worker a payload could steer.
        return failure(UNSUPPORTED_OPERATION)
    try:
        record = await graph.clock_store.read()
    except DemoClockError as error:
        # Fails the *invocation*, not the answer -- and it is safe to, because it happens
        # strictly before the operation is claimed: no runner has executed, no SES call is
        # possible, nothing has been written. A normal return here would be read as
        # "delivered" by the async caller and never retried, silently losing the job; raising
        # lets AWS's own async retry (and, where configured, its DLQ) see the outage instead
        # (P2-6). This is exactly the boundary that keeps it safe for ``SEND_ACTION`` too: the
        # send path's own ``SEND_UNKNOWN``/``SENDING`` quarantine is untouched, because nothing
        # here ever reaches it.
        raise InvocationFailedError(CLOCK_UNAVAILABLE) from error
    with graph.scope.bound_to(record.logical_time):
        operation = await _execute(runner, job)
    return {
        "status": "COMPLETED",
        "operation_id": str(operation.operation_id),
        "kind": operation.kind.value,
        "operation_status": operation.status.value,
        "error_code": operation.error_code,
    }


async def _execute(runner: OperationRunner, job: object) -> ApplicationOperation:
    """One call, on the one method every operation worker declares."""

    return await runner.execute(job)


def handler(event: object, context: object = None) -> dict[str, Any]:
    """The Lambda entry point. One invocation, one event loop, one job."""

    return anyio.run(run, event)


__all__ = [
    "ACCEPTED_OPERATIONS",
    "CLOCK_UNAVAILABLE",
    "COMMUNITY_PUBLIC_LABEL",
    "MALFORMED_EVENT",
    "UNSUPPORTED_OPERATION",
    "OperationRunner",
    "composition",
    "handler",
    "run",
    "worker_settings",
]
