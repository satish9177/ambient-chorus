"""Unit tests for S3InboundRawMessageReader (ADR-030 § 3 pinned bounded read)."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from chorus.infrastructure.s3.inbound_raw import S3InboundRawMessageReader
from chorus.ports.errors import NotFoundError
from chorus.ports.objects import MAX_INBOUND_REPLY_BYTES

EXPECTED_BUCKET = "chorus-private-evidence-demo"
VALID_KEY = "ns/DEMO/inbound/message-001"


class FakeStreamingBody:
    def __init__(self, data: bytes) -> None:
        self._stream = io.BytesIO(data)

    def read(self, amt: int | None = None) -> bytes:
        return self._stream.read(amt)


def _mock_s3_client(
    data: bytes = b"test message body",
    *,
    version_id: str = "v1-pinned",
    etag: str = '"etag-12345"',
) -> MagicMock:
    client = MagicMock()
    client.get_object.return_value = {
        "Body": FakeStreamingBody(data),
        "VersionId": version_id,
        "ETag": etag,
    }
    return client


@pytest.mark.anyio
async def test_s3_inbound_raw_reader_success() -> None:
    expected_data = b"synthetic raw mime content"
    client = _mock_s3_client(expected_data, version_id="ver-42", etag='"etag-abc"')

    reader = S3InboundRawMessageReader(client=client, expected_bucket=EXPECTED_BUCKET)

    result = await reader.read(bucket=EXPECTED_BUCKET, key=VALID_KEY)

    assert result == expected_data
    assert reader.read_count == 1
    assert reader.last_version_id == "ver-42"
    assert reader.last_etag == '"etag-abc"'

    client.get_object.assert_called_once_with(
        Bucket=EXPECTED_BUCKET,
        Key=VALID_KEY,
        Range=f"bytes=0-{MAX_INBOUND_REPLY_BYTES}",
    )


@pytest.mark.anyio
async def test_s3_inbound_raw_reader_rejects_wrong_bucket() -> None:
    client = _mock_s3_client()
    reader = S3InboundRawMessageReader(client=client, expected_bucket=EXPECTED_BUCKET)

    with pytest.raises(NotFoundError, match="s3://wrong-bucket"):
        await reader.read(bucket="wrong-bucket", key=VALID_KEY)

    assert client.get_object.call_count == 0


@pytest.mark.anyio
async def test_s3_inbound_raw_reader_rejects_wrong_prefix() -> None:
    client = _mock_s3_client()
    reader = S3InboundRawMessageReader(client=client, expected_bucket=EXPECTED_BUCKET)

    with pytest.raises(NotFoundError, match="s3://"):
        await reader.read(bucket=EXPECTED_BUCKET, key="ns/DEMO/cases/case-123/evidence/abc")

    assert client.get_object.call_count == 0


@pytest.mark.anyio
async def test_s3_inbound_raw_reader_prohibits_multiple_reads() -> None:
    client = _mock_s3_client(b"first read")
    reader = S3InboundRawMessageReader(client=client, expected_bucket=EXPECTED_BUCKET)

    first = await reader.read(bucket=EXPECTED_BUCKET, key=VALID_KEY)
    assert first == b"first read"

    with pytest.raises(RuntimeError, match="multiple reads prohibited"):
        await reader.read(bucket=EXPECTED_BUCKET, key=VALID_KEY)


@pytest.mark.anyio
async def test_s3_inbound_raw_reader_handles_not_found_client_error() -> None:
    client = MagicMock()
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}}, "GetObject"
    )
    reader = S3InboundRawMessageReader(client=client, expected_bucket=EXPECTED_BUCKET)

    with pytest.raises(NotFoundError):
        await reader.read(bucket=EXPECTED_BUCKET, key=VALID_KEY)
