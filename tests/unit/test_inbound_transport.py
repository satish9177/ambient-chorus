"""Unit tests for SNS inbound transport decoding and SES receipt transport authenticator."""

from __future__ import annotations

import json
from typing import Any

import pytest
from functions.inbound_mail.transport import (
    TopicArnMismatchError,
    TransportEventError,
    decode_sns_transport_event,
)

from chorus.infrastructure.ses.inbound_transport import SesReceiptTransportAuthenticator
from chorus.ports.inbound_mail import InboundMailTransportContext

EXPECTED_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:chorus-demo-inbound-receipt"
EXPECTED_RULE_ARN = (
    "arn:aws:ses:us-east-1:123456789012:receipt-rule-set/chorus-demo-inbound"
    ":receipt-rule/chorus-demo-reply"
)


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


def test_decode_sns_transport_event_success() -> None:
    ses_payload = {"notificationType": "Received", "mail": {"source": "alice@example.com"}}
    event = _sns_event(message=json.dumps(ses_payload))

    context = decode_sns_transport_event(
        event,
        expected_topic_arn=EXPECTED_TOPIC_ARN,
        expected_receipt_rule_arn=EXPECTED_RULE_ARN,
    )

    assert isinstance(context, InboundMailTransportContext)
    assert context.transport == "aws:ses-receipt"
    assert context.source_arn == EXPECTED_RULE_ARN
    assert context.envelope == ses_payload


def test_decode_sns_transport_event_rejects_non_mapping() -> None:
    with pytest.raises(TransportEventError, match="must be a mapping"):
        decode_sns_transport_event(
            "not-a-mapping",
            expected_topic_arn=EXPECTED_TOPIC_ARN,
            expected_receipt_rule_arn=EXPECTED_RULE_ARN,
        )


def test_decode_sns_transport_event_rejects_wrong_record_count() -> None:
    with pytest.raises(TransportEventError, match="exactly one Record"):
        decode_sns_transport_event(
            {"Records": []},
            expected_topic_arn=EXPECTED_TOPIC_ARN,
            expected_receipt_rule_arn=EXPECTED_RULE_ARN,
        )
    with pytest.raises(TransportEventError, match="exactly one Record"):
        decode_sns_transport_event(
            {"Records": [{}, {}]},
            expected_topic_arn=EXPECTED_TOPIC_ARN,
            expected_receipt_rule_arn=EXPECTED_RULE_ARN,
        )


def test_decode_sns_transport_event_rejects_wrong_event_source() -> None:
    event = {
        "Records": [
            {
                "EventSource": "aws:sqs",
                "Sns": {"TopicArn": EXPECTED_TOPIC_ARN, "Message": "{}"},
            }
        ]
    }
    with pytest.raises(TransportEventError, match="expected eventSource 'aws:sns'"):
        decode_sns_transport_event(
            event,
            expected_topic_arn=EXPECTED_TOPIC_ARN,
            expected_receipt_rule_arn=EXPECTED_RULE_ARN,
        )


def test_decode_sns_transport_event_rejects_topic_arn_mismatch() -> None:
    event = _sns_event(topic_arn="arn:aws:sns:us-east-1:123456789012:wrong-topic")
    with pytest.raises(TopicArnMismatchError, match="does not match expected"):
        decode_sns_transport_event(
            event,
            expected_topic_arn=EXPECTED_TOPIC_ARN,
            expected_receipt_rule_arn=EXPECTED_RULE_ARN,
        )


def test_decode_sns_transport_event_rejects_non_json_message() -> None:
    event = {
        "Records": [
            {
                "EventSource": "aws:sns",
                "Sns": {"TopicArn": EXPECTED_TOPIC_ARN, "Message": "invalid json {{{"},
            }
        ]
    }
    with pytest.raises(TransportEventError, match="not valid JSON"):
        decode_sns_transport_event(
            event,
            expected_topic_arn=EXPECTED_TOPIC_ARN,
            expected_receipt_rule_arn=EXPECTED_RULE_ARN,
        )


def test_decode_sns_transport_event_rejects_non_object_json_message() -> None:
    event = {
        "Records": [
            {
                "EventSource": "aws:sns",
                "Sns": {"TopicArn": EXPECTED_TOPIC_ARN, "Message": "[1, 2, 3]"},
            }
        ]
    }
    with pytest.raises(TransportEventError, match="must be a JSON object"):
        decode_sns_transport_event(
            event,
            expected_topic_arn=EXPECTED_TOPIC_ARN,
            expected_receipt_rule_arn=EXPECTED_RULE_ARN,
        )


@pytest.mark.anyio
async def test_ses_receipt_transport_authenticator() -> None:
    authenticator = SesReceiptTransportAuthenticator(expected_receipt_rule_arn=EXPECTED_RULE_ARN)

    # Valid
    valid_context = InboundMailTransportContext(
        transport="aws:ses-receipt",
        source_arn=EXPECTED_RULE_ARN,
        envelope={},
    )
    assert await authenticator.authenticate(valid_context) is True

    # Wrong transport
    foreign_transport = InboundMailTransportContext(
        transport="local:fixture",
        source_arn=EXPECTED_RULE_ARN,
        envelope={},
    )
    assert await authenticator.authenticate(foreign_transport) is False

    # Wrong rule ARN
    foreign_arn = InboundMailTransportContext(
        transport="aws:ses-receipt",
        source_arn="arn:aws:ses:us-east-1:999999999999:receipt-rule-set/wrong:receipt-rule/wrong",
        envelope={},
    )
    assert await authenticator.authenticate(foreign_arn) is False
