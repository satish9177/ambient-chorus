"""The Action runtime's boundary, asserted from the synthesized template.

Isolation is an identity property, so it can be proved before anything is deployed. Every
assertion here is about the role: what it may do (invoke one inference profile, write its own
logs, emit its own traces, read its own artifact) and what it is explicitly denied.

The Action role is the one worth stating in full rather than by analogy. It is the agent that
drafts a message for somebody outside the community, so the interesting assertions are not that
it *has* no send permission but that a send action is explicitly denied -- and the same for
every data store, every secret, and every path to another agent.

A static policy assertion is necessary and not sufficient. Post-deploy ``AccessDenied`` canaries
are the other half and belong to Phase 11; this file is what keeps a regression from reaching
that point unnoticed.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from typing import Any

import pytest
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


def template(**kwargs: object) -> assertions.Template:
    """The Agents stack synthesized **alone**, with no network and no artifact bucket.

    Macro B made the three application inference profiles resources of this stack rather than
    context-supplied ARNs, and they -- like the runtimes -- are created only when the VPC,
    subnets, security groups and artifact bucket are all supplied. So an isolated synthesis has
    roles, log groups and every deny, but no profile and therefore no model grant. That is the
    right shape for the boundary assertions below, which are about what the role may *not* do;
    the model-grant assertions use :func:`runtime_template` instead.
    """

    app = App()
    stack = ChorusAgentStack(app, "TestAgents", config=CdkBuildConfig(), **kwargs)  # type: ignore[arg-type]
    return assertions.Template.from_stack(stack)


@cache
def runtime_template() -> assertions.Template:
    """The Agents stack as it synthesizes in the real app, where the profiles exist."""

    app = build_app(offline=True, context={"environment": "demo", "namespace": "DEMO"})
    stack = next(
        child
        for child in app.node.children
        if isinstance(child, Stack) and child.stack_name == "AmbientChorusAgents"
    )
    return assertions.Template.from_stack(stack)


def _statements_of(built: assertions.Template) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    for policy in built.find_resources(POLICY_TYPE).values():
        found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def statements() -> list[Mapping[str, Any]]:
    return _statements_of(template())


def statement(sid: str) -> Mapping[str, Any]:
    return next(item for item in statements() if item.get("Sid") == sid)


def runtime_statement(sid: str) -> Mapping[str, Any]:
    return next(item for item in _statements_of(runtime_template()) if item.get("Sid") == sid)


def actions_of(sid: str) -> set[str]:
    action = statement(sid)["Action"]
    return {action} if isinstance(action, str) else set(action)


def runtime_actions_of(sid: str) -> set[str]:
    action = runtime_statement(sid)["Action"]
    return {action} if isinstance(action, str) else set(action)


def profile_attr(agent: str) -> dict[str, Any]:
    """The generated ARN reference for one agent's application inference profile."""

    return {"Fn::GetAtt": [f"{agent.capitalize()}InferenceProfile", "InferenceProfileArn"]}


def test_the_action_role_exists_and_is_assumable_only_by_agentcore() -> None:
    roles = template().find_resources(ROLE_TYPE)
    action = next(
        item
        for item in roles.values()
        if "chorus-action-runtime" in str(item["Properties"].get("RoleName"))
    )
    document = action["Properties"]["AssumeRolePolicyDocument"]

    assert [item["Principal"] for item in document["Statement"]] == [
        {"Service": AGENTCORE_SERVICE_PRINCIPAL}
    ]


def test_the_role_may_invoke_only_its_own_inference_profile() -> None:
    allowed = runtime_statement("InvokeActionInferenceProfileOnly")

    assert allowed["Effect"] == "Allow"
    # Macro B Chunk 2: exactly ONE model action. ``structured_output`` -> ``stream`` ->
    # ``converse_stream`` (streaming defaults to True and no runtime disables it), so
    # ``ConverseStream`` is the only Bedrock call any runtime makes and
    # ``bedrock:InvokeModel`` would be an unused grant.
    assert runtime_actions_of("InvokeActionInferenceProfileOnly") == {
        "bedrock:InvokeModelWithResponseStream"
    }
    # The generated attribute of this stack's own profile resource -- never a name-built ARN.
    assert allowed["Resource"] == profile_attr("action")
    assert allowed["Resource"] != "*"


def test_the_role_cannot_invoke_another_agents_profile() -> None:
    """One profile per agent, for IAM and cost attribution both.

    A shared profile would make "the Action runtime ran" indistinguishable from "the
    Investigator ran" in the one place that is still true after a compromise.
    """

    profile = str(runtime_statement("InvokeActionInferenceProfileOnly")["Resource"])
    assert "MonitorInferenceProfile" not in profile
    assert "InvestigatorInferenceProfile" not in profile

    # The FM grant is condition-bound to the Action profile, never another agent's.
    fm = runtime_statement("InvokeActionFoundationModelsViaProfileOnly")
    bound = fm["Condition"]["StringEquals"][INFERENCE_PROFILE_ARN_CONDITION_KEY]
    assert bound == profile_attr("action")
    assert "MonitorInferenceProfile" not in str(fm)
    assert "InvestigatorInferenceProfile" not in str(fm)


def test_the_action_fm_grant_covers_the_three_frozen_us_regions_without_an_account() -> None:
    fm = runtime_statement("InvokeActionFoundationModelsViaProfileOnly")

    assert runtime_actions_of("InvokeActionFoundationModelsViaProfileOnly") == {
        "bedrock:InvokeModelWithResponseStream"
    }
    assert set(fm["Resource"]) == {
        f"arn:aws:bedrock:{region}::foundation-model/{NOVA_2_LITE_BASE_MODEL_ID}"
        for region in US_INFERENCE_PROFILE_DESTINATION_REGIONS
    }
    assert all("111122223333" not in arn for arn in fm["Resource"])


def test_the_role_writes_only_to_its_own_log_group() -> None:
    allowed = statement("WriteOwnActionLogsOnly")

    assert allowed["Effect"] == "Allow"
    assert set(allowed["Action"]) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams",
    }
    # The ARN is a CloudFormation reference rather than a literal, so the assertion is on the
    # construct it points at -- which is what proves the grant is scoped to *this* group.
    assert "ActionRuntimeLogGroup" in str(allowed["Resource"])


@pytest.mark.parametrize("action", sorted(DENIED_DATASTORE_ACTIONS))
def test_every_data_store_action_is_explicitly_denied(action: str) -> None:
    denied = statement("DenyEveryDataStoreForAction")

    assert denied["Effect"] == "Deny"
    assert denied["Resource"] == "*"
    assert action in denied["Action"]


@pytest.mark.parametrize("action", sorted(DENIED_SIDE_EFFECT_ACTIONS))
def test_every_external_effect_is_explicitly_denied(action: str) -> None:
    denied = statement("DenyEveryExternalEffectForAction")

    assert denied["Effect"] == "Deny"
    assert action in denied["Action"]


def test_the_role_can_never_read_core_shareable_or_audit() -> None:
    """One deny covers all three tables, because the grant is table-shaped and absent.

    The Action runtime is handed only external-safe data, and that is exactly why this matters:
    a runtime that could read the Shareable table could read *other cases'* views, and one that
    could read Core could read the private facts its own view was compiled to exclude.
    """

    denied = actions_of("DenyEveryDataStoreForAction")

    assert {
        "dynamodb:GetItem",
        "dynamodb:BatchGetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:TransactWriteItems",
    } <= denied


def test_the_role_can_never_read_or_write_either_evidence_bucket() -> None:
    """SS 6: the evidence ``GetObject`` deny is scoped to the two buckets, never to ``*``."""

    denied = statement("DenyEvidenceObjectAccessForAction")

    assert denied["Effect"] == "Deny"
    assert set(denied["Action"]) == set(DENIED_EVIDENCE_OBJECT_ACTIONS)
    assert {"s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"} <= set(
        denied["Action"]
    )
    rendered = str(denied["Resource"])
    assert "chorus-private-evidence" in rendered and "chorus-export-evidence" in rendered
    assert denied["Resource"] != "*"

    mutation = statement("DenyObjectMutationForAction")
    assert mutation["Resource"] == "*"
    assert set(mutation["Action"]) == set(DENIED_OBJECT_MUTATION_ACTIONS)
    assert "s3:GetObject" not in mutation["Action"]


def test_the_action_runtime_reads_its_own_artifact_and_no_deny_overrides_it() -> None:
    """ALLOW own artifact object; the deny split is what keeps it reachable (I8)."""

    document = template(artifact_bucket_arn="arn:aws:s3:::chorus-agent-artifacts").find_resources(
        POLICY_TYPE
    )
    items = [
        item
        for policy in document.values()
        for item in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    allow = next(item for item in items if item.get("Sid") == "ReadOwnActionArtifact")
    assert allow["Resource"] == "arn:aws:s3:::chorus-agent-artifacts/action/*"

    for item in items:
        if item["Effect"] != "Deny":
            continue
        acts = item["Action"] if isinstance(item["Action"], list) else [item["Action"]]
        if "s3:GetObject" in acts:
            assert item["Resource"] != "*"
            assert "chorus-agent-artifacts" not in str(item["Resource"])


def test_the_role_can_never_send_email() -> None:
    """The agent that drafts an external message cannot send one, as an IAM fact."""

    denied = actions_of("DenyEveryExternalEffectForAction")

    assert {
        "ses:SendEmail",
        "ses:SendRawEmail",
        "sesv2:SendEmail",
        "sesv2:SendBulkEmail",
    } <= denied


def test_the_role_can_never_invoke_the_compiler_the_sender_or_another_agent() -> None:
    denied = actions_of("DenyEveryExternalEffectForAction")

    assert {"lambda:InvokeFunction", "lambda:InvokeAsync"} <= denied
    assert "bedrock-agentcore:InvokeAgentRuntime" in denied


def test_the_role_can_never_read_a_secret_or_use_an_evidence_key() -> None:
    """No Secrets Manager access, which is what ADR-022 § 4 required of the sending identity.

    ``from_identity_id`` is deployment configuration held by the application, so nothing in the
    agent tier ever needs a secret to obtain it -- and the deny makes that a boundary rather
    than a current absence of need.
    """

    denied = actions_of("DenyEveryExternalEffectForAction")

    assert "secretsmanager:GetSecretValue" in denied
    assert {"kms:Decrypt", "kms:GenerateDataKey"} <= denied


def test_the_role_can_never_create_a_schedule() -> None:
    denied = actions_of("DenyEveryExternalEffectForAction")

    assert {
        "scheduler:CreateSchedule",
        "scheduler:UpdateSchedule",
        "scheduler:DeleteSchedule",
    } <= denied


def test_no_allow_statement_grants_the_action_role_a_data_or_send_action() -> None:
    """The positive sweep beside the explicit denies.

    A deny cannot be overridden, but an allow that somebody adds for a "temporary" reason is
    still worth catching where it is written rather than where it fails.
    """

    for item in statements():
        if item["Effect"] != "Allow":
            continue
        actions = item["Action"] if isinstance(item["Action"], list) else [item["Action"]]
        sid = item.get("Sid", "")
        for action in actions:
            assert not action.startswith("dynamodb:")
            assert not action.startswith("secretsmanager:")
            assert not action.startswith("ses")
            assert not action.startswith("s3:") or "Artifact" in str(sid)
            assert action != "*"


def test_the_artifact_grant_is_scoped_to_the_action_prefix_when_supplied() -> None:
    document = template(artifact_bucket_arn="arn:aws:s3:::chorus-agent-artifacts").find_resources(
        POLICY_TYPE
    )
    found = [
        item
        for policy in document.values()
        for item in policy["Properties"]["PolicyDocument"]["Statement"]
        if item.get("Sid") == "ReadOwnActionArtifact"
    ]

    assert found
    assert found[0]["Resource"] == "arn:aws:s3:::chorus-agent-artifacts/action/*"
    action = found[0]["Action"]
    assert (set(action) if isinstance(action, list) else {action}) == {"s3:GetObject"}


def test_the_runtime_log_group_is_dedicated_to_the_action_agent() -> None:
    template().has_resource_properties(
        "AWS::Logs::LogGroup",
        {"LogGroupName": "/aws/bedrock-agentcore/chorus-action-development"},
    )


def test_no_agentcore_runtime_resource_is_created() -> None:
    """Identity and boundary now; the runtime resource in Phase 11, with its VPC.

    Creating a public-mode runtime to have something to point at would contradict the network
    design it is supposed to satisfy, and a resource nobody deployed is not a proof of anything.
    """

    for resource_type in template().to_json().get("Resources", {}).values():
        assert "AgentRuntime" not in str(resource_type.get("Type", ""))
