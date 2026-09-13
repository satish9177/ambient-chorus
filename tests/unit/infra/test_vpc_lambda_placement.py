"""Which of the five production Lambdas are inside the isolated VPC, proved from the resources.

Deployment contract § 2, § 37. The frozen network contract does **not** place every Lambda in
the VPC: the worker, compiler, and sender go inside (they invoke the VPC-only runtimes, or hold
the private-bucket / secret / SES reach); the API and the watcher stay **out** (a request-path
front end and a DynamoDB-only function, each better off with a faster cold start). This is
asserted against the synthesized ``VpcConfig`` of each actual ``AWS::Lambda::Function`` -- not a
construct property.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from infra.cdk.app import build_app

VPC_ATTACHED = ("worker", "compiler", "sender")
NOT_VPC_ATTACHED = ("api", "commitment-watcher")

FUNCTION_STACK = {
    "api": "AmbientChorusApplication",
    "worker": "AmbientChorusApplication",
    "compiler": "AmbientChorusCompiler",
    "sender": "AmbientChorusSender",
    "commitment-watcher": "AmbientChorusWatcher",
    "demo-reset": "AmbientChorusReset",
}


@cache
def _assembly() -> Any:
    return build_app(offline=True).synth()


def _function(suffix: str) -> dict[str, Any]:
    template = _assembly().get_stack_by_name(FUNCTION_STACK[suffix]).template
    name = f"chorus-{suffix}-development"
    for resource in template["Resources"].values():
        if (
            resource["Type"] == "AWS::Lambda::Function"
            and resource["Properties"].get("FunctionName") == name
        ):
            props: dict[str, Any] = resource["Properties"]
            return props
    raise AssertionError(f"no function {name} in {FUNCTION_STACK[suffix]}")


def test_the_api_function_has_no_vpc_config() -> None:
    """The API is a request-path front end behind API Gateway; the VPC buys it no boundary and
    adds ENI cold-start latency to the one component a presenter waits on."""

    assert "VpcConfig" not in _function("api")


def test_the_watcher_function_has_no_vpc_config() -> None:
    """DynamoDB only, no private data, no secret -- and a faster cold start on the demo path."""

    assert "VpcConfig" not in _function("commitment-watcher")


def test_the_worker_compiler_and_sender_are_vpc_attached_to_the_two_isolated_subnets() -> None:
    for suffix in VPC_ATTACHED:
        props = _function(suffix)
        assert "VpcConfig" in props, f"{suffix} has no VpcConfig"
        vpc_config = props["VpcConfig"]
        subnet_refs = vpc_config["SubnetIds"]
        assert len(subnet_refs) == 2, f"{suffix} is not in exactly two subnets"
        # the subnets come from the dedicated Network stack, by cross-stack import
        assert all("AmbientChorusNetwork" in str(ref) for ref in subnet_refs)
        assert len(vpc_config["SecurityGroupIds"]) == 1
        assert "AmbientChorusNetwork" in str(vpc_config["SecurityGroupIds"][0])


def test_the_three_vpc_functions_use_three_distinct_security_groups() -> None:
    groups = {str(_function(suffix)["VpcConfig"]["SecurityGroupIds"][0]) for suffix in VPC_ATTACHED}
    assert len(groups) == 3


def test_the_reset_function_is_also_vpc_attached() -> None:
    """Deployment contract § 24: reset only needs DynamoDB / S3 (gateway) and Scheduler, so it
    reuses the isolated network with its own SG and its own ENI IAM."""

    props = _function("demo-reset")
    assert "VpcConfig" in props
    assert len(props["VpcConfig"]["SubnetIds"]) == 2
    assert "AmbientChorusNetwork" in str(props["VpcConfig"]["SecurityGroupIds"][0])
