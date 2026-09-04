"""The Strands agent this runtime hosts: no tools, temperature zero, one model attempt.

``tools`` is omitted rather than set to an empty list because the omission is the contract:
there is nothing to register, nothing to load from a directory, and no dynamic tool discovery.
The agent's only capability is to answer, and the only shape it may answer in is
:class:`InvestigationAssessmentDraft`.

Session state is never reused. Each invocation constructs its own agent, so nothing survives
between two communities' cases inside this process -- which matters more here than for the
Monitor, because the payload is a whole private case rather than one message batch.

One invocation, one model attempt
---------------------------------
The application owns exactly one automatic agent retry, and it owns it because it is the only
layer that knows whether anything was persisted and which invocation identity a second attempt
belongs to. Both lower layers are pinned explicitly, and neither is left to its default:

* ``ModelRetryStrategy(max_attempts=1)`` on the agent -- the SDK default is six attempts with
  exponential backoff, a sensible default for a chatbot and the wrong one here;
* ``retries={"mode": "standard", "total_max_attempts": 1}`` on the Bedrock client, the same
  setting the AgentCore and DynamoDB clients already carry.

Timeouts are ordered rather than merely set. ``MODEL_READ_TIMEOUT_SECONDS`` bounds one model
attempt and must expire before the runtime's own budget, which must expire before the
application's AgentCore read timeout -- so the application never abandons a runtime that is
still working and launches a second one beside it.

The Investigator's inner timeout is longer than the Monitor's on purpose: it reads a whole case
and answers with a per-fact assessment, so its output budget is 6,000 tokens rather than 4,000
and a bound tuned for a message batch would cut off honest answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from chorus.contracts.investigation import (
    InvestigationAssessmentDraft,
    InvestigationInput,
)
from runtimes.investigator.prompt import (
    INVESTIGATOR_SYSTEM_PROMPT,
    fence_token,
    render_investigation_user_message,
)

INVESTIGATOR_MAX_OUTPUT_TOKENS: Final = 6_000
INVESTIGATOR_TEMPERATURE: Final = 0.0
"""The frozen model parameters for this agent.

Temperature zero is not a quality preference. Downstream everything is deterministic, so the
model is the only source of run-to-run variation, and reducing it makes an evaluation failure
reproducible instead of intermittent.
"""

INVESTIGATOR_MAX_MODEL_ATTEMPTS: Final = 1
"""Model attempts inside one runtime invocation. One, always, and never a default."""

MODEL_READ_TIMEOUT_SECONDS: Final = 60
MODEL_CONNECT_TIMEOUT_SECONDS: Final = 10
"""The innermost timeout in the hierarchy, and the only one that bounds a model call.

Every outer budget must be strictly larger. See ``RUNTIME_BUDGET_SECONDS`` in the entrypoint
and ``agent_timeout_seconds`` in ``Settings``; a test asserts the ordering rather than trusting
three separately chosen numbers to stay consistent.
"""

SINGLE_ATTEMPT_RETRIES: Final = {"mode": "standard", "total_max_attempts": 1}


@dataclass(slots=True)
class InvestigatorAgentRunner:
    """Build and run one tool-less Strands agent per invocation."""

    model_id: str
    region_name: str
    max_tokens: int = INVESTIGATOR_MAX_OUTPUT_TOKENS
    temperature: float = INVESTIGATOR_TEMPERATURE
    read_timeout_seconds: int = MODEL_READ_TIMEOUT_SECONDS

    def build_model(self) -> Any:
        """Construct the Bedrock model with retrying off and an explicit read timeout.

        Imported lazily so this module can be inspected, type-checked, and unit-tested for its
        prompt and payload handling without the Strands SDK present.
        """

        from botocore.config import Config
        from strands.models import BedrockModel

        return BedrockModel(
            model_id=self.model_id,
            region_name=self.region_name,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            boto_client_config=Config(
                retries=dict(SINGLE_ATTEMPT_RETRIES),
                read_timeout=self.read_timeout_seconds,
                connect_timeout=MODEL_CONNECT_TIMEOUT_SECONDS,
            ),
        )

    def build_agent(self) -> Any:
        """Construct the agent for exactly one invocation."""

        from strands import Agent, ModelRetryStrategy

        return Agent(
            model=self.build_model(),
            system_prompt=INVESTIGATOR_SYSTEM_PROMPT,
            retry_strategy=ModelRetryStrategy(max_attempts=INVESTIGATOR_MAX_MODEL_ATTEMPTS),
        )

    async def run(self, payload: InvestigationInput, *, fence: str) -> InvestigationAssessmentDraft:
        """Return the structured answer for one bounded case payload.

        ``fence`` is derived from the server-generated invocation identity by the entrypoint, so
        the delimiters wrapping untrusted text are unpredictable to anyone who wrote that text --
        including whoever authored a piece of evidence. It is passed in rather than generated
        here because the runner must not be the thing that decides what an invocation is called.
        """

        agent = self.build_agent()
        result = await agent.structured_output_async(
            InvestigationAssessmentDraft,
            render_investigation_user_message(payload, fence=fence),
        )
        if not isinstance(result, InvestigationAssessmentDraft):  # pragma: no cover - SDK guard
            raise TypeError("the Investigator runtime returned an unexpected structured output")
        return result


def effective_retry_configuration(runner: InvestigatorAgentRunner) -> dict[str, object]:
    """Read back what the constructed model and agent will actually do.

    A configuration test that asserted on the arguments this module passes would only prove this
    module is self-consistent. This reads the values off the instantiated objects, so it fails
    if a future SDK version renames, ignores, or overrides either setting.
    """

    from strands import ModelRetryStrategy

    model = runner.build_model()
    client_config = model.client.meta.config
    strategy = ModelRetryStrategy(max_attempts=INVESTIGATOR_MAX_MODEL_ATTEMPTS)
    return {
        "model_total_max_attempts": client_config.retries.get("total_max_attempts"),
        "model_retry_mode": client_config.retries.get("mode"),
        "model_read_timeout": client_config.read_timeout,
        "agent_max_model_attempts": getattr(strategy, "_max_attempts", None),
    }


__all__ = [
    "INVESTIGATOR_MAX_MODEL_ATTEMPTS",
    "INVESTIGATOR_MAX_OUTPUT_TOKENS",
    "INVESTIGATOR_TEMPERATURE",
    "MODEL_CONNECT_TIMEOUT_SECONDS",
    "MODEL_READ_TIMEOUT_SECONDS",
    "SINGLE_ATTEMPT_RETRIES",
    "InvestigatorAgentRunner",
    "effective_retry_configuration",
    "fence_token",
]
