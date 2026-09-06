"""F02 -- punctuation must not hide an unsupported proper name.

Codex reproduced the bypass exactly: ``Please contact Bob Smith.`` was rejected while
``Please contact Bob/Smith.`` and ``Please contact Bob:Smith.`` were accepted, because the
detector split on whitespace only and then refused ``Bob/Smith`` as a capitalized word --
producing *zero* candidates for a field naming an unsupported person.

The repair implements the frozen ADR-021 § 7 grammar: the body class ``\\p{L}\\p{M}``
plus apostrophes and the hyphen defines a word, **every other character delimits**, and a
``NAME_RUN`` joins two capitalized words on a single space and nothing else. The invariant
these tests protect is one sentence long: punctuation may change how many candidates a
field yields, and may never change that number to zero.
"""

from __future__ import annotations

import pytest

from chorus.application.services.action_grounding import (
    TEMPLATE_COPY_ALLOWLIST,
    GroundingRejection,
    ground_field,
    name_candidates,
    unsupported_names,
)

CITED_FACT = (
    "The elevator was out of service on 2030-01-14, 2030-01-15 and 2030-01-19; "
    "4 residents reported an impact."
)
DESTINATION_LABEL = "Property Management"
COMMUNITY_LABEL = "Maple Court"
NAME_SUPPORT = (CITED_FACT, DESTINATION_LABEL, COMMUNITY_LABEL, *sorted(TEMPLATE_COPY_ALLOWLIST))


def _ground(text: str) -> tuple[str, ...]:
    outcome = ground_field(text, token_support=(CITED_FACT,), name_support=NAME_SUPPORT)
    return tuple(reason.value for reason in outcome.rejections)


# ---------------------------------------------------------------------------------------
# The reproduction
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Please contact Bob Smith.",
        "Please contact Bob/Smith.",
        "Please contact Bob:Smith.",
        "Please contact Bob,Smith.",
        "Please contact Bob;Smith.",
        "Please contact Bob.Smith.",
        "Please contact Bob (Smith).",
        "Please contact Bob [Smith].",
        "Please contact Bob-Smith.",
        "Please contact Bob—Smith.",
    ],
)
def test_punctuation_never_hides_an_unsupported_name(text: str) -> None:
    """Every separator the reproduction used, and several it did not, still rejects."""

    assert GroundingRejection.UNSUPPORTED_NAME.value in _ground(text)


@pytest.mark.parametrize(
    "text",
    [
        "Please contact Bob/Smith.",
        "Please contact Bob:Smith.",
        "Please contact Bob,Smith.",
        "Please contact Bob (Smith).",
    ],
)
def test_punctuation_never_produces_zero_candidates(text: str) -> None:
    """The precise defect: a punctuated name yielded no candidate at all to check."""

    candidates = [item.text for item in name_candidates(text)]
    assert "Bob" in candidates
    assert "Smith" in candidates


# ---------------------------------------------------------------------------------------
# The grammar the repair implements
# ---------------------------------------------------------------------------------------


def test_a_single_space_joins_a_run_and_punctuation_does_not() -> None:
    """``NAME_RUN`` is capitalized words separated by *single spaces* -- exactly."""

    assert [item.text for item in name_candidates("Please contact Bob Smith.")] == [
        "Please",
        "Bob Smith",
    ]
    assert [item.text for item in name_candidates("Please contact Bob/Smith.")] == [
        "Please",
        "Bob",
        "Smith",
    ]


def test_building_b_yields_the_capitalized_word_of_the_run() -> None:
    """``Building B``: the bare initial is not a ``CAPITALIZED_WORD``.

    The frozen grammar is an uppercase start followed by at least one body character
    (a letter, a combining mark, an apostrophe, or a hyphen) -- so ``B`` is not a word of a
    name run, and ``Building`` is the candidate that must be supported. What matters for this
    finding is that the run does not vanish: an unsupported ``Building`` is still refused.
    """

    assert [item.text for item in name_candidates("The elevator is in Building B.")] == [
        "The",
        "Building",
    ]
    assert GroundingRejection.UNSUPPORTED_NAME.value in _ground("The elevator is in Building B.")


def test_sentence_initial_exemption_is_exactly_single_word_and_positional() -> None:
    """A word glued to punctuation opens no sentence, so it gets no exemption."""

    exempt = name_candidates("The elevator failed.")
    assert [item.text for item in exempt] == ["The"]
    assert exempt[0].sentence_initial_single_word

    glued = {item.text: item for item in name_candidates("Please contact Bob/Smith.")}
    assert glued["Please"].sentence_initial_single_word
    assert not glued["Bob"].sentence_initial_single_word
    assert not glued["Smith"].sentence_initial_single_word


def test_a_multi_word_run_is_never_exempt_even_sentence_initially() -> None:
    candidates = name_candidates("Bob Smith reported the outage.")
    assert [item.text for item in candidates] == ["Bob Smith"]
    assert not candidates[0].sentence_initial_single_word


# ---------------------------------------------------------------------------------------
# Supported names still pass, punctuated or not
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Residents of Maple Court are affected.",
        "Residents of Maple Court, please respond.",
        "Please write to Property Management.",
        "Please write to Property Management: the elevator is out of service.",
        "Ambient CHORUS compiled this message.",
    ],
)
def test_supported_names_survive_the_stricter_word_split(text: str) -> None:
    """Over-rejection is the accepted direction, but not for names the view publishes."""

    assert _ground(text) == ()


def test_word_internal_apostrophes_and_hyphens_remain_one_word() -> None:
    """The body class is unchanged; only the characters *outside* it now delimit."""

    assert [item.text for item in name_candidates("Contact Ångström-Núñez.")] == [
        "Contact Ångström-Núñez"
    ]
    assert unsupported_names("The elevator's door jammed.", NAME_SUPPORT) == ()


def test_leading_and_trailing_hyphens_do_not_hide_a_name() -> None:
    """A hyphen is a body character *inside* a word; the grammar still starts at an uppercase."""

    assert [item.text for item in name_candidates("Please contact -Smith at once.")] == [
        "Please",
        "Smith",
    ]
    assert GroundingRejection.UNSUPPORTED_NAME.value in _ground("Please contact -Smith at once.")
