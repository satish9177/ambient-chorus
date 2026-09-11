"""S3 reader for inbound raw MIME messages (ADR-030 § 3).

Enforces:
1. Locator validation: bucket must equal configured private evidence bucket,
   key must start with the ingress prefix (default 'ns/DEMO/inbound/').
2. Exactly one bounded get_object call reading at most MAX_INBOUND_REPLY_BYTES + 1 bytes.
3. Pins object identity (VersionId and ETag captured from that same read).
4. Asserts single read -- no re-reading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from botocore.exceptions import BotoCoreError, ClientError

from chorus.infrastructure.s3.client import S3Client
from chorus.ports.errors import NotFoundError
from chorus.ports.inbound_mail import InboundRawMessageReader
from chorus.ports.objects import MAX_INBOUND_REPLY_BYTES

DEFAULT_INGRESS_PREFIX: Final = "ns/DEMO/inbound/"


@dataclass(slots=True)
class S3InboundRawMessageReader(InboundRawMessageReader):
    """Fetches raw MIME from S3 with single-read pinning and bounds checking."""

    client: S3Client
    expected_bucket: str
    ingress_prefix: str = DEFAULT_INGRESS_PREFIX
    _read_count: int = field(default=0, init=False)
    _last_version_id: str | None = field(default=None, init=False)
    _last_etag: str | None = field(default=None, init=False)

    @property
    def last_version_id(self) -> str | None:
        return self._last_version_id

    @property
    def last_etag(self) -> str | None:
        return self._last_etag

    @property
    def read_count(self) -> int:
        return self._read_count

    async def read(self, *, bucket: str, key: str) -> bytes:
        """Fetch raw MIME bytes. Enforces locator check, bounded read, and single-read pinning."""
        if bucket != self.expected_bucket:
            raise NotFoundError(f"s3://{bucket}/{key}")
        if not key.startswith(self.ingress_prefix):
            raise NotFoundError(f"s3://{bucket}/{key}")

        if self._read_count > 0:
            raise RuntimeError(
                "S3InboundRawMessageReader: multiple reads prohibited; exactly one read allowed"
            )
        self._read_count += 1

        range_header = f"bytes=0-{MAX_INBOUND_REPLY_BYTES}"
        try:
            response = self.client.get_object(
                Bucket=bucket,
                Key=key,
                Range=range_header,
            )
        except ClientError as error:
            error_code = str(error.response.get("Error", {}).get("Code", ""))
            if error_code in {"NoSuchKey", "404", "NotFound", "AccessDenied"}:
                raise NotFoundError(f"s3://{bucket}/{key}") from error
            raise
        except BotoCoreError as error:
            raise NotFoundError(f"s3://{bucket}/{key}") from error

        version_id = response.get("VersionId")
        etag = response.get("ETag")
        self._last_version_id = str(version_id) if version_id is not None else None
        self._last_etag = str(etag) if etag is not None else None

        body = response.get("Body")
        if body is None:
            raise NotFoundError(f"s3://{bucket}/{key}")

        content: bytes = body.read(MAX_INBOUND_REPLY_BYTES + 1)
        return content


__all__ = ["DEFAULT_INGRESS_PREFIX", "S3InboundRawMessageReader"]
