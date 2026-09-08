"""``GET /v1/cases/{case_id}/audit``: the safe, paginated audit trail.

A read over ``AuditRepositoryPort.read_case_events`` and nothing else -- no new persisted
projection, no write path. ``actor_id_hash`` and ``idempotency_key_hash`` are omitted from the
projection type itself, which is what makes "no raw payload, no cross-persona correlation
channel" a property of the shape rather than a review promise: there is nowhere on
``AuditEventPage`` to put either one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from chorus.domain.entities import ActorType, AuditDecision, AuditEvent
from chorus.ports.pagination import Page, PageCursor, PageRequest
from chorus.ports.repositories import AuditRepositoryPort
from chorus.ports.scopes import CaseScope


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEntityRefView:
    entity_type: str
    entity_id: UUID
    version: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditDetailsView:
    count: int | None
    rule_id: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEventView:
    audit_event_id: UUID
    event_type: str
    occurred_at: datetime
    actor_type: ActorType
    decision: AuditDecision
    reason_codes: tuple[str, ...]
    entity_refs: tuple[AuditEntityRefView, ...]
    safe_details: AuditDetailsView
    correlation_id: UUID
    causation_id: UUID | None
    input_hash: str | None
    output_hash: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEventPage:
    items: tuple[AuditEventView, ...]
    next_cursor: PageCursor | None


@dataclass(slots=True)
class ReadCaseAudit:
    """Presenter-only. Pages ``read_case_events`` in occurrence order, safely."""

    audit: AuditRepositoryPort

    async def execute(self, scope: CaseScope, request: PageRequest) -> AuditEventPage:
        page: Page[AuditEvent] = await self.audit.read_case_events(scope, request)
        return AuditEventPage(
            items=tuple(_project(event) for event in page.items),
            next_cursor=page.next_cursor,
        )


def _project(event: AuditEvent) -> AuditEventView:
    return AuditEventView(
        audit_event_id=event.audit_event_id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        actor_type=event.actor_type,
        decision=event.decision,
        reason_codes=event.reason_codes,
        entity_refs=tuple(
            AuditEntityRefView(
                entity_type=ref.entity_type, entity_id=ref.entity_id, version=ref.version
            )
            for ref in event.entity_refs
        ),
        safe_details=AuditDetailsView(
            count=event.safe_details.count,
            rule_id=event.safe_details.rule_id,
        ),
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        input_hash=None if event.input_hash is None else event.input_hash.value,
        output_hash=None if event.output_hash is None else event.output_hash.value,
    )


__all__ = [
    "AuditDetailsView",
    "AuditEntityRefView",
    "AuditEventPage",
    "AuditEventView",
    "ReadCaseAudit",
]
