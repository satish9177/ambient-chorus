"""The compiler boundary as an identity: one role, one log group, and its exact grants.

What this stack creates is the compiler's *identity* and the policies that bound it. That is
the part the security argument rests on, and it can be asserted from a synthesized template
long before the Lambda exists -- which is exactly the split Phase 5 used for the agent runtimes
and which Phase 6 uses here. The deployed function, the live compile, and the post-deploy
`AccessDenied` canaries belong to Phase 11.

The grants are the frozen trust matrix, and three of them are unusual enough to say out loud.

**Core is read-everything, write-almost-nothing.** The compiler reads private state because
that is what compiling *is*, and its only Core write is the send-authorization fence. It has no
grant that could touch a case, a fact, or a mandate, which is what makes "a compile never
mutates the case" an IAM fact rather than a code convention.

**Core writes reach one partition prefix holding one item type.** The send fence has its
own partition (ADR-019) precisely so this grant can exist: ``LeadingKeys`` constrains the
partition key and nothing constrains the sort key, so a fence inside the case partition
could not be granted without granting the case row, its facts, and its mandates too. The
case-version guard is a ``ConditionCheckItem``, which is read-only authority.

**Shareable is restricted by ``dynamodb:LeadingKeys``.** The compiler may write only the two
view partitions. It cannot create an action, an approval, or an execution; the application can
create those and cannot create a view. Sole-writer-of-views is enforced by key grammar rather
than by convention, which is the reason the Shareable table uses entity-type partition prefixes
at all.

**Bedrock is denied outright.** Not merely ungranted -- denied. The compiler is the one place
policy is decided, and an explicit deny is what stops a future change from making that decision
probabilistic by attaching a model grant to this role.
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
"""The only Shareable partitions the compiler may write.

``VIEW#`` holds the immutable views; ``VIEW_CURRENT#`` holds the current pointer, the history
locators, and -- because the compiler's Shareable writes are confined to these two prefixes --
the compile idempotency record as well.
"""

CASE_KEY_PREFIX = "NS#*#CASE#*"
"""Case partitions. The compiler reads these and condition-checks them; it never writes one."""

FENCE_KEY_PREFIX = "NS#*#FENCE#*"
"""The send fence's own partition.

The fence used to share the case partition, which made "compiler Core write is the fence alone"
unenforceable: ``dynamodb:LeadingKeys`` constrains the partition key and nothing constrains the
sort key, so granting the fence granted the case row, its facts, its reports, its evidence and
its mandates along with it. ADR-019 moved the fence to its own partition so the sentence became
a permission AWS can express.
"""

READ_ACTIONS = ("dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query")

CONDITION_CHECK_ACTION = "dynamodb:ConditionCheckItem"
"""Read-only transactional authority: assert an item's state without being able to change it.

DynamoDB authorizes a ``TransactWriteItems`` request through the permission each *participant*
needs, so a ``ConditionCheck`` participant requires this action and a ``Put`` requires
``dynamodb:PutItem``. That is what lets the case-version guard exist without any write grant on
case partitions at all.
"""

FENCE_WRITE_ACTIONS = ("dynamodb:PutItem", "dynamodb:DeleteItem")
"""Acquire is a conditional put; release is a conditional delete. There is no update path,
so ``dynamodb:UpdateItem`` is not granted anywhere in this role."""

SHAREABLE_WRITE_ACTIONS = (
    "dynamodb:PutItem",
    "dynamodb:GetItem",
    "dynamodb:BatchGetItem",
    "dynamodb:Query",
)
AUDIT_WRITE_ACTIONS = ("dynamodb:PutItem", "dynamodb:GetItem")

TRANSACTION_ONLY = {"StringEquals": {"dynamodb:EnclosingOperation": "TransactWriteItems"}}
"""Narrows a grant to participants of a transaction.

Applied only where the operation genuinely never happens standalone. The case-version and
fence condition checks are staged exclusively inside the compile, mandate and investigation
transactions, so restricting them costs nothing and removes a standalone capability.
"""

DENIED_MODEL_ACTIONS = (
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
    "bedrock-agentcore:InvokeAgentRuntime",
)
"""Policy must not become probabilistic. The compiler decides; it never asks."""

DENIED_SIDE_EFFECT_ACTIONS = (
    "ses:SendEmail",
    "ses:SendRawEmail",
    "scheduler:CreateSchedule",
    "lambda:InvokeFunction",
)
"""The compiler produces an artifact. It does not act on one."""

DENIED_CASE_WRITE_ACTIONS = (
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
)
"""Never writable by this role, and denied rather than merely ungranted.

``ForAnyValue`` is deliberate: a transaction naming *any* case-partition item alongside
legitimate fence items is refused whole, rather than being permitted because most of its
keys were acceptable.
"""

DENIED_OBJECT_ACTIONS = ("s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutBucketPolicy")
"""No compensating delete exists on the compile path, so no grant for one does either.

An unreferenced derivative is swept by bucket lifecycle. Granting the compiler deletion would
add a failure mode -- a compensating write that can itself fail -- to remove one that ADR-018
already removed by making the object harmless.
"""


@dataclass(frozen=True, slots=True)
class CompilerTables:
    """The three tables the compiler's grants are scoped to."""

    core: dynamodb.ITable
    shareable: dynamodb.ITable
    audit: dynamodb.ITable


@dataclass(frozen=True, slots=True)
class CompilerBuckets:
    """The two evidence buckets and the separate keys that actually gate them."""

    private: s3.IBucket
    export: s3.IBucket
    private_key: kms.IKey
    export_key: kms.IKey


class ChorusCompilerStack(Stack):
    """The compiler's role and log group. No function resource is created here."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: CdkBuildConfig,
        tables: CompilerTables,
        buckets: CompilerBuckets,
    ) -> None:
        super().__init__(scope, construct_id)
        Tags.of(self).add("Project", config.project)
        Tags.of(self).add("Environment", config.environment)
        Tags.of(self).add("Namespace", config.namespace)
        Tags.of(self).add("DataClass", "PRIVATE")

        self.log_group = logs.LogGroup(
            self,
            "CompilerLogGroup",
            log_group_name=f"/chorus/{config.environment}/compiler",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.role_name = f"chorus-compiler-{config.environment}"
        self.role = iam.Role(
            self,
            "CompilerRole",
            role_name=self.role_name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Deterministic privacy compiler: sole creator of ShareableCaseView",
        )
        # Named explicitly so the export bucket's sole-writer policy can name this role
        # without referencing its resource. A CloudFormation reference in the other
        # direction would make the data stack depend on the compiler stack, which already
        # depends on it -- and the buckets must exist before the principal that writes them.
        self.role_arn_literal = f"arn:aws:iam::{self.account}:role/{self.role_name}"
        self._grant_boundary(tables=tables, buckets=buckets)

    def _grant_boundary(self, *, tables: CompilerTables, buckets: CompilerBuckets) -> None:
        """Attach the complete allow list and the three explicit denies, in one place.

        Every DynamoDB grant names the *underlying* action a transaction participant needs
        rather than a blanket ``dynamodb:TransactWriteItems``. AWS authorizes a transaction
        through its members, so the blanket action would have been a permission this role does
        not need and a place for a future participant to hide.
        """

        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadPrivateCore",
                effect=iam.Effect.ALLOW,
                actions=list(READ_ACTIONS),
                resources=[tables.core.table_arn],
            )
        )
        # Read-only transactional authority over case partitions. This is the whole of the
        # compiler's relationship with a case row: it may assert the version and it may not
        # change anything. There is deliberately no Put, Update, or Delete for this prefix.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ConditionCheckCaseVersion",
                effect=iam.Effect.ALLOW,
                actions=[CONDITION_CHECK_ACTION],
                resources=[tables.core.table_arn],
                conditions={
                    "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [CASE_KEY_PREFIX]},
                    **TRANSACTION_ONLY,
                },
            )
        )
        # The no-live-send-fence condition compile, mandate decisions and investigation applies
        # all stage. Also read-only.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ConditionCheckSendFence",
                effect=iam.Effect.ALLOW,
                actions=[CONDITION_CHECK_ACTION],
                resources=[tables.core.table_arn],
                conditions={
                    "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [FENCE_KEY_PREFIX]},
                    **TRANSACTION_ONLY,
                },
            )
        )
        # The compiler's entire Core *write* capability, and it reaches exactly one partition
        # prefix that holds exactly one item type.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteSendFenceOnly",
                effect=iam.Effect.ALLOW,
                actions=list(FENCE_WRITE_ACTIONS),
                resources=[tables.core.table_arn],
                conditions={
                    "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [FENCE_KEY_PREFIX]}
                },
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteViewPrefixesOnly",
                effect=iam.Effect.ALLOW,
                actions=list(SHAREABLE_WRITE_ACTIONS),
                resources=[tables.shareable.table_arn],
                conditions={
                    "ForAllValues:StringLike": {"dynamodb:LeadingKeys": list(VIEW_KEY_PREFIXES)}
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
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadPrivateEvidenceObjects",
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject"],
                resources=[buckets.private.arn_for_objects("*")],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteExportEvidenceObjects",
                effect=iam.Effect.ALLOW,
                actions=["s3:PutObject", "s3:GetObject"],
                resources=[buckets.export.arn_for_objects("*")],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DecryptPrivateEvidence",
                effect=iam.Effect.ALLOW,
                actions=["kms:Decrypt", "kms:DescribeKey"],
                resources=[buckets.private_key.key_arn],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="EncryptExportEvidence",
                effect=iam.Effect.ALLOW,
                actions=["kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                resources=[buckets.export_key.key_arn],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteOwnLogs",
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
                resources=[
                    self.log_group.log_group_arn,
                    f"{self.log_group.log_group_arn}:log-stream:*",
                ],
            )
        )
        # Defence in depth over the boundary the partition split now expresses positively: even
        # if some future grant named a case partition, these denies refuse the write outright.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyCasePartitionWrites",
                effect=iam.Effect.DENY,
                actions=list(DENIED_CASE_WRITE_ACTIONS),
                resources=[tables.core.table_arn],
                conditions={"ForAnyValue:StringLike": {"dynamodb:LeadingKeys": [CASE_KEY_PREFIX]}},
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyModelAccess",
                effect=iam.Effect.DENY,
                actions=list(DENIED_MODEL_ACTIONS),
                resources=["*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenySideEffects",
                effect=iam.Effect.DENY,
                actions=list(DENIED_SIDE_EFFECT_ACTIONS),
                resources=["*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyObjectDeletion",
                effect=iam.Effect.DENY,
                actions=list(DENIED_OBJECT_ACTIONS),
                resources=["*"],
            )
        )
        # Only the compiler writes a view, and the bucket policy says so about objects too:
        # a principal that is not this role cannot put an export derivative even if some future
        # identity policy granted it ``s3:PutObject``.
        buckets.export.add_to_resource_policy(
            iam.PolicyStatement(
                sid="OnlyCompilerWritesExportEvidence",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject", "s3:DeleteObject"],
                resources=[buckets.export.arn_for_objects("*")],
                conditions={"StringNotLike": {"aws:PrincipalArn": [self.role_arn_literal]}},
            )
        )
