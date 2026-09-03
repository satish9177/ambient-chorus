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

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Annotated

from fastapi import Header, HTTPException, Request

from chorus.application.commands.ingest_messages import IngestMessages
from chorus.application.operations import ApplicationOperations
from chorus.application.queries.feed import ReadAmbientFeed
from chorus.domain.ids import Namespace, Sha256Digest
from chorus.ports.operations import OperationDispatchPort

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


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiContainer:
    """Everything the Phase 3 routes are allowed to reach."""

    namespace: Namespace
    ingest_messages: IngestMessages
    read_feed: ReadAmbientFeed
    operations: ApplicationOperations
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
