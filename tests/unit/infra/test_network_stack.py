"""The isolated network is a boundary asserted from a template, not from a docstring.

Macro A (deployment contract §§ 6-9, 16, 36;
[ADR-010](../../../docs/adr/ADR-010-agentcore-runtime.md)). ADR-010 freezes VPC mode in two
isolated subnets with no NAT and no internet route as a **security** decision, so the proof it
holds has to read the synthesized ``AmbientChorusNetwork`` template: no ``NatGateway``, no
``InternetGateway``, no ``EIP``, no default route, exactly six interface endpoints and two
gateway endpoints, HTTPS-only endpoint ingress, and no unrestricted workload egress.
"""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from aws_cdk import App, Environment, assertions
from infra.cdk.config import CdkBuildConfig
from infra.cdk.network_support import INTERFACE_ENDPOINTS, NetworkConfig
from infra.cdk.stacks.network import ChorusNetworkStack

SUBNET = "AWS::EC2::Subnet"
ENDPOINT = "AWS::EC2::VPCEndpoint"
SECURITY_GROUP = "AWS::EC2::SecurityGroup"
SG_INGRESS = "AWS::EC2::SecurityGroupIngress"
SG_EGRESS = "AWS::EC2::SecurityGroupEgress"
ROUTE = "AWS::EC2::Route"


@cache
def network_template() -> assertions.Template:
    app = App()
    config = CdkBuildConfig(environment="demo", namespace="DEMO")
    stack = ChorusNetworkStack(
        app,
        "AmbientChorusNetwork",
        config=config,
        network=NetworkConfig(("us-east-1a", "us-east-1b"), offline=True),
        artifact_bucket_arn="arn:aws:s3:::chorus-agent-artifacts-demo",
        env=Environment(region="us-east-1"),
    )
    return assertions.Template.from_stack(stack)


def _resources(resource_type: str) -> dict[str, Any]:
    return dict(network_template().find_resources(resource_type))


def _endpoints() -> list[dict[str, Any]]:
    return [r["Properties"] for r in _resources(ENDPOINT).values()]


# -- the VPC shape (deployment contract §§ 4, 36) ----------------------------------------


def test_the_network_stack_synthesizes_exactly_one_vpc_with_two_isolated_subnets() -> None:
    network_template().resource_count_is("AWS::EC2::VPC", 1)
    network_template().resource_count_is(SUBNET, 2)


def test_there_is_no_nat_gateway_no_internet_gateway_and_no_eip() -> None:
    """The egress boundary is frozen by ADR-010; its absence is asserted, not assumed."""

    network_template().resource_count_is("AWS::EC2::NatGateway", 0)
    network_template().resource_count_is("AWS::EC2::InternetGateway", 0)
    network_template().resource_count_is("AWS::EC2::VPCGatewayAttachment", 0)
    network_template().resource_count_is("AWS::EC2::EIP", 0)


def test_no_route_leaves_the_vpc_to_the_internet() -> None:
    """No ``0.0.0.0/0`` and no ``::/0`` route -- and no IGW/NAT to point one at."""

    for route in _resources(ROUTE).values():
        props = route["Properties"]
        assert props.get("DestinationCidrBlock") not in ("0.0.0.0/0",)
        assert props.get("DestinationIpv6CidrBlock") not in ("::/0",)
        assert "GatewayId" not in props and "NatGatewayId" not in props


def test_both_subnets_are_private_isolated_and_map_no_public_ip() -> None:
    subnets = list(_resources(SUBNET).values())
    assert len(subnets) == 2
    for subnet in subnets:
        assert subnet["Properties"].get("MapPublicIpOnLaunch", False) is False


def test_the_two_subnets_are_in_the_two_configured_availability_zones() -> None:
    zones = {s["Properties"]["AvailabilityZone"] for s in _resources(SUBNET).values()}
    assert zones == {"us-east-1a", "us-east-1b"}


# -- the frozen endpoint inventory (deployment contract §§ 7, 36) ------------------------


def test_exactly_six_interface_endpoints_and_two_gateway_endpoints() -> None:
    endpoints = _endpoints()
    interface = [e for e in endpoints if e.get("VpcEndpointType") == "Interface"]
    gateway = [e for e in endpoints if e.get("VpcEndpointType") == "Gateway"]
    assert len(interface) == 6
    assert len(gateway) == 2


def test_the_interface_endpoint_service_names_are_the_frozen_ones() -> None:
    names = {e["ServiceName"] for e in _endpoints() if e.get("VpcEndpointType") == "Interface"}
    assert names == {
        "com.amazonaws.us-east-1.bedrock-runtime",
        "com.amazonaws.us-east-1.bedrock-agentcore",
        "com.amazonaws.us-east-1.lambda",
        "com.amazonaws.us-east-1.secretsmanager",
        "com.amazonaws.us-east-1.scheduler",
        "com.amazonaws.us-east-1.email",
    }


def _interface_service_names() -> set[str]:
    return {
        e["ServiceName"]
        for e in _endpoints()
        if e.get("VpcEndpointType") == "Interface" and isinstance(e["ServiceName"], str)
    }


def test_the_ses_endpoint_is_email_not_sesv2_or_email_smtp() -> None:
    names = _interface_service_names()
    assert "com.amazonaws.us-east-1.email" in names
    assert not any(name.endswith((".sesv2", ".email-smtp")) for name in names)


def test_no_kms_and_no_cloudwatch_logs_endpoint_is_created() -> None:
    """Deployment contract §§ 12-13: no direct KMS SDK client, and Lambda log delivery is
    service-managed -- neither endpoint is added merely because the service exists."""

    names = " ".join(_interface_service_names())
    assert ".kms" not in names
    assert ".logs" not in names
    assert ".sts" not in names


def test_every_interface_endpoint_enables_private_dns() -> None:
    for endpoint in _endpoints():
        if endpoint.get("VpcEndpointType") == "Interface":
            assert endpoint["PrivateDnsEnabled"] is True


def test_the_gateway_endpoints_attach_to_both_isolated_route_tables() -> None:
    route_table_ids = set(_resources("AWS::EC2::RouteTable"))
    assert len(route_table_ids) == 2
    for endpoint in _endpoints():
        if endpoint.get("VpcEndpointType") != "Gateway":
            continue
        referenced = {r["Ref"] for r in endpoint["RouteTableIds"]}
        assert referenced == route_table_ids


# -- security groups: HTTPS-only, no public ingress, no open egress (deployment contract § 8) --


def test_no_interface_endpoint_security_group_has_public_ingress() -> None:
    for props in _all_ingress_rules():
        assert props.get("CidrIp") not in ("0.0.0.0/0",)
        assert props.get("CidrIpv6") not in ("::/0",)


def test_every_endpoint_ingress_rule_is_tcp_443_from_a_workload_security_group() -> None:
    rules = _all_ingress_rules()
    assert rules  # there is at least one
    for props in rules:
        assert props["IpProtocol"] == "tcp"
        assert props["FromPort"] == 443
        assert props["ToPort"] == 443
        # sourced from another SG, never a CIDR
        assert "SourceSecurityGroupId" in props or "GroupId" in props
        assert "CidrIp" not in props


def test_no_workload_security_group_allows_unrestricted_egress() -> None:
    """No ``0.0.0.0/0`` / ``-1`` egress on any SG the isolated workloads carry."""

    for props in _all_egress_rules():
        is_open_cidr = props.get("CidrIp") == "0.0.0.0/0" or props.get("CidrIpv6") == "::/0"
        is_all_protocols = props.get("IpProtocol") in ("-1", -1)
        assert not (is_open_cidr and is_all_protocols)
        if is_open_cidr:
            # a CIDR egress, if any, is at most HTTPS -- never all traffic
            assert props.get("FromPort") == 443 and props.get("ToPort") == 443


def test_workload_egress_to_the_gateway_endpoints_uses_the_managed_prefix_lists() -> None:
    """Deployment contract § 9: gateway-endpoint egress names the AWS-managed prefix list, not
    an open ``0.0.0.0/0``."""

    prefix_list_rules = [
        props for props in _all_egress_rules() if "DestinationPrefixListId" in props
    ]
    assert prefix_list_rules
    for props in prefix_list_rules:
        assert props["FromPort"] == 443 and props["ToPort"] == 443
        assert props["DestinationPrefixListId"].startswith("pl-")


def test_the_bedrock_runtime_endpoint_is_reachable_by_no_current_workload() -> None:
    """It is the Macro B AgentCore runtimes' endpoint; no Macro A Lambda SG may reach it
    (deployment contract § 8)."""

    spec = next(s for s in INTERFACE_ENDPOINTS if s.logical_id == "BedrockRuntimeEndpoint")
    assert spec.reachable_by == ()


def _all_ingress_rules() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = [r["Properties"] for r in _resources(SG_INGRESS).values()]
    for sg in _resources(SECURITY_GROUP).values():
        rules.extend(sg["Properties"].get("SecurityGroupIngress", []) or [])
    return rules


def _all_egress_rules() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = [r["Properties"] for r in _resources(SG_EGRESS).values()]
    for sg in _resources(SECURITY_GROUP).values():
        rules.extend(sg["Properties"].get("SecurityGroupEgress", []) or [])
    return rules


# -- the endpoint policies (deployment contract §§ 10-11) --------------------------------


def _s3_endpoint_statements() -> list[dict[str, Any]]:
    s3_endpoint = next(
        e
        for e in _endpoints()
        if e.get("VpcEndpointType") == "Gateway" and "s3" in json.dumps(e["ServiceName"]).lower()
    )
    return list(s3_endpoint["PolicyDocument"]["Statement"])


def _s3_endpoint_statement(sid: str) -> dict[str, Any]:
    for s in _s3_endpoint_statements():
        if s.get("Sid") == sid:
            return s
    raise AssertionError(f"no S3 endpoint statement {sid}")


def test_the_s3_endpoint_policy_has_no_global_bucket_read() -> None:
    """Review R4: the old unconditioned ``s3:GetObject`` on ``arn:aws:s3:::*/*`` for every
    principal is gone."""

    for s in _s3_endpoint_statements():
        resources = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
        for resource in resources:
            assert resource not in ("*", "arn:aws:s3:::*", "arn:aws:s3:::*/*")


def test_the_agentcore_s3_exception_is_the_documented_service_owned_pattern_only() -> None:
    """Review R4: only the regional ``acr-code-*`` service bucket, GetObject only, and only
    for the AgentCore service principal."""

    stmt = _s3_endpoint_statement("AllowAgentCoreServiceOwnedArtifactRead")
    assert stmt["Action"] == "s3:GetObject"
    assert stmt["Resource"] == "arn:aws:s3:::acr-code-*-us-east-1-an/*"
    assert (
        stmt["Condition"]["StringEquals"]["aws:PrincipalServiceName"]
        == "bedrock-agentcore.amazonaws.com"
    )


def test_the_evidence_object_access_covers_reset_delete_and_list() -> None:
    """Review R5-A: the endpoint policy must not block the operations reset needs -- List,
    DeleteObject, DeleteObjectVersion -- or the reset role's own IAM is unusable here."""

    obj = _s3_endpoint_statement("EvidenceObjectAccess")
    assert {"s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutObject", "s3:GetObject"} == set(
        obj["Action"]
    )
    assert all("chorus-agent-artifacts" not in r for r in obj["Resource"])

    lst = _s3_endpoint_statement("ListDemoEvidencePrefixes")
    assert lst["Action"] == "s3:ListBucket"
    assert lst["Condition"]["StringLike"]["s3:prefix"] == ["ns/DEMO/*"]
    assert all("/*" not in r for r in lst["Resource"])  # bucket-level, not object-level


def test_existing_chorus_bucket_policy_entries_remain() -> None:
    sids = {s.get("Sid") for s in _s3_endpoint_statements()}
    assert "ReadAgentArtifactPrefixes" in sids
    assert "EvidenceObjectAccess" in sids


def test_the_dynamodb_endpoint_policy_names_only_the_three_chorus_tables() -> None:
    ddb_endpoint = next(
        e
        for e in _endpoints()
        if e.get("VpcEndpointType") == "Gateway"
        and "dynamodb" in json.dumps(e["ServiceName"]).lower()
    )
    statement = ddb_endpoint["PolicyDocument"]["Statement"][0]
    rendered = json.dumps(statement["Resource"])
    assert "chorus-core-demo" in rendered
    assert "chorus-shareable-demo" in rendered
    assert "chorus-audit-demo" in rendered
    assert "dynamodb:Scan" not in json.dumps(statement["Action"])
