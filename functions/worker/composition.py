"""Wire the asynchronous operation worker to the five operations it exists to run.

A composition root and nothing else: it constructs, it does not decide. Every claim rule, every
binding check, every replay table and every ordering constraint lives inside the operation
workers in :mod:`chorus.application.commands`, and a branch here on a status, a kind, or an
outcome would be a second implementation of one of them.

The worker is the broadest principal in the system after the API, and its boundary is drawn by
what this root constructs
([the deployment contract](../../docs/plans/phase-11-deployment-contract.md) § 8.1):

* the three **AgentCore runtime** adapters -- the worker is the only principal that may invoke
  them, and it reaches them through the runtimes, never through Bedrock directly;
* the **EventBridge Scheduler** client, ``CreateSchedule``/``GetSchedule`` on one group;
* one synchronous **Lambda invoker** aimed at the sender's exact configured ARN;
* a **read-only** handle on the deployed demo clock.

What it deliberately does not construct: no SES client, no Secrets Manager client, no Bedrock
client, no object store, and no demo-clock advance path. The worker reads no secret at all --
the demo bearer token is the API's and the destination registry is the sender's -- and it cannot
move logical time. The evidence object stores are the compiler's and the inbound entry point's;
none of the five operations here reads or writes one, so none is built.

Why the worker holds a clock at all, and why it is read-only
--------------------------------------------------------------
It is not decoration. ``EXTRACT_COMMITMENT`` runs here, and
:class:`~chorus.application.commands.extract_commitment_operation.ExtractCommitment` passes
``clock.now()` as the ``logical_now`` of its ``CreateDueSchedule`` request -- the deadline the
schedule is *about*, never the wall-clock instant it should *fire at* (P1/P2-2; that second
question is ``CreateDueSchedule.wall_clock``, always ``SystemClock``, below). So the worker
genuinely requires **authoritative logical time**, it gets a strongly consistent read of the
one clock row, and it gets no write of any kind -- ``GetItem`` on the exact literal
``NS#DEMO#CLOCK`` partition and nothing more
([ADR-029](../../docs/adr/ADR-029-deployed-demo-clock-authority.md)).

``PROPOSE_ACTION`` reads it a second way (P2-3): ``scope`` is bound once for the whole
invocation, so ``ProposeAction``'s post-model freshness check -- which exists specifically to
catch a view that expired *while the model was answering* -- is handed the same ``clock_store``
as ``freshness_clock`` and re-reads it strongly at that one step, rather than asking the frozen
``scope`` binding a second time and getting the same answer back.

**No deployed resource is created here.** The function resource and its packaging belong to a
later Phase 11 batch; this is the code that function runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.application.commands.apply_commitment import ApplyCommitment
from chorus.application.commands.create_due_schedule import CreateDueSchedule
from chorus.application.commands.extract_commitment_operation import (
    ExtractCommitment,
    ExtractCommitmentOperationWorker,
)
from chorus.application.commands.project_action_outcome import ProjectActionOutcome
from chorus.application.commands.propose_action import ProposeAction
from chorus.application.commands.propose_action_operation import ProposeActionOperationWorker
from chorus.application.commands.reconcile_send_outcome import ReconcileSendOutcome
from chorus.application.commands.run_investigation import RunInvestigation
from chorus.application.commands.run_investigation_operation import InvestigationOperationWorker
from chorus.application.commands.run_monitor import RunMonitor
from chorus.application.commands.run_monitor_operation import MonitorOperationWorker
from chorus.application.commands.send_action_operation import SendActionOperationWorker
from chorus.application.jobs import WorkerJobKind
from chorus.application.operations import ApplicationOperations
from chorus.application.send_contract import RemoteSendAction
from chorus.application.services.monitor_snapshots import MonitorSnapshots
from chorus.domain.entities import Purpose
from chorus.domain.ids import IdGenerator, Namespace, Uuid4Generator
from chorus.domain.time import SystemClock
from chorus.infrastructure.agentcore.action import AgentCoreActionAgent
from chorus.infrastructure.agentcore.client import create_agentcore_invoker
from chorus.infrastructure.agentcore.commitment import AgentCoreCommitmentExtractionAgent
from chorus.infrastructure.agentcore.investigator import AgentCoreInvestigatorAgent
from chorus.infrastructure.agentcore.monitor import AgentCoreMonitorAgent
from chorus.infrastructure.dynamodb.audit import AuditRepository
from chorus.infrastructure.dynamodb.client import create_dynamodb_client
from chorus.infrastructure.dynamodb.core import CoreRepository
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.demo_clock import DynamoDbDemoClockStore
from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver
from chorus.infrastructure.dynamodb.idempotency import IdempotencyRepository
from chorus.infrastructure.dynamodb.shareable import ShareableRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.fixtures.synthetic_feed import SyntheticAmbientAdapter
from chorus.infrastructure.lambdas.invoker import (
    SynchronousLambdaInvoker,
    create_lambda_client,
)
from chorus.infrastructure.persistent_clock import ScopedLogicalClock
from chorus.infrastructure.scheduler.client import create_scheduler_client
from chorus.infrastructure.scheduler.eventbridge import EventBridgeDeadlineScheduler
from chorus.ports.demo_clock import DemoClockStorePort
from chorus.ports.records import StoredSafeDestination
from chorus.ports.retention import AuditRetention
from chorus.ports.storage import TableName
from chorus.privacy.policy import SafeDestination

WORKER_PURPOSE = Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE
"""The one V1 purpose, supplied by composition and never read from a delivered job."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerSettings:
    """Everything the composition root needs, and nothing it could decide policy from.

    Every ARN here is a *deployment* value -- an exact function, an exact runtime endpoint, an
    exact role. None of them is ever read from a delivered job, which is what makes "no
    caller-controlled target" a property of the object graph rather than a validation rule.
    """

    region: str
    namespace: str
    core_table: str
    shareable_table: str
    audit_table: str
    monitor_runtime_arn: str
    investigator_runtime_arn: str
    action_runtime_arn: str
    agent_timeout_seconds: int
    sender_function_arn: str
    scheduler_group: str
    scheduler_environment: str
    scheduler_role_arn: str
    watcher_function_arn: str
    """The schedule's target. The **``:live`` alias** ARN, never the unqualified function --
    rollback repoints the alias at a published version and no schedule or policy changes
    (deployment contract § 20)."""

    destination: StoredSafeDestination
    from_identity_id: str
    ses_configuration_set: str
    policy_version: str
    community_public_label: str
    cursor_secret: bytes
    scheduler_dead_letter_arn: str | None = None
    dynamodb_endpoint: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerComposition:
    """The five operation workers, the clock they read, and the slot a reading binds into."""

    runners: dict[WorkerJobKind, object]
    clock_store: DemoClockStorePort
    scope: ScopedLogicalClock


def _require(value: str | None, what: str) -> str:
    """Refuse a missing deployment value at construction rather than at first use.

    A worker with no sender ARN, no runtime endpoint, or no evidence key can never complete the
    operation that needs it, and that failure has to happen at cold start -- where it is one
    legible error -- rather than inside a claimed operation, where it is an agent invocation
    that never happened and a record that says so for reasons nobody can read.
    """

    if not value:
        raise ValueError(f"a deployed worker needs {what}")
    return value


def build_worker(settings: WorkerSettings, *, ids: IdGenerator | None = None) -> WorkerComposition:
    """Construct every operation the worker runs, over one set of deployed adapters."""

    namespace = Namespace(settings.namespace)
    generator = ids or Uuid4Generator()
    scope = ScopedLogicalClock()

    driver = DynamoDbStorageDriver(
        client=create_dynamodb_client(
            region_name=settings.region, endpoint_url=settings.dynamodb_endpoint
        ),
        table_names={
            TableName.CORE: settings.core_table,
            TableName.SHAREABLE: settings.shareable_table,
            TableName.AUDIT: settings.audit_table,
        },
    )
    cursors = SignedCursorCodec(secret=settings.cursor_secret)
    core = CoreRepository(driver=driver, cursors=cursors)
    shareable = ShareableRepository(driver=driver, cursors=cursors)
    audit = AuditRepository(driver=driver, cursors=cursors, retention=AuditRetention.demo())
    idempotency_core = IdempotencyRepository(driver=driver, table=TableName.CORE)
    idempotency_shareable = IdempotencyRepository(driver=driver, table=TableName.SHAREABLE)
    unit_of_work = StorageUnitOfWork(driver=driver)
    clock_store = DynamoDbDemoClockStore(driver=driver, namespace=namespace)

    invoker = create_agentcore_invoker(
        region_name=settings.region, timeout_seconds=settings.agent_timeout_seconds
    )
    operations = ApplicationOperations(
        core=core,
        idempotency=idempotency_core,
        unit_of_work=unit_of_work,
        clock=scope,
        ids=generator,
    )

    monitor = MonitorOperationWorker(
        operations=operations,
        run_monitor=RunMonitor(
            core=core,
            audit=audit,
            idempotency=idempotency_core,
            unit_of_work=unit_of_work,
            agent=AgentCoreMonitorAgent(
                invoker=invoker,
                runtime_arn=_require(settings.monitor_runtime_arn, "the Monitor runtime ARN"),
            ),
            attachments=SyntheticAmbientAdapter(),
            snapshots=MonitorSnapshots(core=core, unit_of_work=unit_of_work),
            clock=scope,
        ),
    )
    investigator = InvestigationOperationWorker(
        operations=operations,
        run_investigation=RunInvestigation(
            core=core,
            audit=audit,
            idempotency=idempotency_core,
            unit_of_work=unit_of_work,
            agent=AgentCoreInvestigatorAgent(
                invoker=invoker,
                runtime_arn=_require(
                    settings.investigator_runtime_arn, "the Investigator runtime ARN"
                ),
            ),
            clock=scope,
            ids=generator,
            community_public_label=settings.community_public_label,
            destination=SafeDestination(
                destination_id=settings.destination.destination_id,
                kind=settings.destination.kind,
                registry_version=settings.destination.registry_version,
                routing_token=settings.destination.routing_token,
                display_label=settings.destination.display_label,
            ),
        ),
    )
    proposer = ProposeActionOperationWorker(
        operations=operations,
        propose_action=ProposeAction(
            core=core,
            shareable=shareable,
            audit=audit,
            idempotency=idempotency_shareable,
            unit_of_work=unit_of_work,
            agent=AgentCoreActionAgent(
                invoker=invoker,
                runtime_arn=_require(settings.action_runtime_arn, "the Action runtime ARN"),
            ),
            clock=scope,
            # P2-3: ``scope`` is bound once for the whole invocation, so a second
            # ``scope.now()`` at the post-model freshness check would answer with the same
            # frozen reading rather than catching a view that expired while the model
            # answered. ``freshness_clock`` re-reads the durable row strongly at that one step.
            freshness_clock=clock_store,
            ids=generator,
            destination=settings.destination,
            from_identity_id=settings.from_identity_id,
            purpose=WORKER_PURPOSE,
        ),
    )
    extractor = ExtractCommitmentOperationWorker(
        operations=operations,
        extract=ExtractCommitment(
            core=core,
            agent=AgentCoreCommitmentExtractionAgent(
                invoker=invoker,
                # The **Investigator** endpoint. There is no fourth runtime, no fourth role, and
                # no fourth profile: the extraction inherits the Investigator's IAM position
                # exactly rather than acquiring one of its own (deployment contract § 11).
                runtime_arn=_require(
                    settings.investigator_runtime_arn, "the Investigator runtime ARN"
                ),
            ),
            apply=ApplyCommitment(
                core=core,
                shareable=shareable,
                audit=audit,
                idempotency=idempotency_shareable,
                unit_of_work=unit_of_work,
                clock=scope,
                ids=generator,
                destination_label=settings.destination.display_label,
                scheduler_environment=settings.scheduler_environment,
            ),
            clock=scope,
            policy_version=settings.policy_version,
            destination_label=settings.destination.display_label,
            schedule=CreateDueSchedule(
                shareable=shareable,
                audit=audit,
                unit_of_work=unit_of_work,
                scheduler=EventBridgeDeadlineScheduler(
                    client=create_scheduler_client(region_name=settings.region),
                    group_name=settings.scheduler_group,
                    target_arn=_require(
                        settings.watcher_function_arn, "the watcher live alias ARN"
                    ),
                    role_arn=_require(
                        settings.scheduler_role_arn, "the scheduler execution role ARN"
                    ),
                    dead_letter_arn=settings.scheduler_dead_letter_arn,
                ),
                clock=scope,
                # P1/P2-2: real wall-clock time, and only for computing when EventBridge
                # Scheduler should actually fire -- never the logical clock ``scope`` reads
                # from. A worker running on a logical clock that reads 2030 must not compute
                # the real schedule instant from that clock, or the created schedule lands in
                # 2030 real time and never fires.
                wall_clock=SystemClock(),
                ids=generator,
                scheduler_environment=settings.scheduler_environment,
            ),
            schedule_commitments=shareable,
        ),
    )
    sender = SendActionOperationWorker(
        operations=operations,
        send_action=RemoteSendAction(
            invoker=SynchronousLambdaInvoker(
                client=create_lambda_client(region_name=settings.region),
                function_name=_require(settings.sender_function_arn, "the sender function ARN"),
            )
        ),
        shareable=shareable,
        project=ProjectActionOutcome(
            core=core,
            shareable=shareable,
            audit=audit,
            idempotency=idempotency_shareable,
            unit_of_work=unit_of_work,
            clock=scope,
            ids=generator,
            destination=settings.destination,
        ),
        reconcile=ReconcileSendOutcome(
            shareable=shareable,
            core=core,
            audit=audit,
            idempotency=idempotency_shareable,
            unit_of_work=unit_of_work,
            clock=scope,
            ids=generator,
            configuration_set=settings.ses_configuration_set,
            evidence_trust=None,
        ),
    )
    return WorkerComposition(
        runners={
            WorkerJobKind.MONITOR: monitor,
            WorkerJobKind.INVESTIGATE: investigator,
            WorkerJobKind.PROPOSE_ACTION: proposer,
            WorkerJobKind.EXTRACT_COMMITMENT: extractor,
            WorkerJobKind.SEND_ACTION: sender,
        },
        clock_store=clock_store,
        scope=scope,
    )


__all__ = [
    "WORKER_PURPOSE",
    "WorkerComposition",
    "WorkerSettings",
    "build_worker",
]
