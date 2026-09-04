"""The Monitor runtime's boundary, asserted from the synthesized template.

Isolation is an identity property, so it can be proved before anything is deployed. Every
assertion here is about the role: what it may do (invoke one inference profile, write its own
logs, emit its own traces) and what it is explicitly denied.

A static policy assertion is necessary and not sufficient. Post-deploy AccessDenied canaries
are the other half and belong to the deployment phase; this file is what keeps a regression
from reaching that point unnoticed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from aws_cdk import App, assertions
from infra.cdk.app import build_app
from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks import ChorusAgentStack
from infra.cdk.stacks.agents import (
    AGENTCORE_SERVICE_PRINCIPAL,
    DENIED_DATA_PLANE_ACTIONS,
    DENIED_SIDE_EFFECT_ACTIONS,
)

POLICY_TYPE = "AWS::IAM::Policy"
ROLE_TYPE = "AWS::IAM::Role"


def template(config: CdkBuildConfig | None = None) -> assertions.Template:
    app = App()
    stack = ChorusAgentStack(app, "TestAgents", config=config or CdkBuildConfig())
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


def test_the_stack_creates_exactly_one_role_per_agent_runtime() -> None:
    """Two roles from Phase 5 on: the Monitor's and the Investigator's, and nothing else.

    The count is asserted rather than the names, because the failure this catches is a *third*
    role appearing -- a shared "agents" role, or a convenience role attached during a later
    phase -- which is how two isolated identities quietly become one.
    """

    template().resource_count_is(ROLE_TYPE, 2)


def test_the_role_is_assumable_only_by_the_agentcore_service() -> None:
    roles = template().find_resources(ROLE_TYPE)
    document = next(iter(roles.values()))["Properties"]["AssumeRolePolicyDocument"]

    principals = [item["Principal"] for item in document["Statement"]]
    assert principals == [{"Service": AGENTCORE_SERVICE_PRINCIPAL}]


def test_the_role_may_invoke_only_its_own_inference_profile() -> None:
    allowed = statement("InvokeMonitorInferenceProfileOnly")

    assert allowed["Effect"] == "Allow"
    assert set(allowed["Action"]) == {
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
    }
    assert "application-inference-profile/chorus-monitor" in str(allowed["Resource"])
    assert allowed["Resource"] != "*"


def test_the_role_writes_only_to_its_own_log_group() -> None:
    allowed = statement("WriteOwnLogsOnly")

    assert allowed["Effect"] == "Allow"
    assert "*" not in [allowed["Resource"]]
    assert set(allowed["Action"]) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams",
    }


@pytest.mark.parametrize("action", sorted(DENIED_DATA_PLANE_ACTIONS))
def test_every_data_store_action_is_explicitly_denied(action: str) -> None:
    denied = statement("DenyEveryDataStore")

    assert denied["Effect"] == "Deny"
    assert action in denied["Action"]


@pytest.mark.parametrize("action", sorted(DENIED_SIDE_EFFECT_ACTIONS))
def test_every_external_effect_is_explicitly_denied(action: str) -> None:
    denied = statement("DenyEveryExternalEffect")

    assert denied["Effect"] == "Deny"
    assert action in denied["Action"]


def test_the_role_can_never_read_a_dynamodb_table() -> None:
    denied = actions_of("DenyEveryDataStore")

    assert {"dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"} <= denied


def test_the_role_can_never_read_or_write_either_evidence_bucket() -> None:
    denied = actions_of("DenyEveryDataStore")

    assert {"s3:GetObject", "s3:PutObject", "s3:ListBucket"} <= denied


def test_the_role_can_never_send_email() -> None:
    denied = actions_of("DenyEveryExternalEffect")

    assert {"ses:SendEmail", "ses:SendRawEmail", "sesv2:SendEmail"} <= denied


def test_the_role_can_never_invoke_the_compiler_the_sender_or_another_agent() -> None:
    denied = actions_of("DenyEveryExternalEffect")

    assert "lambda:InvokeFunction" in denied
    assert "bedrock-agentcore:InvokeAgentRuntime" in denied


def test_the_role_can_never_read_a_secret_or_use_an_evidence_key() -> None:
    denied = actions_of("DenyEveryExternalEffect")

    assert "secretsmanager:GetSecretValue" in denied
    assert {"kms:Decrypt", "kms:GenerateDataKey"} <= denied


def test_no_allow_statement_grants_a_wildcard_data_action() -> None:
    for item in statements():
        if item["Effect"] != "Allow":
            continue
        actions = item["Action"] if isinstance(item["Action"], list) else [item["Action"]]
        for action in actions:
            assert not action.startswith("dynamodb:")
            assert not action.startswith("s3:") or item.get("Sid") == "ReadOwnDirectCodeArtifact"
            assert not action.startswith("ses")
            assert action != "*"


def test_the_artifact_grant_is_absent_until_an_artifact_bucket_is_supplied() -> None:
    assert all(item.get("Sid") != "ReadOwnDirectCodeArtifact" for item in statements())


def test_the_artifact_grant_is_scoped_to_the_monitor_prefix_when_supplied() -> None:
    app = App()
    stack = ChorusAgentStack(
        app,
        "TestAgents",
        config=CdkBuildConfig(),
        artifact_bucket_arn="arn:aws:s3:::chorus-agent-artifacts",
    )
    document = assertions.Template.from_stack(stack).find_resources(POLICY_TYPE)
    found = [
        item
        for policy in document.values()
        for item in policy["Properties"]["PolicyDocument"]["Statement"]
        if item.get("Sid") == "ReadOwnDirectCodeArtifact"
    ]

    assert found
    assert found[0]["Resource"] == "arn:aws:s3:::chorus-agent-artifacts/monitor/*"


def test_the_runtime_log_group_is_dedicated_to_the_monitor() -> None:
    template().has_resource_properties(
        "AWS::Logs::LogGroup",
        {"LogGroupName": "/aws/bedrock-agentcore/chorus-monitor-development"},
    )


def test_the_application_synthesizes_the_agent_stack() -> None:
    assembly = build_app().synth()

    assert "AmbientChorusAgents" in [stack.stack_name for stack in assembly.stacks]
