"""Local extraction adapters: one scripted, one deterministic. Neither is ever the demo path.

Both implement :class:`~chorus.ports.agents.CommitmentExtractionPort`, so the application code
under test is byte-for-byte the code that runs against Bedrock. What changes is only who
answers.

``ScriptedCommitmentExtractor`` answers with whatever a test hands it, including answers no
honest model would produce -- a span outside the text, a fabricated obligor, a ``due_at`` that
disagrees with the date it cited, a promise the reply never made. That is how the adversarial
suite exercises the nine checks without needing a model that can be persuaded to overreach on
demand.

``LiteralSpanCommitmentExtractor`` is a deterministic stand-in for local development. It is a
*fake model*, not a fallback extractor: it finds the first ISO date in the reply and cites the
sentence around it, and it performs none of the judgement the real prompt asks for. Its whole
value is that the deterministic checks still have to accept or reject what it produces, so a
local run exercises the validator rather than a shortcut around it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Final

from chorus.contracts.commitment import (
    COMMITMENT_EXTRACTION_PROMPT_VERSION,
    CommitmentExtractionOutput,
    ProposedCommitmentDraft,
    SourceSpan,
)
from chorus.contracts.common import (
    AGENT_OUTPUT_SCHEMA_VERSION,
    AgentName,
    AgentResultEnvelope,
)
from chorus.ports.agents import (
    AgentError,
    CommitmentExtractionInvocation,
    CommitmentExtractionResult,
)

FAKE_MODEL_PROFILE_HASH: Final = f"sha256:{sha256(b'fake-commitment-extractor').hexdigest()}"

_ISO_DATE = re.compile(r"(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])")
_SENTENCE_BOUND = frozenset({".", "!", "?"})


def _envelope(
    invocation: CommitmentExtractionInvocation,
    output: CommitmentExtractionOutput,
    *,
    prompt_version: str,
) -> CommitmentExtractionResult:
    started = datetime.now(UTC)
    return AgentResultEnvelope[CommitmentExtractionOutput](
        schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
        invocation_id=invocation.invocation_id,
        namespace=invocation.namespace,
        agent_name=AgentName.INVESTIGATOR,
        case_id=invocation.case_id,
        case_version=invocation.case_version,
        model_profile_arn_hash=FAKE_MODEL_PROFILE_HASH,
        prompt_version=prompt_version,
        started_at=started,
        completed_at=started,
        output=output,
    )


@dataclass(slots=True)
class ScriptedCommitmentExtractor:
    """Answer with an exact, test-supplied extraction or failure.

    ``responder`` receives the invocation so a test can assert what the application actually
    projected -- which is how "the payload contained only the reply text" becomes a test rather
    than a claim.
    """

    responder: Callable[[CommitmentExtractionInvocation], CommitmentExtractionOutput]
    failures: list[AgentError] = field(default_factory=list)
    invocations: list[CommitmentExtractionInvocation] = field(default_factory=list)
    prompt_version: str = COMMITMENT_EXTRACTION_PROMPT_VERSION

    async def invoke_commitment_extraction(
        self, invocation: CommitmentExtractionInvocation
    ) -> CommitmentExtractionResult:
        self.invocations.append(invocation)
        if self.failures:
            raise self.failures.pop(0)
        return _envelope(invocation, self.responder(invocation), prompt_version=self.prompt_version)

    @property
    def call_count(self) -> int:
        """How many model passes were made over a stranger's email. Frozen target: **one**."""

        return len(self.invocations)


@dataclass(slots=True)
class LiteralSpanCommitmentExtractor:
    """Cite the first ISO date in the reply and the sentence containing it, or propose nothing.

    Deliberately literal. It restates the obligor as the safe label **the payload carries**
    rather than reading one out of the reply, which is not cheating: check 4 compares the model's
    value with that label anyway, so a stand-in that guessed would only ever be wrong. And it
    restates the action as the cited sentence, which the grounding check will accept precisely
    because every token in it came from the reply.

    The label used to arrive out-of-band, on this dataclass. It now comes from
    ``CommitmentExtractionInput`` -- the same field the live prompt renders -- so this stand-in
    is answering the same question a deployed model is asked, from the same input, rather than
    from a value only a local fake could see.

    What it cannot do is more interesting than what it does: it has no field for a status, a
    case state, a destination, or a verification method, so there is nothing for this stand-in
    to overreach with even if it tried.
    """

    invocations: list[CommitmentExtractionInvocation] = field(default_factory=list)

    async def invoke_commitment_extraction(
        self, invocation: CommitmentExtractionInvocation
    ) -> CommitmentExtractionResult:
        self.invocations.append(invocation)
        return _envelope(
            invocation,
            self._answer(invocation),
            prompt_version=COMMITMENT_EXTRACTION_PROMPT_VERSION,
        )

    def _answer(self, invocation: CommitmentExtractionInvocation) -> CommitmentExtractionOutput:
        payload = invocation.payload
        text = payload.reply_text
        match = _ISO_DATE.search(text)
        if match is None:
            # No date at all means no commitment is possible. Proposing one anyway is the
            # invented-deadline failure, and this stand-in has no way to reach it.
            return CommitmentExtractionOutput(
                case_id=payload.case_id,
                source_evidence_id=payload.source_evidence_id,
                commitments=(),
            )
        start, end = self._sentence_span(text, match.start())
        return CommitmentExtractionOutput(
            case_id=payload.case_id,
            source_evidence_id=payload.source_evidence_id,
            commitments=(
                ProposedCommitmentDraft(
                    obligor_span=SourceSpan(start=start, end=min(end, start + 40)),
                    action_span=SourceSpan(start=start, end=end),
                    due_date_span=SourceSpan(start=match.start(), end=match.end()),
                    obligor=payload.destination_display_label,
                    action_text=text[start:end].strip(),
                    # Advisory and never read. It is deliberately the plain midnight instant
                    # rather than the derived end of day, so a test that read it instead of the
                    # derived value would fail rather than accidentally agree.
                    due_at=datetime.fromisoformat(match.group(0)).replace(tzinfo=UTC),
                    refusal_detected=False,
                ),
            ),
        )

    @staticmethod
    def _sentence_span(text: str, index: int) -> tuple[int, int]:
        start = 0
        for position in range(index - 1, -1, -1):
            if text[position] in _SENTENCE_BOUND:
                start = position + 1
                break
        end = len(text)
        for position in range(index, len(text)):
            if text[position] in _SENTENCE_BOUND:
                end = position
                break
        while start < end and text[start].isspace():
            start += 1
        return start, end


__all__ = [
    "FAKE_MODEL_PROFILE_HASH",
    "LiteralSpanCommitmentExtractor",
    "ScriptedCommitmentExtractor",
]
