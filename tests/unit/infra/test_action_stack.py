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
from typing import Any

import pytest
from aws_cdk import App, assertions
from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks import ChorusAgentStack
from infra.cdk.stacks.agents import (
    AGENTCORE_SERVICE_PRINCIPAL,
    DENIED_DATA_PLANE_ACTIONS,
    DENIED_SIDE_EFFECT_ACTIONS,
)

POLICY_TYPE = "AWS::IAM::Policy"
ROLE_TYPE = "AWS::IAM::Role"


def template(**kwargs: object) -> assertions.Template:
    app = App()
    stack = ChorusAgentStack(app, "TestAgents", config=CdkBuildConfig(), **kwargs)  # type: ignore[arg-type]
    return assertions.Template.from_stack(stack)


def statements() -> list[Mapping[str, Any]]:
    policies = template().find_resources(POLICY_TYPE)
    found: list[Mapping[str, Any]] = []
    for policy in policies.values():
        found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def statement(sid: str) -> Mapping[str, Any]:
    return next(item for item in statements() if item.get("Sid") == sid)


def actions_of(sid: str) -> set[str]:
    action = statement(sid)["Action"]
    return {action} if isinstance(action, str) else set(action)


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
    allowed = statement("InvokeActionInferenceProfileOnly")

    assert allowed["Effect"] == "Allow"
    assert set(allowed["Action"]) == {
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
    }
    assert "application-inference-profile/chorus-action" in str(allowed["Resource"])
    assert allowed["Resource"] != "*"


def test_the_role_cannot_invoke_another_agents_profile() -> None:
    """One profile per agent, for IAM and cost attribution both.

    A shared profile would make "the Action runtime ran" indistinguishable from "the
    Investigator ran" in the one place that is still true after a compromise.
    """

    resource = str(statement("InvokeActionInferenceProfileOnly")["Resource"])

    assert "chorus-monitor" not in resource
    assert "chorus-investigator" not in resource


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


@pytest.mark.parametrize("action", sorted(DENIED_DATA_PLANE_ACTIONS))
def test_every_data_store_action_is_explicitly_denied(action: str) -> None:
    denied = statement("DenyEveryDataStoreForAction")

    assert denied["Effect"] == "Deny"
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
    denied = actions_of("DenyEveryDataStoreForAction")

    assert {"s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"} <= denied


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
