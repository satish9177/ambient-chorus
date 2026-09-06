"""Local Action adapters: one scripted, one deterministic. Neither is ever the demo path.

Both implement :class:`~chorus.ports.agents.ActionAgentPort`, so the application code under
test is byte-for-byte the code that runs against Bedrock. What changes is only who answers.

``ScriptedActionAgent`` answers with whatever a test hands it, including deliberately hostile
answers -- invented export-fact identifiers, uncited numbers, a phone number, a quoted span,
an uncaveated contradicted fact. It is how the adversarial suite exercises the grounding
grammar and the validator without needing a model that can be persuaded to overreach on demand.

``CautiousFakeActionAgent`` is a deterministic stand-in for local development. It is a *fake
model*, not a fallback writer: the deployed demo rejects ``CHORUS_AGENT_MODE=fake`` at startup.
It writes the narrowest proposal the contract admits -- one claim whose text is a cited fact's
own ``safe_text``, a normative request, and a caveat for every contradicted fact it relies on
-- because a stand-in that improvised prose would be the one component in the system quietly
deciding what an external recipient reads.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Final
from uuid import UUID, uuid5

from chorus.contracts.action import (
    ACTION_PROMPT_VERSION,
    ActionCaveatDraft,
    ActionClaimDraft,
    ActionInput,
    ActionProposalDraft,
    ActionRequestDraft,
    ActionToneValue,
    SafeEvidenceStatus,
)
from chorus.contracts.common import (
    AGENT_OUTPUT_SCHEMA_VERSION,
    AgentName,
    AgentResultEnvelope,
)
from chorus.ports.agents import ActionInvocation, ActionResult, AgentError

FAKE_MODEL_PROFILE_HASH: Final = f"sha256:{sha256(b'fake-action-runtime').hexdigest()}"

_FAKE_NAMESPACE: Final = UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")
"""A fixed UUIDv5 namespace so the stand-in's model-local IDs are reproducible.

They are model-local by contract -- a ``claim_id`` names nothing outside its own proposal --
so deriving them from the view identity costs nothing and makes a local run repeatable.
"""


def _local_id(view_id: UUID, label: str) -> UUID:
    return uuid5(_FAKE_NAMESPACE, f"{view_id}:{label}")


@dataclass(slots=True)
class ScriptedActionAgent:
    """Answer with an exact, test-supplied proposal or failure.

    ``responder`` receives the invocation so a test can assert what the application actually
    projected -- which is how "the Action payload contained only the view" becomes a test
    rather than a claim.
    """

    responder: Callable[[ActionInvocation], ActionProposalDraft]
    failures: list[AgentError] = field(default_factory=list)
    invocations: list[ActionInvocation] = field(default_factory=list)
    prompt_version: str = ACTION_PROMPT_VERSION
    envelope_override: Callable[[ActionResult], ActionResult] | None = None
    on_invoke: Callable[[ActionInvocation], Awaitable[None]] | None = None
    """Awaited before the answer is built, so a race test can move durable state *while* the
    model is notionally running.

    Awaitable rather than synchronous because the world moving mid-invocation is itself
    persistence work, and a synchronous hook would have to start a second event loop inside the
    one already running the use case. This is the only way to reach the race the pre-invocation
    checks structurally cannot see.
    """

    async def invoke_action(self, invocation: ActionInvocation) -> ActionResult:
        self.invocations.append(invocation)
        if self.on_invoke is not None:
            await self.on_invoke(invocation)
        if self.failures:
            raise self.failures.pop(0)
        started = datetime.now(UTC)
        envelope = AgentResultEnvelope[ActionProposalDraft](
            schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
            invocation_id=invocation.invocation_id,
            namespace=invocation.namespace,
            agent_name=AgentName.ACTION,
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
class CautiousFakeActionAgent:
    """A deterministic, deliberately unimaginative stand-in for local development."""

    invocations: list[ActionInvocation] = field(default_factory=list)

    async def invoke_action(self, invocation: ActionInvocation) -> ActionResult:
        self.invocations.append(invocation)
        started = datetime.now(UTC)
        return AgentResultEnvelope[ActionProposalDraft](
            schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
            invocation_id=invocation.invocation_id,
            namespace=invocation.namespace,
            agent_name=AgentName.ACTION,
            case_id=invocation.case_id,
            case_version=invocation.case_version,
            model_profile_arn_hash=FAKE_MODEL_PROFILE_HASH,
            prompt_version=ACTION_PROMPT_VERSION,
            started_at=started,
            completed_at=started,
            output=self._answer(invocation.payload),
        )

    @staticmethod
    def _answer(payload: ActionInput) -> ActionProposalDraft:
        """Restate one safe fact and ask for a repair, citing the fact that justifies asking.

        The claim text **is** the cited fact's ``safe_text``, which is the only wording that is
        grounded by construction. That is not a shortcut around the validator -- it is what an
        honest stand-in looks like when the component being stood in for is the one that writes
        prose. A local run therefore exercises the whole pipeline without any component
        improvising the words an external recipient would read.
        """

        fact = payload.shareable_facts[0]
        claim = ActionClaimDraft(
            claim_id=_local_id(payload.view_id, "claim"),
            text=fact.safe_text,
            export_fact_ids=(fact.export_fact_id,),
        )
        caveats = (
            (
                ActionCaveatDraft(
                    caveat_id=_local_id(payload.view_id, "caveat"),
                    # Fixed copy with no risk token and no name candidate beyond a
                    # sentence-initial word, so the obligation is discharged without the
                    # stand-in inventing a factual qualifier.
                    text="This observation is disputed by other reports in the same case.",
                    export_fact_ids=(fact.export_fact_id,),
                ),
            )
            if fact.evidence_status is SafeEvidenceStatus.CONTRADICTED
            else ()
        )
        return ActionProposalDraft(
            view_id=payload.view_id,
            view_hash=payload.view_hash,
            case_id=payload.case_id,
            case_version=payload.case_version,
            authorization_version=payload.authorization_version,
            subject="Repair request",
            claims=(claim,),
            request=ActionRequestDraft(
                requested_action="Please inspect and repair, then confirm the schedule.",
                requested_deadline=None,
                request_fact_ids=(fact.export_fact_id,),
            ),
            caveats=caveats,
            tone=ActionToneValue.NEUTRAL,
        )


__all__ = ["FAKE_MODEL_PROFILE_HASH", "CautiousFakeActionAgent", "ScriptedActionAgent"]
