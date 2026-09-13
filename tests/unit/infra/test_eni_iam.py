"""The VPC-attachment ENI permissions: usable service-side Allow, code-blocking Deny (review R1).

Deployment contract §§ 8.6, 15-17, 38; review R1. AWS's Lambda service performs ENI lifecycle
management under the execution role and those calls do **not** carry
``lambda:SourceFunctionArn`` -- so the previous ``SourceFunctionArn`` condition on the *Allow*
made it un-matchable and the function never attached. The repaired shape:

* the service-side Allow is split by action and carries **no** ``SourceFunctionArn``:
  ``AllowCreateVpcEni`` (Create, ``*``, no condition -- AWS service limitation),
  ``AllowManageVpcEni`` (Delete/Assign/Unassign, ``*``, no condition), ``AllowDescribeVpcEni``
  (Describe*, ``*``, no condition);
* ``DenyVpcEniFromFunctionCode`` denies all six actions when ``lambda:SourceFunctionArn`` =
  this exact function -- fires only for the function's own code, never for service-side
  management.

``AllowManageVpcEni`` was originally conditioned on ``StringEquals ec2:Subnet`` = the two
isolated subnet ARNs. A Macro C live canary against a real ``AmbientChorusReset`` deploy proved
that assumption wrong: ``DeleteNetworkInterface`` calls Lambda's own service makes while
attaching a VPC-attached function never carry an ``ec2:Subnet`` key in their authorization
context (decoded via ``sts:DecodeAuthorizationMessage`` -- only ``aws:Region``/``Service``/
``Resource``/``Type``/``Account``/``ARN``/``ID`` were present), so the condition could never
match and silently blocked Lambda's own ENI cleanup with an implicit deny. The repair drops the
condition, matching the unconditioned shape AWS's own ``AWSLambdaVPCAccessExecutionRole`` uses
for exactly these three actions; ``DenyVpcEniFromFunctionCode`` remains the actual boundary that
keeps a function's own code from reaching the EC2 ENI API.

Read off the synthesized IAM for the four VPC roles and the two non-VPC roles.
"""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from infra.cdk.app import build_app
from infra.cdk.network_support import (
    ENI_CREATE_ACTIONS,
    ENI_DESCRIBE_ACTIONS,
    ENI_MANAGE_ACTIONS,
)

CREATE = set(ENI_CREATE_ACTIONS)
MANAGE = set(ENI_MANAGE_ACTIONS)
DESCRIBE = set(ENI_DESCRIBE_ACTIONS)
ALL_ENI = CREATE | MANAGE | DESCRIBE

ROLE_HAS_ENI = {
    "WorkerRole": ("AmbientChorusApplication", "chorus-worker-development"),
    "CompilerRole": ("AmbientChorusCompiler", "chorus-compiler-development"),
    "SenderRole": ("AmbientChorusSender", "chorus-sender-development"),
    "ResetRole": ("AmbientChorusReset", "chorus-demo-reset-development"),
}
ROLE_HAS_NO_ENI = {
    "ApiRole": "AmbientChorusApplication",
    "WatcherRole": "AmbientChorusWatcher",
}


@cache
def _assembly() -> Any:
    return build_app(offline=True).synth()


def _policy_statements(stack_name: str, role_logical_prefix: str) -> list[dict[str, Any]]:
    template = _assembly().get_stack_by_name(stack_name).template
    found: list[dict[str, Any]] = []
    for logical_id, resource in template["Resources"].items():
        if resource["Type"] != "AWS::IAM::Policy":
            continue
        if logical_id.startswith(role_logical_prefix):
            found.extend(resource["Properties"]["PolicyDocument"]["Statement"])
    return found


def _actions(statement: dict[str, Any]) -> set[str]:
    if "NotAction" in statement:
        # A complementary deny grants no ENI action; never ignore an Allow/NotAction.
        assert statement["Effect"] == "Deny"
        return set()
    action = statement["Action"]
    return {action} if isinstance(action, str) else set(action)


def _by_sid(stack_name: str, role_prefix: str) -> dict[str, dict[str, Any]]:
    return {
        s["Sid"]: s
        for s in _policy_statements(stack_name, role_prefix)
        if isinstance(s.get("Sid"), str)
    }


def _managed_policy_arns(stack_name: str, role_logical_prefix: str) -> str:
    template = _assembly().get_stack_by_name(stack_name).template
    for logical_id, resource in template["Resources"].items():
        if resource["Type"] == "AWS::IAM::Role" and logical_id.startswith(role_logical_prefix):
            return json.dumps(resource["Properties"].get("ManagedPolicyArns", []))
    raise AssertionError(f"no role {role_logical_prefix} in {stack_name}")


def test_no_allow_statement_carries_source_function_arn() -> None:
    """Review R1: the service-side ENI Allow must not depend on ``lambda:SourceFunctionArn`` --
    Lambda's service-side calls do not carry it, and the Allow would be un-matchable."""

    for role_prefix, (stack_name, _fn) in ROLE_HAS_ENI.items():
        for statement in _policy_statements(stack_name, role_prefix):
            if statement["Effect"] != "Allow":
                continue
            if not any(a.startswith("ec2:") for a in _actions(statement)):
                continue
            rendered = json.dumps(statement.get("Condition", {}))
            assert "SourceFunctionArn" not in rendered, (
                f"{role_prefix} {statement.get('Sid')} conditions the ENI Allow on "
                "SourceFunctionArn"
            )


def test_create_eni_is_unconditioned_and_documented_as_a_service_limitation() -> None:
    for role_prefix, (stack_name, _fn) in ROLE_HAS_ENI.items():
        create = _by_sid(stack_name, role_prefix)["AllowCreateVpcEni"]
        assert _actions(create) == CREATE
        assert create["Resource"] == "*"
        assert "Condition" not in create  # no ineffective ec2:Subnet on Create


def test_manage_eni_is_unconditioned_and_documented_as_a_live_proven_repair() -> None:
    """Macro C live canary: ``ec2:Subnet`` never appears in the authorization context of
    Lambda's own service-side ``DeleteNetworkInterface`` call, so a ``StringEquals ec2:Subnet``
    condition here is unmatchable and silently blocks VPC-attached function creation -- proven
    live via ``sts:DecodeAuthorizationMessage`` against a real deploy, not asserted offline."""

    for role_prefix, (stack_name, _fn) in ROLE_HAS_ENI.items():
        manage = _by_sid(stack_name, role_prefix)["AllowManageVpcEni"]
        assert _actions(manage) == MANAGE
        assert manage["Resource"] == "*"
        assert "Condition" not in manage  # no ineffective ec2:Subnet on Delete/Assign/Unassign


def test_describe_eni_is_unconditioned() -> None:
    for role_prefix, (stack_name, _fn) in ROLE_HAS_ENI.items():
        describe = _by_sid(stack_name, role_prefix)["AllowDescribeVpcEni"]
        assert _actions(describe) == DESCRIBE
        assert describe["Resource"] == "*"
        assert "Condition" not in describe


def test_a_deny_blocks_function_code_from_the_ec2_eni_api() -> None:
    """Review R1-B: the ``SourceFunctionArn`` condition lives on a DENY, keyed to this exact
    function -- so function code that reached for ``ec2:CreateNetworkInterface`` is denied,
    while Lambda's service-side management (no ``SourceFunctionArn``) is untouched."""

    for role_prefix, (stack_name, function_name) in ROLE_HAS_ENI.items():
        deny = _by_sid(stack_name, role_prefix)["DenyVpcEniFromFunctionCode"]
        assert deny["Effect"] == "Deny"
        assert _actions(deny) == ALL_ENI
        assert deny["Resource"] == "*"
        rendered = json.dumps(deny["Condition"]["ArnEquals"]["lambda:SourceFunctionArn"])
        assert function_name in rendered
        # names only this function, never a sibling
        for other in {fn for (_s, fn) in ROLE_HAS_ENI.values()} - {function_name}:
            assert other not in rendered


def test_the_api_and_watcher_roles_receive_no_eni_permission() -> None:
    for role_prefix, stack_name in ROLE_HAS_NO_ENI.items():
        for statement in _policy_statements(stack_name, role_prefix):
            assert not any(a.startswith("ec2:") for a in _actions(statement)), (
                f"{role_prefix} holds an ec2 action"
            )


def test_no_role_gets_the_ec2_wildcard_or_the_managed_vpc_policy() -> None:
    for role_prefix, (stack_name, _fn) in ROLE_HAS_ENI.items():
        assert "AWSLambdaVPCAccessExecutionRole" not in _managed_policy_arns(
            stack_name, role_prefix
        )
        granted = set()
        for statement in _policy_statements(stack_name, role_prefix):
            if statement["Effect"] == "Allow":
                granted |= _actions(statement)
        assert "ec2:*" not in granted
        ec2_actions = {a for a in granted if a.startswith("ec2:")}
        assert ec2_actions == ALL_ENI  # exactly the six, nothing else in the ec2 namespace


def test_no_unsupported_condition_is_asserted_merely_to_look_narrow() -> None:
    """Review R1-C item 9, repaired by the Macro C live canary: none of the three service-side
    Allow statements carries a condition EC2 will not actually evaluate against Lambda's own
    ENI calls -- ``ec2:Subnet`` included, now that it is proven unmatchable there too."""

    for role_prefix, (stack_name, _fn) in ROLE_HAS_ENI.items():
        by_sid = _by_sid(stack_name, role_prefix)
        assert "Condition" not in by_sid["AllowCreateVpcEni"]
        assert "Condition" not in by_sid["AllowDescribeVpcEni"]
        assert "Condition" not in by_sid["AllowManageVpcEni"]


def test_deny_vpc_eni_from_function_code_still_present_after_the_manage_repair() -> None:
    """The ``AllowManageVpcEni`` repair removes a condition on the Allow; it must not have
    touched the Deny that is the actual function-code boundary."""

    for role_prefix, (stack_name, function_name) in ROLE_HAS_ENI.items():
        deny = _by_sid(stack_name, role_prefix)["DenyVpcEniFromFunctionCode"]
        assert deny["Effect"] == "Deny"
        assert _actions(deny) == ALL_ENI
        assert deny["Resource"] == "*"
        assert "SourceFunctionArn" in json.dumps(deny["Condition"])
        assert function_name in json.dumps(deny["Condition"])
