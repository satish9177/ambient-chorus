"""The frozen Phase 11 network shape, in one auditable place.

Macro A's network foundation (deployment contract §§ 6-9, 16). Everything here is a *value* --
the VPC CIDR, the two-AZ deployment-config contract, the frozen interface/gateway endpoint
inventory and which workload may reach each one, the AWS-managed prefix lists the isolated
subnets route S3/DynamoDB through, and the exact EC2 ENI action families a VPC-attached Lambda
role needs. :mod:`infra.cdk.stacks.network` turns them into resources; the compute stacks and
:mod:`infra.cdk.stacks.reset` consume the security groups and subnets it exports.

Why the AZ names are deployment configuration, not a default
-----------------------------------------------------------
AgentCore supports a fixed set of availability zones identified by **AZ ID** (``use1-az1``),
and an AZ ID maps to a different AZ *name* in every account (deployment contract § 6). So the
two isolated subnets are placed by resolving the supported AZ IDs to *this account's* names
once, out of band, and passing the result in as ``-c network_availability_zones=<a>,<b>``.
:class:`NetworkConfig` fails closed in deployment mode when that configuration is absent or
malformed, exactly as :class:`~infra.cdk.config.DeploymentIdentities` does for a missing ARN;
an explicit offline synth fills a clearly-named synthetic fixture that deployment mode rejects.
``us-east-1a``/``us-east-1b`` are **never** a silent account-independent assumption -- a real
deploy must name the resolved values.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam

from infra.cdk.config import PHASE_11_REGION, MissingDeploymentIdentityError

VPC_CIDR: Final = "10.80.0.0/16"
"""One /16 for the isolated VPC. RFC 1918 space; it routes nowhere off the account."""

ISOLATED_SUBNET_CIDR_MASK: Final = 24
"""A /24 per isolated subnet -- 256 addresses per AZ is ample for the ENIs of a handful of
VPC-attached Lambdas and, later, three AgentCore runtimes (deployment contract § 35)."""

HTTPS_PORT: Final = 443
"""The only port any workload security group opens, to any destination."""

# The AWS-managed prefix lists for the frozen region's S3 and DynamoDB **gateway** endpoints.
# These IDs are public, documented, and identical in every account in ``us-east-1`` -- they are
# not account configuration and not a secret. ``aws-cdk-lib`` exposes no lookup-free accessor
# for a ``GatewayVpcEndpoint``'s managed prefix list, and Phase 11 is frozen to one region
# (:data:`infra.cdk.config.PHASE_11_REGION`), so the VPC-attached workload security groups name
# them directly for their gateway-endpoint egress rather than opening 443 to the internet
# (deployment contract § 9). Source: ``aws ec2 describe-managed-prefix-lists --region us-east-1``.
MANAGED_PREFIX_LIST_S3: Final = "pl-63a5400a"
MANAGED_PREFIX_LIST_DYNAMODB: Final = "pl-02cd2c6b"

_AZ_NAME_RE: Final = re.compile(r"^us-east-1[a-f]$")

_OFFLINE_SYNTH_AVAILABILITY_ZONES: Final[tuple[str, str]] = ("us-east-1a", "us-east-1b")
"""Synthetic -- **explicit offline synth only** (deployment contract § 5). A real deploy resolves
the two AgentCore-supported AZ IDs to this account's names and passes
``-c network_availability_zones=<a>,<b>``; deployment mode raises :class:`NetworkConfigError`
without them rather than falling back to this pair."""


class NetworkConfigError(MissingDeploymentIdentityError):
    """The isolated-subnet AZ configuration is absent, malformed, or a synthetic placeholder.

    A subclass of :class:`~infra.cdk.config.MissingDeploymentIdentityError` so the default
    deployment-capable ``build_app`` refuses before any stack is constructed -- the same
    fail-closed contract Batch 5 established for deployment identities (deployment contract
    §§ 5, 32).
    """


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    """The two isolated-subnet AZ names, resolved out of band and pinned in deploy config.

    ``offline`` selects the mode. Deployment mode (:meth:`from_context` with ``offline=False``)
    requires two well-formed ``us-east-1`` AZ names from context; offline mode fills
    :data:`_OFFLINE_SYNTH_AVAILABILITY_ZONES` when none are supplied.
    """

    availability_zones: tuple[str, str]
    offline: bool = field(default=True, kw_only=True)

    def __post_init__(self) -> None:
        azs = self.availability_zones
        if len(azs) != 2 or azs[0] == azs[1]:
            raise NetworkConfigError(
                f"the isolated subnets need two distinct AZ names, got {list(azs)!r}"
            )
        if not self.offline:
            for name in azs:
                if not _AZ_NAME_RE.match(name):
                    raise NetworkConfigError(
                        f"network_availability_zones entry {name!r} is not a {PHASE_11_REGION} "
                        "AZ name -- deployment contract § 6: resolve the AgentCore-supported AZ "
                        "IDs to this account's names and pass "
                        "-c network_availability_zones=<a>,<b>"
                    )

    @classmethod
    def from_context(
        cls, lookup: Callable[[str], object] | object, *, offline: bool
    ) -> NetworkConfig:
        """Build from CDK context.

        Accepts either ``network_availability_zones=<a>,<b>`` or the pair
        ``network_availability_zone_a`` / ``network_availability_zone_b``. In deployment mode a
        missing or single-valued setting raises :class:`NetworkConfigError`; in offline mode it
        falls back to the synthetic fixture.
        """

        get = lookup if callable(lookup) else (lambda _key: None)

        def _str(key: str) -> str | None:
            value = get(key)
            return value if isinstance(value, str) and value.strip() else None

        combined = _str("network_availability_zones")
        if combined is not None:
            names = tuple(part.strip() for part in combined.split(",") if part.strip())
        else:
            names = tuple(
                value
                for value in (
                    _str("network_availability_zone_a"),
                    _str("network_availability_zone_b"),
                )
                if value is not None
            )

        if not names:
            if offline:
                return cls(_OFFLINE_SYNTH_AVAILABILITY_ZONES, offline=True)
            raise NetworkConfigError(
                "network_availability_zones is required in deployment mode -- pass "
                "-c network_availability_zones=<a>,<b> with the two AZ names the "
                "AgentCore-supported AZ IDs resolve to in this account, or select offline mode "
                "explicitly (-c offline_synth=true)"
            )
        if len(names) != 2:
            raise NetworkConfigError(
                f"network_availability_zones must name exactly two AZs, got {list(names)!r}"
            )
        return cls((names[0], names[1]), offline=offline)

    @property
    def is_synthetic(self) -> bool:
        """Whether these AZ names are the offline-only synthetic fixture."""

        return self.availability_zones == _OFFLINE_SYNTH_AVAILABILITY_ZONES


# ---------------------------------------------------------------------------------------------
# Frozen endpoint inventory (deployment contract §§ 6-8, 36)
# ---------------------------------------------------------------------------------------------

# The three VPC-attached Macro A workloads plus the reset principal (deployment contract §§ 14,
# 24). ``bedrock-runtime`` is deliberately reachable by *none* of them: its interface endpoint
# exists for the Macro B AgentCore runtimes, and granting a current Lambda SG reachability to it
# merely because the endpoint exists is exactly what deployment contract § 8 forbids.
WORKLOAD_WORKER: Final = "worker"
WORKLOAD_COMPILER: Final = "compiler"
WORKLOAD_SENDER: Final = "sender"
WORKLOAD_RESET: Final = "reset"
WORKLOAD_INBOUND: Final = "inbound"

WORKLOAD_MONITOR_RUNTIME: Final = "monitor_runtime"
WORKLOAD_INVESTIGATOR_RUNTIME: Final = "investigator_runtime"
WORKLOAD_ACTION_RUNTIME: Final = "action_runtime"
AGENT_RUNTIME_WORKLOADS: Final = (
    WORKLOAD_MONITOR_RUNTIME,
    WORKLOAD_INVESTIGATOR_RUNTIME,
    WORKLOAD_ACTION_RUNTIME,
)

VPC_WORKLOADS: Final = (
    WORKLOAD_WORKER,
    WORKLOAD_COMPILER,
    WORKLOAD_SENDER,
    WORKLOAD_RESET,
    WORKLOAD_INBOUND,
    *AGENT_RUNTIME_WORKLOADS,
)


@dataclass(frozen=True, slots=True)
class InterfaceEndpointSpec:
    """One frozen interface endpoint and the workloads whose real SDK clients reach it."""

    logical_id: str
    service: ec2.IInterfaceVpcEndpointService
    reachable_by: tuple[str, ...]
    """Workload keys whose security group gets TCP 443 egress to this endpoint's SG, and whose
    SG the endpoint's SG admits on 443 ingress. Empty means *no current workload* -- the
    endpoint is created but unreachable until a later macro adds a rule."""


# ``InterfaceVpcEndpointAwsService('scheduler')`` -- there is no typed CDK constant for
# EventBridge Scheduler in aws-cdk-lib 2.267, and the frozen service identity is
# ``com.amazonaws.<region>.scheduler`` (verified from the synthesized ``ServiceName``).
SCHEDULER_ENDPOINT_SERVICE: Final = ec2.InterfaceVpcEndpointAwsService("scheduler")

# Why the runtime security groups reach Bedrock Runtime and S3 and nothing else:
# The three AgentCore direct-code runtimes (Monitor, Investigator, Action) make foundation model
# invocations via the Bedrock Runtime interface endpoint, and fetch their direct-code deployment
# zip during cold start via the S3 gateway endpoint (deployment contract §§ 6-7).
# They hold no client or reachability for DynamoDB, Lambda, Secrets Manager, Scheduler, SES, or
# bedrock-agentcore (no agent invokes another agent -- an IAM fact and a network boundary).

INTERFACE_ENDPOINTS: Final[tuple[InterfaceEndpointSpec, ...]] = (
    InterfaceEndpointSpec(
        "BedrockRuntimeEndpoint",
        ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
        reachable_by=AGENT_RUNTIME_WORKLOADS,
    ),
    InterfaceEndpointSpec(
        "BedrockAgentCoreEndpoint",
        ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE,
        reachable_by=(WORKLOAD_WORKER,),
    ),
    InterfaceEndpointSpec(
        "LambdaEndpoint",
        ec2.InterfaceVpcEndpointAwsService.LAMBDA_,
        reachable_by=(WORKLOAD_WORKER, WORKLOAD_SENDER),
    ),
    InterfaceEndpointSpec(
        "SecretsManagerEndpoint",
        ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
        reachable_by=(WORKLOAD_SENDER,),
    ),
    InterfaceEndpointSpec(
        "SchedulerEndpoint",
        SCHEDULER_ENDPOINT_SERVICE,
        reachable_by=(WORKLOAD_WORKER, WORKLOAD_RESET),
    ),
    InterfaceEndpointSpec(
        "EmailEndpoint",
        ec2.InterfaceVpcEndpointAwsService.EMAIL,
        reachable_by=(WORKLOAD_SENDER,),
    ),
)
"""Exactly six interface endpoints (deployment contract § 7). The SESv2 ``SendEmail`` API
endpoint is ``email`` -- not ``sesv2`` (the boto3 client name) and not ``email-smtp`` (a
different protocol this system does not use)."""

# Which workloads route S3 / DynamoDB through the two free gateway endpoints. The sender holds
# a total evidence-object deny (ADR-024), so it needs no S3 route. The three runtime workloads
# fetch their direct-code artifact from S3 during cold start (deployment contract § 6).
GATEWAY_S3_REACHABLE_BY: Final = (
    WORKLOAD_WORKER,
    WORKLOAD_COMPILER,
    WORKLOAD_RESET,
    WORKLOAD_INBOUND,
    *AGENT_RUNTIME_WORKLOADS,
)
GATEWAY_DYNAMODB_REACHABLE_BY: Final = (
    WORKLOAD_WORKER,
    WORKLOAD_COMPILER,
    WORKLOAD_SENDER,
    WORKLOAD_RESET,
    WORKLOAD_INBOUND,
)


# ---------------------------------------------------------------------------------------------
# VPC-attachment ENI IAM (deployment contract §§ 8.6, 16-17; review R1)
# ---------------------------------------------------------------------------------------------
#
# **How Lambda VPC attachment authorizes, and why the old shape did not work (review R1).**
# When a function is VPC-attached the *Lambda service* -- not the function's code -- creates and
# manages the Hyperplane ENIs, using the execution role. Those service-side EC2 calls do **not**
# carry ``lambda:SourceFunctionArn``: AWS injects that key for requests that originate *inside*
# the execution environment, not for the service's own ENI lifecycle management. So a
# ``SourceFunctionArn`` condition on the *Allow* makes the Allow un-matchable for the very calls
# it exists to permit, and the function never attaches.
#
# The AWS-recommended shape is the inverse: leave the service-side Allow unconditioned by
# ``SourceFunctionArn``, and add an explicit conditional **Deny** keyed on
# ``lambda:SourceFunctionArn`` = this exact function -- which fires only for ENI calls the
# function's own code makes (those carry the key) and never for service-side management (which
# does not). ``ec2:Subnet`` is applied only where the AWS Service Authorization Reference
# actually supports it in this shape: on ``DeleteNetworkInterface`` /
# ``AssignPrivateIpAddresses`` / ``UnassignPrivateIpAddresses`` (they act on an ENI that has a
# subnet), and **not** on ``CreateNetworkInterface`` -- Lambda's Hyperplane creation path is a
# service operation and a subnet condition there is an ineffective narrowing, so Create stays on
# ``Resource: "*"`` and that is documented as an AWS service limitation rather than dressed up.

ENI_CREATE_ACTIONS: Final = ("ec2:CreateNetworkInterface",)
"""Create is a service operation on a resource that does not exist yet; it stays on
``Resource: "*"`` with no ``ec2:Subnet`` condition -- an AWS service limitation (review R1-A),
not a narrowing to skip. The code-blocking Deny below still covers it."""

ENI_MANAGE_ACTIONS: Final = (
    "ec2:DeleteNetworkInterface",
    "ec2:AssignPrivateIpAddresses",
    "ec2:UnassignPrivateIpAddresses",
)
"""These act on an existing ENI, which has a subnet, so ``ec2:Subnet`` is a supported and
effective narrowing to the two isolated subnets (review R1-A)."""

ENI_DESCRIBE_ACTIONS: Final = (
    "ec2:DescribeNetworkInterfaces",
    "ec2:DescribeSubnets",
)
"""Read-only network metadata. AWS provides no resource-level ARN and no useful condition key,
so they stay on ``Resource: "*"`` unconditioned -- stated, not dressed up (contract § 8.6)."""

ENI_MUTATING_ACTIONS: Final = (*ENI_CREATE_ACTIONS, *ENI_MANAGE_ACTIONS)
ENI_ALL_ACTIONS: Final = (*ENI_MUTATING_ACTIONS, *ENI_DESCRIBE_ACTIONS)


def vpc_eni_policy_statements(
    *, function_arn: str, subnet_arns: list[str]
) -> list[iam.PolicyStatement]:
    """The exact inline ENI permissions for one VPC-attached Lambda execution role (review R1).

    Four statements and no managed policy (deployment contract §§ 15-17):

    * ``AllowCreateVpcEni`` -- ``ec2:CreateNetworkInterface`` on ``Resource: "*"``, **no
      condition**. Lambda's Hyperplane creation is a service operation; a subnet condition here
      is ineffective, and this is an AWS service limitation, not a narrowing skipped;
    * ``AllowManageVpcEni`` -- ``DeleteNetworkInterface`` / ``AssignPrivateIpAddresses`` /
      ``UnassignPrivateIpAddresses`` on ``Resource: "*"``, ``Condition StringEquals ec2:Subnet``
      = the two isolated subnet ARNs (supported for actions that act on an existing ENI);
    * ``AllowDescribeVpcEni`` -- ``DescribeNetworkInterfaces`` / ``DescribeSubnets`` on
      ``Resource: "*"``, unconditioned;
    * ``DenyVpcEniFromFunctionCode`` -- all six actions, ``Effect: DENY``, ``Condition
      ArnEquals lambda:SourceFunctionArn`` = this exact function ARN. It fires only for ENI
      calls the function's **own code** makes (those carry the key) and never for Lambda's
      service-side ENI lifecycle management (which does not), so it blocks function-code EC2
      access without preventing VPC attachment.

    **No ``lambda:SourceFunctionArn`` appears on any Allow.** ``AWSLambdaVPCAccessExecutionRole``
    is never attached; ``ec2:*`` and unrelated EC2 actions are never granted.
    """

    return [
        iam.PolicyStatement(
            sid="AllowCreateVpcEni",
            effect=iam.Effect.ALLOW,
            actions=list(ENI_CREATE_ACTIONS),
            resources=["*"],
        ),
        iam.PolicyStatement(
            sid="AllowManageVpcEni",
            effect=iam.Effect.ALLOW,
            actions=list(ENI_MANAGE_ACTIONS),
            resources=["*"],
            conditions={"StringEquals": {"ec2:Subnet": subnet_arns}},
        ),
        iam.PolicyStatement(
            sid="AllowDescribeVpcEni",
            effect=iam.Effect.ALLOW,
            actions=list(ENI_DESCRIBE_ACTIONS),
            resources=["*"],
        ),
        iam.PolicyStatement(
            sid="DenyVpcEniFromFunctionCode",
            effect=iam.Effect.DENY,
            actions=list(ENI_ALL_ACTIONS),
            resources=["*"],
            conditions={"ArnEquals": {"lambda:SourceFunctionArn": function_arn}},
        ),
    ]


__all__ = [
    "AGENT_RUNTIME_WORKLOADS",
    "ENI_ALL_ACTIONS",
    "ENI_CREATE_ACTIONS",
    "ENI_DESCRIBE_ACTIONS",
    "ENI_MANAGE_ACTIONS",
    "ENI_MUTATING_ACTIONS",
    "GATEWAY_DYNAMODB_REACHABLE_BY",
    "GATEWAY_S3_REACHABLE_BY",
    "HTTPS_PORT",
    "INTERFACE_ENDPOINTS",
    "ISOLATED_SUBNET_CIDR_MASK",
    "MANAGED_PREFIX_LIST_DYNAMODB",
    "MANAGED_PREFIX_LIST_S3",
    "SCHEDULER_ENDPOINT_SERVICE",
    "VPC_CIDR",
    "VPC_WORKLOADS",
    "WORKLOAD_ACTION_RUNTIME",
    "WORKLOAD_COMPILER",
    "WORKLOAD_INBOUND",
    "WORKLOAD_INVESTIGATOR_RUNTIME",
    "WORKLOAD_MONITOR_RUNTIME",
    "WORKLOAD_RESET",
    "WORKLOAD_SENDER",
    "WORKLOAD_WORKER",
    "InterfaceEndpointSpec",
    "NetworkConfig",
    "NetworkConfigError",
    "vpc_eni_policy_statements",
]
