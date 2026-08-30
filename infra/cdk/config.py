"""Non-secret CDK build configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CdkBuildConfig:
    """Stable tags for the Phase 0 synthesis proof."""

    project: str = "ambient-chorus"
    environment: str = "development"
    namespace: str = "LOCAL"
