"""Resource-free Phase 0 stack proving CDK/toolchain compatibility."""

from __future__ import annotations

from aws_cdk import Stack, Tags
from constructs import Construct

from infra.cdk.config import CdkBuildConfig


class ChorusFoundationStack(Stack):
    """A resource-free stack; concrete AWS resources start in their planned phases."""

    def __init__(self, scope: Construct, construct_id: str, *, config: CdkBuildConfig) -> None:
        super().__init__(scope, construct_id)
        Tags.of(self).add("Project", config.project)
        Tags.of(self).add("Environment", config.environment)
        Tags.of(self).add("Namespace", config.namespace)
        Tags.of(self).add("DataClass", "NONE")
