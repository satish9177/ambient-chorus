"""Narrow DynamoDB client protocol and its single boto3 construction point.

The protocol names only the six approved data-plane calls. There is deliberately no ``scan``
member, so no repository can reach one even by mistake, and a static scan of this package
finds no scan access path.

The client is also the place SDK-level retrying is switched off. CHORUS decides for itself
whether a write may be attempted again, and it decides only after reading the transaction's
own commit proof. A retry the SDK performs on its own behalf breaks that: the caller is
handed one exception describing the *last* attempt, so an earlier attempt that reached the
service -- and may have committed -- disappears from the record the error classifier sees.
"""

from __future__ import annotations

from typing import Any, Final, Protocol, TypedDict, cast

from botocore.config import Config

from chorus.infrastructure.dynamodb.attributes import AttributeMap

SINGLE_ATTEMPT_RETRIES: Final = {"mode": "standard", "total_max_attempts": 1}
"""One driver operation is exactly one request attempt.

``total_max_attempts`` counts the initial request, so ``1`` means nothing is ever retried
inside botocore. It is used rather than ``max_attempts`` because its meaning does not change
between the client argument, ``AWS_MAX_ATTEMPTS``, and the shared config file, and because it
takes precedence over ``max_attempts`` wherever both appear.

Left at its default, botocore applies DynamoDB's ``legacy`` service policy of up to ten
attempts per call. Those attempts reuse the same ``ClientRequestToken``, so DynamoDB itself
deduplicates them -- but the retry is invisible above the SDK, and the exception that finally
surfaces need not describe the attempt that actually reached the service.
"""


class GetItemOutput(TypedDict, total=False):
    Item: AttributeMap


class QueryOutput(TypedDict, total=False):
    Items: list[AttributeMap]
    LastEvaluatedKey: AttributeMap


class BatchGetOutput(TypedDict, total=False):
    Responses: dict[str, list[AttributeMap]]
    UnprocessedKeys: dict[str, dict[str, object]]


class DynamoDbClient(Protocol):
    """The exact DynamoDB surface CHORUS is permitted to call."""

    def get_item(self, **kwargs: object) -> GetItemOutput: ...

    def batch_get_item(self, **kwargs: object) -> BatchGetOutput: ...

    def query(self, **kwargs: object) -> QueryOutput: ...

    def put_item(self, **kwargs: object) -> object: ...

    def delete_item(self, **kwargs: object) -> object: ...

    def transact_write_items(self, **kwargs: object) -> object: ...


def create_dynamodb_client(*, region_name: str, endpoint_url: str | None = None) -> DynamoDbClient:
    """Build the boto3 client with SDK retrying switched off.

    boto3 exposes dynamically generated clients that carry no type information, so this
    function is the one place a cast is required. Everything above it is statically typed
    against ``DynamoDbClient``.

    The retry policy is passed explicitly so it wins over ``AWS_MAX_ATTEMPTS``,
    ``AWS_RETRY_MODE``, and the shared config file: an ambient environment variable must not
    be able to reintroduce a hidden attempt. Connect and read timeouts stay at their defaults.
    """

    import boto3

    client: Any = boto3.client(
        "dynamodb",
        region_name=region_name,
        endpoint_url=endpoint_url,
        config=Config(retries=dict(SINGLE_ATTEMPT_RETRIES)),
    )
    return cast(DynamoDbClient, client)
