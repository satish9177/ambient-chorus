"""Wire the deployed demo reset to the one application reset service, over production adapters.

Review R2. The handler is a thin transport boundary; this root constructs
:class:`~chorus.composition.deployed_demo_reset.DeployedDemoReset`, which runs the frozen
manifest-driven sequence and calls the **shared**
:class:`~chorus.composition.demo_reset.DemoResetService` (``seed_only`` / ``build_result``) for
the seed and the receipt. There is no second definition of "reset the demo" here.

**Cold start touches no network.** The graph is built lazily on the first invocation; boto3
clients are created without a credential lookup or a request.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from chorus.application.commands.ingest_messages import IngestMessages
from chorus.composition.demo_reset import (
    DemoEvidenceObjects,
    DemoResetService,
    NamespaceStorePurge,
    SupportsSchedulePurge,
    demo_message_id_generator,
    predict_demo_case_id,
)
from chorus.composition.deployed_demo_reset import DeployedDemoReset
from chorus.domain.entities import DestinationKind
from chorus.domain.ids import CaseId, DestinationId, Namespace
from chorus.domain.time import SystemClock
from chorus.infrastructure.demo_reset_purge import (
    S3DemoObjectPrefixPurge,
    S3PurgeClient,
    SchedulerDemoSchedulePurge,
    SchedulerPurgeClient,
)
from chorus.infrastructure.dynamodb.client import create_dynamodb_client
from chorus.infrastructure.dynamodb.core import CoreRepository
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.demo_clock import DynamoDbDemoClockResetStore
from chorus.infrastructure.dynamodb.demo_reset_store import (
    DynamoDbDemoManifestRegistrar,
    DynamoDbDemoManifestStore,
    DynamoDbDemoPartitionPurge,
    DynamoDbDemoResetLock,
    DynamoDbDemoResetReceiptStore,
)
from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver
from chorus.infrastructure.dynamodb.idempotency import IdempotencyRepository
from chorus.infrastructure.dynamodb.shareable import ShareableRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.fixtures.synthetic_feed import SyntheticAmbientAdapter
from chorus.infrastructure.local.demo_clock import LogicalDemoClock
from chorus.infrastructure.s3.client import create_s3_client
from chorus.infrastructure.s3.objects import S3ObjectStore
from chorus.infrastructure.scheduler.client import create_scheduler_client
from chorus.ports.records import StoredSafeDestination
from chorus.ports.storage import StoredItem, TableName

DEMO_SEED_INSTANT = datetime.fromisoformat("2030-01-14T09:00:00+00:00")
DEMO_NAMESPACE = "DEMO"
DEMO_SEED_VERSION = "elevator/v1"


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoResetSettings:
    """Everything the composition root needs, and nothing it could decide policy from."""

    region: str
    environment: str
    namespace: str
    core_table: str
    shareable_table: str
    audit_table: str
    private_evidence_bucket: str
    export_evidence_bucket: str
    private_evidence_key_arn: str
    export_evidence_key_arn: str
    scheduler_group: str
    scheduler_environment: str
    destination_id: str
    destination_display_label: str
    destination_registry_version: int
    destination_routing_token: str
    dynamodb_endpoint: str | None = None


class _UnusedNamespacePurge:
    """A ``NamespaceStorePurge`` the deployed seed never calls -- ``DeployedDemoReset`` runs the
    manifest-driven purge itself and only invokes ``DemoResetService.seed_only``."""

    async def purge_namespace(self, namespace: str) -> int:  # pragma: no cover
        raise NotImplementedError("the deployed reset purges via DynamoDbDemoPartitionPurge")

    async def namespace_items(self, namespace: str) -> tuple[StoredItem, ...]:  # pragma: no cover
        raise NotImplementedError("the deployed reset guards via the manifest EXECUTION partitions")


class _UnusedSchedulePurge:
    def reset(self) -> None:  # pragma: no cover
        raise NotImplementedError("the deployed reset purges via SchedulerDemoSchedulePurge")


def _schedule_name_prefix(settings: DemoResetSettings) -> str:
    """Use the same namespace boundary as normal schedule creation."""
    from chorus.application.services.commitment_schedule import schedule_name_prefix

    return schedule_name_prefix(
        environment=settings.scheduler_environment, namespace=Namespace("DEMO")
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoResetComposition:
    reset: DeployedDemoReset


def build_demo_reset(settings: DemoResetSettings) -> DemoResetComposition:
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
    cursors = SignedCursorCodec(secrets.token_bytes(32))
    core = CoreRepository(driver=driver, cursors=cursors)
    shareable = ShareableRepository(driver=driver, cursors=cursors)
    unit_of_work = StorageUnitOfWork(driver=driver)
    idempotency_core = IdempotencyRepository(driver=driver, table=TableName.CORE)

    namespace = Namespace(settings.namespace)
    adapter = SyntheticAmbientAdapter()
    community_id = adapter.community.community_id
    demo_case_id: CaseId = predict_demo_case_id(
        adapter, namespace=namespace, community_id=community_id
    )
    demo_clock = LogicalDemoClock(instant=adapter.logical_clock_start)
    message_ids = demo_message_id_generator()

    s3_object_store = S3ObjectStore(
        client=create_s3_client(region_name=settings.region),
        private_bucket=settings.private_evidence_bucket,
        export_bucket=settings.export_evidence_bucket,
        private_kms_key_id=settings.private_evidence_key_arn,
        export_kms_key_id=settings.export_evidence_key_arn,
    )

    destination = StoredSafeDestination(
        destination_id=DestinationId(settings.destination_id),
        kind=DestinationKind.PROPERTY_MANAGER,
        registry_version=settings.destination_registry_version,
        routing_token=UUID(settings.destination_routing_token),
        display_label=settings.destination_display_label,
    )

    seeder = DemoResetService(
        settings_environment=settings.environment,
        adapter=adapter,
        driver=cast(NamespaceStorePurge, _UnusedNamespacePurge()),
        core=core,
        shareable=shareable,
        unit_of_work=unit_of_work,
        ingest_messages=IngestMessages(
            core=core,
            idempotency=idempotency_core,
            unit_of_work=unit_of_work,
            clock=demo_clock,
            ids=message_ids,
        ),
        message_ids=message_ids,
        scheduler=cast(SupportsSchedulePurge, _UnusedSchedulePurge()),
        outbox_dir=None,
        objects=cast(DemoEvidenceObjects, s3_object_store),
        demo_clock=demo_clock,
        clock=demo_clock,
        namespace=namespace,
        community_id=community_id,
        destination=destination,
        demo_case_id=demo_case_id,
    )

    name_prefix = _schedule_name_prefix(settings)
    deployed = DeployedDemoReset(
        seeder=seeder,
        driver=driver,
        manifest_store=DynamoDbDemoManifestStore(
            driver=driver, namespace=namespace, seed_version=DEMO_SEED_VERSION
        ),
        registrar=DynamoDbDemoManifestRegistrar(driver=driver, namespace=namespace),
        lock=DynamoDbDemoResetLock(driver=driver, namespace=namespace),
        receipts=DynamoDbDemoResetReceiptStore(driver=driver, namespace=namespace),
        partition_purge=DynamoDbDemoPartitionPurge(driver=driver),
        private_object_purge=S3DemoObjectPrefixPurge(
            client=cast(S3PurgeClient, s3_object_store.client)
        ),
        export_object_purge=S3DemoObjectPrefixPurge(
            client=cast(S3PurgeClient, s3_object_store.client)
        ),
        schedule_purge=SchedulerDemoSchedulePurge(
            client=cast(SchedulerPurgeClient, create_scheduler_client(region_name=settings.region)),
            group_name=settings.scheduler_group,
            name_prefix=name_prefix,
        ),
        clock_reset_store=DynamoDbDemoClockResetStore(driver=driver, namespace=namespace),
        wall_clock=SystemClock(),
        namespace=namespace,
        seed_version=DEMO_SEED_VERSION,
        settings_environment=settings.environment,
        seed_instant=DEMO_SEED_INSTANT,
        private_bucket=settings.private_evidence_bucket,
        export_bucket=settings.export_evidence_bucket,
        schedule_name_prefix=name_prefix,
    )
    return DemoResetComposition(reset=deployed)


__all__ = [
    "DEMO_NAMESPACE",
    "DEMO_SEED_INSTANT",
    "DEMO_SEED_VERSION",
    "DemoResetComposition",
    "DemoResetSettings",
    "build_demo_reset",
]
