"""CDK application used by the pinned synth command.

Phase 11 batch 5 wires the five production Lambdas, the HTTP API, and the watcher ``:live``
alias into the existing identity stacks, and connects them by their **actual resource ARNs**
(deployment contract SS 16, SS 30; review P2-2):

    Data
      -> Compiler        (compiler Lambda)        -> compiler.function.function_arn
      -> Sender          (sender Lambda)          -> sender.function.function_arn
      -> Watcher         (watcher Lambda -> Version -> live Alias) -> watcher.alias.function_arn
      -> Application      (API + worker Lambdas, HTTP API; names the three ARNs above,
                           the secret identities, and the scheduler identity)

Every deployment stack is created for the frozen region ``us-east-1`` through
``env=Environment(region=...)`` -- none synthesizes as ``unknown-region``. The compute stacks
are independent of one another and every one precedes Application, whose two roles name their
ARNs; explicit ``add_stack_dependency`` calls make the deploy order match.

**Two modes, chosen explicitly (review P2-3, P2-4):**

* deployment-capable (the default) -- every Lambda ZIP must be built and every required
  deployment identity must be a real, validated ARN, or synthesis refuses;
* offline review -- ``build_app(offline=True)`` or ``-c offline_synth=true`` /
  ``CHORUS_CDK_OFFLINE_SYNTH=1`` -- uses the clearly-named placeholder code fixture and
  synthetic identity fixtures, and its output is never deploy-ready.
"""

from __future__ import annotations

from aws_cdk import App, Environment

from infra.cdk.config import (
    CdkBuildConfig,
    DeploymentIdentities,
    offline_synth_requested,
)
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

_DEFAULT_NAMESPACE_BY_ENVIRONMENT = {"demo": "DEMO"}


def _resolve_config(app: App) -> CdkBuildConfig:
    """Build the stable config from context. Region defaults to -- and is validated against --
    the frozen Phase 11 region; the account is used only when explicitly supplied."""

    def _str(key: str) -> str | None:
        value = app.node.try_get_context(key)
        return value if isinstance(value, str) and value else None

    environment = _str("environment") or "development"
    namespace = _str("namespace") or _DEFAULT_NAMESPACE_BY_ENVIRONMENT.get(environment) or "LOCAL"
    kwargs: dict[str, str] = {"environment": environment, "namespace": namespace}
    region = _str("aws_region")
    if region is not None:
        kwargs["aws_region"] = region  # CdkBuildConfig.__post_init__ rejects a non-frozen region
    account = _str("account")
    if account is not None:
        kwargs["account"] = account
    return CdkBuildConfig(**kwargs)


def _stack_env(config: CdkBuildConfig) -> Environment:
    """The frozen region on every deployment stack; the account only when supplied."""

    return Environment(region=config.aws_region, account=config.account)


def build_app(*, offline: bool | None = None, context: dict[str, str] | None = None) -> App:
    """Construct the CDK application without deploying resources.

    ``offline`` selects the synthesis mode; when ``None`` it is read from context /
    ``CHORUS_CDK_OFFLINE_SYNTH`` and otherwise defaults to **deployment-capable** -- so the
    normal ``python infra/cdk/app.py`` path never silently uses a placeholder (review P2-3).
    ``context`` is an optional CDK context map (equivalent to repeated ``-c key=value``), used
    by tests to exercise deployment mode with real identity ARNs.
    """

    app = App(context=context)
    config = _resolve_config(app)
    offline_synth = (
        offline if offline is not None else offline_synth_requested(app.node.try_get_context)
    )
    identities = DeploymentIdentities.from_context(
        config.environment, app.node.try_get_context, offline=offline_synth
    )
    env = _stack_env(config)

    ChorusFoundationStack(app, "AmbientChorusFoundation", config=config, env=env)
    data = ChorusDataStack(app, "AmbientChorusData", config=config, env=env)
    # The customer artifact bucket is a third bucket, separate from both evidence buckets and
    # their keys, because agent code is not evidence (deployment contract SS 6). Its ARN is a
    # literal derived from the environment token; no bucket resource is created here.
    ChorusAgentStack(
        app,
        "AmbientChorusAgents",
        config=config,
        env=env,
        artifact_bucket_arn=f"arn:aws:s3:::chorus-agent-artifacts-{config.environment}",
    )
    compiler = ChorusCompilerStack(
        app,
        "AmbientChorusCompiler",
        config=config,
        env=env,
        identities=identities,
        offline_synth=offline_synth,
        tables=CompilerTables(
            core=data.core_table, shareable=data.shareable_table, audit=data.audit_table
        ),
        buckets=CompilerBuckets(
            private=data.private_evidence_bucket,
            export=data.export_evidence_bucket,
            private_key=data.private_evidence_key,
            export_key=data.export_evidence_key,
        ),
    )
    # The sender invokes the compiler for both halves of the send fence, so it takes the
    # compiler's **actual function ARN** (review P2-2); batch 5 also always wires its
    # destination-registry secret identity.
    sender = ChorusSenderStack(
        app,
        "AmbientChorusSender",
        config=config,
        env=env,
        identities=identities,
        offline_synth=offline_synth,
        compiler_function_arn=compiler.function.function_arn,
        tables=SenderTables(
            core=data.core_table, shareable=data.shareable_table, audit=data.audit_table
        ),
        buckets=SenderBuckets(
            private=data.private_evidence_bucket, export=data.export_evidence_bucket
        ),
    )
    # Created before Application because the application's narrowed scheduler grant names this
    # stack's schedule group and passes this stack's execution role. Batch 5 adds the watcher
    # Lambda here, plus its published Version and the ``live`` Alias that the scheduler role and
    # the API role both invoke.
    watcher = ChorusWatcherStack(
        app,
        "AmbientChorusWatcher",
        config=config,
        env=env,
        identities=identities,
        offline_synth=offline_synth,
        tables=WatcherTables(
            core=data.core_table, shareable=data.shareable_table, audit=data.audit_table
        ),
        buckets=WatcherBuckets(
            private=data.private_evidence_bucket, export=data.export_evidence_bucket
        ),
    )
    # Batch 5 adds the API and worker Lambdas and the HTTP API here. Application receives the
    # compiler and sender **actual function ARNs**, the watcher ``live`` Alias resource ARN, the
    # scheduler identity, and the secret identities; the worker is created in this stack, so the
    # API binds to it directly.
    application = ChorusApplicationStack(
        app,
        "AmbientChorusApplication",
        config=config,
        env=env,
        identities=identities,
        offline_synth=offline_synth,
        tables=ApplicationTables(
            core=data.core_table, shareable=data.shareable_table, audit=data.audit_table
        ),
        buckets=ApplicationBuckets(
            private=data.private_evidence_bucket,
            export=data.export_evidence_bucket,
            private_key=data.private_evidence_key,
            export_key=data.export_evidence_key,
        ),
        scheduler_group_name=watcher.schedule_group_name,
        scheduler_role_arn=watcher.scheduler_role_arn_literal,
        compiler_function_arn=compiler.function.function_arn,
        sender_function_arn=sender.function.function_arn,
        # The actual ``Alias`` resource ARN, so ``POST /v1/demo/clock/advance`` invokes the same
        # ``:live`` identity the scheduler does (deployment contract SS 8.1, SS 38, SS 46).
        watcher_alias_arn=watcher.watcher_alias_arn,
    )

    # A principal's stack deploys after every resource its policy names (deployment contract
    # SS 16). Cross-stack ARN references already create most of these edges; the explicit calls
    # make the intent legible and cover the deterministic-literal cases.
    sender.add_stack_dependency(compiler)
    application.add_stack_dependency(compiler)
    application.add_stack_dependency(sender)
    application.add_stack_dependency(watcher)

    return app


if __name__ == "__main__":
    build_app().synth()
