"""The inbound mail Lambda entry point: bridge SNS delivery to the trust boundary (ADR-030).

Receives SNS notifications published by the SES receipt rule's S3Action TopicArn.
Validates outer transport, authenticates via SES receipt rule ARN, reads pinned raw MIME
from S3, attests via InboundMailAttester, and ingests via IngestExternalReply.

On rejection or trust failure: writes a closed audit rejection row (never containing
sensitive content, addresses, or raw MIME) and returns a structured REJECTED envelope.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final
from uuid import uuid4

import anyio

from chorus.application.commands.ingest_external_reply import IngestExternalReplyCommand
from chorus.application.services.inbound_mail import (
    InboundMailUntrusted,
    InboundReplyRejected,
    InboundReplyRejection,
    address_digest,
)
from chorus.domain.entities import DestinationKind
from chorus.domain.ids import DestinationId, Namespace, Sha256Digest
from chorus.infrastructure.dynamodb.client import create_dynamodb_client
from chorus.infrastructure.dynamodb.demo_clock import DynamoDbDemoClockStore
from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver
from chorus.infrastructure.persistent_clock import ScopedLogicalClock
from chorus.infrastructure.s3.client import create_s3_client
from chorus.infrastructure.s3.inbound_raw import S3InboundRawMessageReader
from chorus.infrastructure.ses.inbound_transport import SesReceiptTransportAuthenticator
from chorus.ports.demo_clock import DemoClockError, DemoClockStorePort
from chorus.ports.errors import NotFoundError
from chorus.ports.records import SafeInboundMailConfiguration, StoredSafeDestination
from chorus.ports.storage import TableName
from chorus.settings import Settings
from functions.envelope import InvocationFailedError
from functions.inbound_mail.composition import (
    InboundMailComposition,
    InboundMailSettings,
    build_inbound_mail,
)
from functions.inbound_mail.transport import (
    TopicArnMismatchError,
    TransportEventError,
    decode_sns_transport_event,
)

CLOCK_UNAVAILABLE: Final = "CLOCK_UNAVAILABLE"
MALFORMED_TRANSPORT: Final = "MALFORMED_TRANSPORT"
_ACTOR_ID_HASH: Final = Sha256Digest(f"sha256:{sha256(b'aws:ses-receipt').hexdigest()}")


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundComposition:
    """The complete graph for inbound delivery handling."""

    composition: InboundMailComposition
    clock_store: DemoClockStorePort
    scope: ScopedLogicalClock
    expected_topic_arn: str
    expected_receipt_rule_arn: str
    namespace: Namespace


_composition: InboundComposition | None = None


def inbound_settings(settings: Settings) -> InboundMailSettings:
    """Map global settings onto InboundMailSettings."""
    namespace = Namespace(settings.namespace)
    destination = StoredSafeDestination(
        destination_id=DestinationId(settings.destination_id),
        kind=DestinationKind.PROPERTY_MANAGER,
        registry_version=settings.destination_registry_version,
        routing_token=settings.destination_routing_token,
        display_label=settings.destination_display_label,
    )

    # The two ADR-030 §§ 5-6 comparison digests. ``inbound_address_digest`` is derived from the
    # configured receiving mailbox when it is not set explicitly -- that derivation is exact, so
    # a deploy that configures ``CHORUS_INBOUND_RECEIVING_ADDRESS`` need not also configure the
    # digest. ``destination_address_digest`` has no such derivation: it is the digest of the
    # correspondent's mailbox, which this principal must never hold in cleartext, so it is a
    # required deploy input (deployment contract § 13; canary L / prerequisite P2). Absent, it
    # falls back to the digest of an ``@invalid`` sentinel -- which no authenticated ``From``
    # mailbox can ever equal, so agreement 4 fails closed and no reply is admitted until the
    # real digest is configured.
    if settings.destination_address_digest:
        dest_digest = Sha256Digest(settings.destination_address_digest)
    else:
        dest_digest = address_digest(namespace, "unconfigured-correspondent@invalid")

    if settings.inbound_address_digest:
        inbound_digest = Sha256Digest(settings.inbound_address_digest)
    elif settings.inbound_receiving_address:
        inbound_digest = address_digest(namespace, settings.inbound_receiving_address)
    else:
        inbound_digest = address_digest(namespace, "unconfigured-inbound@invalid")

    inbound_config = SafeInboundMailConfiguration(
        destination=destination,
        destination_address_digest=dest_digest,
        inbound_address_digest=inbound_digest,
    )

    private_key_arn = settings.private_evidence_key_arn
    export_key_arn = settings.export_evidence_key_arn or private_key_arn
    export_bucket = settings.export_evidence_bucket or settings.private_evidence_bucket

    return InboundMailSettings(
        region=settings.aws_region,
        namespace=namespace,
        core_table=settings.core_table,
        shareable_table=settings.shareable_table,
        audit_table=settings.audit_table,
        private_evidence_bucket=settings.private_evidence_bucket,
        export_evidence_bucket=export_bucket,
        inbound_transport=settings.inbound_transport,
        inbound_source_arn=settings.inbound_source_arn or "",
        inbound_config=inbound_config,
        from_identity_id=settings.ses_from_identity_id,
        cursor_secret=secrets.token_bytes(32),
        private_evidence_key_arn=private_key_arn,
        export_evidence_key_arn=export_key_arn,
        dynamodb_endpoint=(
            str(settings.dynamodb_endpoint) if settings.dynamodb_endpoint is not None else None
        ),
    )


def composition() -> InboundComposition:
    """Build the inbound handler graph once per execution environment."""
    global _composition
    if _composition is None:
        settings = Settings.load()
        inbound_cfg = inbound_settings(settings)
        driver = DynamoDbStorageDriver(
            client=create_dynamodb_client(
                region_name=inbound_cfg.region, endpoint_url=inbound_cfg.dynamodb_endpoint
            ),
            table_names={
                TableName.CORE: inbound_cfg.core_table,
                TableName.SHAREABLE: inbound_cfg.shareable_table,
                TableName.AUDIT: inbound_cfg.audit_table,
            },
        )
        scope = ScopedLogicalClock()
        clock_store = DynamoDbDemoClockStore(driver=driver, namespace=inbound_cfg.namespace)
        s3_client = create_s3_client(region_name=inbound_cfg.region)
        raw_messages = S3InboundRawMessageReader(
            client=s3_client, expected_bucket=inbound_cfg.private_evidence_bucket
        )
        authenticator = SesReceiptTransportAuthenticator(
            expected_receipt_rule_arn=inbound_cfg.inbound_source_arn
        )
        comp = build_inbound_mail(
            inbound_cfg,
            clock=scope,
            raw_messages=raw_messages,
            authenticator=authenticator,
        )
        _composition = InboundComposition(
            composition=comp,
            clock_store=clock_store,
            scope=scope,
            expected_topic_arn=settings.inbound_topic_arn or "",
            expected_receipt_rule_arn=inbound_cfg.inbound_source_arn,
            namespace=inbound_cfg.namespace,
        )
    return _composition


async def run(event: object, *, built: InboundComposition | None = None) -> dict[str, Any]:
    """Process one inbound SNS event through clock scoping and the trust boundary."""
    graph = built or composition()
    try:
        record = await graph.clock_store.read()
    except DemoClockError as error:
        raise InvocationFailedError(CLOCK_UNAVAILABLE) from error

    correlation_id = uuid4()

    with graph.scope.bound_to(record.logical_time):
        try:
            transport_context = decode_sns_transport_event(
                event,
                expected_topic_arn=graph.expected_topic_arn,
                expected_receipt_rule_arn=graph.expected_receipt_rule_arn,
            )
            attested = await graph.composition.attester.attest(transport_context)
            result = await graph.composition.ingest.execute(
                IngestExternalReplyCommand(
                    attested=attested,
                    actor_id_hash=_ACTOR_ID_HASH,
                    correlation_id=correlation_id,
                )
            )
            return {
                "status": "INGESTED",
                "evidence_id": str(result.evidence_id),
                "replayed": result.replayed,
            }
        except (InboundMailUntrusted, InboundReplyRejected) as error:
            reason = error.safe_code
            await _record_rejection_safe(graph, reason, correlation_id)
            return {"status": "REJECTED", "reason": reason}
        except (TransportEventError, TopicArnMismatchError):
            reason = MALFORMED_TRANSPORT
            await _record_rejection_safe(graph, reason, correlation_id)
            return {"status": "REJECTED", "reason": reason}
        except NotFoundError:
            reason = InboundReplyRejection.MALFORMED_ENVELOPE.value
            await _record_rejection_safe(graph, reason, correlation_id)
            return {"status": "REJECTED", "reason": reason}


async def _record_rejection_safe(
    graph: InboundComposition, reason: str, correlation_id: Any
) -> None:
    """Write the one ``reply.rejected`` audit row a refused delivery earns (ADR-030 § 8).

    The rejection decision has already been made and returned to the caller by the time this
    runs. A failure to persist the audit row must not turn a correct refusal into a raised
    exception (which SNS would retry) -- the row is a record, not the decision -- so the audit
    failure is swallowed here. It is logged at ``warning`` with a bounded code and no content;
    nothing about the refused delivery (address, header, body, raw MIME) is ever in a log line.
    """

    try:
        await graph.composition.record_rejection.execute(
            namespace=graph.namespace,
            actor_id_hash=_ACTOR_ID_HASH,
            correlation_id=correlation_id,
            reason_code=reason,
        )
    except Exception:
        # The refusal has already been decided and returned. Re-raising here would turn a
        # correct rejection into an SNS retry loop; the missing audit row is the lesser cost.
        return


def handler(event: object, context: object = None) -> dict[str, Any]:
    """The Lambda entry point. One SNS event, one logical clock read, one ingestion."""
    return anyio.run(run, event)


__all__ = [
    "CLOCK_UNAVAILABLE",
    "MALFORMED_TRANSPORT",
    "InboundComposition",
    "composition",
    "handler",
    "inbound_settings",
    "run",
]
