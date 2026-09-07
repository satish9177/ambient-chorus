"""Nine deterministic checks, per proposal, and one derived deadline.

The asymmetry with the outbound side is worth naming. There, a model's prose is grounded
against safe facts *the system already published*. Here, a model's structured claim is grounded
against text *an outside party wrote*. The direction is opposite; the requirement -- a token in
the output is supported only by a matching token in the source -- is identical, and
[ADR-021](../../../../docs/adr/ADR-021-action-grounding-and-caveats.md) already ships the
grammar, so this module reuses it rather than inventing a second one.

Every proposal is validated and rejected **independently**: a failure drops that proposal and
does not discard its siblings. Every rejection is a closed code, audited, and carries no text.

What the model can and cannot reach
------------------------------------
* **the obligor** is asserted by the correlation, never extracted (check 4);
* **the deadline** is a cited ISO date at end of day UTC, never the model's ``due_at`` (§ 4);
* **the verification method** is a V1 constant the contract has no field for (check 7);
* **the identifier** is the application's.

So the two failures this phase exists to prevent -- a wrong responsible party and an invented
deadline -- are closed structurally rather than by asking a model to behave (SEC-23).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from chorus.application.services.action_grounding import (
    REJECTED_DATE_CONSTRUCTS,
    ground_field,
    normalize,
    structural_rejections,
)
from chorus.contracts.commitment import MAX_SPAN_LENGTH, ProposedCommitmentDraft, SourceSpan

VERIFICATION_METHOD = "AFFECTED_CONTRIBUTOR_CONFIRMATION"
"""The V1 constant. The model has no field for it, and there is no second value to select."""

MIN_DUE_HORIZON = timedelta(hours=1)
MAX_DUE_HORIZON = timedelta(days=30)
"""``received_at + 1 hour <= due_at <= received_at + 30 days``.

A past date fails the lower bound, so there is no separate past-date rule to get wrong.
"""

ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
"""The **one** permitted due-date form. Every other construct is rejected outright."""

DUE_TIME_OF_DAY = (23, 59, 59, 999_999)
"""End of day, in UTC (ADR-027 § 4).

End of day because a promise "by 2026-09-10" is kept at any hour of that day, and an earlier
instant would let a deadline pass before the promise could be. UTC because no construct
carrying a timezone survives check 5 -- there is nothing to convert *from*, which is why
07-action-ses-and-commitments.md's "in UTC after timezone conversion" is superseded: the
grammar removed the conversion rather than specifying it.
"""

SENTENCE_TERMINATORS = frozenset({".", "!", "?"})

CONDITIONAL_TOKENS: frozenset[str] = frozenset(
    {
        "if",
        "unless",
        "subject",
        "pending",
        "tentative",
        "provided",
        "providing",
        "assuming",
        "may",
        "might",
        "could",
        "should",
        "would",
        "hope",
        "hoping",
        "try",
        "trying",
        "attempt",
        "attempting",
        "consider",
        "considering",
        "look",
        "looking",
        "review",
        "reviewing",
        "investigate",
        "investigating",
        "approximately",
        "approx",
        "around",
        "about",
        "possibly",
        "potentially",
        "likely",
        "cannot",
        "can't",
        "won't",
        "unable",
        "decline",
        "declining",
        "refuse",
        "refusing",
        "no",
        "not",
        "never",
    }
)
"""The frozen ADR-027 § 6 conditional-and-refusal list, matched as whole normalized words.

Two of the ADR's entries are **phrases** written across a flat list, and both are already
covered by a member that stands alone: ``will not`` by ``not``, and ``subject to`` by
``subject``. Adding ``will`` and ``to`` as standalone tokens would refuse ordinary English --
every promise contains ``will`` and most contain ``to`` -- which turns a conservative check into
one that accepts nothing, and it would refuse the ADR's own worked example. So the list holds
the standalone words and the phrases are covered by their distinctive half.

"We'll look into it" fails on ``look``; "we may repair elevator B by 2026-09-10" fails on
``may``; "we will repair elevator B by 2026-09-10" passes. Check 5 independently rejects the
first two for having no ISO date.

The list is closed and language is not. A promise phrased to avoid every token -- "the
technician attends 2026-09-10" -- passes as unconditional. That is accepted for the reason
ADR-021 accepts its own conservatism: the list can only cause a valid commitment to be dropped
or an unhedged sentence to be accepted, and an accepted commitment still requires a human to
say it was kept.
"""

_WORD = re.compile(r"[a-z0-9']+")

_DATE_PREFIX_CONTINUATION = re.compile(r"[0-9]")
_DATE_SUFFIX_CONTINUATION = re.compile(r"(?i)^[0-9tz:+-]")
"""What may **not** sit against a cited date span, on either side.

``ISO_DATE`` fullmatches the cited *span text* alone, so a model that cites only the
``YYYY-MM-DD`` prefix of ``2030-01-14T10:00:00Z`` would otherwise pass check 5 having quoted a
bare date out of a timestamp the reply never made -- the grammar permits only the bare form
(ADR-027 § 4), and a span that is merely a *prefix* of a longer construct is not a citation of
that form. A leading digit is rejected the same way, so a span landing mid-year in a longer digit
run is caught symmetrically.

``.`` is deliberately **not** in the suffix set. It is the one character that legitimately sits
immediately after a valid citation -- the sentence's own terminator -- and a fractional-seconds
period never sits directly against the date itself; it follows ``T`` and a clock time first.
Rejecting on it would refuse the accepted, unhedged, correctly-cited worked example.
"""


class CommitmentRejection(StrEnum):
    """Why one proposed commitment was dropped. Closed codes; never the offending text."""

    SPAN_OUT_OF_RANGE = "SPAN_OUT_OF_RANGE"
    COMMITMENT_TEXT_UNSAFE = "COMMITMENT_TEXT_UNSAFE"
    COMMITMENT_UNGROUNDED = "COMMITMENT_UNGROUNDED"
    COMMITMENT_CLAUSE_MISMATCH = "COMMITMENT_CLAUSE_MISMATCH"
    COMMITMENT_OBLIGOR_MISMATCH = "COMMITMENT_OBLIGOR_MISMATCH"
    COMMITMENT_DUE_NOT_ISO = "COMMITMENT_DUE_NOT_ISO"
    COMMITMENT_NOT_UNCONDITIONAL = "COMMITMENT_NOT_UNCONDITIONAL"
    COMMITMENT_DUE_OUT_OF_RANGE = "COMMITMENT_DUE_OUT_OF_RANGE"
    COMMITMENT_ALREADY_ACTIVE = "COMMITMENT_ALREADY_ACTIVE"


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedCommitment:
    """One proposal that passed every check, with the values deterministic code derived.

    ``obligor`` and ``action_text`` are the normalized restatements; ``due_at`` is the derived
    instant and never the model's; ``verification_method`` is the constant.
    """

    obligor: str
    action_text: str
    due_at: datetime
    verification_method: str = VERIFICATION_METHOD


@dataclass(frozen=True, slots=True, kw_only=True)
class RejectedCommitment:
    """One proposal that failed, named by its position and its first failing check."""

    index: int
    rejection: CommitmentRejection


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitmentValidationOutcome:
    """What one extraction produced: at most one acceptance, and every rejection code.

    At most one, because check 9 caps a case's live commitments at one per action. The first
    proposal that passes every check wins and the rest are recorded as
    ``COMMITMENT_ALREADY_ACTIVE`` -- which is the same code a second reply would earn, because
    it is the same rule.
    """

    accepted: ValidatedCommitment | None
    rejections: tuple[RejectedCommitment, ...]

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """The distinct rejection codes, in first-seen order, for one audit event."""

        return tuple(dict.fromkeys(item.rejection.value for item in self.rejections))


def derive_due_at(iso_date: str) -> datetime:
    """The cited ISO date at ``T23:59:59.999999Z``. Raises on anything that is not one."""

    parsed = date.fromisoformat(iso_date)
    hour, minute, second, microsecond = DUE_TIME_OF_DAY
    return datetime(
        parsed.year, parsed.month, parsed.day, hour, minute, second, microsecond, tzinfo=UTC
    )


def sentence_bounds(text: str, span: SourceSpan) -> tuple[int, int]:
    """The ``[start, end)`` of the extent ``sentence_containing`` describes, as offsets.

    Kept apart from :func:`sentence_containing` because the clause-binding check (§ 3a) has to
    *compare* two extents rather than read one, and comparing substrings would accept two
    identical sentences appearing twice in one reply as "the same clause".
    """

    start = 0
    for index in range(min(span.start, len(text)) - 1, -1, -1):
        if text[index] in SENTENCE_TERMINATORS:
            start = index + 1
            break
    end = len(text)
    for index in range(min(span.end, len(text)), len(text)):
        if text[index] in SENTENCE_TERMINATORS:
            end = index
            break
    return start, end


def sentence_containing(text: str, span: SourceSpan) -> str:
    """The extent bounded by the nearest ``.``, ``!``, ``?``, or text boundary on each side.

    A newline is not a terminator, because the stored text joins normalized lines and a
    sentence that wrapped in the correspondent's mail client did not become two.
    """

    start, end = sentence_bounds(text, span)
    return text[start:end]


def _hedged(sentence: str) -> bool:
    """Whether the sentence carries a frozen conditional or refusal token."""

    return any(word in CONDITIONAL_TOKENS for word in _WORD.findall(normalize(sentence)))


def _span_within(span: SourceSpan, text: str) -> bool:
    return 0 <= span.start < span.end <= len(text) and span.end - span.start <= MAX_SPAN_LENGTH


def validate_proposal(
    proposal: ProposedCommitmentDraft,
    *,
    extracted_text: str,
    destination_label: str,
    received_at: datetime,
) -> ValidatedCommitment | CommitmentRejection:
    """Run checks 1 through 8 over one proposal, in order, and stop at the first failure.

    Check 9 -- at most one live commitment per action -- is not here: it is a question about
    durable state rather than about this proposal, and it is answered by
    :func:`validate_extraction` against a strongly read row.
    """

    # 1. Span validity. Every span lies inside the stored text and is at most 200 characters.
    spans = (proposal.obligor_span, proposal.action_span, proposal.due_date_span)
    if not all(_span_within(span, extracted_text) for span in spans):
        return CommitmentRejection.SPAN_OUT_OF_RANGE

    obligor = normalize(proposal.obligor)
    action_text = normalize(proposal.action_text)

    # 2. Structural safety, on the ADR-021 § 5 scanner, unchanged.
    if structural_rejections(proposal.obligor) or structural_rejections(proposal.action_text):
        return CommitmentRejection.COMMITMENT_TEXT_UNSAFE

    # 3a. Clause binding. The cited action and the cited date must belong to the same promise
    #     sentence, or nothing here says the date was ever *about* the action: a reply with an
    #     unrelated ISO date in a neighbouring sentence -- or an action span that names the
    #     wrong sentence entirely -- would otherwise let check 3 ground against reply-wide text
    #     and check 6 read the wrong sentence's hedging. Binding both spans to one extent before
    #     either check runs is what makes them checks about the *same promise* (ADR-027 § 3).
    action_clause = sentence_bounds(extracted_text, proposal.action_span)
    if action_clause != sentence_bounds(extracted_text, proposal.due_date_span):
        return CommitmentRejection.COMMITMENT_CLAUSE_MISMATCH
    clause_start, clause_end = action_clause
    clause_text = extracted_text[clause_start:clause_end]

    # 3. Lexical grounding against exactly the promise clause, never the reply at large. Every
    #    risk token by match equality, every proper-name candidate by normalized substring. This
    #    is the direct answer to a model inventing a commitment, or citing one sentence's words
    #    to ground a restatement of a different sentence's promise.
    grounded = ground_field(
        proposal.action_text,
        token_support=(clause_text,),
        name_support=(clause_text,),
    )
    if not grounded.accepted:
        return CommitmentRejection.COMMITMENT_UNGROUNDED

    # 4. The obligor is asserted by the correlation, never extracted. The model's value is only
    #    ever checked for agreement with a fact the attester already established.
    if obligor != normalize(destination_label):
        return CommitmentRejection.COMMITMENT_OBLIGOR_MISMATCH

    # 5. The due date has exactly one permitted form, and the cited span must be the *whole* of
    #    it: a span that is merely a bounded prefix of a longer timestamp -- ``2030-01-14`` cited
    #    out of ``2030-01-14T10:00:00Z`` -- is not a citation of the one permitted grammar.
    due_start, due_end = proposal.due_date_span.start, proposal.due_date_span.end
    if due_start > 0 and _DATE_PREFIX_CONTINUATION.match(extracted_text[due_start - 1]):
        return CommitmentRejection.COMMITMENT_DUE_NOT_ISO
    if due_end < len(extracted_text) and _DATE_SUFFIX_CONTINUATION.match(extracted_text[due_end]):
        return CommitmentRejection.COMMITMENT_DUE_NOT_ISO
    cited = normalize(extracted_text[due_start:due_end])
    if not ISO_DATE.fullmatch(cited):
        return CommitmentRejection.COMMITMENT_DUE_NOT_ISO
    if any(pattern.search(cited) for pattern in REJECTED_DATE_CONSTRUCTS):
        return CommitmentRejection.COMMITMENT_DUE_NOT_ISO
    try:
        due_at = derive_due_at(cited)
    except ValueError:
        return CommitmentRejection.COMMITMENT_DUE_NOT_ISO

    # 6. Unconditionality, over the sentence containing the action span -- which check 3a has
    #    already proven is the same sentence the cited date lives in.
    if _hedged(sentence_containing(extracted_text, proposal.action_span)):
        return CommitmentRejection.COMMITMENT_NOT_UNCONDITIONAL

    # 7. The verification method is the V1 constant. Nothing to check: the contract has no field.

    # 8. Range.
    if not received_at + MIN_DUE_HORIZON <= due_at <= received_at + MAX_DUE_HORIZON:
        return CommitmentRejection.COMMITMENT_DUE_OUT_OF_RANGE

    return ValidatedCommitment(obligor=obligor, action_text=action_text, due_at=due_at)


def validate_extraction(
    proposals: tuple[ProposedCommitmentDraft, ...],
    *,
    extracted_text: str,
    destination_label: str,
    received_at: datetime,
    action_has_live_commitment: bool,
) -> CommitmentValidationOutcome:
    """Validate every proposal independently and return at most one acceptance.

    ``action_has_live_commitment`` is check 9, answered by the caller from a strongly read row
    rather than inferred here: whether another ``PENDING`` or ``DUE`` commitment exists is a
    fact about durable state, and a validator that guessed at it would be a second authority on
    a question the transaction's own conditions already settle.
    """

    rejections: list[RejectedCommitment] = []
    accepted: ValidatedCommitment | None = None
    for index, proposal in enumerate(proposals):
        if accepted is not None or action_has_live_commitment:
            rejections.append(
                RejectedCommitment(
                    index=index, rejection=CommitmentRejection.COMMITMENT_ALREADY_ACTIVE
                )
            )
            continue
        outcome = validate_proposal(
            proposal,
            extracted_text=extracted_text,
            destination_label=destination_label,
            received_at=received_at,
        )
        if isinstance(outcome, CommitmentRejection):
            rejections.append(RejectedCommitment(index=index, rejection=outcome))
            continue
        accepted = outcome
    return CommitmentValidationOutcome(accepted=accepted, rejections=tuple(rejections))


__all__ = [
    "CONDITIONAL_TOKENS",
    "ISO_DATE",
    "MAX_DUE_HORIZON",
    "MIN_DUE_HORIZON",
    "VERIFICATION_METHOD",
    "CommitmentRejection",
    "CommitmentValidationOutcome",
    "RejectedCommitment",
    "ValidatedCommitment",
    "derive_due_at",
    "sentence_bounds",
    "sentence_containing",
    "validate_extraction",
    "validate_proposal",
]
