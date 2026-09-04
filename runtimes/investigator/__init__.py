"""The Investigator AgentCore runtime: a tool-less agent, one prompt version, one strict output."""

from runtimes.investigator.prompt import (
    INVESTIGATOR_PROMPT_VERSION,
    INVESTIGATOR_SYSTEM_PROMPT,
    derive_fence,
    fence_token,
    render_investigation_user_message,
)

__all__ = [
    "INVESTIGATOR_PROMPT_VERSION",
    "INVESTIGATOR_SYSTEM_PROMPT",
    "derive_fence",
    "fence_token",
    "render_investigation_user_message",
]
