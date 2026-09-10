"""The five Lambda compute resources: shape, identity, environment, and cross-function wiring.

Phase 11 batch 5. These assertions cut across all four compute stacks, so they synthesize the
whole ``build_app`` (offline mode) once and read the templates -- the runtime / architecture /
handler / budget of each function (from its ``lambda.toml``, the packaging tool's source); that each
function runs under its **pre-existing** execution role with no managed policy added (SS 42-43);
that each function's environment identity and the matching IAM resource are one configured ARN
(SS 40); that the synthesized environment satisfies ``Settings.load()`` and the composition
mapper (SS 44); the API Gateway HTTP API payload-v2 integration (SS 45); the cross-function IAM
matrix (SS 41); and the watcher ``:live`` alias identity (SS 46).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from functools import cache
from typing import Any

import pytest
from aws_cdk import assertions
from infra.cdk.app import build_app
from infra.cdk.config import CdkBuildConfig, DeploymentIdentities
from infra.cdk.lambda_support import (
    ResourceNames,
    api_environment,
    compiler_environment,
    load_lambda_manifest,
    sender_environment,
    watcher_environment,
    worker_environment,
)

from chorus.settings import Settings

POLICY_TYPE = "AWS::IAM::Policy"

FUNCTION_STACK = {
    "api": "AmbientChorusApplication",
    "worker": "AmbientChorusApplication",
    "compiler": "AmbientChorusCompiler",
    "sender": "AmbientChorusSender",
    "commitment_watcher": "AmbientChorusWatcher",
}
EXPECTED_ROLE = {
    "api": "ApiRole",
    "worker": "WorkerRole",
    "compiler": "CompilerRole",
    "sender": "SenderRole",
    "commitment_watcher": "WatcherRole",
}
EXPECTED_LOG_GROUP = {
    "api": "ApiLogGroup",
    "worker": "WorkerLogGroup",
    "compiler": "CompilerLogGroup",
    "sender": "SenderLogGroup",
    "commitment_watcher": "WatcherLogGroup",
}


@cache
def _templates() -> dict[str, assertions.Template]:
    app = build_app(offline=True)
    names = set(FUNCTION_STACK.values())
    return {
        name: assertions.Template.from_stack(app.node.find_child(name))  # type: ignore[arg-type]
        for name in names
    }


def _template(directory: str) -> assertions.Template:
    return _templates()[FUNCTION_STACK[directory]]


def _function(directory: str) -> tuple[str, Mapping[str, Any]]:
    manifest = load_lambda_manifest(directory)
    for logical_id, resource in (
        _template(directory).find_resources("AWS::Lambda::Function").items()
    ):
        if resource["Properties"].get("FunctionName") == f"{manifest.name}-development":
            return logical_id, resource["Properties"]
    raise AssertionError(f"no {manifest.name} function synthesized")


def _statements(template: assertions.Template, role_prefix: str) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    for logical_id, policy in template.find_resources(POLICY_TYPE).items():
        if logical_id.startswith(role_prefix):
            found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def _allow_actions(statements: list[Mapping[str, Any]]) -> set[str]:
    out: set[str] = set()
    for item in statements:
        if item["Effect"] != "Allow":
            continue
        action = item["Action"]
        out |= {action} if isinstance(action, str) else set(action)
    return out


def _statement(statements: list[Mapping[str, Any]], sid: str) -> Mapping[str, Any]:
    return next(item for item in statements if item.get("Sid") == sid)


# -- SS 14, SS 34: shape from the manifest ------------------------------------------------


@pytest.mark.parametrize("directory", sorted(FUNCTION_STACK))
def test_each_function_matches_its_manifest_exactly(directory: str) -> None:
    manifest = load_lambda_manifest(directory)
    _, props = _function(directory)

    assert props["Runtime"] == "python3.12"
    assert props["Architectures"] == ["x86_64"]
    assert props["Handler"] == manifest.handler
    assert props["Timeout"] == manifest.timeout_seconds
    assert props["MemorySize"] == manifest.memory_mb
    assert "ReservedConcurrentExecutions" not in props  # SS 36: none until measured
    assert "VpcConfig" not in props  # SS 32-33: network attachment still deferred


# -- SS 42-43: the pre-existing role, no managed policy ---------------------------------


@pytest.mark.parametrize("directory", sorted(FUNCTION_STACK))
def test_each_function_uses_its_pre_existing_role_and_no_managed_policy(directory: str) -> None:
    template = _template(directory)
    _, props = _function(directory)

    role_ref = props["Role"]["Fn::GetAtt"][0]
    assert role_ref.startswith(EXPECTED_ROLE[directory])

    for role in template.find_resources("AWS::IAM::Role").values():
        managed = role["Properties"].get("ManagedPolicyArns", [])
        rendered = json.dumps(managed)
        assert "AWSLambdaBasicExecutionRole" not in rendered
        assert "AWSLambdaVPCAccessExecutionRole" not in rendered


@pytest.mark.parametrize("directory", sorted(FUNCTION_STACK))
def test_each_function_logs_to_the_existing_dedicated_group(directory: str) -> None:
    _, props = _function(directory)
    assert props["LoggingConfig"]["LogGroup"]["Ref"].startswith(EXPECTED_LOG_GROUP[directory])


# -- SS 44: the synthesized environment satisfies Settings and the composition mapper ----


@pytest.fixture
def _demo_env() -> Iterator[None]:
    saved = {k: v for k, v in os.environ.items() if k.startswith(("CHORUS_", "AWS_"))}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        for key in [k for k in os.environ if k.startswith(("CHORUS_", "AWS_"))]:
            del os.environ[key]
        os.environ.update(saved)


_FAKE_FN = "arn:aws:lambda:us-east-1:111122223333:function:{}"
_FAKE_KEY = "arn:aws:kms:us-east-1:111122223333:key/{}"
_FAKE_SECRET = "arn:aws:secretsmanager:us-east-1:111122223333:secret:{}"
_FAKE_RUNTIME = (
    "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/chorus_{}/runtime-endpoint/live"
)


def _builders(config: CdkBuildConfig) -> dict[str, dict[str, str]]:
    identities = DeploymentIdentities(
        environment=config.environment,
        offline=False,
        demo_access_secret_arn=_FAKE_SECRET.format("chorus-demo-access-AbC"),
        cursor_signing_secret_arn=_FAKE_SECRET.format("chorus-cursor-signing-QrS"),
        destination_registry_secret_arn=_FAKE_SECRET.format("chorus-dest-XyZ"),
        monitor_runtime_arn=_FAKE_RUNTIME.format("monitor-abc"),
        investigator_runtime_arn=_FAKE_RUNTIME.format("investigator-def"),
        action_runtime_arn=_FAKE_RUNTIME.format("action-ghi"),
    )
    names = ResourceNames.for_config(config)
    return {
        "api": api_environment(
            config=config,
            names=names,
            worker_function_arn=_FAKE_FN.format("chorus-worker-demo"),
            compiler_function_arn=_FAKE_FN.format("chorus-compiler-demo"),
            watcher_alias_arn=_FAKE_FN.format("chorus-commitment-watcher-demo") + ":live",
            demo_access_secret_arn=identities.demo_access_secret_arn,
            cursor_signing_secret_arn=identities.cursor_signing_secret_arn,
        ),
        "worker": worker_environment(
            config=config,
            identities=identities,
            names=names,
            compiler_function_arn=_FAKE_FN.format("chorus-compiler-demo"),
            sender_function_arn=_FAKE_FN.format("chorus-sender-demo"),
            watcher_alias_arn=_FAKE_FN.format("chorus-commitment-watcher-demo") + ":live",
            scheduler_role_arn="arn:aws:iam::111122223333:role/chorus-scheduler-demo",
        ),
        "compiler": compiler_environment(
            config=config,
            names=names,
            private_evidence_key_arn=_FAKE_KEY.format("private"),
            export_evidence_key_arn=_FAKE_KEY.format("export"),
        ),
        "sender": sender_environment(
            config=config,
            names=names,
            compiler_function_arn=_FAKE_FN.format("chorus-compiler-demo"),
            destination_registry_secret_arn=identities.destination_registry_secret_arn,
        ),
        "commitment_watcher": watcher_environment(config=config, names=names),
    }


@pytest.mark.parametrize("directory", sorted(FUNCTION_STACK))
def test_the_environment_keys_match_between_the_builder_and_the_template(
    directory: str,
) -> None:
    """No "CDK variable exists but Settings expects another name" drift: the keys CDK emits are
    exactly the keys the one typed environment builder produces (the templates and the builders
    both use the ``development`` config)."""

    _, props = _function(directory)
    template_keys = set(props["Environment"]["Variables"])
    builder_keys = set(_builders(CdkBuildConfig())[directory])
    assert template_keys == builder_keys


@pytest.mark.parametrize("directory", sorted(FUNCTION_STACK))
def test_the_synthesized_environment_constructs_settings(directory: str, _demo_env: None) -> None:
    demo = CdkBuildConfig(environment="demo", namespace="DEMO")
    os.environ.update(_builders(demo)[directory])
    settings = Settings.load()
    assert settings.environment.value == "demo"
    assert settings.namespace == "DEMO"
    assert settings.dynamodb_endpoint is None  # a deployed function reaches the real service


def test_the_api_environment_feeds_api_settings(_demo_env: None) -> None:
    from functions.api.composition import api_settings

    demo = CdkBuildConfig(environment="demo", namespace="DEMO")
    os.environ.update(_builders(demo)["api"])
    mapped = api_settings(Settings.load())
    assert mapped.watcher_function_arn.endswith(":live")
    assert mapped.demo_access_secret_arn.startswith("arn:aws:secretsmanager:")


# -- SS 40: environment identity == IAM resource, per configured secret -----------------


def test_api_demo_access_and_cursor_secret_env_match_the_iam_resources() -> None:
    template = _templates()["AmbientChorusApplication"]
    _, props = _function("api")
    env = props["Environment"]["Variables"]
    api = _statements(template, "ApiRole")

    demo = _statement(api, "ReadDemoAccessTokenSecretOnly")
    assert env["CHORUS_DEMO_ACCESS_SECRET_ARN"] == demo["Resource"]
    cursor = _statement(api, "ReadCursorSigningKeySecretOnly")
    assert env["CHORUS_CURSOR_SIGNING_SECRET_ARN"] == cursor["Resource"]
    assert demo["Resource"] != cursor["Resource"]


def test_sender_destination_registry_env_matches_the_iam_resource() -> None:
    template = _templates()["AmbientChorusSender"]
    _, props = _function("sender")
    env = props["Environment"]["Variables"]
    grant = _statement(_statements(template, "SenderRole"), "ReadDestinationRegistrySecretOnly")
    assert env["CHORUS_DESTINATION_REGISTRY_SECRET_ARN"] == grant["Resource"]


def test_the_worker_has_no_secret_arn_in_its_environment() -> None:
    _, props = _function("worker")
    env = props["Environment"]["Variables"]
    assert not any("SECRET_ARN" in key for key in env)


def test_api_and_worker_watcher_env_is_the_imported_live_alias_not_a_hand_built_arn() -> None:
    """SS 37-38: ``CHORUS_WATCHER_FUNCTION_ARN`` is the qualified ``:live`` alias, and it is the
    same identity the API's ``lambda:InvokeFunction`` grant names -- an ``Fn::ImportValue`` of
    the watcher stack's alias export, never a joined function ARN or a numeric version."""

    template = _templates()["AmbientChorusApplication"]
    _, api_props = _function("api")
    _, worker_props = _function("worker")
    api_arn = api_props["Environment"]["Variables"]["CHORUS_WATCHER_FUNCTION_ARN"]
    worker_arn = worker_props["Environment"]["Variables"]["CHORUS_WATCHER_FUNCTION_ARN"]

    assert isinstance(api_arn, dict) and "Fn::ImportValue" in api_arn
    assert api_arn == worker_arn  # SS 37: the scheduler and the worker point at one target

    grant = _statement(_statements(template, "ApiRole"), "InvokeCommitmentWatcherLiveAliasOnly")
    assert grant["Resource"] == api_arn


# -- SS 41: the cross-function IAM matrix ----------------------------------------------


def test_api_invokes_worker_compiler_and_watcher_live_but_not_sender_or_agentcore() -> None:
    api = _statements(_templates()["AmbientChorusApplication"], "ApiRole")
    allowed = _allow_actions(api)
    assert "bedrock-agentcore:InvokeAgentRuntime" not in allowed

    invoke_stmts = [
        item for item in api if "lambda:InvokeFunction" in json.dumps(item.get("Action"))
    ]
    assert len(invoke_stmts) == 2  # worker+compiler, then the watcher :live alias, separately
    combined = json.dumps([item["Resource"] for item in invoke_stmts])
    # the compiler by an Fn::ImportValue of its actual Function resource (review P2-2), the
    # worker by a GetAtt of the in-stack resource -- never a hand-built ARN literal
    assert "CompilerFunction" in combined and "Fn::ImportValue" in combined
    assert "WorkerFunction" in combined
    assert any(
        i.get("Sid") == "InvokeCommitmentWatcherLiveAliasOnly"
        and "Fn::ImportValue" in json.dumps(i["Resource"])
        for i in invoke_stmts
    )
    assert "SenderFunction" not in combined and "chorus-sender" not in combined


def test_worker_invokes_compiler_sender_and_three_runtimes_but_not_the_watcher() -> None:
    worker = _statements(_templates()["AmbientChorusApplication"], "WorkerRole")
    runtimes = _statement(worker, "InvokeNamedAgentRuntimesOnly")
    assert len(runtimes["Resource"]) == 3

    downstream = _statement(worker, "InvokeCompilerAndSenderOnly")
    rendered = json.dumps(downstream["Resource"])
    # both by Fn::ImportValue of the actual Function resources (review P2-2)
    assert "CompilerFunction" in rendered
    assert "SenderFunction" in rendered
    assert "commitment-watcher" not in rendered and "WatcherLiveAlias" not in rendered


@pytest.mark.parametrize(
    ("directory", "role_prefix"),
    [("compiler", "CompilerRole"), ("commitment_watcher", "WatcherRole")],
)
def test_the_compiler_and_watcher_invoke_no_lambda(directory: str, role_prefix: str) -> None:
    statements = _statements(_template(directory), role_prefix)
    assert "lambda:InvokeFunction" not in _allow_actions(statements)


def test_the_sender_invokes_only_the_compiler() -> None:
    sender = _statements(_templates()["AmbientChorusSender"], "SenderRole")
    grant = _statement(sender, "InvokeCompilerFenceOperationOnly")
    assert grant["Action"] == "lambda:InvokeFunction"
    # an Fn::ImportValue of the compiler's actual Function resource, not a literal (review P2-2)
    rendered = json.dumps(grant["Resource"])
    assert "Fn::ImportValue" in rendered and "CompilerFunction" in rendered
    # only that one Lambda invoke grant
    invokes = [i for i in sender if "lambda:InvokeFunction" in json.dumps(i.get("Action"))]
    assert len(invokes) == 1


# -- SS 45: the HTTP API, payload format 2.0 -----------------------------------------


def test_the_http_api_is_an_http_protocol_v2_proxy_to_the_api_lambda() -> None:
    template = _templates()["AmbientChorusApplication"]
    template.resource_count_is("AWS::ApiGatewayV2::Api", 1)
    api = next(iter(template.find_resources("AWS::ApiGatewayV2::Api").values()))
    assert api["Properties"]["ProtocolType"] == "HTTP"

    api_fn_logical, _ = _function("api")
    integration = next(iter(template.find_resources("AWS::ApiGatewayV2::Integration").values()))[
        "Properties"
    ]
    assert integration["IntegrationType"] == "AWS_PROXY"
    assert integration["PayloadFormatVersion"] == "2.0"
    assert integration["IntegrationUri"]["Fn::GetAtt"][0] == api_fn_logical

    routes = [r["Properties"] for r in template.find_resources("AWS::ApiGatewayV2::Route").values()]
    assert any(r["RouteKey"] == "$default" for r in routes)

    stages = [r["Properties"] for r in template.find_resources("AWS::ApiGatewayV2::Stage").values()]
    assert len(stages) == 1
    assert stages[0]["StageName"] == "$default"
    assert stages[0]["AutoDeploy"] is True


def test_api_gateway_may_invoke_only_the_api_lambda() -> None:
    template = _templates()["AmbientChorusApplication"]
    api_fn_logical, _ = _function("api")
    permissions = [
        p["Properties"] for p in template.find_resources("AWS::Lambda::Permission").values()
    ]
    gateway = [p for p in permissions if p["Principal"] == "apigateway.amazonaws.com"]
    assert gateway
    for permission in gateway:
        assert permission["FunctionName"]["Fn::GetAtt"][0] == api_fn_logical
        assert "execute-api" in json.dumps(permission["SourceArn"])


def test_no_function_url_is_created() -> None:
    for template in _templates().values():
        assert template.find_resources("AWS::Lambda::Url") == {}


# -- SS 46: the watcher :live alias is one identity ---------------------------------


def test_the_watcher_function_version_and_alias_are_one_chain() -> None:
    template = _templates()["AmbientChorusWatcher"]
    fn_logical, _ = _function("commitment_watcher")

    version_logical, version = next(iter(template.find_resources("AWS::Lambda::Version").items()))
    assert version["Properties"]["FunctionName"]["Ref"] == fn_logical

    alias_logical, alias = next(iter(template.find_resources("AWS::Lambda::Alias").items()))
    assert alias["Properties"]["Name"] == "live"
    assert alias["Properties"]["FunctionName"]["Ref"] == fn_logical
    assert alias["Properties"]["FunctionVersion"]["Fn::GetAtt"][0] == version_logical

    scheduler = _statements(template, "SchedulerExecutionRole")
    invoke = _statement(scheduler, "InvokeCommitmentWatcherLiveAliasOnly")
    assert invoke["Resource"] == {"Ref": alias_logical}


def test_the_api_watcher_grant_imports_the_same_alias_the_watcher_stack_exports() -> None:
    watcher_t = _templates()["AmbientChorusWatcher"]
    application_t = _templates()["AmbientChorusApplication"]

    alias_logical = next(iter(watcher_t.find_resources("AWS::Lambda::Alias")))

    api = _statements(application_t, "ApiRole")
    grant = _statement(api, "InvokeCommitmentWatcherLiveAliasOnly")
    resource = grant["Resource"]
    assert isinstance(resource, dict) and "Fn::ImportValue" in resource
    export_name = resource["Fn::ImportValue"]

    outputs = watcher_t.find_outputs("*")
    exported = [
        body for body in outputs.values() if body.get("Export", {}).get("Name") == export_name
    ]
    assert exported, f"no watcher export named {export_name}"
    assert alias_logical in json.dumps(exported[0]["Value"])
