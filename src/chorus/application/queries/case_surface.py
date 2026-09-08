"""The five sections Phase 10 completes on ``GET /v1/cases/{case_id}``.

[08-api-design.md § Case surfaces](../../../../docs/architecture/08-api-design.md) freezes each
source: every field here is a read over a repository method that already exists, and none of
them adds domain state, a persisted projection, a new pointer, or a write path.

``current_action`` (Phase 7's ``ReadCurrentAction``) is unchanged and is not reproduced here --
the transport route composes this projection beside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from chorus.application.queries._compile_lookup import find_compile_id_for_view
from chorus.application.queries.compile_audit import ReadCompileExplanation
from chorus.domain.entities import CaseState, CommitmentStatus, EvidenceStatus, FactType
from chorus.domain.facts import FactStatus
from chorus.ports.pagination import PageRequest
from chorus.ports.records import CommitmentScheduleStatus, StoredShareableView
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    ShareableRepositoryPort,
)
from chorus.ports.scopes import CaseScope


@dataclass(frozen=True, slots=True, kw_only=True)
class CaseHeaderView:
    case_id: UUID
    title: str | None
    issue_type: str
    state: CaseState
    version: int
    authorization_version: int
    corroboration_source_count: int
    state_reason_code: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceSummaryRow:
    fact_id: UUID
    fact_type: FactType
    sensitivity: str
    evidence_status: EvidenceStatus
    status: FactStatus
    contributor_id: UUID
    evidence_ids: tuple[UUID, ...]
    version: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitmentSafeView:
    commitment_id: UUID
    action_id: UUID | None
    obligor: str
    action_text: str
    due_at: str
    verification_method: str
    status: CommitmentStatus
    schedule_generation: int
    version: int
    verified_by_contributor_id: UUID | None
    outcome_note: str | None
    created_at: str
    updated_at: str
    schedule_status: CommitmentScheduleStatus | None
    """Whether the alarm clock exists yet -- ``None`` only if no schedule row was ever staged.

    Surfaced (P2-9) so the demo can visibly prove the commitment is being watched, without
    exposing ``schedule_name`` (transport addressing) or the due-event/replay identities the
    watcher itself authenticates against.
    """
    schedule_last_error_code: str | None
    """The one safe, closed code from the schedule row's own validated field, or ``None``.

    Never a message, never a stack trace: ``CommitmentScheduleProjection`` cannot hold
    anything else here (its own constructor rejects a value that is not a safe closed code).
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class PrivacyCountsView:
    compile_id: UUID
    included: int
    excluded: int
    denied_by_reason: dict[str, int]


@dataclass(frozen=True, slots=True, kw_only=True)
class CaseSurfaceExtras:
    """The sections this query adds; the route merges them beside ``current_action``."""

    case: CaseHeaderView
    evidence_summary: tuple[EvidenceSummaryRow, ...]
    current_shareable_view: StoredShareableView | None
    commitments: tuple[CommitmentSafeView, ...]
    privacy_counts: PrivacyCountsView | None


@dataclass(slots=True)
class ReadCaseSurface:
    """The Phase 10 read half of ``GET /cases/{case_id}``, for both readable personas.

    Persona-dependent filtering (``case.title`` and ``privacy_counts`` are presenter-only, and
    ``evidence_summary`` is presenter-only) happens in the transport route, which already knows
    which persona is asking; this query always returns the widest, presenter-eligible shape.
    """

    core: CoreRepositoryPort
    shareable: ShareableRepositoryPort
    audit: AuditRepositoryPort

    async def execute(self, scope: CaseScope) -> CaseSurfaceExtras:
        case = await self.core.read_case_for_display(scope)
        evidence_summary = await self._evidence_summary(scope)
        current_view = None
        privacy_counts = None
        pointer = await self.shareable.load_current_view_pointer(scope)
        if pointer is not None:
            current_view = await self.shareable.load_view(scope, pointer.view_id)
            compile_id = await find_compile_id_for_view(self.audit, scope, pointer.view_id.value)
            if compile_id is not None:
                explanation = await ReadCompileExplanation(audit=self.audit).read(scope, compile_id)
                if explanation is not None:
                    denied_by_reason: dict[str, int] = {}
                    for fact in explanation.facts:
                        if fact.included:
                            continue
                        for reason in fact.reason_codes:
                            denied_by_reason[reason] = denied_by_reason.get(reason, 0) + 1
                    privacy_counts = PrivacyCountsView(
                        compile_id=explanation.compile_id,
                        included=explanation.included_count,
                        excluded=explanation.excluded_count,
                        denied_by_reason=denied_by_reason,
                    )
        commitments = await self._commitments(scope)
        return CaseSurfaceExtras(
            case=CaseHeaderView(
                case_id=case.case_id.value,
                title=case.title,
                issue_type=case.issue_type,
                state=case.state,
                version=case.version,
                authorization_version=case.authorization_version,
                corroboration_source_count=case.corroboration_source_count,
                state_reason_code=case.state_reason_code,
            ),
            evidence_summary=evidence_summary,
            current_shareable_view=current_view,
            commitments=commitments,
            privacy_counts=privacy_counts,
        )

    async def _evidence_summary(self, scope: CaseScope) -> tuple[EvidenceSummaryRow, ...]:
        rows: list[EvidenceSummaryRow] = []
        request = PageRequest()
        while True:
            page = await self.core.read_case_facts(scope, request)
            rows.extend(
                EvidenceSummaryRow(
                    fact_id=fact.fact_id.value,
                    fact_type=fact.fact_type,
                    sensitivity=fact.sensitivity.value,
                    evidence_status=fact.evidence_status,
                    status=fact.status,
                    contributor_id=fact.contributor_id.value,
                    evidence_ids=tuple(item.value for item in fact.evidence_ids),
                    version=fact.version,
                )
                for fact in page.items
            )
            if page.next_cursor is None:
                return tuple(rows)
            request = PageRequest(cursor=page.next_cursor)

    async def _commitments(self, scope: CaseScope) -> tuple[CommitmentSafeView, ...]:
        rows: list[CommitmentSafeView] = []
        request = PageRequest()
        while True:
            page = await self.shareable.read_case_commitments(scope, request)
            for item in page.items:
                schedule = await self.shareable.load_commitment_schedule(scope, item.commitment_id)
                rows.append(
                    CommitmentSafeView(
                        commitment_id=item.commitment_id.value,
                        action_id=None if item.action_id is None else item.action_id.value,
                        obligor=item.obligor,
                        action_text=item.action_text,
                        due_at=item.due_at.isoformat(),
                        verification_method=item.verification_method,
                        status=item.status,
                        schedule_generation=item.schedule_generation,
                        version=item.version,
                        verified_by_contributor_id=(
                            None
                            if item.verified_by_contributor_id is None
                            else item.verified_by_contributor_id.value
                        ),
                        outcome_note=item.outcome_note,
                        created_at=item.created_at.isoformat(),
                        updated_at=item.updated_at.isoformat(),
                        schedule_status=None if schedule is None else schedule.status,
                        schedule_last_error_code=(
                            None if schedule is None else schedule.last_error_code
                        ),
                    )
                )
            if page.next_cursor is None:
                return tuple(rows)
            request = PageRequest(cursor=page.next_cursor)


__all__ = [
    "CaseHeaderView",
    "CaseSurfaceExtras",
    "CommitmentSafeView",
    "EvidenceSummaryRow",
    "PrivacyCountsView",
    "ReadCaseSurface",
]
