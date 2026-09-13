"""The two internal Lambda transports, each bound to exactly one configured function.

These are the deployed halves of :mod:`chorus.ports.invocation`. They serialize, they invoke,
they check, and they decode -- and they contain no branch on an operation name, no fallback, and
no target a caller can influence.

One resource per adapter, and it comes from deployment configuration
---------------------------------------------------------------------
``function_name`` is the ARN the constructing composition was configured with, and it is the
exact resource the calling role's ``lambda:InvokeFunction`` grant names. It is never read from
the payload, never derived from a request, and never defaulted -- so "no caller-controlled
Lambda ARN" is a property of the object graph rather than a rule somebody remembered.

A transport success is not an application success
--------------------------------------------------
:class:`SynchronousLambdaInvoker` checks three things in order before it returns anything: the
SDK-level status of the invocation itself, the presence of ``FunctionError`` (the callee raised
-- which is the authority failing to answer, never a denial), and that the decoded body is a
JSON object. Any of the three failing raises, because a caller that could not tell "unreachable"
from "answered" would act on the wrong one.

**Two status fields exist, and only one of them proves the invocation (Phase 11 batch 4 final
repair, P2-A).** The Lambda ``Invoke`` API response carries a *top-level* ``StatusCode`` --
``200`` for a completed ``RequestResponse`` call, ``202`` for an accepted ``Event`` call -- which
is the documented invocation-result contract. It also carries ``ResponseMetadata.HTTPStatusCode``,
botocore's own generic transport metadata for *any* API call, which normally mirrors the same
value but is a different field answering a different question. Validating only the metadata
field let a response that never populated the real top-level ``StatusCode`` at all -- or
populated it with something else entirely -- pass as a completed call, so long as the metadata
alone looked right. The top-level ``StatusCode`` is now the field that must independently prove
what AWS actually did; if ``ResponseMetadata.HTTPStatusCode`` is present as well, it must agree
with it, and a response where the two disagree is contradictory evidence, refused exactly like
a missing status.

**A missing status is a failure, never a pass-through.** The exact expected code is required by
exact equality; a response with no top-level ``StatusCode``, or one of the wrong type (a
``bool``, a numeric string, a float) is rejected. ``ResponseMetadata`` is optional, but when
present it must be an object containing a strict integer ``HTTPStatusCode`` matching that code.
A caller that treated "the status could not be determined" as "probably fine" would accept a
malformed SDK response as a completed call. Nothing is
coerced: ``"202"`` is not ``202``, and ``True`` -- an ``int`` subclass in Python -- is not ``1``.

Nothing is retried underneath the caller. ``SINGLE_ATTEMPT_RETRIES`` is pinned on the client for
the reason it is pinned everywhere else in this system: an SDK retry is a second attempt nothing
records, and the caller is the only party that knows whether repeating is safe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from botocore.exceptions import BotoCoreError, ClientError

from chorus.infrastructure.compiler.invoker import (
    LAMBDA_SERVICE_NAME,
    SINGLE_ATTEMPT_RETRIES,
    create_lambda_client,
)
from chorus.ports.errors import ExternalDependencyError, PersistenceErrorCode

REQUEST_RESPONSE: Final = "RequestResponse"
EVENT: Final = "Event"

ACCEPTED_EVENT_STATUS: Final = 202
"""What Lambda answers an accepted asynchronous invocation with, and the only success here.

An ``Event`` invocation returns ``202`` and an empty body. A ``200`` would mean the request was
executed synchronously, which is a different call than the one this adapter believes it made.
"""

OK_STATUS: Final = 200


def _unusable(operation: str) -> ExternalDependencyError:
    """The callee could not be reached or could not be understood. Never an answer."""

    return ExternalDependencyError(
        operation, code=PersistenceErrorCode.DEPENDENCY_REJECTED, retryable=False
    )


def _unreachable(operation: str) -> ExternalDependencyError:
    """The handover did not happen, definitely. Safe to repeat, so it says so."""

    return ExternalDependencyError(
        operation, code=PersistenceErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True
    )


def _strict_int(value: Any) -> int | None:
    """``value`` read as an ``int`` with no coercion, or ``None``.

    ``bool`` is excluded explicitly -- it is an ``int`` subclass in Python, and a malformed
    ``StatusCode: true`` must not be read as a status of ``1``. A numeric string and a float are
    likewise refused rather than converted: a status is an exact SDK-typed integer or it proves
    nothing.
    """

    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    return value


def _top_level_status_of(response: Any) -> int | None:
    """The Lambda ``Invoke`` API's own top-level ``StatusCode``, or ``None``.

    This is the field the documented invocation-result contract actually defines -- ``200`` for
    a completed ``RequestResponse`` call, ``202`` for an accepted ``Event`` call -- and the one
    that must independently prove the invocation (P2-A). It is never read from
    ``ResponseMetadata``, which answers a different question (see :func:`_metadata_status_of`).
    """

    if not isinstance(response, dict):
        return None
    return _strict_int(response.get("StatusCode"))


def _metadata_status_of(response: Any) -> int | None:
    """Botocore's own generic transport ``ResponseMetadata.HTTPStatusCode``, or ``None``.

    Present on every botocore response, for every AWS API, and not specific to what Lambda's
    ``Invoke`` contract means by success. Used only to check that it does not *contradict* the
    top-level status above -- never as a substitute for it.
    """

    if not isinstance(response, dict):
        return None
    metadata = response.get("ResponseMetadata")
    if not isinstance(metadata, dict):
        return None
    return _strict_int(metadata.get("HTTPStatusCode"))


def _proves_status(response: Any, expected: int) -> bool:
    """``True`` only when the top-level ``StatusCode`` independently equals ``expected`` and,
    if ``ResponseMetadata`` is present, it contains a valid, matching ``HTTPStatusCode``.

    Every caller compares against this rather than reading a status directly, so "the top-level
    field is missing", "it names some other code", and "it agrees but the metadata contradicts
    it" are all the same refusal: none of them is a call that proved what it claims to have
    done.
    """

    top = _top_level_status_of(response)
    if top != expected:
        return False
    if "ResponseMetadata" not in response:
        return True
    return _metadata_status_of(response) == expected


@dataclass(slots=True)
class SynchronousLambdaInvoker:
    """``RequestResponse`` against one configured function, with the answer actually parsed."""

    client: Any
    function_name: str

    async def invoke(self, *, operation: str, payload: dict[str, object]) -> dict[str, object]:
        body = json.dumps({"operation": operation, "payload": payload}).encode("utf-8")
        try:
            response = self.client.invoke(
                FunctionName=self.function_name,
                InvocationType=REQUEST_RESPONSE,
                Payload=body,
            )
        except (ClientError, BotoCoreError) as error:
            raise _unusable(operation) from error
        # The top-level StatusCode must independently prove a completed RequestResponse call;
        # a response with no status at all, the wrong code, or metadata that contradicts it are
        # all the same refusal -- see ``_proves_status``.
        if not _proves_status(response, OK_STATUS):
            raise _unusable(operation)
        if "FunctionError" in response:
            # The callee raised. That is the authority failing to answer, not a decision, and
            # the two must never collapse into one value.
            raise _unusable(operation)
        try:
            decoded = json.loads(response["Payload"].read().decode("utf-8"))
        except (
            KeyError,
            ValueError,
            UnicodeDecodeError,
            AttributeError,
            OSError,
            BotoCoreError,
        ) as error:
            # ``OSError``/``BotoCoreError`` cover a streaming-body read failure -- the status
            # and ``FunctionError`` checks above passed, but the payload itself could not be
            # read. That is exactly as unusable as a payload that was never there, and the
            # underlying error's own text never crosses this boundary: ``_unusable`` names only
            # the operation, never the original exception's message.
            raise _unusable(operation) from error
        if not isinstance(decoded, dict):
            raise _unusable(operation)
        return decoded


@dataclass(slots=True)
class AsynchronousLambdaInvoker:
    """``Event`` against one configured function. Accepted, or raised -- never a result.

    The response body of an ``Event`` invocation is empty by definition, so this deliberately
    reads nothing from it beyond the accepted status. A caller expecting a business answer from
    here is a caller that has confused a handover with a call.
    """

    client: Any
    function_name: str

    async def dispatch(self, *, operation: str, payload: dict[str, object]) -> None:
        body = json.dumps({"operation": operation, "payload": payload}).encode("utf-8")
        try:
            response = self.client.invoke(
                FunctionName=self.function_name,
                InvocationType=EVENT,
                Payload=body,
            )
        except (ClientError, BotoCoreError) as error:
            # A failed handover leaves a durable operation that looks entirely healthy with
            # nothing anywhere that knows to run it, so it is raised rather than swallowed --
            # and marked retryable, because the invocation definitely did not happen.
            raise _unreachable(operation) from error
        # The top-level StatusCode must independently prove an accepted Event call -- a
        # response with no status, a non-integer one, any other code, or metadata that
        # contradicts it are all rejected identically -- see ``_proves_status``.
        if not _proves_status(response, ACCEPTED_EVENT_STATUS):
            raise _unreachable(operation)
        if "FunctionError" in response:
            # Contradictory transport metadata: an ``Event`` invocation accepted for
            # asynchronous execution does not run inline and cannot itself have raised yet.
            # Treated as unusable rather than trusted, whichever field turns out to be wrong.
            raise _unreachable(operation)


__all__ = [
    "ACCEPTED_EVENT_STATUS",
    "EVENT",
    "LAMBDA_SERVICE_NAME",
    "OK_STATUS",
    "REQUEST_RESPONSE",
    "SINGLE_ATTEMPT_RETRIES",
    "AsynchronousLambdaInvoker",
    "SynchronousLambdaInvoker",
    "create_lambda_client",
]
