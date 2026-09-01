"""Non-secret CDK build configuration."""

from __future__ import annotations

from dataclasses import dataclass

DISPOSABLE_ENVIRONMENTS = frozenset({"development", "test"})
"""Environments whose data may be destroyed with the stack.

Anything else is treated as durable. The default is deliberately fail-safe: an environment
name nobody anticipated keeps deletion protection and point-in-time recovery enabled.
"""


@dataclass(frozen=True, slots=True)
class CdkBuildConfig:
    """Stable tags for the Phase 0 synthesis proof."""

    project: str = "ambient-chorus"
    environment: str = "development"
    namespace: str = "LOCAL"

    @property
    def is_disposable(self) -> bool:
        """Whether stored data in this environment may be destroyed with the stack."""

        return self.environment in DISPOSABLE_ENVIRONMENTS
