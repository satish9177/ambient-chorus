"""RFC 9457 responses that describe a failure without describing the data that caused it.

Every handler here maps a closed error type onto a fixed status, a stable code, and a fixed
sentence. The detail text is written in this file and nowhere else: it never interpolates a
request field, an identifier the caller supplied, an exception message, or an SDK response, so
no error path can become a channel for the private content that produced it.

Absence and foreignness are answered identically. A caller who is not entitled to a case must
not be able to tell "that case does not exist" from "that case exists and is not yours", so
both return the same 404 body.

Two fields in a Problem Details document look harmless and are not, because a caller chooses
what goes in them: ``instance`` and the field paths under ``errors``. Both are built here from
things the *application* declared -- a resolved route template, an allowlist of schema field
names -- and never from anything that arrived in the request. A path segment or a URL segment
is caller-supplied text, and text a caller supplies is text a caller can choose to be a health
condition, an apartment number, or a name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from chorus.domain.errors import DomainError, DomainErrorCode
from chorus.ports.agents import AgentError, AgentErrorCode
from chorus.ports.errors import PersistenceError, PersistenceErrorCode

CORRELATION_HEADER: Final = "X-Correlation-Id"
PROBLEM_MEDIA_TYPE: Final = "application/problem+json"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProblemShape:
    """The fixed presentation of one closed error code."""

    status: int
    title: str
    detail: str
    retryable: bool


_DOMAIN_PROBLEMS: Final[dict[DomainErrorCode, ProblemShape]] = {
    DomainErrorCode.VALIDATION_ERROR: ProblemShape(
        status=422,
        title="Request is not valid",
        detail="The request did not satisfy the contract for this endpoint.",
        retryable=False,
    ),
    DomainErrorCode.STATE_TRANSITION_ERROR: ProblemShape(
        status=409,
        title="State transition is not allowed",
        detail="Reload the current state and retry the command against it.",
        retryable=False,
    ),
    DomainErrorCode.STALE_VERSION: ProblemShape(
        status=409,
        title="Version is stale",
        detail="Reload the current version and resubmit.",
        retryable=False,
    ),
    DomainErrorCode.INTEGRITY_ERROR: ProblemShape(
        status=500,
        title="Stored data failed an integrity check",
        detail="The request was refused. An operator has to inspect this record.",
        retryable=False,
    ),
}

_PERSISTENCE_PROBLEMS: Final[dict[PersistenceErrorCode, ProblemShape]] = {
    PersistenceErrorCode.NOT_FOUND: ProblemShape(
        status=404,
        title="Resource not found",
        detail="No resource is available at this address for this caller.",
        retryable=False,
    ),
    PersistenceErrorCode.CROSS_CASE_VIOLATION: ProblemShape(
        status=404,
        title="Resource not found",
        detail="No resource is available at this address for this caller.",
        retryable=False,
    ),
    PersistenceErrorCode.PERSISTENCE_CONFLICT: ProblemShape(
        status=409,
        title="Concurrent modification",
        detail="Reload the current version and retry the command.",
        retryable=False,
    ),
    PersistenceErrorCode.IDEMPOTENCY_CONFLICT: ProblemShape(
        status=409,
        title="Idempotency key is bound to a different request",
        detail="Use a new idempotency key, or resend the original request unchanged.",
        retryable=False,
    ),
    PersistenceErrorCode.INVALID_CURSOR: ProblemShape(
        status=422,
        title="Pagination cursor is not valid",
        detail="Restart the listing without a cursor.",
        retryable=False,
    ),
    PersistenceErrorCode.MODEL_LIMIT_EXCEEDED: ProblemShape(
        status=422,
        title="Frozen model limit exceeded",
        detail="This case has reached a V1 size limit.",
        retryable=False,
    ),
    PersistenceErrorCode.TRANSACTION_LIMIT_EXCEEDED: ProblemShape(
        status=422,
        title="Transaction limit exceeded",
        detail="The command needed more writes than one transaction permits.",
        retryable=False,
    ),
    PersistenceErrorCode.UNAUDITED_MUTATION: ProblemShape(
        status=500,
        title="Mutation was refused",
        detail="The request was refused because it would not have been audited.",
        retryable=False,
    ),
    PersistenceErrorCode.DEPENDENCY_REJECTED: ProblemShape(
        status=503,
        title="Storage dependency failed",
        detail="A storage dependency rejected the request.",
        retryable=False,
    ),
    PersistenceErrorCode.DEPENDENCY_UNAVAILABLE: ProblemShape(
        status=503,
        title="Storage dependency unavailable",
        detail="A storage dependency was unavailable. Retry the command.",
        retryable=True,
    ),
    PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME: ProblemShape(
        status=503,
        title="Storage outcome is unknown",
        detail="The outcome could not be established. Do not retry; poll for current state.",
        retryable=False,
    ),
}

_AGENT_PROBLEMS: Final[dict[AgentErrorCode, ProblemShape]] = {
    AgentErrorCode.AGENT_CONTRACT_VIOLATION: ProblemShape(
        status=502,
        title="Agent output was refused",
        detail="The agent answer failed deterministic validation and was not applied.",
        retryable=False,
    ),
    AgentErrorCode.AGENT_TIMEOUT: ProblemShape(
        status=504,
        title="Agent did not answer in time",
        detail="No output was produced. The command may be issued again.",
        retryable=True,
    ),
    AgentErrorCode.AGENT_DEPENDENCY_ERROR: ProblemShape(
        status=503,
        title="Agent runtime unavailable",
        detail="The agent runtime could not be reached.",
        retryable=False,
    ),
}


def safe_instance(request: Request) -> str | None:
    """The route template this request resolved to, or nothing at all.

    ``request.url.path`` is caller-controlled, and a 404 that echoed it would answer
    ``GET /v1/PRIVATE_HEALTH_DETAIL`` by repeating the caller's own words back to them --
    into a response body, and from there into whatever logs or screenshots the body reaches.
    A path *parameter* is no better: an operation identifier or a case identifier that failed
    to parse is still text somebody chose.

    So the value comes from the framework's resolved route object, which is a template written
    in this repository (``/v1/operations/{operation_id}``) and contains no request data. When
    no route matched -- an unknown URL, a bad method -- there is no template, and the field is
    omitted rather than filled in with something safe-looking.
    """

    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if not isinstance(path, str) or not path:
        return None
    root = request.scope.get("root_path")
    prefix = root if isinstance(root, str) else ""
    return f"{prefix}{path}"


def problem_response(
    *,
    code: str,
    shape: ProblemShape,
    instance: str | None,
    correlation_id: UUID,
    reason_codes: tuple[str, ...] = (),
    extra: Mapping[str, object] | None = None,
) -> JSONResponse:
    """Build one Problem Details response with the frozen extension fields.

    ``extra`` exists for the one caller that has more than reason codes to report -- request
    validation, which names bounded safe items. Every value it may carry is built inside this
    module from closed vocabularies; nothing a caller sent is ever routed through it.

    ``instance`` is omitted entirely when it is ``None``. RFC 9457 makes the field optional,
    and an absent field says less than a present one that had to be invented.
    """

    body: dict[str, object] = {
        "type": f"urn:chorus:error:{code.lower().replace('_', '-')}",
        "title": shape.title,
        "status": shape.status,
        "code": code,
        "detail": shape.detail,
        "correlation_id": str(correlation_id),
        "retryable": shape.retryable,
        "errors": list(reason_codes),
    }
    if instance is not None:
        body["instance"] = instance
    if extra is not None:
        body.update(extra)
    return JSONResponse(
        status_code=shape.status,
        content=body,
        media_type=PROBLEM_MEDIA_TYPE,
        headers={CORRELATION_HEADER: str(correlation_id), "Cache-Control": "no-store"},
    )


_INTERNAL = ProblemShape(
    status=500,
    title="Internal error",
    detail="The request failed. Quote the correlation identifier when reporting it.",
    retryable=False,
)


def register_problem_handlers(app: FastAPI) -> None:
    """Install the closed error-to-response mapping on one application."""

    @app.exception_handler(DomainError)
    async def _domain(request: Request, error: DomainError) -> JSONResponse:
        shape = _DOMAIN_PROBLEMS.get(error.code, _INTERNAL)
        return problem_response(
            code=error.code.value,
            shape=shape,
            instance=safe_instance(request),
            correlation_id=correlation_id_of(request),
        )

    @app.exception_handler(PersistenceError)
    async def _persistence(request: Request, error: PersistenceError) -> JSONResponse:
        shape = _PERSISTENCE_PROBLEMS.get(error.code, _INTERNAL)
        return problem_response(
            code=error.code.value,
            shape=shape,
            instance=safe_instance(request),
            correlation_id=correlation_id_of(request),
        )

    @app.exception_handler(AgentError)
    async def _agent(request: Request, error: AgentError) -> JSONResponse:
        shape = _AGENT_PROBLEMS.get(error.code, _INTERNAL)
        return problem_response(
            code=error.code.value,
            shape=shape,
            instance=safe_instance(request),
            correlation_id=correlation_id_of(request),
            # Reason codes are a closed enum. They name which gate refused the answer without
            # revealing the identifier, quotation, or text that failed it.
            reason_codes=error.reason_codes,
        )


def correlation_id_of(request: Request) -> UUID:
    """Return the correlation identifier this request is being handled under."""

    value = getattr(request.state, "correlation_id", None)
    if isinstance(value, UUID):
        return value
    return UUID(int=0)


# ---------------------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------------------

MAX_REPORTED_VALIDATION_ITEMS: Final = 20
"""How many field-level items one validation problem may name.

A rejected body can contain thousands of invalid items. The response has to stay bounded
regardless, so the list is truncated and the remainder is reported as a count.
"""

MAX_PATH_SEGMENTS: Final = 12
MAX_PATH_LENGTH: Final = 200
MAX_PATH_INDEX: Final = 1_000_000

TRANSPORT_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        # request locations the validation library names
        "body",
        "query",
        "path",
        "header",
        "cookie",
        # IngestMessagesRequest
        "community_id",
        "messages",
        # IngestMessageRequest
        "adapter",
        "channel_message_id",
        "contributor_id",
        "sent_at",
        "text",
        "attachments",
        # IngestAttachmentRequest
        "evidence_id",
        "media_type",
        "byte_length",
        "sha256",
        # feed and operation query/path parameters
        "limit",
        "cursor",
        "operation_id",
        "Idempotency-Key",
        "X-Chorus-Demo-Actor",
    }
)
"""Every field name a validation problem is allowed to say out loud.

An allowlist, and deliberately not a pattern. Pydantic puts the *offending key* into ``loc``
for an unexpected field, and that key is written by the caller -- so a regex asking "does this
look like an identifier?" answers yes to ``PRIVATE_HEALTH_DETAIL`` and to
``motherLeelaAsthma4B``, and the response then repeats the exact thing this system exists to
keep private. Safety cannot be decided from syntax, because the attacker chooses the syntax.

Anything not on this list is rendered as ``?``, at every depth. A caller who genuinely
mistyped a declared field still gets a usable path down to the object that was wrong; a caller
probing for an echo gets a question mark.
"""


class ValidationCategory(StrEnum):
    """The closed vocabulary a validation item may report.

    A category says what kind of rule was broken -- not what the value was. There is
    deliberately no category that could only be explained by quoting the input.
    """

    MISSING = "MISSING"
    UNEXPECTED_FIELD = "UNEXPECTED_FIELD"
    WRONG_TYPE = "WRONG_TYPE"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    MALFORMED_SYNTAX = "MALFORMED_SYNTAX"
    NOT_ALLOWED_VALUE = "NOT_ALLOWED_VALUE"
    INVALID_VALUE = "INVALID_VALUE"


_CATEGORY_BY_TOKEN: Final[tuple[tuple[str, ValidationCategory], ...]] = (
    ("missing", ValidationCategory.MISSING),
    ("extra_forbidden", ValidationCategory.UNEXPECTED_FIELD),
    ("json_invalid", ValidationCategory.MALFORMED_SYNTAX),
    ("json_type", ValidationCategory.MALFORMED_SYNTAX),
    ("jsondecode", ValidationCategory.MALFORMED_SYNTAX),
    ("too_short", ValidationCategory.OUT_OF_RANGE),
    ("too_long", ValidationCategory.OUT_OF_RANGE),
    ("greater_than", ValidationCategory.OUT_OF_RANGE),
    ("less_than", ValidationCategory.OUT_OF_RANGE),
    ("multiple_of", ValidationCategory.OUT_OF_RANGE),
    ("literal_error", ValidationCategory.NOT_ALLOWED_VALUE),
    ("enum", ValidationCategory.NOT_ALLOWED_VALUE),
    ("pattern_mismatch", ValidationCategory.INVALID_VALUE),
    ("finite_number", ValidationCategory.INVALID_VALUE),
    ("uuid", ValidationCategory.INVALID_VALUE),
    ("datetime", ValidationCategory.INVALID_VALUE),
    ("parsing", ValidationCategory.WRONG_TYPE),
    ("type", ValidationCategory.WRONG_TYPE),
)
"""Map the validation library's error identifier onto a safe category.

Only the *identifier* is read. ``msg``, ``input``, ``ctx``, and ``url`` are never consulted:
``msg`` interpolates the rejected value for several error types, and ``input`` is the
rejected value itself.
"""

_SAFE_ERROR_TYPE: Final = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


def _validation_category(error_type: str) -> ValidationCategory:
    for token, category in _CATEGORY_BY_TOKEN:
        if token in error_type:
            return category
    return ValidationCategory.INVALID_VALUE


def _safe_code(error_type: str) -> str:
    """Return the library's error identifier, or a placeholder if it is not an identifier.

    The identifier is a fixed string from the validation library rather than anything the
    caller wrote, but it is pattern-checked anyway: a library that ever put caller text in
    this field must not be the reason a request body leaks.
    """

    if _SAFE_ERROR_TYPE.fullmatch(error_type) is None:
        return "INVALID"
    return error_type.upper().replace(".", "_")


def _safe_path(location: tuple[object, ...]) -> str:
    """Render one field path from allowlisted names and bounded array indices only.

    No string segment is ever copied out of the validation report. Either it is a field name
    this application declared, in which case it is already public knowledge, or it is ``?``.
    """

    segments: list[str] = []
    for raw in location[:MAX_PATH_SEGMENTS]:
        if isinstance(raw, bool):
            segments.append("?")
        elif isinstance(raw, int):
            segments.append(str(raw) if 0 <= raw < MAX_PATH_INDEX else "?")
        elif isinstance(raw, str) and raw in TRANSPORT_FIELD_NAMES:
            segments.append(raw)
        else:
            segments.append("?")
    if len(location) > MAX_PATH_SEGMENTS:
        segments.append("...")
    return ".".join(segments)[:MAX_PATH_LENGTH] or "body"


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationItem:
    """One bounded, safe description of a rejected field."""

    code: str
    path: str
    category: ValidationCategory

    def as_json(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "category": self.category.value}


def safe_validation_items(
    raw_errors: Sequence[Mapping[str, object]],
) -> tuple[ValidationItem, ...]:
    """Reduce a validation report to bounded, input-free items.

    This function is the whole privacy boundary for validation failures. It reads exactly two
    keys -- ``type`` and ``loc`` -- and constructs everything else from closed vocabularies,
    so there is no path by which a rejected value reaches a response.
    """

    items: list[ValidationItem] = []
    for entry in raw_errors[:MAX_REPORTED_VALIDATION_ITEMS]:
        error_type = entry.get("type")
        rendered_type = error_type if isinstance(error_type, str) else ""
        location = entry.get("loc")
        rendered_location = tuple(location) if isinstance(location, tuple | list) else ()
        items.append(
            ValidationItem(
                code=_safe_code(rendered_type),
                path=_safe_path(rendered_location),
                category=_validation_category(rendered_type),
            )
        )
    return tuple(items)


_VALIDATION = ProblemShape(
    status=422,
    title="Request is not valid",
    detail="The request did not satisfy the contract for this endpoint.",
    retryable=False,
)

_HTTP_PROBLEMS: Final[dict[int, ProblemShape]] = {
    400: ProblemShape(
        status=400,
        title="Request could not be read",
        detail="The request could not be read as a valid command.",
        retryable=False,
    ),
    401: ProblemShape(
        status=401,
        title="Authentication is required",
        detail="This surface requires an authenticated caller.",
        retryable=False,
    ),
    403: ProblemShape(
        status=403,
        title="Caller is not permitted",
        detail="This caller may not use this surface.",
        retryable=False,
    ),
    404: ProblemShape(
        status=404,
        title="Resource not found",
        detail="No resource is available at this address for this caller.",
        retryable=False,
    ),
    405: ProblemShape(
        status=405,
        title="Method is not allowed",
        detail="This method is not available at this address.",
        retryable=False,
    ),
    409: ProblemShape(
        status=409,
        title="Concurrent modification",
        detail="Reload the current version and retry the command.",
        retryable=False,
    ),
    413: ProblemShape(
        status=413,
        title="Request is too large",
        detail="The request exceeded the frozen size bound for this endpoint.",
        retryable=False,
    ),
    415: ProblemShape(
        status=415,
        title="Media type is not supported",
        detail="This endpoint accepts JSON only.",
        retryable=False,
    ),
    422: _VALIDATION,
    429: ProblemShape(
        status=429,
        title="Too many requests",
        detail="Slow down and retry the command.",
        retryable=True,
    ),
}

_HTTP_CODES: Final[dict[int, str]] = {
    400: "MALFORMED_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "PERSISTENCE_CONFLICT",
    413: "REQUEST_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
}


def validation_problem(*, request: Request, error: RequestValidationError) -> JSONResponse:
    """Answer a rejected request without describing the data that caused the rejection."""

    try:
        raw_errors = error.errors()
    except Exception:  # pragma: no cover - a report we cannot read is never quoted
        raw_errors = []
    items = safe_validation_items(raw_errors)
    extra: dict[str, object] = {"errors": [item.as_json() for item in items]}
    omitted = max(len(raw_errors) - len(items), 0)
    if omitted:
        extra["omitted_error_count"] = omitted
    return problem_response(
        code="VALIDATION_ERROR",
        shape=_VALIDATION,
        instance=safe_instance(request),
        correlation_id=correlation_id_of(request),
        extra=extra,
    )


def register_transport_handlers(app: FastAPI) -> None:
    """Install the CHORUS-owned validation, HTTP, and catch-all handlers.

    FastAPI's own ``RequestValidationError`` handler serializes ``exc.errors()`` verbatim,
    including each rejected ``input`` value. For a body carrying private community text that
    turns a 422 into a disclosure channel, so it is replaced rather than configured.
    """

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, RequestValidationError):  # pragma: no cover - handler wiring
            raise error
        return validation_problem(request=request, error=error)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, StarletteHTTPException):  # pragma: no cover - handler wiring
            raise error
        status = error.status_code
        shape = _HTTP_PROBLEMS.get(status)
        if shape is None:
            shape = ProblemShape(
                status=status if 400 <= status <= 599 else 500,
                title="Request was refused",
                detail="The request was refused.",
                retryable=False,
            )
        # The exception's own ``detail`` is deliberately not read. It is a free-form string,
        # and a dependency that ever interpolated a header or a path parameter into it would
        # echo caller input straight back out of this handler.
        return problem_response(
            code=_HTTP_CODES.get(status, "REQUEST_REFUSED"),
            shape=shape,
            instance=safe_instance(request),
            correlation_id=correlation_id_of(request),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, error: Exception) -> JSONResponse:
        """Answer an unmapped failure with a correlation identifier and nothing else."""

        return problem_response(
            code="INTERNAL_ERROR",
            shape=_INTERNAL,
            instance=safe_instance(request),
            correlation_id=correlation_id_of(request),
        )
