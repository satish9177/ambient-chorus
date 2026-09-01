"""Signed, namespace-bound pagination cursors.

A cursor is an opaque base64url payload plus an HMAC tag. The payload carries the schema
version, the namespace, the access pattern it was issued for, the partition it continues, and
the last evaluated sort key. It carries no message text, no summary, no contact value, and no
private identifier beyond the IDs already embedded in the partition key.

Verification is fail-closed: a malformed, truncated, re-encoded, tampered, or foreign cursor
is rejected, and the signature comparison is constant time. The frozen contract defines no
cursor expiry, so none is implemented here.
"""

from __future__ import annotations

import base64
import hmac
import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Final

import rfc8785

from chorus.domain.ids import Namespace
from chorus.ports.errors import InvalidCursorError
from chorus.ports.pagination import PageCursor, QueryBinding

CURSOR_SCHEMA_VERSION = "cursor/v1"
_MIN_SECRET_BYTES = 32
_BASE64URL: Final = re.compile(r"[A-Za-z0-9_-]+")
"""The only alphabet a CHORUS cursor may use: unpadded base64url."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    """Decode one canonically encoded, unpadded base64url segment.

    Python's decoder is permissive: it silently discards characters outside the alphabet and
    accepts trailing bits that no encoder would ever emit, so two different strings can decode
    to the same bytes. A cursor is an opaque token the caller returns verbatim, so every such
    alternate spelling is rejected rather than normalised -- first by matching the alphabet
    and length, then by requiring that re-encoding reproduces the input exactly.
    """

    if _BASE64URL.fullmatch(value) is None or len(value) % 4 == 1:
        raise InvalidCursorError("ENCODING")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise InvalidCursorError("ENCODING") from error
    if _b64encode(raw) != value:
        raise InvalidCursorError("ENCODING")
    return raw


@dataclass(frozen=True, slots=True)
class SignedCursorCodec:
    """Issues and verifies cursors bound to one namespace and one access pattern."""

    secret: bytes

    def __post_init__(self) -> None:
        if len(self.secret) < _MIN_SECRET_BYTES:
            raise ValueError("pagination secret must be at least 32 bytes")

    def _payload(
        self,
        *,
        namespace: Namespace,
        binding: QueryBinding,
        partition_key: str,
        sort_key: str,
    ) -> bytes:
        return rfc8785.dumps(
            {
                "v": CURSOR_SCHEMA_VERSION,
                "ns": namespace.value,
                "q": binding.value,
                "p": partition_key,
                "k": sort_key,
            }
        )

    def _tag(self, payload: bytes) -> bytes:
        return hmac.new(self.secret, payload, sha256).digest()

    def issue(
        self,
        *,
        namespace: Namespace,
        binding: QueryBinding,
        partition_key: str,
        sort_key: str,
    ) -> PageCursor:
        """Create a cursor bound to exactly this namespace, query, and partition."""

        payload = self._payload(
            namespace=namespace,
            binding=binding,
            partition_key=partition_key,
            sort_key=sort_key,
        )
        return PageCursor(f"{_b64encode(payload)}.{_b64encode(self._tag(payload))}")

    def verify(
        self,
        cursor: PageCursor,
        *,
        namespace: Namespace,
        binding: QueryBinding,
        partition_key: str,
    ) -> str:
        """Return the last evaluated sort key, or fail closed."""

        parts = cursor.value.split(".")
        if len(parts) != 2:
            raise InvalidCursorError("FORMAT")
        payload = _b64decode(parts[0])
        provided_tag = _b64decode(parts[1])
        if not hmac.compare_digest(self._tag(payload), provided_tag):
            raise InvalidCursorError("SIGNATURE")
        decoded = _decode_payload(payload)
        if decoded.schema_version != CURSOR_SCHEMA_VERSION:
            raise InvalidCursorError("SCHEMA")
        if (
            decoded.namespace != namespace.value
            or decoded.binding != binding.value
            or decoded.partition_key != partition_key
        ):
            raise InvalidCursorError("BINDING")
        # Re-serialising the verified fields must reproduce the signed bytes exactly, which
        # rejects any payload that carries extra or reordered attributes.
        reissued = self._payload(
            namespace=namespace,
            binding=binding,
            partition_key=partition_key,
            sort_key=decoded.sort_key,
        )
        if reissued != payload:
            raise InvalidCursorError("CANONICAL")
        return decoded.sort_key


@dataclass(frozen=True, slots=True, kw_only=True)
class _CursorPayload:
    schema_version: str
    namespace: str
    binding: str
    partition_key: str
    sort_key: str


def _decode_payload(payload: bytes) -> _CursorPayload:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise InvalidCursorError("PAYLOAD") from error
    if not isinstance(parsed, dict) or set(parsed) != {"v", "ns", "q", "p", "k"}:
        raise InvalidCursorError("PAYLOAD")
    values = [parsed["v"], parsed["ns"], parsed["q"], parsed["p"], parsed["k"]]
    if not all(isinstance(value, str) for value in values):
        raise InvalidCursorError("PAYLOAD")
    return _CursorPayload(
        schema_version=str(parsed["v"]),
        namespace=str(parsed["ns"]),
        binding=str(parsed["q"]),
        partition_key=str(parsed["p"]),
        sort_key=str(parsed["k"]),
    )
