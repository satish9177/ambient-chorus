"""``FAILED`` requires proof; everything else is ``SEND_UNKNOWN``.

The most important test in this file is the last one, and it deliberately uses an exception
class that exists nowhere in the production code. The definite side is a **closed frozen set**;
the unknown side is reached by falling off the end. An exception nobody anticipated must
quarantine the execution by construction rather than because somebody remembered to add it.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)

from chorus.infrastructure.ses.classification import (
    THROTTLING_ERROR_CODES,
    classify_exception,
)
from chorus.ports.sender import (
    SendFailureCode,
    SendUnknownReason,
    SesDefiniteFailure,
    SesUnknown,
)


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "a message that must never be persisted"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "SendEmail",
    )


@pytest.mark.parametrize(
    ("error", "code"),
    [
        pytest.param(
            _client_error("MessageRejected", 400),
            SendFailureCode.SES_REJECTED,
            id="message-rejected",
        ),
        pytest.param(
            _client_error("MailFromDomainNotVerifiedException", 400),
            SendFailureCode.SES_REJECTED,
            id="unverified-identity",
        ),
        pytest.param(
            _client_error("BadRequestException", 400),
            SendFailureCode.SES_REJECTED,
            id="validation",
        ),
        pytest.param(
            _client_error("TooManyRequestsException", 429),
            SendFailureCode.SES_DEFINITE_FAILURE,
            id="throttling",
        ),
        pytest.param(
            _client_error("InternalServiceError", 500),
            SendFailureCode.SES_DEFINITE_FAILURE,
            id="server-error",
        ),
    ],
)
def test_a_received_response_is_always_a_definite_failure(
    error: ClientError, code: SendFailureCode
) -> None:
    """A response means SES processed the request and declined it.

    The boundary is drawn at *whether a response was received*, which is observable, rather
    than at what the response said -- so throttling and 5xx are definite too. Widening
    ``SEND_UNKNOWN`` to cover them would quarantine cases for ordinary rate limiting.
    """

    outcome = classify_exception(error)

    assert isinstance(outcome, SesDefiniteFailure)
    assert outcome.failure_code is code


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            EndpointConnectionError(endpoint_url="https://email.us-east-1.amazonaws.com"),
            id="endpoint-unreachable",
        ),
        pytest.param(
            ConnectTimeoutError(endpoint_url="https://email.us-east-1.amazonaws.com"),
            id="connect-timeout",
        ),
        pytest.param(NoCredentialsError(), id="no-credentials"),
    ],
)
def test_a_connection_that_never_completed_is_a_definite_failure(
    error: BaseException,
) -> None:
    """Proof at the transport layer that no request was transmitted."""

    outcome = classify_exception(error)

    assert isinstance(outcome, SesDefiniteFailure)
    assert outcome.failure_code is SendFailureCode.SES_UNREACHABLE


def test_a_read_timeout_is_unknown_because_a_request_was_transmitted() -> None:
    """Between the two proofs. A message may have been queued and nothing will say so."""

    outcome = classify_exception(
        ReadTimeoutError(endpoint_url="https://email.us-east-1.amazonaws.com")
    )

    assert isinstance(outcome, SesUnknown)
    assert outcome.reason_code is SendUnknownReason.SES_TIMEOUT


def test_a_connection_closed_mid_flight_is_unknown() -> None:
    outcome = classify_exception(
        ConnectionClosedError(endpoint_url="https://email.us-east-1.amazonaws.com")
    )

    assert isinstance(outcome, SesUnknown)


def test_unlisted_ses_exception_classifies_as_send_unknown() -> None:
    """The safe side is the default, not the remembered case.

    This exception type exists nowhere in the production code and never will. It classifies as
    unknown because unknown is what the function falls through to -- which is the whole reason
    the definite side is an enumerated list and this branch is not.
    """

    class AnExceptionNobodyAnticipated(Exception):
        pass

    outcome = classify_exception(AnExceptionNobodyAnticipated("from some future SDK"))

    assert isinstance(outcome, SesUnknown)
    assert outcome.reason_code is SendUnknownReason.SES_TRANSPORT_AMBIGUOUS


def test_the_ses_error_message_never_reaches_the_persisted_detail() -> None:
    """Only the *code* is kept. The message is free text SES composes and can name a recipient."""

    outcome = classify_exception(_client_error("MessageRejected", 400))

    assert isinstance(outcome, SesDefiniteFailure)
    assert outcome.detail_safe == "MessageRejected"
    assert "must never be persisted" not in (outcome.detail_safe or "")


def test_throttling_codes_are_a_closed_list_on_the_definite_side() -> None:
    """Named so that moving one of them is a deliberate edit rather than a silent drift."""

    assert "TooManyRequestsException" in THROTTLING_ERROR_CODES
    assert "MessageRejected" not in THROTTLING_ERROR_CODES
