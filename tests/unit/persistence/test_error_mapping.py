"""Category K: SDK failures map to the closed taxonomy and leak nothing."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from chorus.infrastructure.dynamodb.errors import (
    DynamoOperation,
    map_client_error,
    map_transport_error,
)
from chorus.ports.errors import (
    IdempotencyConflictError,
    PersistenceConflictError,
    PersistenceErrorCode,
    UnknownTransactionOutcomeError,
)

SECRET_TEXT = "SECRET_SENTINEL_ITEM_CONTENT"


def client_error(code: str, **extra: Any) -> ClientError:
    response: dict[str, Any] = {
        "Error": {"Code": code, "Message": f"human readable text {SECRET_TEXT}"},
        "ResponseMetadata": {"RequestId": "req-1234"},
    }
    response.update(extra)
    return ClientError(response, "TransactWriteItems")


@pytest.mark.parametrize(
    "code", ["ConditionalCheckFailedException", "TransactionConflictException"]
)
def test_condition_failures_map_to_a_conflict(code: str) -> None:
    mapped = map_client_error(client_error(code), operation=DynamoOperation.WRITE)

    assert isinstance(mapped, PersistenceConflictError)
    assert mapped.code is PersistenceErrorCode.PERSISTENCE_CONFLICT


def test_a_cancelled_transaction_with_a_condition_reason_is_a_conflict() -> None:
    error = client_error(
        "TransactionCanceledException",
        CancellationReasons=[
            {"Code": "None"},
            {
                "Code": "ConditionalCheckFailed",
                "Message": SECRET_TEXT,
                "Item": {"raw": {"S": SECRET_TEXT}},
            },
        ],
    )

    mapped = map_client_error(error, operation=DynamoOperation.TRANSACTION)

    assert isinstance(mapped, PersistenceConflictError)
    assert SECRET_TEXT not in str(mapped)


def test_a_cancelled_transaction_without_a_condition_reason_is_a_definite_rejection() -> None:
    error = client_error(
        "TransactionCanceledException",
        CancellationReasons=[{"Code": "ValidationError", "Message": SECRET_TEXT}],
    )

    mapped = map_client_error(error, operation=DynamoOperation.TRANSACTION)

    assert mapped.code is PersistenceErrorCode.DEPENDENCY_REJECTED
    assert mapped.retryable is False


def test_an_idempotent_parameter_mismatch_is_an_idempotency_conflict() -> None:
    mapped = map_client_error(
        client_error("IdempotentParameterMismatchException"),
        operation=DynamoOperation.TRANSACTION,
    )

    assert isinstance(mapped, IdempotencyConflictError)


@pytest.mark.parametrize(
    "code",
    ["ValidationException", "ResourceNotFoundException", "AccessDeniedException"],
)
def test_definite_rejections_are_not_retryable(code: str) -> None:
    mapped = map_client_error(client_error(code), operation=DynamoOperation.WRITE)

    assert mapped.code is PersistenceErrorCode.DEPENDENCY_REJECTED
    assert mapped.retryable is False


@pytest.mark.parametrize(
    "code", ["InternalServerError", "ServiceUnavailable", "ThrottlingException"]
)
def test_an_ambiguous_write_failure_is_an_unknown_outcome(code: str) -> None:
    mapped = map_client_error(client_error(code), operation=DynamoOperation.TRANSACTION)

    assert isinstance(mapped, UnknownTransactionOutcomeError)
    assert mapped.retryable is False


@pytest.mark.parametrize(
    "code", ["InternalServerError", "ServiceUnavailable", "ThrottlingException"]
)
def test_the_same_failure_on_a_read_is_only_a_dependency_failure(code: str) -> None:
    mapped = map_client_error(client_error(code), operation=DynamoOperation.READ)

    assert mapped.code is PersistenceErrorCode.DEPENDENCY_UNAVAILABLE
    assert not isinstance(mapped, UnknownTransactionOutcomeError)


def test_an_unrecognised_service_code_degrades_conservatively() -> None:
    write = map_client_error(client_error("BrandNewException"), operation=DynamoOperation.WRITE)
    read = map_client_error(client_error("BrandNewException"), operation=DynamoOperation.READ)

    assert isinstance(write, UnknownTransactionOutcomeError)
    assert read.code is PersistenceErrorCode.DEPENDENCY_UNAVAILABLE


def test_a_request_that_never_reached_the_service_is_a_definite_non_commit() -> None:
    for error in (
        ConnectTimeoutError(endpoint_url="http://endpoint.invalid"),
        EndpointConnectionError(endpoint_url="http://endpoint.invalid"),
    ):
        mapped = map_transport_error(error, operation=DynamoOperation.TRANSACTION)

        assert not isinstance(mapped, UnknownTransactionOutcomeError)
        assert mapped.code is PersistenceErrorCode.DEPENDENCY_UNAVAILABLE


def test_a_lost_response_on_a_write_is_an_unknown_outcome() -> None:
    for error in (
        ReadTimeoutError(endpoint_url="http://endpoint.invalid"),
        ConnectionClosedError(endpoint_url="http://endpoint.invalid"),
    ):
        mapped = map_transport_error(error, operation=DynamoOperation.TRANSACTION)

        assert isinstance(mapped, UnknownTransactionOutcomeError)


def test_no_aws_text_or_request_identifier_ever_escapes() -> None:
    errors = [
        map_client_error(client_error(code), operation=operation)
        for code in (
            "ConditionalCheckFailedException",
            "ValidationException",
            "InternalServerError",
            "BrandNewException",
        )
        for operation in DynamoOperation
    ]
    errors.append(
        map_client_error(
            client_error(
                "TransactionCanceledException",
                CancellationReasons=[{"Code": "ConditionalCheckFailed", "Message": SECRET_TEXT}],
            ),
            operation=DynamoOperation.TRANSACTION,
        )
    )

    for mapped in errors:
        rendered = f"{mapped!s} {mapped!r}"
        assert SECRET_TEXT not in rendered
        assert "req-1234" not in rendered
        assert "human readable" not in rendered
