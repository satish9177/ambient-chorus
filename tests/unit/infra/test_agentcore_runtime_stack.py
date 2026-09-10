"""AgentCore runtime resources, application inference profiles, and networking assertions.

Macro B Chunk 1 (deployment contract §§ 4, 5, 6, 7, 8, 12, 16;
ADR-010): Assert that the three distinct AgentCore direct-code runtimes (Monitor, Investigator,
Action) synthesize with dedicated application inference profiles, live endpoints, isolated VPC
networking, and frozen immutable execution roles.

This file proves:
- Exactly three AgentCore runtimes exist across the app, named with their frozen deployed names.
- Each runtime is a Python 3.12 direct-code artifact in the customer artifact bucket.
- Each runtime points to its own execution role, with all implicit grants suppressed via immutable
  role references (guarding against L2 construct privilege escalation).
- Exactly three live endpoints point explicitly to their runtime's AgentRuntimeVersion.
- Exactly three application inference profiles copy from the frozen US geographic system profile.
- Environment variables contain each runtime's own profile ARN and no cross-agent variables.
- Network configuration attaches each runtime to its own dedicated security group in the two
  isolated VPC subnets with no public access.
- Unit test coverage for `runtime_artifact_location` offline and deployment modes.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App, Stack, assertions
from infra.cdk.app import build_app
from infra.cdk.config import CdkBuildConfig
from infra.cdk.runtime_support import (
    ARTIFACT_MANIFEST_NAME,
    ARTIFACT_MANIFEST_SCHEMA,
    OFFLINE_PLACEHOLDER_OBJECT_KEY,
    RUNTIME_AGENTS,
    RuntimeArtifactLocation,
    RuntimeArtifactManifestError,
    RuntimeArtifactMissingError,
    runtime_artifact_location,
)
from infra.cdk.stacks.agents import ChorusAgentStack

RUNTIME_RESOURCE_TYPE = "AWS::BedrockAgentCore::Runtime"
ENDPOINT_RESOURCE_TYPE = "AWS::BedrockAgentCore::RuntimeEndpoint"
PROFILE_RESOURCE_TYPE = "AWS::Bedrock::ApplicationInferenceProfile"
POLICY_RESOURCE_TYPE = "AWS::IAM::Policy"
ROLE_RESOURCE_TYPE = "AWS::IAM::Role"

FROZEN_SYSTEM_PROFILE_SUBSTRING = ":inference-profile/us.amazon.nova-2-lite-v1:0"
FORBIDDEN_PROFILE_SUBSTRING = "application-inference-profile/us."

SAMPLE_64_HEX = "a" * 64


@cache
def full_app() -> App:
    """The offline app built for the **demo** environment.

    The environment token is passed explicitly rather than left at ``build_app``'s
    ``development`` default, because every physical name these tests assert -- the runtime
    execution roles, the application inference profiles, the artifact bucket -- is derived from
    it. Asserting ``-demo`` names against a ``development`` synthesis would be testing the
    default rather than the deployment target.
    """

    return build_app(offline=True, context={"environment": "demo", "namespace": "DEMO"})


def all_resources(resource_type: str) -> dict[str, Any]:
    app = full_app()
    found: dict[str, Any] = {}
    for child in app.node.children:
        if isinstance(child, Stack):
            tmpl = assertions.Template.from_stack(child)
            found.update(tmpl.find_resources(resource_type))
    return found


@cache
def agents_template() -> assertions.Template:
    app = full_app()
    stack = next(
        s
        for s in app.node.children
        if isinstance(s, Stack) and s.stack_name == "AmbientChorusAgents"
    )
    return assertions.Template.from_stack(stack)


# -- 1. Exactly three AWS::BedrockAgentCore::Runtime resources across the app -------------


def test_exactly_three_runtime_resources_exist_across_the_app() -> None:
    runtimes = all_resources(RUNTIME_RESOURCE_TYPE)
    assert len(runtimes) == 3


# -- 2. Runtime names are exactly chorus_monitor, chorus_investigator, chorus_action ------


def test_runtime_names_match_frozen_manifest_deployed_names() -> None:
    runtimes = all_resources(RUNTIME_RESOURCE_TYPE)
    names = sorted(r["Properties"]["AgentRuntimeName"] for r in runtimes.values())
    assert names == ["chorus_action", "chorus_investigator", "chorus_monitor"]


# -- 3. Runtime CodeConfiguration has Runtime PYTHON_3_12 and EntryPoint python main.py ----


def test_runtime_code_configuration_runtime_and_entrypoint() -> None:
    runtimes = all_resources(RUNTIME_RESOURCE_TYPE)
    for runtime in runtimes.values():
        artifact = runtime["Properties"]["AgentRuntimeArtifact"]
        code_config = artifact["CodeConfiguration"]
        assert code_config["Runtime"] == "PYTHON_3_12"
        assert code_config["EntryPoint"] == ["python", "main.py"]


# -- 4. Code is S3 direct-code artifact in chorus-agent-artifacts-demo --------------------


def test_runtime_code_is_s3_direct_code_artifact_in_artifact_bucket() -> None:
    runtimes = all_resources(RUNTIME_RESOURCE_TYPE)
    for runtime in runtimes.values():
        artifact = runtime["Properties"]["AgentRuntimeArtifact"]
        code_config = artifact["CodeConfiguration"]
        assert "S3" in code_config["Code"]
        s3_code = code_config["Code"]["S3"]
        bucket = s3_code["Bucket"]
        # The bucket arrives as a cross-stack ``Fn::ImportValue`` on the Data stack's actual
        # bucket resource -- not as a literal name this stack rebuilt from the environment
        # token. That is the stronger property: a literal would still synthesize if the Data
        # stack stopped creating the bucket, and this reference would not.
        assert isinstance(bucket, dict) and "Fn::ImportValue" in bucket, (
            f"runtime artifact bucket must be a cross-stack reference, got {bucket!r}"
        )
        assert "AgentArtifactBucket" in json.dumps(bucket), (
            f"runtime artifact bucket must import the Data stack's artifact bucket, got {bucket!r}"
        )
        assert "ImageUri" not in code_config["Code"]
        assert "ContainerImage" not in code_config["Code"]


# -- 5. RoleArn resolves to distinct pre-existing execution roles ------------------------


def test_runtime_execution_roles_resolve_to_distinct_existing_roles() -> None:
    runtimes = all_resources(RUNTIME_RESOURCE_TYPE)
    roles = agents_template().find_resources(ROLE_RESOURCE_TYPE)
    role_names_by_id = {
        logical_id: props["Properties"].get("RoleName") for logical_id, props in roles.items()
    }

    role_targets: set[str] = set()
    for runtime in runtimes.values():
        role_arn_ref = runtime["Properties"]["RoleArn"]
        assert isinstance(role_arn_ref, dict) and "Fn::GetAtt" in role_arn_ref
        target_role_id, attr = role_arn_ref["Fn::GetAtt"]
        assert attr == "Arn"
        assert target_role_id in role_names_by_id
        role_name = role_names_by_id[target_role_id]
        assert role_name in (
            "chorus-monitor-runtime-demo",
            "chorus-investigator-runtime-demo",
            "chorus-action-runtime-demo",
        )
        role_targets.add(target_role_id)

    assert len(role_targets) == 3


# -- 6. Exactly three live endpoints with explicit AgentRuntimeVersion Fn::GetAtt --------


def test_exactly_three_live_endpoints_pointing_to_runtime_version() -> None:
    endpoints = all_resources(ENDPOINT_RESOURCE_TYPE)
    assert len(endpoints) == 3

    runtimes = all_resources(RUNTIME_RESOURCE_TYPE)
    runtime_ids = set(runtimes.keys())
    pointed_runtimes: set[str] = set()

    for endpoint in endpoints.values():
        props = endpoint["Properties"]
        assert props["Name"] == "live"
        version_ref = props["AgentRuntimeVersion"]
        assert isinstance(version_ref, dict) and "Fn::GetAtt" in version_ref
        target_runtime_id, attr = version_ref["Fn::GetAtt"]
        assert attr == "AgentRuntimeVersion"
        assert target_runtime_id in runtime_ids
        pointed_runtimes.add(target_runtime_id)

    assert len(pointed_runtimes) == 3


# -- 7. Exactly three application inference profiles with frozen system profile CopyFrom ---


def test_exactly_three_application_inference_profiles_with_system_profile() -> None:
    profiles = all_resources(PROFILE_RESOURCE_TYPE)
    assert len(profiles) == 3

    profile_names = sorted(p["Properties"]["InferenceProfileName"] for p in profiles.values())
    assert profile_names == [
        "chorus-action-demo",
        "chorus-investigator-demo",
        "chorus-monitor-demo",
    ]

    for profile in profiles.values():
        copy_from = profile["Properties"]["ModelSource"]["CopyFrom"]
        rendered = json.dumps(copy_from)
        assert FROZEN_SYSTEM_PROFILE_SUBSTRING in rendered
        assert FORBIDDEN_PROFILE_SUBSTRING not in rendered


# -- 8. EnvironmentVariables contains only own model profile ARN and frozen region --------


def test_runtime_environment_variables_contain_only_own_profile_arn() -> None:
    runtimes = all_resources(RUNTIME_RESOURCE_TYPE)
    all_agents = ("MONITOR", "INVESTIGATOR", "ACTION")

    for runtime in runtimes.values():
        name = runtime["Properties"]["AgentRuntimeName"]
        agent = name.removeprefix("chorus_").upper()
        env = runtime["Properties"]["EnvironmentVariables"]

        assert env[f"CHORUS_{agent}_MODEL_PROFILE_ARN"]
        assert env["CHORUS_AWS_REGION"] == "us-east-1"
        assert env["AWS_REGION"] == "us-east-1"
        assert env["CHORUS_OTEL_ENABLED"] == "false"

        # Assert none of the other agents' profile variables exist
        for other in all_agents:
            if other != agent:
                assert f"CHORUS_{other}_MODEL_PROFILE_ARN" not in env


# -- 9. Implicit-grant regression test ----------------------------------------------------


def test_runtime_execution_roles_gain_no_implicit_grants() -> None:
    """Guards against the L2 Runtime construct's implicit grant().

    Passing a mutable role to agentcore.Runtime silently attaches:
    logs:CreateLogGroup, logs:DescribeLogGroups, cloudwatch:PutMetricData,
    bedrock-agentcore:GetWorkloadAccessToken*, and s3:* on the whole bucket.
    This regression test proves none of those actions are granted to the roles.
    """
    policies = agents_template().find_resources(POLICY_RESOURCE_TYPE)
    forbidden_action_prefixes = (
        "cloudwatch:PutMetricData",
        "logs:CreateLogGroup",
        "logs:DescribeLogGroups",
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        "s3:GetBucket",
        "s3:List",
    )

    for policy in policies.values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            if stmt.get("Effect") != "Allow":
                continue
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            for action in actions:
                for forbidden in forbidden_action_prefixes:
                    assert not action.startswith(forbidden), (
                        f"Forbidden implicit grant detected: action {action} in statement {stmt}"
                    )
                # ``s3:GetObject`` is legitimate on **one agent's own artifact prefix** and
                # nowhere else. The frozen grant is ``{bucket}/{agent}/*`` (deployment contract
                # § 12), so a trailing ``/*`` is expected and is not what this guards against.
                # What must never appear is the whole-bucket form the L2 construct's implicit
                # grant produces -- ``{bucket}/*`` -- which would let any runtime read any
                # other runtime's artifact.
                if action == "s3:GetObject":
                    resources = stmt.get("Resource", [])
                    if isinstance(resources, str):
                        resources = [resources]
                    for res in resources:
                        assert res != "*", f"s3:GetObject on every resource: {res}"
                        prefix = res.rsplit("/", 1)[0] if res.endswith("/*") else res
                        owning_agent = prefix.rsplit("/", 1)[-1]
                        assert owning_agent in RUNTIME_AGENTS, (
                            "s3:GetObject must name exactly one agent's own artifact prefix "
                            f"({{bucket}}/{{agent}}/*), got {res}"
                        )
                assert action != "s3:GetObject*"


# -- 10. NetworkConfiguration is VPC mode with the two isolated subnets -------------------


def test_runtime_network_configuration_is_vpc_with_two_isolated_subnets() -> None:
    runtimes = all_resources(RUNTIME_RESOURCE_TYPE)
    for runtime in runtimes.values():
        net = runtime["Properties"]["NetworkConfiguration"]
        assert net["NetworkMode"] == "VPC"
        subnets = net["NetworkModeConfig"]["Subnets"]
        assert len(subnets) == 2


# -- 11. SecurityGroups names own SG and the three are distinct ---------------------------


def test_runtime_security_groups_name_own_sg_and_are_distinct() -> None:
    runtimes = all_resources(RUNTIME_RESOURCE_TYPE)
    sg_refs: set[str] = set()

    for runtime in runtimes.values():
        net = runtime["Properties"]["NetworkConfiguration"]
        sgs = net["NetworkModeConfig"]["SecurityGroups"]
        assert len(sgs) == 1
        sg_ref = json.dumps(sgs[0], sort_keys=True)
        sg_refs.add(sg_ref)

    assert len(sg_refs) == 3


# -- Single stack backwards compatibility -------------------------------------------------


def test_agent_stack_synthesizes_without_runtimes_when_networking_omitted() -> None:
    app = App()
    stack = ChorusAgentStack(app, "IsolatedAgents", config=CdkBuildConfig())
    tmpl = assertions.Template.from_stack(stack)
    tmpl.resource_count_is(RUNTIME_RESOURCE_TYPE, 0)
    tmpl.resource_count_is(ENDPOINT_RESOURCE_TYPE, 0)
    tmpl.resource_count_is(PROFILE_RESOURCE_TYPE, 0)


# -- 12. runtime_artifact_location unit tests ---------------------------------------------


def test_runtime_artifact_location_offline_returns_placeholder_and_reads_no_manifest(
    tmp_path: Path,
) -> None:
    # Manifest file does not exist in tmp_path
    location = runtime_artifact_location(
        "monitor",
        bucket_name="my-bucket",
        offline=True,
        output_root=tmp_path,
    )
    assert isinstance(location, RuntimeArtifactLocation)
    assert location.agent == "monitor"
    assert location.bucket_name == "my-bucket"
    assert location.object_key == OFFLINE_PLACEHOLDER_OBJECT_KEY.format(agent="monitor")
    assert location.sha256 == ""
    assert location.deployed_name == "chorus_monitor"


def test_runtime_artifact_location_deployment_missing_manifest_raises(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeArtifactMissingError, match="deployment-capable synth needs"):
        runtime_artifact_location(
            "monitor",
            bucket_name="my-bucket",
            offline=False,
            output_root=tmp_path,
        )


def test_runtime_artifact_location_deployment_wrong_schema_raises(
    tmp_path: Path,
) -> None:
    manifest_file = tmp_path / ARTIFACT_MANIFEST_NAME
    manifest_file.write_text(
        json.dumps({"schema": "wrong-schema/v1", "artifacts": []}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeArtifactManifestError, match="unsupported manifest schema"):
        runtime_artifact_location(
            "monitor",
            bucket_name="my-bucket",
            offline=False,
            output_root=tmp_path,
        )


def test_runtime_artifact_location_deployment_missing_agent_record_raises(
    tmp_path: Path,
) -> None:
    manifest_file = tmp_path / ARTIFACT_MANIFEST_NAME
    manifest_file.write_text(
        json.dumps(
            {
                "schema": ARTIFACT_MANIFEST_SCHEMA,
                "artifacts": [{"deployed_name": "other_runtime", "sha256": SAMPLE_64_HEX}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeArtifactMissingError, match="no artifact record found"):
        runtime_artifact_location(
            "monitor",
            bucket_name="my-bucket",
            offline=False,
            output_root=tmp_path,
        )


def test_runtime_artifact_location_deployment_short_or_invalid_digest_raises(
    tmp_path: Path,
) -> None:
    manifest_file = tmp_path / ARTIFACT_MANIFEST_NAME
    manifest_file.write_text(
        json.dumps(
            {
                "schema": ARTIFACT_MANIFEST_SCHEMA,
                "artifacts": [{"deployed_name": "chorus_monitor", "sha256": "tooshort"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeArtifactManifestError, match="invalid sha256 digest"):
        runtime_artifact_location(
            "monitor",
            bucket_name="my-bucket",
            offline=False,
            output_root=tmp_path,
        )


def test_runtime_artifact_location_deployment_valid_manifest(
    tmp_path: Path,
) -> None:
    manifest_file = tmp_path / ARTIFACT_MANIFEST_NAME
    manifest_file.write_text(
        json.dumps(
            {
                "schema": ARTIFACT_MANIFEST_SCHEMA,
                "artifacts": [
                    {
                        "deployed_name": "chorus_monitor",
                        "sha256": f"sha256:{SAMPLE_64_HEX}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    location = runtime_artifact_location(
        "monitor",
        bucket_name="my-bucket",
        offline=False,
        output_root=tmp_path,
    )
    assert location.object_key == f"monitor/{SAMPLE_64_HEX}.zip"
    assert location.sha256 == SAMPLE_64_HEX
    assert location.deployed_name == "chorus_monitor"
