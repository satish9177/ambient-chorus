"""The frozen ADR-018 image sanitizer: one decoder, one encoder, one output shape.

This is the only place in the system that points a parser at bytes it did not produce, so
every bound is a number rather than a judgement and every failure is a refusal rather than a
best effort. The profile is frozen in
[ADR-018](../../../../docs/adr/ADR-018-safe-evidence-and-compile-commit.md) and repeated in
[06-persistence-and-evidence.md](../../../../docs/architecture/06-persistence-and-evidence.md);
the constants below are that table, and changing one is an ADR.

Nothing here decides whether an image *may* be exported. That is the compiler's evidence-safety
gate. This module answers a narrower question -- what would a safe derivative of these bytes
be -- and answers it identically every time or not at all.

**Order matters twice.**

EXIF orientation is applied *before* metadata is discarded, because the two do not commute. A
photograph taken sideways carries its rotation in EXIF; dropping the metadata first would
produce a correctly sanitized picture of the wrong thing.

The output image is built by pasting onto a fresh canvas rather than by converting in place.
A converted image carries ``info`` forward, and ``info`` is where EXIF, ICC profiles, XMP
packets, and PNG text chunks live. Starting from a new image means the metadata is not stripped
so much as never present -- there is no dictionary to forget to clear.

**Determinism is claimed exactly.** For the same source bytes, output is byte-identical under
the pinned Pillow and its bundled zlib. Bit identity across arbitrary future encoder versions is
not claimed, because PNG filter selection and deflate output belong to the encoder build. The
golden tests pin the runtime, and a dependency bump that moves a hash is a reviewed change.
"""

from __future__ import annotations

import warnings
from hashlib import sha256
from io import BytesIO
from typing import Final

from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

from chorus.domain.errors import ValidationError
from chorus.domain.ids import Sha256Digest
from chorus.ports.imaging import (
    ACCEPTED_SOURCE_MEDIA_TYPES as _PORT_ACCEPTED,
)
from chorus.ports.imaging import SAFE_IMAGE_MEDIA_TYPE, SafeImage

ACCEPTED_SOURCE_MEDIA_TYPES: Final[frozenset[str]] = _PORT_ACCEPTED
"""The complete accepted source set. A document, an email, and an SVG have no rule at all."""

DERIVATIVE_MEDIA_TYPE: Final = SAFE_IMAGE_MEDIA_TYPE
"""Every accepted image leaves as PNG, so the safe reference's media type is a constant."""

MAX_SOURCE_BYTES: Final = 10_000_000
MAX_DECODED_PIXELS: Final = 16_000_000
MAX_INPUT_DIMENSION: Final = 8192
MAX_OUTPUT_EDGE: Final = 2048
PNG_COMPRESS_LEVEL: Final = 9
PNG_OPTIMIZE: Final = False
WORKING_MODE: Final = "RGB"
ALPHA_BACKGROUND: Final = (255, 255, 255)

_ACCEPTED_PIL_FORMATS: Final[frozenset[str]] = frozenset({"JPEG", "PNG"})
_FORMAT_MEDIA_TYPES: Final[dict[str, str]] = {"JPEG": "image/jpeg", "PNG": "image/png"}


class UnsafeImageError(ValidationError):
    """The source cannot be sanitized under the frozen profile.

    A ``ValidationError`` because it is a statement about the input, and the message is a fixed
    code: an image parser's own error text can quote file content, and this one never does.
    """

    __slots__ = ()


def _refuse(code: str) -> UnsafeImageError:
    return UnsafeImageError(f"UNSAFE_IMAGE:{code}")


def _open(source: bytes) -> Image.Image:
    """Decode under every frozen guard, converting each library failure into a fixed code."""

    # The bomb guard is Pillow's own, tightened to the frozen cap. Pillow warns above the cap
    # and raises above twice it, so the warning is promoted: a warning that only prints is a
    # guard that does nothing in a Lambda.
    previous_limit = Image.MAX_IMAGE_PIXELS
    previous_truncated = ImageFile.LOAD_TRUNCATED_IMAGES
    Image.MAX_IMAGE_PIXELS = MAX_DECODED_PIXELS
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                image = Image.open(BytesIO(source))
                image.load()
            except Image.DecompressionBombWarning as error:
                raise _refuse("decompression_bomb") from error
            except Image.DecompressionBombError as error:
                raise _refuse("decompression_bomb") from error
            except UnidentifiedImageError as error:
                raise _refuse("undecodable") from error
            except (OSError, ValueError, SyntaxError) as error:
                # A truncated JPEG, a corrupt PNG chunk, and a malformed header all arrive
                # here. They are one answer, because telling them apart tells a caller what
                # the parser thought of bytes it should never have been handed twice.
                raise _refuse("malformed") from error
        return image
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
        ImageFile.LOAD_TRUNCATED_IMAGES = previous_truncated


def _target_size(width: int, height: int) -> tuple[int, int]:
    """Scale the longest edge down to the cap, preserving aspect ratio deterministically."""

    longest = max(width, height)
    if longest <= MAX_OUTPUT_EDGE:
        return width, height
    scale = MAX_OUTPUT_EDGE / longest
    return max(1, round(width * scale)), max(1, round(height * scale))


def sanitize_image(source: bytes, *, declared_media_type: str) -> SafeImage:
    """Produce the one safe derivative of these bytes, or refuse them.

    ``declared_media_type`` is what the stored ``EvidenceItem`` claims. It is checked against
    the accepted set *and* against what the decoder actually found, because a declaration is a
    claim: a file named as a PNG that decodes as something else is exactly the input a
    media-type allowlist alone would wave through.
    """

    if declared_media_type not in ACCEPTED_SOURCE_MEDIA_TYPES:
        raise _refuse("media_type")
    if not source:
        raise _refuse("empty")
    if len(source) > MAX_SOURCE_BYTES:
        raise _refuse("source_bytes")

    image = _open(source)
    try:
        if image.format is None or image.format not in _ACCEPTED_PIL_FORMATS:
            raise _refuse("format")
        if _FORMAT_MEDIA_TYPES[image.format] != declared_media_type:
            raise _refuse("format_mismatch")
        if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
            # An animated PNG is one file carrying many pictures. Exporting the first frame
            # would silently discard the rest, and exporting all of them is not a thing the
            # safe reference can describe.
            raise _refuse("multiple_frames")
        width, height = image.size
        if width < 1 or height < 1:
            raise _refuse("dimensions")
        if width > MAX_INPUT_DIMENSION or height > MAX_INPUT_DIMENSION:
            raise _refuse("dimensions")
        if width * height > MAX_DECODED_PIXELS:
            raise _refuse("decompression_bomb")

        oriented = ImageOps.exif_transpose(image)
        if oriented is None:  # pragma: no cover - defensive; Pillow returns a copy or the image
            oriented = image
        try:
            flattened = _flatten(oriented)
            target = _target_size(*flattened.size)
            if target != flattened.size:
                flattened = flattened.resize(target, resample=Image.Resampling.LANCZOS)
            content = _encode(flattened)
            emitted_width, emitted_height = flattened.size
        finally:
            if oriented is not image:
                oriented.close()
    finally:
        image.close()

    return SafeImage(
        content=content,
        media_type=DERIVATIVE_MEDIA_TYPE,
        byte_length=len(content),
        sha256=Sha256Digest(f"sha256:{sha256(content).hexdigest()}"),
        width=emitted_width,
        height=emitted_height,
    )


def _flatten(image: Image.Image) -> Image.Image:
    """Composite onto opaque white on a fresh canvas that carries no source metadata.

    ``Image.new`` starts with an empty ``info``, so EXIF, ICC, XMP, and PNG text chunks are
    absent rather than removed. Transparency is resolved against a fixed background instead of
    being carried into the output, because an alpha channel a mail client renders against its
    own background is a picture whose appearance the compiler did not decide.
    """

    canvas = Image.new(WORKING_MODE, image.size, ALPHA_BACKGROUND)
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        source = image.convert("RGBA")
        try:
            canvas.paste(source, mask=source.split()[-1])
        finally:
            source.close()
        return canvas
    source = image.convert(WORKING_MODE)
    try:
        canvas.paste(source)
    finally:
        if source is not image:
            source.close()
    return canvas


def _encode(image: Image.Image) -> bytes:
    """Emit PNG under the frozen writer settings and nothing else.

    No ``pnginfo``, no ``exif``, and no ``icc_profile`` argument is passed, and the image was
    built on a fresh canvas, so there is nothing for the writer to carry through.
    """

    buffer = BytesIO()
    image.save(
        buffer,
        format="PNG",
        optimize=PNG_OPTIMIZE,
        compress_level=PNG_COMPRESS_LEVEL,
    )
    return buffer.getvalue()
