"""The API composition root: what is wired, and who is allowed to ask.

Construction is explicit. There is no service locator and no framework-managed container: the
application object is handed a fully built :class:`ApiContainer`, and a route reaches for a use
case rather than for a repository.

Access control here is the Phase 3 half of the frozen demo model. The actor header selects one
seeded persona from a fixed set and every route states which personas may use it.

Bearer-token validation against Secrets Manager is **not implemented**. It belongs to the
deployed demo in Phase 11, and nothing in this module or in :func:`~chorus_api.main.build_app`
substitutes for it: there is no placeholder token check, because a check that accepts anything
would read as authentication in review while providing none. Until Phase 11 lands, the actor
header alone selects a persona, and that is only sound behind a trusted local boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Annotated

from fastapi import Header, HTTPException, Request

from chorus.application.commands.approve_action import ApproveAction
from chorus.application.commands.compile_view import CompileView
from chorus.application.commands.decide_mandate import DecideMandate
from chorus.application.commands.ingest_messages import IngestMessages
from chorus.application.commands.invalidate_action import InvalidateAction
from chorus.application.commands.propose_mandates import ProposeMandates
from chorus.application.operations import ApplicationOperations
from chorus.application.queries.current_action import ReadCurrentAction
from chorus.application.queries.feed import ReadAmbientFeed
from chorus.application.queries.mandates import ReadMandateThread
from chorus.domain.ids import CommunityId, ContributorId, DestinationId, Namespace, Sha256Digest
from chorus.ports.operations import OperationDispatchPort
from chorus.ports.records import StoredSafeDestination

ACTOR_HEADER = "X-Chorus-Demo-Actor"


class DemoActor(StrEnum):
    """The fixed persona registry; an arbitrary identifier is never accepted."""

    PRESENTER_ADMIN = "presenter_admin"
    RESIDENT_A = "resident_a"
    RESIDENT_B = "resident_b"
    RESIDENT_C = "resident_c"
    RESIDENT_D = "resident_d"
    CASE_APPROVER = "case_approver"


def actor_id_hash(actor: DemoActor) -> Sha256Digest:
    """Hash the persona so an audit row and a log line can name it without storing it."""

    return Sha256Digest(f"sha256:{sha256(actor.value.encode('utf-8')).hexdigest()}")


RESIDENT_ACTORS: frozenset[DemoActor] = frozenset(
    {
        DemoActor.RESIDENT_A,
        DemoActor.RESIDENT_B,
        DemoActor.RESIDENT_C,
        DemoActor.RESIDENT_D,
    }
)
"""The personas that own facts, and therefore the only ones that can decide a mandate."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiContainer:
    """Everything the Phase 3 and Phase 4 routes are allowed to reach."""

    namespace: Namespace
    community_id: CommunityId
    destination_id: DestinationId
    destination: StoredSafeDestination
    """The deployment's full safe registry entry.

    A compile needs the version and routing token as well as the identifier, and a caller
    must never be able to supply any of them: a request that could name its own destination
    could compile toward one nobody approved.
    """
    contributor_by_actor: Mapping[DemoActor, ContributorId]
    ingest_messages: IngestMessages
    read_feed: ReadAmbientFeed
    operations: ApplicationOperations
    propose_mandates: ProposeMandates
    decide_mandate: DecideMandate
    read_mandate_thread: ReadMandateThread
    compile_view: CompileView
    read_current_action: ReadCurrentAction
    """The Phase-7 safe preview read path.

    Wired here rather than reached for, because a use case that exists and is not in the
    container is a use case no route can call -- which is exactly what it was.
    """

    approve_action: ApproveAction
    """The one place a human contributes authority, and it contributes exactly one bit."""

    invalidate_action: InvalidateAction
    """Withdrawal and clearing. Without it the failure matrix's own remedy for a definite send
    failure -- create and approve a fresh proposal -- has no reachable path."""

    dispatcher: OperationDispatchPort


def container_of(request: Request) -> ApiContainer:
    container = request.app.state.container
    if not isinstance(container, ApiContainer):  # pragma: no cover - composition guard
        raise RuntimeError("the application was built without a container")
    return container


def require_actor(
    value: Annotated[str | None, Header(alias=ACTOR_HEADER)] = None,
) -> DemoActor:
    """Resolve the caller to one seeded persona, or refuse the request."""

    if value is None:
        raise HTTPException(status_code=401, detail="An actor header is required.")
    try:
        return DemoActor(value)
    except ValueError as error:
        raise HTTPException(status_code=403, detail="Unknown actor.") from error


def require_presenter(actor: DemoActor) -> DemoActor:
    """Restrict a route to the presenter persona.

    The ambient feed and ingestion are private-zone surfaces: they show and accept raw
    community messages, so the approver and resident personas have no access to them at all.
    """

    if actor is not DemoActor.PRESENTER_ADMIN:
        raise HTTPException(status_code=403, detail="This surface requires the presenter role.")
    return actor


def require_case_reader(actor: DemoActor) -> DemoActor:
    """Restrict the case surface to the two personas the frozen access table names.

    ``GET /cases/{case_id}`` is "presenter/approver safe subset". The presenter runs the demo
    and the approver has to read a proposal before deciding on it; a resident persona has no
    business reading the case surface, and this route returns action-safe data only, so the
    approver's narrower view and the presenter's coincide for everything Phase 7 puts here.
    """

    if actor not in {DemoActor.PRESENTER_ADMIN, DemoActor.CASE_APPROVER}:
        raise HTTPException(status_code=403, detail="This surface requires a case reader role.")
    return actor


def require_case_approver(actor: DemoActor) -> DemoActor:
    """Restrict the approval, invalidation, and execute routes to the approver persona.

    The presenter is refused here even though they may *read* the case surface. Watching a
    proposal and authorizing an external message are different powers, and the frozen access
    model grants the second to ``case_approver`` and to nobody else.

    This resolves a persona, not a person. What it establishes is that somebody holding the
    demo access token asserted the approver persona -- recorded as ``approver_id_hash`` with
    ``approver_assurance = DEMO_SHARED_TOKEN`` -- and it is single-presenter demo access
    control rather than authentication (ADR-023 SS 4).
    """

    if actor is not DemoActor.CASE_APPROVER:
        raise HTTPException(status_code=403, detail="This surface requires the approver role.")
    return actor


def require_resident(container: ApiContainer, actor: DemoActor) -> ContributorId:
    """Resolve a resident persona to the one seeded contributor it may act as.

    The mapping is seeded configuration, never a request field. A contributor identifier that
    arrived in a path or a body is a claim; this is the only thing in the system that turns an
    authenticated persona into an identity, so a caller cannot name whose decision they are
    taking.

    The presenter is refused here even though they may *read* every mandate thread. Watching a
    private surface and answering on somebody's behalf are different powers, and the frozen
    access model grants only the first: ``presenter_admin`` gets "feed, case, investigation,
    compile, external reply, demo clock/reset", while a mandate decision belongs to
    ``resident_a..resident_d`` and to nobody else.
    """

    if actor not in RESIDENT_ACTORS:
        raise HTTPException(status_code=403, detail="Only a resident may decide their mandate.")
    contributor_id = container.contributor_by_actor.get(actor)
    if contributor_id is None:  # pragma: no cover - a composition root that seeded no persona
        raise HTTPException(status_code=403, detail="This persona has no seeded contributor.")
    return contributor_id


def resolve_contributor(container: ApiContainer, actor: DemoActor) -> ContributorId | None:
    """Return the contributor a persona acts as, or ``None`` for a non-resident persona."""

    return container.contributor_by_actor.get(actor)
