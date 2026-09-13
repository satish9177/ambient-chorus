"""The demo reset authority: a narrow role, no public route, exact DEMO grammar, usable S3/KMS.

Deployment contract §§ 12, 19-26, 39; review R3, R5. Reset is an operator action, not a
request-path route: the role is held by no application principal, every DynamoDB grant is
bounded to the **delimiter-aware** ``["NS#DEMO", "NS#DEMO#*"]`` grammar (not the old
``NS#DEMO*`` prefix that also matched ``NS#DEMO2`` / ``NS#DEMONSTRATION``), every S3 grant is
scoped to ``ns/DEMO/*``, and the reseed's SSE-KMS access names the private evidence key alone.
Read off the synthesized ``AmbientChorusReset`` template.
"""

from __future__ import annotations

import json
import re
from functools import cache
from typing import Any

from aws_cdk import App, Environment, assertions
from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks import ChorusDataStack
from infra.cdk.stacks.reset import (
    DEMO_NAMESPACE_LEADING_KEYS,
    ChorusResetStack,
    ResetBuckets,
    ResetTables,
)

POLICY = "AWS::IAM::Policy"


@cache
def reset_template() -> assertions.Template:
    app = App()
    config = CdkBuildConfig(environment="demo", namespace="DEMO")
    env = Environment(region="us-east-1")
    data = ChorusDataStack(app, "TestData", config=config, env=env)
    stack = ChorusResetStack(
        app,
        "AmbientChorusReset",
        config=config,
        env=env,
        tables=ResetTables(
            core=data.core_table, shareable=data.shareable_table, audit=data.audit_table
        ),
        buckets=ResetBuckets(
            private=data.private_evidence_bucket,
            export=data.export_evidence_bucket,
            private_key=data.private_evidence_key,
            export_key=data.export_evidence_key,
        ),
    )
    return assertions.Template.from_stack(stack)


def _statements() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for resource in reset_template().find_resources(POLICY).values():
        found.extend(resource["Properties"]["PolicyDocument"]["Statement"])
    return found


def _statement(sid: str) -> dict[str, Any]:
    for item in _statements():
        if item.get("Sid") == sid:
            return item
    raise AssertionError(f"no statement {sid}")


def _actions(item: dict[str, Any]) -> set[str]:
    action = item["Action"]
    return {action} if isinstance(action, str) else set(action)


def _allowed_actions() -> set[str]:
    granted: set[str] = set()
    for item in _statements():
        if item["Effect"] == "Allow":
            granted |= _actions(item)
    return granted


# -- the dedicated identity -------------------------------------------------------------------


def test_the_reset_role_function_and_log_group_are_dedicated() -> None:
    built = reset_template()
    built.has_resource_properties("AWS::IAM::Role", {"RoleName": "chorus-demo-reset-demo"})
    built.has_resource_properties(
        "AWS::Logs::LogGroup", {"LogGroupName": "/chorus/demo/demo-reset"}
    )
    functions = built.find_resources("AWS::Lambda::Function")
    assert len(functions) == 1
    props = next(iter(functions.values()))["Properties"]
    assert props["FunctionName"] == "chorus-demo-reset-demo"
    assert props["Handler"] == "functions.demo_reset.handler.handler"


# -- no public route (deployment contract §§ 19, 25) ---------------------------------------


def test_no_lambda_permission_admits_any_invoker() -> None:
    reset_template().resource_count_is("AWS::Lambda::Permission", 0)
    reset_template().resource_count_is("AWS::Lambda::Url", 0)
    reset_template().resource_count_is("AWS::ApiGateway::RestApi", 0)
    reset_template().resource_count_is("AWS::ApiGatewayV2::Api", 0)
    reset_template().resource_count_is("AWS::Scheduler::Schedule", 0)
    reset_template().resource_count_is("AWS::SNS::Subscription", 0)


# -- R3: the exact, delimiter-aware DEMO namespace grammar --------------------------------


def test_the_leading_keys_grammar_is_delimiter_aware_not_a_prefix() -> None:
    assert DEMO_NAMESPACE_LEADING_KEYS == ("NS#DEMO", "NS#DEMO#*")


def test_every_dynamodb_grant_uses_the_exact_demo_namespace_grammar() -> None:
    for item in _statements():
        if item["Effect"] != "Allow":
            continue
        if not any(a.startswith("dynamodb:") for a in _actions(item)):
            continue
        keys = (
            item.get("Condition", {})
            .get("ForAllValues:StringLike", {})
            .get("dynamodb:LeadingKeys", [])
        )
        assert keys == ["NS#DEMO", "NS#DEMO#*"], f"{item.get('Sid')} is not exact-DEMO bound"


def test_neighbouring_namespaces_are_not_matched_by_the_grammar() -> None:
    """``NS#DEMO2`` and ``NS#DEMONSTRATION`` match neither ``NS#DEMO`` nor ``NS#DEMO#*``; a
    valid DEMO partition key matches one of them. IAM ``StringLike`` treats ``*`` as the only
    wildcard, so this mirrors the policy evaluation."""

    def matches(key: str) -> bool:
        for pattern in DEMO_NAMESPACE_LEADING_KEYS:
            regex = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
            if re.match(regex, key):
                return True
        return False

    assert matches("NS#DEMO")
    assert matches("NS#DEMO#CLOCK")
    assert matches("NS#DEMO#CASE#abc")
    assert matches("NS#DEMO#COMM#c")
    assert not matches("NS#DEMO2")
    assert not matches("NS#DEMONSTRATION")
    assert not matches("NS#DEMO_OTHER")
    assert not matches("NS#OTHER")


def test_no_clock_grant_is_a_namespace_wildcard() -> None:
    for item in _statements():
        for block in ("ForAllValues:StringLike", "ForAnyValue:StringLike"):
            keys = item.get("Condition", {}).get(block, {}).get("dynamodb:LeadingKeys", [])
            for key in keys if isinstance(keys, list) else [keys]:
                assert not key.startswith("NS#*")


# -- R5: reset S3 / KMS access is actually usable ----------------------------------------


def test_every_s3_object_grant_is_scoped_to_the_demo_object_prefix() -> None:
    for item in _statements():
        if item["Effect"] != "Allow":
            continue
        actions = _actions(item)
        if not any(a.startswith("s3:") for a in actions):
            continue
        rendered = json.dumps(item["Resource"]) + json.dumps(item.get("Condition", {}))
        assert "ns/DEMO/" in rendered
        assert "s3:DeleteBucket" not in actions


def test_reset_may_read_reseed_and_bounded_delete_evidence_objects() -> None:
    grant = _statement("DemoObjectPrefixPrivate")
    assert _actions(grant) == {
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
    }
    assert "ns/DEMO/" in json.dumps(grant["Resource"])


def test_reset_holds_sse_kms_on_the_private_evidence_key_only() -> None:
    """Review R5-C: the reseed ``PutObject`` and verification ``GetObject`` need the private
    key's crypto through S3's server-side integration; the export key gets nothing."""

    grant = _statement("UsePrivateEvidenceKeyForReseed")
    assert _actions(grant) == {
        "kms:Decrypt",
        "kms:Encrypt",
        "kms:GenerateDataKey",
        "kms:DescribeKey",
    }
    rendered = json.dumps(grant["Resource"])
    assert "PrivateEvidenceKey" in rendered
    assert "ExportEvidenceKey" not in rendered
    assert "kms:*" not in _allowed_actions()


def test_no_kms_interface_endpoint_is_added_by_the_reset_stack() -> None:
    reset_template().resource_count_is("AWS::EC2::VPCEndpoint", 0)


# -- the rest of the bounded boundary -----------------------------------------------------


def test_the_reset_role_cannot_scan_delete_a_table_or_delete_a_bucket() -> None:
    granted = _allowed_actions()
    for forbidden in ("dynamodb:Scan", "dynamodb:DeleteTable", "s3:DeleteBucket"):
        assert forbidden not in granted
    deny = _statement("DenyDestructiveAccountOperations")
    assert deny["Effect"] == "Deny"
    assert {"dynamodb:Scan", "dynamodb:DeleteTable", "s3:DeleteBucket"} <= _actions(deny)


def test_the_scheduler_authority_is_delete_and_list_on_the_one_group_only() -> None:
    delete = _statement("DeleteDemoGroupSchedulesOnly")
    assert _actions(delete) == {"scheduler:DeleteSchedule", "scheduler:GetSchedule"}
    assert "chorus-demo/*" in json.dumps(delete["Resource"])
    assert "scheduler:CreateSchedule" not in _allowed_actions()
    assert "scheduler:UpdateSchedule" not in _allowed_actions()


def test_the_reset_role_holds_no_send_model_secret_or_invoke_authority() -> None:
    granted = _allowed_actions()
    assert not any(a.startswith(("ses:", "sesv2:", "bedrock")) for a in granted)
    assert "secretsmanager:GetSecretValue" not in granted
    assert "lambda:InvokeFunction" not in granted
    deny = _statement("DenyResetApplicationAuthority")
    assert deny["Effect"] == "Deny"
    assert {"secretsmanager:GetSecretValue", "lambda:InvokeFunction"} <= _actions(deny)


def test_the_reset_role_gets_no_managed_vpc_access_policy() -> None:
    for role in reset_template().find_resources("AWS::IAM::Role").values():
        assert "AWSLambdaVPCAccessExecutionRole" not in json.dumps(
            role["Properties"].get("ManagedPolicyArns", [])
        )
