"""What the extraction contract has no field for, and therefore what a model cannot do.

The absences are the design. There is no status, no case-state, no destination, no verification
method, no evidence status, no schedule, and no second evidence identifier -- so a model that
tried to assert one fails ``extra=forbid`` rather than reaching a validator that has to decline
it (T38).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from chorus.contracts.commitment import (
    COMMITMENT_EXTRACTION_PROMPT_VERSION,
    MAX_EXTRACTED_COMMITMENTS,
    MAX_SPAN_LENGTH,
    CommitmentExtractionInput,
    CommitmentExtractionOutput,
    ProposedCommitmentDraft,
    SourceSpan,
)


def draft(**overrides: object) -> ProposedCommitmentDraft:
    fields: dict[str, object] = {
        "obligor_span": SourceSpan(start=0, end=10),
        "action_span": SourceSpan(start=0, end=40),
        "due_date_span": SourceSpan(start=30, end=40),
        "obligor": "Property Management",
        "action_text": "Restore elevator B by 2030-01-14",
        "due_at": datetime(2030, 1, 14, tzinfo=UTC),
        "refusal_detected": False,
    }
    fields.update(overrides)
    return ProposedCommitmentDraft(**fields)  # type: ignore[arg-type]


def test_a_span_is_half_open_and_bounded() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(start=10, end=10)
    with pytest.raises(ValidationError):
        SourceSpan(start=0, end=MAX_SPAN_LENGTH + 1)

    assert SourceSpan(start=0, end=MAX_SPAN_LENGTH).end == MAX_SPAN_LENGTH


def test_a_negative_offset_is_refused() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(start=-1, end=4)


@pytest.mark.parametrize(
    "field",
    [
        "status",
        "case_state",
        "destination_id",
        "verification_method",
        "evidence_status",
        "scheduler_name",
        "resolved",
    ],
)
def test_the_model_has_no_field_to_assert_anything_consequential(field: str) -> None:
    with pytest.raises(ValidationError):
        draft(**{field: "anything"})


def test_the_output_admits_at_most_three_proposals() -> None:
    proposals = tuple(draft() for _ in range(MAX_EXTRACTED_COMMITMENTS + 1))

    with pytest.raises(ValidationError):
        CommitmentExtractionOutput(
            case_id=uuid4(), source_evidence_id=uuid4(), commitments=proposals
        )


def test_the_output_may_propose_nothing() -> None:
    """A reply with no promise in it is answered, not failed."""

    output = CommitmentExtractionOutput(case_id=uuid4(), source_evidence_id=uuid4())

    assert output.commitments == ()


def test_the_output_names_exactly_one_source_evidence_id() -> None:
    """It cannot address anything outside the artifact it was given."""

    assert set(CommitmentExtractionOutput.model_fields) == {
        "schema_version",
        "case_id",
        "source_evidence_id",
        "commitments",
    }


def test_the_input_carries_the_reply_text_and_nothing_else_about_the_case() -> None:
    """Not the case, not other evidence, not facts, not mandates, not contributor data."""

    assert set(CommitmentExtractionInput.model_fields) == {
        "schema_version",
        "case_id",
        "source_evidence_id",
        "reply_text",
    }


def test_the_input_admits_an_empty_reply_text() -> None:
    """A reply that was only our own quoted message leaves nothing, and still has an artifact."""

    payload = CommitmentExtractionInput(case_id=uuid4(), source_evidence_id=uuid4(), reply_text="")

    assert payload.reply_text == ""


def test_a_naive_due_at_is_refused() -> None:
    with pytest.raises(ValidationError):
        draft(due_at=datetime(2030, 1, 14))


def test_the_prompt_version_is_its_own_and_not_the_investigators() -> None:
    """It runs on the Investigator runtime under its **own** reviewed artifact (ADR-027 § 1)."""

    from chorus.contracts.common import INVESTIGATOR_PROMPT_VERSION

    assert COMMITMENT_EXTRACTION_PROMPT_VERSION == "commitment-extraction/v1"
    assert str(INVESTIGATOR_PROMPT_VERSION) != str(COMMITMENT_EXTRACTION_PROMPT_VERSION)
