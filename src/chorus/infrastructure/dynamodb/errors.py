"""Single translation point from botocore exceptions to closed CHORUS errors.

Nothing from an AWS response crosses this boundary: no message text, no response body, no
request payload, no item content, no credentials. Only an allowlisted service error *code* is
inspected, and even that is never echoed into the raised error; an unrecognised code degrades
to a generic dependency failure.

Write ambiguity is treated conservatively. Anything that might have committed becomes
``UnknownTransactionOutcomeError``, which the unit of work is required to resolve by reading
the transaction's own commit proof rather than by retrying.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
    ResponseStreamingError,
)

from chorus.ports.errors import (
    ExternalDependencyError,
    IdempotencyConflictError,
    PersistenceConflictError,
    PersistenceError,
    PersistenceErrorCode,
    UnknownTransactionOutcomeError,
)


class DynamoOperation(StrEnum):
    """Operation family, which decides how an ambiguous failure is classified."""

    READ = "READ"
    WRITE = "WRITE"
    TRANSACTION = "TRANSACTION"


CONDITIONAL_FAILURE_CODES: Final = frozenset(
    {"ConditionalCheckFailedException", "TransactionConflictException"}
)

IDEMPOTENCY_CODES: Final = frozenset({"IdempotentParameterMismatchException"})

DEFINITE_REJECTION_CODES: Final = frozenset(
    {
        "ValidationException",
        "ResourceNotFoundException",
        "AccessDeniedException",
        "ItemCollectionSizeLimitExceededException",
        "RequestLimitExceeded",
    }
)

AMBIGUOUS_WRITE_CODES: Final = frozenset(
    {
        "InternalServerError",
        "ServiceUnavailable",
        "ThrottlingException",
        "ProvisionedThroughputExceededException",
        "TransactionInProgressException",
    }
)

_CANCELLATION_CONDITION_REASON: Final = "ConditionalCheckFailed"
_CANCELLATION_CONFLICT_REASON: Final = "TransactionConflict"


def _cancellation_reasons(error: ClientError) -> tuple[str, ...]:
    """Return only the reason codes; cancellation messages and items are discarded."""

    raw = error.response.get("CancellationReasons")
    if not isinstance(raw, list):
        return ()
    codes: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        code = entry.get("Code")
        if isinstance(code, str):
            codes.append(code)
    return tuple(codes)


def _service_code(error: ClientError) -> str:
    raw = error.response.get("Error")
    if isinstance(raw, dict):
        code = raw.get("Code")
        if isinstance(code, str):
            return code
    return ""


def map_client_error(error: ClientError, *, operation: DynamoOperation) -> PersistenceError:
    """Translate a botocore ``ClientError`` into exactly one closed CHORUS error."""

    code = _service_code(error)
    if code == "TransactionCanceledException":
        reasons = _cancellation_reasons(error)
        if _CANCELLATION_CONDITION_REASON in reasons:
            return PersistenceConflictError("TRANSACTION")
        if _CANCELLATION_CONFLICT_REASON in reasons:
            return PersistenceConflictError("TRANSACTION")
        return ExternalDependencyError(
            "TRANSACTION",
            code=PersistenceErrorCode.DEPENDENCY_REJECTED,
            retryable=False,
        )
    if code in CONDITIONAL_FAILURE_CODES:
        return PersistenceConflictError(operation.value)
    if code in IDEMPOTENCY_CODES:
        return IdempotencyConflictError(operation.value)
    if code in DEFINITE_REJECTION_CODES:
        return ExternalDependencyError(
            operation.value,
            code=PersistenceErrorCode.DEPENDENCY_REJECTED,
            retryable=False,
        )
    if code in AMBIGUOUS_WRITE_CODES:
        if operation is DynamoOperation.READ:
            return ExternalDependencyError(operation.value)
        return UnknownTransactionOutcomeError(operation.value)
    if operation is DynamoOperation.READ:
        return ExternalDependencyError(operation.value)
    return UnknownTransactionOutcomeError(operation.value)


def map_transport_error(error: Exception, *, operation: DynamoOperation) -> PersistenceError:
    """Translate a transport-level botocore failure without inspecting its message.

    Every classification here describes *one* request attempt, because that is all the
    exception can describe. The client is built with ``SINGLE_ATTEMPT_RETRIES`` so one call
    is one attempt; without that, a connect failure on a later SDK-internal retry would be
    read as evidence about an earlier attempt that had already reached the service.
    """

    if isinstance(error, ClientError):
        return map_client_error(error, operation=operation)
    if isinstance(error, ConnectTimeoutError | EndpointConnectionError):
        # Raised while establishing the connection, before any request byte is written, so
        # this attempt cannot have applied a mutation. Sound only for a single-attempt client.
        return ExternalDependencyError(operation.value)
    if isinstance(error, ReadTimeoutError | ConnectionClosedError | ResponseStreamingError):
        if operation is DynamoOperation.READ:
            return ExternalDependencyError(operation.value)
        return UnknownTransactionOutcomeError(operation.value)
    if operation is DynamoOperation.READ:
        return ExternalDependencyError(operation.value)
    return UnknownTransactionOutcomeError(operation.value)
