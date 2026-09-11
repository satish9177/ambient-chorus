"""Deployed SES receipt-rule transport authenticator (ADR-026, ADR-030).

Authenticates that a delivery arrived through the deployment's configured SES receipt rule
transport pair: transport == "aws:ses-receipt" and source_arn == expected_receipt_rule_arn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from chorus.ports.inbound_mail import (
    InboundMailTransportContext,
)

EXPECTED_TRANSPORT: Final = "aws:ses-receipt"


@dataclass(slots=True)
class SesReceiptTransportAuthenticator:
    """Authenticates that an inbound delivery arrived through the configured SES receipt rule."""

    expected_receipt_rule_arn: str

    def __post_init__(self) -> None:
        if not self.expected_receipt_rule_arn:
            raise ValueError("an expected receipt rule ARN is required")

    async def authenticate(self, context: InboundMailTransportContext) -> bool:
        """Return True only when transport is 'aws:ses-receipt' and source_arn matches."""
        if context.transport != EXPECTED_TRANSPORT:
            return False
        return context.source_arn == self.expected_receipt_rule_arn


__all__ = ["EXPECTED_TRANSPORT", "SesReceiptTransportAuthenticator"]
