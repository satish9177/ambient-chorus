"""The Action AgentCore runtime: a tool-less agent, one prompt version, one strict output."""

from runtimes.action.prompt import (
    ACTION_PROMPT_VERSION,
    ACTION_SYSTEM_PROMPT,
    derive_fence,
    fence_token,
    render_action_user_message,
)

__all__ = [
    "ACTION_PROMPT_VERSION",
    "ACTION_SYSTEM_PROMPT",
    "derive_fence",
    "fence_token",
    "render_action_user_message",
]
