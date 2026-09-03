"""The Monitor AgentCore runtime: a tool-less agent, one prompt version, one strict output."""

from runtimes.monitor.prompt import (
    MONITOR_PROMPT_VERSION,
    MONITOR_SYSTEM_PROMPT,
    derive_fence,
    fence_token,
    render_monitor_user_message,
)

__all__ = [
    "MONITOR_PROMPT_VERSION",
    "MONITOR_SYSTEM_PROMPT",
    "derive_fence",
    "fence_token",
    "render_monitor_user_message",
]
