"""The investigation transport: start one skeptical review of one case.

The body is ``{expected_case_version, reason}`` and nothing else. It names no fact, no report,
no evidence, and no finding, because the Investigator's payload is assembled by the application
from the case at that version -- a client that could name what the model reads would be doing
the investigating.

The response is ``202``. Invoking a model is not something an HTTP request should hold a
connection open for, so the caller polls an operation. What the operation eventually reports is
a status and a result reference; the assessment itself is private and is read through the
authorized case surface.

A stale ``expected_case_version`` is ``409`` with nothing written. The check happens here so a
caller learns immediately, and it happens *again* inside the worker against a strong read, and a
third time as the apply transaction's version condition -- because the case can move between any
two of those points and an assessment bound to a version that no longer exists must never be
applied.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from chorus.application.commands.run_investigation import InvestigationReason
from chorus.application.operations import (
    StartedOperation,
    StartReservation,
    investigate_binding_hash,
)
from chorus.application.queries.investigation import InvestigationProjectionResponse
from chorus.application.services.mandate_terms import key_hash
from chorus.domain.entities import ApplicationOperationKind, ApplicationOperationStatus
from chorus.domain.ids import CaseId, Sha256Digest
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.idempotency import IdempotentCommand
from chorus.ports.operations import InvestigationOperationJob
from chorus.ports.scopes import CaseScope
from chorus_api.dependencies import (
    ApiContainer,
    DemoActor,
    actor_id_hash,
    container_of,
    require_actor,
    require_presenter,
)

router = APIRouter(tags=["investigations"])

IdempotencyKeyStr = Annotated[
    str, StringConstraints(min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$")
]


class TransportRequest(BaseModel):
    """A closed HTTP request body; a field nobody declared can never ride along."""

    model_config = ConfigDict(extra="forbid")


class StartInvestigationRequest(TransportRequest):
    expected_case_version: Annotated[int, Field(ge=1)]
    reason: Literal["INITIAL", "NEW_EVIDENCE", "REOPEN"]


class OperationReference(BaseModel):
    operation_id: UUID
    status: str
    poll_url: str


@router.post("/cases/{case_id}/investigations", status_code=202, response_model=OperationReference)
async def start_investigation(
    request: Request,
    response: Response,
    case_id: UUID,
    body: StartInvestigationRequest,
    idempotency_key: Annotated[IdempotencyKeyStr, Header(alias="Idempotency-Key")],
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> OperationReference:
    """Create one ``INVESTIGATE`` operation and hand it over. No model is called here."""

    require_presenter(actor)
    container: ApiContainer = container_of(request)
    actor_hash: Sha256Digest = actor_id_hash(actor)
    identity = CaseId(case_id)
    reason = InvestigationReason(body.reason)

    binding = investigate_binding_hash(
        case_id=identity,
        expected_case_version=body.expected_case_version,
        reason=reason.value,
    )
    reserved = await container.operations.reserve_start(
        namespace=container.namespace,
        command=IdempotentCommand.APPLY_INVESTIGATION,
        actor_id_hash=actor_hash,
        key_hash=_key_hash(idempotency_key),
        # The binding digest doubles as this command's request hash. The two cover the same
        # three values -- case, expected version, and reason -- because that is exactly what
        # the request *is*, and deriving one from the other keeps them from ever describing
        # different requests under one key.
        request_hash=binding,
        correlation_id=request.state.correlation_id,
    )
    operation = await _start_investigation_operation(
        container=container,
        case_id=identity,
        actor_hash=actor_hash,
        reserved=reserved,
        binding=binding,
        expected_case_version=body.expected_case_version,
        reason=reason,
        idempotency_key=idempotency_key,
        correlation_id=request.state.correlation_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return operation


async def _start_investigation_operation(
    *,
    container: ApiContainer,
    case_id: CaseId,
    actor_hash: Sha256Digest,
    reserved: StartReservation | StartedOperation,
    binding: Sha256Digest,
    expected_case_version: int,
    reason: InvestigationReason,
    idempotency_key: str,
    correlation_id: UUID,
) -> OperationReference:
    """Complete this request's reservation into a durable operation, or answer from the record.

    The operation is created carrying its **agent handover identity**: the invocation it
    authorizes and the digest of the exact work that invocation may do. Both are written before
    dispatch and before the first model call, which is what lets the worker refuse a misrouted
    *first* delivery -- the one delivery that would otherwise have no durable record to disagree
    with, and could therefore present a fresh invocation identity, find no invocation record,
    and spend a second model pass over the same private case.

    A replay that finds the operation still ``PENDING`` dispatches it **again**, for the same
    reason ingestion does: dispatch is the one step after the durable record that can fail on
    its own, and an operation whose only delivery was lost would otherwise sit ``PENDING``
    forever. The worker's conditional claim, not the dispatcher, is where duplicate execution is
    actually prevented.
    """

    if isinstance(reserved, StartReservation):
        started = await container.operations.complete_start(
            reserved,
            namespace=container.namespace,
            kind=ApplicationOperationKind.INVESTIGATE,
            actor_id_hash=actor_hash,
            case_id=case_id,
            agent_binding_hash=binding,
            correlation_id=correlation_id,
        )
    else:
        started = reserved
    if started.operation.case_id != case_id:
        # The key is bound to another case's investigation. Answering with that operation would
        # tell this caller their case is being investigated when it is not.
        raise PersistenceConflictError("APPLICATION_OPERATION")
    if started.operation.status is ApplicationOperationStatus.PENDING:
        await container.dispatcher.dispatch_investigation(
            InvestigationOperationJob(
                operation_id=started.operation.operation_id,
                namespace=container.namespace,
                community_id=container.community_id,
                case_id=case_id,
                invocation_id=started.invocation_id,
                correlation_id=correlation_id,
                actor_id_hash=actor_hash,
                request_hash=started.operation.request_hash,
                expected_case_version=expected_case_version,
                reason=reason.value,
                idempotency_key=idempotency_key,
            )
        )
    return OperationReference(
        operation_id=started.operation.operation_id.value,
        status=started.operation.status.value,
        poll_url=f"/v1/operations/{started.operation.operation_id}",
    )


def _key_hash(idempotency_key: str) -> Sha256Digest:
    """Hash the caller's key, because caller text never enters a storage key."""

    return key_hash(f"investigate-start\x1f{idempotency_key}")


# -- GET /cases/{case_id}/investigation: the private investigation surface -------------------


class FactResponse(BaseModel):
    fact_id: UUID
    fact_type: str
    sensitivity: str
    value_preview: str
    evidence_status: str
    status: str
    contributor_id: UUID
    evidence_ids: tuple[UUID, ...]
    source_message_ids: tuple[UUID, ...]
    version: int


class ReportResponse(BaseModel):
    report_id: UUID
    contributor_id: UUID
    status: str
    created_at: str


class FindingResponse(BaseModel):
    fact_id: UUID
    evidence_status: str
    reason_code: str


class ContradictionResponse(BaseModel):
    statement_fact_ids: tuple[UUID, ...]
    description: str
    materiality: str


class AlternativeExplanationResponse(BaseModel):
    description: str
    cited_report_ids: tuple[UUID, ...]
    cited_fact_ids: tuple[UUID, ...]
    cited_evidence_ids: tuple[UUID, ...]


class AssessmentResponse(BaseModel):
    assessment_id: UUID
    based_on_case_version: int
    linkage_decision: str
    independent_source_count: int
    is_corroborated: bool
    recommended_disposition: str
    assessment_hash: str
    created_at: str
    findings: tuple[FindingResponse, ...]
    contradictions: tuple[ContradictionResponse, ...]
    alternative_explanations: tuple[AlternativeExplanationResponse, ...]


class CompileFactResponse(BaseModel):
    fact_id: UUID
    included: bool
    granted_scope: str | None
    reason_codes: tuple[str, ...]
    export_fact_ids: tuple[UUID, ...]
    transformation_rule_id: str | None


class CompileEvidenceResponse(BaseModel):
    source_evidence_id: UUID
    included: bool
    reason_codes: tuple[str, ...]
    export_handle_id: UUID | None
    derivative_sha256: str | None


class CompileExplanationResponse(BaseModel):
    compile_id: UUID
    decision: str
    based_on_case_version: int
    policy_version: str
    compiler_version: str
    view_id: UUID | None
    view_hash: str | None
    reason_codes: tuple[str, ...]
    facts: tuple[CompileFactResponse, ...]
    evidence: tuple[CompileEvidenceResponse, ...]


class InvestigationCaseResponse(BaseModel):
    case_id: UUID
    title: str
    state: str
    version: int
    authorization_version: int
    corroboration_source_count: int


class InvestigationResponse(BaseModel):
    case: InvestigationCaseResponse
    reports: tuple[ReportResponse, ...]
    facts: tuple[FactResponse, ...]
    assessment: AssessmentResponse | None
    compile: CompileExplanationResponse | None


@router.get("/cases/{case_id}/investigation", response_model=InvestigationResponse)
async def read_investigation(
    request: Request,
    case_id: UUID,
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> InvestigationResponse:
    """Presenter-only. Creates nothing; the private half of ``PrivacyBoundaryCompare``."""

    require_presenter(actor)
    container: ApiContainer = container_of(request)
    if container.investigation is None:
        raise HTTPException(status_code=503, detail="The investigation surface is not wired.")
    scope = CaseScope(
        namespace=container.namespace,
        community_id=container.community_id,
        case_id=CaseId(case_id),
    )
    projection: InvestigationProjectionResponse = await container.investigation.execute(scope)
    return InvestigationResponse(
        case=InvestigationCaseResponse(
            case_id=projection.case.case_id,
            title=projection.case.title,
            state=projection.case.state.value,
            version=projection.case.version,
            authorization_version=projection.case.authorization_version,
            corroboration_source_count=projection.case.corroboration_source_count,
        ),
        reports=tuple(
            ReportResponse(
                report_id=report.report_id,
                contributor_id=report.contributor_id,
                status=report.status.value,
                created_at=report.created_at.isoformat(),
            )
            for report in projection.reports
        ),
        facts=tuple(
            FactResponse(
                fact_id=fact.fact_id,
                fact_type=fact.fact_type.value,
                sensitivity=fact.sensitivity.value,
                value_preview=fact.value_preview,
                evidence_status=fact.evidence_status.value,
                status=fact.status.value,
                contributor_id=fact.contributor_id,
                evidence_ids=fact.evidence_ids,
                source_message_ids=fact.source_message_ids,
                version=fact.version,
            )
            for fact in projection.facts
        ),
        assessment=(
            None
            if projection.assessment is None
            else AssessmentResponse(
                assessment_id=projection.assessment.assessment_id,
                based_on_case_version=projection.assessment.based_on_case_version,
                linkage_decision=projection.assessment.linkage_decision,
                independent_source_count=projection.assessment.independent_source_count,
                is_corroborated=projection.assessment.is_corroborated,
                recommended_disposition=projection.assessment.recommended_disposition,
                assessment_hash=projection.assessment.assessment_hash,
                created_at=projection.assessment.created_at.isoformat(),
                findings=tuple(
                    FindingResponse(
                        fact_id=f.fact_id,
                        evidence_status=f.evidence_status.value,
                        reason_code=f.reason_code,
                    )
                    for f in projection.assessment.findings
                ),
                contradictions=tuple(
                    ContradictionResponse(
                        statement_fact_ids=c.statement_fact_ids,
                        description=c.description,
                        materiality=c.materiality,
                    )
                    for c in projection.assessment.contradictions
                ),
                alternative_explanations=tuple(
                    AlternativeExplanationResponse(
                        description=a.description,
                        cited_report_ids=a.cited_report_ids,
                        cited_fact_ids=a.cited_fact_ids,
                        cited_evidence_ids=a.cited_evidence_ids,
                    )
                    for a in projection.assessment.alternative_explanations
                ),
            )
        ),
        compile=(
            None
            if projection.compile is None
            else CompileExplanationResponse(
                compile_id=projection.compile.compile_id,
                decision=projection.compile.decision.value,
                based_on_case_version=projection.compile.based_on_case_version,
                policy_version=projection.compile.policy_version,
                compiler_version=projection.compile.compiler_version,
                view_id=None
                if projection.compile.view_id is None
                else projection.compile.view_id.value,
                view_hash=(
                    None
                    if projection.compile.view_hash is None
                    else projection.compile.view_hash.value
                ),
                reason_codes=projection.compile.reason_codes,
                facts=tuple(
                    CompileFactResponse(
                        fact_id=f.fact_id.value,
                        included=f.included,
                        granted_scope=None if f.granted_scope is None else f.granted_scope.value,
                        reason_codes=f.reason_codes,
                        export_fact_ids=tuple(item.value for item in f.export_fact_ids),
                        transformation_rule_id=f.transformation_rule_id,
                    )
                    for f in projection.compile.facts
                ),
                evidence=tuple(
                    CompileEvidenceResponse(
                        source_evidence_id=e.source_evidence_id.value,
                        included=e.included,
                        reason_codes=e.reason_codes,
                        export_handle_id=e.export_handle_id,
                        derivative_sha256=(
                            None if e.derivative_sha256 is None else e.derivative_sha256.value
                        ),
                    )
                    for e in projection.compile.evidence
                ),
            )
        ),
    )
