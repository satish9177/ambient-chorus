"""The image-sanitizer port: one operation, one output shape, no policy.

The sanitizer lives in infrastructure because it owns a third-party decoder, and infrastructure
may not import ``chorus.privacy``. The application is the layer that can see both, so it takes
a sanitizer through this port and assembles the compiler's ``SafeEvidenceCandidate`` itself.
That is why the port speaks in plain domain values and knows nothing about mandates, scopes,
captions, or reviews.

The frozen profile the implementation must satisfy -- accepted media types, byte, pixel and
dimension caps, single-frame rule, orientation ordering, alpha handling, PNG writer settings,
and total metadata discard -- is
[ADR-018](../../../docs/adr/ADR-018-safe-evidence-and-compile-commit.md). The port cannot
enforce it; the adapter's own tests do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from chorus.domain.ids import Sha256Digest

SAFE_IMAGE_MEDIA_TYPE = "image/png"
"""Every accepted image leaves as PNG, so the derivative's media type is a constant."""

ACCEPTED_SOURCE_MEDIA_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png"})
"""The complete accepted source set.

Stated on the port because the application needs it to decide which requested evidence is
even a candidate for sanitization -- an email or a text document is not refused by the
sanitizer so much as never handed to it, and the compiler's evidence-safety gate is what
turns "no derivative" into a disclosure outcome.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class SafeImage:
    """The emitted derivative: its bytes, its content address, and its shape.

    ``sha256`` is over exactly ``content``, and it is what the export object is keyed on, so
    this value is simultaneously the integrity check and the address. Nothing here describes
    the source: a derivative that remembered where it came from would be a lineage field on an
    object the external side can fetch.
    """

    content: bytes
    media_type: str
    byte_length: int
    sha256: Sha256Digest
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.media_type != SAFE_IMAGE_MEDIA_TYPE:
            raise ValueError("a safe derivative is always PNG")
        if self.byte_length != len(self.content):
            raise ValueError("derivative byte length disagrees with its content")
        if self.width < 1 or self.height < 1:
            raise ValueError("derivative dimensions must be positive")


class ImageSanitizerPort(Protocol):
    """Produce the one safe derivative of these bytes, or refuse them."""

    def sanitize(self, source: bytes, *, declared_media_type: str) -> SafeImage:
        """Decode, normalize, strip, re-encode, and hash under the frozen profile.

        Raises a closed validation error carrying a fixed code for every refusal. It never
        quotes the input: an image parser's own error text can contain file content.
        """
