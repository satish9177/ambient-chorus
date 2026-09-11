"""Case X, the IAM half: the Investigator's boundary, asserted from the synthesized template.

Isolation is an identity property, so it can be proved before anything is deployed. The
assertions mirror the Monitor's because the two roles are built by the same helper from the
same denied-action lists -- and the point of asserting both is that a future change to one
must not quietly loosen the other.

A static policy assertion is necessary and not sufficient. Post-deploy AccessDenied canaries
are the other half and belong to Phase 11.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from typing import Any

from aws_cdk import App, Stack, assertions
from infra.cdk.app import build_app
from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks import ChorusAgentStack
from infra.cdk.stacks.agents import (
    AGENTCORE_SERVICE_PRINCIPAL,
    DENIED_DATASTORE_ACTIONS,
    DENIED_EVIDENCE_OBJECT_ACTIONS,
    DENIED_OBJECT_MUTATION_ACTIONS,
    DENIED_SIDE_EFFECT_ACTIONS,
    INFERENCE_PROFILE_ARN_CONDITION_KEY,
    INVESTIGATOR_STATEMENT_IDS,
    MONITOR_STATEMENT_IDS,
    NOVA_2_LITE_BASE_MODEL_ID,
    US_INFERENCE_PROFILE_DESTINATION_REGIONS,
)

POLICY_TYPE = "AWS::IAM::Policy"
ROLE_TYPE = "AWS::IAM::Role"

MONITOR_PROFILE_ARN = (
    "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/chorus-monitor-a1b2c3d4"
)
INVESTIGATOR_PROFILE_ARN = (
    "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/chorus-investigator-e5f6"
)
ACTION_PROFILE_ARN = (
    "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/chorus-action-99887766"
)
PROFILE_ARNS = {
    "monitor_model_profile_arn": MONITOR_PROFILE_ARN,
    "investigator_model_profile_arn": INVESTIGATOR_PROFILE_ARN,
    "action_model_profile_arn": ACTION_PROFILE_ARN,
}


def template(*, artifact_bucket_arn: str | None = None) -> assertions.Template:
    app = App()
    stack = ChorusAgentStack(
        app,
        "TestAgents",
        config=CdkBuildConfig(),
        artifact_bucket_arn=artifact_bucket_arn,
    )
    return assertions.Template.from_stack(stack)


@cache
def runtime_template() -> assertions.Template:
    """The Agents stack as it synthesizes in the real app, where the profiles exist.

    Macro B made the three application inference profiles resources of this stack, created only
    when the VPC, subnets, security groups and artifact bucket are all supplied. The isolated
    ``template()`` above therefore has no model grant, so the model assertions read the real
    synthesis instead.
    """

    app = build_app(offline=True, context={"environment": "demo", "namespace": "DEMO"})
    stack = next(
        child
        for child in app.node.children
        if isinstance(child, Stack) and child.stack_name == "AmbientChorusAgents"
    )
    return assertions.Template.from_stack(stack)


def profile_attr(agent: str) -> dict[str, object]:
    """The generated ARN reference for one agent's application inference profile."""

    return {"Fn::GetAtt": [f"{agent.capitalize()}InferenceProfile", "InferenceProfileArn"]}


def statements(built: assertions.Template) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    for policy in built.find_resources(POLICY_TYPE).values():
        found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def statement(built: assertions.Template, sid: str) -> Mapping[str, Any]:
    return next(item for item in statements(built) if item.get("Sid") == sid)


def actions_of(built: assertions.Template, sid: str) -> set[str]:
    action = statement(built, sid)["Action"]
    return {action} if isinstance(action, str) else set(action)


def test_the_two_runtimes_have_separate_statement_identifiers() -> None:
    """One flattened policy document; two boundaries that must never be confused for one."""

    fields = (
        "invoke_profile",
        "invoke_foundation_models",
        "write_logs",
        "emit_traces",
        "read_artifact",
        "deny_data",
        "deny_evidence_objects",
        "deny_object_mutation",
        "deny_effects",
    )
    monitor = {getattr(MONITOR_STATEMENT_IDS, name) for name in fields}
    investigator = {getattr(INVESTIGATOR_STATEMENT_IDS, name) for name in fields}
    assert len(monitor) == len(investigator) == len(fields)
    assert monitor & investigator == set()


def test_the_investigator_role_is_assumable_only_by_the_agentcore_service() -> None:
    roles = template().find_resources(ROLE_TYPE)
    investigator = next(
        role
        for role in roles.values()
        if "investigator" in str(role["Properties"].get("RoleName", "")).lower()
    )
    principals = investigator["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert [item["Principal"]["Service"] for item in principals] == [AGENTCORE_SERVICE_PRINCIPAL]


def test_the_investigator_may_invoke_only_its_own_inference_profile() -> None:
    built = runtime_template()
    allowed = statement(built, INVESTIGATOR_STATEMENT_IDS.invoke_profile)
    # Macro B Chunk 2: exactly ONE model action. ``structured_output`` -> ``stream`` ->
    # ``converse_stream``, so ``bedrock:InvokeModel`` would be an unused grant.
    assert actions_of(built, INVESTIGATOR_STATEMENT_IDS.invoke_profile) == {
        "bedrock:InvokeModelWithResponseStream",
    }
    assert allowed["Resource"] == profile_attr("investigator")
    assert "MonitorInferenceProfile" not in str(allowed["Resource"])


def test_the_investigator_fm_grant_is_bound_to_its_own_profile_across_three_regions() -> None:
    built = runtime_template()
    fm = statement(built, INVESTIGATOR_STATEMENT_IDS.invoke_foundation_models)

    assert fm["Effect"] == "Allow"
    assert actions_of(built, INVESTIGATOR_STATEMENT_IDS.invoke_foundation_models) == {
        "bedrock:InvokeModelWithResponseStream",
    }
    assert set(fm["Resource"]) == {
        f"arn:aws:bedrock:{region}::foundation-model/{NOVA_2_LITE_BASE_MODEL_ID}"
        for region in US_INFERENCE_PROFILE_DESTINATION_REGIONS
    }
    assert all("111122223333" not in arn for arn in fm["Resource"])
    assert fm["Condition"]["StringEquals"][INFERENCE_PROFILE_ARN_CONDITION_KEY] == profile_attr(
        "investigator"
    )
    assert MONITOR_PROFILE_ARN not in str(fm)
    assert ACTION_PROFILE_ARN not in str(fm)


def test_the_investigator_is_denied_every_data_store() -> None:
    built = template()
    denied = statement(built, INVESTIGATOR_STATEMENT_IDS.deny_data)
    assert denied["Effect"] == "Deny"
    assert set(denied["Action"]) == set(DENIED_DATASTORE_ACTIONS)
    assert denied["Resource"] == "*"
    assert not any(str(a).startswith("s3:") for a in denied["Action"])


def test_the_investigator_evidence_object_deny_is_bucket_scoped() -> None:
    """SS 6: the ``GetObject`` deny names the evidence buckets, so the artifact prefix is free."""

    built = template()
    denied = statement(built, INVESTIGATOR_STATEMENT_IDS.deny_evidence_objects)
    assert denied["Effect"] == "Deny"
    assert set(denied["Action"]) == set(DENIED_EVIDENCE_OBJECT_ACTIONS)
    rendered = str(denied["Resource"])
    assert "chorus-private-evidence" in rendered and "chorus-export-evidence" in rendered
    assert denied["Resource"] != "*"

    mutation = statement(built, INVESTIGATOR_STATEMENT_IDS.deny_object_mutation)
    assert mutation["Resource"] == "*"
    assert set(mutation["Action"]) == set(DENIED_OBJECT_MUTATION_ACTIONS)
    assert "s3:GetObject" not in mutation["Action"]


def test_the_investigator_reads_its_own_artifact_with_no_deny_overriding_it() -> None:
    """ALLOW own artifact object; DENY another runtime's (no allow names it)."""

    built = template(artifact_bucket_arn="arn:aws:s3:::chorus-artifacts")
    allow = statement(built, INVESTIGATOR_STATEMENT_IDS.read_artifact)
    assert allow["Resource"] == "arn:aws:s3:::chorus-artifacts/investigator/*"

    for item in statements(built):
        if item["Effect"] != "Deny":
            continue
        acts = item["Action"] if isinstance(item["Action"], list) else [item["Action"]]
        if "s3:GetObject" in acts:
            assert "chorus-artifacts" not in str(item["Resource"])
            assert item["Resource"] != "*"


def test_the_investigator_is_denied_every_external_effect_and_every_other_agent() -> None:
    built = template()
    denied = statement(built, INVESTIGATOR_STATEMENT_IDS.deny_effects)
    assert denied["Effect"] == "Deny"
    assert set(denied["Action"]) == set(DENIED_SIDE_EFFECT_ACTIONS)
    assert "bedrock-agentcore:InvokeAgentRuntime" in set(denied["Action"])


def test_the_investigator_reads_only_its_own_artifact_prefix() -> None:
    built = template(artifact_bucket_arn="arn:aws:s3:::chorus-artifacts")
    allowed = statement(built, INVESTIGATOR_STATEMENT_IDS.read_artifact)
    assert allowed["Resource"] == "arn:aws:s3:::chorus-artifacts/investigator/*"


def test_the_investigator_has_its_own_log_group() -> None:
    groups = template().find_resources("AWS::Logs::LogGroup")
    names = {str(group["Properties"]["LogGroupName"]) for group in groups.values()}
    assert any("chorus-investigator" in name for name in names)
    # One dedicated group per agent runtime, three from Phase 7 on. A shared group would put
    # three isolated runtimes' telemetry behind one read permission.
    assert len(names) == 3


def test_neither_runtime_is_granted_a_data_action_anywhere() -> None:
    """The allow lists are asserted as a whole, not only the denies."""

    built = template(artifact_bucket_arn="arn:aws:s3:::chorus-artifacts")
    allowed: set[str] = set()
    for item in statements(built):
        if item.get("Effect") == "Allow":
            action = item["Action"]
            allowed.update({action} if isinstance(action, str) else action)
    forbidden = (
        set(DENIED_DATASTORE_ACTIONS)
        | set(DENIED_EVIDENCE_OBJECT_ACTIONS)
        | set(DENIED_OBJECT_MUTATION_ACTIONS)
    )
    # ``s3:GetObject`` is the one the artifact grant legitimately uses.
    assert allowed & forbidden - {"s3:GetObject"} == set()
    assert allowed & set(DENIED_SIDE_EFFECT_ACTIONS) == set()
