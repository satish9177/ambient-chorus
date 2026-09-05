"""The frozen ADR-018 sanitizer profile, asserted as numbers rather than as intentions.

Every row of the frozen table gets a test, and every refusal is checked for its exact fixed
code -- because "it raised something" is satisfied by a bug too. The metadata inputs are all
built here: the committed elevator photo is a 1x1 placeholder with only a JFIF marker, so it
cannot demonstrate stripping, and no fixture in this repository gains real location data.
"""

from __future__ import annotations

import subprocess
import sys
from io import BytesIO

import pytest
from PIL import Image

from chorus.infrastructure.imaging import sanitizer as module
from chorus.infrastructure.imaging.sanitizer import (
    ACCEPTED_SOURCE_MEDIA_TYPES,
    DERIVATIVE_MEDIA_TYPE,
    MAX_DECODED_PIXELS,
    MAX_INPUT_DIMENSION,
    MAX_OUTPUT_EDGE,
    MAX_SOURCE_BYTES,
    PNG_COMPRESS_LEVEL,
    PNG_OPTIMIZE,
    UnsafeImageError,
    sanitize_image,
)
from chorus.privacy.transformations import SAFE_EVIDENCE_MEDIA_TYPE

METADATA_CHUNKS = (b"tEXt", b"iTXt", b"zTXt", b"iCCP", b"eXIf", b"Exif")


def jpeg(size: tuple[int, int] = (24, 18), **save: object) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, (32, 64, 96)).save(buffer, format="JPEG", **save)
    return buffer.getvalue()


def png(size: tuple[int, int] = (24, 18), mode: str = "RGB", **save: object) -> bytes:
    buffer = BytesIO()
    colour = (32, 64, 96, 0) if mode == "RGBA" else (32, 64, 96)
    Image.new(mode, size, colour).save(buffer, format="PNG", **save)
    return buffer.getvalue()


def loaded(content: bytes) -> Image.Image:
    return Image.open(BytesIO(content))


# -- the frozen profile ----------------------------------------------------------------


def test_the_frozen_profile_numbers_are_exactly_the_adr_values() -> None:
    """These are the ADR-018 table. A change here is a change to an accepted decision."""

    assert frozenset({"image/jpeg", "image/png"}) == ACCEPTED_SOURCE_MEDIA_TYPES
    assert MAX_SOURCE_BYTES == 10_000_000
    assert MAX_DECODED_PIXELS == 16_000_000
    assert MAX_INPUT_DIMENSION == 8192
    assert MAX_OUTPUT_EDGE == 2048
    assert DERIVATIVE_MEDIA_TYPE == "image/png"
    assert PNG_OPTIMIZE is False
    assert PNG_COMPRESS_LEVEL == 9


def test_the_derivative_media_type_agrees_with_the_privacy_constant() -> None:
    """Two constants, because infrastructure may not import privacy. One value."""

    assert DERIVATIVE_MEDIA_TYPE == SAFE_EVIDENCE_MEDIA_TYPE


# -- acceptance ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "declared"),
    [(jpeg(), "image/jpeg"), (png(), "image/png")],
    ids=["jpeg", "png"],
)
def test_an_accepted_source_becomes_a_png_derivative(content: bytes, declared: str) -> None:
    result = sanitize_image(content, declared_media_type=declared)

    assert result.media_type == "image/png"
    assert result.byte_length == len(result.content)
    assert result.sha256.value.startswith("sha256:")
    assert loaded(result.content).format == "PNG"


# -- refusals --------------------------------------------------------------------------


def test_an_unaccepted_media_type_is_refused_before_the_decoder_sees_it() -> None:
    """Matrix P. The check is first, so an unsupported type never reaches the parser."""

    with pytest.raises(UnsafeImageError, match="media_type"):
        sanitize_image(b"GIF89a", declared_media_type="image/gif")


def test_a_declared_type_that_disagrees_with_the_decoded_format_is_refused() -> None:
    """A declaration is a claim. An allowlist that trusted it would be waving through a lie."""

    with pytest.raises(UnsafeImageError, match="format_mismatch"):
        sanitize_image(jpeg(), declared_media_type="image/png")


@pytest.mark.parametrize(
    "content",
    [b"", b"\xff\xd8\xff\xe0notanimage", jpeg()[: len(jpeg()) // 2]],
    ids=["empty", "garbage", "truncated"],
)
def test_a_malformed_or_truncated_source_is_refused(content: bytes) -> None:
    """Matrix Q. Truncated loading stays disabled, so a half file is not half an image."""

    with pytest.raises(UnsafeImageError):
        sanitize_image(content, declared_media_type="image/jpeg")


def test_an_oversized_source_is_refused_by_byte_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matrix R, the byte half. The bound is exact, not a rounded mebibyte."""

    monkeypatch.setattr(module, "MAX_SOURCE_BYTES", 32)

    with pytest.raises(UnsafeImageError, match="source_bytes"):
        sanitize_image(jpeg(), declared_media_type="image/jpeg")


def test_an_oversized_dimension_is_refused() -> None:
    """Matrix R, the dimension half. 8300 pixels on one axis, well inside the pixel cap."""

    with pytest.raises(UnsafeImageError, match="dimensions"):
        sanitize_image(png((8300, 10), compress_level=1), declared_media_type="image/png")


def test_a_decompression_bomb_is_refused() -> None:
    """Matrix R. Pillow's warning is promoted to an error; a warning that prints guards nothing."""

    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        bomb = png((5000, 4000), compress_level=1)
    finally:
        Image.MAX_IMAGE_PIXELS = previous

    with pytest.raises(UnsafeImageError, match="decompression_bomb"):
        sanitize_image(bomb, declared_media_type="image/png")


def test_the_sanitizer_restores_the_process_wide_pillow_limits() -> None:
    """It tightens two globals. Leaving them tightened would change unrelated code."""

    before_pixels = Image.MAX_IMAGE_PIXELS
    from PIL import ImageFile

    before_truncated = ImageFile.LOAD_TRUNCATED_IMAGES

    sanitize_image(jpeg(), declared_media_type="image/jpeg")

    assert before_pixels == Image.MAX_IMAGE_PIXELS
    assert before_truncated == ImageFile.LOAD_TRUNCATED_IMAGES


def test_an_animated_source_is_refused() -> None:
    """One file carrying many pictures is not something a safe reference can describe."""

    buffer = BytesIO()
    frames = [Image.new("RGB", (8, 8), (i * 20, 0, 0)) for i in range(3)]
    frames[0].save(buffer, format="PNG", save_all=True, append_images=frames[1:], duration=10)
    content = buffer.getvalue()
    if getattr(Image.open(BytesIO(content)), "n_frames", 1) == 1:  # pragma: no cover
        pytest.skip("this Pillow build did not produce a multi-frame PNG")

    with pytest.raises(UnsafeImageError, match="multiple_frames"):
        sanitize_image(content, declared_media_type="image/png")


# -- normalization ---------------------------------------------------------------------


def test_exif_orientation_is_applied_before_metadata_is_discarded() -> None:
    """Matrix S. The operations do not commute, and this is the proof.

    A 40x30 source tagged orientation 6 must emit 30x40. If the metadata were dropped first the
    output would still be 40x30 -- correctly sanitized, and a picture of the wrong thing.
    """

    buffer = BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (40, 30), (10, 20, 30)).save(buffer, format="JPEG", exif=exif)

    result = sanitize_image(buffer.getvalue(), declared_media_type="image/jpeg")

    assert (result.width, result.height) == (30, 40)


def test_every_metadata_carrier_is_absent_from_the_derivative() -> None:
    """Matrix S. GPS, maker note, comment, and every PNG metadata chunk."""

    buffer = BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F] = "SECRET_SENTINEL_MAKE"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (51.0, 30.0, 0.0)
    gps[3] = "W"
    gps[4] = (0.0, 7.0, 0.0)
    Image.new("RGB", (40, 30), (10, 20, 30)).save(
        buffer, format="JPEG", exif=exif, comment=b"SECRET_SENTINEL_COMMENT"
    )
    source = buffer.getvalue()
    assert b"SECRET_SENTINEL" in source

    result = sanitize_image(source, declared_media_type="image/jpeg")

    assert b"SECRET_SENTINEL" not in result.content
    for chunk in METADATA_CHUNKS:
        assert chunk not in result.content
    assert loaded(result.content).info.get("exif") is None


def test_a_png_text_chunk_does_not_survive() -> None:
    """A PNG source can carry its own metadata, and a fresh canvas carries none of it."""

    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text("Comment", "SECRET_SENTINEL_TEXT")
    buffer = BytesIO()
    Image.new("RGB", (12, 12), (1, 2, 3)).save(buffer, format="PNG", pnginfo=info)
    source = buffer.getvalue()
    assert b"SECRET_SENTINEL_TEXT" in source

    result = sanitize_image(source, declared_media_type="image/png")

    assert b"SECRET_SENTINEL_TEXT" not in result.content


def test_alpha_is_composited_onto_opaque_white() -> None:
    """Transparency resolved by the compiler, not by whatever renders the message."""

    result = sanitize_image(png(mode="RGBA"), declared_media_type="image/png")
    image = loaded(result.content)

    assert image.mode == "RGB"
    assert image.getpixel((0, 0)) == (255, 255, 255)


def test_a_large_image_is_scaled_with_its_aspect_ratio_preserved() -> None:
    result = sanitize_image(png((5000, 1000), compress_level=1), declared_media_type="image/png")

    assert max(result.width, result.height) == MAX_OUTPUT_EDGE
    assert (result.width, result.height) == (2048, 410)


def test_an_image_inside_the_output_cap_is_not_resampled() -> None:
    """Resizing what already fits would be a lossy step nobody asked for."""

    result = sanitize_image(jpeg((24, 18)), declared_media_type="image/jpeg")

    assert (result.width, result.height) == (24, 18)


# -- determinism -----------------------------------------------------------------------


def test_sanitizing_the_same_source_twice_produces_identical_bytes() -> None:
    """Matrix T, in one process."""

    source = jpeg()

    first = sanitize_image(source, declared_media_type="image/jpeg")
    second = sanitize_image(source, declared_media_type="image/jpeg")

    assert first.content == second.content
    assert first.sha256 == second.sha256


def test_sanitizing_the_same_source_in_a_separate_process_produces_identical_bytes(
    tmp_path: object,
) -> None:
    """Matrix AX. One process proves the function is pure; two prove the *build* is.

    A Lambda and a developer's laptop are two processes, and the golden view hash depends on the
    derivative digest being the same in both. That is a claim about the pinned Pillow and its
    bundled zlib, so it is checked by actually starting another interpreter.
    """

    script = (
        "from io import BytesIO\n"
        "from PIL import Image\n"
        "from chorus.infrastructure.imaging.sanitizer import sanitize_image\n"
        "buffer = BytesIO()\n"
        "Image.new('RGB', (24, 18), (32, 64, 96)).save(buffer, format='JPEG', quality=90)\n"
        "print(sanitize_image(buffer.getvalue(), declared_media_type='image/jpeg').sha256.value)\n"
    )
    # The argv is this module's own literal plus ``sys.executable``: no shell, no user
    # input, and no path from a fixture into the command line.
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )

    local = sanitize_image(jpeg(), declared_media_type="image/jpeg")
    assert completed.stdout.strip() == local.sha256.value


def test_the_digest_is_over_exactly_the_emitted_bytes() -> None:
    """The digest is simultaneously the integrity check and the export object's address."""

    from hashlib import sha256

    result = sanitize_image(jpeg(), declared_media_type="image/jpeg")

    assert result.sha256.value == f"sha256:{sha256(result.content).hexdigest()}"


def test_a_refusal_never_quotes_the_input() -> None:
    """An image parser's own error text can contain file content. This one cannot."""

    poisoned = b"\xff\xd8\xff\xe0" + b"SECRET_SENTINEL_IN_BYTES" * 4

    with pytest.raises(UnsafeImageError) as error:
        sanitize_image(poisoned, declared_media_type="image/jpeg")

    assert "SECRET_SENTINEL" not in str(error.value)
