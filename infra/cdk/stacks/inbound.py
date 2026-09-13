"""The inbound reply entry point: SES receipt rule set, SNS topic, and isolated Lambda.

ADR-026, ADR-030, and Phase 11 deployment contract § 10. The entry point receives SNS
notifications from SES inbound receiving, verifies receipt verdicts, fetches raw MIME from the
ingress prefix in the private evidence bucket, parses and attests the reply, and ingests it as an
immutable private evidence item.

What this stack creates
-----------------------
1. An SNS topic (``chorus-{env}-inbound-receipt``) with a strict resource policy admitting only SES
   publish calls conditioned on the exact source account and full receipt-rule ARN.
2. An SES receipt rule set (``chorus-{env}-inbound``) and receipt rule (``chorus-{env}-reply``)
   with a single S3Action writing to ``ns/DEMO/inbound/`` and publishing to the SNS topic.
3. The six bucket-policy carve-out statements of ADR-030 § 7 added to the private evidence bucket.
4. The SES KMS key grant on the private evidence key.
5. The dedicated execution role for the inbound function, VPC-attached to isolated subnets with
   gateway S3/DynamoDB egress only, least-privilege allows, and explicit denies for SES send,
   Bedrock, AgentCore, Secrets Manager, Scheduler, Lambda invocation, S3 delete, and
   DynamoDB delete.
6. The ``chorus-inbound-{env}`` Lambda function and its SNS subscription.
"""

from __future__ import annotations

from typing import Final

from aws_cdk import CfnOutput, Environment, RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ses as ses
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as sns_subscriptions
from constructs import Construct

from infra.cdk.config import CdkBuildConfig, DeploymentIdentities
from infra.cdk.lambda_support import (
    ResourceNames,
    chorus_lambda,
    inbound_environment,
    load_lambda_manifest,
)
from infra.cdk.network_support import vpc_eni_policy_statements

DEMO_NAMESPACE_LEADING_KEYS: Final[tuple[str, ...]] = ("NS#DEMO", "NS#DEMO#*")
DEMO_INBOUND_PREFIX: Final = "ns/DEMO/inbound/"
DEFAULT_INBOUND_RECEIVING_ADDRESS: Final = "reply@inbound.demo.invalid"


class ChorusInboundStack(Stack):
    """``AmbientChorusInbound`` -- SES inbound receiving transport, SNS notification topic,
    and the inbound reply processing Lambda."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: CdkBuildConfig,
        private_evidence_bucket: s3.IBucket,
        private_evidence_key: kms.IKey,
        core_table: dynamodb.ITable,
        shareable_table: dynamodb.ITable,
        audit_table: dynamodb.ITable,
        vpc: ec2.IVpc,
        vpc_subnets: ec2.SubnetSelection,
        vpc_subnet_arns: list[str],
        security_group: ec2.ISecurityGroup,
        inbound_source_arn: str | None = None,
        inbound_receiving_address: str = DEFAULT_INBOUND_RECEIVING_ADDRESS,
        identities: DeploymentIdentities | None = None,
        offline_synth: bool = True,
        env: Environment | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env)
        Tags.of(self).add("Project", config.project)
        Tags.of(self).add("Environment", config.environment)
        Tags.of(self).add("Namespace", config.namespace)
        Tags.of(self).add("DataClass", "PRIVATE")

        account = config.account or ("000000000000" if offline_synth else self.account)
        rule_set_name = f"chorus-{config.environment}-inbound"
        rule_name = f"chorus-{config.environment}-reply"

        # Exact pinning: the FULL receipt-rule ARN, never the rule-set ARN (deployment contract
        # § 10.2; ADR-030 § 2). It is **built from this stack's own rule-set and rule names**, not
        # taken from ``identities.inbound_source_arn`` -- the ARN is a cross-stack *output* of the
        # rule this stack creates, not an operator prerequisite (deployment contract § 16 stage
        # 10). Every resource policy below and ``CHORUS_INBOUND_SOURCE_ARN`` name this one string,
        # so the rule that exists and the rule the policies authorize are the same rule by
        # construction. ``inbound_source_arn``, when supplied, is only asserted to agree.
        self.rule_arn = (
            f"arn:aws:ses:{config.aws_region}:{account}:receipt-rule-set/"
            f"{rule_set_name}:receipt-rule/{rule_name}"
        )
        if inbound_source_arn and inbound_source_arn != self.rule_arn and not offline_synth:
            raise ValueError(
                "inbound_source_arn was supplied and does not match this stack's own receipt "
                f"rule ARN {self.rule_arn!r}; drop the override or fix the deploy config"
            )

        # -- SNS notification topic with strict resource policy -------------------------------
        self.topic = sns.Topic(
            self,
            "InboundReceiptTopic",
            topic_name=f"chorus-{config.environment}-inbound-receipt",
        )

        # Admits ses.amazonaws.com only, conditioned on aws:SourceAccount and aws:SourceArn
        self.topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowSesPublishReceiptToInboundTopic",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("ses.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[self.topic.topic_arn],
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnEquals": {"aws:SourceArn": self.rule_arn},
                },
            )
        )
        self.topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenySesWrongOrMissingSourceAccount",
                effect=iam.Effect.DENY,
                principals=[iam.ServicePrincipal("ses.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[self.topic.topic_arn],
                conditions={"StringNotEqualsIfExists": {"aws:SourceAccount": account}},
            )
        )
        self.topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenySesWrongOrMissingReceiptRule",
                effect=iam.Effect.DENY,
                principals=[iam.ServicePrincipal("ses.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[self.topic.topic_arn],
                conditions={"ArnNotEqualsIfExists": {"aws:SourceArn": self.rule_arn}},
            )
        )
        self.topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyNonSesPublish",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["sns:Publish"],
                resources=[self.topic.topic_arn],
                conditions={
                    "StringNotEqualsIfExists": {"aws:PrincipalServiceName": "ses.amazonaws.com"}
                },
            )
        )

        # -- SES Receipt Rule Set and Rule (single S3Action with topicArn) --------------------
        self.receipt_rule_set = ses.CfnReceiptRuleSet(
            self,
            "InboundReceiptRuleSet",
            rule_set_name=rule_set_name,
        )

        self.receipt_rule = ses.CfnReceiptRule(
            self,
            "InboundReceiptRule",
            rule_set_name=rule_set_name,
            rule=ses.CfnReceiptRule.RuleProperty(
                name=rule_name,
                enabled=True,
                tls_policy="Require",
                scan_enabled=True,
                recipients=[inbound_receiving_address],
                actions=[
                    ses.CfnReceiptRule.ActionProperty(
                        s3_action=ses.CfnReceiptRule.S3ActionProperty(
                            bucket_name=private_evidence_bucket.bucket_name,
                            object_key_prefix=DEMO_INBOUND_PREFIX,
                            topic_arn=self.topic.topic_arn,
                        )
                    )
                ],
            ),
        )
        self.receipt_rule.add_dependency(self.receipt_rule_set)

        # -- Private evidence bucket policy additions (ADR-030 § 7 six statements) ------------
        private_evidence_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowSesReceiptWriteToInboundPrefixOnly",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("ses.amazonaws.com")],
                actions=["s3:PutObject"],
                resources=[f"{private_evidence_bucket.bucket_arn}/{DEMO_INBOUND_PREFIX}*"],
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnEquals": {"aws:SourceArn": self.rule_arn},
                },
            )
        )
        private_evidence_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyNonSesWrongOrMissingEncryption",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject"],
                resources=[f"{private_evidence_bucket.bucket_arn}/*"],
                conditions={
                    "StringNotEqualsIfExists": {
                        "aws:PrincipalServiceName": "ses.amazonaws.com",
                        "s3:x-amz-server-side-encryption": "aws:kms",
                    }
                },
            )
        )
        private_evidence_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyNonSesWrongOrMissingKmsKey",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject"],
                resources=[f"{private_evidence_bucket.bucket_arn}/*"],
                conditions={
                    "StringNotEqualsIfExists": {
                        "aws:PrincipalServiceName": "ses.amazonaws.com",
                        "s3:x-amz-server-side-encryption-aws-kms-key-id": (
                            private_evidence_key.key_arn
                        ),
                    }
                },
            )
        )
        private_evidence_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenySesWrongOrMissingSourceAccount",
                effect=iam.Effect.DENY,
                principals=[iam.ServicePrincipal("ses.amazonaws.com")],
                actions=["s3:PutObject"],
                resources=[f"{private_evidence_bucket.bucket_arn}/*"],
                conditions={"StringNotEqualsIfExists": {"aws:SourceAccount": account}},
            )
        )
        private_evidence_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenySesWrongOrMissingReceiptRule",
                effect=iam.Effect.DENY,
                principals=[iam.ServicePrincipal("ses.amazonaws.com")],
                actions=["s3:PutObject"],
                resources=[f"{private_evidence_bucket.bucket_arn}/*"],
                conditions={"ArnNotEqualsIfExists": {"aws:SourceArn": self.rule_arn}},
            )
        )
        private_evidence_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenySesOutsideInboundPrefix",
                effect=iam.Effect.DENY,
                principals=[iam.ServicePrincipal("ses.amazonaws.com")],
                actions=["s3:*"],
                not_resources=[f"{private_evidence_bucket.bucket_arn}/{DEMO_INBOUND_PREFIX}*"],
            )
        )

        # -- Private KMS key policy additions (SES generate data key + decrypt) --------------
        private_evidence_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowSesDecryptForInboundReceipt",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("ses.amazonaws.com")],
                actions=["kms:GenerateDataKey*", "kms:Decrypt"],
                resources=["*"],
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnEquals": {"aws:SourceArn": self.rule_arn},
                },
            )
        )

        # -- Logging: dedicated log group -----------------------------------------------------
        self.log_group = logs.LogGroup(
            self,
            "InboundLogGroup",
            log_group_name=f"/chorus/{config.environment}/inbound",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # -- Inbound Lambda execution role: least privilege + explicit denies -----------------
        self.role = iam.Role(
            self,
            "InboundRole",
            role_name=f"chorus-inbound-{config.environment}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="CHORUS inbound reply Lambda execution role.",
        )

        function_arn = self.format_arn(
            service="lambda",
            resource="function",
            resource_name=f"chorus-inbound-{config.environment}",
        )
        for statement in vpc_eni_policy_statements(
            function_arn=function_arn, subnet_arns=vpc_subnet_arns
        ):
            self.role.add_to_policy(statement)

        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteInboundLogs",
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[self.log_group.log_group_arn, f"{self.log_group.log_group_arn}:*"],
            )
        )

        # S3 read: ns/DEMO/inbound/* ONLY
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadInboundRawMessage",
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject"],
                resources=[f"{private_evidence_bucket.bucket_arn}/{DEMO_INBOUND_PREFIX}*"],
            )
        )

        # S3 write: .../community/*/case/*/reply/*/content ONLY
        reply_evidence_resources = [
            f"{private_evidence_bucket.bucket_arn}/ns/DEMO/community/*/case/*/reply/*/content"
        ]
        if config.namespace != "DEMO":
            reply_evidence_resources.insert(
                0,
                f"{private_evidence_bucket.bucket_arn}/ns/{config.namespace}/community/*/case/*/reply/*/content",
            )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteInboundReplyEvidence",
                effect=iam.Effect.ALLOW,
                actions=["s3:PutObject"],
                resources=reply_evidence_resources,
            )
        )

        # KMS: private evidence key ONLY
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="PrivateEvidenceKmsAccess",
                effect=iam.Effect.ALLOW,
                actions=[
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                    "kms:GenerateDataKeyWithoutPlaintext",
                ],
                resources=[private_evidence_key.key_arn],
            )
        )

        # DynamoDB: Core, Shareable, Audit scoped to leading keys ["NS#DEMO", "NS#DEMO#*"]
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DynamoDbDemoTablesAccess",
                effect=iam.Effect.ALLOW,
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:BatchGetItem",
                    "dynamodb:Query",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:ConditionCheckItem",
                ],
                resources=[
                    core_table.table_arn,
                    shareable_table.table_arn,
                    audit_table.table_arn,
                ],
                conditions={
                    "ForAllValues:StringLike": {
                        "dynamodb:LeadingKeys": list(DEMO_NAMESPACE_LEADING_KEYS)
                    }
                },
            )
        )

        # Explicit Denies. An action wildcard inside a Deny is the strongest form (deployment
        # contract § 8.7) -- the inbound path drafts nothing, invokes nothing, and reads no
        # secret, and each entry here says so in the one place a grant could otherwise appear.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyInboundForbiddenActions",
                effect=iam.Effect.DENY,
                actions=[
                    "ses:Send*",
                    "sesv2:Send*",
                    "bedrock:*",
                    "bedrock-agentcore:*",
                    "secretsmanager:*",
                    "scheduler:*",
                    "lambda:InvokeFunction",
                    "s3:Delete*",
                    "dynamodb:Delete*",
                    "dynamodb:Scan",
                ],
                resources=["*"],
            )
        )

        # -- Inbound Lambda Function ----------------------------------------------------------
        manifest = load_lambda_manifest("inbound_mail")
        names = ResourceNames.for_config(config)
        env_vars = inbound_environment(
            config=config,
            names=names,
            private_evidence_key_arn=private_evidence_key.key_arn,
            inbound_source_arn=self.rule_arn,
            inbound_receiving_address=inbound_receiving_address,
            inbound_topic_arn=self.topic.topic_arn,
        )

        self.function = chorus_lambda(
            self,
            "InboundFunction",
            config=config,
            manifest=manifest,
            role=self.role,
            environment=env_vars,
            log_group=self.log_group,
            offline_synth=offline_synth,
            vpc=vpc,
            vpc_subnets=vpc_subnets,
            security_groups=[security_group],
        )

        # SNS -> Lambda subscription
        self.topic.add_subscription(sns_subscriptions.LambdaSubscription(self.function))

        # -- Outputs --------------------------------------------------------------------------
        CfnOutput(self, "InboundReceiptTopicArn", value=self.topic.topic_arn)
        CfnOutput(self, "InboundReceiptRuleArn", value=self.rule_arn)
        CfnOutput(self, "InboundFunctionArn", value=self.function.function_arn)
        CfnOutput(self, "InboundExecutionRoleArn", value=self.role.role_arn)


__all__ = [
    "DEFAULT_INBOUND_RECEIVING_ADDRESS",
    "DEMO_INBOUND_PREFIX",
    "DEMO_NAMESPACE_LEADING_KEYS",
    "ChorusInboundStack",
]
