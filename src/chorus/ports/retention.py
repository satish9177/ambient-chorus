"""Retention policies chosen by a composition root, never by a domain entity.

An audit event is the same immutable record in every environment; how long a *deployment*
keeps it is a property of that deployment. Expressing the choice as an injected value keeps
environment branching out of the entity and out of the codec, and makes "this environment
does not expire audit records" a thing a test can state directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from chorus.domain.time import epoch_seconds_ceiling
from chorus.ports.limits import AUDIT_TTL_SECONDS


@dataclass(frozen=True, slots=True)
class AuditRetention:
    """How long persisted audit events live in one environment.

    ``ttl_seconds`` of ``None`` means the deployment keeps audit events until an operator
    removes them, and no TTL attribute is written at all. Expiry is cleanup and never
    authorization: whether a historical decision was authorized does not change when its
    audit row is swept.
    """

    ttl_seconds: int | None

    def __post_init__(self) -> None:
        if self.ttl_seconds is not None and self.ttl_seconds < 1:
            raise ValueError("audit retention must be positive when expiry is enabled")

    @classmethod
    def demo(cls) -> AuditRetention:
        """The frozen 90-day demo retention."""

        return cls(AUDIT_TTL_SECONDS)

    @classmethod
    def durable(cls) -> AuditRetention:
        """Keep audit events indefinitely; no TTL attribute is written."""

        return cls(None)

    def expires_at_epoch(self, occurred_at: datetime) -> int | None:
        """Return the TTL attribute value, or ``None`` when this environment writes none."""

        if self.ttl_seconds is None:
            return None
        return epoch_seconds_ceiling(occurred_at) + self.ttl_seconds
