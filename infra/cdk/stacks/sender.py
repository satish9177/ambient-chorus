"""The sender boundary as an identity: one role, one log group, one configuration set.

What this stack creates is the sender's *identity*, the policies that bound it, and the SES
configuration set its events are published through. That is the part the security argument rests
on, and it can be asserted from a synthesized template long before the Lambda exists -- the same
static-now split Phase 5 used for the agent runtimes, Phase 6 for the compiler, and Phase 7 for
the application. The deployed function, the live send, the verified identity, the sandbox exit,
and the post-deploy ``AccessDenied`` canaries belong to Phase 11.

Four grants are unusual enough to say out loud.

**The Shareable write reaches one partition prefix.** ``NS#*#EXECUTION#*`` and nothing else.
The execution used to share ``NS#n#ACTION#a`` with the immutable proposal and the immutable
approval, and ``dynamodb:LeadingKeys`` constrains the partition key while nothing constrains the
sort key -- so the narrowest grant that could write an execution also authorized overwriting the
message a human approved. A compromised sender could then have rendered its own replacement,
recomputed a matching digest, and passed every check in the chain
([ADR-024](../../../docs/adr/ADR-024-execution-partition-and-sender-boundary.md), T33).

**Core is denied in total.** Not merely ungranted -- denied, over the whole table. The sender
resolves its recipient from an allowlisted registry in configuration; it has no reason to read a
case, a fact, a mandate, or even the fence row, and it acquires the fence by invoking the
compiler's typed operation rather than by touching the item.

**No ``dynamodb:UpdateItem`` anywhere, and no blanket ``dynamodb:TransactWriteItems``.** Every
execution write is a conditional ``PutItem`` of the whole record, and AWS authorizes a
transaction through the permission each *participant* needs -- so the blanket action would be a
permission this role does not need and a place for a future participant to hide.

**The SES grant is ``ses:SendEmail`` and is deliberately not narrowed by ``ses:Recipients``.**
SES v2's ``SendEmail`` API authorizes under the ``ses:`` action name; the ``sesv2:`` entries in
the existing deny lists name no real IAM action and are inert, which is harmless in a deny and
would be a silent hole in an allow. Narrowing by recipient would put an email address into a
synthesized CloudFormation template -- a build artifact that gets read, diffed, and attached to
a pull request -- so the single-recipient rule is enforced in code against the registry instead,
and a static assertion fails the build if any address-shaped string appears in the template.
"""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ses as ses
from constructs import Construct

from infra.cdk.config import CdkBuildConfig

EXECUTION_KEY_PREFIX = "NS#*#EXECUTION#*"
"""The execution's own partition, and the only Shareable prefix this role may write."""

FORBIDDEN_WRITE_PREFIXES = (
    "NS#*#ACTION#*",
    "NS#*#ACTION_CURRENT#*",
    "NS#*#VIEW#*",
    "NS#*#VIEW_CURRENT#*",
    "NS#*#CASE#*",
)
"""Every Shareable prefix the sender must never write, denied by ``ForAnyValue``.

``ForAnyValue`` is deliberate and is the same choice ADR-022 made for the application's view
deny: a transaction naming *any* proposal- or approval-partition item alongside legitimate
execution items is refused whole, rather than permitted because most of its keys were
acceptable.
"""

READ_ACTIONS = ("dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query")

EXECUTION_WRITE_ACTIONS = ("dynamodb:PutItem", "dynamodb:ConditionCheckItem")
"""A conditional whole-record put, and read-only transactional authority. No update path."""

AUDIT_WRITE_ACTIONS = ("dynamodb:PutItem",)

DENIED_SHAREABLE_WRITE_ACTIONS = (
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
)

DENIED_MODEL_ACTIONS = (
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
    "bedrock-agentcore:InvokeAgentRuntime",
)
"""The sender transmits an already-approved message. It never asks anything what to say."""

DENIED_SCHEDULER_ACTIONS = (
    "scheduler:CreateSchedule",
    "scheduler:UpdateSchedule",
    "scheduler:DeleteSchedule",
)

SES_SEND_ACTION = "ses:SendEmail"
"""The real IAM action name. ``sesv2:SendEmail`` is not one and would grant nothing."""


@dataclass(frozen=True, slots=True)
class SenderTables:
    """The two tables the sender touches, and the one it is denied outright."""

    core: dynamodb.ITable
    shareable: dynamodb.ITable
    audit: dynamodb.ITable


@dataclass(frozen=True, slots=True)
class SenderBuckets:
    """Both evidence buckets, present here only so they can be denied."""

    private: s3.IBucket
    export: s3.IBucket


class ChorusSenderStack(Stack):
    """The sender's role, log group, and SES configuration set. No function is created here."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: CdkBuildConfig,
        tables: SenderTables,
        buckets: SenderBuckets,
        compiler_function_arn: str | None = None,
        destination_registry_secret_arn: str | None = None,
        ses_identity_arn: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id)
        Tags.of(self).add("Project", config.project)
        Tags.of(self).add("Environment", config.environment)
        Tags.of(self).add("Namespace", config.namespace)
        Tags.of(self).add("DataClass", "SHAREABLE")

        self.log_group = logs.LogGroup(
            self,
            "SenderLogGroup",
            log_group_name=f"/chorus/{config.environment}/sender",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.configuration_set_name = f"chorus-{config.environment}"
        self.configuration_set = ses.ConfigurationSet(
            self,
            "SenderConfigurationSet",
            configuration_set_name=self.configuration_set_name,
            reputation_metrics=True,
        )
        """The set whose events carry the ``chorus_execution`` tag reconciliation reads.

        Defined here and never sent through: it is what makes ``SEND_UNKNOWN -> SENT`` on
        positive evidence possible at all, and an event from a *different* configuration set is
        refused by the reconciliation command.
        """

        self.role_name = f"chorus-sender-{config.environment}"
        self.role = iam.Role(
            self,
            "SenderRole",
            role_name=self.role_name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="External sender: one approved message, one deliberate SES attempt.",
        )
        self.role_arn_literal = f"arn:aws:iam::{self.account}:role/{self.role_name}"
        self._grant_boundary(
            tables=tables,
            buckets=buckets,
            config=config,
            compiler_function_arn=compiler_function_arn,
            destination_registry_secret_arn=destination_registry_secret_arn,
            ses_identity_arn=ses_identity_arn,
        )

    def _grant_boundary(
        self,
        *,
        tables: SenderTables,
        buckets: SenderBuckets,
        config: CdkBuildConfig,
        compiler_function_arn: str | None,
        destination_registry_secret_arn: str | None,
        ses_identity_arn: str | None,
    ) -> None:
        """Attach the complete allow list and every explicit deny, in one place."""

        # It must load the view, the proposal, the approval, and both pointers, and every one
        # of those is an external-safe record. Unrestricted *read* over the Shareable table is
        # therefore the grant; the whole boundary is on the write side.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadShareable",
                effect=iam.Effect.ALLOW,
                actions=list(READ_ACTIONS),
                resources=[tables.shareable.table_arn],
            )
        )
        # The sender's entire Shareable write capability, and it reaches exactly one prefix.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteExecutionPartitionOnly",
                effect=iam.Effect.ALLOW,
                actions=list(EXECUTION_WRITE_ACTIONS),
                resources=[tables.shareable.table_arn],
                conditions={
                    "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [EXECUTION_KEY_PREFIX]}
                },
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="AppendAudit",
                effect=iam.Effect.ALLOW,
                actions=list(AUDIT_WRITE_ACTIONS),
                resources=[tables.audit.table_arn],
            )
        )
        if compiler_function_arn:
            # The fence is acquired and released through the compiler's typed operation. The
            # sender holds no DynamoDB access to Core at all, so this invoke is the only way it
            # can reach the fence -- which is what makes the Core deny below total rather than
            # aspirational.
            self.role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeCompilerFenceOperationOnly",
                    effect=iam.Effect.ALLOW,
                    actions=["lambda:InvokeFunction"],
                    resources=[compiler_function_arn],
                )
            )
        if destination_registry_secret_arn:
            self.role.add_to_policy(
                iam.PolicyStatement(
                    sid="ReadDestinationRegistrySecretOnly",
                    effect=iam.Effect.ALLOW,
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[destination_registry_secret_arn],
                )
            )
        send_resources = [
            self.format_arn(
                service="ses",
                resource="configuration-set",
                resource_name=self.configuration_set_name,
            )
        ]
        if ses_identity_arn:
            send_resources.append(ses_identity_arn)
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="SendThroughConfiguredIdentityOnly",
                effect=iam.Effect.ALLOW,
                # ``ses:``, not ``sesv2:``. SES v2's SendEmail authorizes under the ses: action
                # name, and the sesv2: spelling names no real IAM action.
                actions=[SES_SEND_ACTION],
                resources=send_resources,
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteOwnSenderLogs",
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
                resources=[
                    self.log_group.log_group_arn,
                    f"{self.log_group.log_group_arn}:log-stream:*",
                ],
            )
        )
        # The negative half of the guarantee whose positive half is the LeadingKeys split
        # above. An explicit deny cannot be overridden by a later grant, so a future change
        # that accidentally widened the write statement still fails closed.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyProposalApprovalViewAndCaseWrites",
                effect=iam.Effect.DENY,
                actions=list(DENIED_SHAREABLE_WRITE_ACTIONS),
                resources=[tables.shareable.table_arn],
                conditions={
                    "ForAnyValue:StringLike": {
                        "dynamodb:LeadingKeys": list(FORBIDDEN_WRITE_PREFIXES)
                    }
                },
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyAllCoreAccess",
                effect=iam.Effect.DENY,
                actions=["dynamodb:*"],
                resources=[tables.core.table_arn, f"{tables.core.table_arn}/*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyEvidenceObjects",
                effect=iam.Effect.DENY,
                actions=["s3:*"],
                resources=[
                    buckets.private.bucket_arn,
                    buckets.private.arn_for_objects("*"),
                    buckets.export.bucket_arn,
                    buckets.export.arn_for_objects("*"),
                ],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenySenderModelAccess",
                effect=iam.Effect.DENY,
                actions=[*DENIED_MODEL_ACTIONS, "bedrock:*", "bedrock-agentcore:*"],
                resources=["*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenySenderScheduler",
                effect=iam.Effect.DENY,
                actions=[*DENIED_SCHEDULER_ACTIONS, "scheduler:*"],
                resources=["*"],
            )
        )


__all__ = [
    "AUDIT_WRITE_ACTIONS",
    "DENIED_MODEL_ACTIONS",
    "DENIED_SCHEDULER_ACTIONS",
    "DENIED_SHAREABLE_WRITE_ACTIONS",
    "EXECUTION_KEY_PREFIX",
    "EXECUTION_WRITE_ACTIONS",
    "FORBIDDEN_WRITE_PREFIXES",
    "SES_SEND_ACTION",
    "ChorusSenderStack",
    "SenderBuckets",
    "SenderTables",
]
