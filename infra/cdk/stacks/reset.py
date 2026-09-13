"""The demo reset authority as an identity: one narrow role, one function, one log group.

Macro A's reset deliverable (deployment contract §§ 12, 19-26;
[ADR-029](../../../docs/adr/ADR-029-deployed-demo-clock-authority.md) § 3;
[11-frontend-and-demo.md](../../../docs/architecture/11-frontend-and-demo.md) § One-command
reset; [08-api-design.md](../../../docs/architecture/08-api-design.md) § Reset). Reset is an
**explicit operator action for deterministic demo restoration**, and the normal application
roles cannot perform it: the API role holds no ``s3:DeleteObject`` and no view-prefix
``DeleteItem``, and is denied ``scheduler:DeleteSchedule`` outright -- granting the request path
the ability to erase the system is exactly what a separate principal avoids.

What this stack creates
-----------------------
* ``chorus-demo-reset-{env}`` -- a dedicated Lambda role, held by no application principal;
* the dedicated reset function, VPC-attached to the isolated network's reset security group and
  the two isolated subnets, with its own inline ENI permissions (review R1: an unconditioned
  service-side Allow plus a ``SourceFunctionArn`` code-blocking Deny, no
  ``AWSLambdaVPCAccessExecutionRole``);
* the dedicated ``/chorus/{env}/demo-reset`` log group.

**No public route.** No API Gateway integration, no Function URL, no EventBridge/Scheduler
target, and no ``AWS::Lambda::Permission`` admitting any service principal (deployment contract
§§ 19, 25). The function's resource policy stays closed by default; the deploy/operator tooling
invokes it under a dedicated authorized identity that is not frozen here.

The bounded IAM boundary (deployment contract § 22; review R3, R5)
---------------------------------------------------------------
Every DynamoDB grant carries ``ForAllValues:StringLike dynamodb:LeadingKeys`` = the **exact,
delimiter-aware** DEMO namespace grammar ``["NS#DEMO", "NS#DEMO#*"]`` (review R3): the previous
``NS#DEMO*`` prefix also matched ``NS#DEMO2`` and ``NS#DEMONSTRATION`` -- neighbouring
namespaces -- and this shape does not. Every S3 grant is scoped to ``ns/DEMO/*``. The role
**cannot name a partition or prefix outside ``DEMO``**, in any table or bucket. It
queries-by-partition and bounded-deletes; it holds no ``dynamodb:Scan``, no ``DeleteTable``, no
``s3:DeleteBucket``, and no ``bucket.grant_read_write`` convenience grant. It may
``s3:PutObject`` under ``ns/DEMO/*`` (deterministic reseeding of the two fixture evidence
objects) and holds ``kms:Decrypt``/``Encrypt``/``GenerateDataKey`` on the **private** evidence
key alone -- the encryption context those SSE-KMS writes and read-backs need (review R5). The
export evidence key gets no grant: reset only deletes export objects, never reads or writes
them. Its scheduler authority is ``ListSchedules`` plus ``DeleteSchedule``/``GetSchedule`` on
the ``chorus-{env}`` group alone -- never ``CreateSchedule`` or ``UpdateSchedule``. It holds no
Secrets Manager, no Bedrock, no ``bedrock-agentcore``, no SES, and no ``lambda:InvokeFunction``.
The clock reseed rides on the Shareable DEMO-namespace grant, which ADR-029 § 2 and ``02`` §
IAM both place inside the reset principal's namespace authority; there is no ``NS#*#CLOCK*``
wildcard anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import CfnOutput, Environment, RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.cdk.config import CdkBuildConfig
from infra.cdk.lambda_support import (
    ResourceNames,
    chorus_lambda,
    demo_reset_environment,
    load_lambda_manifest,
)
from infra.cdk.network_support import vpc_eni_policy_statements

DEMO_NAMESPACE_LEADING_KEYS: tuple[str, ...] = ("NS#DEMO", "NS#DEMO#*")
"""The exact, delimiter-aware DEMO namespace grammar for every ``dynamodb:LeadingKeys`` scope
(``02`` § IAM, deployment contract § 12; review R3).

``NS#DEMO`` is the literal partition holding the community, the demo manifest, and the reset
lock; ``NS#DEMO#*`` is every entity partition under it (``NS#DEMO#COMM#…``, ``NS#DEMO#CASE#…``,
``NS#DEMO#FENCE#…``, ``NS#DEMO#OPERATION#…``, ``NS#DEMO#CLOCK``, …). ``NS#DEMO2`` and
``NS#DEMONSTRATION`` match **neither** entry -- the ``#`` delimiter is what makes this exact
rather than a prefix. This is not the ``NS#*#CLOCK*`` shape ADR-029 § 2 forbids; a template
test proves no namespace-wildcard clock grant exists on this role, and proves the neighbouring
namespaces are excluded."""

DEMO_OBJECT_PREFIX = "ns/DEMO/*"
"""Every S3 object grant's key scope, in both evidence buckets (deployment contract § 12,
§ 22): the reset principal touches only DEMO-owned object prefixes and lists only under them."""

DEMO_LIST_PREFIX = "ns/DEMO/"

RESET_READ_ACTIONS = ("dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query")
RESET_WRITE_ACTIONS = (
    "dynamodb:PutItem",
    "dynamodb:DeleteItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:ConditionCheckItem",
)
"""Query-by-partition then bounded delete, the ``DEMO_RESET_LOCK`` conditional acquire/release,
the ``DemoManifest`` writes, and the fenced clock reseed's conditional put. No
``dynamodb:UpdateItem`` (the storage driver has no attribute-level update path) and no
``dynamodb:Scan``."""

RESET_S3_ACTIONS = (
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:DeleteObjectVersion",
)
"""Read one object back to verify, deterministically reseed the two fixture evidence objects,
and delete DEMO-owned objects (including the un-admitted inbound ingress objects of ADR-030
§ 10.3). No bucket-policy or bucket-level mutation; ``s3:ListBucket`` is a separate,
prefix-constrained statement."""

RESET_KMS_ACTIONS = ("kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey")
"""What an SSE-KMS ``PutObject`` and a verification ``GetObject`` of a private evidence object
need through S3's server-side integration -- a data key to encrypt, decrypt to read back, and
``DescribeKey`` for the bucket-key path. Scoped to the **private** evidence key alone (review
R5-C); no ``kms:*``, and no KMS VPC endpoint (S3 performs the crypto integration)."""

RESET_SCHEDULER_DELETE_ACTIONS = ("scheduler:DeleteSchedule", "scheduler:GetSchedule")

DENIED_DESTRUCTIVE_ACTIONS = (
    "dynamodb:DeleteTable",
    "dynamodb:CreateTable",
    "dynamodb:Scan",
    "s3:DeleteBucket",
    "s3:PutBucketPolicy",
    "scheduler:CreateSchedule",
    "scheduler:UpdateSchedule",
)
"""Denied rather than merely ungranted (deployment contract § 22): reset never clears whole
account resources, deletes a table or a bucket, scans, or creates/updates a schedule."""

DENIED_APPLICATION_ACTIONS = (
    "ses:SendEmail",
    "ses:SendRawEmail",
    "sesv2:SendEmail",
    "bedrock:InvokeModel",
    "bedrock-agentcore:InvokeAgentRuntime",
    "secretsmanager:GetSecretValue",
    "lambda:InvokeFunction",
)
"""Reset is not an application principal: it sends nothing, asks no model, reads no secret, and
invokes no function. ``02`` § IAM marks SES ``D`` for the demo reset row."""


@dataclass(frozen=True, slots=True)
class ResetTables:
    """The three tables the reset principal's DEMO-namespace grants are scoped to."""

    core: dynamodb.ITable
    shareable: dynamodb.ITable
    audit: dynamodb.ITable


@dataclass(frozen=True, slots=True)
class ResetBuckets:
    """Both evidence buckets and the private key the reseed writes need (review R5).

    ``export_key`` is present only so a caller need not special-case it; the reset role gets
    **no** grant on it -- export objects are delete-only for reset, and a delete needs no KMS.
    """

    private: s3.IBucket
    export: s3.IBucket
    private_key: kms.IKey
    export_key: kms.IKey


class ChorusResetStack(Stack):
    """``AmbientChorusReset`` -- the dedicated reset function, role, and log group."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: CdkBuildConfig,
        tables: ResetTables,
        buckets: ResetBuckets,
        offline_synth: bool = True,
        vpc: ec2.IVpc | None = None,
        vpc_subnets: ec2.SubnetSelection | None = None,
        vpc_subnet_arns: list[str] | None = None,
        security_group: ec2.ISecurityGroup | None = None,
        env: Environment | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env)
        Tags.of(self).add("Project", config.project)
        Tags.of(self).add("Environment", config.environment)
        Tags.of(self).add("Namespace", config.namespace)
        Tags.of(self).add("DataClass", "PRIVATE")

        self.log_group = logs.LogGroup(
            self,
            "ResetLogGroup",
            log_group_name=f"/chorus/{config.environment}/demo-reset",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.role_name = f"chorus-demo-reset-{config.environment}"
        self.role = iam.Role(
            self,
            "ResetRole",
            role_name=self.role_name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Operator-only demo reset: NS#DEMO / NS#DEMO#* and ns/DEMO/ bounded, no send."
            ),
        )
        self.role_arn_literal = f"arn:aws:iam::{self.account}:role/{self.role_name}"

        self._grant_boundary(tables=tables, buckets=buckets, config=config)

        self.function_name = f"chorus-demo-reset-{config.environment}"
        self.function_arn_literal = (
            f"arn:aws:lambda:{config.aws_region}:{self.account}:function:{self.function_name}"
        )
        self.function = chorus_lambda(
            self,
            "ResetFunction",
            config=config,
            manifest=load_lambda_manifest("demo_reset"),
            role=self.role,
            environment=demo_reset_environment(
                config=config,
                names=ResourceNames.for_config(config),
                private_evidence_key_arn=buckets.private_key.key_arn,
                export_evidence_key_arn=buckets.export_key.key_arn,
            ),
            log_group=self.log_group,
            offline_synth=offline_synth,
            vpc=vpc,
            vpc_subnets=vpc_subnets,
            security_groups=[security_group] if security_group is not None else None,
        )
        self.function_arn = self.function.function_arn

        # VPC placement (deployment contract § 24): reset only needs DynamoDB / S3 (both gateway
        # endpoints) and the Scheduler interface endpoint, so putting it inside the isolated
        # network reuses the already-created endpoints without any broader access. Its own SG,
        # the exact isolated subnets, and its own inline ENI permissions under its own role.
        # The deterministic function ARN literal, not the ``Fn::GetAtt`` -- routing the reset
        # role's policy through its own function would be a ``role -> function -> role`` cycle.
        if vpc is not None and vpc_subnet_arns is not None:
            for eni_statement in vpc_eni_policy_statements(
                function_arn=self.function_arn_literal, subnet_arns=vpc_subnet_arns
            ):
                self.role.add_to_policy(eni_statement)

        CfnOutput(self, "ResetFunctionName", value=self.function.function_name)
        CfnOutput(self, "ResetFunctionArn", value=self.function.function_arn)
        CfnOutput(self, "ResetRoleArn", value=self.role.role_arn)

    def _grant_boundary(
        self, *, tables: ResetTables, buckets: ResetBuckets, config: CdkBuildConfig
    ) -> None:
        """Attach the complete bounded allow list and every explicit deny, in one place."""

        for label, table in (
            ("Core", tables.core),
            ("Shareable", tables.shareable),
            ("Audit", tables.audit),
        ):
            self.role.add_to_policy(
                iam.PolicyStatement(
                    sid=f"ReadWriteDemoNamespace{label}",
                    effect=iam.Effect.ALLOW,
                    actions=[*RESET_READ_ACTIONS, *RESET_WRITE_ACTIONS],
                    resources=[table.table_arn],
                    conditions={
                        "ForAllValues:StringLike": {
                            "dynamodb:LeadingKeys": list(DEMO_NAMESPACE_LEADING_KEYS)
                        }
                    },
                )
            )

        for label, bucket in (("Private", buckets.private), ("Export", buckets.export)):
            self.role.add_to_policy(
                iam.PolicyStatement(
                    sid=f"DemoObjectPrefix{label}",
                    effect=iam.Effect.ALLOW,
                    actions=(
                        list(RESET_S3_ACTIONS)
                        if label == "Private"
                        else ["s3:DeleteObject", "s3:DeleteObjectVersion"]
                    ),
                    resources=[bucket.arn_for_objects(DEMO_OBJECT_PREFIX)],
                )
            )
            # List is prefix-constrained: the role cannot enumerate anything outside ns/DEMO/.
            self.role.add_to_policy(
                iam.PolicyStatement(
                    sid=f"ListDemoObjectPrefix{label}",
                    effect=iam.Effect.ALLOW,
                    actions=["s3:ListBucket"],
                    resources=[bucket.bucket_arn],
                    conditions={"StringLike": {"s3:prefix": [f"{DEMO_LIST_PREFIX}*"]}},
                )
            )

        # SSE-KMS on the private evidence key -- the reseed's ``PutObject`` and the
        # verification ``GetObject`` both go through S3's server-side crypto integration
        # (review R5-C). The export key gets nothing: reset only deletes export objects.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="UsePrivateEvidenceKeyForReseed",
                effect=iam.Effect.ALLOW,
                actions=list(RESET_KMS_ACTIONS),
                resources=[buckets.private_key.key_arn],
            )
        )

        # ListSchedules is not resource-scopable; the destructive verbs are, to the one group.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ListSchedulesForDemoCleanup",
                effect=iam.Effect.ALLOW,
                actions=["scheduler:ListSchedules"],
                resources=["*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DeleteDemoGroupSchedulesOnly",
                effect=iam.Effect.ALLOW,
                actions=list(RESET_SCHEDULER_DELETE_ACTIONS),
                resources=[
                    self.format_arn(
                        service="scheduler",
                        resource="schedule",
                        resource_name=f"chorus-{config.environment}/*",
                    )
                ],
            )
        )

        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteOwnResetLogs",
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
                resources=[
                    self.log_group.log_group_arn,
                    f"{self.log_group.log_group_arn}:log-stream:*",
                ],
            )
        )

        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyDestructiveAccountOperations",
                effect=iam.Effect.DENY,
                actions=list(DENIED_DESTRUCTIVE_ACTIONS),
                resources=["*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyResetApplicationAuthority",
                effect=iam.Effect.DENY,
                actions=[*DENIED_APPLICATION_ACTIONS, "bedrock:*", "bedrock-agentcore:*", "ses:*"],
                resources=["*"],
            )
        )


__all__ = [
    "DEMO_NAMESPACE_LEADING_KEYS",
    "DEMO_OBJECT_PREFIX",
    "DENIED_APPLICATION_ACTIONS",
    "DENIED_DESTRUCTIVE_ACTIONS",
    "RESET_KMS_ACTIONS",
    "RESET_S3_ACTIONS",
    "RESET_SCHEDULER_DELETE_ACTIONS",
    "RESET_WRITE_ACTIONS",
    "ChorusResetStack",
    "ResetBuckets",
    "ResetTables",
]
