"""A Phase 3 harness: a seeded community, the frozen feed, and the real use cases.

Everything a Monitor test needs is assembled from production classes. The only substitution is
the agent itself, which is the point: if the ingestion path, the validator, the planner, the
transaction, and the feed query were stubbed too, a passing test would say nothing about the
system that runs.

The harness is driver-agnostic, so the same scenarios run against the in-memory emulator and
against DynamoDB Local.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4, uuid5

from chorus.application.commands.decide_mandate import DecideMandate
from chorus.application.commands.ingest_messages import (
    IngestAttachment,
    IngestMessage,
    IngestMessages,
    IngestMessagesCommand,
    IngestMessagesResult,
)
from chorus.application.commands.propose_mandates import ProposeMandates
from chorus.application.commands.run_monitor import RunMonitor, RunMonitorCommand
from chorus.application.commands.run_monitor_operation import MonitorOperationWorker
from chorus.application.operations import ApplicationOperations, monitor_locator_hash
from chorus.application.queries.feed import ReadAmbientFeed
from chorus.application.queries.mandates import ReadMandateThread
from chorus.application.services.monitor_snapshots import MonitorSnapshots
from chorus.domain.entities import (
    ApplicationOperation,
    ApplicationOperationKind,
    Community,
    CommunityStatus,
    Contributor,
    ContributorStatus,
)
from chorus.domain.ids import (
    CommunityId,
    ContributorId,
    DestinationId,
    Namespace,
    OperationId,
    SensitiveStr,
    Sha256Digest,
    Uuid5Generator,
)
from chorus.infrastructure.dynamodb.audit import AuditRepository
from chorus.infrastructure.dynamodb.core import CoreRepository
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.idempotency import IdempotencyRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.fixtures.synthetic_feed import SyntheticAmbientAdapter
from chorus.ports.agents import MonitorAgentPort
from chorus.ports.ambient import AmbientMessage
from chorus.ports.operations import MonitorOperationJob
from chorus.ports.records import MessageFeedEntry
from chorus.ports.retention import AuditRetention
from chorus.ports.scopes import CommunityScope
from chorus.ports.storage import StorageDriver, TableName
from chorus.ports.unit_of_work import TransactionPlan

NAMESPACE = Namespace("TEST_MONITOR")
OTHER_NAMESPACE = Namespace("TEST_MONITOR_ALT")
CURSOR_SECRET = b"chorus-monitor-test-pagination-secret"
NOW = datetime(2030, 1, 14, 9, 0, 0, tzinfo=UTC)

FIXTURE_ID_NAMESPACE = UUID("0f5a4a6a-6c1c-5f3a-8a4f-9d3a2b1c0e77")
PRESENTER_ACTOR_HASH = Sha256Digest(f"sha256:{sha256(b'presenter_admin').hexdigest()}")
DESTINATION_ID = DestinationId("property_manager:demo")

RESIDENT_PSEUDONYM_BY_ACTOR: dict[str, str] = {
    "resident_a": "resident-a",
    "resident_b": "resident-b",
    "resident_c": "resident-c",
    "resident_d": "resident-d",
}
"""The seeded persona registry: which demo actor acts as which corpus contributor.

Deliberately a mapping rather than a string transformation. A persona is an authentication
concept and a pseudonym is corpus data, and deriving one from the other by replacing an
underscore would mean any future persona name silently minted an identity.
"""


def resident_actor_hash(actor: str) -> Sha256Digest:
    """Hash one resident persona the way the API does, for a command built without HTTP."""

    return Sha256Digest(f"sha256:{sha256(actor.encode('utf-8')).hexdigest()}")


@dataclass(slots=True)
class SteppableClock:
    """A clock that only moves when a test says so."""

    instant: datetime = NOW

    def now(self) -> datetime:
        return self.instant

    def advance(self, *, seconds: int) -> None:
        self.instant += timedelta(seconds=seconds)


@dataclass(slots=True)
class MonitorHarness:
    """One seeded community plus every Phase 3 use case wired over one storage driver."""

    driver: StorageDriver
    namespace: Namespace = NAMESPACE
    clock: SteppableClock = field(default_factory=SteppableClock)
    adapter: SyntheticAmbientAdapter = field(default_factory=SyntheticAmbientAdapter)
    core: CoreRepository = field(init=False)
    audit: AuditRepository = field(init=False)
    idempotency: IdempotencyRepository = field(init=False)
    unit_of_work: StorageUnitOfWork = field(init=False)
    ids: Uuid5Generator = field(init=False)

    def __post_init__(self) -> None:
        cursors = SignedCursorCodec(CURSOR_SECRET)
        self.core = CoreRepository(driver=self.driver, cursors=cursors)
        self.audit = AuditRepository(
            driver=self.driver, cursors=cursors, retention=AuditRetention.demo()
        )
        self.idempotency = IdempotencyRepository(driver=self.driver, table=TableName.CORE)
        self.unit_of_work = StorageUnitOfWork(driver=self.driver)
        self.ids = Uuid5Generator(namespace=FIXTURE_ID_NAMESPACE, prefix=self.namespace.value)

    # -- identity ----------------------------------------------------------------------

    @property
    def community_id(self) -> CommunityId:
        return CommunityId(uuid5(FIXTURE_ID_NAMESPACE, f"{self.namespace.value}:community"))

    def contributor_id(self, pseudonym: str) -> ContributorId:
        return ContributorId(
            uuid5(FIXTURE_ID_NAMESPACE, f"{self.namespace.value}:contributor:{pseudonym}")
        )

    # -- seeding -----------------------------------------------------------------------

    async def seed(self) -> None:
        """Create the community and its pseudonymous contributors."""

        now = self.clock.now()
        scope = self.core_scope
        operations = [
            self.core.stage_create_community(
                scope.namespace_scope,
                Community(
                    community_id=self.community_id,
                    namespace=self.namespace,
                    name=self.adapter.community.name,
                    timezone=self.adapter.community.timezone,
                    status=CommunityStatus.ACTIVE,
                    version=1,
                    created_at=now,
                    updated_at=now,
                ),
            )
        ]
        for seed in self.adapter.contributor_seeds:
            operations.append(
                self.core.stage_create_contributor(
                    scope,
                    Contributor(
                        contributor_id=self.contributor_id(seed.pseudonym),
                        community_id=self.community_id,
                        namespace=self.namespace,
                        pseudonym=seed.pseudonym,
                        display_name=SensitiveStr(seed.display_name),
                        email=SensitiveStr(f"{seed.pseudonym}@example.invalid"),
                        status=ContributorStatus.ACTIVE,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    ),
                )
            )
        await self.unit_of_work.commit(
            TransactionPlan(
                name="seed-community", operations=tuple(operations), audit_required=False
            )
        )

    @property
    def core_scope(self) -> CommunityScope:
        return CommunityScope(namespace=self.namespace, community_id=self.community_id)

    # -- use cases ---------------------------------------------------------------------

    @property
    def ingest(self) -> IngestMessages:
        return IngestMessages(
            core=self.core,
            idempotency=self.idempotency,
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            ids=self.ids,
        )

    @property
    def snapshots(self) -> MonitorSnapshots:
        return MonitorSnapshots(core=self.core, unit_of_work=self.unit_of_work)

    def run_monitor(self, agent: MonitorAgentPort) -> RunMonitor:
        return RunMonitor(
            core=self.core,
            audit=self.audit,
            idempotency=self.idempotency,
            unit_of_work=self.unit_of_work,
            agent=agent,
            attachments=self.adapter,
            snapshots=self.snapshots,
            clock=self.clock,
        )

    @property
    def operations(self) -> ApplicationOperations:
        return ApplicationOperations(
            core=self.core,
            idempotency=self.idempotency,
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            ids=self.ids,
        )

    def worker(self, agent: MonitorAgentPort) -> MonitorOperationWorker:
        return MonitorOperationWorker(
            operations=self.operations, run_monitor=self.run_monitor(agent)
        )

    @property
    def read_feed(self) -> ReadAmbientFeed:
        return ReadAmbientFeed(core=self.core, attachments=self.adapter)

    # -- mandates ----------------------------------------------------------------------

    @property
    def propose_mandates(self) -> ProposeMandates:
        return ProposeMandates(
            core=self.core,
            audit=self.audit,
            idempotency=self.idempotency,
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            ids=self.ids,
        )

    @property
    def decide_mandate(self) -> DecideMandate:
        return DecideMandate(
            core=self.core,
            audit=self.audit,
            idempotency=self.idempotency,
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            ids=self.ids,
        )

    @property
    def read_mandate_thread(self) -> ReadMandateThread:
        return ReadMandateThread(core=self.core, clock=self.clock)

    @property
    def contributor_by_actor(self) -> dict[str, ContributorId]:
        """The seeded actor registry the API resolves a resident persona through."""

        return {
            actor: self.contributor_id(pseudonym)
            for actor, pseudonym in RESIDENT_PSEUDONYM_BY_ACTOR.items()
        }

    # -- ingestion ---------------------------------------------------------------------

    def command_message(self, message: AmbientMessage) -> IngestMessage:
        pseudonym = message.contributor_pseudonym
        return IngestMessage(
            channel_message_id=message.channel_message_id,
            contributor_id=None if pseudonym is None else self.contributor_id(pseudonym),
            sent_at=message.sent_at,
            text=message.text,
            attachments=tuple(
                IngestAttachment(
                    evidence_id=attachment.evidence_id,
                    media_type=attachment.media_type,
                    byte_length=attachment.byte_length,
                    sha256=attachment.sha256,
                )
                for attachment in message.attachments
            ),
        )

    async def ingest_messages(
        self,
        messages: tuple[AmbientMessage, ...],
        *,
        idempotency_key: str = "ingest-fixture-key-0001",
    ) -> IngestMessagesResult:
        return await self.ingest.execute(
            IngestMessagesCommand(
                namespace=self.namespace,
                community_id=self.community_id,
                actor_id_hash=PRESENTER_ACTOR_HASH,
                idempotency_key=idempotency_key,
                messages=tuple(self.command_message(message) for message in messages),
            )
        )

    async def ingest_feed(self, *, batch_size: int = 25) -> tuple[MessageFeedEntry, ...]:
        """Ingest the whole frozen corpus in request-sized batches."""

        corpus = self.adapter.messages()
        sent_at = {message.channel_message_id: message.sent_at for message in corpus}
        locators: list[MessageFeedEntry] = []
        for index in range(0, len(corpus), batch_size):
            batch = corpus[index : index + batch_size]
            result = await self.ingest_messages(
                batch, idempotency_key=f"ingest-fixture-key-{index:04d}"
            )
            locators.extend(
                MessageFeedEntry(
                    message_id=item.message_id, sent_at=sent_at[item.channel_message_id]
                )
                for item in result.messages
            )
        return tuple(locators)

    def operation_id(self, label: str = "1") -> OperationId:
        return OperationId(uuid5(FIXTURE_ID_NAMESPACE, f"{self.namespace.value}:operation:{label}"))

    # -- the durable handover ----------------------------------------------------------

    async def bound_operation(
        self,
        locators: tuple[MessageFeedEntry, ...] = (),
        *,
        kind: ApplicationOperationKind = ApplicationOperationKind.MONITOR,
        actor_id_hash: Sha256Digest = PRESENTER_ACTOR_HASH,
        request_hash: Sha256Digest = PRESENTER_ACTOR_HASH,
        invocation_id: UUID | None = None,
    ) -> ApplicationOperation:
        """Create an operation the way the route creates one: already bound to its handover.

        A ``MONITOR`` operation is not dispatchable until it names the invocation it authorizes
        and the digest of the exact new-message set that invocation may use, so a test fixture
        that created one without them would be building a shape production never produces --
        and every worker binding assertion would be about a state that cannot occur.
        """

        is_monitor = kind is ApplicationOperationKind.MONITOR
        return await self.operations.create(
            namespace=self.namespace,
            kind=kind,
            actor_id_hash=actor_id_hash,
            request_hash=request_hash,
            monitor_invocation_id=(invocation_id or uuid4()) if is_monitor else None,
            monitor_locator_hash=monitor_locator_hash(locators) if is_monitor else None,
        )

    def job_for(
        self,
        operation: ApplicationOperation,
        locators: tuple[MessageFeedEntry, ...],
        *,
        invocation_id: UUID | None = None,
        correlation_id: UUID | None = None,
        actor_id_hash: Sha256Digest | None = None,
        request_hash: Sha256Digest | None = None,
    ) -> MonitorOperationJob:
        """The job a dispatcher would hand a worker for this operation."""

        return MonitorOperationJob(
            operation_id=operation.operation_id,
            namespace=self.namespace,
            community_id=self.community_id,
            invocation_id=invocation_id or operation.monitor_invocation_id or uuid4(),
            correlation_id=correlation_id or uuid4(),
            actor_id_hash=actor_id_hash or operation.actor_id_hash,
            request_hash=request_hash or operation.request_hash,
            message_locators=locators,
        )

    async def dispatched(
        self,
        locators: tuple[MessageFeedEntry, ...],
        *,
        actor_id_hash: Sha256Digest = PRESENTER_ACTOR_HASH,
        request_hash: Sha256Digest = PRESENTER_ACTOR_HASH,
        invocation_id: UUID | None = None,
    ) -> tuple[ApplicationOperation, MonitorOperationJob]:
        """One bound operation and the job that belongs to it, in the shape production makes."""

        operation = await self.bound_operation(
            locators,
            actor_id_hash=actor_id_hash,
            request_hash=request_hash,
            invocation_id=invocation_id,
        )
        return operation, self.job_for(operation, locators)

    def monitor_command(
        self,
        locators: tuple[MessageFeedEntry, ...],
        *,
        invocation_id: UUID | None = None,
        operation_id: OperationId | None = None,
    ) -> RunMonitorCommand:
        return RunMonitorCommand(
            namespace=self.namespace,
            community_id=self.community_id,
            operation_id=operation_id or self.operation_id(),
            invocation_id=invocation_id
            or uuid5(FIXTURE_ID_NAMESPACE, f"{self.namespace.value}:invocation:1"),
            correlation_id=uuid5(FIXTURE_ID_NAMESPACE, f"{self.namespace.value}:correlation:1"),
            actor_id_hash=PRESENTER_ACTOR_HASH,
            message_locators=locators,
        )
