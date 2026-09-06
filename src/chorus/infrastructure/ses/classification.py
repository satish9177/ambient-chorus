"""The frozen outcome classification, as a pure function of an exception.

> **``FAILED`` requires proof. Everything else is ``SEND_UNKNOWN``.**

A **received error response** is proof that SES processed the request and declined it; a message
it declined was not queued for delivery. A **failed connection** is proof at the transport layer
that no request was transmitted. Everything between those two proofs, and everything not
enumerated, is unknown (ADR-025 SS 8).

The last part is the load-bearing one, and it is why this module exists separately from the
client. The definite side is a **closed frozen set** written down here; the unknown side is
everything else, reached by falling off the end rather than by anybody remembering to add a
case. An exception class nobody anticipated therefore lands on the safe side *by construction*.

This is a pure function over exception objects, so the whole table is testable without a client,
without credentials, and without a network -- including the case that matters most, which is an
exception type this file has never heard of.
"""

from __future__ import annotations

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)

from chorus.ports.sender import (
    SendFailureCode,
    SendUnknownReason,
    SesDefiniteFailure,
    SesOutcome,
    SesUnknown,
)

UNREACHABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    EndpointConnectionError,
    ConnectTimeoutError,
)
"""Proof that no request was transmitted: the connection never completed.

``NoCredentialsError`` is deliberately **absent**. A missing credential is a misconfiguration
discovered before any socket is opened, but it is not something this module should classify as
a *send* outcome at all -- it is handled separately below so it cannot be mistaken for a
transport proof about a request that was never even attempted.
"""

AMBIGUOUS_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ReadTimeoutError,
    ConnectionClosedError,
)
"""A request was transmitted and no response was received. Enumerated for clarity only.

Listing these changes nothing: they would classify as unknown anyway, because unknown is the
default. They are named so a reader can see that the two obvious ambiguous cases were
considered rather than overlooked.
"""

THROTTLING_ERROR_CODES: frozenset[str] = frozenset(
    {
        "TooManyRequestsException",
        "Throttling",
        "ThrottlingException",
        "SlowDown",
        "RequestThrottled",
        "LimitExceededException",
    }
)
"""Received responses that decline for load rather than for content.

Still **definite**: SES answered, and a request it declined was not queued. The boundary is
drawn at *whether a response was received*, which is observable, rather than at what the
response said -- widening ``SEND_UNKNOWN`` to cover throttling would quarantine cases for
ordinary rate limiting.
"""


def classify_exception(error: BaseException) -> SesOutcome:
    """Map one exception onto its frozen outcome, defaulting to unknown.

    The order is: a received response first, because that is the strongest proof available;
    then the two transport proofs; then everything else, which is unknown.
    """

    if isinstance(error, ClientError):
        return _from_response(error)
    if isinstance(error, NoCredentialsError):
        # No socket was opened and no request was formed, so nothing was transmitted. The same
        # proof a failed connection gives, arrived at earlier.
        return SesDefiniteFailure(
            failure_code=SendFailureCode.SES_UNREACHABLE, detail_safe="NO_CREDENTIALS"
        )
    if isinstance(error, UNREACHABLE_EXCEPTIONS):
        return SesDefiniteFailure(failure_code=SendFailureCode.SES_UNREACHABLE)
    if isinstance(error, ReadTimeoutError):
        return SesUnknown(reason_code=SendUnknownReason.SES_TIMEOUT)
    # Everything else, named and unnamed. This is the fail-safe default and the reason the
    # definite side is a closed list rather than this branch being one.
    return SesUnknown(reason_code=SendUnknownReason.SES_TRANSPORT_AMBIGUOUS)


def _from_response(error: ClientError) -> SesDefiniteFailure:
    """A response was received, so this is definite. Which code it gets is the only question."""

    response = error.response or {}
    metadata = response.get("ResponseMetadata") or {}
    status = metadata.get("HTTPStatusCode")
    code = (response.get("Error") or {}).get("Code") or ""
    if code in THROTTLING_ERROR_CODES or (isinstance(status, int) and status >= 500):
        return SesDefiniteFailure(
            failure_code=SendFailureCode.SES_DEFINITE_FAILURE, detail_safe=_safe_detail(code)
        )
    return SesDefiniteFailure(
        failure_code=SendFailureCode.SES_REJECTED, detail_safe=_safe_detail(code)
    )


def _safe_detail(code: str) -> str | None:
    """Keep an SES error *code* and never a message.

    The code is a closed vocabulary the service publishes; the message is free text SES
    composes, and it can contain the recipient address. Only the first is safe to persist on an
    execution row or to put in an audit event.
    """

    if not code:
        return None
    trimmed = "".join(character for character in code if character.isalnum() or character == "_")
    return trimmed[:64] or None


__all__ = [
    "AMBIGUOUS_EXCEPTIONS",
    "THROTTLING_ERROR_CODES",
    "UNREACHABLE_EXCEPTIONS",
    "classify_exception",
]
