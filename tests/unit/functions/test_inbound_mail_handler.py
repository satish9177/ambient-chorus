"""Unit tests for the inbound_mail Lambda handler (ADR-030)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from functions.envelope import InvocationFailedError
from functions.inbound_mail.composition import (
    InboundMailComposition,
    InboundMailSettings,
)
from functions.inbound_mail.handler import (
    CLOCK_UNAVAILABLE,
    MALFORMED_TRANSPORT,
    InboundComposition,
    inbound_settings,
    run,
)

from chorus.application.commands.ingest_external_reply import (
    IngestExternalReplyResult,
)
from chorus.application.services.inbound_mail import (
    AttestedInboundReply,
    InboundMailTrustFailure,
    InboundMailUntrusted,
    InboundReplyEvidence,
    InboundReplyRejected,
    InboundReplyRejection,
    ReceiptVerdicts,
)
from chorus.domain.ids import (
    ActionId,
    CaseId,
    CommunityId,
    EvidenceItemId,
    ExecutionId,
    Namespace,
    Sha256Digest,
)
from chorus.infrastructure.persistent_clock import ScopedLogicalClock
from chorus.ports.demo_clock import DemoClockRecord, DemoClockUnavailableError
from chorus.settings import Settings

EXPECTED_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:chorus-demo-inbound-receipt"
EXPECTED_RULE_ARN = (
    "arn:aws:ses:us-east-1:123456789012:receipt-rule-set/chorus-demo-inbound"
    ":receipt-rule/chorus-demo-reply"
)
NOW = datetime(2030, 1, 14, 10, 0, tzinfo=UTC)


class StubClockStore:
    def __init__(self, *, instant: datetime | None = NOW) -> None:
        self.instant = instant
        self.reads = 0

    async def read(self) -> DemoClockRecord:
        self.reads += 1
        if self.instant is None:
            raise DemoClockUnavailableError("no clock record")
        return DemoClockRecord(
            logical_time=self.instant,
            seed_instant=self.instant,
            reset_generation=1,
            advance_count=0,
            version=1,
        )

    async def advance(self, expected: DemoClockRecord, *, to: datetime) -> DemoClockRecord:
        # The inbound handler only ever reads the clock; advance exists to satisfy the port.
        raise AssertionError("the inbound handler must not advance the demo clock")


def _sns_event(*, topic_arn: str = EXPECTED_TOPIC_ARN, message: Any = None) -> dict[str, Any]:
    if message is None:
        message = json.dumps({"notificationType": "Received", "mail": {}, "receipt": {}})
    elif not isinstance(message, str):
        message = json.dumps(message)
    return {
        "Records": [
            {
                "EventSource": "aws:sns",
                "Sns": {
                    "TopicArn": topic_arn,
                    "Message": message,
                },
            }
        ]
    }


def _dummy_evidence() -> InboundReplyEvidence:
    return InboundReplyEvidence(
        namespace=Namespace("DEMO"),
        community_id=CommunityId(uuid4()),
        case_id=CaseId(uuid4()),
        action_id=ActionId(uuid4()),
        execution_id=ExecutionId(uuid4()),
        destination_id="property_manager:demo",
        registry_version=1,
        routing_token=uuid4(),
        inbound_message_id_hash=Sha256Digest("sha256:" + "a" * 64),
        sender_address_digest=Sha256Digest("sha256:" + "b" * 64),
        recipient_address_digest=Sha256Digest("sha256:" + "c" * 64),
        transport="aws:ses-receipt",
        verdicts=ReceiptVerdicts(spf="PASS", dkim="PASS", dmarc="PASS", spam="PASS", virus="PASS"),
        received_at=NOW,
        raw_sha256=Sha256Digest("sha256:" + "d" * 64),
        raw_byte_length=100,
        extracted_text="Schedule confirmed.",
    )


def _setup_mock_graph(
    *, clock_instant: datetime | None = NOW
) -> tuple[InboundComposition, MagicMock, MagicMock, MagicMock]:
    mock_attester = MagicMock()
    mock_ingest = MagicMock()
    mock_record_rejection = MagicMock()

    mock_attester.attest = AsyncMock()
    mock_ingest.execute = AsyncMock()
    mock_record_rejection.execute = AsyncMock()

    comp = InboundMailComposition(
        attester=mock_attester,
        verifier=MagicMock(),
        ingest=mock_ingest,
        record_rejection=mock_record_rejection,
    )
    scope = ScopedLogicalClock()
    clock_store = StubClockStore(instant=clock_instant)

    graph = InboundComposition(
        composition=comp,
        clock_store=clock_store,
        scope=scope,
        expected_topic_arn=EXPECTED_TOPIC_ARN,
        expected_receipt_rule_arn=EXPECTED_RULE_ARN,
        namespace=Namespace("DEMO"),
    )
    return graph, mock_attester, mock_ingest, mock_record_rejection


def test_inbound_settings_mapping() -> None:
    settings = Settings(
        environment="demo",
        namespace="DEMO",
        agent_mode="agentcore",  # the demo-wide invariant every Settings load validates
        aws_region="us-east-1",
        core_table="chorus-core-demo",
        shareable_table="chorus-shareable-demo",
        audit_table="chorus-audit-demo",
        private_evidence_bucket="chorus-private-evidence-demo",
        private_evidence_key_arn="arn:aws:kms:us-east-1:123456789012:key/private-key",
        inbound_source_arn=EXPECTED_RULE_ARN,
        inbound_topic_arn=EXPECTED_TOPIC_ARN,
    )
    inbound_cfg = inbound_settings(settings)

    assert isinstance(inbound_cfg, InboundMailSettings)
    assert inbound_cfg.region == "us-east-1"
    assert inbound_cfg.namespace == Namespace("DEMO")
    assert inbound_cfg.core_table == "chorus-core-demo"
    assert inbound_cfg.private_evidence_bucket == "chorus-private-evidence-demo"
    assert (
        inbound_cfg.private_evidence_key_arn == "arn:aws:kms:us-east-1:123456789012:key/private-key"
    )
    assert (
        inbound_cfg.export_evidence_key_arn == "arn:aws:kms:us-east-1:123456789012:key/private-key"
    )
    assert inbound_cfg.inbound_source_arn == EXPECTED_RULE_ARN


@pytest.mark.anyio
async def test_run_success_path() -> None:
    graph, mock_attester, mock_ingest, _ = _setup_mock_graph()

    evidence = _dummy_evidence()
    attested = AttestedInboundReply(
        evidence=evidence,
        source_arn=EXPECTED_RULE_ARN,
        attestation="hmac-sig",
        raw_mime=b"raw",
    )
    mock_attester.attest.return_value = attested

    evidence_id = EvidenceItemId(uuid4())
    mock_ingest.execute.return_value = IngestExternalReplyResult(
        namespace=Namespace("DEMO"),
        community_id=evidence.community_id,
        case_id=evidence.case_id,
        action_id=evidence.action_id,
        evidence_id=evidence_id,
        case_version=2,
        authorization_version=2,
        replayed=False,
    )

    event = _sns_event()
    result = await run(event, built=graph)

    assert result["status"] == "INGESTED"
    assert result["evidence_id"] == str(evidence_id)
    assert result["replayed"] is False

    assert mock_attester.attest.call_count == 1
    assert mock_ingest.execute.call_count == 1


@pytest.mark.anyio
async def test_run_untrusted_transport_failure() -> None:
    graph, mock_attester, _, mock_record_rejection = _setup_mock_graph()
    mock_attester.attest.side_effect = InboundMailUntrusted(
        InboundMailTrustFailure.INBOUND_VERDICT_FAILED
    )

    event = _sns_event()
    result = await run(event, built=graph)

    assert result["status"] == "REJECTED"
    assert result["reason"] == "INBOUND_VERDICT_FAILED"

    assert mock_record_rejection.execute.call_count == 1
    call_kwargs = mock_record_rejection.execute.call_args.kwargs
    assert call_kwargs["reason_code"] == "INBOUND_VERDICT_FAILED"


@pytest.mark.anyio
async def test_run_reply_rejected() -> None:
    graph, mock_attester, _, mock_record_rejection = _setup_mock_graph()
    mock_attester.attest.side_effect = InboundReplyRejected(
        InboundReplyRejection.REPLY_SENDER_NOT_DESTINATION
    )

    event = _sns_event()
    result = await run(event, built=graph)

    assert result["status"] == "REJECTED"
    assert result["reason"] == "REPLY_SENDER_NOT_DESTINATION"

    assert mock_record_rejection.execute.call_count == 1
    call_kwargs = mock_record_rejection.execute.call_args.kwargs
    assert call_kwargs["reason_code"] == "REPLY_SENDER_NOT_DESTINATION"


@pytest.mark.anyio
async def test_run_malformed_transport() -> None:
    graph, _, _, mock_record_rejection = _setup_mock_graph()
    bad_event = {"not": "valid-sns-event"}

    result = await run(bad_event, built=graph)

    assert result["status"] == "REJECTED"
    assert result["reason"] == MALFORMED_TRANSPORT
    assert mock_record_rejection.execute.call_count == 1


@pytest.mark.anyio
async def test_run_clock_unavailable_raises_invocation_failed_error() -> None:
    graph, _, _, _ = _setup_mock_graph(clock_instant=None)

    event = _sns_event()
    with pytest.raises(InvocationFailedError, match=CLOCK_UNAVAILABLE):
        await run(event, built=graph)
