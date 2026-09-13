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
    """Not the case, not other evidence, not facts, not mandates, not contributor data.

    ``destination_display_label`` is the one addition, and it is the opposite of a leak: it is
    the safe organization label the deployment already publishes as a non-secret environment
    variable, and it exists here because check 4 compares the model's ``obligor`` with it. A
    model that was never shown it could satisfy that check only by accident. It names no
    mailbox, carries no address, and grants nothing -- deterministic validation still decides.
    """

    assert set(CommitmentExtractionInput.model_fields) == {
        "schema_version",
        "case_id",
        "source_evidence_id",
        "destination_display_label",
        "reply_text",
    }


def test_the_input_still_carries_no_private_or_secret_destination_field() -> None:
    """The safe label is admitted; the things it is adjacent to are not."""

    fields = set(CommitmentExtractionInput.model_fields)
    assert not fields & {
        "destination_address",
        "destination_address_digest",
        "destination_registry_secret_arn",
        "destination_routing_token",
        "from_identity_id",
        "reply_to",
        "correspondent_mailbox",
    }


def test_a_blank_destination_label_is_refused_before_a_model_is_reached() -> None:
    """A deployment with no configured correspondent fails at payload construction.

    Not at check 4, and not after a model pass over a stranger's email: the field is bounded
    ``1..120`` so the invocation cannot be built at all.
    """

    with pytest.raises(ValidationError):
        CommitmentExtractionInput(
            case_id=uuid4(),
            source_evidence_id=uuid4(),
            destination_display_label="",
            reply_text="We will repair elevator B by 2030-09-10.",
        )


def test_the_input_admits_an_empty_reply_text() -> None:
    """A reply that was only our own quoted message leaves nothing, and still has an artifact."""

    payload = CommitmentExtractionInput(
        case_id=uuid4(),
        source_evidence_id=uuid4(),
        destination_display_label="Property Management",
        reply_text="",
    )

    assert payload.reply_text == ""


def test_a_naive_due_at_is_refused() -> None:
    with pytest.raises(ValidationError):
        draft(due_at=datetime(2030, 1, 14))


def test_the_prompt_version_is_its_own_and_not_the_investigators() -> None:
    """It runs on the Investigator runtime under its **own** reviewed artifact (ADR-027 § 1)."""

    from chorus.contracts.common import INVESTIGATOR_PROMPT_VERSION

    assert COMMITMENT_EXTRACTION_PROMPT_VERSION == "commitment-extraction/v1"
    assert str(INVESTIGATOR_PROMPT_VERSION) != str(COMMITMENT_EXTRACTION_PROMPT_VERSION)
