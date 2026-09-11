"""The compile boundary: one request, one compiled view, and no policy on either side.

The deployed API **cannot compile**. Its role carries an explicit
``Deny dynamodb:PutItem/UpdateItem/DeleteItem`` on the ``NS#*#VIEW#*`` and ``NS#*#VIEW_CURRENT#*``
prefixes, because the compiler is the sole creator of views by IAM and not by convention
(deployment contract § 8.1). An in-process :class:`~chorus.application.commands.compile_view.
CompileView` in the request path would therefore be an object that could only fail, and could
only fail in an account -- the identical defect § 8.2 found for the sender's in-process send
authorization.

So ``POST /v1/cases/{case_id}/views`` becomes one synchronous invocation of the compiler
function, and this module is the wire contract for it: ``compile-request/v1`` in,
``compile-response/v1`` out.

Nothing here decides anything
------------------------------
Every gate, every scope rule, every exclusion code, the view hash, the mandate version set, and
the whole denial vocabulary live inside ``chorus.privacy`` and run in the compiler process. This
module serializes a request, invokes, and parses one answer. A branch here on a scope, a
necessity, or a reason code would be a second policy implementation.

It also never invents a permissive answer. A denial is *raised* on the compiler side and travels
as an error, exactly as it does in-process; an answer this module cannot parse raises rather
than resolving to an empty view, because "the compiler could not be reached" and "the compiler
excluded everything" are different facts and only one of them is safe to show a presenter as a
compile result.

Why the view is spelled out field by field
-------------------------------------------
The obvious shortcut is to ship the persisted DynamoDB item. It is rejected: the storage codec
is a *storage* contract, free to change its attribute names for storage reasons, and binding an
inter-function wire format to it would make a persistence refactor a silent protocol break. The
fields below are the frozen ``shareable-case-view/v2`` value object's own, and a round-trip test
over the full structure is what keeps the two ends honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol
from uuid import UUID

from chorus.application.commands.compile_view import (
    CompileViewCommand,
    CompileViewResult,
    ExcludedFactView,
    IncludedFactView,
    RequestedFactInput,
)
from chorus.application.errors import ApplicationError, ApplicationErrorCode
from chorus.domain.entities import (
    DestinationKind,
    DisclosureScope,
    EvidenceStatus,
    FactType,
    Purpose,
)
from chorus.domain.errors import DomainError, DomainErrorCode
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    DestinationId,
    EvidenceItemId,
    ExportFactId,
    FactId,
    Namespace,
    SafeEvidenceRefId,
    Sha256Digest,
    ViewId,
)
from chorus.domain.time import format_utc, parse_utc
from chorus.ports.errors import ExternalDependencyError, PersistenceError, PersistenceErrorCode
from chorus.ports.invocation import SynchronousInvokerPort
from chorus.ports.records import (
    StoredMandateVersionRef,
    StoredSafeDestination,
    StoredSafeEvidenceRef,
    StoredShareableFact,
    StoredShareableView,
    TransformationKind,
)

COMPILE_OPERATION: Final = "CompileView"
COMPILE_REQUEST_SCHEMA: Final = "compile-request/v1"
COMPILE_RESPONSE_SCHEMA: Final = "compile-response/v1"


class CompileFailureKind(StrEnum):
    """The closed set of failure shapes a compile may report across the boundary.

    Mirrors :class:`chorus.application.send_contract.SendFailureKind` -- the same argument
    applies: the caller (the API route, through ``chorus_api.problem_details``) branches on
    *which family* a failure belongs to, so the family has to travel with the safe code rather
    than collapse into one generic dependency rejection.
    """

    DOMAIN = "DOMAIN"
    APPLICATION = "APPLICATION"
    PERSISTENCE = "PERSISTENCE"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    """The write may or may not have committed. Reconstructed as the ambiguous persistence
    type, never settled into a definite one -- exactly as the sender's own outcome is."""


class CompileViewRunner(Protocol):
    """Compile one safe view, wherever the compiler happens to run.

    Declared so a caller -- the compile route's container -- holds *the compiler* rather than one
    of the two ways of reaching it. The in-process
    :class:`~chorus.application.commands.compile_view.CompileView` and the remote
    :class:`RemoteCompileView` both satisfy it, and no route branches on which.
    """

    async def execute(self, command: CompileViewCommand) -> CompileViewResult:
        """Return the compiled view. A denial is raised, never returned."""


class CompileRequestError(ValueError):
    """A delivered compile request is not one this compiler can run.

    Raised while parsing, before a repository is touched, so a malformed request costs a
    validation and nothing else.
    """


def _unusable() -> ExternalDependencyError:
    return ExternalDependencyError(
        COMPILE_OPERATION, code=PersistenceErrorCode.DEPENDENCY_REJECTED, retryable=False
    )


# -- request ---------------------------------------------------------------------------------


def encode_destination(destination: StoredSafeDestination) -> dict[str, Any]:
    return {
        "destination_id": str(destination.destination_id),
        "kind": destination.kind.value,
        "registry_version": destination.registry_version,
        "routing_token": str(destination.routing_token),
        "display_label": destination.display_label,
    }


def decode_destination(raw: object) -> StoredSafeDestination:
    body = _object(raw, "destination")
    return StoredSafeDestination(
        destination_id=DestinationId(_text(body, "destination_id")),
        kind=DestinationKind(_text(body, "kind")),
        registry_version=_number(body, "registry_version"),
        routing_token=_uuid(body, "routing_token"),
        display_label=_text(body, "display_label"),
    )


def encode_compile_request(command: CompileViewCommand) -> dict[str, Any]:
    """The frozen wire shape of one compile request, field for field.

    The **namespace, community, and destination are not caller fields** here any more than they
    are in the route: they come from the API's own composition, so a compile can never be aimed
    at another community or toward a destination nobody registered.
    """

    return {
        "schema": COMPILE_REQUEST_SCHEMA,
        "namespace": command.namespace.value,
        "community_id": str(command.community_id),
        "case_id": str(command.case_id),
        "compile_id": str(command.compile_id),
        "expected_case_version": command.expected_case_version,
        "requested_facts": [
            {
                "fact_id": str(item.fact_id),
                "necessity": item.necessity,
                "intended_usage": item.intended_usage,
            }
            for item in command.requested_facts
        ],
        "requested_evidence_ids": [str(value) for value in command.requested_evidence_ids],
        "destination": encode_destination(command.destination),
        "purpose": command.purpose.value,
        "actor_id_hash": command.actor_id_hash.value,
        "idempotency_key": command.idempotency_key,
        "correlation_id": (None if command.correlation_id is None else str(command.correlation_id)),
    }


def decode_compile_request(payload: object) -> CompileViewCommand:
    """Parse one delivered compile request exactly, or refuse it before anything is read."""

    body = _object(payload, "request")
    if body.get("schema") != COMPILE_REQUEST_SCHEMA:
        raise CompileRequestError("a compile request names an unknown schema version")
    raw_facts = body.get("requested_facts")
    if not isinstance(raw_facts, list):
        raise CompileRequestError("a compile request names no facts")
    raw_evidence = body.get("requested_evidence_ids")
    if not isinstance(raw_evidence, list):
        raise CompileRequestError("a compile request names no evidence list")
    correlation = body.get("correlation_id")
    try:
        return CompileViewCommand(
            namespace=Namespace(_text(body, "namespace")),
            community_id=CommunityId(_uuid(body, "community_id")),
            case_id=CaseId(_uuid(body, "case_id")),
            compile_id=_uuid(body, "compile_id"),
            expected_case_version=_number(body, "expected_case_version"),
            requested_facts=tuple(
                RequestedFactInput(
                    fact_id=FactId(_uuid(_object(item, "fact"), "fact_id")),
                    necessity=_text(_object(item, "fact"), "necessity"),
                    intended_usage=_text(_object(item, "fact"), "intended_usage"),
                )
                for item in raw_facts
            ),
            requested_evidence_ids=tuple(
                EvidenceItemId(_parse_uuid(value)) for value in raw_evidence
            ),
            destination=decode_destination(body.get("destination")),
            purpose=Purpose(_text(body, "purpose")),
            actor_id_hash=Sha256Digest(_text(body, "actor_id_hash")),
            idempotency_key=_text(body, "idempotency_key"),
            correlation_id=None if correlation is None else _parse_uuid(correlation),
        )
    except CompileRequestError:
        raise
    except (TypeError, ValueError) as error:
        raise CompileRequestError("a compile request is not well formed") from error


# -- response --------------------------------------------------------------------------------


def encode_view(view: StoredShareableView) -> dict[str, Any]:
    """The compiled view's exact wire shape. Only what the compiler already marked shareable."""

    return {
        "schema_version": view.schema_version,
        "view_id": str(view.view_id),
        "case_id": str(view.case_id),
        "community_public_label": view.community_public_label,
        "case_version": view.case_version,
        "authorization_version": view.authorization_version,
        "policy_version": view.policy_version,
        "compiler_version": view.compiler_version,
        "policy_build_hash": view.policy_build_hash.value,
        "destination": encode_destination(view.destination),
        "purpose": view.purpose.value,
        "generated_at": format_utc(view.generated_at),
        "expires_at": format_utc(view.expires_at),
        "mandate_version_set": [
            {
                "mandate_id": str(ref.mandate_id),
                "version": ref.version,
                "terms_hash": ref.terms_hash.value,
            }
            for ref in view.mandate_version_set
        ],
        "authorization_snapshot_hash": view.authorization_snapshot_hash.value,
        "shareable_facts": [
            {
                "export_fact_id": str(fact.export_fact_id),
                "fact_type": fact.fact_type.value,
                "safe_text": fact.safe_text,
                "effective_scope": fact.effective_scope.value,
                "evidence_status": fact.evidence_status.value,
                "contributor_count": fact.contributor_count,
                "transformation": fact.transformation.value,
                "transformation_rule_id": fact.transformation_rule_id,
                "safe_evidence_ref_ids": [str(value) for value in fact.safe_evidence_ref_ids],
                "content_hash": fact.content_hash.value,
            }
            for fact in view.shareable_facts
        ],
        "safe_evidence_refs": [
            {
                "safe_evidence_ref_id": str(ref.safe_evidence_ref_id),
                "media_type": ref.media_type,
                "export_handle_id": str(ref.export_handle_id),
                "sha256": ref.sha256.value,
                "caption": ref.caption,
                "created_by_rule_id": ref.created_by_rule_id,
                "content_hash": ref.content_hash.value,
            }
            for ref in view.safe_evidence_refs
        ],
        "audit_refs": [str(value) for value in view.audit_refs],
        "view_hash": view.view_hash.value,
    }


def decode_view(raw: object) -> StoredShareableView:
    """Rebuild the compiled view, letting its own invariants refuse a malformed one.

    ``StoredShareableView.__post_init__`` re-checks expiry ordering, positive versions, at least
    one safe fact, and the uniqueness of export-fact and evidence-reference identifiers -- so a
    response that survived the network but not the value object is rejected here rather than
    displayed.
    """

    body = _object(raw, "view")
    return StoredShareableView(
        schema_version=_text(body, "schema_version"),
        view_id=ViewId(_uuid(body, "view_id")),
        case_id=CaseId(_uuid(body, "case_id")),
        community_public_label=_text(body, "community_public_label"),
        case_version=_number(body, "case_version"),
        authorization_version=_number(body, "authorization_version"),
        policy_version=_text(body, "policy_version"),
        compiler_version=_text(body, "compiler_version"),
        policy_build_hash=Sha256Digest(_text(body, "policy_build_hash")),
        destination=decode_destination(body.get("destination")),
        purpose=Purpose(_text(body, "purpose")),
        generated_at=parse_utc(_text(body, "generated_at")),
        expires_at=parse_utc(_text(body, "expires_at")),
        mandate_version_set=tuple(
            StoredMandateVersionRef(
                mandate_id=_uuid(_object(item, "mandate"), "mandate_id"),
                version=_number(_object(item, "mandate"), "version"),
                terms_hash=Sha256Digest(_text(_object(item, "mandate"), "terms_hash")),
            )
            for item in _list(body, "mandate_version_set")
        ),
        authorization_snapshot_hash=Sha256Digest(_text(body, "authorization_snapshot_hash")),
        shareable_facts=tuple(
            StoredShareableFact(
                export_fact_id=ExportFactId(_uuid(_object(item, "fact"), "export_fact_id")),
                fact_type=FactType(_text(_object(item, "fact"), "fact_type")),
                safe_text=_text(_object(item, "fact"), "safe_text"),
                effective_scope=DisclosureScope(_text(_object(item, "fact"), "effective_scope")),
                evidence_status=EvidenceStatus(_text(_object(item, "fact"), "evidence_status")),
                contributor_count=_number(_object(item, "fact"), "contributor_count"),
                transformation=TransformationKind(_text(_object(item, "fact"), "transformation")),
                transformation_rule_id=_text(_object(item, "fact"), "transformation_rule_id"),
                safe_evidence_ref_ids=tuple(
                    SafeEvidenceRefId(_parse_uuid(value))
                    for value in _list(_object(item, "fact"), "safe_evidence_ref_ids")
                ),
                content_hash=Sha256Digest(_text(_object(item, "fact"), "content_hash")),
            )
            for item in _list(body, "shareable_facts")
        ),
        safe_evidence_refs=tuple(
            StoredSafeEvidenceRef(
                safe_evidence_ref_id=SafeEvidenceRefId(
                    _uuid(_object(item, "evidence"), "safe_evidence_ref_id")
                ),
                media_type=_text(_object(item, "evidence"), "media_type"),
                export_handle_id=_uuid(_object(item, "evidence"), "export_handle_id"),
                sha256=Sha256Digest(_text(_object(item, "evidence"), "sha256")),
                caption=_text(_object(item, "evidence"), "caption"),
                created_by_rule_id=_text(_object(item, "evidence"), "created_by_rule_id"),
                content_hash=Sha256Digest(_text(_object(item, "evidence"), "content_hash")),
            )
            for item in _list(body, "safe_evidence_refs")
        ),
        audit_refs=tuple(_parse_uuid(value) for value in _list(body, "audit_refs")),
        view_hash=Sha256Digest(_text(body, "view_hash")),
    )


def encode_compile_response(result: CompileViewResult) -> dict[str, Any]:
    """The frozen answer of an **allowed** compile. A denial never travels this way."""

    return {
        "schema": COMPILE_RESPONSE_SCHEMA,
        "status": "COMPLETED",
        "compile_id": str(result.compile_id),
        "audit_event_id": str(result.audit_event_id),
        "view": None if result.view is None else encode_view(result.view),
        "included": [
            {
                "fact_id": str(item.fact_id),
                "export_fact_ids": [str(value) for value in item.export_fact_ids],
            }
            for item in result.included
        ],
        "excluded": [
            {"fact_id": str(item.fact_id), "reason_codes": list(item.reason_codes)}
            for item in result.excluded
        ],
        "replayed": result.replayed,
    }


def encode_compile_failure(error: Exception) -> dict[str, Any]:
    """Classify one failed compile into its frozen kind and safe code(s), never a traceback.

    The order matters: an ambiguous persistence outcome must be recognised before a definite
    one, because settling it here would record a definite failure for a write that may have
    committed. ``ApplicationError.reason_codes`` -- the closed policy codes a ``PolicyDeniedError``
    names -- are carried across, because they are exactly what the API's 422 response is
    required to preserve (deployment contract's compile parity requirement); nothing else about
    the case, the fact, or the request is.
    """

    if isinstance(error, PersistenceError):
        kind = (
            CompileFailureKind.UNKNOWN_OUTCOME
            if error.code is PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME
            else CompileFailureKind.PERSISTENCE
        )
        return _compile_failure(kind, error.code.value)
    if isinstance(error, ApplicationError):
        return _compile_failure(
            CompileFailureKind.APPLICATION,
            error.code.value,
            reason_codes=error.reason_codes,
            retryable=error.retryable,
        )
    if isinstance(error, DomainError):
        return _compile_failure(CompileFailureKind.DOMAIN, error.code.value)
    raise error


def _compile_failure(
    kind: CompileFailureKind,
    code: str,
    *,
    reason_codes: tuple[str, ...] = (),
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "schema": COMPILE_RESPONSE_SCHEMA,
        "status": "FAILED",
        "kind": kind.value,
        "code": code,
        "reason_codes": list(reason_codes),
        "retryable": retryable,
    }


def _restore_compile_failure(body: dict[str, Any]) -> Exception:
    """Reconstruct the exact exception type :func:`encode_compile_failure` reported.

    Never resolves an ambiguity into a definite answer: an unrecognised kind or code becomes
    the same opaque ``_unusable()`` every other unparseable response does, rather than a
    guessed classification.
    """

    raw_kind = body.get("kind")
    code = body.get("code")
    if not isinstance(raw_kind, str) or not isinstance(code, str):
        return _unusable()
    try:
        kind = CompileFailureKind(raw_kind)
    except ValueError:
        return _unusable()
    raw_reason_codes = body.get("reason_codes")
    if not isinstance(raw_reason_codes, list) or not all(
        isinstance(item, str) for item in raw_reason_codes
    ):
        return _unusable()
    reason_codes = tuple(raw_reason_codes)
    match kind:
        case CompileFailureKind.UNKNOWN_OUTCOME:
            return PersistenceError(
                PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME,
                COMPILE_OPERATION,
                retryable=False,
            )
        case CompileFailureKind.PERSISTENCE:
            try:
                return PersistenceError(
                    PersistenceErrorCode(code), COMPILE_OPERATION, retryable=False
                )
            except ValueError:
                return _unusable()
        case CompileFailureKind.APPLICATION:
            try:
                return ApplicationError(
                    ApplicationErrorCode(code),
                    reason_codes,
                    bool(body.get("retryable", False)),
                )
            except ValueError:
                return _unusable()
        case CompileFailureKind.DOMAIN:
            try:
                return DomainError(DomainErrorCode(code))
            except ValueError:
                return _unusable()
    return _unusable()  # pragma: no cover - the kind set is closed


def decode_compile_response(body: dict[str, Any]) -> CompileViewResult:
    """Parse the compiler's answer, or raise. It never resolves an ambiguity into a result."""

    if body.get("schema") != COMPILE_RESPONSE_SCHEMA:
        raise _unusable()
    status = body.get("status")
    if status == "FAILED":
        raise _restore_compile_failure(body)
    if status != "COMPLETED":
        raise _unusable()
    try:
        raw_view = body.get("view")
        return CompileViewResult(
            compile_id=_uuid(body, "compile_id"),
            audit_event_id=_uuid(body, "audit_event_id"),
            view=None if raw_view is None else decode_view(raw_view),
            included=tuple(
                IncludedFactView(
                    fact_id=FactId(_uuid(_object(item, "included"), "fact_id")),
                    export_fact_ids=tuple(
                        _parse_uuid(value)
                        for value in _list(_object(item, "included"), "export_fact_ids")
                    ),
                )
                for item in _list(body, "included")
            ),
            excluded=tuple(
                ExcludedFactView(
                    fact_id=FactId(_uuid(_object(item, "excluded"), "fact_id")),
                    reason_codes=tuple(
                        _require_text(value)
                        for value in _list(_object(item, "excluded"), "reason_codes")
                    ),
                )
                for item in _list(body, "excluded")
            ),
            replayed=_flag(body, "replayed"),
        )
    except (CompileRequestError, TypeError, ValueError) as error:
        raise _unusable() from error


@dataclass(frozen=True, slots=True)
class RemoteCompileView:
    """The deployed API's whole relationship with the compiler: one synchronous invocation.

    It satisfies the same ``execute`` shape the in-process
    :class:`~chorus.application.commands.compile_view.CompileView` does, so the compile route
    holds one thing and never branches on the deployment. What differs is everything the API is
    not permitted to hold: no privacy compiler, no safe-evidence service, no object store, and
    no write path to a view partition.
    """

    invoker: SynchronousInvokerPort

    async def execute(self, command: CompileViewCommand) -> CompileViewResult:
        body = await self.invoker.invoke(
            operation=COMPILE_OPERATION, payload=encode_compile_request(command)
        )
        return decode_compile_response(body)


# -- readers ---------------------------------------------------------------------------------


def _object(raw: object, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CompileRequestError(f"a compile {name} is not an object")
    return raw


def _list(body: dict[str, Any], name: str) -> list[Any]:
    value = body.get(name)
    if not isinstance(value, list):
        raise CompileRequestError(f"a compile field {name} is not a list")
    return value


def _text(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise CompileRequestError(f"a compile field {name} is missing")
    return value


def _require_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise CompileRequestError("a compile list item is not a string")
    return value


def _number(body: dict[str, Any], name: str) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CompileRequestError(f"a compile field {name} is not a number")
    return value


def _flag(body: dict[str, Any], name: str) -> bool:
    value = body.get(name)
    if not isinstance(value, bool):
        raise CompileRequestError(f"a compile field {name} is not a flag")
    return value


def _parse_uuid(value: object) -> UUID:
    text = _require_text(value)
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise CompileRequestError("a compile identifier is not a UUID") from error
    if str(parsed) != text:
        raise CompileRequestError("a compile identifier is not canonical")
    return parsed


def _uuid(body: dict[str, Any], name: str) -> UUID:
    return _parse_uuid(_text(body, name))


__all__ = [
    "COMPILE_OPERATION",
    "COMPILE_REQUEST_SCHEMA",
    "COMPILE_RESPONSE_SCHEMA",
    "CompileFailureKind",
    "CompileRequestError",
    "CompileViewRunner",
    "RemoteCompileView",
    "decode_compile_request",
    "decode_compile_response",
    "decode_destination",
    "decode_view",
    "encode_compile_failure",
    "encode_compile_request",
    "encode_compile_response",
    "encode_destination",
    "encode_view",
]
