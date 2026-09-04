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
from typing import Any

from aws_cdk import App, assertions
from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks import ChorusAgentStack
from infra.cdk.stacks.agents import (
    AGENTCORE_SERVICE_PRINCIPAL,
    DENIED_DATA_PLANE_ACTIONS,
    DENIED_SIDE_EFFECT_ACTIONS,
    INVESTIGATOR_STATEMENT_IDS,
    MONITOR_STATEMENT_IDS,
)

POLICY_TYPE = "AWS::IAM::Policy"
ROLE_TYPE = "AWS::IAM::Role"


def template(*, artifact_bucket_arn: str | None = None) -> assertions.Template:
    app = App()
    stack = ChorusAgentStack(
        app,
        "TestAgents",
        config=CdkBuildConfig(),
        artifact_bucket_arn=artifact_bucket_arn,
    )
    return assertions.Template.from_stack(stack)


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
        "write_logs",
        "emit_traces",
        "read_artifact",
        "deny_data",
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
    built = template()
    allowed = statement(built, INVESTIGATOR_STATEMENT_IDS.invoke_profile)
    assert set(allowed["Action"]) == {
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
    }
    resources = allowed["Resource"]
    rendered = str(resources)
    assert "chorus-investigator" in rendered
    assert "chorus-monitor" not in rendered


def test_the_investigator_is_denied_every_data_store() -> None:
    built = template()
    denied = statement(built, INVESTIGATOR_STATEMENT_IDS.deny_data)
    assert denied["Effect"] == "Deny"
    assert set(denied["Action"]) == set(DENIED_DATA_PLANE_ACTIONS)
    assert denied["Resource"] == "*"


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
    assert len(names) == 2


def test_neither_runtime_is_granted_a_data_action_anywhere() -> None:
    """The allow lists are asserted as a whole, not only the denies."""

    built = template(artifact_bucket_arn="arn:aws:s3:::chorus-artifacts")
    allowed: set[str] = set()
    for item in statements(built):
        if item.get("Effect") == "Allow":
            action = item["Action"]
            allowed.update({action} if isinstance(action, str) else action)
    assert allowed & set(DENIED_DATA_PLANE_ACTIONS) - {"s3:GetObject"} == set()
    assert allowed & set(DENIED_SIDE_EFFECT_ACTIONS) == set()
