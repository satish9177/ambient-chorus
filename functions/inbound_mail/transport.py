"""SNS -> SES transport decoder for inbound mail Lambda (ADR-030 § 2).

Validates the incoming SNS event:
- EventSource == "aws:sns"
- Records contains exactly one record
- TopicArn matches expected_topic_arn (consistency check)
- Sns.Message is well-formed JSON representing SES receipt notification

Produces an InboundMailTransportContext:
- transport = "aws:ses-receipt"
- source_arn = expected_receipt_rule_arn
- envelope = parsed SES JSON
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from chorus.ports.inbound_mail import InboundMailTransportContext


class TransportEventError(ValueError):
    """The incoming transport event is malformed or invalid."""


class TopicArnMismatchError(TransportEventError):
    """The SNS TopicArn does not match the configured ingress topic ARN."""


def decode_sns_transport_event(
    event: Mapping[str, Any] | object,
    *,
    expected_topic_arn: str,
    expected_receipt_rule_arn: str,
) -> InboundMailTransportContext:
    """Validate SNS event and produce InboundMailTransportContext."""
    if not isinstance(event, Mapping):
        raise TransportEventError("event must be a mapping")

    records = event.get("Records")
    if not isinstance(records, list) or len(records) != 1:
        raise TransportEventError("event must contain exactly one Record")

    record = records[0]
    if not isinstance(record, Mapping):
        raise TransportEventError("record must be a mapping")

    event_source = record.get("EventSource") or record.get("eventSource")
    if event_source != "aws:sns":
        raise TransportEventError(f"expected eventSource 'aws:sns', got {event_source!r}")

    sns_data = record.get("Sns") or record.get("sns")
    if not isinstance(sns_data, Mapping):
        raise TransportEventError("record missing Sns payload")

    topic_arn = sns_data.get("TopicArn") or sns_data.get("topicArn")
    if not topic_arn or topic_arn != expected_topic_arn:
        raise TopicArnMismatchError(
            f"SNS TopicArn {topic_arn!r} does not match expected {expected_topic_arn!r}"
        )

    message_raw = sns_data.get("Message") or sns_data.get("message")
    if not isinstance(message_raw, str):
        raise TransportEventError("SNS Message must be a string")

    try:
        envelope = json.loads(message_raw)
    except Exception as error:
        raise TransportEventError("SNS Message is not valid JSON") from error

    if not isinstance(envelope, Mapping):
        raise TransportEventError("SES notification must be a JSON object")

    return InboundMailTransportContext(
        transport="aws:ses-receipt",
        source_arn=expected_receipt_rule_arn,
        envelope=envelope,
    )


__all__ = [
    "TopicArnMismatchError",
    "TransportEventError",
    "decode_sns_transport_event",
]
