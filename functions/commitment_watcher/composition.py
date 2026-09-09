"""Wire the commitment watcher to the one use case it exists to run.

A composition root and nothing else: it constructs, it does not decide. Every ordering rule is
inside :mod:`chorus.application.commands.record_commitment_due`, and if this module ever grows a
branch on a status, a generation, or a clock reading, that branch is a second implementation of
something already frozen.

**No deployed resource is created here.** Phase 9 owns this artifact, its role, its log group,
the schedule group, the DLQ, and the alarms, and deploys none of them; the deployed function,
the live schedule, and the post-deploy IAM canaries belong to Phase 11. That is the same
static-now split the compiler, the three agent runtimes, and the sender already use.

The smallest object graph in the system
----------------------------------------
The watcher has **no Core repository, no object store, no agent client, no SES port, no compiler
client, and no scheduler client**. Its whole data-plane authority is the Shareable
``NS#n#CASE#k`` partition plus an audit append
([ADR-028](../../../docs/adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) § 6), and the
absence of those fields is what makes "the watcher invokes no model and sends nothing" a
property of the type rather than of a policy nobody ran. A static test asserts the same thing
from the artifact's imports, and the synthesized role denies each of them outright.

It is a schedule **target**, never a schedule client: it holds no ``scheduler:*`` grant, so it
cannot create the schedule that invoked it or any other.

One clock, and the demo advances it
------------------------------------
The deployment supplies exactly one :class:`~chorus.ports.clock.Clock`. In ``demo`` it is
the **durable** logical clock at the literal Shareable partition ``NS#DEMO#CLOCK``, read
strongly at the start of every invocation and advanced only by ``POST /v1/demo/clock/advance``
([ADR-029](../../../docs/adr/ADR-029-deployed-demo-clock-authority.md)); elsewhere it is
:class:`~chorus.domain.time.SystemClock` and no clock row exists. The watcher never holds two
and never chooses between them, so the early-firing comparison means the same thing on both
paths -- and now means it *across processes*, which is what "exactly one clock" was always
supposed to mean and could not mean while the clock lived in a Python object.

The watcher's clock authority is **read-only**, in the object graph and in IAM. It is handed a
:class:`~chorus.ports.demo_clock.DemoClockStorePort`, its policy carries no write action on the
clock prefix, and an explicit deny backs the absence -- because a watcher that could move
logical time could make its own early-firing check pass.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.application.commands.record_commitment_due import RecordCommitmentDue
from chorus.domain.ids import IdGenerator, Namespace, Uuid4Generator
from chorus.infrastructure.dynamodb.audit import AuditRepository
from chorus.infrastructure.dynamodb.client import create_dynamodb_client
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.demo_clock import DynamoDbDemoClockStore
from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver
from chorus.infrastructure.dynamodb.shareable import ShareableRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.persistent_clock import ScopedLogicalClock
from chorus.ports.clock import Clock
from chorus.ports.demo_clock import DemoClockStorePort
from chorus.ports.retention import AuditRetention
from chorus.ports.storage import StorageDriver, TableName


@dataclass(frozen=True, slots=True, kw_only=True)
class WatcherSettings:
    """Everything the composition root needs, and nothing it could decide policy from.

    There is deliberately no ``core_table``. The watcher is denied that table outright, so a
    setting naming it would be a value the role could never use and a hint that somebody should
    try.

    ``namespace`` is here because the clock row's partition is built from it, and it is
    deployment configuration rather than anything a delivered event may name: the watcher's
    clock grant is an exact ``dynamodb:LeadingKeys`` literal, so a namespace an event could
    choose would be a partition the policy never authorized.
    """

    region: str
    namespace: str
    shareable_table: str
    audit_table: str
    cursor_secret: bytes
    dynamodb_endpoint: str | None = None


def build_watcher_driver(settings: WatcherSettings) -> DynamoDbStorageDriver:
    """The watcher's one storage handle, with a table map of exactly **two** entries.

    A third would be a Core handle in an object graph whose whole security argument is that it
    has none, and the map is where that is expressible rather than merely intended.
    """

    return DynamoDbStorageDriver(
        client=create_dynamodb_client(
            region_name=settings.region, endpoint_url=settings.dynamodb_endpoint
        ),
        table_names={
            TableName.SHAREABLE: settings.shareable_table,
            TableName.AUDIT: settings.audit_table,
        },
    )


def build_demo_clock_store(
    settings: WatcherSettings, *, driver: StorageDriver | None = None
) -> DemoClockStorePort:
    """The handle onto the one authoritative clock row.

    Typed as :class:`~chorus.ports.demo_clock.DemoClockStorePort` at this seam, which is the
    honest description of the authority ADR-029 § 2 grants the watcher: a strongly consistent
    **read** of ``NS#DEMO#CLOCK`` and no write of any form. IAM is what enforces that -- the
    write is denied outright -- and a template test reads the policy rather than this sentence.
    """

    return DynamoDbDemoClockStore(
        driver=driver or build_watcher_driver(settings), namespace=Namespace(settings.namespace)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class WatcherComposition:
    """The watcher, the clock it reads, and the slot one invocation's reading is bound into."""

    watcher: RecordCommitmentDue
    clock_store: DemoClockStorePort
    scope: ScopedLogicalClock


def build_watcher(
    settings: WatcherSettings, *, ids: IdGenerator | None = None
) -> WatcherComposition:
    """Construct the deployed watcher over one driver, one clock store, and one scoped clock.

    The scoped clock is built at cold start and bound once per invocation, because a durable
    clock cannot answer a synchronous ``now()`` and reading the row again at each ``now()``
    would let one invocation's early-firing check and its own audit timestamp disagree. Outside
    a bound scope it raises rather than defaulting, so a path that reached for time without a
    reading fails closed instead of inventing one.
    """

    driver = build_watcher_driver(settings)
    scope = ScopedLogicalClock()
    return WatcherComposition(
        watcher=build_record_commitment_due(settings, clock=scope, ids=ids, driver=driver),
        clock_store=build_demo_clock_store(settings, driver=driver),
        scope=scope,
    )


def build_record_commitment_due(
    settings: WatcherSettings,
    *,
    clock: Clock,
    ids: IdGenerator | None = None,
    driver: StorageDriver | None = None,
) -> RecordCommitmentDue:
    """Construct the watcher use case over deployed adapters."""

    driver = driver or build_watcher_driver(settings)
    cursors = SignedCursorCodec(secret=settings.cursor_secret)
    return RecordCommitmentDue(
        shareable=ShareableRepository(driver=driver, cursors=cursors),
        audit=AuditRepository(driver=driver, cursors=cursors, retention=AuditRetention.demo()),
        unit_of_work=StorageUnitOfWork(driver=driver),
        clock=clock,
        ids=ids or Uuid4Generator(),
    )


__all__ = [
    "WatcherComposition",
    "WatcherSettings",
    "build_demo_clock_store",
    "build_record_commitment_due",
    "build_watcher",
    "build_watcher_driver",
]
