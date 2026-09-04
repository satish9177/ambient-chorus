"""Agent resources: each runtime's identity and its boundary.

What this stack creates is one IAM role and one log group **per agent runtime**. That is the
part that matters for the security argument: an agent's isolation is an identity property, not
a code property, so it can be asserted from the synthesized template long before a runtime is
deployed.

Phase 5 adds the Investigator beside the Monitor. The two roles are built by the same helper
from the same denied-action lists, because "the Investigator is isolated like the Monitor" has
to be a fact about one construction rather than a resemblance between two hand-written blocks.
The Investigator's allow list is narrower in exactly one respect and wider in none: it invokes
its own inference profile, not the Monitor's.

Each role's allow list is three things -- invoke exactly one inference profile, write to its own
log group, read its own artifact -- and its deny list is everything the frozen trust matrix
marks ``D``.

Neither AgentCore runtime resource is created here. The frozen network
design requires VPC mode in two isolated subnets with no NAT route, and that VPC belongs to
the deployment stack. Creating a public-mode runtime now to have something to point at would
contradict the design it is supposed to satisfy.
"""

from __future__ import annotations

from dataclasses import dataclass

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
"""Storage no agent runtime may ever touch.

Both agents are inside the private zone and are deliberately *given* private text in their
payloads. That is exactly why neither may also read the stores: a compromised runtime should be
limited to the one payload it was handed, not to the corpus. The point is sharper for the
Investigator, whose payload is one whole case -- reading the tables would turn a single case's
exposure into every case's.
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


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeStatementIds:
    """The policy statement identifiers one runtime's boundary is written under.

    Carried as data because a statement ID is part of a *deployed* policy's identity:
    renaming the Monitor's for symmetry with a later agent would rewrite a deployed
    artifact for no security gain. So the Monitor keeps the identifiers it already has,
    the Investigator gets its own, and the construction that produces both is shared.
    """

    invoke_profile: str
    write_logs: str
    emit_traces: str
    read_artifact: str
    deny_data: str
    deny_effects: str


MONITOR_STATEMENT_IDS = RuntimeStatementIds(
    invoke_profile="InvokeMonitorInferenceProfileOnly",
    write_logs="WriteOwnLogsOnly",
    emit_traces="EmitOwnTraces",
    read_artifact="ReadOwnDirectCodeArtifact",
    deny_data="DenyEveryDataStore",
    deny_effects="DenyEveryExternalEffect",
)

INVESTIGATOR_STATEMENT_IDS = RuntimeStatementIds(
    invoke_profile="InvokeInvestigatorInferenceProfileOnly",
    write_logs="WriteOwnInvestigatorLogsOnly",
    emit_traces="EmitOwnInvestigatorTraces",
    read_artifact="ReadOwnInvestigatorArtifact",
    deny_data="DenyEveryDataStoreForInvestigator",
    deny_effects="DenyEveryExternalEffectForInvestigator",
)


class ChorusAgentStack(Stack):
    """Creates each agent runtime's execution role and its dedicated log group."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: CdkBuildConfig,
        monitor_model_profile_arn: str | None = None,
        investigator_model_profile_arn: str | None = None,
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
        self._grant_runtime_boundary(
            role=self.monitor_role,
            log_group=self.monitor_log_group,
            profile_arn=monitor_model_profile_arn
            or self._default_profile_arn("monitor", config.environment),
            artifact_bucket_arn=artifact_bucket_arn,
            artifact_prefix="monitor",
            sids=MONITOR_STATEMENT_IDS,
        )

        self.investigator_log_group = logs.LogGroup(
            self,
            "InvestigatorRuntimeLogGroup",
            log_group_name=f"/aws/bedrock-agentcore/chorus-investigator-{config.environment}",
            retention=logs.RetentionDays.TWO_WEEKS,
        )
        self.investigator_role = iam.Role(
            self,
            "InvestigatorRuntimeRole",
            role_name=f"chorus-investigator-runtime-{config.environment}",
            assumed_by=iam.ServicePrincipal(AGENTCORE_SERVICE_PRINCIPAL),
            description=(
                "Investigator AgentCore runtime: model invocation and own telemetry only."
            ),
        )
        self._grant_runtime_boundary(
            role=self.investigator_role,
            log_group=self.investigator_log_group,
            profile_arn=investigator_model_profile_arn
            or self._default_profile_arn("investigator", config.environment),
            artifact_bucket_arn=artifact_bucket_arn,
            artifact_prefix="investigator",
            sids=INVESTIGATOR_STATEMENT_IDS,
        )

    def _default_profile_arn(self, agent: str, environment: str) -> str:
        return (
            f"arn:aws:bedrock:{self.region}:{self.account}"
            f":application-inference-profile/chorus-{agent}-{environment}"
        )

    def _grant_runtime_boundary(
        self,
        *,
        role: iam.Role,
        log_group: logs.LogGroup,
        profile_arn: str,
        artifact_bucket_arn: str | None,
        artifact_prefix: str,
        sids: RuntimeStatementIds,
    ) -> None:
        """Attach one agent runtime's complete allow list and its two explicit denies.

        One helper for every agent, so the boundary is a property of one construction rather
        than of two blocks that happen to look alike today. The denies are defence in depth:
        none of them is reachable through an allow, and an explicit deny cannot be overridden by
        a later grant, so a future change that accidentally attaches a data policy to one of
        these roles still fails closed.
        """

        role.add_to_policy(
            iam.PolicyStatement(
                sid=sids.invoke_profile,
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[profile_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid=sids.write_logs,
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
                resources=[
                    log_group.log_group_arn,
                    f"{log_group.log_group_arn}:log-stream:*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid=sids.emit_traces,
                effect=iam.Effect.ALLOW,
                actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                resources=["*"],
            )
        )
        if artifact_bucket_arn is not None:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid=sids.read_artifact,
                    effect=iam.Effect.ALLOW,
                    actions=["s3:GetObject"],
                    resources=[f"{artifact_bucket_arn}/{artifact_prefix}/*"],
                )
            )
        role.add_to_policy(
            iam.PolicyStatement(
                sid=sids.deny_data,
                effect=iam.Effect.DENY,
                actions=list(DENIED_DATA_PLANE_ACTIONS),
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid=sids.deny_effects,
                effect=iam.Effect.DENY,
                actions=list(DENIED_SIDE_EFFECT_ACTIONS),
                resources=["*"],
            )
        )
