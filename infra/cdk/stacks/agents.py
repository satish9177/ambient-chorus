"""Phase 3 agent resources: the Monitor runtime identity and its boundary.

What this stack creates is one IAM role and one log group. That is the whole Phase 3 surface,
and it is the part that matters for the security argument: the Monitor's isolation is an
identity property, not a code property, so it can be asserted from the synthesized template
long before a runtime is deployed.

The role's allow list is three things -- invoke exactly one inference profile, write to its own
log group, read its own artifact -- and its deny list is everything the frozen trust matrix
marks ``D``. The denies are defence in depth: none of them is reachable through an allow, and
an explicit deny cannot be overridden by a later grant, so a future change that accidentally
attaches a data policy to this role still fails closed.

The AgentCore runtime resource itself is deliberately not created here. The frozen network
design requires VPC mode in two isolated subnets with no NAT route, and that VPC belongs to
the deployment stack. Creating a public-mode runtime now to have something to point at would
contradict the design it is supposed to satisfy.
"""

from __future__ import annotations

from aws_cdk import Stack, Tags
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from infra.cdk.config import CdkBuildConfig

AGENTCORE_SERVICE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"

DENIED_DATA_PLANE_ACTIONS = (
    "dynamodb:GetItem",
    "dynamodb:BatchGetItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:TransactWriteItems",
    "dynamodb:TransactGetItems",
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:ListBucket",
)
"""Storage the Monitor must never touch.

The Monitor is inside the private investigation zone and is deliberately *given* private text
in its payload. That is exactly why it may not also read the stores: a compromised runtime
should be limited to the one batch it was handed, not to the corpus.
"""

DENIED_SIDE_EFFECT_ACTIONS = (
    "ses:SendEmail",
    "ses:SendRawEmail",
    "sesv2:SendEmail",
    "lambda:InvokeFunction",
    "lambda:InvokeAsync",
    "bedrock-agentcore:InvokeAgentRuntime",
    "scheduler:CreateSchedule",
    "scheduler:UpdateSchedule",
    "scheduler:DeleteSchedule",
    "secretsmanager:GetSecretValue",
    "kms:Decrypt",
    "kms:GenerateDataKey",
)
"""Every external effect and every escalation path.

``bedrock-agentcore:InvokeAgentRuntime`` is denied so an agent cannot call another agent, which
is what keeps "agents never call one another" an IAM fact rather than a coding convention.
"""


class ChorusAgentStack(Stack):
    """Creates the Monitor runtime execution role and its dedicated log group."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: CdkBuildConfig,
        monitor_model_profile_arn: str | None = None,
        artifact_bucket_arn: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id)
        Tags.of(self).add("Project", config.project)
        Tags.of(self).add("Environment", config.environment)
        Tags.of(self).add("Namespace", config.namespace)
        Tags.of(self).add("DataClass", "PRIVATE")

        self.monitor_log_group = logs.LogGroup(
            self,
            "MonitorRuntimeLogGroup",
            log_group_name=f"/aws/bedrock-agentcore/chorus-monitor-{config.environment}",
            retention=logs.RetentionDays.TWO_WEEKS,
        )

        self.monitor_role = iam.Role(
            self,
            "MonitorRuntimeRole",
            role_name=f"chorus-monitor-runtime-{config.environment}",
            assumed_by=iam.ServicePrincipal(AGENTCORE_SERVICE_PRINCIPAL),
            description="Monitor AgentCore runtime: model invocation and own telemetry only.",
        )

        profile_arn = monitor_model_profile_arn or (
            f"arn:aws:bedrock:{self.region}:{self.account}"
            f":application-inference-profile/chorus-monitor-{config.environment}"
        )
        self.monitor_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeMonitorInferenceProfileOnly",
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[profile_arn],
            )
        )
        self.monitor_role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteOwnLogsOnly",
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
                resources=[
                    self.monitor_log_group.log_group_arn,
                    f"{self.monitor_log_group.log_group_arn}:log-stream:*",
                ],
            )
        )
        self.monitor_role.add_to_policy(
            iam.PolicyStatement(
                sid="EmitOwnTraces",
                effect=iam.Effect.ALLOW,
                actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                resources=["*"],
            )
        )
        if artifact_bucket_arn is not None:
            self.monitor_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ReadOwnDirectCodeArtifact",
                    effect=iam.Effect.ALLOW,
                    actions=["s3:GetObject"],
                    resources=[f"{artifact_bucket_arn}/monitor/*"],
                )
            )

        self.monitor_role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyEveryDataStore",
                effect=iam.Effect.DENY,
                actions=list(DENIED_DATA_PLANE_ACTIONS),
                resources=["*"],
            )
        )
        self.monitor_role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyEveryExternalEffect",
                effect=iam.Effect.DENY,
                actions=list(DENIED_SIDE_EFFECT_ACTIONS),
                resources=["*"],
            )
        )
