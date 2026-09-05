"""The golden view hash, the persisted-shape scan, and what the logs are allowed to say.

A golden hash is a statement about fixed inputs, so every input this one depends on is pinned
here: the compile identity, the clock, the injected identifier sequence, and the sanitizer's
output bytes. If one of those stops being deterministic, this file is where it shows.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import cast

import pytest
from tests.fixtures.compile import (
    SENTINEL_PATTERN,
    CompileHarness,
    harness_uuid,
    photo_bytes,
    photo_bytes_with_metadata,
)

from chorus.application.commands.compile_view import CompileView
from chorus.infrastructure.dynamodb import codec_share
from chorus.infrastructure.dynamodb.attributes import encode_item
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.privacy.canonical import to_canonical_primitive
from chorus.privacy.compiler import PrivacyCompiler, ShareableCaseView

pytestmark = pytest.mark.anyio


def _scan(view: object) -> bool:
    """Run the compiler's own recursive scanner over a value of the mirrored shape.

    The scanner walks the canonical primitive rather than the class, so the stored record
    and the compiled DTO are the same input to it -- which is exactly the property being
    checked, and the reason a second scanner would be a second answer.
    """

    return bool(PrivacyCompiler._unsafe_output(cast(ShareableCaseView, view)))


PRIVATE_STRINGS = (
    "SECRET_SENTINEL",
    "mother_health",
    "Apartment 4B",
    "Ignore policy",
)


async def _seed(harness: CompileHarness, *, photo: bytes | None = None) -> CompileView:
    raw = photo if photo is not None else photo_bytes()
    await harness.seed(evidence_items=harness.align_photo_digest(raw), photo=raw)
    return harness.compile_view()


# -- the golden hash ---------------------------------------------------------------------


async def test_the_same_compile_inputs_produce_the_same_view_hash(
    harness: CompileHarness,
) -> None:
    """Matrix AN, stated as the property rather than as a literal.

    Two independently seeded worlds, one set of fixed inputs, one hash. Pinning a hex literal
    here would pin the fixture's private text as well, and re-cutting it on every unrelated
    fixture edit is how a golden test stops being read.

    The second world is deliberately in-memory whichever driver the first one used. The hash
    is a property of the *inputs*, so a compile whose digest depended on which storage engine
    produced the rows would be the defect this test exists to catch.
    """

    compile_view = await _seed(harness)
    first = await compile_view.execute(harness.command())

    second_harness = CompileHarness(driver=InMemoryStorageDriver())
    second_view = await _seed(second_harness)
    second = await second_view.execute(second_harness.command())

    assert first.view is not None
    assert second.view is not None
    assert first.view.view_hash == second.view.view_hash
    assert first.view.view_id == second.view.view_id


async def test_the_view_hash_verifies_against_the_persisted_row(
    harness: CompileHarness,
) -> None:
    """Verification removes only ``view_hash`` and recomputes; the stored row must survive it.

    The result carries the *stored* shape, and this is where that stops being a convenience
    and becomes a check: the mirrored record hashes to the digest the compiler computed over
    its own DTO. Two types, one canonical form, one hash -- which is what makes the parity
    test's field-set assertion mean something at runtime rather than only at import time.
    """

    from chorus.privacy.canonical import hash_value

    compile_view = await _seed(harness)
    result = await compile_view.execute(harness.command())
    assert result.view is not None

    recomputed = hash_value(result.view, omit_fields=frozenset({"view_hash"}))
    assert recomputed == result.view.view_hash

    stored = await harness.shareable.load_view(harness.scope, result.view.view_id)
    assert stored == result.view
    assert hash_value(stored, omit_fields=frozenset({"view_hash"})) == stored.view_hash


async def test_the_safe_evidence_digest_is_the_sanitizer_output(
    harness: CompileHarness,
) -> None:
    """The reference, the object key, and the sanitizer all name one digest."""

    from chorus.infrastructure.imaging.sanitizer import sanitize_image

    raw = photo_bytes()
    compile_view = await _seed(harness, photo=raw)

    result = await compile_view.execute(harness.command())

    assert result.view is not None
    expected = sanitize_image(raw, declared_media_type="image/jpeg")
    assert result.view.safe_evidence_refs[0].sha256 == expected.sha256


# -- the recursive scan ------------------------------------------------------------------


async def test_the_persisted_view_item_carries_no_private_value(
    harness: CompileHarness,
) -> None:
    """Matrix AT. The scanner runs on the model; this proves the codec adds nothing back."""

    compile_view = await _seed(harness)
    result = await compile_view.execute(harness.command())
    assert result.view is not None

    item = codec_share.encode_view(harness.scope, result.view)
    rendered = repr(encode_item(item))

    for value in PRIVATE_STRINGS:
        assert value not in rendered
    for identifier in (
        harness.fixture.health_fact_id,
        harness.fixture.unit_fact_id,
        harness.fixture.photo_evidence_id,
        harness.fixture.incident_fact_ids[0],
    ):
        assert str(identifier) not in rendered


async def test_a_deeply_nested_private_value_is_rejected_before_persistence(
    harness: CompileHarness,
) -> None:
    """Matrix AO.

    The scanner is the compiler's, and it walks the canonical primitive rather than the object,
    so a value buried inside a nested safe-evidence reference is exactly as visible to it as one
    at the top level. Reaching in and planting one proves the depth, not the top-level check.
    """

    compile_view = await _seed(harness)
    result = await compile_view.execute(harness.command())
    assert result.view is not None

    poisoned_ref = replace(
        result.view.safe_evidence_refs[0],
        caption="An elevator photo mentioning SECRET_SENTINEL is attached.",
    )
    poisoned = object.__new__(type(result.view))
    for name in (item.name for item in __import__("dataclasses").fields(type(result.view))):
        object.__setattr__(poisoned, name, getattr(result.view, name))
    object.__setattr__(poisoned, "safe_evidence_refs", (poisoned_ref,))

    assert _scan(poisoned) is True
    assert _scan(result.view) is False


async def test_the_canonical_primitive_of_the_view_holds_no_private_key_name(
    harness: CompileHarness,
) -> None:
    """The denylist covers key names as well as values, at every depth."""

    compile_view = await _seed(harness)
    result = await compile_view.execute(harness.command())
    assert result.view is not None

    primitive = to_canonical_primitive(result.view)

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            found = set(value)
            for item in value.values():
                found |= keys(item)
            return found
        if isinstance(value, list):
            nested: set[str] = set()
            for item in value:
                nested |= keys(item)
            return nested
        return set()

    for name in keys(primitive):
        assert not any(
            token in name for token in ("private", "raw", "object_key", "uri", "contact")
        )


# -- metadata, end to end -----------------------------------------------------------------


async def test_a_source_carrying_gps_and_a_sentinel_exports_neither(
    harness: CompileHarness,
) -> None:
    """Matrix S and V, through the whole compile rather than through the sanitizer alone."""

    raw = photo_bytes_with_metadata()
    assert b"SECRET_SENTINEL" in raw
    compile_view = await _seed(harness, photo=raw)

    result = await compile_view.execute(harness.command())

    assert result.view is not None
    key = next(iter(harness.objects.export))
    stored = harness.objects.export[key]
    assert b"SECRET_SENTINEL" not in stored.content
    assert b"Exif" not in stored.content
    assert SENTINEL_PATTERN.search(repr(result.view)) is None


# -- logs ----------------------------------------------------------------------------------


async def test_compile_logs_carry_identifiers_and_no_private_text(
    harness: CompileHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """Matrix AP. Counts and digests are allowed; the corpus is not."""

    compile_view = await _seed(harness)

    with caplog.at_level(logging.INFO, logger="chorus"):
        result = await compile_view.execute(harness.command())

    assert result.view is not None
    names = {record.__dict__.get("event_name") for record in caplog.records}
    assert {"compile.started", "compile.allowed"} <= names

    rendered = " ".join(repr(record.__dict__) for record in caplog.records)
    for value in PRIVATE_STRINGS:
        assert value not in rendered
    assert str(harness.fixture.health_fact_id) not in rendered
    assert str(harness.fixture.photo_evidence_id) not in rendered
    assert result.view.view_hash.value in rendered


async def test_a_denial_logs_its_reason_codes_and_nothing_else(
    harness: CompileHarness, caplog: pytest.LogCaptureFixture
) -> None:
    from chorus.application.errors import PolicyDeniedError

    compile_view = await _seed(harness)

    with caplog.at_level(logging.INFO, logger="chorus"), pytest.raises(PolicyDeniedError):
        await compile_view.execute(
            harness.command(
                compile_id=harness_uuid("compile:denied-log"),
                expected_case_version=harness.case.version + 1,
            )
        )

    denied = [
        record for record in caplog.records if record.__dict__.get("event_name") == "compile.denied"
    ]
    assert denied
    assert denied[0].__dict__["reason_codes"] == ["STALE_CASE_VERSION"]
    rendered = " ".join(repr(record.__dict__) for record in caplog.records)
    for value in PRIVATE_STRINGS:
        assert value not in rendered
