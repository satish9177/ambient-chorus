"""Function physical names come from the configured environment string; the region is frozen.

Review P2-1: the old ``chorus_lambda`` parsed the environment back out of a CloudFormation
token (a ``LogGroup`` name), which resolved to the ``development`` fallback -- so a ``demo``
synthesis created ``chorus-compiler-development`` while every consumer targeted
``chorus-compiler-demo``. Names are now ``{manifest.name}-{config.environment}`` directly.

Review P2-2: every deployment stack is created for ``us-east-1`` via ``Environment(region=...)``;
``CdkBuildConfig`` rejects any other region; and consumers name the compiler / sender **actual
resource** ARNs, not independently constructed literals.
"""

from __future__ import annotations

import json

import pytest
from aws_cdk import App, Environment, assertions
from infra.cdk.config import PHASE_11_REGION, CdkBuildConfig
from infra.cdk.stacks import (
    ApplicationBuckets,
    ApplicationTables,
    ChorusApplicationStack,
    ChorusCompilerStack,
    ChorusDataStack,
    ChorusSenderStack,
    ChorusWatcherStack,
    CompilerBuckets,
    CompilerTables,
    SenderBuckets,
    SenderTables,
    WatcherBuckets,
    WatcherTables,
)

ENVIRONMENTS = ["development", "test", "demo"]
NAMESPACE = {"development": "LOCAL", "test": "TEST_V1", "demo": "DEMO"}


def _function_names(template: assertions.Template) -> set[str]:
    return {
        str(r["Properties"]["FunctionName"])
        for r in template.find_resources("AWS::Lambda::Function").values()
    }


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_every_functions_physical_name_carries_the_configured_environment(
    environment: str,
) -> None:
    app = App()
    config = CdkBuildConfig(environment=environment, namespace=NAMESPACE[environment])
    env = Environment(region=config.aws_region)
    data = ChorusDataStack(app, "D", config=config, env=env)
    compiler = ChorusCompilerStack(
        app,
        "C",
        config=config,
        env=env,
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
    sender = ChorusSenderStack(
        app,
        "S",
        config=config,
        env=env,
        tables=SenderTables(
            core=data.core_table, shareable=data.shareable_table, audit=data.audit_table
        ),
        buckets=SenderBuckets(
            private=data.private_evidence_bucket, export=data.export_evidence_bucket
        ),
    )
    watcher = ChorusWatcherStack(
        app,
        "W",
        config=config,
        env=env,
        tables=WatcherTables(
            core=data.core_table, shareable=data.shareable_table, audit=data.audit_table
        ),
        buckets=WatcherBuckets(
            private=data.private_evidence_bucket, export=data.export_evidence_bucket
        ),
    )
    application = ChorusApplicationStack(
        app,
        "A",
        config=config,
        env=env,
        tables=ApplicationTables(
            core=data.core_table, shareable=data.shareable_table, audit=data.audit_table
        ),
        buckets=ApplicationBuckets(
            private=data.private_evidence_bucket,
            export=data.export_evidence_bucket,
            private_key=data.private_evidence_key,
            export_key=data.export_evidence_key,
        ),
    )

    names: set[str] = set()
    for stack in (compiler, sender, watcher, application):
        names |= _function_names(assertions.Template.from_stack(stack))

    assert names == {
        f"chorus-api-{environment}",
        f"chorus-worker-{environment}",
        f"chorus-compiler-{environment}",
        f"chorus-sender-{environment}",
        f"chorus-commitment-watcher-{environment}",
    }


def test_config_rejects_a_region_other_than_the_frozen_one() -> None:
    assert CdkBuildConfig().aws_region == PHASE_11_REGION
    with pytest.raises(ValueError, match="frozen to 'us-east-1'"):
        CdkBuildConfig(aws_region="eu-west-1")


def test_the_offline_assembly_has_no_unknown_region_stack() -> None:
    from infra.cdk.app import build_app

    assembly = build_app(offline=True).synth()
    for stack in assembly.stacks:
        assert stack.environment.region == PHASE_11_REGION
        assert "unknown-region" not in stack.environment.region


def test_the_sender_and_application_name_the_compilers_actual_function_arn() -> None:
    """Review P2-2: no independently constructed compiler/sender ARN when the Function exists --
    consumers reference it by ``Fn::ImportValue`` of the real resource."""

    from infra.cdk.app import build_app

    assembly = build_app(offline=True).synth()
    sender = assembly.get_stack_by_name("AmbientChorusSender").template
    application = assembly.get_stack_by_name("AmbientChorusApplication").template

    sender_invoke = next(
        s
        for r in sender["Resources"].values()
        if r["Type"] == "AWS::IAM::Policy"
        for s in r["Properties"]["PolicyDocument"]["Statement"]
        if s.get("Sid") == "InvokeCompilerFenceOperationOnly"
    )
    assert "Fn::ImportValue" in json.dumps(sender_invoke["Resource"])
    assert "CompilerFunction" in json.dumps(sender_invoke["Resource"])

    app_invoke = next(
        s
        for r in application["Resources"].values()
        if r["Type"] == "AWS::IAM::Policy"
        for s in r["Properties"]["PolicyDocument"]["Statement"]
        if s.get("Sid") == "InvokeCompilerAndSenderOnly"
    )
    rendered = json.dumps(app_invoke["Resource"])
    assert "CompilerFunction" in rendered and "SenderFunction" in rendered
