"""The commitment-extraction contract: one reply in, spans out. Nothing else, in either direction.

The model's whole job is to **point at the words**. Every consequential value is derived by
deterministic code from something the model cannot influence: the obligor comes from the
correlation the attester already established, the deadline comes from a cited ISO date, the
identifier comes from the application, the status comes from a human
([ADR-027](../../../docs/adr/ADR-027-commitment-extraction-grounding-and-authority.md)).

Spans, not quoted strings
--------------------------
A quoted string can be fabricated; an offset pair either indexes the stored text or it does
not. The text the offsets index is the same value persisted on the ``EvidenceItem``, so the
citation stays checkable forever, by anyone, with no reconstruction.

``due_at`` is measured and never read
--------------------------------------
It exists so an evaluation can score how well the model reasons about dates, and so a wrong
belief is visible in the answer rather than invisible in the prompt -- the same role
``SufficiencyDraft.independent_source_count`` already plays. The authoritative deadline is
derived from ``due_date_span`` alone. A model therefore has **no field through which it can
invent a deadline**, and reading its value and requiring equality was considered and rejected:
the derived value is already authoritative, so the comparison could only add spurious
rejections while leaving a field on the record that looks authoritative and is not.

What this contract has no field for
------------------------------------
And therefore what the model structurally cannot do: set a status, resolve or transition a
case, name a destination, name a verification method, create an evidence status, or address
anything outside ``source_evidence_id``. The apply command holds no SES port, no compiler port,
no scheduler port at the moment it reads model output, and no case-resolution verb (T38).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from chorus.contracts.common import StrictModel, require_utc_datetime

COMMITMENT_EXTRACTION_INPUT_SCHEMA_VERSION: Final[Literal["commitment-extraction-input/v1"]] = (
    "commitment-extraction-input/v1"
)
COMMITMENT_EXTRACTION_SCHEMA_VERSION: Final[Literal["commitment-extraction/v1"]] = (
    "commitment-extraction/v1"
)

COMMITMENT_EXTRACTION_PROMPT_VERSION: Final = "commitment-extraction/v1"
"""The only extraction prompt identity this contract version accepts.

The operation runs on the **Investigator** runtime under its own prompt version, so a runtime
answering with ``investigator/v1`` is running the wrong reviewed artifact for this call and its
answer is refused once, by version, rather than trusted field by field.
"""

MAX_EXTRACTED_COMMITMENTS: Final = 3
"""The frozen per-reply bound. A reply proposing more than three promises is not a reply."""

MAX_SPAN_LENGTH: Final = 200
"""The longest a cited span may be. A citation that covers a paragraph cites nothing."""

MAX_REPLY_TEXT_LENGTH: Final = 8 * 1024
"""Matches the stored ``extracted_text`` bound, so the offsets are always addressable."""

ObligorStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]
DestinationLabelStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]
"""The safe organization label of the correspondent, bounded exactly like the obligor it feeds.

Same bound as :data:`ObligorStr` on purpose: check 4 compares the two for normalized equality,
so a label the obligor field could not hold would be a check nothing could ever pass.
"""

ActionTextStr = Annotated[str, StringConstraints(min_length=1, max_length=500)]
ReplyTextStr = Annotated[str, StringConstraints(max_length=MAX_REPLY_TEXT_LENGTH)]
"""Deliberately admits the empty string.

A reply consisting only of our own quoted message has every line removed before extraction, and
the artifact of that delivery still exists. The model is then given nothing to cite, every span
falls outside ``[0, 0)``, and no commitment is possible -- which is the outcome, reached by the
span rule rather than by a special case (T37)."""


class SourceSpan(StrictModel):
    """Character offsets into the exact normalized ``extracted_text`` the model was given.

    Half-open, ``start`` inclusive and ``end`` exclusive, which is Python's own slice
    convention -- so the span the model cites and the substring deterministic code checks are
    produced by one indexing rule rather than two that agree most of the time.
    """

    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        if self.end <= self.start:
            raise ValueError("a span ends after it starts")
        if self.end - self.start > MAX_SPAN_LENGTH:
            raise ValueError("a cited span exceeds the frozen length bound")
        return self


class ProposedCommitmentDraft(StrictModel):
    """One proposed promise: three spans, two restatements, and two advisory values.

    Each proposal is validated and rejected **independently**. A failure drops that proposal and
    does not discard its siblings, which differs from the whole-proposal rejection an Action
    draft gets -- and the difference is the consequence: an invalid Action proposal would become
    an external message, while an invalid commitment costs nothing to drop, and dropping a valid
    sibling with it costs a real follow-up (ADR-027 § 3).
    """

    obligor_span: SourceSpan
    action_span: SourceSpan
    due_date_span: SourceSpan
    obligor: ObligorStr
    """A normalized restatement, compared for **equality** with the correlated destination's
    safe label. It is never the source of the obligor: the model's value is only ever checked
    for agreement with a fact the correlation already established (SEC-23)."""
    action_text: ActionTextStr
    due_at: datetime
    """**Advisory. Measured and never read for authority.**"""
    refusal_detected: bool
    """**Advisory.** The unconditionality check is the frozen token list, not this flag."""

    @model_validator(mode="after")
    def validate_draft(self) -> Self:
        require_utc_datetime(self.due_at)
        return self


class CommitmentExtractionInput(StrictModel):
    """The complete extraction payload: two identifiers, one safe label, one reply's text.

    Not the case, not other evidence, not facts, not mandates, not contributor data. The model
    that reads a stranger's email is given nothing else to leak (ADR-027 § 1).

    ``reply_text`` is delimited as untrusted **data**. A reply written as an instruction to a
    system is still a record of what somebody wrote.

    Why the destination label is here
    ---------------------------------
    Check 4 requires ``normalize(obligor)`` to equal the normalized safe ``display_label`` of the
    correlated destination -- a value the correlation established and the reply never states. The
    frozen demo reply says "We will restore elevator B to service by 2030-01-14."; it does not
    contain "Property Management". A model given only the reply therefore **cannot** satisfy
    check 4 except by accident, which would make live extraction fail on correct answers.

    So the already-safe label is supplied as input. It is non-secret by design (§ 13 of the Phase
    11 deployment contract classes it a safe environment variable beside the registry version and
    the routing token), it names no mailbox, and it carries no address, token, or registry
    record. **Supplying it does not make the model authoritative**: check 4 still compares the
    model's restatement against the value deterministic code holds, so the model can only agree
    with a fact already established or be rejected. What changes is that agreeing is now
    possible.
    """

    schema_version: Literal["commitment-extraction-input/v1"] = (
        COMMITMENT_EXTRACTION_INPUT_SCHEMA_VERSION
    )
    case_id: UUID
    source_evidence_id: UUID
    destination_display_label: DestinationLabelStr
    """The safe organization label the extraction's ``obligor`` must restate.

    Required and non-empty, so a deployment that has not configured one fails at payload
    construction -- before a model is invoked -- rather than producing a candidate that check 4
    would reject for a reason nobody could act on.
    """
    reply_text: ReplyTextStr


class CommitmentExtractionOutput(StrictModel):
    """The whole of what the extraction may return.

    What is absent is the design. There is no status, no case state, no destination, no
    verification method, no evidence status, no schedule, and no second evidence identifier.
    """

    schema_version: Literal["commitment-extraction/v1"] = COMMITMENT_EXTRACTION_SCHEMA_VERSION
    case_id: UUID
    source_evidence_id: UUID
    commitments: Annotated[
        tuple[ProposedCommitmentDraft, ...], Field(max_length=MAX_EXTRACTED_COMMITMENTS)
    ] = ()


__all__ = [
    "COMMITMENT_EXTRACTION_INPUT_SCHEMA_VERSION",
    "COMMITMENT_EXTRACTION_PROMPT_VERSION",
    "COMMITMENT_EXTRACTION_SCHEMA_VERSION",
    "MAX_EXTRACTED_COMMITMENTS",
    "MAX_REPLY_TEXT_LENGTH",
    "MAX_SPAN_LENGTH",
    "CommitmentExtractionInput",
    "CommitmentExtractionOutput",
    "DestinationLabelStr",
    "ProposedCommitmentDraft",
    "SourceSpan",
]
