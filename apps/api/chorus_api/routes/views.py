"""The compile transport: ask the deterministic boundary, synchronously, for one safe view.

This route is **synchronous**, and that is a contract rather than a convenience. Compiling
calls no model and holds no external dependency open; it is a bounded set of strongly
consistent reads, a pure evaluation, and one transaction. Wrapping it in an operation would
create a durable status row whose only content is "the deterministic answer is not ready yet",
and a poller for a decision that was already made before the response could have been written.
There is deliberately no ``COMPILE`` ``ApplicationOperationKind``.

An ``ALLOW`` returns 200 with the persisted view. A deterministic refusal is *not* an error the
route formats: it raises ``PolicyDeniedError``, and the frozen problem-details handler maps it
to 422 with its reason codes. A stale case, an idempotency conflict, and a live send fence each
map to 409 through the same table, so the transport adds no mapping of its own.

The destination is never a request field. It is resolved server-side from the deployment's
registry entry, because a caller who could name where a compile is going could compile toward
somewhere nobody approved.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from chorus.application.commands.compile_view import (
    CompileViewCommand,
    CompileViewResult,
    RequestedFactInput,
)
from chorus.domain.entities import Purpose
from chorus.domain.ids import CaseId, EvidenceItemId, FactId
from chorus.domain.time import format_utc
from chorus.ports.limits import MAX_COMPILE_REQUESTED_EVIDENCE, MAX_COMPILE_REQUESTED_FACTS
from chorus.ports.records import StoredShareableView
from chorus_api.dependencies import (
    ApiContainer,
    DemoActor,
    actor_id_hash,
    container_of,
    require_actor,
    require_presenter,
)

router = APIRouter(tags=["views"])

IdempotencyKeyStr = Annotated[
    str, StringConstraints(min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$")
]


class TransportRequest(BaseModel):
    """A closed HTTP request body; a field nobody declared can never ride along."""

    model_config = ConfigDict(extra="forbid")


class RequestedFactBody(TransportRequest):
    fact_id: UUID
    necessity: Literal["REQUIRED", "OPTIONAL"]
    intended_usage: Literal["CLAIM", "AGGREGATION_INPUT", "EVIDENCE"]


class CompileViewRequest(TransportRequest):
    """The frozen ``CompileCommand``, minus everything the server decides for itself.

    Namespace, community, and case come from the path and the deployment. The destination, the
    policy version, and the compiler contract version are configuration. What remains is what
    the caller genuinely chooses: which compile this is, which case version they believe they
    are compiling, and which of that case's facts and evidence they are asking about.
    """

    compile_id: UUID
    expected_case_version: Annotated[int, Field(ge=1)]
    requested_facts: Annotated[
        list[RequestedFactBody], Field(min_length=1, max_length=MAX_COMPILE_REQUESTED_FACTS)
    ]
    requested_evidence_ids: Annotated[
        list[UUID], Field(default_factory=list, max_length=MAX_COMPILE_REQUESTED_EVIDENCE)
    ]
    purpose: Literal["REQUEST_ELEVATOR_REPAIR_AND_RESPONSE"]


class SafeDestinationBody(BaseModel):
    destination_id: str
    kind: str
    registry_version: int
    routing_token: UUID
    display_label: str


class ShareableEvidenceRefBody(BaseModel):
    safe_evidence_ref_id: UUID
    media_type: str
    export_handle_id: UUID
    sha256: str
    caption: str
    created_by_rule_id: str
    content_hash: str


class ShareableFactBody(BaseModel):
    export_fact_id: UUID
    fact_type: str
    safe_text: str
    effective_scope: str
    evidence_status: str
    contributor_count: int
    transformation: str
    transformation_rule_id: str
    safe_evidence_ref_ids: list[UUID]
    content_hash: str


class MandateVersionRefBody(BaseModel):
    mandate_id: UUID
    version: int
    terms_hash: str


class ShareableCaseViewBody(BaseModel):
    """The safe artifact, rendered field for field. Nothing is added and nothing is dropped."""

    schema_version: str
    view_id: UUID
    case_id: UUID
    community_public_label: str
    case_version: int
    policy_version: str
    compiler_version: str
    destination: SafeDestinationBody
    purpose: str
    generated_at: str
    expires_at: str
    mandate_version_set: list[MandateVersionRefBody]
    authorization_snapshot_hash: str
    shareable_facts: list[ShareableFactBody]
    safe_evidence_refs: list[ShareableEvidenceRefBody]
    audit_refs: list[UUID]
    view_hash: str


class IncludedFactBody(BaseModel):
    fact_id: UUID
    export_fact_ids: list[UUID]


class ExcludedFactBody(BaseModel):
    """Why one requested fact did not travel. Private, and never part of the Action input."""

    fact_id: UUID
    reason_codes: list[str]


class CompileViewResponse(BaseModel):
    decision: Literal["ALLOW"]
    compile_id: UUID
    audit_event_id: UUID
    view: ShareableCaseViewBody | None
    included: list[IncludedFactBody]
    excluded: list[ExcludedFactBody]
    replayed: bool


@router.post("/cases/{case_id}/views", status_code=200, response_model=CompileViewResponse)
async def compile_view(
    request: Request,
    response: Response,
    case_id: UUID,
    body: CompileViewRequest,
    idempotency_key: Annotated[IdempotencyKeyStr, Header(alias="Idempotency-Key")],
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> CompileViewResponse:
    """Compile one safe view, or refuse deterministically. No model is called here."""

    require_presenter(actor)
    container: ApiContainer = container_of(request)
    command = CompileViewCommand(
        namespace=container.namespace,
        community_id=container.community_id,
        case_id=CaseId(case_id),
        compile_id=body.compile_id,
        expected_case_version=body.expected_case_version,
        requested_facts=tuple(
            RequestedFactInput(
                fact_id=FactId(item.fact_id),
                necessity=item.necessity,
                intended_usage=item.intended_usage,
            )
            for item in body.requested_facts
        ),
        requested_evidence_ids=tuple(
            EvidenceItemId(value) for value in body.requested_evidence_ids
        ),
        destination=container.destination,
        purpose=Purpose(body.purpose),
        actor_id_hash=actor_id_hash(actor),
        idempotency_key=idempotency_key,
        correlation_id=request.state.correlation_id,
    )
    result = await container.compile_view.execute(command)
    response.headers["Cache-Control"] = "no-store"
    return _response(result)


def _response(result: CompileViewResult) -> CompileViewResponse:
    """Render the allowed result.

    A replay carries the same body as the original, because the application answers a replay
    with the artifact that was *persisted* rather than one it recomputed. That is what makes
    ``replayed`` a piece of information rather than a different contract: the caller gets the
    same view either way and learns only that it was not the attempt that created it.
    """

    view = result.view
    return CompileViewResponse(
        decision="ALLOW",
        compile_id=result.compile_id,
        audit_event_id=result.audit_event_id,
        view=None if view is None else _view_body(view),
        included=[
            IncludedFactBody(
                fact_id=entry.fact_id.value, export_fact_ids=list(entry.export_fact_ids)
            )
            for entry in result.included
        ],
        excluded=[
            ExcludedFactBody(fact_id=entry.fact_id.value, reason_codes=list(entry.reason_codes))
            for entry in result.excluded
        ],
        replayed=result.replayed,
    )


def _view_body(view: StoredShareableView) -> ShareableCaseViewBody:
    return ShareableCaseViewBody(
        schema_version=view.schema_version,
        view_id=view.view_id.value,
        case_id=view.case_id.value,
        community_public_label=view.community_public_label,
        case_version=view.case_version,
        policy_version=view.policy_version,
        compiler_version=view.compiler_version,
        destination=SafeDestinationBody(
            destination_id=view.destination.destination_id.value,
            kind=view.destination.kind.value,
            registry_version=view.destination.registry_version,
            routing_token=view.destination.routing_token,
            display_label=view.destination.display_label,
        ),
        purpose=view.purpose.value,
        generated_at=format_utc(view.generated_at),
        expires_at=format_utc(view.expires_at),
        mandate_version_set=[
            MandateVersionRefBody(
                mandate_id=ref.mandate_id, version=ref.version, terms_hash=ref.terms_hash.value
            )
            for ref in view.mandate_version_set
        ],
        authorization_snapshot_hash=view.authorization_snapshot_hash.value,
        shareable_facts=[
            ShareableFactBody(
                export_fact_id=fact.export_fact_id.value,
                fact_type=fact.fact_type.value,
                safe_text=fact.safe_text,
                effective_scope=fact.effective_scope.value,
                evidence_status=fact.evidence_status.value,
                contributor_count=fact.contributor_count,
                transformation=fact.transformation.value,
                transformation_rule_id=fact.transformation_rule_id,
                safe_evidence_ref_ids=[value.value for value in fact.safe_evidence_ref_ids],
                content_hash=fact.content_hash.value,
            )
            for fact in view.shareable_facts
        ],
        safe_evidence_refs=[
            ShareableEvidenceRefBody(
                safe_evidence_ref_id=ref.safe_evidence_ref_id.value,
                media_type=ref.media_type,
                export_handle_id=ref.export_handle_id,
                sha256=ref.sha256.value,
                caption=ref.caption,
                created_by_rule_id=ref.created_by_rule_id,
                content_hash=ref.content_hash.value,
            )
            for ref in view.safe_evidence_refs
        ],
        audit_refs=list(view.audit_refs),
        view_hash=view.view_hash.value,
    )
