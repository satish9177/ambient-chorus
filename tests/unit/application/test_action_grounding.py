"""The frozen ADR-021 grammar, asserted rule by rule and example by example.

Every worked example in ADR-021 § 13 appears here, plus the cases the ADR calls out in prose:
the ISO-date/telephone precedence, the ``mailto:`` rule as its own rule, the ban on
substring-based numeric support, and the deliberate asymmetry that makes name support a
substring match.

A golden is a statement about a specific algorithm, so these tests are the algorithm's
specification in executable form. If one of them starts failing, the grammar changed -- and the
grammar may only change through a superseding ADR that makes a rule *more* specific.
"""

from __future__ import annotations

import pytest

from chorus.application.services.action_grounding import (
    NUMBER_WORDS,
    TEMPLATE_COPY_ALLOWLIST,
    GroundingRejection,
    ground_field,
    iso_date_spans,
    name_candidates,
    normalize,
    risk_tokens,
    structural_rejections,
    unsupported_names,
    unsupported_tokens,
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
# § 4 normalization
# ---------------------------------------------------------------------------------------


def test_normalization_casefolds_collapses_whitespace_and_strips() -> None:
    # The no-break space is written as an escape rather than pasted: it is invisible in a
    # diff, and this assertion exists precisely because the rule collapses runs of *Unicode*
    # whitespace to one ASCII space rather than only the ASCII ones.
    assert normalize("  The\u00a0Elevator\t\n FAILED  ") == "the elevator failed"


def test_normalization_is_idempotent_and_nfc_stable() -> None:
    # Casefolding can denormalize, which is why NFC runs twice. Idempotence is the observable
    # consequence: a value already normalized must survive a second pass unchanged.
    once = normalize("Straße ẞ")
    assert normalize(once) == once


def test_normalization_does_not_strip_punctuation_or_fold_diacritics() -> None:
    # Each of these would be a way of guessing that two different strings mean the same thing.
    assert normalize("café-au-lait's") == "café-au-lait's"


# ---------------------------------------------------------------------------------------
# § 5 structural rejection
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("See https://example.com/report", GroundingRejection.URL_PATTERN),
        ("Visit www.example.com today", GroundingRejection.URL_PATTERN),
        ("Write to mailto:pm@example.test", GroundingRejection.MAILTO_PATTERN),
        ("Write to MAILTO:pm@example.test", GroundingRejection.MAILTO_PATTERN),
        ("Reach pm@example.test for details", GroundingRejection.EMAIL_PATTERN),
        ("Call 555-123-4567 to confirm.", GroundingRejection.PHONE_PATTERN),
        ("Call +1 (555) 123-4567.", GroundingRejection.PHONE_PATTERN),
        ("Contact unit 4B.", GroundingRejection.UNIT_PATTERN),
        ("Apartment 12 is affected.", GroundingRejection.UNIT_PATTERN),
        ("A tag <b>here</b>", GroundingRejection.MARKUP),
        ("An entity &amp; here", GroundingRejection.MARKUP),
        ("An image ![alt](x)", GroundingRejection.MARKDOWN_LINK),
        ('A resident said "it stopped".', GroundingRejection.QUOTATION),
        ("A resident said 'it stopped'.", GroundingRejection.QUOTATION),
        ("Reference 3f2a9c11-0b7e-4d18-9a52-1c7f0e6b84d3.", GroundingRejection.IDENTIFIER_SHAPE),
        (
            "Digest sha256:" + "a" * 64,
            GroundingRejection.IDENTIFIER_SHAPE,
        ),
        ("Bell", GroundingRejection.CONTROL_CHARACTER),
        ("Right-to-left ‮ override", GroundingRejection.BIDI_CONTROL),
        ("Zero ​ width", GroundingRejection.INVISIBLE_FORMATTING),
        ("The elevator failed on 14 January.", GroundingRejection.REJECTED_DATE_CONSTRUCT),
        ("The elevator failed on Jan 14.", GroundingRejection.REJECTED_DATE_CONSTRUCT),
        ("The elevator failed on 01/14/2030.", GroundingRejection.REJECTED_DATE_CONSTRUCT),
        ("It failed on Monday.", GroundingRejection.REJECTED_DATE_CONSTRUCT),
        ("It failed yesterday.", GroundingRejection.REJECTED_DATE_CONSTRUCT),
    ],
)
def test_rejected_constructs(text: str, expected: GroundingRejection) -> None:
    assert expected in structural_rejections(text)


def test_word_internal_apostrophe_is_legal() -> None:
    # ``elevator's`` has to work, or the ban on quotation would ban ordinary English.
    assert structural_rejections("The elevator's door jammed.") == ()


def test_sensitive_term_rule_reuses_the_compiler_scanner() -> None:
    # Absolute, and the frozen "not present in a safe fact" exception is vacuous: gate 21 runs
    # this exact pattern over every constructed view, so a view satisfying that exception
    # cannot exist. Reused rather than restated, so there is no second denylist to drift.
    assert GroundingRejection.SENSITIVE_TERM in structural_rejections(
        "Her mother has a medical condition."
    )


# ---------------------------------------------------------------------------------------
# The ISO-date / telephone precedence, which is normative
# ---------------------------------------------------------------------------------------


def test_valid_iso_date_is_not_a_phone_number() -> None:
    assert GroundingRejection.PHONE_PATTERN not in structural_rejections(
        "Please confirm by 2030-01-19."
    )


@pytest.mark.parametrize("text", ["2030-02-29", "2030-13-01", "2030-99-99"])
def test_invalid_calendar_date_gets_no_phone_exemption(text: str) -> None:
    # The exemption is from PHONE_PATTERN only and it applies to *validated* spans. A
    # syntactically date-shaped string that is not a real Gregorian date is a phone candidate.
    assert iso_date_spans(text) == ()
    assert GroundingRejection.PHONE_PATTERN in structural_rejections(f"It began {text}.")


def test_there_is_no_substring_exemption_from_the_phone_rule() -> None:
    # A telephone match that merely *contains* a valid ISO date is still a telephone number.
    assert GroundingRejection.PHONE_PATTERN in structural_rejections("Call 2030-01-14-5551234.")


def test_bare_digit_run_is_a_phone_candidate() -> None:
    assert GroundingRejection.PHONE_PATTERN in structural_rejections("12345678")


def test_mailto_is_its_own_rule_and_the_url_pattern_never_matches_one() -> None:
    # ``mailto:`` has no authority component, so ``[a-z][a-z0-9+.-]*://`` never matches it.
    # Folding it into "a URL" would have quietly dropped a rule the contract has always had.
    from chorus.application.services.action_grounding import URL_PATTERN

    assert URL_PATTERN.search("mailto:pm@example.test") is None
    assert GroundingRejection.MAILTO_PATTERN in structural_rejections("mailto:pm@example.test")


# ---------------------------------------------------------------------------------------
# § 6 risk tokens and exact support
# ---------------------------------------------------------------------------------------


def test_iso_date_precedes_numeric_so_a_date_is_one_token() -> None:
    kinds = [token.kind for token in risk_tokens(normalize("failed on 2030-01-14"))]
    assert kinds == ["iso_date"]


def test_token_alternation_covers_the_five_frozen_kinds() -> None:
    tokens = risk_tokens(normalize("2030-01-14 09:30 3rd 4.5% seven"))
    assert [token.kind for token in tokens] == [
        "iso_date",
        "clock_time",
        "ordinal_numeric",
        "numeric",
        "number_word",
    ]


def test_number_word_list_is_closed_and_contains_the_evasion_it_exists_to_close() -> None:
    assert "four" in NUMBER_WORDS
    assert "second" in NUMBER_WORDS


@pytest.mark.parametrize(
    "text",
    [
        "Four residents reported an impact.",  # word form; the fact says 4
        "24 residents reported an impact.",  # substring containment is not support
        "04 residents reported an impact.",
        "4.0 residents reported an impact.",
        "The elevator failed three times.",
        "It failed on 2030-01-20.",  # a date the cited fact does not state
    ],
)
def test_unsupported_tokens_are_rejected(text: str) -> None:
    assert GroundingRejection.UNSUPPORTED_TOKEN.value in _ground(text)


@pytest.mark.parametrize(
    "text",
    [
        "The elevator was out of service on 2030-01-14.",
        "4 residents reported an impact.",
        "Please confirm by 2030-01-19.",
    ],
)
def test_supported_tokens_are_accepted(text: str) -> None:
    assert _ground(text) == ()


def test_support_is_match_equality_and_never_containment() -> None:
    # ``4`` is a substring of ``24``. Accepting in that direction would put a false quantity in
    # an external message, which is the whole reason numeric support is match-to-match.
    assert unsupported_tokens("24 residents", ("4 residents",))
    assert not unsupported_tokens("4 residents", ("4 residents",))


# ---------------------------------------------------------------------------------------
# § 7 proper names
# ---------------------------------------------------------------------------------------


def test_sentence_initial_single_word_is_not_a_candidate() -> None:
    candidates = name_candidates("The elevator failed.")
    assert [item.text for item in candidates] == ["The"]
    assert candidates[0].sentence_initial_single_word


def test_multi_word_run_is_a_candidate_even_sentence_initially() -> None:
    candidates = name_candidates("Bob Smith should be called.")
    assert [item.text for item in candidates] == ["Bob Smith"]
    assert not candidates[0].sentence_initial_single_word


def test_non_initial_single_word_is_a_candidate() -> None:
    candidates = name_candidates("Please contact Bob.")
    assert [item.text for item in candidates] == ["Please", "Bob"]
    assert candidates[1].sentence_initial_single_word is False


def test_word_after_a_terminator_is_sentence_initial() -> None:
    candidates = name_candidates("It failed. Repairs are needed.")
    assert all(item.sentence_initial_single_word for item in candidates)


def test_unsupported_name_is_rejected() -> None:
    assert GroundingRejection.UNSUPPORTED_NAME.value in _ground("Bob Smith reported the outage.")


@pytest.mark.parametrize(
    "text",
    [
        "Residents of Maple Court are affected.",
        "Please write to Property Management.",
        "Ambient CHORUS compiled this message.",
    ],
)
def test_supported_names_are_accepted(text: str) -> None:
    assert _ground(text) == ()


def test_name_support_is_substring_and_the_asymmetry_is_deliberate() -> None:
    # A fragment of a published name asserts nothing new, so ``Maple Court`` passes against a
    # longer published label. The opposite direction -- match equality for names -- would
    # reject a correct name for no safety gain, while substring matching for *numbers* would
    # accept a different quantity. The two rules point opposite ways on purpose.
    assert not unsupported_names("Residents of Maple Court", ("Maple Court Residents Association",))


def test_unicode_category_grammar_accepts_marks_and_hyphens() -> None:
    # The frozen grammar is an uppercase start followed by letters, combining marks,
    # apostrophes, and hyphens -- realized with unicodedata.category and no new dependency.
    # One maximal run rather than two candidates: ``NAME_RUN`` is a sequence of capitalized
    # words separated by single spaces, so a sentence-initial capital followed by a name is one
    # run and therefore not exempt.
    assert [item.text for item in name_candidates("Contact Ångström-Núñez now.")] == [
        "Contact Ångström-Núñez"
    ]


def test_identifiers_never_support_a_name() -> None:
    # A proposal cannot become grounded by naming an identifier, and an identifier in prose is
    # already refused by § 5.
    assert unsupported_names(
        "Please contact Bob Smith", ("3f2a9c11-0b7e-4d18-9a52-1c7f0e6b84d3",)
    ) == ("Bob Smith",)


# ---------------------------------------------------------------------------------------
# The ADR § 13 worked examples, end to end
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        ("The elevator was out of service on 2030-01-14.", True),
        ("4 residents reported an impact.", True),
        ("Four residents reported an impact.", False),
        ("The elevator failed on 14 January.", False),
        ("24 residents reported an impact.", False),
        ("Residents of Maple Court are affected.", True),
        ("Please write to Property Management.", True),
        ("Bob Smith reported the outage.", False),
        ("The elevator's door jammed.", True),
        ('A resident said "it stopped again".', False),
        ("See https://example.com/report", False),
        ("Write to mailto:pm@example.test", False),
        ("Call 555-123-4567 to confirm.", False),
        ("Please confirm by 2030-01-19.", True),
        ("The outage began 2030-02-29.", False),
        ("Reference 3f2a9c11-0b7e-4d18-9a52-1c7f0e6b84d3.", False),
        ("Contact unit 4B.", False),
        ("Please repair the elevator.", True),
        ("The elevator was out of service three times.", False),
    ],
)
def test_adr_021_worked_examples(text: str, accepted: bool) -> None:
    assert (_ground(text) == ()) is accepted


def test_structural_rejection_short_circuits_before_support_checking() -> None:
    # A string containing a URL has nothing useful to say about whether its numbers were cited,
    # so the later stages do not run and the reason list describes one refusal rather than
    # three symptoms of it.
    reasons = _ground("See https://example.com/99999 with 99999 residents")
    assert GroundingRejection.UNSUPPORTED_TOKEN.value not in reasons
