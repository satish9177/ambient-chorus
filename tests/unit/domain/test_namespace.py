"""The frozen isolation-namespace grammar.

``docs/architecture/06-persistence-and-evidence.md`` fixes the contract: key segments are
uppercase ASCII and ``namespace`` is validated ``[A-Z][A-Z0-9_]{1,31}``. The namespace is the
leading segment of every partition key, so the grammar is an isolation boundary and not a
formatting preference: it is validated and never transformed. Silently upper-casing a value
would alias two namespaces onto one partition, which is exactly the merge the boundary exists
to prevent.

The grammar is generic on purpose. Which namespaces a given environment may use is a
configuration rule (see ``tests/unit/tooling/test_settings.py``), not a property of the type.
"""

from __future__ import annotations

import pytest

from chorus.domain.ids import NAMESPACE_PATTERN, Namespace

ACCEPTED = [
    "DEMO",
    "LOCAL_DEV",
    "LOCAL_DEVELOPER",
    "TEST_ELEVATOR_V1",
    "TEST_PERSISTENCE",
    "A_",
    "AB",
    "A0",
    "A" * 32,
    "Z9_9Z",
]

REJECTED = [
    "LOCAL_dev",
    "TEST_elevator_v1",
    "LOCAL-DEV",
    "local_dev",
    "demo",
    "dEMO",
    "1ABC",
    "_ABC",
    "A B",
    "A\tB",
    " DEMO",
    "DEMO ",
    "DEMO\n",
    "",
    "A",
    "A" * 33,
    "A.B",
    "A:B",
    "A/B",
    "A#B",
    "A+B",
]


@pytest.mark.parametrize("value", ACCEPTED)
def test_a_canonical_namespace_is_accepted(value: str) -> None:
    assert Namespace(value).value == value


@pytest.mark.parametrize("value", REJECTED)
def test_a_non_canonical_namespace_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="invalid namespace"):
        Namespace(value)


@pytest.mark.parametrize("value", ACCEPTED)
def test_a_namespace_is_never_transformed(value: str) -> None:
    """Validation only. Normalising would merge two isolation boundaries into one."""

    namespace = Namespace(value)

    assert namespace.value == value
    assert str(namespace) == value


@pytest.mark.parametrize("value", ["local_dev", "LOCAL-DEV", "TEST_elevator_v1"])
def test_an_invalid_namespace_is_not_repaired_into_a_valid_one(value: str) -> None:
    """The uppercase form of a rejected value exists, and is still not what is stored."""

    assert NAMESPACE_PATTERN.fullmatch(value) is None
    with pytest.raises(ValueError, match="invalid namespace"):
        Namespace(value)


def test_the_length_bounds_are_two_and_thirty_two() -> None:
    assert Namespace("A" * 2).value == "AA"
    assert Namespace("A" * 32).value == "A" * 32
    for length in (1, 33):
        with pytest.raises(ValueError, match="invalid namespace"):
            Namespace("A" * length)


def test_the_grammar_carries_no_environment_prefix() -> None:
    """``LOCAL_``/``TEST_``/``DEMO`` are configuration conventions, not type constraints."""

    assert Namespace("COMMUNITY_ONE").value == "COMMUNITY_ONE"
    assert Namespace("X_").value == "X_"


def test_the_pattern_is_the_one_the_architecture_freezes() -> None:
    assert NAMESPACE_PATTERN.pattern == r"[A-Z][A-Z0-9_]{1,31}"


def test_a_non_ascii_look_alike_is_rejected() -> None:
    """The grammar is ASCII. A confusable letter must not pass for the letter it resembles.

    The value is built from code points rather than written literally so that this file
    stays ASCII and the character cannot be mistaken for its Latin twin when read.
    """

    cyrillic_em = chr(0x041C)  # CYRILLIC CAPITAL LETTER EM
    latin_em = "M"

    assert cyrillic_em != latin_em
    assert Namespace(f"DE{latin_em}O").value == "DEMO"
    with pytest.raises(ValueError, match="invalid namespace"):
        Namespace(f"DE{cyrillic_em}O")
    with pytest.raises(ValueError, match="invalid namespace"):
        Namespace("DEMO" + chr(0x00C9))  # LATIN CAPITAL LETTER E WITH ACUTE
