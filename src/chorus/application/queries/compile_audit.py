"""Read one compile's private lineage for the authorized investigation surface.

The safe view deliberately forgets which private fact became which exported fact. This query is
where an authorized presenter gets that back, and it is a private-zone read: every value it
returns is an identifier or a closed code, and the caller must already be entitled to see a
fact identifier at all.

It is a read and only a read. There is no method here that creates, amends, or reinterprets a
projection -- the row is written once, inside the compile transaction, by the principal that
made the decision it records.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from chorus.domain.entities import DisclosureScope
from chorus.domain.ids import EvidenceItemId, ExportFactId, FactId, Sha256Digest, ViewId
from chorus.ports.records import (
    CompileDecisionOutcome,
    CompileItemOutcome,
    CompilerAuditProjection,
)
from chorus.ports.repositories import AuditRepositoryPort
from chorus.ports.scopes import CaseScope


@dataclass(frozen=True, slots=True, kw_only=True)
class FactDisclosureExplanation:
    """Why one requested fact travelled, or why it did not."""

    fact_id: FactId
    granted_scope: DisclosureScope | None
    included: bool
    reason_codes: tuple[str, ...]
    export_fact_ids: tuple[ExportFactId, ...]
    transformation_rule_id: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceDisclosureExplanation:
    """Which source evidence became which safe reference, and by which digest.

    The bucket and the key are absent by construction: the export object's address *is* the
    digest, so an authorized private reader can find it without this row storing a locator.
    """

    source_evidence_id: EvidenceItemId
    included: bool
    reason_codes: tuple[str, ...]
    export_handle_id: UUID | None
    derivative_sha256: Sha256Digest | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CompileExplanation:
    """One compile, as the private investigation surface shows it."""

    compile_id: UUID
    decision: CompileDecisionOutcome
    based_on_case_version: int
    policy_version: str
    compiler_version: str
    reason_codes: tuple[str, ...]
    view_id: ViewId | None
    view_hash: Sha256Digest | None
    gates: tuple[tuple[int, str, str, tuple[str, ...]], ...]
    facts: tuple[FactDisclosureExplanation, ...]
    evidence: tuple[EvidenceDisclosureExplanation, ...]

    @property
    def included_count(self) -> int:
        return sum(1 for fact in self.facts if fact.included)

    @property
    def excluded_count(self) -> int:
        return sum(1 for fact in self.facts if not fact.included)


@dataclass(slots=True)
class ReadCompileExplanation:
    """Load one compile's private lineage, or report that there is none."""

    audit: AuditRepositoryPort

    async def read(self, scope: CaseScope, compile_id: UUID) -> CompileExplanation | None:
        projection = await self.audit.load_compile_projection(scope, compile_id)
        if projection is None:
            return None
        return _explain(projection)


def _explain(projection: CompilerAuditProjection) -> CompileExplanation:
    return CompileExplanation(
        compile_id=projection.compile_id,
        decision=projection.decision,
        based_on_case_version=projection.based_on_case_version,
        policy_version=projection.policy_version,
        compiler_version=projection.compiler_version,
        reason_codes=projection.reason_codes,
        view_id=projection.view_id,
        view_hash=projection.view_hash,
        gates=tuple(
            (record.gate, record.gate_name, record.outcome, record.reason_codes)
            for record in projection.gates
        ),
        facts=tuple(
            FactDisclosureExplanation(
                fact_id=record.fact_id,
                granted_scope=record.granted_scope,
                included=record.outcome is CompileItemOutcome.INCLUDED,
                reason_codes=record.reason_codes,
                export_fact_ids=record.export_fact_ids,
                transformation_rule_id=record.transformation_rule_id,
            )
            for record in projection.facts
        ),
        evidence=tuple(
            EvidenceDisclosureExplanation(
                source_evidence_id=record.source_evidence_id,
                included=record.outcome is CompileItemOutcome.INCLUDED,
                reason_codes=record.reason_codes,
                export_handle_id=record.export_handle_id,
                derivative_sha256=record.derivative_sha256,
            )
            for record in projection.evidence
        ),
    )
