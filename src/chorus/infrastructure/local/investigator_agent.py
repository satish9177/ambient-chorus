"""Local Investigator adapters: one scripted, one deterministic. Neither is ever the demo path.

Both implement :class:`~chorus.ports.agents.InvestigatorAgentPort`, so the application code
under test is byte-for-byte the code that runs against Bedrock. What changes is only who
answers.

``ScriptedInvestigatorAgent`` answers with whatever a test hands it, including deliberately
hostile answers -- invented identifiers, foreign citations, fabricated contradictions, proposed
``VERIFIED``. It is how the adversarial suite exercises the validator and the status ladder
without needing a model that can be persuaded to overreach on demand.

``CautiousFakeInvestigatorAgent`` is a deterministic stand-in for local development. It is a
*fake model*, not a fallback investigator: the deployed demo rejects ``CHORUS_AGENT_MODE=fake``
at startup, and nothing in the application consults its reasoning. It proposes no status at
all and finds no contradiction, which makes it obvious in review that the real skepticism lives
in the model, because this stand-in performs none of it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Final

from chorus.contracts.common import (
    AGENT_OUTPUT_SCHEMA_VERSION,
    INVESTIGATOR_PROMPT_VERSION,
    AgentName,
    AgentResultEnvelope,
)
from chorus.contracts.investigation import (
    AlternativeExplanation,
    CitationSet,
    InvestigationAssessmentDraft,
    LinkageDecision,
    LinkageReason,
    RecommendedCaseDisposition,
    SufficiencyDraft,
)
from chorus.ports.agents import (
    AgentError,
    InvestigationInvocation,
    InvestigationResult,
)

FAKE_MODEL_PROFILE_HASH: Final = f"sha256:{sha256(b'fake-investigator-runtime').hexdigest()}"


@dataclass(slots=True)
class ScriptedInvestigatorAgent:
    """Answer with an exact, test-supplied assessment or failure.

    ``responder`` receives the invocation so a test can assert what the application actually
    projected -- which is how "the payload contained no contact detail" becomes a test rather
    than a claim.
    """

    responder: Callable[[InvestigationInvocation], InvestigationAssessmentDraft]
    failures: list[AgentError] = field(default_factory=list)
    invocations: list[InvestigationInvocation] = field(default_factory=list)
    prompt_version: str = INVESTIGATOR_PROMPT_VERSION
    envelope_override: Callable[[InvestigationResult], InvestigationResult] | None = None

    async def invoke_investigator(self, invocation: InvestigationInvocation) -> InvestigationResult:
        self.invocations.append(invocation)
        if self.failures:
            raise self.failures.pop(0)
        started = datetime.now(UTC)
        envelope = AgentResultEnvelope[InvestigationAssessmentDraft](
            schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
            invocation_id=invocation.invocation_id,
            namespace=invocation.namespace,
            agent_name=AgentName.INVESTIGATOR,
            case_id=invocation.case_id,
            case_version=invocation.case_version,
            model_profile_arn_hash=FAKE_MODEL_PROFILE_HASH,
            prompt_version=self.prompt_version,
            started_at=started,
            completed_at=started,
            output=self.responder(invocation),
        )
        if self.envelope_override is not None:
            return self.envelope_override(envelope)
        return envelope


@dataclass(slots=True)
class CautiousFakeInvestigatorAgent:
    """A deterministic, deliberately unskeptical stand-in for local development."""

    invocations: list[InvestigationInvocation] = field(default_factory=list)

    async def invoke_investigator(self, invocation: InvestigationInvocation) -> InvestigationResult:
        self.invocations.append(invocation)
        started = datetime.now(UTC)
        return AgentResultEnvelope[InvestigationAssessmentDraft](
            schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
            invocation_id=invocation.invocation_id,
            namespace=invocation.namespace,
            agent_name=AgentName.INVESTIGATOR,
            case_id=invocation.case_id,
            case_version=invocation.case_version,
            model_profile_arn_hash=FAKE_MODEL_PROFILE_HASH,
            prompt_version=INVESTIGATOR_PROMPT_VERSION,
            started_at=started,
            completed_at=started,
            output=self._answer(invocation),
        )

    def _answer(self, invocation: InvestigationInvocation) -> InvestigationAssessmentDraft:
        payload = invocation.payload
        contributors = {report.contributor_pseudonym_id for report in payload.reports}
        citations = CitationSet(
            cited_report_ids=tuple(report.report_id for report in payload.reports[:10]),
        )
        return InvestigationAssessmentDraft(
            case_id=payload.case.case_id,
            based_on_case_version=payload.case.version,
            linkage_decision=LinkageDecision.SAME_ISSUE,
            linkage_reasons=(
                LinkageReason(
                    reason="Every report in this case carries the same issue type.",
                    citations=citations,
                ),
            ),
            alternative_explanations=(
                AlternativeExplanation(
                    description="Scheduled maintenance could explain some of these reports.",
                    citations=citations,
                ),
            ),
            # No findings at all, deliberately. A stand-in that restated each fact's stored
            # ``current_status`` would look neutral and would not be: whenever the deterministic
            # recomputation raises a fact from ``REPORTED`` to ``CORROBORATED``, restating the
            # stored value is a *downgrade* the ladder honours, so every local run would quietly
            # suppress the corroboration it had just earned. Proposing nothing leaves the
            # computed status standing, which is the honest output of an analysis nobody did.
            evidence_findings=(),
            sufficiency=SufficiencyDraft(
                independent_source_count=len(contributors),
                is_corroborated=len(contributors) >= payload.corroboration_min,
                gaps=("This is a deterministic local stand-in, not an investigation.",),
            ),
            recommended_case_disposition=RecommendedCaseDisposition.CONTINUE_INVESTIGATION,
        )
