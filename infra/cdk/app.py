"""CDK application used by the pinned synth command."""

from __future__ import annotations

from aws_cdk import App

from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks import (
    ApplicationBuckets,
    ApplicationTables,
    ChorusAgentStack,
    ChorusApplicationStack,
    ChorusCompilerStack,
    ChorusDataStack,
    ChorusFoundationStack,
    ChorusSenderStack,
    CompilerBuckets,
    CompilerTables,
    SenderBuckets,
    SenderTables,
)


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
    data = ChorusDataStack(
        app,
        "AmbientChorusData",
        config=config,
    )
    ChorusAgentStack(
        app,
        "AmbientChorusAgents",
        config=config,
    )
    ChorusCompilerStack(
        app,
        "AmbientChorusCompiler",
        config=config,
        tables=CompilerTables(
            core=data.core_table,
            shareable=data.shareable_table,
            audit=data.audit_table,
        ),
        buckets=CompilerBuckets(
            private=data.private_evidence_bucket,
            export=data.export_evidence_bucket,
            private_key=data.private_evidence_key,
            export_key=data.export_evidence_key,
        ),
    )
    # Synthesized in Phase 7 so the ADR-022 negative-capability assertion has a policy to read.
    # The application principal existed as a row in the trust matrix and as nothing a test could
    # check; a condition-check grant on a compiler-owned prefix is exactly the kind of statement
    # that has to be provable from the template rather than argued from the repository.
    ChorusApplicationStack(
        app,
        "AmbientChorusApplication",
        config=config,
        tables=ApplicationTables(
            core=data.core_table,
            shareable=data.shareable_table,
            audit=data.audit_table,
        ),
        buckets=ApplicationBuckets(
            private=data.private_evidence_bucket,
            export=data.export_evidence_bucket,
            private_key=data.private_evidence_key,
            export_key=data.export_evidence_key,
        ),
    )
    # Synthesized in Phase 8 so the ADR-024 negative-capability sweep has a policy to read.
    # The sender is the principal whose documented boundary was, until now, a sentence beside
    # a grant that contradicted it: ``W(execution only)`` authorized rewriting the proposal
    # and the approval, because they shared a partition. Nothing here is deployed.
    ChorusSenderStack(
        app,
        "AmbientChorusSender",
        config=config,
        tables=SenderTables(
            core=data.core_table,
            shareable=data.shareable_table,
            audit=data.audit_table,
        ),
        buckets=SenderBuckets(
            private=data.private_evidence_bucket,
            export=data.export_evidence_bucket,
        ),
    )
    return app


if __name__ == "__main__":
    build_app().synth()
