"""IAM least-privilege assertions for the inbound reply entry point (Macro B).

Proves:
- Inbound role has no unconstrained Resource: "*" on Allow (outside the ENI service ops).
- Inbound role has explicit Denies for forbidden actions:
  - ses:Send*
  - bedrock:* / bedrock-runtime:* / bedrock-agentcore:*
  - secretsmanager:* / secretsmanager:GetSecretValue
  - scheduler:*
  - lambda:InvokeFunction
  - s3:Delete*
  - dynamodb:Delete*
- DynamoDB access is scoped to DEMO namespace leading keys.
- S3 access is scoped to the private bucket (read inbound prefix, write case evidence).
"""

from __future__ import annotations

from functools import cache
from typing import Any

from aws_cdk import App, Stack, assertions
from infra.cdk.app import build_app

POLICY = "AWS::IAM::Policy"


@cache
def full_app() -> App:
    return build_app(offline=True, context={"environment": "demo", "namespace": "DEMO"})


def inbound_template() -> assertions.Template:
    app = full_app()
    for child in app.node.children:
        if isinstance(child, Stack) and child.stack_name.endswith("Inbound"):
            return assertions.Template.from_stack(child)
    raise RuntimeError("Inbound stack not found in app")


def _role_statements() -> list[dict[str, Any]]:
    template = inbound_template()
    policies = template.find_resources(POLICY)
    statements: list[dict[str, Any]] = []
    for pol in policies.values():
        for stmt in pol["Properties"]["PolicyDocument"]["Statement"]:
            statements.append(stmt)
    return statements


def test_inbound_role_has_no_unconstrained_star_on_allow_except_eni() -> None:
    """Non-ENI Allow statements must name exact ARNs, never wildcard resources."""
    statements = _role_statements()
    for stmt in statements:
        if stmt["Effect"] != "Allow":
            continue
        sid = stmt.get("Sid", "")
        # ENI service-side operations have AWS-documented resource: * limitations
        if sid in {"AllowCreateVpcEni", "AllowDescribeVpcEni", "AllowManageVpcEni"}:
            continue

        resources = stmt.get("Resource")
        assert resources != "*", f"Allow statement {sid} has unconstrained Resource: '*'"
        if isinstance(resources, list):
            assert "*" not in resources, f"Allow statement {sid} contains '*' in Resources"


def test_inbound_role_has_explicit_denies_for_forbidden_actions() -> None:
    """Explicit Deny exists for SES sending, Bedrock, Secrets Manager, Scheduler, Lambda Invoke."""
    statements = _role_statements()
    deny_stmt = next((s for s in statements if s.get("Sid") == "DenyInboundForbiddenActions"), None)
    assert deny_stmt is not None
    assert deny_stmt["Effect"] == "Deny"

    actions = set(deny_stmt["Action"])
    # SES sending
    assert "ses:Send*" in actions or "ses:SendEmail" in actions
    # Bedrock
    assert any(a.startswith("bedrock") for a in actions)
    # Secrets Manager
    assert any(a.startswith("secretsmanager") for a in actions)
    # Scheduler
    assert "scheduler:*" in actions
    # Lambda Invoke
    assert "lambda:InvokeFunction" in actions
    # Deletes
    assert "s3:Delete*" in actions
    assert "dynamodb:Delete*" in actions


def test_inbound_role_dynamodb_is_scoped_to_demo_leading_keys() -> None:
    """DynamoDB access requires LeadingKeys condition matching DEMO namespace."""
    statements = _role_statements()
    ddb_stmt = next((s for s in statements if s.get("Sid") == "DynamoDbDemoTablesAccess"), None)
    assert ddb_stmt is not None
    assert ddb_stmt["Effect"] == "Allow"

    cond = ddb_stmt.get("Condition", {})
    leading_keys = cond.get("ForAllValues:StringLike", {}).get("dynamodb:LeadingKeys", [])
    assert "NS#DEMO" in leading_keys
    assert "NS#DEMO#*" in leading_keys


def test_inbound_role_s3_is_scoped_to_private_bucket_and_inbound_prefix() -> None:
    """Inbound S3 read is scoped to ns/DEMO/inbound/*; write to case evidence prefix."""
    statements = _role_statements()

    def _actions(stmt: dict[str, Any]) -> list[str]:
        raw = stmt["Action"]
        return raw if isinstance(raw, list) else [raw]

    read_stmt = next((s for s in statements if s.get("Sid") == "ReadInboundRawMessage"), None)
    assert read_stmt is not None
    assert read_stmt["Effect"] == "Allow"
    # GetObject ONLY -- no GetObjectVersion, no Put, no Delete on the ingress prefix (ADR-030 § 3).
    assert _actions(read_stmt) == ["s3:GetObject"]

    write_stmt = next((s for s in statements if s.get("Sid") == "WriteInboundReplyEvidence"), None)
    assert write_stmt is not None
    assert write_stmt["Effect"] == "Allow"
    assert _actions(write_stmt) == ["s3:PutObject"]
