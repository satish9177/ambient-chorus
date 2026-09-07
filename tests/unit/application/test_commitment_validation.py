"""The nine deterministic checks, and the one deadline they derive.

Everything here is a pure function over strings and instants, so these are the tests that can
say precisely which rule fired -- the contract suite proves the rules are reached, and this one
proves each of them means what ADR-027 § 3 and § 4 say.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from chorus.application.services.commitment_validation import (
    CONDITIONAL_TOKENS,
    MAX_DUE_HORIZON,
    VERIFICATION_METHOD,
    CommitmentRejection,
    ValidatedCommitment,
    derive_due_at,
    sentence_containing,
    validate_extraction,
    validate_proposal,
)
from chorus.contracts.commitment import (
    MAX_EXTRACTED_COMMITMENTS,
    ProposedCommitmentDraft,
    SourceSpan,
)

LABEL = "Property Management"
REPLY = (
    "thank you for the report.\nwe will restore elevator b to service by 2030-01-14.\n"
    "please contact the office."
)
RECEIVED = datetime(2030, 1, 2, 9, 0, tzinfo=UTC)


def draft(**overrides: object) -> ProposedCommitmentDraft:
    date_start = REPLY.index("2030-01-14")
    action_start = REPLY.index("we will restore")
    fields: dict[str, object] = {
        "obligor_span": SourceSpan(start=action_start, end=action_start + 10),
        "action_span": SourceSpan(start=action_start, end=date_start + 10),
        "due_date_span": SourceSpan(start=date_start, end=date_start + 10),
        "obligor": LABEL,
        "action_text": "Restore elevator B to service by 2030-01-14",
        "due_at": datetime(2030, 1, 14, tzinfo=UTC),
        "refusal_detected": False,
    }
    fields.update(overrides)
    return ProposedCommitmentDraft(**fields)  # type: ignore[arg-type]


def check(**overrides: object) -> ValidatedCommitment | CommitmentRejection:
    return validate_proposal(
        draft(**overrides),
        extracted_text=REPLY,
        destination_label=LABEL,
        received_at=RECEIVED,
    )


def test_a_grounded_unconditional_dated_promise_passes() -> None:
    outcome = check()

    assert isinstance(outcome, ValidatedCommitment)
    assert outcome.verification_method == VERIFICATION_METHOD
    assert outcome.obligor == "property management"


def test_the_deadline_is_end_of_day_utc_and_never_the_model_value() -> None:
    """A promise "by 2030-01-14" is kept at any hour of that day (ADR-027 § 4)."""

    outcome = check(due_at=datetime(2030, 1, 1, tzinfo=UTC))

    assert isinstance(outcome, ValidatedCommitment)
    assert outcome.due_at == datetime(2030, 1, 14, 23, 59, 59, 999_999, tzinfo=UTC)
    assert derive_due_at("2030-01-14") == outcome.due_at


@pytest.mark.parametrize(
    "cited",
    ["within 3 days", "next week", "tomorrow", "Wednesday", "14 January 2030", "01/14/2030"],
)
def test_every_other_date_construct_is_rejected(cited: str) -> None:
    """There is no timezone to convert and no relative expression to resolve."""

    text = f"we will restore elevator b {cited}."
    start = text.index(cited)
    outcome = validate_proposal(
        ProposedCommitmentDraft(
            obligor_span=SourceSpan(start=0, end=2),
            action_span=SourceSpan(start=0, end=len(text) - 1),
            due_date_span=SourceSpan(start=start, end=start + len(cited)),
            obligor=LABEL,
            action_text="Restore elevator B",
            due_at=datetime(2030, 1, 14, tzinfo=UTC),
            refusal_detected=False,
        ),
        extracted_text=text,
        destination_label=LABEL,
        received_at=RECEIVED,
    )

    assert outcome is CommitmentRejection.COMMITMENT_DUE_NOT_ISO


def test_an_impossible_calendar_date_is_rejected() -> None:
    """``date.fromisoformat`` on the exact substring, so 2030-02-30 is not an ISO-date span.

    The restatement deliberately omits the date. ADR-021's telephone rule exempts only spans
    that are *validated* calendar dates, so an action text carrying ``2030-02-30`` is refused
    one check earlier as structurally unsafe -- correct, and not the rule under test here.
    """

    text = "we will restore elevator b by 2030-02-30."
    start = text.index("2030-02-30")
    outcome = validate_proposal(
        ProposedCommitmentDraft(
            obligor_span=SourceSpan(start=0, end=2),
            action_span=SourceSpan(start=0, end=len(text) - 1),
            due_date_span=SourceSpan(start=start, end=start + 10),
            obligor=LABEL,
            action_text="Restore elevator B",
            due_at=datetime(2030, 2, 28, tzinfo=UTC),
            refusal_detected=False,
        ),
        extracted_text=text,
        destination_label=LABEL,
        received_at=RECEIVED,
    )

    assert outcome is CommitmentRejection.COMMITMENT_DUE_NOT_ISO


def test_a_past_date_fails_the_lower_bound_rather_than_a_rule_of_its_own() -> None:
    outcome = check()
    assert isinstance(outcome, ValidatedCommitment)

    late = validate_proposal(
        draft(),
        extracted_text=REPLY,
        destination_label=LABEL,
        received_at=datetime(2031, 1, 1, tzinfo=UTC),
    )
    assert late is CommitmentRejection.COMMITMENT_DUE_OUT_OF_RANGE


def test_a_date_beyond_thirty_days_is_out_of_range() -> None:
    early = RECEIVED - MAX_DUE_HORIZON - timedelta(days=1)
    outcome = validate_proposal(
        draft(), extracted_text=REPLY, destination_label=LABEL, received_at=early
    )

    assert outcome is CommitmentRejection.COMMITMENT_DUE_OUT_OF_RANGE


@pytest.mark.parametrize(
    "span", [SourceSpan(start=900, end=910), SourceSpan(start=len(REPLY) - 2, end=len(REPLY) + 8)]
)
def test_a_span_outside_the_stored_text_is_rejected(span: SourceSpan) -> None:
    """Wholly past the end, and straddling it. The contract bounds the *length* of a span; only
    the validator knows how long the stored text actually is."""

    assert check(due_date_span=span) is CommitmentRejection.SPAN_OUT_OF_RANGE


def test_the_obligor_is_compared_with_the_correlated_label_and_never_extracted() -> None:
    assert check(obligor="Acme Elevators") is CommitmentRejection.COMMITMENT_OBLIGOR_MISMATCH


def test_an_action_text_naming_a_number_the_reply_never_stated_is_ungrounded() -> None:
    """Match equality, never substring: a supported number is one the reply actually stated."""

    outcome = check(action_text="Restore elevator B to service by 2030-01-15")

    assert outcome is CommitmentRejection.COMMITMENT_UNGROUNDED


def test_an_action_text_carrying_a_url_is_structurally_unsafe() -> None:
    outcome = check(action_text="Restore elevator B https://example.invalid by 2030-01-14")

    assert outcome is CommitmentRejection.COMMITMENT_TEXT_UNSAFE


@pytest.mark.parametrize(
    "sentence",
    [
        "we may restore elevator b to service by 2030-01-14",
        "we will look into elevator b by 2030-01-14",
        "we will not restore elevator b by 2030-01-14",
        "subject to approval we restore elevator b by 2030-01-14",
    ],
)
def test_a_hedged_sentence_is_not_a_commitment(sentence: str) -> None:
    text = f"thank you.\n{sentence}."
    start = text.index(sentence)
    date_start = text.index("2030-01-14")
    outcome = validate_proposal(
        ProposedCommitmentDraft(
            obligor_span=SourceSpan(start=start, end=start + 5),
            action_span=SourceSpan(start=start, end=date_start + 10),
            due_date_span=SourceSpan(start=date_start, end=date_start + 10),
            obligor=LABEL,
            action_text="Restore elevator B by 2030-01-14",
            due_at=datetime(2030, 1, 14, tzinfo=UTC),
            refusal_detected=False,
        ),
        extracted_text=text,
        destination_label=LABEL,
        received_at=RECEIVED,
    )

    assert outcome is CommitmentRejection.COMMITMENT_NOT_UNCONDITIONAL


def test_the_conditional_list_holds_the_words_that_stand_alone() -> None:
    """``will not`` is covered by ``not`` and ``subject to`` by ``subject``.

    Adding ``will`` and ``to`` as standalone tokens would refuse ordinary English and the ADR's
    own worked example, so the list holds the distinctive half of each phrase.
    """

    assert "not" in CONDITIONAL_TOKENS
    assert "subject" in CONDITIONAL_TOKENS
    assert "will" not in CONDITIONAL_TOKENS
    assert "to" not in CONDITIONAL_TOKENS


def test_the_sentence_extent_stops_at_terminators_and_not_at_newlines() -> None:
    text = "one thing happened. a second\nthing happened too. a third."
    span = SourceSpan(start=text.index("a second"), end=text.index("too") + 3)

    assert sentence_containing(text, span).strip() == "a second\nthing happened too"


def test_a_span_on_an_unrelated_sentence_cannot_ground_a_refused_promise() -> None:
    """Astra P1-1.A: ``action_span="thank"`` cannot borrow a later sentence's date.

    ``thank`` and the cited date sit in different sentences, so clause binding refuses the
    proposal before grounding or hedging ever run -- and in particular before the refusal in the
    *actual* promise sentence could be missed because the action span pointed elsewhere.
    """

    text = "thank you for the report. we will not restore elevator b by 2030-01-14."
    start = text.index("thank")
    date_start = text.index("2030-01-14")
    outcome = validate_proposal(
        ProposedCommitmentDraft(
            obligor_span=SourceSpan(start=start, end=start + 5),
            action_span=SourceSpan(start=start, end=start + 5),
            due_date_span=SourceSpan(start=date_start, end=date_start + 10),
            obligor=LABEL,
            action_text="Restore elevator B",
            due_at=datetime(2030, 1, 14, tzinfo=UTC),
            refusal_detected=False,
        ),
        extracted_text=text,
        destination_label=LABEL,
        received_at=RECEIVED,
    )

    assert outcome is CommitmentRejection.COMMITMENT_CLAUSE_MISMATCH


def test_a_dateless_promise_cannot_borrow_a_date_from_another_sentence() -> None:
    """Astra P1-1.B: the promise sentence has no date; a later sentence does."""

    text = "we will restore elevator b to service. the office reopens on 2030-01-14."
    action_start = text.index("we will restore")
    action_end = text.index(".", action_start)
    date_start = text.index("2030-01-14")
    outcome = validate_proposal(
        ProposedCommitmentDraft(
            obligor_span=SourceSpan(start=action_start, end=action_start + 10),
            action_span=SourceSpan(start=action_start, end=action_end),
            due_date_span=SourceSpan(start=date_start, end=date_start + 10),
            obligor=LABEL,
            action_text="Restore elevator B to service",
            due_at=datetime(2030, 1, 14, tzinfo=UTC),
            refusal_detected=False,
        ),
        extracted_text=text,
        destination_label=LABEL,
        received_at=RECEIVED,
    )

    assert outcome is CommitmentRejection.COMMITMENT_CLAUSE_MISMATCH


def test_an_action_span_naming_the_wrong_sentence_is_rejected() -> None:
    """Astra P1-1.C: the cited action span sits in a sentence that never made the promise."""

    text = (
        "please contact the office with any questions. "
        "we will restore elevator b to service by 2030-01-14."
    )
    wrong_start = text.index("please contact")
    wrong_end = text.index(".", wrong_start)
    date_start = text.index("2030-01-14")
    outcome = validate_proposal(
        ProposedCommitmentDraft(
            obligor_span=SourceSpan(start=wrong_start, end=wrong_start + 6),
            action_span=SourceSpan(start=wrong_start, end=wrong_end),
            due_date_span=SourceSpan(start=date_start, end=date_start + 10),
            obligor=LABEL,
            action_text="Restore elevator B to service",
            due_at=datetime(2030, 1, 14, tzinfo=UTC),
            refusal_detected=False,
        ),
        extracted_text=text,
        destination_label=LABEL,
        received_at=RECEIVED,
    )

    assert outcome is CommitmentRejection.COMMITMENT_CLAUSE_MISMATCH


def test_a_span_citing_only_the_date_prefix_of_a_timestamp_is_rejected() -> None:
    """Astra P1-1.D: the frozen grammar admits only the bare ``YYYY-MM-DD`` form."""

    text = "we will restore elevator b by 2030-01-14T10:00:00Z."
    action_start = text.index("we will restore")
    date_start = text.index("2030-01-14")
    outcome = validate_proposal(
        ProposedCommitmentDraft(
            obligor_span=SourceSpan(start=action_start, end=action_start + 10),
            action_span=SourceSpan(start=action_start, end=date_start + 10),
            due_date_span=SourceSpan(start=date_start, end=date_start + 10),
            obligor=LABEL,
            action_text="Restore elevator B",
            due_at=datetime(2030, 1, 14, tzinfo=UTC),
            refusal_detected=False,
        ),
        extracted_text=text,
        destination_label=LABEL,
        received_at=RECEIVED,
    )

    assert outcome is CommitmentRejection.COMMITMENT_DUE_NOT_ISO


def test_the_exact_grounded_worked_example_still_accepts() -> None:
    """Astra P1-1.E: the accepted case is unaffected by the clause-binding repair."""

    outcome = check()

    assert isinstance(outcome, ValidatedCommitment)
    assert outcome.due_at == datetime(2030, 1, 14, 23, 59, 59, 999_999, tzinfo=UTC)


def test_one_failing_proposal_does_not_discard_a_valid_sibling() -> None:
    outcome = validate_extraction(
        (draft(due_date_span=SourceSpan(start=900, end=910)), draft()),
        extracted_text=REPLY,
        destination_label=LABEL,
        received_at=RECEIVED,
        action_has_live_commitment=False,
    )

    assert outcome.accepted is not None
    assert outcome.reason_codes == (CommitmentRejection.SPAN_OUT_OF_RANGE.value,)


def test_at_most_one_commitment_is_accepted_per_extraction() -> None:
    outcome = validate_extraction(
        (draft(), draft(), draft()),
        extracted_text=REPLY,
        destination_label=LABEL,
        received_at=RECEIVED,
        action_has_live_commitment=False,
    )

    assert outcome.accepted is not None
    assert outcome.reason_codes == (CommitmentRejection.COMMITMENT_ALREADY_ACTIVE.value,)
    assert len(outcome.rejections) == MAX_EXTRACTED_COMMITMENTS - 1


def test_a_live_commitment_refuses_every_proposal() -> None:
    outcome = validate_extraction(
        (draft(),),
        extracted_text=REPLY,
        destination_label=LABEL,
        received_at=RECEIVED,
        action_has_live_commitment=True,
    )

    assert outcome.accepted is None
    assert outcome.reason_codes == (CommitmentRejection.COMMITMENT_ALREADY_ACTIVE.value,)


def test_an_empty_reply_admits_no_span_and_therefore_no_commitment() -> None:
    """A reply that was only our own quoted message leaves nothing to cite."""

    outcome = validate_extraction(
        (draft(),),
        extracted_text="",
        destination_label=LABEL,
        received_at=RECEIVED,
        action_has_live_commitment=False,
    )

    assert outcome.accepted is None
    assert outcome.reason_codes == (CommitmentRejection.SPAN_OUT_OF_RANGE.value,)
