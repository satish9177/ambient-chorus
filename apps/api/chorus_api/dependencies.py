"""The API composition root: what is wired, and who is allowed to ask.

Construction is explicit. There is no service locator and no framework-managed container: the
application object is handed a fully built :class:`ApiContainer`, and a route reaches for a use
case rather than for a repository.

Access control here is the Phase 3 half of the frozen demo model. The actor header selects one
seeded persona from a fixed set and every route states which personas may use it.

Bearer-token validation against Secrets Manager is the deployed half, and it is a *container
field* rather than a hard-wired middleware: :attr:`ApiContainer.access` holds a
:class:`~chorus.ports.access.DemoAccessVerifierPort` in the deployed composition and ``None`` in
the local one, and :func:`~chorus_api.main.build_app` installs the check only where a verifier
exists. There is still no placeholder token check, because a check that accepts anything would
read as authentication in review while providing none -- so behind a trusted local boundary the
actor header alone selects a persona, exactly as before, and in the deployed demo the token is
required first.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Annotated

from fastapi import Header, HTTPException, Request

from chorus.application.commands.approve_action import ApproveAction
from chorus.application.commands.decide_mandate import DecideMandate
from chorus.application.commands.ingest_external_reply import (
    IngestExternalReply,
    RecordReplyRejection,
)
from chorus.application.commands.ingest_messages import IngestMessages
from chorus.application.commands.invalidate_action import InvalidateAction
from chorus.application.commands.propose_mandates import ProposeMandates
from chorus.application.commands.verify_commitment import VerifyCommitment
from chorus.application.compile_contract import CompileViewRunner
from chorus.application.operations import ApplicationOperations
from chorus.application.queries.audit_page import ReadCaseAudit
from chorus.application.queries.case_surface import ReadCaseSurface
from chorus.application.queries.current_action import ReadCurrentAction
from chorus.application.queries.feed import ReadAmbientFeed
from chorus.application.queries.investigation import ReadInvestigation
from chorus.application.queries.mandates import ReadMandateThread
from chorus.application.services.inbound_mail import InboundMailAttester
from chorus.application.watcher_contract import CommitmentWatcher
from chorus.composition.demo_reset import DemoResetService
from chorus.domain.entities import Commitment
from chorus.domain.ids import (
    CaseId,
    CommitmentId,
    CommunityId,
    ContributorId,
    DestinationId,
    Namespace,
    Sha256Digest,
)
from chorus.infrastructure.fixtures.inbound_delivery import DemoReplyDeliverySource
from chorus.infrastructure.persistent_clock import ScopedLogicalClock
from chorus.ports.access import DemoAccessVerifierPort
from chorus.ports.demo_clock import DemoClockPort, DemoClockStorePort
from chorus.ports.operations import OperationDispatchPort
from chorus.ports.records import StoredSafeDestination
from chorus.ports.repositories import ShareableRepositoryPort
from chorus.ports.scopes import CaseScope

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
class InboundReplySurface:
    """Everything the demo reply route reaches, and deliberately nothing more.

    No SES port, no agent client, no compiler client, and no scheduler client. The route turns a
    reviewed fixture into a delivery, hands it to the attester, and persists what comes back --
    and the absence of the other four is what makes "the inbound path can do nothing else" a
    property of this type (ADR-026 § Consequences).
    """

    attester: InboundMailAttester
    ingest: IngestExternalReply
    record_rejection: RecordReplyRejection
    demo_replies: DemoReplyDeliverySource


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestLogicalTime:
    """The durable clock, and the slot one request's reading is bound into.

    Both together or neither: a store with no scope is a read nothing can use, and a scope with
    no store is a clock nothing ever binds -- which raises on the first ``now()`` rather than
    quietly returning something. Grouping them is what makes "the deployed API has one
    authoritative clock" a single field to wire and a single field to assert.
    """

    store: DemoClockStorePort
    scope: ScopedLogicalClock


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
    compile_view: CompileViewRunner
    """The deterministic privacy compiler, in-process locally and behind one synchronous
    invocation when deployed.

    Typed as the runner protocol because the deployed request path is *denied* every write on
    the view partitions -- the compiler is the sole creator of views by IAM and not by
    convention -- so an in-process compiler here could only fail, and only in an account
    (deployment contract SS 8.1).
    """

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

    verify_commitment: VerifyCommitment | None = None
    """The one path by which a commitment is satisfied or missed, and a case resolved.

    Optional because a composition may wire the read surfaces without it; a route that finds it
    absent answers ``503`` rather than pretending a decision was recorded (ADR-027 § 8).
    """

    inbound_replies: InboundReplySurface | None = None
    """The four objects the demo reply route needs, or ``None`` for a deployment with no boundary.

    Grouped rather than four fields, because they are only ever wired together: an attester with
    no verifier mints artifacts nothing accepts, and a verifier with no attester accepts nothing.
    """

    record_commitment_due: CommitmentWatcher | None = None
    """The watcher, in-process locally and behind one synchronous invocation when deployed.

    Typed as the protocol rather than as :class:`RecordCommitmentDue` because the deployed API
    holds no Shareable case-write path, no unit of work, and no audit repository -- it holds
    ``lambda:InvokeFunction`` on the watcher's ``live`` alias and
    :class:`~chorus.application.watcher_contract.RemoteRecordCommitmentDue`. Both reach the
    **same** watcher application logic, which is what ADR-028 SS 5 requires: a second
    implementation for the demo path would make the early-firing comparison mean two different
    things on the two paths.
    """

    demo_clock: DemoClockPort | None = None
    """The demo's one logical clock, or ``None`` outside the demo.

    The route that advances it holds no repository write path to ``COMMITMENT#``: it holds this
    clock and the watcher, and the watcher re-verifies every field of the event it is handed
    against the strongly loaded row.

    Locally this is the process-local
    :class:`~chorus.infrastructure.local.demo_clock.LogicalDemoClock`; deployed it is
    :class:`~chorus.infrastructure.persistent_clock.PersistentDemoClock` over the durable
    ``NS#DEMO#CLOCK`` row (ADR-029). One port, so the route never branches on the deployment.
    """

    access: DemoAccessVerifierPort | None = None
    """The deployed demo access boundary, or ``None`` behind a trusted local boundary.

    ``None`` means the token check is not installed at all rather than installed permissively:
    :func:`~chorus_api.main.build_app` adds the middleware only when a verifier is present, so
    there is no code path in which a check runs and accepts everything.
    """

    logical_time: RequestLogicalTime | None = None
    """The durable clock the deployed API binds once per request, or ``None`` locally.

    Deployed, every timestamp a request writes has to name the same authoritative logical
    instant, and that instant cannot come from a process-local object in a system where the
    watcher runs in a different Lambda. So the deployed composition supplies the store, one
    strongly consistent read happens per request, and the reading is bound for that request's
    duration (:class:`~chorus.infrastructure.persistent_clock.ScopedLogicalClock`).
    """

    commitments: ShareableRepositoryPort | None = None
    """The read handle the demo clock route uses to name which commitment it is waking.

    A read-only use of the Shareable repository, and the only one this container holds: the
    route needs the commitment's generation and due time to build the event the watcher will
    then re-verify against that same row.
    """

    dispatcher: OperationDispatchPort

    reset_demo: DemoResetService | None = None
    """The Phase 10 local reset service, or ``None`` for a composition with no reset route.

    Optional for the same reason ``verify_commitment`` is: a composition may wire every read
    surface without it, and a route that finds it absent answers ``503`` rather than pretending
    a reset happened.
    """

    investigation: ReadInvestigation | None = None
    """The Phase 10 private investigation read, presenter-only. ``None`` answers ``503``."""

    audit_page: ReadCaseAudit | None = None
    """The Phase 10 safe audit page read, presenter-only. ``None`` answers ``503``."""

    case_surface: ReadCaseSurface | None = None
    """The Phase 10 completion of the five remaining ``GET /cases/{id}`` sections.

    ``None`` falls back to the Phase 7 ``current_action``-only surface, which is what every
    existing Phase 3-9 contract test still constructs and still expects to keep working.
    """

    async def read_commitment(self, *, case_id: CaseId, commitment_id: CommitmentId) -> Commitment:
        """Strongly read one commitment for the demo clock route, or refuse.

        On the container rather than in the route because a route that assembled a scope would
        be a route deciding which namespace and community it is acting in -- and those are
        exactly the two values a caller must never be able to choose.
        """

        if self.commitments is None:
            raise RuntimeError("no commitment read handle is wired")
        return await self.commitments.load_commitment(
            CaseScope(namespace=self.namespace, community_id=self.community_id, case_id=case_id),
            commitment_id,
        )


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
    """Admit any seeded persona to the case surface; the route serves each its safe subset.

    ``GET /cases/{case_id}`` returns action-safe data only. The presenter gets the full
    presenter subset (private title, evidence summary, privacy counts); every other persona --
    the approver, and a resident -- gets the strictly narrower shareable subset the route
    already computes for a non-presenter: no private title, no evidence summary, no privacy
    counts, and nothing the compiler did not mark shareable.

    A resident is admitted because the *only* path by which a case is resolved is a resident
    recording an affected contributor's verification of a due commitment
    (``POST .../commitments/{id}/verification``), and the commitment they must see to do that
    lives on this surface. Before this repair a resident reached it only by the browser
    replaying an earlier presenter/approver identity -- a privileged read elevation this
    persona never held (P1-1). Removing that elevation without admitting the resident here
    would leave them no honest way to see their own community's due commitment. The private
    surfaces stay presenter-only regardless: ``GET .../investigation`` and ``GET .../audit``
    still call :func:`require_presenter`.
    """

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
