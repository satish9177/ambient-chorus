"""The compiler side of the send fence: the wire shape decoded, and the authority constructed.

:mod:`chorus.infrastructure.compiler.send_authorization` is the *sender's* half -- it encodes a
request and parses one of two answers. This is the compiler's half, and the two are frozen
against each other: ``send-authorization-request/v1`` in, a granted fence or a denial out,
``send-authorization-release/v1`` for the release.

The authority itself is :class:`chorus.application.services.send_authorization.SendAuthorization`
unchanged. Nothing here decides: every check named in
[ADR-025](../../../docs/adr/ADR-025-one-deliberate-ses-attempt.md) § 4, the fence's expiry
arithmetic, the release condition, and the whole denial vocabulary live inside that service, and
a branch here on a case state, a mandate, or a reason code would be a second implementation of
it.

Why the request is decoded strictly
------------------------------------
Every field of a fence request is a value some immutable artifact already binds, so the compiler
**verifies rather than trusts** it -- and a request it cannot parse is refused before the fence
is acquired, which is the only point at which refusing is free. A malformed request must never
become a partially-read one, because a fence acquired against a half-understood request is a
fence that authorizes something nobody asked for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from uuid import UUID

from chorus.application.services.send_authorization import SendAuthorization
from chorus.domain.entities import Purpose
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CaseId,
    CommunityId,
    DestinationId,
    ExecutionId,
    Namespace,
    Sha256Digest,
    ViewId,
)
from chorus.domain.time import format_utc, require_utc
from chorus.infrastructure.compiler.send_authorization import (
    FENCE_PAYLOAD_SCHEMA,
    RELEASE_PAYLOAD_SCHEMA,
)
from chorus.infrastructure.dynamodb.core import CoreRepository
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.shareable import ShareableRepository
from chorus.ports.clock import Clock
from chorus.ports.scopes import CaseScope
from chorus.ports.send_authorization import (
    SendAuthorizationDenied,
    SendAuthorizationGranted,
    SendAuthorizationOutcome,
    SendAuthorizationRequest,
)
from chorus.privacy.compiler import POLICY_BUILD_HASH
from chorus.privacy.policy import COMPILER_VERSION, POLICY_VERSION
from functions.compiler.composition import CompilerSettings, build_compiler_driver

FENCE_PURPOSE: Final = Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE
"""The one V1 purpose, supplied by composition and never read from a request payload."""


class FenceRequestError(ValueError):
    """A delivered fence request is not one this compiler can answer.

    Raised before the fence is acquired, so a malformed request writes nothing and leaves
    nothing to release.
    """


def build_send_authorization(settings: CompilerSettings, *, clock: Clock) -> SendAuthorization:
    """Construct the send authority over the compiler's own repositories.

    It is built here rather than in :mod:`functions.compiler.composition` because it is a second
    use case of the same adapters rather than a second composition: one driver, one Core handle,
    one Shareable handle, and the four frozen policy constants the authority compares against.
    """

    driver = build_compiler_driver(settings)
    cursors = SignedCursorCodec(secret=settings.cursor_secret)
    return SendAuthorization(
        core=CoreRepository(driver=driver, cursors=cursors),
        shareable=ShareableRepository(driver=driver, cursors=cursors),
        clock=clock,
        policy_version=POLICY_VERSION,
        compiler_version=COMPILER_VERSION,
        policy_build_hash=POLICY_BUILD_HASH,
        purpose=FENCE_PURPOSE,
    )


def decode_fence_request(payload: object) -> SendAuthorizationRequest:
    """Parse ``send-authorization-request/v1`` exactly, or refuse it."""

    body = _object(payload)
    if body.get("schema") != FENCE_PAYLOAD_SCHEMA:
        raise FenceRequestError("a fence request names an unknown schema version")
    try:
        return SendAuthorizationRequest(
            namespace=Namespace(_text(body, "namespace")),
            community_id=CommunityId(_uuid(body, "community_id")),
            case_id=CaseId(_uuid(body, "case_id")),
            action_id=ActionId(_uuid(body, "action_id")),
            execution_id=ExecutionId(_uuid(body, "execution_id")),
            approval_id=ApprovalId(_uuid(body, "approval_id")),
            proposal_hash=Sha256Digest(_text(body, "proposal_hash")),
            view_id=ViewId(_uuid(body, "view_id")),
            view_hash=Sha256Digest(_text(body, "view_hash")),
            authorization_version=_number(body, "authorization_version"),
            policy_version=_text(body, "policy_version"),
            compiler_version=_text(body, "compiler_version"),
            policy_build_hash=Sha256Digest(_text(body, "policy_build_hash")),
            destination_id=DestinationId(_text(body, "destination_id")),
            destination_registry_version=_number(body, "destination_registry_version"),
            routing_token=_uuid(body, "routing_token"),
            purpose=Purpose(_text(body, "purpose")),
            authorization_snapshot_hash=Sha256Digest(_text(body, "authorization_snapshot_hash")),
            requested_at=_instant(body, "requested_at"),
        )
    except FenceRequestError:
        raise
    except (TypeError, ValueError) as error:
        raise FenceRequestError("a fence request is not well formed") from error


def decode_release_request(payload: object) -> tuple[CaseScope, ExecutionId]:
    """Parse ``send-authorization-release/v1`` into the scope and execution it names."""

    body = _object(payload)
    if body.get("schema") != RELEASE_PAYLOAD_SCHEMA:
        raise FenceRequestError("a release request names an unknown schema version")
    try:
        scope = CaseScope(
            namespace=Namespace(_text(body, "namespace")),
            community_id=CommunityId(_uuid(body, "community_id")),
            case_id=CaseId(_uuid(body, "case_id")),
        )
    except (TypeError, ValueError) as error:
        raise FenceRequestError("a release request is not well formed") from error
    return scope, ExecutionId(_uuid(body, "execution_id"))


def encode_fence_outcome(outcome: SendAuthorizationOutcome) -> dict[str, Any]:
    """The frozen answer: a grant carrying the fence, or a denial carrying reason codes only.

    A denial carries **no fence and no diagnostics beyond the closed codes**. The sender must be
    able to tell "denied" from "unreachable", and it must learn nothing else -- a denial that
    described the case would be a case description delivered to the one principal denied Core.
    """

    match outcome:
        case SendAuthorizationDenied(reason_codes=codes):
            return {"outcome": "DENIED", "reason_codes": list(codes)}
        case SendAuthorizationGranted(fence=fence, replayed=replayed):
            return {
                "outcome": "GRANTED",
                "replayed": replayed,
                "fence": {
                    "namespace": fence.namespace.value,
                    "community_id": str(fence.community_id),
                    "case_id": str(fence.case_id),
                    "execution_id": str(fence.execution_id),
                    "action_id": str(fence.action_id),
                    "approval_id": str(fence.approval_id),
                    "view_id": str(fence.view_id),
                    "authorization_snapshot_hash": fence.authorization_snapshot_hash.value,
                    "acquired_at": format_utc(fence.acquired_at),
                    "expires_at": format_utc(fence.expires_at),
                },
            }
        case _:  # pragma: no cover - the outcome union is closed
            raise AssertionError("unreachable send authorization outcome")


def _object(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise FenceRequestError("a fence request is not an object")
    return payload


def _text(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise FenceRequestError(f"a fence request is missing {name}")
    return value


def _number(body: dict[str, Any], name: str) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise FenceRequestError(f"a fence request is missing {name}")
    return value


def _instant(body: dict[str, Any], name: str) -> datetime:
    """Parse the instant the sender's encoder actually produces.

    :func:`~chorus.infrastructure.compiler.send_authorization.encode_authorization_request`
    serializes with ``datetime.isoformat()``, which emits ``+00:00`` and omits the fractional
    part when it is zero -- not the canonical ``...Z`` form ``parse_utc`` accepts. Its own
    response decoder already reads instants with ``fromisoformat`` for the same reason, so this
    is the *frozen* wire format being read as it is written rather than a second one being
    invented here. ``require_utc`` still refuses anything that is not an aware UTC instant.
    """

    try:
        return require_utc(datetime.fromisoformat(_text(body, name)))
    except (TypeError, ValueError) as error:
        raise FenceRequestError(f"{name} is not a UTC instant") from error


def _uuid(body: dict[str, Any], name: str) -> UUID:
    raw = _text(body, name)
    try:
        parsed = UUID(raw)
    except ValueError as error:
        raise FenceRequestError(f"{name} is not a UUID") from error
    if str(parsed) != raw:
        raise FenceRequestError(f"{name} is not canonical")
    return parsed


__all__ = [
    "FENCE_PURPOSE",
    "FenceRequestError",
    "build_send_authorization",
    "decode_fence_request",
    "decode_release_request",
    "encode_fence_outcome",
]
