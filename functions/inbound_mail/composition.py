"""Wire the inbound reply entry point: an attester, a verifier, and the command that persists.

**Not a new principal.** This is an additional entry point of the existing application worker
artifact, which already holds the Core read/write and private-S3 grants that persisting an
``EvidenceItem`` requires. A fourth principal needing the identical case-partition grant would
buy no isolation -- ``LeadingKeys`` cannot separate ``EVIDENCE#`` from ``FACT#`` on a sort key,
the defect ADR-019 and ADR-024 each found once -- and would add a role to audit
(``docs/architecture/02-trust-iam-deployment-configuration.md``).

What Phase 9 adds instead is a **composition-level deny**, asserted by static test: this root
constructs no SES port, no Bedrock or AgentCore client, no compiler client, and no scheduler
client. IAM already denies the worker SES; this is the second, independent guarantee.

The authenticator, and why the deployed one is ``None``
--------------------------------------------------------
``build_inbound_boundary`` takes the authenticator as an argument and defaults it to ``None``.
On the AWS path it stays ``None``, because Phase 11 owes the only implementation and a
permissive stand-in would be indistinguishable at the call site from a real one. With no
authenticator the attester refuses every delivery, ``IngestExternalReply`` has no verifier to
satisfy, and a delivered reply is simply not evidence -- which is the correct outcome
([ADR-026](../../docs/adr/ADR-026-inbound-reply-trust-and-correlation.md) § 1).

A local composition passes ``LocalInboundMailAuthenticator``, which refuses at construction
outside ``test`` and ``development``. A static test asserts the
AWS root passes ``None``.

**No deployed resource is created here.** The SES receipt rule set, its S3 bucket, and the
delivery path are Phase 11's, along with the authenticator that makes them trustworthy.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.application.commands.ingest_external_reply import (
    IngestExternalReply,
    RecordReplyRejection,
)
from chorus.application.services.action_renderer import TEMPLATE_VERSION
from chorus.application.services.inbound_mail import (
    InboundMailAttester,
    InboundMailEvidenceVerifier,
    InboundRawMessageReader,
    inbound_mail_trust_boundary,
)
from chorus.domain.ids import IdGenerator, Namespace, Uuid4Generator
from chorus.infrastructure.dynamodb.audit import AuditRepository
from chorus.infrastructure.dynamodb.client import create_dynamodb_client
from chorus.infrastructure.dynamodb.core import CoreRepository
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver
from chorus.infrastructure.dynamodb.idempotency import IdempotencyRepository
from chorus.infrastructure.dynamodb.shareable import ShareableRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.s3.client import create_s3_client
from chorus.infrastructure.s3.objects import S3ObjectStore
from chorus.ports.clock import Clock
from chorus.ports.inbound_mail import InboundMailTransportAuthenticator
from chorus.ports.records import SafeInboundMailConfiguration
from chorus.ports.repositories import CoreRepositoryPort, ShareableRepositoryPort
from chorus.ports.retention import AuditRetention
from chorus.ports.storage import TableName


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundMailSettings:
    """Everything the composition root needs, and nothing it could decide policy from.

    The two digests are configuration and the addresses they cover are not here and never will
    be: the destination-address secret belongs to the sender alone, and this principal must not
    become a second holder of it (ADR-026 § 3).
    """

    region: str
    namespace: Namespace
    core_table: str
    shareable_table: str
    audit_table: str
    private_evidence_bucket: str
    export_evidence_bucket: str
    inbound_transport: str
    inbound_source_arn: str
    inbound_config: SafeInboundMailConfiguration
    from_identity_id: str
    cursor_secret: bytes
    template_version: str = TEMPLATE_VERSION
    dynamodb_endpoint: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundMailComposition:
    """The three objects the entry point holds, and deliberately nothing else.

    No SES port. No agent client. No compiler client. No scheduler client. The absence is the
    design, and a static test reads this class to prove it.
    """

    attester: InboundMailAttester
    verifier: InboundMailEvidenceVerifier
    ingest: IngestExternalReply
    record_rejection: RecordReplyRejection


def build_inbound_mail(
    settings: InboundMailSettings,
    *,
    clock: Clock,
    raw_messages: InboundRawMessageReader,
    authenticator: InboundMailTransportAuthenticator | None = None,
    ids: IdGenerator | None = None,
    core: CoreRepositoryPort | None = None,
    shareable: ShareableRepositoryPort | None = None,
) -> InboundMailComposition:
    """Construct the inbound boundary and the command behind it, over deployed adapters.

    ``authenticator`` defaults to ``None`` and the AWS root leaves it there. The attester and
    the verifier are built by one call so both halves share one process-local key, which is the
    whole mechanism: the only way to obtain a verifiable attestation is to hold the attester,
    and composition gives that to this entry point and to nothing else.
    """

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
    core_repository = core or CoreRepository(driver=driver, cursors=cursors)
    shareable_repository = shareable or ShareableRepository(driver=driver, cursors=cursors)
    generator = ids or Uuid4Generator()
    unit_of_work = StorageUnitOfWork(driver=driver)
    audit = AuditRepository(driver=driver, cursors=cursors, retention=AuditRetention.demo())

    attester, verifier = inbound_mail_trust_boundary(
        transport=settings.inbound_transport,
        source_arn=settings.inbound_source_arn,
        authenticator=authenticator,
        raw_messages=raw_messages,
        core=core_repository,
        shareable=shareable_repository,
        config=settings.inbound_config,
        namespace=settings.namespace,
        from_identity_id=settings.from_identity_id,
        template_version=settings.template_version,
    )
    return InboundMailComposition(
        attester=attester,
        verifier=verifier,
        ingest=IngestExternalReply(
            core=core_repository,
            audit=audit,
            idempotency=IdempotencyRepository(driver=driver, table=TableName.CORE),
            objects=S3ObjectStore(
                client=create_s3_client(region_name=settings.region),
                private_bucket=settings.private_evidence_bucket,
                export_bucket=settings.export_evidence_bucket,
            ),
            unit_of_work=unit_of_work,
            clock=clock,
            ids=generator,
            evidence_trust=verifier,
        ),
        record_rejection=RecordReplyRejection(
            audit=audit, unit_of_work=unit_of_work, clock=clock, ids=generator
        ),
    )


__all__ = ["InboundMailComposition", "InboundMailSettings", "build_inbound_mail"]
