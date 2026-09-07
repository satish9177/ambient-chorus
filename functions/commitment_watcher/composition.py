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
The deployment supplies exactly one :class:`~chorus.ports.clock.Clock`. In ``demo`` it is a
logical clock, monotonic and advanced only by ``POST /v1/demo/clock/advance``; elsewhere it is
:class:`~chorus.domain.time.SystemClock`. The watcher never holds two and never chooses between
them, so the early-firing comparison means the same thing on both paths.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.application.commands.record_commitment_due import RecordCommitmentDue
from chorus.domain.ids import IdGenerator, Uuid4Generator
from chorus.infrastructure.dynamodb.audit import AuditRepository
from chorus.infrastructure.dynamodb.client import create_dynamodb_client
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver
from chorus.infrastructure.dynamodb.shareable import ShareableRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.ports.clock import Clock
from chorus.ports.retention import AuditRetention
from chorus.ports.storage import TableName


@dataclass(frozen=True, slots=True, kw_only=True)
class WatcherSettings:
    """Everything the composition root needs, and nothing it could decide policy from.

    There is deliberately no ``core_table``. The watcher is denied that table outright, so a
    setting naming it would be a value the role could never use and a hint that somebody should
    try.
    """

    region: str
    shareable_table: str
    audit_table: str
    cursor_secret: bytes
    dynamodb_endpoint: str | None = None


def build_record_commitment_due(
    settings: WatcherSettings,
    *,
    clock: Clock,
    ids: IdGenerator | None = None,
) -> RecordCommitmentDue:
    """Construct the watcher use case over deployed adapters.

    The driver is given a table map with **two** entries. A third would be a Core handle in an
    object graph whose whole security argument is that it has none, and the map is where that
    is expressible rather than merely intended.
    """

    driver = DynamoDbStorageDriver(
        client=create_dynamodb_client(
            region_name=settings.region, endpoint_url=settings.dynamodb_endpoint
        ),
        table_names={
            TableName.SHAREABLE: settings.shareable_table,
            TableName.AUDIT: settings.audit_table,
        },
    )
    cursors = SignedCursorCodec(secret=settings.cursor_secret)
    return RecordCommitmentDue(
        shareable=ShareableRepository(driver=driver, cursors=cursors),
        audit=AuditRepository(driver=driver, cursors=cursors, retention=AuditRetention.demo()),
        unit_of_work=StorageUnitOfWork(driver=driver),
        clock=clock,
        ids=ids or Uuid4Generator(),
    )


__all__ = ["WatcherSettings", "build_record_commitment_due"]
