"""Agent resources: each runtime's identity and its boundary.

What this stack creates is one IAM role and one log group **per agent runtime**. That is the
part that matters for the security argument: an agent's isolation is an identity property, not
a code property, so it can be asserted from the synthesized template long before a runtime is
deployed.

Phase 5 added the Investigator beside the Monitor and Phase 7 adds the Action runtime beside
both. All three roles are built by the same helper from the same denied-action lists, because
"the Action runtime is isolated like the others" has to be a fact about one construction rather
than a resemblance between three hand-written blocks. Each allow list is narrower in exactly one
respect and wider in none: each invokes its own inference profile and no other agent's.

Neither AgentCore runtime *resource* is created here, the Action one included. What is created
is identity and boundary, which is the part that can be asserted from a synthesized template
long before a runtime exists -- and the part the security argument actually rests on.

Each role's allow list is three things -- invoke exactly one inference profile, write to its own
log group, read its own artifact -- and its deny list is everything the frozen trust matrix
marks ``D``.

No AgentCore runtime resource is created here. The frozen network design requires VPC mode in
two isolated subnets with no NAT route, and that VPC belongs to the deployment stack. Creating a
public-mode runtime now to have something to point at would contradict the design it is supposed
to satisfy.
"""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import ArnFormat, CfnOutput, Environment, Stack, Tags
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.cdk.config import PHASE_11_REGION, CdkBuildConfig
from infra.cdk.runtime_support import runtime_artifact_location

AGENTCORE_SERVICE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"

DENIED_DATASTORE_ACTIONS = (
    "dynamodb:GetItem",
    "dynamodb:BatchGetItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:TransactWriteItems",
    "dynamodb:TransactGetItems",
)
"""Every DynamoDB action, denied table-wide on ``*``. No agent runtime reaches a data store.

The Monitor and the Investigator are inside the private zone and are deliberately *given*
private text in their payloads. That is exactly why neither may also read the stores: a
compromised runtime should be limited to the one payload it was handed, not to the corpus. The
point is sharper for the Investigator, whose payload is one whole case -- reading the tables
would turn a single case's exposure into every case's.

The Action runtime is handed nothing private at all: its payload is the compiled external-safe
view. It is denied the same actions anyway, and that is the interesting case rather than the
redundant one -- a runtime that could read the Shareable table could read *other cases'* views,
and a runtime that could read Core could read the private facts its own view was compiled to
exclude. Being given only safe data is not the same as being unable to reach unsafe data.
"""

DENIED_EVIDENCE_OBJECT_ACTIONS = (
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:ListBucket",
)
"""Read and write, denied on the private and export evidence buckets **by name**.

This used to be a blanket ``s3:GetObject`` deny on ``*`` folded into the data-store list -- and
because an explicit deny always wins, it also caught each runtime's own direct-code artifact
object, so no AgentCore runtime could cold-start (deployment contract SS 6). Splitting it so the
``GetObject`` denial is scoped to the evidence resources it is actually about is *stronger*, not
weaker: private-evidence and export isolation is now stated against those buckets, and the
artifact prefix each runtime must read is left readable. No agent can read either evidence
bucket, and the DynamoDB denies above are untouched.
"""

DENIED_OBJECT_MUTATION_ACTIONS = (
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:ListBucket",
)
"""Write and list, denied on ``*``.

A runtime reads exactly its own artifact prefix and nothing else. It never writes an object
anywhere and never enumerates a bucket, so these stay a wildcard deny -- an action wildcard
inside a deny is the strongest form, and there is no legitimate object write on any of these
roles for it to collide with.
"""

DENIED_DATA_PLANE_ACTIONS = (*DENIED_DATASTORE_ACTIONS, *DENIED_EVIDENCE_OBJECT_ACTIONS)
"""The union an external sweep still reasons about: every table action plus every evidence
object action. Retained as the name other modules and tests import; the two denies it spans are
now written as separate statements so the artifact read is not caught by the object one."""

DENIED_SIDE_EFFECT_ACTIONS = (
    "ses:SendEmail",
    "ses:SendRawEmail",
    "sesv2:SendEmail",
    "sesv2:SendBulkEmail",
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

The SES entries matter most for the Action runtime, and they are the reason this list is shared
rather than per-agent: it is the agent that drafts an external message, so "it cannot send one"
should be the same denied action for it as for the two agents that were never going to try.
``secretsmanager:GetSecretValue`` is denied for the same shape of reason -- the sending identity
the preview binds is deployment configuration held by the application, and no agent runtime ever
reads a secret to obtain it.
"""


# -- I4: Nova 2 Lite through one application inference profile per agent -----------------

NOVA_2_LITE_BASE_MODEL_ID = "amazon.nova-2-lite-v1:0"
"""The frozen base model (deployment contract § 4). Not substituted, not a Nova Lite fallback."""

NOVA_2_LITE_US_SYSTEM_PROFILE_ID = "us.amazon.nova-2-lite-v1:0"
"""The US geographic cross-region *system* inference profile each agent's *application*
profile is derived from. Recorded here for provenance; nothing in this stack invokes it by id --
the runtime is handed its own application-profile ARN as a discovered deployment input."""

US_INFERENCE_PROFILE_DESTINATION_REGIONS = ("us-east-1", "us-east-2", "us-west-2")
"""The regions the US geographic profile currently routes inference to (deployment contract
§ 4). The deploy CLI re-reads this from the system profile's own ``models`` list and fails if it
has grown a region this tuple does not name; the tuple is the policy-side copy that list is
checked against."""

INFERENCE_PROFILE_ARN_CONDITION_KEY = "bedrock:InferenceProfileArn"
"""The Bedrock condition key that binds a foundation-model grant to one inference profile
(deployment contract § 4). The foundation-model statement is usable *only* through the profile
whose ARN this key equals, so the grant is not a direct unrestricted model invocation."""

RUNTIME_MODEL_ACTIONS = ("bedrock:InvokeModelWithResponseStream",)
"""The single action required for Bedrock model invocation by the agent runtimes.

Traced through ``strands-agents 1.54.0``:
* ``runtimes/*/agent.py`` calls ``agent.structured_output_async(...)``;
* ``BedrockModel.structured_output`` calls ``self.stream(...)``;
* inside ``stream``, the model source reads ``streaming = self.config.get("streaming", True)``
  and then ``converse_method = self.client.converse_stream if streaming else self.client.converse``;
* no runtime constructs ``BedrockModel(streaming=False)`` -- none passes ``streaming`` at all.

So the only Bedrock data-plane call any runtime makes is ``ConverseStream``, which IAM authorizes
through ``bedrock:InvokeModelWithResponseStream``. ``bedrock:InvokeModel`` was removed because
``Converse`` is never called (unused grant, Macro B § 7). A future adapter that sets
``streaming=False`` must add ``bedrock:InvokeModel`` back, and the test asserting the exact action
set is what will force that. No ``Converse``/``ConverseStream`` grant -- those are the data-plane
API names, not the IAM actions Bedrock evaluates.
"""


def _foundation_model_arns() -> list[str]:
    """The Nova 2 Lite foundation-model ARNs for every frozen US destination region.

    A foundation-model ARN carries **no account id** -- the resource is AWS-owned -- so the
    account is never interpolated here. These are the routes the US geographic profile fans an
    invocation out to; a role that names its profile but not these gets ``AccessDenied`` from a
    region the request never mentioned, which reads as a model error (deployment contract § 4).
    """

    return [
        f"arn:aws:bedrock:{region}::foundation-model/{NOVA_2_LITE_BASE_MODEL_ID}"
        for region in US_INFERENCE_PROFILE_DESTINATION_REGIONS
    ]


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeStatementIds:
    """The policy statement identifiers one runtime's boundary is written under.

    Carried as data because a statement ID is part of a *deployed* policy's identity:
    renaming the Monitor's for symmetry with a later agent would rewrite a deployed
    artifact for no security gain. So the Monitor keeps the identifiers it already has,
    the Investigator gets its own, and the construction that produces both is shared.
    """

    invoke_profile: str
    invoke_foundation_models: str
    write_logs: str
    emit_traces: str
    read_artifact: str
    deny_data: str
    deny_evidence_objects: str
    deny_object_mutation: str
    deny_effects: str


MONITOR_STATEMENT_IDS = RuntimeStatementIds(
    invoke_profile="InvokeMonitorInferenceProfileOnly",
    invoke_foundation_models="InvokeMonitorFoundationModelsViaProfileOnly",
    write_logs="WriteOwnLogsOnly",
    emit_traces="EmitOwnTraces",
    read_artifact="ReadOwnDirectCodeArtifact",
    deny_data="DenyEveryDataStore",
    deny_evidence_objects="DenyEvidenceObjectAccess",
    deny_object_mutation="DenyObjectMutation",
    deny_effects="DenyEveryExternalEffect",
)

INVESTIGATOR_STATEMENT_IDS = RuntimeStatementIds(
    invoke_profile="InvokeInvestigatorInferenceProfileOnly",
    invoke_foundation_models="InvokeInvestigatorFoundationModelsViaProfileOnly",
    write_logs="WriteOwnInvestigatorLogsOnly",
    emit_traces="EmitOwnInvestigatorTraces",
    read_artifact="ReadOwnInvestigatorArtifact",
    deny_data="DenyEveryDataStoreForInvestigator",
    deny_evidence_objects="DenyEvidenceObjectAccessForInvestigator",
    deny_object_mutation="DenyObjectMutationForInvestigator",
    deny_effects="DenyEveryExternalEffectForInvestigator",
)

ACTION_STATEMENT_IDS = RuntimeStatementIds(
    invoke_profile="InvokeActionInferenceProfileOnly",
    invoke_foundation_models="InvokeActionFoundationModelsViaProfileOnly",
    write_logs="WriteOwnActionLogsOnly",
    emit_traces="EmitOwnActionTraces",
    read_artifact="ReadOwnActionArtifact",
    deny_data="DenyEveryDataStoreForAction",
    deny_evidence_objects="DenyEvidenceObjectAccessForAction",
    deny_object_mutation="DenyObjectMutationForAction",
    deny_effects="DenyEveryExternalEffectForAction",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeSpec:
    """Specification for constructing one AgentCore runtime and its application profile."""

    agent: str
    construct_prefix: str
    role: iam.Role
    log_group: logs.LogGroup
    statement_ids: RuntimeStatementIds
    security_group: ec2.ISecurityGroup
    manage_delivery_resource_policy: bool
    description: str


class ChorusAgentStack(Stack):
    """Creates each agent runtime's execution role and its dedicated log group."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: CdkBuildConfig,
        artifact_bucket_arn: str | None = None,
        vpc: ec2.IVpc | None = None,
        vpc_subnets: ec2.SubnetSelection | None = None,
        monitor_security_group: ec2.ISecurityGroup | None = None,
        investigator_security_group: ec2.ISecurityGroup | None = None,
        action_security_group: ec2.ISecurityGroup | None = None,
        artifact_bucket_name: str | None = None,
        offline_synth: bool = True,
        env: Environment | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env)
        Tags.of(self).add("Project", config.project)
        Tags.of(self).add("Environment", config.environment)
        Tags.of(self).add("Namespace", config.namespace)
        Tags.of(self).add("DataClass", "PRIVATE")

        self.monitor_runtime: agentcore.Runtime | None = None
        self.monitor_live_endpoint: agentcore.RuntimeEndpoint | None = None
        self.monitor_inference_profile: bedrock.CfnApplicationInferenceProfile | None = None

        self.investigator_runtime: agentcore.Runtime | None = None
        self.investigator_live_endpoint: agentcore.RuntimeEndpoint | None = None
        self.investigator_inference_profile: bedrock.CfnApplicationInferenceProfile | None = None

        self.action_runtime: agentcore.Runtime | None = None
        self.action_live_endpoint: agentcore.RuntimeEndpoint | None = None
        self.action_inference_profile: bedrock.CfnApplicationInferenceProfile | None = None

        runtime_inputs = {
            "vpc": vpc,
            "vpc_subnets": vpc_subnets,
            "monitor_security_group": monitor_security_group,
            "investigator_security_group": investigator_security_group,
            "action_security_group": action_security_group,
            "artifact_bucket_name": artifact_bucket_name,
        }
        supplied_runtime = {name for name, val in runtime_inputs.items() if val is not None}
        if supplied_runtime and supplied_runtime != set(runtime_inputs):
            missing_runtime = sorted(set(runtime_inputs) - supplied_runtime)
            raise ValueError(
                "AgentCore runtime networking and artifact bucket must be supplied together "
                f"or not at all; missing: {', '.join(missing_runtime)}"
            )

        # The two evidence buckets, named exactly as the data stack names them so the
        # ``GetObject`` deny can be stated against the resources it protects rather than
        # against ``*``. Literals rather than a cross-stack reference: the deny must be present
        # and correctly scoped even when this stack is synthesized on its own.
        self._private_bucket_arn = f"arn:aws:s3:::chorus-private-evidence-{config.environment}"
        self._export_bucket_arn = f"arn:aws:s3:::chorus-export-evidence-{config.environment}"

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

        self.action_log_group = logs.LogGroup(
            self,
            "ActionRuntimeLogGroup",
            log_group_name=f"/aws/bedrock-agentcore/chorus-action-{config.environment}",
            retention=logs.RetentionDays.TWO_WEEKS,
        )
        self.action_role = iam.Role(
            self,
            "ActionRuntimeRole",
            role_name=f"chorus-action-runtime-{config.environment}",
            assumed_by=iam.ServicePrincipal(AGENTCORE_SERVICE_PRINCIPAL),
            description="Action AgentCore runtime: model invocation and own telemetry only.",
        )

        # The runtimes exist only once every network and artifact input is present. The guard is
        # written as six explicit ``is not None`` tests rather than as a set comparison against
        # ``runtime_inputs`` so the narrowing is visible to the type checker as well as to a
        # reader: every value below is used unconditionally, and none needs a suppression to say
        # so. The all-or-nothing refusal above is what makes reaching here with a partial set
        # impossible.
        if (
            vpc is not None
            and vpc_subnets is not None
            and monitor_security_group is not None
            and investigator_security_group is not None
            and action_security_group is not None
            and artifact_bucket_name is not None
        ):
            system_profile_arn = self.format_arn(
                service="bedrock",
                region=PHASE_11_REGION,
                account=self.account,
                resource="inference-profile",
                resource_name=NOVA_2_LITE_US_SYSTEM_PROFILE_ID,
                arn_format=ArnFormat.SLASH_RESOURCE_NAME,
            )

            specs = (
                RuntimeSpec(
                    agent="monitor",
                    construct_prefix="Monitor",
                    role=self.monitor_role,
                    log_group=self.monitor_log_group,
                    statement_ids=MONITOR_STATEMENT_IDS,
                    security_group=monitor_security_group,
                    manage_delivery_resource_policy=True,
                    description="CHORUS Monitor AgentCore runtime",
                ),
                RuntimeSpec(
                    agent="investigator",
                    construct_prefix="Investigator",
                    role=self.investigator_role,
                    log_group=self.investigator_log_group,
                    statement_ids=INVESTIGATOR_STATEMENT_IDS,
                    security_group=investigator_security_group,
                    manage_delivery_resource_policy=False,
                    description="CHORUS Investigator AgentCore runtime",
                ),
                RuntimeSpec(
                    agent="action",
                    construct_prefix="Action",
                    role=self.action_role,
                    log_group=self.action_log_group,
                    statement_ids=ACTION_STATEMENT_IDS,
                    security_group=action_security_group,
                    manage_delivery_resource_policy=False,
                    description="CHORUS Action AgentCore runtime",
                ),
            )

            for spec in specs:
                profile = bedrock.CfnApplicationInferenceProfile(
                    self,
                    f"{spec.construct_prefix}InferenceProfile",
                    inference_profile_name=f"chorus-{spec.agent}-{config.environment}",
                    description=f"CHORUS {spec.agent} application inference profile",
                    model_source=bedrock.CfnApplicationInferenceProfile.InferenceProfileModelSourceProperty(
                        copy_from=system_profile_arn,
                    ),
                )

                self._grant_runtime_boundary(
                    role=spec.role,
                    log_group=spec.log_group,
                    profile_arn=profile.attr_inference_profile_arn,
                    artifact_bucket_arn=artifact_bucket_arn,
                    artifact_prefix=spec.agent,
                    sids=spec.statement_ids,
                )

                artifact_loc = runtime_artifact_location(
                    spec.agent,
                    bucket_name=artifact_bucket_name,
                    offline=offline_synth,
                )

                # **Do not replace this with ``spec.role``.** It looks like an indirection that
                # buys nothing, and removing it silently widens all three runtime roles.
                #
                # ``agentcore.Runtime`` calls ``grant()`` on whatever role it is handed. Given
                # the real mutable ``iam.Role``, it appends -- with no warning and no opt-out --
                # ``logs:CreateLogGroup``, ``logs:DescribeLogGroups``,
                # ``cloudwatch:PutMetricData``, the three
                # ``bedrock-agentcore:GetWorkloadAccessToken*`` actions, and
                # ``s3:GetObject*``/``s3:GetBucket*``/``s3:List*`` **on the entire artifact
                # bucket**. That last one is the serious one: it would let every runtime read
                # every other runtime's artifact, which is precisely the isolation deployment
                # contract § 12 exists to state, and which ``ReadOwn*Artifact`` scopes to one
                # prefix. The workload-token family is a capability no accepted document grants,
                # and the manifests declare every AgentCore capability ``false``.
                #
                # An **immutable** reference to the same role makes CDK skip every one of those
                # grants while still resolving to the real role's ``Fn::GetAtt ... Arn``, so the
                # deployed identity is unchanged and its policy is exactly what this stack wrote.
                # ``test_runtime_execution_roles_gain_no_implicit_grants`` is the regression
                # guard; the stack keeps using the mutable ``spec.role`` for its own statements.
                frozen_role = iam.Role.from_role_arn(
                    self,
                    f"{spec.construct_prefix}RuntimeRoleRef",
                    spec.role.role_arn,
                    mutable=False,
                )

                net_config = agentcore.RuntimeNetworkConfiguration.using_vpc(
                    self,
                    vpc=vpc,
                    security_groups=[spec.security_group],
                    vpc_subnets=vpc_subnets,
                )

                runtime = agentcore.Runtime(
                    self,
                    f"{spec.construct_prefix}Runtime",
                    runtime_name=artifact_loc.deployed_name,
                    agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_s3(
                        s3.Location(
                            bucket_name=artifact_loc.bucket_name,
                            object_key=artifact_loc.object_key,
                        ),
                        agentcore.AgentCoreRuntime.PYTHON_3_12,
                        ["python", "main.py"],
                    ),
                    execution_role=frozen_role,
                    environment_variables={
                        f"CHORUS_{spec.agent.upper()}_MODEL_PROFILE_ARN": (
                            profile.attr_inference_profile_arn
                        ),
                        "CHORUS_AWS_REGION": PHASE_11_REGION,
                        "AWS_REGION": PHASE_11_REGION,
                        "CHORUS_OTEL_ENABLED": "false",
                    },
                    network_configuration=net_config,
                    authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_iam(),
                    logging_configs=[
                        agentcore.LoggingConfig(
                            destination=agentcore.LoggingDestination.cloud_watch_logs(
                                spec.log_group
                            ),
                            log_type=agentcore.LogType.APPLICATION_LOGS,
                        )
                    ],
                    manage_delivery_resource_policy=spec.manage_delivery_resource_policy,
                    tracing_enabled=False,
                    description=spec.description,
                )
                endpoint = runtime.add_endpoint("live")

                if spec.agent == "monitor":
                    self.monitor_runtime = runtime
                    self.monitor_live_endpoint = endpoint
                    self.monitor_inference_profile = profile
                elif spec.agent == "investigator":
                    self.investigator_runtime = runtime
                    self.investigator_live_endpoint = endpoint
                    self.investigator_inference_profile = profile
                elif spec.agent == "action":
                    self.action_runtime = runtime
                    self.action_live_endpoint = endpoint
                    self.action_inference_profile = profile

                CfnOutput(
                    self,
                    f"{spec.construct_prefix}InferenceProfileArn",
                    value=profile.attr_inference_profile_arn,
                )
                CfnOutput(
                    self,
                    f"{spec.construct_prefix}RuntimeArn",
                    value=runtime.agent_runtime_arn,
                )
                CfnOutput(
                    self,
                    f"{spec.construct_prefix}RuntimeId",
                    value=runtime.agent_runtime_id,
                )
                CfnOutput(
                    self,
                    f"{spec.construct_prefix}LiveEndpointArn",
                    value=endpoint.agent_runtime_endpoint_arn,
                )
        else:
            self._grant_runtime_boundary(
                role=self.monitor_role,
                log_group=self.monitor_log_group,
                profile_arn=None,
                artifact_bucket_arn=artifact_bucket_arn,
                artifact_prefix="monitor",
                sids=MONITOR_STATEMENT_IDS,
            )
            self._grant_runtime_boundary(
                role=self.investigator_role,
                log_group=self.investigator_log_group,
                profile_arn=None,
                artifact_bucket_arn=artifact_bucket_arn,
                artifact_prefix="investigator",
                sids=INVESTIGATOR_STATEMENT_IDS,
            )
            self._grant_runtime_boundary(
                role=self.action_role,
                log_group=self.action_log_group,
                profile_arn=None,
                artifact_bucket_arn=artifact_bucket_arn,
                artifact_prefix="action",
                sids=ACTION_STATEMENT_IDS,
            )

    def _grant_runtime_boundary(
        self,
        *,
        role: iam.Role,
        log_group: logs.LogGroup,
        profile_arn: str | None,
        artifact_bucket_arn: str | None,
        artifact_prefix: str,
        sids: RuntimeStatementIds,
    ) -> None:
        """Attach one agent runtime's complete allow list and its explicit denies.

        One helper for every agent, so the boundary is a property of one construction rather
        than of three blocks that happen to look alike today. The denies are defence in depth:
        none of them is reachable through an allow, and an explicit deny cannot be overridden by
        a later grant, so a future change that accidentally attaches a data policy to one of
        these roles still fails closed.

        The S3 deny is deliberately in two parts. ``deny_evidence_objects`` names the private
        and export buckets and denies read and write on both; ``deny_object_mutation`` denies
        write and list on ``*``. Together they forbid every object write anywhere, every bucket
        listing, and any read of either evidence bucket -- while leaving ``s3:GetObject`` on the
        runtime's own artifact prefix reachable, which a blanket ``GetObject`` deny made
        impossible (deployment contract SS 6).

        I4: the model grant is two statements and appears only once ``profile_arn`` is
        available from the created application inference profile. ``invoke_profile`` allows
        :data:`RUNTIME_MODEL_ACTIONS` (``bedrock:InvokeModelWithResponseStream`` -- the
        runtime's ``strands`` structured-output call streams by default) on that exact
        application inference-profile ARN; ``invoke_foundation_models`` allows the same action
        on the Nova 2 Lite foundation-model ARNs for every frozen US destination region,
        **condition-bound** to the same profile through ``bedrock:InferenceProfileArn`` so the
        foundation-model grant is usable only through the profile it belongs to and is not a
        direct unrestricted model invocation (deployment contract § 4).
        """

        if profile_arn is not None:
            role.add_to_policy(
                iam.PolicyStatement(
                    sid=sids.invoke_profile,
                    effect=iam.Effect.ALLOW,
                    actions=list(RUNTIME_MODEL_ACTIONS),
                    resources=[profile_arn],
                )
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    sid=sids.invoke_foundation_models,
                    effect=iam.Effect.ALLOW,
                    actions=list(RUNTIME_MODEL_ACTIONS),
                    resources=_foundation_model_arns(),
                    conditions={"StringEquals": {INFERENCE_PROFILE_ARN_CONDITION_KEY: profile_arn}},
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
                actions=list(DENIED_DATASTORE_ACTIONS),
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid=sids.deny_evidence_objects,
                effect=iam.Effect.DENY,
                actions=list(DENIED_EVIDENCE_OBJECT_ACTIONS),
                resources=[
                    self._private_bucket_arn,
                    f"{self._private_bucket_arn}/*",
                    self._export_bucket_arn,
                    f"{self._export_bucket_arn}/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid=sids.deny_object_mutation,
                effect=iam.Effect.DENY,
                actions=list(DENIED_OBJECT_MUTATION_ACTIONS),
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
