"""The frozen ADR-021 grounding grammar: pure functions over strings and one bound view.

Everything here is deterministic and total. There are no embeddings, no similarity scores, no
stemming, no plural folding, no numeric conversion, no date reformatting, and no second model
adjudicating a suspected false positive. A rejection is a rejection: it refuses the **whole**
proposal, and the answer is a re-proposal rather than a bypass, an override, or an opinion.

The three questions it answers, in the order it answers them
------------------------------------------------------------
1. **Is this string structurally admissible at all?** (§5) A closed list of rejected
   constructs, run on the NFC text before any normalization. Control characters, bidi and
   invisible formatting, markup, Markdown link and image syntax, URLs, ``mailto:``, email
   addresses, telephone numbers, unit patterns, identifier shapes, quotation marks, non-ISO
   date constructs, and the compiler's own sensitive-value pattern.
2. **Is every risk token it contains actually stated by a cited fact?** (§6) One ordered
   alternation extracts dates, times, ordinals, numbers, and number words, and support is
   **match-to-match equality** against tokens extracted the same way from a cited ``safe_text``.
3. **Is every proper-name candidate a name the view already publishes?** (§7) Capitalization is
   the signal, position is the only exemption, and support is **substring** containment.

The asymmetry in 2 and 3 is the point rather than an inconsistency. A numeric substring match
would accept a different *quantity* -- ``4`` is a substring of ``24`` -- which is a false
factual assertion in an external message. A name substring match can only accept a fragment of
a name the view already publishes, which asserts nothing new.

Scope
-----
These rules run on exactly four model-authored textual fields: ``subject``, every claim text,
``requested_action``, and every caveat text. They never run on the typed structural fields --
``case_id``, ``view_id``, ``view_hash``, ``claim_id``, ``caveat_id``, ``export_fact_ids``,
``request_fact_ids`` -- which are contract data validated by ownership and current-view checks
instead. Reading "identifiers are rejected outright" as a rule about the whole payload would
make the contract reject its own required identifiers.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from chorus.privacy.compiler import UNSAFE_VALUE_PATTERN

# ---------------------------------------------------------------------------------------
# §4  Comparison normalization
# ---------------------------------------------------------------------------------------

_WHITESPACE_RUN = re.compile(r"\s+", re.UNICODE)


def normalize(value: str) -> str:
    """Fold one string into the single form every support comparison runs in (ADR-021 § 4).

    Normalization exists **only for comparison**. It never rewrites persisted model text, and a
    proposal that passes is stored exactly as the model wrote it.

    ``NFC`` is applied twice on purpose: casefolding can denormalize -- ``\\u1e9e`` casefolds to
    a sequence that is not in NFC -- so re-fixing afterwards is what makes the output canonical
    rather than merely lowercase.

    Nothing else happens here. No punctuation stripping, no hyphen folding, no diacritic
    removal, no stemming, no plural folding. Each of those is a way of guessing that two
    different strings mean the same thing, which is the thing this module refuses to do.
    """

    folded = unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).casefold())
    return _WHITESPACE_RUN.sub(" ", folded).strip()


# ---------------------------------------------------------------------------------------
# §5  Structural rejection
# ---------------------------------------------------------------------------------------


class GroundingRejection(StrEnum):
    """Why one string was refused. Every member refuses the whole proposal.

    Separate from the transport-level ``ActionRejection`` codes so a grounding failure says
    *which rule* fired without the caller having to re-derive it, and so no member here can
    ever carry the offending text with it.
    """

    ENCODING_INVALID = "ENCODING_INVALID"
    CONTROL_CHARACTER = "CONTROL_CHARACTER"
    BIDI_CONTROL = "BIDI_CONTROL"
    INVISIBLE_FORMATTING = "INVISIBLE_FORMATTING"
    MARKUP = "MARKUP"
    MARKDOWN_LINK = "MARKDOWN_LINK"
    URL_PATTERN = "URL_PATTERN"
    MAILTO_PATTERN = "MAILTO_PATTERN"
    EMAIL_PATTERN = "EMAIL_PATTERN"
    PHONE_PATTERN = "PHONE_PATTERN"
    UNIT_PATTERN = "UNIT_PATTERN"
    IDENTIFIER_SHAPE = "IDENTIFIER_SHAPE"
    QUOTATION = "QUOTATION"
    SENSITIVE_TERM = "SENSITIVE_TERM"
    REJECTED_DATE_CONSTRUCT = "REJECTED_DATE_CONSTRUCT"
    UNSUPPORTED_TOKEN = "UNSUPPORTED_TOKEN"  # noqa: S105 - a closed reason code, not a credential
    UNSUPPORTED_NAME = "UNSUPPORTED_NAME"


CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
BIDI_CONTROLS = re.compile(r"[؜‎‏‪-‮⁦-⁩]")
INVISIBLE_FORMATTING = re.compile(r"[­​‌‍⁠﻿]")
MARKUP = re.compile(r"[<>]|&[#0-9A-Za-z]{1,10};")
MARKDOWN_LINK = re.compile(r"!\[|\]\(")
URL_PATTERN = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://|\bwww\.")
MAILTO_PATTERN = re.compile(r"(?i)\bmailto:")
"""A **separate** rule, not a case of the URL rule.

``https://example.com`` has an authority component and matches ``[a-z][a-z0-9+.\\-]*://``;
``mailto:user@example.com`` has none and matches nothing in that pattern. The frozen validator
contract has always required ``mailto:`` rejection, and folding it into "a URL" would have
quietly dropped it. ``MAILTO:`` in any casing is the same rule, and the email-address pattern
remains a second, independent defence against the address itself.
"""
EMAIL_PATTERN = re.compile(r"(?i)[^\s@]+@[^\s@]+\.[a-z]{2,}")
PHONE_CANDIDATE = re.compile(r"\+?[0-9][0-9 \-().]{5,}[0-9]")
UNIT_PATTERN = re.compile(
    r"(?i)\b(?:apt|apartment|unit|suite|ste|flat)\s*#?\s*[0-9]{1,5}[a-z]?\b|#\s*[0-9]{1,5}[a-z]?\b"
)
IDENTIFIER_SHAPE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|sha256:[0-9a-f]{64}"
)
QUOTATION = re.compile(r"[\"`“”„‘«»]|(?<!\w)['’]|['’](?!\w)")  # noqa: RUF001
"""Quotation is banned rather than grounded.

Deciding whether a quoted string is faithfully drawn from a cited fact is entailment over a
span. The obvious alternative -- exact substring equality against ``safe_text`` -- would
silently teach the model that quoting safe text verbatim is the reliable way to pass, which is
the re-identification vector compiler gate 18 already refuses. A word-internal apostrophe stays
legal, so ``elevator's`` works and ``'quoted'`` does not.
"""

ISO_DATE_CANDIDATE = re.compile(r"(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])")

REJECTED_DATE_CONSTRUCTS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+[0-9]{1,2}\b"),
    re.compile(r"(?i)\b[0-9]{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"),
    re.compile(r"\b[0-9]{1,4}/[0-9]{1,2}/[0-9]{1,4}\b"),
    re.compile(r"(?i)\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"),
    re.compile(r"(?i)\b(?:yesterday|today|tomorrow|last\s+(?:week|month)|this\s+(?:week|month))\b"),
)
"""Every date construct other than ``YYYY-MM-DD``, rejected outright rather than grounded.

Rejecting rather than grounding is what removes format-equivalence reasoning from the system.
It is also consistent with what a view actually contains: ``chorus.privacy.transformations``
renders incident dates through ``date.isoformat()``, so ISO is the only date form a safe fact
ever carries. The relative expressions are rejected because their referent is the reading time,
which is not a fact any view can support.
"""


def iso_date_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Every span that is a syntactic ISO-date candidate **and** a real calendar date.

    Step A of the normative precedence in ADR-021 § 5. The frozen telephone candidate matches
    ``2030-01-14``, and § 6 makes ``YYYY-MM-DD`` the only date a model may write, so an
    unordered reading of the two rules would reject every supported date. Validation is
    ``date.fromisoformat`` on the exact substring, so ``2030-02-29`` and ``2030-13-01`` are not
    ISO-date spans and get no exemption from anything.
    """

    spans: list[tuple[int, int]] = []
    for match in ISO_DATE_CANDIDATE.finditer(text):
        try:
            date.fromisoformat(match.group(0))
        except ValueError:
            continue
        spans.append(match.span())
    return tuple(spans)


def _phone_rejection(text: str) -> bool:
    """Step B: a telephone match that is not *exactly* one validated ISO-date span.

    **There is no substring exemption.** A telephone match that merely contains a valid ISO
    date, or overlaps one, is still a telephone number -- ``2030-01-14 555-0100`` is a phone
    number beside a date, and accepting it because part of it parsed would be the exemption
    doing the opposite of its job.

    The exemption is from ``PHONE_PATTERN`` only and authorizes nothing: a date that survives
    here must still be matched exactly by an ``ISO_DATE`` token in a cited ``safe_text``.
    """

    dates = set(iso_date_spans(text))
    return any(match.span() not in dates for match in PHONE_CANDIDATE.finditer(text))


_SIMPLE_RULES: tuple[tuple[re.Pattern[str], GroundingRejection], ...] = (
    (CONTROL_CHARACTERS, GroundingRejection.CONTROL_CHARACTER),
    (BIDI_CONTROLS, GroundingRejection.BIDI_CONTROL),
    (INVISIBLE_FORMATTING, GroundingRejection.INVISIBLE_FORMATTING),
    (MARKUP, GroundingRejection.MARKUP),
    (MARKDOWN_LINK, GroundingRejection.MARKDOWN_LINK),
    (URL_PATTERN, GroundingRejection.URL_PATTERN),
    (MAILTO_PATTERN, GroundingRejection.MAILTO_PATTERN),
    (EMAIL_PATTERN, GroundingRejection.EMAIL_PATTERN),
    (UNIT_PATTERN, GroundingRejection.UNIT_PATTERN),
    (IDENTIFIER_SHAPE, GroundingRejection.IDENTIFIER_SHAPE),
    (QUOTATION, GroundingRejection.QUOTATION),
)


def structural_rejections(value: str) -> tuple[GroundingRejection, ...]:
    """Every rejected construct in one model-authored string, on its NFC text.

    Runs **before** normalization and before any support checking, so a rule that depends on
    case or on an exact code point sees what the model actually wrote.
    """

    try:
        text = unicodedata.normalize("NFC", value)
        text.encode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        # A lone surrogate, or anything that is not strict UTF-8. Nothing further is inspected:
        # the string cannot be compared, rendered, or hashed, so there is nothing to say about
        # its contents that would be true.
        return (GroundingRejection.ENCODING_INVALID,)

    found = [code for pattern, code in _SIMPLE_RULES if pattern.search(text)]
    if _phone_rejection(text):
        found.append(GroundingRejection.PHONE_PATTERN)
    if any(pattern.search(text) for pattern in REJECTED_DATE_CONSTRUCTS):
        found.append(GroundingRejection.REJECTED_DATE_CONSTRUCT)
    if UNSAFE_VALUE_PATTERN.search(text):
        # Absolute, and the frozen "not present in a safe fact" exception is vacuous: compiler
        # gate 21 runs this exact scanner over the constructed view and denies the whole
        # compile on a match, so a current view satisfying that exception cannot exist. The
        # pattern is reused rather than restated, so there is no second denylist to drift.
        found.append(GroundingRejection.SENSITIVE_TERM)
    return tuple(dict.fromkeys(found))


# ---------------------------------------------------------------------------------------
# §6  Risk tokens and exact support
# ---------------------------------------------------------------------------------------

NUMBER_WORDS: frozenset[str] = frozenset(
    {
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
        "hundred",
        "thousand",
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "eleventh",
        "twelfth",
        "thirteenth",
        "fourteenth",
        "fifteenth",
        "sixteenth",
        "seventeenth",
        "eighteenth",
        "nineteenth",
        "twentieth",
    }
)
"""A closed list, existing to close an evasion rather than to add semantics.

Without it a model could write "the elevator failed four times" and pass a check that only
looks at digits. ``second`` is on the list in its ordinal sense and is treated as a risk token
in every sense; a model wanting the time unit should not be writing one.
"""

_NUMBER_WORD_ALTERNATION = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))

RISK_TOKEN = re.compile(
    r"(?P<iso_date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"|(?P<clock_time>[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)"
    r"|(?P<ordinal_numeric>[0-9]+(?:st|nd|rd|th))"
    r"|(?P<numeric>[0-9]+(?:[.,][0-9]+)*%?)"
    rf"|(?P<number_word>\b(?:{_NUMBER_WORD_ALTERNATION})\b)"
)
"""One ordered alternation, scanned left to right with non-overlapping matches.

Order is normative. ``ISO_DATE`` precedes ``NUMERIC`` so that ``2030``, ``01``, and ``14``
inside ``2030-01-14`` are not extracted as three separate numbers; an earlier alternative
consumes its span and a later one cannot re-match inside it.
"""


@dataclass(frozen=True, slots=True)
class RiskToken:
    """One extracted token, carrying its kind so support is compared like for like."""

    kind: str
    text: str


def risk_tokens(normalized_text: str) -> tuple[RiskToken, ...]:
    """Extract every risk token from already-normalized text, in document order."""

    return tuple(
        RiskToken(kind=match.lastgroup or "", text=match.group(0))
        for match in RISK_TOKEN.finditer(normalized_text)
    )


def unsupported_tokens(field_text: str, supporting_texts: tuple[str, ...]) -> tuple[RiskToken, ...]:
    """Tokens the model wrote that no cited fact states in the same form.

    **Match-to-match equality, never substring containment.** Containment accepts in the
    dangerous direction: ``4`` is a substring of ``24``, and ``2030-01-14`` contains ``01``.
    Equality means a supported number is a number the safe fact actually stated.

    There is no cross-form conversion in either direction. ``four`` is not supported by ``4``,
    ``4`` is not supported by ``four``, ``04`` is not supported by ``4``, and ``4.0`` is not
    supported by ``4``. Each equivalence table anyone might add is a small semantic engine with
    locale, ordinal, and range edge cases, and every entry is a place two implementations can
    disagree. Rejecting the ambiguous form and telling the model which form to use costs one
    prompt sentence.
    """

    supported = {
        (token.kind, token.text)
        for text in supporting_texts
        for token in risk_tokens(normalize(text))
    }
    return tuple(
        token
        for token in risk_tokens(normalize(field_text))
        if (token.kind, token.text) not in supported
    )


# ---------------------------------------------------------------------------------------
# §7  Proper-name candidates
# ---------------------------------------------------------------------------------------

TEMPLATE_COPY_ALLOWLIST: frozenset[str] = frozenset({"Ambient CHORUS", "CHORUS"})
"""The closed reviewed set of names the template itself may contribute. Exactly two in V1."""

_NAME_EXTRA_CHARACTERS = frozenset({"'", "’", "-"})  # noqa: RUF001
_SENTENCE_TERMINATORS = frozenset({".", "!", "?"})


def _is_uppercase_letter(character: str) -> bool:
    return unicodedata.category(character) == "Lu"


def _is_name_body(character: str) -> bool:
    """Letters, combining marks, apostrophes, and hyphens -- the frozen ``\\p{L}\\p{M}'-`` set.

    Realized with :func:`unicodedata.category` rather than by adding a regex engine that
    understands ``\\p{}`` syntax. The ADR states the grammar in Unicode general-category
    notation and explicitly declines to mandate a dependency for it; ``pyproject.toml`` and
    ``uv.lock`` are untouched by this module.
    """

    return unicodedata.category(character)[0] in {"L", "M"} or character in _NAME_EXTRA_CHARACTERS


@dataclass(frozen=True, slots=True)
class NameCandidate:
    """One maximal run of capitalized words, and whether position exempts it."""

    text: str
    sentence_initial_single_word: bool


@dataclass(frozen=True, slots=True)
class _Word:
    """One maximal run of name-body characters, with the span it occupied.

    The span is kept because ``NAME_RUN`` is defined by what separates two words -- a *single
    space* and nothing else -- so joining two words requires knowing exactly which characters
    stood between them.
    """

    text: str
    start: int
    end: int
    sentence_initial: bool


def name_candidates(value: str) -> tuple[NameCandidate, ...]:
    """Find proper-name candidates in the **NFC text before casefolding**.

    Capitalization is the signal, so this runs before normalization -- a casefolded string has
    no proper names in it.

    A word is a maximal run of the frozen body class -- ``\\p{L}\\p{M}`` plus
    apostrophes and the hyphen -- and **every other character delimits**, punctuation
    included. That is what makes ``Bob/Smith``,
    ``Bob:Smith``, ``Bob,Smith``, and ``Bob (Smith)`` two words rather than one unparseable
    blob -- and it is the property whose absence let a capitalized name disappear from
    detection entirely by being written with a slash in it.

    A ``NAME_RUN`` is a maximal sequence of ``CAPITALIZED_WORD``s separated by a *single space*.
    Any other separator -- punctuation, two spaces, a newline -- ends the run, so the words on
    either side are each considered on their own and neither vanishes.

    A run is a candidate unless it is a *single* word in sentence-initial position, where
    sentence-initial means the first word of the field or the first word after ``.``, ``!``, or
    ``?`` followed by whitespace. That exemption is deliberately positional and not a stop-word
    list: a stop-word list is the arbitrary lexical heuristic ADR-012 refused, and it would need
    maintaining in every language the demo might ever show. Position is decidable from the
    string, and punctuation glued to a word never makes it sentence-initial.
    """

    text = unicodedata.normalize("NFC", value)
    words = _split_words(text)
    candidates: list[NameCandidate] = []
    run: list[_Word] = []
    for word in words:
        if not _is_capitalized_word(word.text):
            if run:
                candidates.append(_close_run(run))
                run = []
            continue
        if run and not _joined_by_single_space(text, run[-1], word):
            candidates.append(_close_run(run))
            run = []
        run.append(word)
    if run:
        candidates.append(_close_run(run))
    return tuple(candidates)


def _split_words(text: str) -> tuple[_Word, ...]:
    """Split into maximal name-body runs. Every non-body character is a delimiter.

    Apostrophes and hyphens are body characters *inside* a word -- ``elevator's`` and
    ``Ångström-Núñez`` are one word each -- but the frozen grammar starts a word at an
    uppercase letter, so a leading or trailing one is trimmed off rather than allowed to make
    ``-Smith`` unrecognizable as a capitalized word.
    """

    words: list[_Word] = []
    index = 0
    length = len(text)
    while index < length:
        if not _is_name_body(text[index]):
            index += 1
            continue
        start = index
        while index < length and _is_name_body(text[index]):
            index += 1
        trimmed_start, trimmed_end = _trim_span(text, start, index)
        if trimmed_start < trimmed_end:
            words.append(
                _Word(
                    text=text[trimmed_start:trimmed_end],
                    start=trimmed_start,
                    end=trimmed_end,
                    sentence_initial=_is_sentence_initial(text, trimmed_start),
                )
            )
    return tuple(words)


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Drop leading and trailing apostrophes and hyphens from one body run."""

    while start < end and text[start] in _NAME_EXTRA_CHARACTERS:
        start += 1
    while end > start and text[end - 1] in _NAME_EXTRA_CHARACTERS:
        end -= 1
    return start, end


def _is_sentence_initial(text: str, start: int) -> bool:
    """Whether the word beginning at ``start`` opens the field or a new sentence.

    "After a terminator followed by whitespace" is checked literally: a word glued to the
    punctuation before it -- ``Bob/Smith`` -- opens no sentence, so ``Smith`` gets no exemption.
    """

    index = start - 1
    saw_whitespace = False
    while index >= 0 and text[index].isspace():
        saw_whitespace = True
        index -= 1
    if index < 0:
        return True
    if not saw_whitespace:
        return False
    return text[index] in _SENTENCE_TERMINATORS


def _joined_by_single_space(text: str, previous: _Word, current: _Word) -> bool:
    """``NAME_RUN`` joins two capitalized words on exactly one space and nothing else."""

    return text[previous.end : current.start] == " "


def _is_capitalized_word(word: str) -> bool:
    """The frozen ``CAPITALIZED_WORD := \\p{Lu}[\\p{L}\\p{M} apostrophe hyphen]{1,}``.

    At least two characters: the grammar requires one body character after the uppercase start,
    so a bare initial such as the ``B`` of ``Building B`` is not a word of a name run.
    """

    if len(word) < 2:
        return False
    return _is_uppercase_letter(word[0]) and all(_is_name_body(item) for item in word[1:])


def _close_run(run: list[_Word]) -> NameCandidate:
    return NameCandidate(
        text=" ".join(word.text for word in run),
        sentence_initial_single_word=len(run) == 1 and run[0].sentence_initial,
    )


def unsupported_names(field_text: str, supporting_texts: tuple[str, ...]) -> tuple[str, ...]:
    """Name candidates the view does not already publish somewhere.

    Support is **substring** containment of the normalized candidate in a normalized supporting
    text, and the asymmetry with numeric match-equality is deliberate. A numeric substring match
    accepts a different quantity, which is a false factual assertion; a name substring match can
    only accept a fragment of a name the view already publishes, which asserts nothing new --
    it is what lets ``Maple Court`` pass against a label of ``Maple Court Residents Association``
    with no safety loss.

    Identifiers never support anything. A UUID or a ``sha256:`` digest is neither a supportable
    token nor a source of support, and one appearing in prose is already refused by § 5.
    """

    haystacks = tuple(normalize(text) for text in supporting_texts)
    unsupported: list[str] = []
    for candidate in name_candidates(field_text):
        if candidate.sentence_initial_single_word:
            continue
        needle = normalize(candidate.text)
        if not needle or any(needle in haystack for haystack in haystacks):
            continue
        unsupported.append(candidate.text)
    return tuple(dict.fromkeys(unsupported))


# ---------------------------------------------------------------------------------------
# The one entry point a caller needs
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class GroundingOutcome:
    """What one field's grounding produced: closed codes only, never the offending text.

    The text is deliberately absent. A rejection is logged, audited, and counted, and a code
    that carried the string with it would put model prose about a private case into every one
    of those places.
    """

    rejections: tuple[GroundingRejection, ...]

    @property
    def accepted(self) -> bool:
        return not self.rejections


def ground_field(
    field_text: str,
    *,
    token_support: tuple[str, ...],
    name_support: tuple[str, ...],
) -> GroundingOutcome:
    """Run §5, §6, and §7 over one model-authored field, in that order.

    ``token_support`` is the cited facts' ``safe_text`` values. ``name_support`` is those plus
    the destination display label, the community public label, and the template-copy allowlist
    -- the two names a proposal legitimately needs that are not facts with an
    ``export_fact_id``, plus the copy the renderer contributes itself.

    Structural rejection runs first and alone: if the string contains a URL or a phone number
    there is nothing useful to say about whether its numbers were cited, and running the later
    stages anyway would produce a longer list describing the same refusal.
    """

    structural = structural_rejections(field_text)
    if structural:
        return GroundingOutcome(rejections=structural)

    rejections: list[GroundingRejection] = []
    if unsupported_tokens(field_text, token_support):
        rejections.append(GroundingRejection.UNSUPPORTED_TOKEN)
    if unsupported_names(field_text, name_support):
        rejections.append(GroundingRejection.UNSUPPORTED_NAME)
    return GroundingOutcome(rejections=tuple(rejections))


__all__ = [
    "NUMBER_WORDS",
    "TEMPLATE_COPY_ALLOWLIST",
    # Re-exported deliberately, not incidentally: the sensitive-term rule reuses the compiler's
    # own scanner rather than writing a second denylist, and naming it here is what lets a test
    # assert the two are the same object rather than two patterns that currently agree.
    "UNSAFE_VALUE_PATTERN",
    "GroundingOutcome",
    "GroundingRejection",
    "NameCandidate",
    "RiskToken",
    "ground_field",
    "iso_date_spans",
    "name_candidates",
    "normalize",
    "risk_tokens",
    "structural_rejections",
    "unsupported_names",
    "unsupported_tokens",
]
