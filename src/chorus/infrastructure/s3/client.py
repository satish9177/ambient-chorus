"""Narrow S3 client protocol and its single boto3 construction point.

The protocol names exactly three calls: ``get_object``, ``head_object``, and ``put_object``.
There is no ``list_objects``, no ``copy_object``, no ``delete_object``, and no
``generate_presigned_url`` member, so no adapter can reach one by mistake and a static scan of
this package finds no access path to them. Deletion belongs to the demo reset role and to
bucket lifecycle rules; presigning belongs to the Phase-10 display endpoint.

SDK retrying is switched off for the same reason it is on the DynamoDB client: CHORUS decides
whether a write may be attempted again, and it decides by reading the object's exact content
address. A retry performed inside botocore hands the caller one exception describing the last
attempt, so an earlier attempt that reached the service -- and may have stored the bytes --
disappears from the record the resolution logic sees.
"""

from __future__ import annotations

from typing import Any, Final, Protocol, TypedDict, cast

from botocore.config import Config

SINGLE_ATTEMPT_RETRIES: Final = {"mode": "standard", "total_max_attempts": 1}
"""One adapter operation is exactly one request attempt. ``1`` counts the initial request."""


class GetObjectOutput(TypedDict, total=False):
    Body: Any
    ContentLength: int
    ContentType: str


class HeadObjectOutput(TypedDict, total=False):
    ContentLength: int
    ContentType: str
    Metadata: dict[str, str]


class S3Client(Protocol):
    """The exact S3 surface CHORUS is permitted to call."""

    def get_object(self, **kwargs: object) -> GetObjectOutput: ...

    def head_object(self, **kwargs: object) -> HeadObjectOutput: ...

    def put_object(self, **kwargs: object) -> object: ...


def create_s3_client(*, region_name: str, endpoint_url: str | None = None) -> S3Client:
    """Build the boto3 S3 client with SDK retrying switched off.

    boto3's generated clients carry no type information, so this function is the one place a
    cast is required; everything above it is statically typed against ``S3Client``. The retry
    policy is passed explicitly so it wins over ``AWS_MAX_ATTEMPTS``, ``AWS_RETRY_MODE``, and
    the shared config file -- an ambient environment variable must not be able to reintroduce a
    hidden attempt at a write whose outcome this system resolves for itself.
    """

    import boto3

    client: Any = boto3.client(
        "s3",
        region_name=region_name,
        endpoint_url=endpoint_url,
        config=Config(retries=dict(SINGLE_ATTEMPT_RETRIES)),
    )
    return cast(S3Client, client)
