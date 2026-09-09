"""The two internal Lambda transports: what they send, and what they refuse to believe.

Both adapters exist to hold one line: **a transport success is not an application success**.
Synchronously that means checking the SDK status, ``FunctionError``, and the shape of the
decoded body before anything is returned; asynchronously it means checking that the handover was
accepted and then reading nothing at all, because an ``Event`` invocation has no result to read.

**Two status fields, and only one of them proves anything (P2-A, Phase 11 batch 4 final
repair).** The Lambda ``Invoke`` API's own *top-level* ``StatusCode`` is the field that
documents what AWS actually did; ``ResponseMetadata.HTTPStatusCode`` is generic botocore
transport metadata that happens to usually carry the same number. Every "P2-A" test below
constructs a response where the two are deliberately set independently, to prove the top-level
field is what the adapter actually reads -- never the metadata alone, and never whichever one
happens to look best.

The other property under test is negative and structural: the invoked function is the one the
adapter was constructed with, and **no payload can name a different one**. There is no
"invoke an arbitrary ARN" capability anywhere below these two classes.

Every client here is an explicit fake. Nothing resolves a credential and nothing reaches AWS.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from botocore.exceptions import EndpointConnectionError, ResponseStreamingError

from chorus.infrastructure.lambdas.invoker import (
    ACCEPTED_EVENT_STATUS,
    EVENT,
    OK_STATUS,
    REQUEST_RESPONSE,
    AsynchronousLambdaInvoker,
    SynchronousLambdaInvoker,
)
from chorus.ports.errors import ExternalDependencyError, PersistenceErrorCode

TARGET = "arn:aws:lambda:us-east-1:000000000000:function:chorus-thing-demo:live"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeLambda:
    """Records the exact request and answers with whatever the test scripted."""

    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None):
        self.response = response or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def body(
    payload: object,
    *,
    status: int = OK_STATUS,
    metadata_status: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A realistic ``invoke()`` response: the top-level ``StatusCode`` the real API defines,
    plus the ``ResponseMetadata.HTTPStatusCode`` botocore always attaches alongside it.

    ``metadata_status`` defaults to ``status`` -- on a real response the two agree -- and is
    given a different value only to construct a deliberately contradictory one.
    """

    response: dict[str, Any] = {
        "StatusCode": status,
        "ResponseMetadata": {
            "HTTPStatusCode": status if metadata_status is None else metadata_status
        },
        "Payload": io.BytesIO(json.dumps(payload).encode("utf-8")),
    }
    response.update(extra)
    return response


# -- synchronous ---------------------------------------------------------------------------


async def test_a_synchronous_call_sends_request_response_to_the_configured_target() -> None:
    client = FakeLambda(body({"outcome": "GRANTED"}))
    result = await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
        operation="DoThing", payload={"case_id": "abc"}
    )

    assert result == {"outcome": "GRANTED"}
    call = client.calls[0]
    assert call["FunctionName"] == TARGET
    assert call["InvocationType"] == REQUEST_RESPONSE
    assert json.loads(call["Payload"]) == {
        "operation": "DoThing",
        "payload": {"case_id": "abc"},
    }


async def test_the_target_cannot_come_from_the_payload() -> None:
    """A payload naming a function is just data; the adapter invokes what it was built with."""

    client = FakeLambda(body({"ok": True}))
    await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
        operation="DoThing",
        payload={"FunctionName": "arn:aws:lambda:us-east-1:000000000000:function:elsewhere"},
    )
    assert client.calls[0]["FunctionName"] == TARGET


async def test_a_function_error_is_never_an_answer() -> None:
    """The callee raised. That is the authority failing to answer, not a decision."""

    client = FakeLambda(body({"outcome": "GRANTED"}, FunctionError="Unhandled"))
    with pytest.raises(ExternalDependencyError) as raised:
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )
    assert raised.value.code is PersistenceErrorCode.DEPENDENCY_REJECTED


async def test_a_non_two_hundred_status_is_refused() -> None:
    client = FakeLambda(body({"outcome": "GRANTED"}, status=502))
    with pytest.raises(ExternalDependencyError):
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )


async def test_a_body_that_is_not_json_is_refused() -> None:
    client = FakeLambda(
        {
            "StatusCode": OK_STATUS,
            "ResponseMetadata": {"HTTPStatusCode": OK_STATUS},
            "Payload": io.BytesIO(b"not json at all"),
        }
    )
    with pytest.raises(ExternalDependencyError):
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )


async def test_a_body_that_is_not_an_object_is_refused() -> None:
    client = FakeLambda(body(["a", "list"]))
    with pytest.raises(ExternalDependencyError):
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )


async def test_a_transport_failure_is_refused_and_not_retried_here() -> None:
    client = FakeLambda(error=EndpointConnectionError(endpoint_url="https://lambda.invalid"))
    with pytest.raises(ExternalDependencyError) as raised:
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )
    assert raised.value.retryable is False
    assert len(client.calls) == 1


# -- P2-7: a missing or malformed status is a failure, never a pass-through ----------------


async def test_an_empty_response_is_refused() -> None:
    client = FakeLambda({})
    with pytest.raises(ExternalDependencyError):
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )


async def test_a_response_with_no_status_code_anywhere_is_refused() -> None:
    client = FakeLambda({"ResponseMetadata": {}, "Payload": io.BytesIO(b'{"ok": true}')})
    with pytest.raises(ExternalDependencyError):
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )


async def test_a_five_hundred_status_with_a_valid_looking_payload_is_still_refused() -> None:
    client = FakeLambda(body({"outcome": "GRANTED"}, status=500))
    with pytest.raises(ExternalDependencyError):
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )


async def test_a_string_status_code_is_refused_not_coerced() -> None:
    client = FakeLambda(
        {
            "StatusCode": "200",
            "ResponseMetadata": {"HTTPStatusCode": OK_STATUS},
            "Payload": io.BytesIO(b'{"ok": true}'),
        }
    )
    with pytest.raises(ExternalDependencyError):
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )


async def test_a_boolean_status_code_is_refused_not_treated_as_one() -> None:
    """``bool`` is an ``int`` subclass in Python; ``StatusCode: true`` must not read as 1."""

    client = FakeLambda(
        {
            "StatusCode": True,
            "ResponseMetadata": {"HTTPStatusCode": OK_STATUS},
            "Payload": io.BytesIO(b'{"ok": true}'),
        }
    )
    with pytest.raises(ExternalDependencyError):
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )


async def test_missing_payload_is_refused() -> None:
    client = FakeLambda(
        {"StatusCode": OK_STATUS, "ResponseMetadata": {"HTTPStatusCode": OK_STATUS}}
    )
    with pytest.raises(ExternalDependencyError):
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )


async def test_a_payload_stream_that_raises_on_read_is_refused_safely() -> None:
    class ExplodingStream:
        def read(self) -> bytes:
            raise ResponseStreamingError(error=OSError("stream reset"))

    client = FakeLambda(
        {
            "StatusCode": OK_STATUS,
            "ResponseMetadata": {"HTTPStatusCode": OK_STATUS},
            "Payload": ExplodingStream(),
        }
    )
    with pytest.raises(ExternalDependencyError):
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )


async def test_an_os_error_on_payload_read_is_refused_and_the_sentinel_never_leaks() -> None:
    """A raw ``OSError`` from ``Payload.read()`` (not wrapped by botocore) must be caught and
    normalized -- not let escape the adapter -- and the exception's own text must never carry
    the original error's message."""

    sentinel = "sentinel-value-must-not-leak-3f9c2a"

    class ExplodingStream:
        def read(self) -> bytes:
            raise OSError(sentinel)

    client = FakeLambda(
        {
            "StatusCode": OK_STATUS,
            "ResponseMetadata": {"HTTPStatusCode": OK_STATUS},
            "Payload": ExplodingStream(),
        }
    )
    with pytest.raises(ExternalDependencyError) as raised:
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )
    assert sentinel not in str(raised.value)
    assert sentinel not in repr(raised.value)


# -- P2-A: the top-level StatusCode, not ResponseMetadata, is what proves the call ----------


async def test_a_valid_top_level_status_with_no_response_metadata_at_all_is_accepted() -> None:
    """The top-level field alone is sufficient: nothing here depends on metadata existing."""

    client = FakeLambda(
        {"StatusCode": OK_STATUS, "Payload": io.BytesIO(json.dumps({"ok": True}).encode())}
    )
    result = await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
        operation="DoThing", payload={}
    )
    assert result == {"ok": True}


async def test_valid_metadata_with_no_top_level_status_is_refused() -> None:
    """The exact defect: a response whose metadata alone looks like success must not pass.

    Before this repair the adapter read only ``ResponseMetadata.HTTPStatusCode`` and would have
    accepted this response outright. The top-level ``StatusCode`` -- the field the real
    ``Invoke`` contract defines -- is simply absent here, and that must refuse the call on its
    own, independent of how convincing the metadata looks.
    """

    client = FakeLambda(
        {
            "ResponseMetadata": {"HTTPStatusCode": OK_STATUS},
            "Payload": io.BytesIO(json.dumps({"outcome": "GRANTED"}).encode()),
        }
    )
    with pytest.raises(ExternalDependencyError):
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )


async def test_a_bad_top_level_status_is_refused_even_when_metadata_claims_success() -> None:
    client = FakeLambda(body({"outcome": "GRANTED"}, status=500, metadata_status=OK_STATUS))
    with pytest.raises(ExternalDependencyError):
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )


async def test_a_good_top_level_status_is_refused_when_metadata_contradicts_it() -> None:
    """Contradictory transport evidence is refused outright -- neither field is trusted over
    the other when they disagree."""

    client = FakeLambda(body({"outcome": "GRANTED"}, status=OK_STATUS, metadata_status=500))
    with pytest.raises(ExternalDependencyError):
        await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
            operation="DoThing", payload={}
        )


# -- asynchronous --------------------------------------------------------------------------


async def test_a_handover_sends_event_and_expects_no_result() -> None:
    client = FakeLambda(
        {
            "StatusCode": ACCEPTED_EVENT_STATUS,
            "ResponseMetadata": {"HTTPStatusCode": ACCEPTED_EVENT_STATUS},
        }
    )
    # The signature returns ``None``: not "an empty result" but no result at all, because an
    # ``Event`` invocation's response body is empty by definition.
    await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
        operation="MONITOR", payload={"schema": "worker-job/v1"}
    )

    call = client.calls[0]
    assert call["InvocationType"] == EVENT
    assert call["FunctionName"] == TARGET
    assert json.loads(call["Payload"])["operation"] == "MONITOR"


async def test_a_synchronous_status_on_an_event_invocation_is_refused() -> None:
    """``200`` means it ran inline, which is a different call than the one we made."""

    client = FakeLambda(
        {"StatusCode": OK_STATUS, "ResponseMetadata": {"HTTPStatusCode": OK_STATUS}}
    )
    with pytest.raises(ExternalDependencyError):
        await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
            operation="MONITOR", payload={}
        )


async def test_a_failed_handover_is_raised_and_marked_retryable() -> None:
    """A silently dropped handover strands a durable operation nothing knows to run."""

    client = FakeLambda(error=EndpointConnectionError(endpoint_url="https://lambda.invalid"))
    with pytest.raises(ExternalDependencyError) as raised:
        await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
            operation="MONITOR", payload={}
        )
    assert raised.value.retryable is True


# -- P2-7: the same "missing status is a failure" rule applies to the async transport -------


async def test_an_empty_response_to_an_event_invocation_is_refused() -> None:
    client = FakeLambda({})
    with pytest.raises(ExternalDependencyError):
        await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
            operation="MONITOR", payload={}
        )


async def test_a_missing_status_code_on_an_event_invocation_is_refused() -> None:
    client = FakeLambda({"ResponseMetadata": {}})
    with pytest.raises(ExternalDependencyError):
        await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
            operation="MONITOR", payload={}
        )


async def test_a_five_hundred_status_on_an_event_invocation_is_refused() -> None:
    client = FakeLambda({"StatusCode": 500, "ResponseMetadata": {"HTTPStatusCode": 500}})
    with pytest.raises(ExternalDependencyError):
        await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
            operation="MONITOR", payload={}
        )


async def test_a_malformed_status_type_on_an_event_invocation_is_refused() -> None:
    client = FakeLambda(
        {
            "StatusCode": "202",
            "ResponseMetadata": {"HTTPStatusCode": ACCEPTED_EVENT_STATUS},
        }
    )
    with pytest.raises(ExternalDependencyError):
        await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
            operation="MONITOR", payload={}
        )


async def test_a_function_error_alongside_an_accepted_status_is_refused() -> None:
    """Contradictory metadata: an accepted async invocation cannot also have run and raised."""

    client = FakeLambda(
        {
            "StatusCode": ACCEPTED_EVENT_STATUS,
            "ResponseMetadata": {"HTTPStatusCode": ACCEPTED_EVENT_STATUS},
            "FunctionError": "Unhandled",
        }
    )
    with pytest.raises(ExternalDependencyError):
        await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
            operation="MONITOR", payload={}
        )


# -- P2-A: the same top-level-vs-metadata proof, for the Event transport -------------------


async def test_a_valid_top_level_status_with_no_metadata_is_accepted_for_event() -> None:
    client = FakeLambda({"StatusCode": ACCEPTED_EVENT_STATUS})
    await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
        operation="MONITOR", payload={}
    )


async def test_valid_metadata_with_no_top_level_status_is_refused_for_event() -> None:
    """Before this repair, metadata alone -- with no real top-level ``StatusCode`` at all --
    would have been accepted as a completed handover."""

    client = FakeLambda({"ResponseMetadata": {"HTTPStatusCode": ACCEPTED_EVENT_STATUS}})
    with pytest.raises(ExternalDependencyError):
        await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
            operation="MONITOR", payload={}
        )


async def test_a_bad_top_level_status_is_refused_even_when_metadata_claims_acceptance() -> None:
    client = FakeLambda(
        {"StatusCode": 500, "ResponseMetadata": {"HTTPStatusCode": ACCEPTED_EVENT_STATUS}}
    )
    with pytest.raises(ExternalDependencyError):
        await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
            operation="MONITOR", payload={}
        )


async def test_good_top_level_status_refused_on_contradicting_metadata_for_event() -> None:
    client = FakeLambda(
        {"StatusCode": ACCEPTED_EVENT_STATUS, "ResponseMetadata": {"HTTPStatusCode": 500}}
    )
    with pytest.raises(ExternalDependencyError):
        await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
            operation="MONITOR", payload={}
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {"HTTPStatusCode": 500},
        {"HTTPStatusCode": 500.0},
        {"HTTPStatusCode": "500"},
        {"HTTPStatusCode": False},
        {"HTTPStatusCode": None},
        {},
        None,
        [],
        "202",
        False,
    ],
)
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_present_invalid_metadata_is_refused(metadata: object, asynchronous: bool) -> None:
    status = ACCEPTED_EVENT_STATUS if asynchronous else OK_STATUS
    client = FakeLambda(body({"ok": True}, status=status))
    client.response["ResponseMetadata"] = metadata
    with pytest.raises(ExternalDependencyError):
        if asynchronous:
            await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
                operation="DoThing", payload={}
            )
        else:
            await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
                operation="DoThing", payload={}
            )


@pytest.mark.parametrize("marker", ["", None, False, "Handled", "Unhandled"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_any_function_error_field_is_refused_without_reading_payload(
    marker: object, asynchronous: bool
) -> None:
    class UnreadableStream:
        def read(self) -> bytes:
            pytest.fail("Payload must not be read after FunctionError is detected")

    status = ACCEPTED_EVENT_STATUS if asynchronous else OK_STATUS
    client = FakeLambda(body({"ok": True}, status=status, FunctionError=marker))
    client.response["Payload"] = UnreadableStream()
    with pytest.raises(ExternalDependencyError):
        if asynchronous:
            await AsynchronousLambdaInvoker(client=client, function_name=TARGET).dispatch(
                operation="DoThing", payload={}
            )
        else:
            await SynchronousLambdaInvoker(client=client, function_name=TARGET).invoke(
                operation="DoThing", payload={}
            )
