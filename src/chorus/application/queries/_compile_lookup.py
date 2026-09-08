"""Resolve one view's compile identity from the case's own audit trail.

[08-api-design.md § privacy_counts](../../../../docs/architecture/08-api-design.md) freezes the
mechanism: the compile transaction already writes one audit event carrying
``AuditEntityRef(entity_type="COMPILER_AUDIT_PROJECTION", entity_id=compile_id)`` alongside its
``SHAREABLE_VIEW`` ref, so a view's compile is found by scanning the case's audit events for the
one whose ``SHAREABLE_VIEW`` ref names that view. No new pointer, no new persisted projection --
a read over ``read_case_events``, which the audit page already reads for its own surface.
"""

from __future__ import annotations

from uuid import UUID

from chorus.domain.entities import AuditEvent
from chorus.ports.pagination import Page, PageCursor, PageRequest
from chorus.ports.repositories import AuditRepositoryPort
from chorus.ports.scopes import CaseScope

_SHAREABLE_VIEW = "SHAREABLE_VIEW"
_COMPILER_AUDIT_PROJECTION = "COMPILER_AUDIT_PROJECTION"


async def find_compile_id_for_view(
    audit: AuditRepositoryPort, scope: CaseScope, view_id: UUID
) -> UUID | None:
    """Find the ``compile_id`` of the compile that produced ``view_id``, or ``None``."""

    cursor: PageCursor | None = None
    while True:
        page: Page[AuditEvent] = await audit.read_case_events(scope, PageRequest(cursor=cursor))
        for event in page.items:
            view_ref = None
            compile_ref = None
            for ref in event.entity_refs:
                if ref.entity_type == _SHAREABLE_VIEW:
                    view_ref = ref
                elif ref.entity_type == _COMPILER_AUDIT_PROJECTION:
                    compile_ref = ref
            if view_ref is not None and view_ref.entity_id == view_id and compile_ref is not None:
                return compile_ref.entity_id
        if page.next_cursor is None:
            return None
        cursor = page.next_cursor


__all__ = ["find_compile_id_for_view"]
