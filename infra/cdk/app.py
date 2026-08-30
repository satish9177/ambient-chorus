"""CDK application used by the pinned synth command."""

from __future__ import annotations

from aws_cdk import App

from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks import ChorusFoundationStack


def build_app() -> App:
    """Construct the Phase 0 CDK application without deploying resources."""

    app = App()
    ChorusFoundationStack(
        app,
        "AmbientChorusFoundation",
        config=CdkBuildConfig(),
    )
    return app


if __name__ == "__main__":
    build_app().synth()
