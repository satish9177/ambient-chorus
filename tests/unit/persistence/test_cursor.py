"""Category F: signed pagination cursors are opaque, bound, and fail closed."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import MISSING, fields
from pathlib import Path

import pytest
from tests.fixtures.persistence import CURSOR_SECRET, OTHER_CURSOR_SECRET, PRIMARY

import chorus
from chorus.domain.ids import Namespace
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.ports.errors import InvalidCursorError, PersistenceErrorCode
from chorus.ports.limits import MAX_PAGE_SIZE
from chorus.ports.pagination import PageCursor, PageRequest, QueryBinding

BINDING = QueryBinding.CORE_CASE_FACTS
PARTITION = "NS#TEST_PERSISTENCE#CASE#00000000-0000-4000-8000-000000000000"
SORT_KEY = "FACT#11111111-1111-4111-8111-111111111111"


def codec() -> SignedCursorCodec:
    return SignedCursorCodec(CURSOR_SECRET)


def verify(cursor: PageCursor) -> str:
    return codec().verify(
        cursor, namespace=PRIMARY.namespace, binding=BINDING, partition_key=PARTITION
    )


def issue() -> PageCursor:
    return codec().issue(
        namespace=PRIMARY.namespace,
        binding=BINDING,
        partition_key=PARTITION,
        sort_key=SORT_KEY,
    )


def test_a_short_secret_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        SignedCursorCodec(b"too-short")


def test_round_trip_returns_the_last_evaluated_sort_key() -> None:
    cursor = issue()

    resumed = codec().verify(
        cursor, namespace=PRIMARY.namespace, binding=BINDING, partition_key=PARTITION
    )

    assert resumed == SORT_KEY


def test_issuing_is_deterministic() -> None:
    assert issue().value == issue().value


def test_a_tampered_payload_is_rejected() -> None:
    cursor = issue()
    payload, _, tag = cursor.value.partition(".")
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    forged = decoded.replace(b"FACT#1", b"FACT#2")
    forged_payload = base64.urlsafe_b64encode(forged).decode("ascii").rstrip("=")

    with pytest.raises(InvalidCursorError) as error:
        codec().verify(
            PageCursor(f"{forged_payload}.{tag}"),
            namespace=PRIMARY.namespace,
            binding=BINDING,
            partition_key=PARTITION,
        )
    assert error.value.code is PersistenceErrorCode.INVALID_CURSOR


def test_a_cursor_signed_with_another_secret_is_rejected() -> None:
    foreign = SignedCursorCodec(OTHER_CURSOR_SECRET).issue(
        namespace=PRIMARY.namespace,
        binding=BINDING,
        partition_key=PARTITION,
        sort_key=SORT_KEY,
    )

    with pytest.raises(InvalidCursorError):
        codec().verify(
            foreign, namespace=PRIMARY.namespace, binding=BINDING, partition_key=PARTITION
        )


def test_a_cursor_cannot_be_replayed_against_another_namespace() -> None:
    cursor = issue()

    with pytest.raises(InvalidCursorError):
        codec().verify(
            cursor,
            namespace=Namespace("TEST_PERSISTENCE_ALT"),
            binding=BINDING,
            partition_key=PARTITION,
        )


def test_a_cursor_cannot_be_replayed_against_another_access_pattern() -> None:
    cursor = issue()

    with pytest.raises(InvalidCursorError):
        codec().verify(
            cursor,
            namespace=PRIMARY.namespace,
            binding=QueryBinding.CORE_CASE_REPORTS,
            partition_key=PARTITION,
        )


def test_a_cursor_cannot_be_replayed_against_another_partition() -> None:
    cursor = issue()

    with pytest.raises(InvalidCursorError):
        codec().verify(
            cursor,
            namespace=PRIMARY.namespace,
            binding=BINDING,
            partition_key="NS#TEST_PERSISTENCE#CASE#99999999-9999-4999-8999-999999999999",
        )


@pytest.mark.parametrize(
    "value",
    [
        "not-a-cursor",
        "a.b.c",
        "!!!!.????",
        "eyJ2IjoiY3Vyc29yL3YxIn0",
    ],
)
def test_malformed_cursors_are_rejected(value: str) -> None:
    with pytest.raises(InvalidCursorError):
        codec().verify(
            PageCursor(value),
            namespace=PRIMARY.namespace,
            binding=BINDING,
            partition_key=PARTITION,
        )


def test_a_correctly_signed_payload_with_extra_fields_is_rejected() -> None:
    """A signed but non-canonical payload cannot smuggle extra state through a cursor."""

    instance = codec()
    payload = json.dumps(
        {
            "v": "cursor/v1",
            "ns": PRIMARY.namespace.value,
            "q": BINDING.value,
            "p": PARTITION,
            "k": SORT_KEY,
            "extra": "smuggled",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    # The forgery is signed with the real secret so only canonicality can reject it.
    tag = instance._tag(payload)
    forged = PageCursor(
        f"{base64.urlsafe_b64encode(payload).decode().rstrip('=')}."
        f"{base64.urlsafe_b64encode(tag).decode().rstrip('=')}"
    )

    with pytest.raises(InvalidCursorError):
        instance.verify(
            forged, namespace=PRIMARY.namespace, binding=BINDING, partition_key=PARTITION
        )


def test_a_cursor_carries_no_private_content() -> None:
    cursor = issue()
    payload = base64.urlsafe_b64decode(
        cursor.value.split(".")[0] + "=" * (-len(cursor.value.split(".")[0]) % 4)
    )

    parsed = json.loads(payload)

    assert set(parsed) == {"v", "ns", "q", "p", "k"}
    assert parsed["p"] == PARTITION
    assert parsed["k"] == SORT_KEY


def test_page_requests_are_bounded() -> None:
    assert PageRequest().limit == MAX_PAGE_SIZE
    with pytest.raises(ValueError, match="page size"):
        PageRequest(limit=MAX_PAGE_SIZE + 1)
    with pytest.raises(ValueError, match="page size"):
        PageRequest(limit=0)


def test_the_codec_has_no_default_secret() -> None:
    """A default would ship one signing key to every deployment and every reader."""

    with pytest.raises(TypeError):
        SignedCursorCodec()  # type: ignore[call-arg]

    field = next(item for item in fields(SignedCursorCodec) if item.name == "secret")
    assert field.default is MISSING
    assert field.default_factory is MISSING


def test_no_runtime_module_embeds_signing_key_material() -> None:
    """Key material lives only in tests; runtime code must receive it from its caller."""

    package = Path(chorus.__file__).parent
    for module in package.rglob("*.py"):
        source = module.read_text(encoding="utf-8")
        assert "CURSOR_SECRET" not in source
        for line in source.splitlines():
            if "secret" not in line.lower():
                continue
            # A byte or text literal on a line mentioning a secret is the shape of an
            # embedded key. Type annotations and messages carry no literal.
            assert not re.search(r"""=\s*(?:b?["'])""", line), f"{module.name}: {line.strip()}"


NON_CANONICAL_ENCODINGS = (
    ("standard alphabet plus", "+"),
    ("standard alphabet slash", "/"),
    ("padding character", "="),
    ("inner space", " "),
    ("line feed", chr(10)),
    ("carriage return", chr(13)),
    ("null byte", chr(0)),
    ("punctuation", "!"),
    ("non ascii", chr(0xE9)),
)


@pytest.mark.parametrize(
    ("label", "character"),
    NON_CANONICAL_ENCODINGS,
    ids=[name for name, _ in NON_CANONICAL_ENCODINGS],
)
def test_a_cursor_outside_the_canonical_alphabet_is_rejected(label: str, character: str) -> None:
    """Python's decoder silently discards stray characters; a cursor must not tolerate them."""

    payload, tag = issue().value.split(".")

    with pytest.raises(InvalidCursorError) as raised:
        verify(PageCursor(f"{payload}{character}.{tag}"))

    assert raised.value.code is PersistenceErrorCode.INVALID_CURSOR


def test_a_cursor_with_an_impossible_length_is_rejected() -> None:
    """A base64 segment is never exactly one character past a four-character group."""

    payload, tag = issue().value.split(".")

    with pytest.raises(InvalidCursorError):
        verify(PageCursor(f"{payload}A.{tag}"))


def test_an_alternate_encoding_of_the_same_bytes_is_rejected() -> None:
    """Trailing bits that no encoder emits must not be normalised into a valid cursor.

    A permissive decoder maps several spellings onto the same bytes, so an opaque token could
    otherwise be respelled and still verify.
    """

    _, tag = issue().value.split(".")
    canonical = base64.urlsafe_b64encode(bytes([0])).decode("ascii").rstrip("=")
    alternate = "AB"
    assert base64.urlsafe_b64decode(alternate + "==") == bytes([0])
    assert canonical != alternate

    with pytest.raises(InvalidCursorError):
        verify(PageCursor(f"{alternate}.{tag}"))


def test_an_empty_segment_is_rejected() -> None:
    _, tag = issue().value.split(".")

    with pytest.raises(InvalidCursorError):
        verify(PageCursor(f".{tag}"))


def test_a_canonically_encoded_cursor_still_verifies() -> None:
    """Strictness must not have broken the cursors the codec itself issues."""

    assert verify(issue()) == SORT_KEY
