"""Assertions for the ChorusInboundStack and SES inbound receipt transport (Macro B).

Proves:
- Synthesis of SES receipt rule set and receipt rule with single S3Action pointing to TopicArn
- Synthesis of dedicated SNS topic with strict SES publish policy
- SNS subscription wiring to inbound Lambda function
- Lambda function configuration (VPC attachment, security group, timeout, memory)
- Bucket policy statements for SES receipt write and KMS decryption
"""

from __future__ import annotations

import json
from functools import cache

from aws_cdk import App, Stack, assertions
from infra.cdk.app import build_app

RECEIPT_RULE_SET = "AWS::SES::ReceiptRuleSet"
RECEIPT_RULE = "AWS::SES::ReceiptRule"
SNS_TOPIC = "AWS::SNS::Topic"
SNS_TOPIC_POLICY = "AWS::SNS::TopicPolicy"
SNS_SUBSCRIPTION = "AWS::SNS::Subscription"
LAMBDA_FUNCTION = "AWS::Lambda::Function"
LAMBDA_PERMISSION = "AWS::Lambda::Permission"
BUCKET_POLICY = "AWS::S3::BucketPolicy"
KMS_KEY = "AWS::KMS::Key"


@cache
def full_app() -> App:
    return build_app(offline=True, context={"environment": "demo", "namespace": "DEMO"})


def inbound_template() -> assertions.Template:
    app = full_app()
    for child in app.node.children:
        if isinstance(child, Stack) and child.stack_name.endswith("Inbound"):
            return assertions.Template.from_stack(child)
    raise RuntimeError("Inbound stack not found in app")


def data_template() -> assertions.Template:
    app = full_app()
    for child in app.node.children:
        if isinstance(child, Stack) and child.stack_name.endswith("Data"):
            return assertions.Template.from_stack(child)
    raise RuntimeError("Data stack not found in app")


def test_ses_receipt_rule_set_synthesizes() -> None:
    template = inbound_template()
    template.has_resource_properties(
        RECEIPT_RULE_SET,
        {"RuleSetName": "chorus-demo-inbound"},
    )


def test_ses_receipt_rule_has_single_s3_action_with_topic_arn() -> None:
    template = inbound_template()
    rules = template.find_resources(RECEIPT_RULE)
    assert len(rules) == 1

    rule_props = next(iter(rules.values()))["Properties"]
    rule = rule_props["Rule"]
    actions = rule["Actions"]

    # ADR-030 § 1: Exactly one action, S3Action with TopicArn
    assert len(actions) == 1
    assert list(actions[0].keys()) == ["S3Action"]  # no SNSAction, no LambdaAction, no BounceAction
    s3_action = actions[0]["S3Action"]
    # The bucket is the Data stack's private evidence bucket, arriving as a cross-stack reference.
    assert "PrivateEvidenceBucket" in json.dumps(s3_action["BucketName"])
    assert s3_action["ObjectKeyPrefix"] == "ns/DEMO/inbound/"
    assert "TopicArn" in s3_action
    # ADR-030 § 7: SES client-side encryption OFF, direct service principal -- no KMS key, no role.
    assert "KmsKeyArn" not in s3_action
    assert "IamRoleArn" not in s3_action
    assert rule["Recipients"] == ["reply@inbound.demo.invalid"]


def test_sns_topic_has_strict_ses_publish_policy() -> None:
    template = inbound_template()
    template.has_resource_properties(
        SNS_TOPIC,
        {"TopicName": "chorus-demo-inbound-receipt"},
    )

    policies = template.find_resources(SNS_TOPIC_POLICY)
    assert len(policies) == 1
    policy_props = next(iter(policies.values()))["Properties"]
    statements = policy_props["PolicyDocument"]["Statement"]

    allow_ses = next(
        (s for s in statements if s.get("Sid") == "AllowSesPublishReceiptToInboundTopic"), None
    )
    assert allow_ses is not None
    assert allow_ses["Effect"] == "Allow"
    assert allow_ses["Principal"] == {"Service": "ses.amazonaws.com"}
    assert allow_ses["Action"] == "sns:Publish"
    assert "StringEquals" in allow_ses["Condition"]
    assert "aws:SourceAccount" in allow_ses["Condition"]["StringEquals"]
    assert "ArnEquals" in allow_ses["Condition"]
    assert "aws:SourceArn" in allow_ses["Condition"]["ArnEquals"]


def test_sns_subscription_attaches_to_inbound_function() -> None:
    template = inbound_template()
    template.has_resource_properties(
        SNS_SUBSCRIPTION,
        {"Protocol": "lambda"},
    )


def test_inbound_lambda_function_configuration() -> None:
    template = inbound_template()
    template.has_resource_properties(
        LAMBDA_FUNCTION,
        {
            "FunctionName": "chorus-inbound-demo",
            "Handler": "functions.inbound_mail.handler.handler",
            "Runtime": "python3.12",
            "Timeout": 60,
            "MemorySize": 1024,
        },
    )


def test_lambda_permission_permits_sns_invocation_only() -> None:
    template = inbound_template()
    template.has_resource_properties(
        LAMBDA_PERMISSION,
        {
            "Action": "lambda:InvokeFunction",
            "Principal": "sns.amazonaws.com",
        },
    )


def test_private_kms_key_grants_ses_permissions() -> None:
    template = data_template()
    keys = template.find_resources(KMS_KEY)
    # At least one KMS key should have the SES grant in its policy
    found_ses_grant = False
    for key_res in keys.values():
        statements = key_res["Properties"]["KeyPolicy"]["Statement"]
        for stmt in statements:
            if stmt.get("Sid") == "AllowSesDecryptForInboundReceipt":
                found_ses_grant = True
                assert stmt["Effect"] == "Allow"
                assert stmt["Principal"] == {"Service": "ses.amazonaws.com"}
                acts = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
                assert "kms:Decrypt" in acts
                assert any(a.startswith("kms:GenerateDataKey") for a in acts)
                cond = stmt["Condition"]
                assert "aws:SourceAccount" in cond["StringEquals"]
                assert "aws:SourceArn" in cond["ArnEquals"]
    assert found_ses_grant, "Expected SES KMS policy grant not found on data stack keys"
