"""The Private Mandate Thread transport: read your own terms, decide your own terms.

Three routes, and the interesting part of each is what it refuses to accept.

The decision body carries no contributor identifier. Who is deciding comes from the
authenticated persona through the seeded registry, so a caller cannot name whose authorization
they are recording -- a body field could be changed, and a persona cannot.

It carries no destination and no purpose either. Those are settled when the proposal is made
and are carried forward version to version; a request that could restate them would be a
request that could widen them, and there is nothing a contributor gains by being asked a
question with exactly one legal answer.

The proposal body carries only an expected case version. It names no fact, no contributor, and
no grant, because everything it produces is derived from the case's own active facts and from
the deterministic policy/v1 tables. Two of those tables are involved and they are not the same:
``proposed_scope`` is what version 1 offers, and ``policy_maximum_scope`` is the ceiling no
later decision may exceed. The offer sits at or below the ceiling and usually well below.

Every response is built field by field from an application DTO. There is no path by which a
domain object, a fact value, or a stored row is serialized directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from chorus.application.commands.decide_mandate import DecideMandateCommand, DecideMandateResult
from chorus.application.commands.propose_mandates import (
    ProposeMandatesCommand,
    ProposeMandatesResult,
)
from chorus.application.queries.mandates import MandateThread
from chorus.domain.entities import DisclosureScope
from chorus.domain.errors import ValidationError
from chorus.domain.ids import CaseId, ContributorId, FactId, MandateId
from chorus.domain.mandates import FactGrant, IdentityGrant, MandateDecision
from chorus.ports.errors import NotFoundError
from chorus_api.dependencies import (
    ApiContainer,
    DemoActor,
    actor_id_hash,
    container_of,
    require_actor,
    require_presenter,
    require_resident,
    resolve_contributor,
)

router = APIRouter(tags=["mandates"])

IdempotencyKeyStr = Annotated[
    str, StringConstraints(min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$")
]

ScopeLiteral = Literal[
    "INTERNAL_ONLY",
    "AGGREGATE_ONLY",
    "ANONYMOUS_CASE",
    "NAMED_CASE",
    "EXTERNAL_ACTION",
]
"""The disclosure vocabulary, spelled out rather than derived from the domain enum.

Restating it is what makes an unknown scope a 422 from the transport schema instead of a value
that reaches a use case. It is covered by a test asserting the two never drift apart.
"""


class TransportRequest(BaseModel):
    """A closed HTTP request body; a field nobody declared can never ride along."""

    model_config = ConfigDict(extra="forbid")


class ProposeMandatesRequest(TransportRequest):
    """Accept one candidate case. It says nothing about what may be disclosed."""

    expected_case_version: Annotated[int, Field(ge=1, strict=True)]


class FactGrantRequest(TransportRequest):
    """One fact's content permission. Never identity permission, whatever the scope says."""

    fact_id: UUID
    max_scope: ScopeLiteral
    allow_safe_transformation: bool


class IdentityGrantRequest(TransportRequest):
    """Identity permission, submitted as its own object and evaluated on its own."""

    externally_shareable: bool
    max_scope: ScopeLiteral


class MandateDecisionRequest(TransportRequest):
    """One contributor's complete answer to one exact mandate version.

    ``fact_grants`` is the **whole** replacement set for ``ADJUST``, never a patch. A body that
    listed one fact would otherwise mean "change this one" to a reader and "revoke all the
    others" to the server, and the two readings differ by everything.
    """

    expected_version: Annotated[int, Field(ge=1, strict=True)]
    decision: Literal["APPROVE", "ADJUST", "REFUSE", "REVOKE"]
    fact_grants: Annotated[tuple[FactGrantRequest, ...], Field(max_length=100)] = ()
    identity_grant: IdentityGrantRequest
    expires_at: datetime | None = None


class ProposedMandateResponse(BaseModel):
    mandate_id: UUID
    version: int
    contributor_id: UUID
    status: str
    terms_hash: str
    fact_grant_count: int


class ProposeMandatesResponse(BaseModel):
    case_id: UUID
    case_version: int
    state: str
    proposals: tuple[ProposedMandateResponse, ...]


class MandateDecisionResponse(BaseModel):
    mandate_id: UUID
    version: int
    status: str
    terms_hash: str
    supersedes_version: int | None
    decided_at: datetime | None
    revoked_at: datetime | None
    case_version: int
    case_state: str


class FactPermissionResponse(BaseModel):
    """One fact row. ``wording`` is contributor-facing text, never the fact's own value."""

    fact_id: UUID
    fact_type: str
    wording: str
    policy_maximum_scope: str
    proposed_scope: str
    current_scope: str
    allow_safe_transformation: bool
    requires_identity_grant: bool
    locked_reason: str | None


class IdentityPermissionResponse(BaseModel):
    externally_shareable: bool
    max_scope: str
    policy_maximum_scope: str


class MandateVersionResponse(BaseModel):
    version: int
    status: str
    terms_hash: str
    decided_at: datetime | None
    revoked_at: datetime | None
    supersedes_version: int | None


class MandateThreadResponse(BaseModel):
    mandate_id: UUID
    case_id: UUID
    case_state: str
    contributor_id: UUID
    current_version: int
    status: str
    terms_hash: str
    fact_permissions: tuple[FactPermissionResponse, ...]
    identity_permission: IdentityPermissionResponse
    allowed_destination_ids: tuple[str, ...]
    allowed_purposes: tuple[str, ...]
    valid_from: datetime
    expires_at: datetime | None
    proposed_at: datetime
    decided_at: datetime | None
    revoked_at: datetime | None
    history: tuple[MandateVersionResponse, ...]


@router.post("/cases/{case_id}/mandates", status_code=200, response_model=ProposeMandatesResponse)
async def propose_mandates(
    request: Request,
    response: Response,
    case_id: UUID,
    body: ProposeMandatesRequest,
    idempotency_key: Annotated[IdempotencyKeyStr, Header(alias="Idempotency-Key")],
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> ProposeMandatesResponse:
    """Accept a candidate case and ask every contributor it names for a mandate."""

    require_presenter(actor)
    container: ApiContainer = container_of(request)
    result: ProposeMandatesResult = await container.propose_mandates.execute(
        ProposeMandatesCommand(
            namespace=container.namespace,
            community_id=container.community_id,
            case_id=CaseId(case_id),
            expected_case_version=body.expected_case_version,
            actor_id_hash=actor_id_hash(actor),
            idempotency_key=idempotency_key,
            destination_id=container.destination_id,
            correlation_id=request.state.correlation_id,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return ProposeMandatesResponse(
        case_id=result.case_id.value,
        case_version=result.case_version,
        state=result.state.value,
        proposals=tuple(
            ProposedMandateResponse(
                mandate_id=item.mandate_id.value,
                version=item.version,
                contributor_id=item.contributor_id.value,
                status=item.status.value,
                terms_hash=item.terms_hash.value,
                fact_grant_count=item.fact_grant_count,
            )
            for item in result.proposals
        ),
    )


@router.post(
    "/cases/{case_id}/mandates/{mandate_id}/decisions",
    status_code=200,
    response_model=MandateDecisionResponse,
)
async def decide_mandate(
    request: Request,
    response: Response,
    case_id: UUID,
    mandate_id: UUID,
    body: MandateDecisionRequest,
    idempotency_key: Annotated[IdempotencyKeyStr, Header(alias="Idempotency-Key")],
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> MandateDecisionResponse:
    """Record one immutable authorization decision by the contributor who owns the mandate."""

    container: ApiContainer = container_of(request)
    contributor_id = require_resident(container, actor)
    result: DecideMandateResult = await container.decide_mandate.execute(
        DecideMandateCommand(
            namespace=container.namespace,
            community_id=container.community_id,
            case_id=CaseId(case_id),
            mandate_id=MandateId(mandate_id),
            actor_contributor_id=contributor_id,
            actor_id_hash=actor_id_hash(actor),
            expected_version=body.expected_version,
            decision=MandateDecision(body.decision),
            fact_grants=tuple(
                FactGrant(
                    fact_id=FactId(grant.fact_id),
                    max_scope=DisclosureScope(grant.max_scope),
                    allow_safe_transformation=grant.allow_safe_transformation,
                )
                for grant in body.fact_grants
            ),
            identity_grant=_identity_grant(body.identity_grant),
            expires_at=body.expires_at,
            idempotency_key=idempotency_key,
            destination_id=container.destination_id,
            correlation_id=request.state.correlation_id,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return MandateDecisionResponse(
        mandate_id=result.mandate_id.value,
        version=result.version,
        status=result.status.value,
        terms_hash=result.terms_hash.value,
        supersedes_version=result.supersedes_version,
        decided_at=result.decided_at,
        revoked_at=result.revoked_at,
        case_version=result.case_version,
        case_state=result.case_state.value,
    )


@router.get(
    "/contributors/{contributor_id}/mandates/current",
    status_code=200,
    response_model=MandateThreadResponse,
)
async def read_current_mandate(
    request: Request,
    response: Response,
    contributor_id: UUID,
    case_id: Annotated[UUID, Query()],
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> MandateThreadResponse:
    """Return one contributor's mandate thread for one case.

    A resident may read only their own. Asking for somebody else's answers ``404`` rather than
    ``403``, and identically to an identifier that names nobody: a caller who could tell those
    apart could enumerate which contributors exist and which of them have mandates in a case
    they cannot otherwise see.
    """

    container: ApiContainer = container_of(request)
    requested = ContributorId(contributor_id)
    acting_as = resolve_contributor(container, actor)
    if actor is not DemoActor.PRESENTER_ADMIN and acting_as != requested:
        raise NotFoundError("DISCLOSURE_MANDATE")
    thread: MandateThread = await container.read_mandate_thread.execute(
        namespace=container.namespace,
        community_id=container.community_id,
        case_id=CaseId(case_id),
        contributor_id=requested,
    )
    response.headers["Cache-Control"] = "no-store"
    return _thread_response(thread)


def _identity_grant(body: IdentityGrantRequest) -> IdentityGrant:
    """Build the identity grant, mapping its own closed invariants onto a transport error.

    ``IdentityGrant`` refuses a non-shareable grant above ``ANONYMOUS_CASE`` because that pair
    means nothing: no identity may leave, stated at a scope describing how far it may travel. A
    request that says it is a malformed request, not a server failure.
    """

    try:
        return IdentityGrant(
            externally_shareable=body.externally_shareable,
            max_scope=DisclosureScope(body.max_scope),
        )
    except ValueError as error:
        raise ValidationError("IDENTITY_GRANT") from error


def _thread_response(thread: MandateThread) -> MandateThreadResponse:
    return MandateThreadResponse(
        mandate_id=thread.mandate_id.value,
        case_id=thread.case_id.value,
        case_state=thread.case_state.value,
        contributor_id=thread.contributor_id.value,
        current_version=thread.current_version,
        status=thread.status.value,
        terms_hash=thread.terms_hash.value,
        fact_permissions=tuple(
            FactPermissionResponse(
                fact_id=item.fact_id.value,
                fact_type=item.fact_type.value,
                wording=item.wording,
                policy_maximum_scope=item.policy_maximum_scope.value,
                proposed_scope=item.proposed_scope.value,
                current_scope=item.current_scope.value,
                allow_safe_transformation=item.allow_safe_transformation,
                requires_identity_grant=item.requires_identity_grant,
                locked_reason=item.locked_reason,
            )
            for item in thread.fact_permissions
        ),
        identity_permission=IdentityPermissionResponse(
            externally_shareable=thread.identity_permission.externally_shareable,
            max_scope=thread.identity_permission.max_scope.value,
            policy_maximum_scope=thread.identity_permission.policy_maximum_scope.value,
        ),
        allowed_destination_ids=tuple(str(item) for item in thread.allowed_destination_ids),
        allowed_purposes=tuple(item.value for item in thread.allowed_purposes),
        valid_from=thread.valid_from,
        expires_at=thread.expires_at,
        proposed_at=thread.proposed_at,
        decided_at=thread.decided_at,
        revoked_at=thread.revoked_at,
        history=tuple(
            MandateVersionResponse(
                version=item.version,
                status=item.status.value,
                terms_hash=item.terms_hash.value,
                decided_at=item.decided_at,
                revoked_at=item.revoked_at,
                supersedes_version=item.supersedes_version,
            )
            for item in thread.history
        ),
    )
