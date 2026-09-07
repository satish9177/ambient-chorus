"""The application boundary as an identity: one role, one log group, and its exact grants.

The application is the broadest principal in the system, and that is exactly why its boundary
has to be written down rather than inferred. It reads and writes the private Core zone, it
creates actions and approvals in the Shareable zone, and it appends to Audit. What it must
**not** be able to do is create or alter a compiled view -- because the compiler is the sole
creator of views by IAM and not by convention.

Three grants are unusual enough to say out loud.

**Shareable is split by key prefix.** ``dynamodb:LeadingKeys`` allows the application to write
only the action and case prefixes. The compiler writes only the two view prefixes. Neither can
reach the other's, so "the application cannot mint a view that authorizes its own proposal" is
a property of the key grammar rather than of anybody's discipline.

**The application holds `ConditionCheckItem` on the view prefixes, and nothing else there.**
The Phase-7 proposal transaction has to be able to refuse to commit against a view that has
moved while the Action model was answering (ADR-022 § 7). AWS authorizes a transaction through
the permission each *participant* needs, so a ``ConditionCheck`` participant requires exactly
this action and a ``Put`` would require ``dynamodb:PutItem`` -- which is what makes a read-only
transactional authority expressible at all. **A condition check must never become a write
grant**, and the alternative -- granting ``UpdateItem`` so the row can be "conditioned on" -- is
named in the ADR so it is refused once rather than proposed repeatedly.

**Bedrock is granted for exactly three inference profiles and denied nowhere else.** The
application invokes agents through AgentCore rather than Bedrock directly, so it holds
``bedrock-agentcore:InvokeAgentRuntime`` on the three runtime ARNs and no raw model action.

SES is denied outright. Its breadth of private access is precisely why it never receives a send
permission: it invokes the sender with an action identifier, never a rendered body and never a
recipient address.
"""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.cdk.config import CdkBuildConfig

VIEW_KEY_PREFIXES = ("NS#*#VIEW#*", "NS#*#VIEW_CURRENT#*")
"""The compiler-owned Shareable partitions. The application reads and condition-checks these
and can never write one."""

APPLICATION_SHAREABLE_PREFIXES = (
    "NS#*#ACTION#*",
    "NS#*#ACTION_CURRENT#*",
    "NS#*#EXECUTION#*",
    "NS#*#OUTBOUND_MESSAGE#*",
    "NS#*#CASE#*",
)
"""The only Shareable partitions the application may write.

``ACTION#`` holds the immutable proposals and approvals; ``ACTION_CURRENT#`` holds the current
action pointer, the action history locators, and the action-apply idempotency record;
``EXECUTION#`` holds the send execution and the send-command records; ``OUTBOUND_MESSAGE#``
holds the immutable correlation locator the action case projection writes at ``SENT``; ``CASE#``
holds commitments, their schedule projections, and their verification requests.

``OUTBOUND_MESSAGE#`` is **not a widening** either. The locator is a participant of the
projection transaction, which is the application worker's and never the sender's -- the sender
is denied that prefix explicitly (ADR-026 § 3). It is a partition of its own rather than a sort
key inside ``NS#n#EXECUTION#a`` because correlation starts from a reply's ``In-Reply-To`` and
has no action identifier yet, so the printed key could only be resolved by the scan or the GSI
that ADR rejects by name.

``EXECUTION#`` is **not a widening** (ADR-024 SS 4). The application already created the
``DRAFT`` execution as participant 2 of the proposal apply and moves it on both human
decisions; this is where a write it already had now lives.

Two principals therefore hold ``PutItem`` over one prefix, and that is the one Phase-8 boundary
IAM does not draw. What keeps them apart is the state machine: the application can only move a
row that is in ``DRAFT`` or ``APPROVED``, and the sender only one in ``APPROVED`` or
``SENDING``, each conditioned on an exact row version. The single overlapping state is the
approval-withdrawal race, which the compare-and-swap resolves with exactly one winner. It is
written down here rather than left implied, because an assertion that it holds is a test over
the transitions and not over a policy.
"""

READ_ACTIONS = ("dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query")
WRITE_ACTIONS = ("dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem")

CONDITION_CHECK_ACTION = "dynamodb:ConditionCheckItem"
"""Read-only transactional authority: assert an item's state without being able to change it."""

TRANSACTION_ONLY = {"StringEquals": {"dynamodb:EnclosingOperation": "TransactWriteItems"}}
"""Narrows a grant to participants of a transaction.

Applied to the view condition check because that participant genuinely never happens
standalone: it is staged only inside the proposal apply. Restricting it costs nothing and
removes a capability that would otherwise exist on its own.
"""

DENIED_VIEW_WRITE_ACTIONS = (
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
)
"""Never writable by this role, and denied rather than merely ungranted.

``ForAnyValue`` is deliberate: a transaction naming *any* view-partition item alongside
legitimate action items is refused whole, rather than being permitted because most of its keys
were acceptable. This is the defence-in-depth half of the guarantee whose positive half is the
``LeadingKeys`` split above -- an explicit deny cannot be overridden by a later grant, so a
future change that accidentally widened the write statement still fails closed.
"""

DENIED_SEND_ACTIONS = (
    "ses:SendEmail",
    "ses:SendRawEmail",
    "sesv2:SendEmail",
    "sesv2:SendBulkEmail",
)
"""The application drafts and approves. Only the sender sends."""

SCHEDULER_ACTIONS = ("scheduler:CreateSchedule", "scheduler:GetSchedule")
"""The application's complete scheduler capability, narrowed by ADR-028 § 6.

**No ``DeleteSchedule`` and no ``UpdateSchedule``.** ``DeadlineSchedulerPort`` has no method for
either, V1 has no reschedule verb, and ``ActionAfterCompletion=DELETE`` handles cleanup -- so a
wider grant would be a permission with no caller and one obvious illegitimate one. A grant wider
than its caller is a grant waiting for a second caller.
"""

DENIED_SCHEDULER_ACTIONS = ("scheduler:DeleteSchedule", "scheduler:UpdateSchedule")
"""Denied rather than merely ungranted, so a later widening still fails closed."""

DENIED_MODEL_ACTIONS = (
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
)
"""Agents are invoked through their runtimes, which pin the prompt and the schema.

A direct model grant would let application code send an unreviewed prompt to the same model,
which is the one path around every artifact-level control the runtimes exist to impose.
"""


@dataclass(frozen=True, slots=True)
class ApplicationTables:
    """The three tables the application's grants are scoped to."""

    core: dynamodb.ITable
    shareable: dynamodb.ITable
    audit: dynamodb.ITable


@dataclass(frozen=True, slots=True)
class ApplicationBuckets:
    """The two evidence buckets and the separate keys that actually gate them."""

    private: s3.IBucket
    export: s3.IBucket
    private_key: kms.IKey
    export_key: kms.IKey


class ChorusApplicationStack(Stack):
    """The application's role and log group. No compute resource is created here."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: CdkBuildConfig,
        tables: ApplicationTables,
        buckets: ApplicationBuckets,
        agent_runtime_arns: tuple[str, ...] = (),
        scheduler_group_name: str | None = None,
        scheduler_role_arn: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id)
        Tags.of(self).add("Project", config.project)
        Tags.of(self).add("Environment", config.environment)
        Tags.of(self).add("Namespace", config.namespace)
        Tags.of(self).add("DataClass", "PRIVATE")

        self.log_group = logs.LogGroup(
            self,
            "ApplicationLogGroup",
            log_group_name=f"/chorus/{config.environment}/application",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.role_name = f"chorus-application-{config.environment}"
        self.role = iam.Role(
            self,
            "ApplicationRole",
            role_name=self.role_name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="FastAPI application and operation worker: private zone and actions.",
        )
        self.scheduler_group_name = scheduler_group_name or f"chorus-{config.environment}"
        self.scheduler_role_arn = scheduler_role_arn
        self._grant_boundary(
            tables=tables, buckets=buckets, agent_runtime_arns=agent_runtime_arns, config=config
        )

    def _grant_boundary(
        self,
        *,
        tables: ApplicationTables,
        buckets: ApplicationBuckets,
        agent_runtime_arns: tuple[str, ...],
        config: CdkBuildConfig,
    ) -> None:
        """Attach the complete allow list and the explicit denies, in one place.

        Every DynamoDB grant names the *underlying* action a transaction participant needs
        rather than a blanket ``dynamodb:TransactWriteItems``. AWS authorizes a transaction
        through its members, so the blanket action would be a permission this role does not
        need and a place for a future participant to hide.
        """

        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadWritePrivateCore",
                effect=iam.Effect.ALLOW,
                actions=[*READ_ACTIONS, *WRITE_ACTIONS, CONDITION_CHECK_ACTION],
                resources=[tables.core.table_arn],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadShareable",
                effect=iam.Effect.ALLOW,
                actions=list(READ_ACTIONS),
                resources=[tables.shareable.table_arn],
            )
        )
        # The application's entire Shareable *write* capability, and it reaches exactly the
        # action and case prefixes. It cannot create a view; the compiler cannot create an
        # action. Sole-writer-of-views is enforced by key grammar rather than by convention.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteActionAndCasePrefixesOnly",
                effect=iam.Effect.ALLOW,
                actions=[*WRITE_ACTIONS, CONDITION_CHECK_ACTION],
                resources=[tables.shareable.table_arn],
                conditions={
                    "ForAllValues:StringLike": {
                        "dynamodb:LeadingKeys": list(APPLICATION_SHAREABLE_PREFIXES)
                    }
                },
            )
        )
        # ADR-022 § 7. Read-only transactional authority over the compiler-owned view
        # partitions, so the Phase-7 proposal apply can refuse to commit against a view that
        # moved while the model was answering -- without ever being able to move one itself.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ConditionCheckCurrentViewPointer",
                effect=iam.Effect.ALLOW,
                actions=[CONDITION_CHECK_ACTION],
                resources=[tables.shareable.table_arn],
                conditions={
                    "ForAllValues:StringLike": {"dynamodb:LeadingKeys": list(VIEW_KEY_PREFIXES)},
                    **TRANSACTION_ONLY,
                },
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="AppendAudit",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query"],
                resources=[tables.audit.table_arn],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadWritePrivateEvidenceObjects",
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[buckets.private.arn_for_objects("*")],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadExportEvidenceObjects",
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject"],
                resources=[buckets.export.arn_for_objects("*")],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="UsePrivateEvidenceKey",
                effect=iam.Effect.ALLOW,
                actions=["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                resources=[buckets.private_key.key_arn],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DecryptExportEvidence",
                effect=iam.Effect.ALLOW,
                actions=["kms:Decrypt", "kms:DescribeKey"],
                resources=[buckets.export_key.key_arn],
            )
        )
        if agent_runtime_arns:
            self.role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeNamedAgentRuntimesOnly",
                    effect=iam.Effect.ALLOW,
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=list(agent_runtime_arns),
                )
            )
        # ADR-028 § 6. Create and read, on the one schedule group, and nothing else.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="CreateAndGetCommitmentSchedulesOnly",
                effect=iam.Effect.ALLOW,
                actions=list(SCHEDULER_ACTIONS),
                resources=[
                    self.format_arn(
                        service="scheduler",
                        resource="schedule",
                        resource_name=f"{self.scheduler_group_name}/*",
                    )
                ],
            )
        )
        if self.scheduler_role_arn:
            # Passing the scheduler execution role is what lets a created schedule invoke the
            # watcher. It is scoped to that one role: a broader ``iam:PassRole`` would let the
            # application hand any role to any target.
            self.role.add_to_policy(
                iam.PolicyStatement(
                    sid="PassSchedulerExecutionRoleOnly",
                    effect=iam.Effect.ALLOW,
                    actions=["iam:PassRole"],
                    resources=[self.scheduler_role_arn],
                    conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
                )
            )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyScheduleDeletionAndUpdate",
                effect=iam.Effect.DENY,
                actions=list(DENIED_SCHEDULER_ACTIONS),
                resources=["*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteOwnApplicationLogs",
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
                resources=[
                    self.log_group.log_group_arn,
                    f"{self.log_group.log_group_arn}:log-stream:*",
                ],
            )
        )
        # The negative half of the view guarantee, and the statement the ADR-022 static
        # assertion actually reads. Not merely ungranted -- denied, because an explicit deny
        # cannot be overridden by a later grant.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyViewPartitionWrites",
                effect=iam.Effect.DENY,
                actions=list(DENIED_VIEW_WRITE_ACTIONS),
                resources=[tables.shareable.table_arn],
                conditions={
                    "ForAnyValue:StringLike": {"dynamodb:LeadingKeys": list(VIEW_KEY_PREFIXES)}
                },
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyApplicationSend",
                effect=iam.Effect.DENY,
                actions=list(DENIED_SEND_ACTIONS),
                resources=["*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyDirectModelAccess",
                effect=iam.Effect.DENY,
                actions=list(DENIED_MODEL_ACTIONS),
                resources=["*"],
            )
        )
