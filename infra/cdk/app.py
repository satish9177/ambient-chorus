"""CDK application used by the pinned synth command."""

from __future__ import annotations

from aws_cdk import App

from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks import ChorusAgentStack, ChorusDataStack, ChorusFoundationStack


def build_app() -> App:
    """Construct the CDK application without deploying resources."""

    app = App()
    environment = app.node.try_get_context("environment")
    config = (
        CdkBuildConfig(environment=environment)
        if isinstance(environment, str) and environment
        else CdkBuildConfig()
    )
    ChorusFoundationStack(
        app,
        "AmbientChorusFoundation",
        config=config,
    )
    ChorusDataStack(
        app,
        "AmbientChorusData",
        config=config,
    )
    ChorusAgentStack(
        app,
        "AmbientChorusAgents",
        config=config,
    )
    return app


if __name__ == "__main__":
    build_app().synth()
