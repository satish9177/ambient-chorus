"""The dedicated isolated network: one VPC, two isolated subnets, a frozen endpoint inventory.

Macro A's network foundation (deployment contract §§ 6-9, 16;
[ADR-010](../../../docs/adr/ADR-010-agentcore-runtime.md)). ADR-010 freezes VPC mode in two
isolated subnets with **no NAT and no internet route** as a security decision -- the egress
boundary, not a cost choice -- so this stack has no ``NatGateway``, no ``InternetGateway``, and
no ``EIP``, and a template test asserts their absence.

What this stack owns, and why it is its own stack
-------------------------------------------------
The VPC, the two isolated subnets, their route tables, the six interface endpoints and two
gateway endpoints of the frozen inventory, the endpoint security groups, and the four
VPC-attached workload security groups (worker, compiler, sender, and the reset principal). It
is deliberately **not** hidden inside the Application/Compiler/Sender stacks (deployment
contract § 3): a network boundary that is asserted from a template has to be a template of its
own, and Macro B attaches the three AgentCore runtimes and the inbound Lambda to these same
subnets and endpoints without touching a compute stack.

Foundational, and acyclic (deployment contract § 18)
---------------------------------------------------
The endpoint policies name the CHORUS table and evidence-bucket ARNs, but as **deterministic
literals derived from the environment token** -- never live ``s3.IBucket`` / ``dynamodb.ITable``
references -- so this stack takes no dependency on the Data stack and stays first in the DAG.
The compute stacks and the Reset stack depend on *it* (they consume its subnets and security
groups); nothing here depends on them.

No account lookup
-----------------
``NetworkConfig`` supplies the two isolated-subnet AZ names from deployment configuration
(``-c network_availability_zones=<a>,<b>``); there is no ``Vpc.from_lookup``, no
``DescribeAvailabilityZones``, and no default VPC. Offline synth uses a clearly-named synthetic
AZ fixture; deployment mode fails closed without the real names (deployment contract §§ 5-6).
"""

from __future__ import annotations

from typing import Final

from aws_cdk import CfnOutput, Environment, Fn, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from constructs import Construct

from infra.cdk.config import PHASE_11_REGION, CdkBuildConfig
from infra.cdk.network_support import (
    AGENT_RUNTIME_WORKLOADS,
    GATEWAY_DYNAMODB_REACHABLE_BY,
    GATEWAY_S3_REACHABLE_BY,
    HTTPS_PORT,
    INTERFACE_ENDPOINTS,
    ISOLATED_SUBNET_CIDR_MASK,
    MANAGED_PREFIX_LIST_DYNAMODB,
    MANAGED_PREFIX_LIST_S3,
    VPC_CIDR,
    VPC_WORKLOADS,
    WORKLOAD_ACTION_RUNTIME,
    WORKLOAD_COMPILER,
    WORKLOAD_INBOUND,
    WORKLOAD_INVESTIGATOR_RUNTIME,
    WORKLOAD_MONITOR_RUNTIME,
    WORKLOAD_RESET,
    WORKLOAD_SENDER,
    WORKLOAD_WORKER,
    NetworkConfig,
)

_WORKLOAD_SECURITY_GROUP_IDENTITY: Final = {
    WORKLOAD_WORKER: ("WorkerSecurityGroup", "chorus-worker-{env}"),
    WORKLOAD_COMPILER: ("CompilerSecurityGroup", "chorus-compiler-{env}"),
    WORKLOAD_SENDER: ("SenderSecurityGroup", "chorus-sender-{env}"),
    WORKLOAD_RESET: ("ResetSecurityGroup", "chorus-reset-{env}"),
    WORKLOAD_INBOUND: ("InboundSecurityGroup", "chorus-inbound-{env}"),
    WORKLOAD_MONITOR_RUNTIME: ("MonitorRuntimeSecurityGroup", "chorus-monitor-runtime-{env}"),
    WORKLOAD_INVESTIGATOR_RUNTIME: (
        "InvestigatorRuntimeSecurityGroup",
        "chorus-investigator-runtime-{env}",
    ),
    WORKLOAD_ACTION_RUNTIME: ("ActionRuntimeSecurityGroup", "chorus-action-runtime-{env}"),
}

ISOLATED_SUBNET_GROUP_NAME = "isolated"
"""The one subnet group. ``PRIVATE_ISOLATED`` with ``nat_gateways=0`` and no public group is
what makes CDK synthesize no IGW, no NAT, and no EIP."""

_S3_ARTIFACT_READ_ACTION = "s3:GetObject"
_EVIDENCE_OBJECT_ACTIONS = (
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:DeleteObjectVersion",
)
"""Object-level actions any CHORUS principal uses on an evidence object through the gateway
endpoint: the compiler / worker read and write, and the reset principal reads, reseeds, and
bounded-deletes the ``ns/DEMO/`` prefixes (review R5-A). The endpoint policy is an allowlist
*filter* only -- the role policies remain the authority, and reset's `DeleteObject` is still
scoped to ``ns/DEMO/*`` in its own IAM."""

# The AWS-documented service-owned bucket AgentCore direct-code runtimes fetch their artifact
# from during cold start (review R4). It is not a customer bucket, so it cannot be an entry in
# a closed allowlist of ours -- but it is also not "every bucket": the pattern is the regional
# ``acr-code-*`` bucket, and the read is admitted **only** for the AgentCore service principal.
_AGENTCORE_SERVICE_ARTIFACT_BUCKET_ARN = f"arn:aws:s3:::acr-code-*-{PHASE_11_REGION}-an/*"
_AGENTCORE_SERVICE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"
_DYNAMODB_ENDPOINT_ACTIONS = (
    "dynamodb:GetItem",
    "dynamodb:BatchGetItem",
    "dynamodb:Query",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:TransactGetItems",
    "dynamodb:TransactWriteItems",
)
"""The DynamoDB data-plane actions any CHORUS principal uses through the gateway endpoint. The
endpoint policy is an allowlist *filter* only -- it never grants, and every role-level
``dynamodb:LeadingKeys`` scope and explicit deny stays authoritative (deployment contract § 11)."""


class ChorusNetworkStack(Stack):
    """``AmbientChorusNetwork`` -- the isolated VPC and the frozen endpoint inventory."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: CdkBuildConfig,
        network: NetworkConfig,
        artifact_bucket_arn: str,
        env: Environment | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env)
        Tags.of(self).add("Project", config.project)
        Tags.of(self).add("Environment", config.environment)
        Tags.of(self).add("Namespace", config.namespace)
        Tags.of(self).add("DataClass", "NONE")

        self._config = config
        self._artifact_bucket_arn = artifact_bucket_arn

        # -- the VPC: exactly two isolated subnets, in two AZs, no egress path -----------------
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=f"chorus-{config.environment}",
            ip_addresses=ec2.IpAddresses.cidr(VPC_CIDR),
            availability_zones=list(network.availability_zones),
            nat_gateways=0,
            restrict_default_security_group=False,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name=ISOLATED_SUBNET_GROUP_NAME,
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=ISOLATED_SUBNET_CIDR_MASK,
                )
            ],
        )
        # No resource is attached to the VPC's default security group -- every workload and
        # every endpoint below uses an explicit, per-purpose SG. Restricting the default SG
        # would add a CDK custom-resource Lambda + role to a stack that otherwise owns no
        # compute, for a group nothing routes through in a VPC with no egress path.

        self.isolated_subnets = list(
            self.vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnets
        )
        self.isolated_subnet_selection = ec2.SubnetSelection(subnets=self.isolated_subnets)
        self.isolated_subnet_ids = [subnet.subnet_id for subnet in self.isolated_subnets]
        self.isolated_subnet_arns = [
            self.format_arn(
                service="ec2",
                region=PHASE_11_REGION,
                resource="subnet",
                resource_name=subnet.subnet_id,
            )
            for subnet in self.isolated_subnets
        ]

        # -- the seven VPC-attached workload security groups (deployment contract §§ 8, 14, 24) --
        self.workload_security_groups = {}
        for key in VPC_WORKLOADS:
            construct_id, name_template = _WORKLOAD_SECURITY_GROUP_IDENTITY[key]
            is_runtime = key in AGENT_RUNTIME_WORKLOADS
            desc = (
                f"CHORUS {key} AgentCore runtime: HTTPS to named endpoints only, no open egress."
                if is_runtime
                else f"CHORUS {key} Lambda: HTTPS to named endpoints only, no open egress."
            )
            self.workload_security_groups[key] = ec2.SecurityGroup(
                self,
                construct_id,
                vpc=self.vpc,
                security_group_name=name_template.format(env=config.environment),
                description=desc,
                allow_all_outbound=False,
            )

        self.worker_security_group = self.workload_security_groups[WORKLOAD_WORKER]
        self.compiler_security_group = self.workload_security_groups[WORKLOAD_COMPILER]
        self.sender_security_group = self.workload_security_groups[WORKLOAD_SENDER]
        self.reset_security_group = self.workload_security_groups[WORKLOAD_RESET]
        self.inbound_security_group = self.workload_security_groups[WORKLOAD_INBOUND]
        self.monitor_runtime_security_group = self.workload_security_groups[
            WORKLOAD_MONITOR_RUNTIME
        ]
        self.investigator_runtime_security_group = self.workload_security_groups[
            WORKLOAD_INVESTIGATOR_RUNTIME
        ]
        self.action_runtime_security_group = self.workload_security_groups[WORKLOAD_ACTION_RUNTIME]

        self._create_interface_endpoints()
        self._create_gateway_endpoints()
        self._declare_outputs()

    # -- interface endpoints: HTTPS only, from only the workloads that use each service -------

    def _create_interface_endpoints(self) -> None:
        """Six interface endpoints (deployment contract § 7), each with its own security group.

        Each endpoint SG accepts **TCP 443 only**, and only from the workload SGs the frozen
        matrix lists for that service; the corresponding workload SG gets a matching 443 egress
        rule. ``bedrock-runtime`` is created with an SG that has *no* ingress -- it is the Macro
        B AgentCore runtimes' endpoint, and no current Lambda SG may reach it (§ 8).
        """

        self.interface_endpoints: dict[str, ec2.InterfaceVpcEndpoint] = {}
        self.interface_endpoint_security_groups: dict[str, ec2.SecurityGroup] = {}

        for spec in INTERFACE_ENDPOINTS:
            endpoint_sg = ec2.SecurityGroup(
                self,
                f"{spec.logical_id}SecurityGroup",
                vpc=self.vpc,
                security_group_name=f"chorus-{spec.logical_id}-{self._config.environment}".lower(),
                description=(
                    f"{spec.logical_id}: HTTPS 443 ingress from named CHORUS workloads only."
                ),
                allow_all_outbound=False,
            )
            for workload_key in spec.reachable_by:
                workload_sg = self.workload_security_groups[workload_key]
                endpoint_sg.add_ingress_rule(
                    peer=workload_sg,
                    connection=ec2.Port.tcp(HTTPS_PORT),
                    description=f"{workload_key} -> {spec.logical_id}",
                )
                workload_sg.add_egress_rule(
                    peer=endpoint_sg,
                    connection=ec2.Port.tcp(HTTPS_PORT),
                    description=f"{workload_key} -> {spec.logical_id}",
                )

            endpoint = ec2.InterfaceVpcEndpoint(
                self,
                spec.logical_id,
                vpc=self.vpc,
                service=spec.service,
                subnets=self.isolated_subnet_selection,
                security_groups=[endpoint_sg],
                private_dns_enabled=True,
                open=False,
            )
            self.interface_endpoints[spec.logical_id] = endpoint
            self.interface_endpoint_security_groups[spec.logical_id] = endpoint_sg

    # -- gateway endpoints: S3 and DynamoDB, on both isolated route tables --------------------

    def _create_gateway_endpoints(self) -> None:
        """The two free gateway endpoints (deployment contract §§ 7, 9-11).

        Both attach to **both** isolated subnets' route tables. The workload SGs that use each
        service get a narrow TCP 443 egress rule to that service's AWS-managed prefix list --
        never an open ``0.0.0.0/0`` egress. Each endpoint carries a defence-in-depth policy;
        the role policies remain the authority.
        """

        self.s3_gateway_endpoint = self.vpc.add_gateway_endpoint(
            "S3GatewayEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[self.isolated_subnet_selection],
        )
        self.dynamodb_gateway_endpoint = self.vpc.add_gateway_endpoint(
            "DynamoDbGatewayEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
            subnets=[self.isolated_subnet_selection],
        )

        for workload_key in GATEWAY_S3_REACHABLE_BY:
            self.workload_security_groups[workload_key].add_egress_rule(
                peer=ec2.Peer.prefix_list(MANAGED_PREFIX_LIST_S3),
                connection=ec2.Port.tcp(HTTPS_PORT),
                description=f"{workload_key} -> S3 gateway endpoint",
            )
        for workload_key in GATEWAY_DYNAMODB_REACHABLE_BY:
            self.workload_security_groups[workload_key].add_egress_rule(
                peer=ec2.Peer.prefix_list(MANAGED_PREFIX_LIST_DYNAMODB),
                connection=ec2.Port.tcp(HTTPS_PORT),
                description=f"{workload_key} -> DynamoDB gateway endpoint",
            )

        self._attach_s3_endpoint_policy()
        self._attach_dynamodb_endpoint_policy()

    def _attach_s3_endpoint_policy(self) -> None:
        env = self._config.environment
        private_bucket = f"arn:aws:s3:::chorus-private-evidence-{env}"
        export_bucket = f"arn:aws:s3:::chorus-export-evidence-{env}"

        self.s3_gateway_endpoint.add_to_policy(
            iam.PolicyStatement(
                sid="ReadAgentArtifactPrefixes",
                effect=iam.Effect.ALLOW,
                principals=[iam.AnyPrincipal()],
                actions=[_S3_ARTIFACT_READ_ACTION],
                resources=[f"{self._artifact_bucket_arn}/*"],
            )
        )
        # Object-level access to the two evidence buckets -- read/write for the compiler and
        # worker, and read/reseed/bounded-delete for the reset principal (review R5-A). Without
        # `DeleteObject` / `DeleteObjectVersion` here the reset role's own IAM grant is
        # unusable through the gateway endpoint.
        self.s3_gateway_endpoint.add_to_policy(
            iam.PolicyStatement(
                sid="EvidenceObjectAccess",
                effect=iam.Effect.ALLOW,
                principals=[iam.AnyPrincipal()],
                actions=list(_EVIDENCE_OBJECT_ACTIONS),
                resources=[f"{private_bucket}/*", f"{export_bucket}/*"],
            )
        )
        # Bucket-level list, so reset can enumerate the `ns/DEMO/` prefixes it deletes (review
        # R5-A). Prefix-constrained at the endpoint as well as in the reset role's IAM.
        self.s3_gateway_endpoint.add_to_policy(
            iam.PolicyStatement(
                sid="ListDemoEvidencePrefixes",
                effect=iam.Effect.ALLOW,
                principals=[iam.AnyPrincipal()],
                actions=["s3:ListBucket"],
                resources=[private_bucket, export_bucket],
                conditions={"StringLike": {"s3:prefix": ["ns/DEMO/*"]}},
            )
        )
        # The AWS-documented service-owned bucket AgentCore direct-code runtimes fetch their
        # artifact from at cold start (review R4). Its ARN is not ours to enumerate, so it
        # cannot be a closed-allowlist entry -- but it is **not** "every bucket": the read is
        # the regional ``acr-code-*`` pattern and is admitted **only** for the AgentCore
        # service principal. A real cold start (canary J) is what proves the fetch mechanism;
        # this is the offline policy shape.
        self.s3_gateway_endpoint.add_to_policy(
            iam.PolicyStatement(
                sid="AllowAgentCoreServiceOwnedArtifactRead",
                effect=iam.Effect.ALLOW,
                principals=[iam.AnyPrincipal()],
                actions=[_S3_ARTIFACT_READ_ACTION],
                resources=[_AGENTCORE_SERVICE_ARTIFACT_BUCKET_ARN],
                conditions={
                    "StringEquals": {"aws:PrincipalServiceName": _AGENTCORE_SERVICE_PRINCIPAL}
                },
            )
        )

    def _attach_dynamodb_endpoint_policy(self) -> None:
        env = self._config.environment
        table_arns = [
            self.format_arn(
                service="dynamodb",
                region=PHASE_11_REGION,
                resource="table",
                resource_name=f"chorus-{name}-{env}",
            )
            for name in ("core", "shareable", "audit")
        ]
        self.dynamodb_gateway_endpoint.add_to_policy(
            iam.PolicyStatement(
                sid="ChorusTablesOnly",
                effect=iam.Effect.ALLOW,
                principals=[iam.AnyPrincipal()],
                actions=list(_DYNAMODB_ENDPOINT_ACTIONS),
                resources=table_arns,
            )
        )

    # -- outputs: only what Macro B and the deploy tooling need (deployment contract § 34) ----

    def _declare_outputs(self) -> None:
        CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        CfnOutput(self, "IsolatedSubnetIds", value=Fn.join(",", self.isolated_subnet_ids))
        CfnOutput(
            self,
            "IsolatedSubnetAvailabilityZones",
            value=Fn.join(",", [subnet.availability_zone for subnet in self.isolated_subnets]),
        )
        CfnOutput(self, "WorkerSecurityGroupId", value=self.worker_security_group.security_group_id)
        CfnOutput(
            self, "CompilerSecurityGroupId", value=self.compiler_security_group.security_group_id
        )
        CfnOutput(self, "SenderSecurityGroupId", value=self.sender_security_group.security_group_id)
        CfnOutput(self, "ResetSecurityGroupId", value=self.reset_security_group.security_group_id)
        CfnOutput(
            self, "InboundSecurityGroupId", value=self.inbound_security_group.security_group_id
        )
        CfnOutput(
            self,
            "MonitorRuntimeSecurityGroupId",
            value=self.monitor_runtime_security_group.security_group_id,
        )
        CfnOutput(
            self,
            "InvestigatorRuntimeSecurityGroupId",
            value=self.investigator_runtime_security_group.security_group_id,
        )
        CfnOutput(
            self,
            "ActionRuntimeSecurityGroupId",
            value=self.action_runtime_security_group.security_group_id,
        )
        CfnOutput(
            self,
            "InterfaceEndpointIds",
            value=Fn.join(
                ",",
                [endpoint.vpc_endpoint_id for endpoint in self.interface_endpoints.values()],
            ),
        )
        CfnOutput(self, "S3GatewayEndpointId", value=self.s3_gateway_endpoint.vpc_endpoint_id)
        CfnOutput(
            self,
            "DynamoDbGatewayEndpointId",
            value=self.dynamodb_gateway_endpoint.vpc_endpoint_id,
        )

    # -- consumed by the compute stacks and the Reset stack ---------------------------------

    def security_group_for(self, workload_key: str) -> ec2.SecurityGroup:
        """The VPC-attached SG for the seven workloads."""

        return self.workload_security_groups[workload_key]


__all__ = [
    "AGENT_RUNTIME_WORKLOADS",
    "ISOLATED_SUBNET_GROUP_NAME",
    "WORKLOAD_ACTION_RUNTIME",
    "WORKLOAD_COMPILER",
    "WORKLOAD_INBOUND",
    "WORKLOAD_INVESTIGATOR_RUNTIME",
    "WORKLOAD_MONITOR_RUNTIME",
    "WORKLOAD_RESET",
    "WORKLOAD_SENDER",
    "WORKLOAD_WORKER",
    "ChorusNetworkStack",
]
