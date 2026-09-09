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
    ChorusWatcherStack,
    CompilerBuckets,
    CompilerTables,
    SenderBuckets,
    SenderTables,
    WatcherBuckets,
    WatcherTables,
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
    # The customer artifact bucket is a third bucket, separate from both evidence buckets and
    # their keys, because agent code is not evidence (deployment contract SS 6). Its ARN is a
    # literal derived from the environment token -- the same shape every other stack uses for a
    # cross-stack name -- so each runtime role can scope ``s3:GetObject`` to its own
    # ``{agent}/*`` prefix. No bucket resource is created here.
    ChorusAgentStack(
        app,
        "AmbientChorusAgents",
        config=config,
        artifact_bucket_arn=f"arn:aws:s3:::chorus-agent-artifacts-{config.environment}",
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
    # Synthesized in Phase 9 so the ADR-028 negative-capability assertions have a policy to
    # read, and created *before* the application stack because the application's narrowed
    # scheduler grant names this stack's schedule group and passes this stack's execution role.
    # The watcher is the smallest principal in the system -- one edge, one Shareable partition,
    # no model, no mail, no scheduler client -- and its trust-matrix row was wrong until now: it
    # read ``Share: R/W(commitment/case projection)``, which mislocated the case row into a table
    # the watcher is denied outright. Nothing here is deployed.
    watcher = ChorusWatcherStack(
        app,
        "AmbientChorusWatcher",
        config=config,
        tables=WatcherTables(
            core=data.core_table,
            shareable=data.shareable_table,
            audit=data.audit_table,
        ),
        buckets=WatcherBuckets(
            private=data.private_evidence_bucket,
            export=data.export_evidence_bucket,
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
        scheduler_group_name=watcher.schedule_group_name,
        scheduler_role_arn=watcher.scheduler_role_arn_literal,
        # The API's one synchronous downstream invocation, and the reason it needs a grant at
        # all: ``POST /v1/demo/clock/advance`` returns the watcher's outcome, so the request
        # path invokes the watcher's ``live`` alias and parses its answer (deployment contract
        # SS 8.1). The literal comes from the watcher stack, which is built above -- the same
        # shape every other cross-stack name here uses, and no cycle either way.
        watcher_alias_arn=watcher.watcher_alias_arn_literal,
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
