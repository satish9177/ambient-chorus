"""The deadline watcher as an identity, plus the schedule group, the DLQ, and the alarms.

What this stack creates is the watcher's *identity*, the policies that bound it, the schedule
group its one-time schedules live in, and the encrypted dead-letter queue a dropped invocation
lands in. That is the part the security argument rests on, and it can be asserted from a
synthesized template long before the Lambda exists -- the same static-now split Phase 5 used for
the agent runtimes, Phase 6 for the compiler, Phase 7 for the application, and Phase 8 for the
sender. The deployed function, the live schedule, the real invocation, and the post-deploy
``AccessDenied`` canaries belong to Phase 11.

Four grants and denies are unusual enough to say out loud.

**Core is denied in total.** The trust matrix used to read ``Share: R/W(commitment/case
projection)``, which mislocated the case row: the case lives in **Core**, and the commitment,
its schedule projection, and the verification-request item all live in the Shareable
``NS#n#CASE#k`` partition. The watcher takes no case edge in either table --
``ACTIONED -> VERIFYING`` happened at commitment creation -- so a Core grant would be a
permission with no caller (ADR-028 § 6).

**The Shareable write reaches one partition prefix.** ``NS#*#CASE#*`` and nothing else, scoped
by ``dynamodb:LeadingKeys``. Everything the watcher may touch is in there, and everything else
in that table -- views, proposals, approvals, executions, the outbound message locators -- is
outside it.

**No ``scheduler:*`` at all.** The watcher is a schedule *target*, never a schedule client. It
cannot create the schedule that invoked it, and it cannot create another.

**No ``bedrock:*``, no ``ses:*``, and no S3 action.** It invokes no model, sends nothing, and
reads no evidence bytes. Each is denied rather than merely ungranted, because an explicit deny
cannot be overridden by a later grant.
"""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.cdk.config import CdkBuildConfig

CASE_KEY_PREFIX = "NS#*#CASE#*"
"""The watcher's complete Shareable data-plane authority, as a key prefix."""

FORBIDDEN_WRITE_PREFIXES = (
    "NS#*#ACTION#*",
    "NS#*#ACTION_CURRENT#*",
    "NS#*#EXECUTION#*",
    "NS#*#OUTBOUND_MESSAGE#*",
    "NS#*#VIEW#*",
    "NS#*#VIEW_CURRENT#*",
)
"""Every Shareable prefix the watcher must never write, denied by ``ForAnyValue``.

``ForAnyValue`` is deliberate and is the same choice ADR-022 and ADR-024 made: a transaction
naming *any* item outside the case partition alongside legitimate commitment items is refused
whole, rather than permitted because most of its keys were acceptable.
"""

READ_ACTIONS = ("dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query")

CASE_WRITE_ACTIONS = ("dynamodb:PutItem", "dynamodb:ConditionCheckItem")
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
"""The watcher decides one thing from durable state. It never asks anything what to think."""

DENIED_SEND_ACTIONS = (
    "ses:SendEmail",
    "ses:SendRawEmail",
    "sesv2:SendEmail",
    "sesv2:SendBulkEmail",
)

DENIED_SCHEDULER_ACTIONS = (
    "scheduler:CreateSchedule",
    "scheduler:UpdateSchedule",
    "scheduler:DeleteSchedule",
    "scheduler:GetSchedule",
)
"""A target, not a client. Even ``GetSchedule`` is denied: nothing here reads a schedule."""

SCHEDULER_INVOKE_ACTION = "lambda:InvokeFunction"
"""The scheduler execution role's entire compute reach: invoke the one watcher function."""

SCHEDULER_DLQ_SEND_ACTION = "sqs:SendMessage"
"""A dropped one-time schedule delivery lands in the DLQ; the role may put it there and nothing
else on any queue."""

SCHEDULER_DLQ_KEY_ACTIONS = ("kms:GenerateDataKey", "kms:Decrypt")
"""What EventBridge Scheduler needs to write an encrypted message to the KMS-encrypted DLQ:
a data key to encrypt the payload, and decrypt for the SQS envelope. Scoped to the DLQ CMK
alone -- no other key, and no ``kms:*``."""

DLQ_RETENTION_DAYS = 14
DLQ_DEPTH_ALARM_THRESHOLD = 1
"""One dropped invocation is worth a person's attention.

A dead-lettered due event means a commitment reached its deadline and nobody was asked to
verify it, which is silent from every other surface -- the case simply stays ``VERIFYING``.
"""


@dataclass(frozen=True, slots=True)
class WatcherTables:
    """The two tables the watcher touches, and the one it is denied outright."""

    core: dynamodb.ITable
    shareable: dynamodb.ITable
    audit: dynamodb.ITable


@dataclass(frozen=True, slots=True)
class WatcherBuckets:
    """Both evidence buckets, present here only so they can be denied."""

    private: s3.IBucket
    export: s3.IBucket


class ChorusWatcherStack(Stack):
    """The watcher's role and log group, the schedule group, the DLQ, and its alarm."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: CdkBuildConfig,
        tables: WatcherTables,
        buckets: WatcherBuckets,
    ) -> None:
        super().__init__(scope, construct_id)
        Tags.of(self).add("Project", config.project)
        Tags.of(self).add("Environment", config.environment)
        Tags.of(self).add("Namespace", config.namespace)
        Tags.of(self).add("DataClass", "SHAREABLE")

        self.log_group = logs.LogGroup(
            self,
            "WatcherLogGroup",
            log_group_name=f"/chorus/{config.environment}/commitment-watcher",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.schedule_group_name = f"chorus-{config.environment}"
        self.schedule_group = scheduler.CfnScheduleGroup(
            self, "CommitmentScheduleGroup", name=self.schedule_group_name
        )
        """The one group every commitment schedule lives in.

        Named so the application's narrowed grant can be scoped to
        ``arn:...:schedule/chorus-{env}/*`` and to nothing wider.
        """

        self.dead_letter_key = kms.Key(
            self,
            "WatcherDeadLetterKey",
            description="Encrypts dropped commitment due events.",
            enable_key_rotation=True,
            removal_policy=(
                RemovalPolicy.DESTROY if config.is_disposable else RemovalPolicy.RETAIN
            ),
        )
        self.dead_letter_queue = sqs.Queue(
            self,
            "WatcherDeadLetterQueue",
            queue_name=f"chorus-commitment-dlq-{config.environment}",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=self.dead_letter_key,
            retention_period=Duration.days(DLQ_RETENTION_DAYS),
            enforce_ssl=True,
            removal_policy=(
                RemovalPolicy.DESTROY if config.is_disposable else RemovalPolicy.RETAIN
            ),
        )
        self.dead_letter_alarm = cloudwatch.Alarm(
            self,
            "WatcherDeadLetterAlarm",
            alarm_name=f"chorus-commitment-dlq-depth-{config.environment}",
            alarm_description=(
                "A commitment due event was dropped: a deadline passed and nobody was asked."
            ),
            metric=self.dead_letter_queue.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5)
            ),
            threshold=DLQ_DEPTH_ALARM_THRESHOLD,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        self.role_name = f"chorus-commitment-watcher-{config.environment}"
        self.role = iam.Role(
            self,
            "WatcherRole",
            role_name=self.role_name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Commitment watcher: one edge, one Shareable partition, nothing else.",
        )
        # The watcher Lambda is deployed in Phase 11 (I2) into this same stack, under the frozen
        # name below, and rollback repoints its ``live`` alias at a published version rather than
        # editing schedules or IAM (deployment contract § 20). So both the scheduler target and
        # the scheduler role's invoke authority bind to the **alias** ARN, not the unqualified
        # function ARN. Deterministic literals here: the resources do not exist yet, they land
        # beside this role later, and the worker's stack already depends on this one -- no cycle.
        self.watcher_function_name = f"chorus-commitment-watcher-{config.environment}"
        self.watcher_alias_name = "live"
        self.watcher_function_arn_literal = (
            f"arn:aws:lambda:{self.region}:{self.account}:function:{self.watcher_function_name}"
        )
        self.watcher_alias_arn_literal = (
            f"{self.watcher_function_arn_literal}:{self.watcher_alias_name}"
        )
        # The confused-deputy boundary for a Scheduler execution role is the **schedule-group**
        # ARN, not an individual schedule ARN and never a wildcard schedule name (deployment
        # contract § 8.5, AWS cross-service guidance). Deterministic literal rather than
        # ``schedule_group.attr_arn`` so the frozen group name is legible in the synthesized
        # trust policy; no cycle either way since the group is built above.
        self.schedule_group_arn = self.format_arn(
            service="scheduler",
            resource="schedule-group",
            resource_name=self.schedule_group_name,
        )

        self.scheduler_role_name = f"chorus-scheduler-{config.environment}"
        self.scheduler_role = iam.Role(
            self,
            "SchedulerExecutionRole",
            role_name=self.scheduler_role_name,
            # Not an unconstrained service trust: EventBridge Scheduler may assume this role only
            # for this account and only on behalf of a schedule in the frozen group. The
            # ``aws:SourceArn`` is the exact schedule-group ARN -- an individual schedule ARN or
            # a wildcard tail would be a wider boundary than the confused-deputy guidance draws.
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnEquals": {"aws:SourceArn": self.schedule_group_arn},
                },
            ),
            description="EventBridge Scheduler: invokes the commitment watcher and nothing else.",
        )
        self.scheduler_role_arn_literal = (
            f"arn:aws:iam::{self.account}:role/{self.scheduler_role_name}"
        )
        self._grant_boundary(tables=tables, buckets=buckets)
        self._grant_scheduler_execution_boundary()

    def _grant_boundary(self, *, tables: WatcherTables, buckets: WatcherBuckets) -> None:
        """Attach the complete allow list and every explicit deny, in one place."""

        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadCasePartitionOnly",
                effect=iam.Effect.ALLOW,
                actions=list(READ_ACTIONS),
                resources=[tables.shareable.table_arn],
                conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [CASE_KEY_PREFIX]}},
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteCasePartitionOnly",
                effect=iam.Effect.ALLOW,
                actions=list(CASE_WRITE_ACTIONS),
                resources=[tables.shareable.table_arn],
                conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [CASE_KEY_PREFIX]}},
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
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteOwnWatcherLogs",
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
                sid="DenyNonCasePartitionWrites",
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
                sid="DenyWatcherModelAccess",
                effect=iam.Effect.DENY,
                actions=[*DENIED_MODEL_ACTIONS, "bedrock:*", "bedrock-agentcore:*"],
                resources=["*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyWatcherSend",
                effect=iam.Effect.DENY,
                actions=list(DENIED_SEND_ACTIONS),
                resources=["*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyWatcherScheduler",
                effect=iam.Effect.DENY,
                actions=[*DENIED_SCHEDULER_ACTIONS, "scheduler:*"],
                resources=["*"],
            )
        )

    def _grant_scheduler_execution_boundary(self) -> None:
        """The exact minimum EventBridge Scheduler needs to deliver one due event.

        Three grants and no fourth (deployment contract § 8.5): invoke the watcher's ``live``
        alias, put a dropped delivery on the one DLQ, and use the one DLQ key to encrypt that
        message. No DynamoDB, no Bedrock, no SES, no AgentCore, no Secrets Manager, no broad
        ``lambda:InvokeFunction``, no general ``sqs:*`` or ``kms:*``. The role had a trust
        policy and zero attached policies until now.

        The invoke resource is the **qualified alias** ARN (``…:function:…:live``), never the
        unqualified function ARN and never ``$LATEST`` or a bare version: rollback repoints the
        alias at a published version and no schedule or policy statement changes (deployment
        contract § 20).
        """

        self.scheduler_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeCommitmentWatcherLiveAliasOnly",
                effect=iam.Effect.ALLOW,
                actions=[SCHEDULER_INVOKE_ACTION],
                resources=[self.watcher_alias_arn_literal],
            )
        )
        self.scheduler_role.add_to_policy(
            iam.PolicyStatement(
                sid="SendDroppedDueEventToDeadLetterQueueOnly",
                effect=iam.Effect.ALLOW,
                actions=[SCHEDULER_DLQ_SEND_ACTION],
                resources=[self.dead_letter_queue.queue_arn],
            )
        )
        self.scheduler_role.add_to_policy(
            iam.PolicyStatement(
                sid="UseDeadLetterQueueKeyForEncryptedSendOnly",
                effect=iam.Effect.ALLOW,
                actions=list(SCHEDULER_DLQ_KEY_ACTIONS),
                resources=[self.dead_letter_key.key_arn],
            )
        )


__all__ = [
    "AUDIT_WRITE_ACTIONS",
    "CASE_KEY_PREFIX",
    "CASE_WRITE_ACTIONS",
    "DENIED_MODEL_ACTIONS",
    "DENIED_SCHEDULER_ACTIONS",
    "DENIED_SEND_ACTIONS",
    "DENIED_SHAREABLE_WRITE_ACTIONS",
    "DLQ_DEPTH_ALARM_THRESHOLD",
    "DLQ_RETENTION_DAYS",
    "FORBIDDEN_WRITE_PREFIXES",
    "SCHEDULER_DLQ_KEY_ACTIONS",
    "SCHEDULER_DLQ_SEND_ACTION",
    "SCHEDULER_INVOKE_ACTION",
    "ChorusWatcherStack",
    "WatcherBuckets",
    "WatcherTables",
]
