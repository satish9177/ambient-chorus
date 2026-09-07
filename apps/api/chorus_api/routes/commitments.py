"""The verification transport, and the demo clock that reaches the same watcher.

``POST /cases/{case_id}/commitments/{commitment_id}/verification`` is **the only path by which a
commitment is satisfied or missed, and the only path by which a case is resolved**
([ADR-027](../../../../docs/adr/ADR-027-commitment-extraction-grounding-and-authority.md) § 8).
Not the extraction model, not a later reply, not new ambient evidence, and not the passage of the
deadline. There is no cancellation route in V1.

The body carries ``{expected_version, outcome, note?, fixture_evidence_id?}`` and **no
contributor identifier**. Who is deciding is resolved from the authenticated persona by
:func:`~chorus_api.dependencies.require_resident`, and whether that person is *affected* is
decided by the use case against loaded case facts. A body that could name either would be a body
that could impersonate the person the whole edge exists to require.

``POST /demo/clock/advance`` moves the demo's one logical clock and invokes the **same** watcher
with the **same** ``CommitmentDueEvent``, plus a ``trigger=DEMO_CLOCK`` audit field. It does not
mutate a commitment, and it cannot: this route holds no repository write path to ``COMMITMENT#``
-- it holds a clock and the watcher use case, and every field of the event it hands over is
re-verified against the strongly loaded row before anything moves
([ADR-028](../../../../docs/adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) § 5).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from chorus.application.commands.record_commitment_due import (
    TRIGGER_DEMO_CLOCK,
    RecordCommitmentDueCommand,
)
from chorus.application.commands.verify_commitment import (
    VerificationOutcome,
    VerifyCommitmentCommand,
)
from chorus.application.services.commitment_schedule import due_event
from chorus.domain.ids import CaseId, CommitmentId, EvidenceItemId, Sha256Digest
from chorus_api.dependencies import (
    ApiContainer,
    DemoActor,
    actor_id_hash,
    container_of,
    require_actor,
    require_presenter,
    require_resident,
)

router = APIRouter(tags=["commitments"])

IdempotencyKeyStr = Annotated[
    str, StringConstraints(min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$")
]
NoteStr = Annotated[str, StringConstraints(min_length=1, max_length=300)]

MAX_DEMO_ADVANCE = timedelta(days=60)
"""How far one demo advance may move the clock.

A bound rather than an open number, because the logical clock is a real authority in the demo
namespace: an unbounded advance would let one request take every future deadline past due at
once. Sixty days comfortably exceeds the thirty-day commitment horizon, so nothing legitimate is
out of reach.
"""


class TransportRequest(BaseModel):
    """A closed HTTP request body; a field nobody declared can never ride along."""

    model_config = ConfigDict(extra="forbid")


class VerifyCommitmentRequest(TransportRequest):
    """The frozen verification body. There is deliberately no contributor field."""

    expected_version: Annotated[int, Field(ge=1)]
    outcome: Literal["FULFILLED", "MISSED"]
    note: NoteStr | None = None
    fixture_evidence_id: UUID | None = None


class VerificationView(BaseModel):
    commitment_id: UUID
    commitment_status: str
    commitment_version: int
    case_state: str
    case_version: int
    action_pointer_invalidated: bool


class AdvanceDemoClockRequest(TransportRequest):
    """Which commitment to wake the watcher for, and how far to move the clock."""

    case_id: UUID
    commitment_id: UUID
    advance_seconds: Annotated[int, Field(ge=1, le=int(MAX_DEMO_ADVANCE.total_seconds()))]


class DemoClockView(BaseModel):
    logical_now: str
    watcher_outcome: str
    commitment_status: str | None


@router.post(
    "/cases/{case_id}/commitments/{commitment_id}/verification",
    status_code=200,
    response_model=VerificationView,
)
async def verify_commitment(
    request: Request,
    response: Response,
    case_id: UUID,
    commitment_id: UUID,
    body: VerifyCommitmentRequest,
    idempotency_key: Annotated[IdempotencyKeyStr, Header(alias="Idempotency-Key")],
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> VerificationView:
    """Record one affected contributor's decision. The only source that may resolve a case."""

    container: ApiContainer = container_of(request)
    # Authorization first, and deliberately before the wiring check. A caller who may not use
    # this surface learns that they may not use it -- not whether the deployment has it. A 503
    # answered to the wrong persona is a probe answered.
    #
    # A resident persona, resolved to the one seeded contributor it may act as. The presenter is
    # refused here even though they may read the case surface: watching a commitment and saying
    # whether it was kept are different powers.
    contributor_id = require_resident(container, actor)
    if container.verify_commitment is None:
        raise HTTPException(status_code=503, detail="The verification surface is not wired.")
    actor_hash: Sha256Digest = actor_id_hash(actor)

    result = await container.verify_commitment.execute(
        VerifyCommitmentCommand(
            namespace=container.namespace,
            community_id=container.community_id,
            case_id=CaseId(case_id),
            commitment_id=CommitmentId(commitment_id),
            contributor_id=contributor_id,
            expected_version=body.expected_version,
            outcome=VerificationOutcome(body.outcome),
            actor_id_hash=actor_hash,
            correlation_id=request.state.correlation_id,
            idempotency_key=idempotency_key,
            note=body.note,
            verification_evidence_id=(
                None
                if body.fixture_evidence_id is None
                else EvidenceItemId(body.fixture_evidence_id)
            ),
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return VerificationView(
        commitment_id=commitment_id,
        commitment_status=result.commitment_status.value,
        commitment_version=result.commitment_version,
        case_state=result.case_state.value,
        case_version=result.case_version,
        action_pointer_invalidated=result.action_pointer_invalidated,
    )


@router.post("/demo/clock/advance", status_code=200, response_model=DemoClockView)
async def advance_demo_clock(
    request: Request,
    response: Response,
    body: AdvanceDemoClockRequest,
    actor: Annotated[DemoActor, Depends(require_actor)],
) -> DemoClockView:
    """Move the logical clock, then invoke the same watcher with the same event.

    The event this route builds is derived from the commitment's own identity and generation --
    it is the value the schedule would have carried -- and the watcher re-verifies every field of
    it against the strongly loaded row. So a presenter can make a deadline arrive and can change
    nothing else: both commitment outcomes still require a contributor.
    """

    require_presenter(actor)
    container: ApiContainer = container_of(request)
    clock = container.demo_clock
    watcher = container.record_commitment_due
    if clock is None or watcher is None:
        raise HTTPException(status_code=503, detail="The demo clock is not enabled.")

    logical_now = clock.advance(timedelta(seconds=body.advance_seconds))
    commitment = await container.read_commitment(
        case_id=CaseId(body.case_id), commitment_id=CommitmentId(body.commitment_id)
    )
    result = await watcher.execute(
        RecordCommitmentDueCommand(
            event=due_event(
                namespace=container.namespace,
                case_id=commitment.case_id,
                commitment_id=commitment.commitment_id,
                generation=commitment.schedule_generation,
                due_at=commitment.due_at,
            ),
            community_id=container.community_id,
            actor_id_hash=actor_id_hash(actor),
            correlation_id=request.state.correlation_id,
            trigger=TRIGGER_DEMO_CLOCK,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return DemoClockView(
        logical_now=logical_now.isoformat(),
        watcher_outcome=result.outcome.value,
        commitment_status=(
            None if result.commitment_status is None else result.commitment_status.value
        ),
    )
