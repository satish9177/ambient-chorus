"""Unit tests for Phase 11 Macro B Chunk 2: Runtime Bedrock IAM and Worker AgentCore IAM.

This test suite proves the frozen identity and boundary properties of the Ambient Chorus
multi-agent system across all synthesized stacks, validating:
1. Pairwise isolation: No agent's execution role references any other agent's inference profile.
2. Inference profile grants: Each runtime role invokes only its dedicated application profile.
3. Foundation model grants: Invocation is conditioned on that agent's own profile ARN.
4. Minimal action sets: Exactly `bedrock:InvokeModelWithResponseStream` (no unused `InvokeModel`).
5. No wildcard bedrock permissions (`bedrock:*`).
6. No wildcard resource on any Bedrock or AgentCore Allow statement.
7. Worker boundary: the worker invokes exactly the three live endpoints of the Agents stack.
8. Role isolation: No other Lambda role holds Bedrock or AgentCore invocation permissions.
9. Identity alignment: Worker environment variables and IAM resource targets are identical objects.
10. L2 construct regression guard: No implicit grants on runtime execution roles.
11. S3 artifact scoping: Each runtime role reads only its own artifact prefix.
"""

from __future__ import annotations

import itertools
import json
from functools import cache
from typing import Any

import pytest
from aws_cdk import App, Stack, assertions
from infra.cdk.app import build_app
from infra.cdk.stacks.agents import (
    INFERENCE_PROFILE_ARN_CONDITION_KEY,
    NOVA_2_LITE_BASE_MODEL_ID,
    RUNTIME_MODEL_ACTIONS,
    US_INFERENCE_PROFILE_DESTINATION_REGIONS,
)

AGENTS: tuple[str, ...] = ("monitor", "investigator", "action")
POLICY_TYPE: str = "AWS::IAM::Policy"
ROLE_TYPE: str = "AWS::IAM::Role"
FUNCTION_TYPE: str = "AWS::Lambda::Function"

FROZEN_FM_ARNS: frozenset[str] = frozenset(
    f"arn:aws:bedrock:{region}::foundation-model/{NOVA_2_LITE_BASE_MODEL_ID}"
    for region in US_INFERENCE_PROFILE_DESTINATION_REGIONS
)


@cache
def full_app() -> App:
    """Synthesize the full offline application in demo environment."""
    return build_app(offline=True, context={"environment": "demo", "namespace": "DEMO"})


@cache
def stack_by_name(name: str) -> Stack:
    """Retrieve a stack construct by its stack name."""
    app = full_app()
    for child in app.node.children:
        if isinstance(child, Stack) and child.stack_name == name:
            return child
    raise KeyError(f"Stack {name} not found in application")


@cache
def template_by_name(name: str) -> assertions.Template:
    """Retrieve an assertions.Template for a named stack."""
    return assertions.Template.from_stack(stack_by_name(name))


@cache
def all_templates() -> dict[str, assertions.Template]:
    """Retrieve all stack templates mapped by stack name."""
    app = full_app()
    templates: dict[str, assertions.Template] = {}
    for child in app.node.children:
        if isinstance(child, Stack):
            templates[child.stack_name] = assertions.Template.from_stack(child)
    return templates


def role_logical_id(agent: str) -> str:
    """Return the logical ID of the agent's runtime execution role.

    Resolved by looking up the role whose **physical** ``RoleName`` is
    ``chorus-{agent}-runtime-demo``, never by rebuilding the logical id from the agent name.
    CDK appends a stable hash to a construct's logical id (``MonitorRuntimeRoleF64F8696``), so a
    hand-built ``MonitorRuntimeRole`` matches no ``Ref`` in the template -- and a statement
    lookup keyed on it silently returns **zero** statements, which would make every assertion
    below pass vacuously. Failing loudly on a missing role is the point.
    """

    expected = f"chorus-{agent}-runtime-demo"
    roles = template_by_name("AmbientChorusAgents").find_resources(ROLE_TYPE)
    for logical_id, resource in roles.items():
        if resource["Properties"].get("RoleName") == expected:
            return logical_id
    raise KeyError(f"no runtime execution role named {expected!r} in the Agents stack")


def profile_logical_id(agent: str) -> str:
    """Return the logical ID of the agent's application inference profile."""
    return f"{agent.capitalize()}InferenceProfile"


def statements_for_role(template: assertions.Template, role_log_id: str) -> list[dict[str, Any]]:
    """Collect all IAM policy statements attached to a given role in a template."""
    statements: list[dict[str, Any]] = []

    # Managed or attached policies referencing the role
    for policy in template.find_resources(POLICY_TYPE).values():
        roles = policy["Properties"].get("Roles", [])
        if any(
            (isinstance(r, dict) and r.get("Ref") == role_log_id) or r == role_log_id for r in roles
        ):
            statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])

    # Inline policies defined directly on the role construct
    roles = template.find_resources(ROLE_TYPE)
    if role_log_id in roles:
        inline_policies = roles[role_log_id]["Properties"].get("Policies", [])
        for inline in inline_policies:
            statements.extend(inline["PolicyDocument"]["Statement"])

    return statements


# --------------------------------------------------------------------------------------
# 1. Pairwise Isolation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent_a", "agent_b"),
    list(itertools.permutations(AGENTS, 2)),
)
def test_pairwise_isolation_between_agent_runtime_roles(agent_a: str, agent_b: str) -> None:
    """Prove agent A's role never references agent B's inference profile logical ID.

    For each of the 6 ordered pairs of distinct agents, no statement in agent A's role
    contains a Resource or bedrock:InferenceProfileArn condition pointing to agent B's
    CfnApplicationInferenceProfile logical ID.
    """
    tmpl = template_by_name("AmbientChorusAgents")
    stmts = statements_for_role(tmpl, role_logical_id(agent_a))
    b_profile_id = profile_logical_id(agent_b)

    for stmt in stmts:
        res_str = json.dumps(stmt.get("Resource", ""))
        assert b_profile_id not in res_str, (
            f"Agent {agent_a}'s role contains a statement referencing {b_profile_id} "
            f"in Resource: {stmt}"
        )

        condition = stmt.get("Condition", {})
        cond_str = json.dumps(condition)
        assert b_profile_id not in cond_str, (
            f"Agent {agent_a}'s role contains a statement referencing {b_profile_id} "
            f"in Condition: {stmt}"
        )


# --------------------------------------------------------------------------------------
# 2. Dedicated Application Inference Profile Invocation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_each_runtime_role_has_one_own_inference_profile_allow(agent: str) -> None:
    """Each runtime role has exactly one Invoke*InferenceProfileOnly statement.

    Its Resource is a single Fn::GetAtt pointing to that agent's own profile's
    InferenceProfileArn attribute.
    """
    tmpl = template_by_name("AmbientChorusAgents")
    stmts = statements_for_role(tmpl, role_logical_id(agent))
    expected_sid = f"Invoke{agent.capitalize()}InferenceProfileOnly"

    matching = [s for s in stmts if s.get("Sid") == expected_sid]
    assert len(matching) == 1, f"Expected exactly 1 statement with Sid {expected_sid}"

    stmt = matching[0]
    assert stmt["Effect"] == "Allow"
    expected_resource = {"Fn::GetAtt": [profile_logical_id(agent), "InferenceProfileArn"]}
    assert stmt["Resource"] == expected_resource


# --------------------------------------------------------------------------------------
# 3. Dedicated Foundation Model Grant Condition-Bound to Profile
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_each_runtime_role_has_one_foundation_models_grant_bound_to_profile(
    agent: str,
) -> None:
    """Each runtime role has exactly one Invoke*FoundationModelsViaProfileOnly statement.

    Its Resource is exactly the 3 frozen Nova 2 Lite FM ARNs, and its condition is
    StringEquals bedrock:InferenceProfileArn = own profile's Fn::GetAtt.
    """
    tmpl = template_by_name("AmbientChorusAgents")
    stmts = statements_for_role(tmpl, role_logical_id(agent))
    expected_sid = f"Invoke{agent.capitalize()}FoundationModelsViaProfileOnly"

    matching = [s for s in stmts if s.get("Sid") == expected_sid]
    assert len(matching) == 1, f"Expected exactly 1 statement with Sid {expected_sid}"

    stmt = matching[0]
    assert stmt["Effect"] == "Allow"
    resources = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
    assert set(resources) == FROZEN_FM_ARNS

    expected_ref = {"Fn::GetAtt": [profile_logical_id(agent), "InferenceProfileArn"]}
    string_equals = stmt.get("Condition", {}).get("StringEquals", {})
    assert string_equals.get(INFERENCE_PROFILE_ARN_CONDITION_KEY) == expected_ref


# --------------------------------------------------------------------------------------
# 4. Action Narrowing to ConverseStream (InvokeModelWithResponseStream)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_runtime_model_actions_contain_only_invoke_model_with_response_stream(
    agent: str,
) -> None:
    """The action set on both model invocation statements is exactly streaming invocation."""
    tmpl = template_by_name("AmbientChorusAgents")
    stmts = statements_for_role(tmpl, role_logical_id(agent))

    profile_sid = f"Invoke{agent.capitalize()}InferenceProfileOnly"
    fm_sid = f"Invoke{agent.capitalize()}FoundationModelsViaProfileOnly"

    profile_stmt = next(s for s in stmts if s.get("Sid") == profile_sid)
    fm_stmt = next(s for s in stmts if s.get("Sid") == fm_sid)

    for stmt in (profile_stmt, fm_stmt):
        raw_actions = stmt.get("Action", [])  # absent on a NotAction Deny
        actions = raw_actions if isinstance(raw_actions, list) else [raw_actions]
        assert set(actions) == {"bedrock:InvokeModelWithResponseStream"}
        assert set(actions) == set(RUNTIME_MODEL_ACTIONS)


def test_bedrock_invoke_model_is_absent_from_agents_stack_and_all_allow_statements() -> None:
    """bedrock:InvokeModel is absent from every statement in AmbientChorusAgents and from

    every Allow statement across the whole application.
    """
    # 1. No statement in AmbientChorusAgents mentions bedrock:InvokeModel
    agents_tmpl = template_by_name("AmbientChorusAgents")
    for policy in agents_tmpl.find_resources(POLICY_TYPE).values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            raw_actions = stmt.get("Action", [])  # absent on a NotAction Deny
            actions = raw_actions if isinstance(raw_actions, list) else [raw_actions]
            assert "bedrock:InvokeModel" not in actions, f"Agents stack statement: {stmt}"

    # 2. No Allow statement anywhere in the application grants bedrock:InvokeModel
    for stack_name, tmpl in all_templates().items():
        for policy in tmpl.find_resources(POLICY_TYPE).values():
            for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
                if stmt.get("Effect") == "Allow":
                    actions = (
                        stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
                    )
                    assert "bedrock:InvokeModel" not in actions, (
                        f"Found bedrock:InvokeModel Allow in {stack_name}: {stmt}"
                    )


# --------------------------------------------------------------------------------------
# 5. No bedrock:* Wildcard Actions Anywhere
# --------------------------------------------------------------------------------------


def test_no_bedrock_wildcard_actions_on_any_allow_in_any_stack() -> None:
    """No **Allow** statement in any stack contains ``bedrock:*`` or ``bedrock-agentcore:*``.

    Scoped to Allow deliberately. An action wildcard inside a **Deny** is the *strongest* form
    of that boundary, not a violation of it, and the deployment contract's wildcard inventory
    (§ 8.7) lists ``bedrock:*`` inside a DENY as an accepted and intended wildcard. The sender,
    watcher, compiler, reset and API roles each carry exactly such a deny; asserting them away
    would be deleting the guarantee this test exists to protect.
    """

    deny_wildcards_seen: set[str] = set()

    for stack_name, tmpl in all_templates().items():
        for policy in tmpl.find_resources(POLICY_TYPE).values():
            for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
                raw_actions = stmt.get("Action", [])  # absent on a NotAction Deny
                actions = raw_actions if isinstance(raw_actions, list) else [raw_actions]
                wildcards = [a for a in actions if a in ("bedrock:*", "bedrock-agentcore:*")]
                if stmt.get("Effect") == "Deny":
                    deny_wildcards_seen.update(wildcards)
                    continue
                assert not wildcards, (
                    f"Found forbidden model wildcard on an Allow in {stack_name}: {stmt}"
                )

    # The negative assertion above is only meaningful while the positive denies still exist.
    assert {"bedrock:*", "bedrock-agentcore:*"} <= deny_wildcards_seen, (
        "the frozen model-access denies no longer carry their action wildcards"
    )


# --------------------------------------------------------------------------------------
# 6. No Resource: "*" for bedrock: or bedrock-agentcore: on any Allow Statement
# --------------------------------------------------------------------------------------


def test_no_wildcard_resource_for_bedrock_or_agentcore_allows() -> None:
    """No Allow statement in any stack has Resource: '*' for bedrock: or bedrock-agentcore:."""
    for stack_name, tmpl in all_templates().items():
        for policy in tmpl.find_resources(POLICY_TYPE).values():
            for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
                if stmt.get("Effect") != "Allow":
                    continue
                raw_actions = stmt.get("Action", [])  # absent on a NotAction Deny
                actions = raw_actions if isinstance(raw_actions, list) else [raw_actions]
                has_bedrock_action = any(
                    a.startswith("bedrock:") or a.startswith("bedrock-agentcore:") for a in actions
                )
                if has_bedrock_action:
                    resource = stmt.get("Resource", "")
                    assert resource != "*", (
                        f"Found Resource: '*' on Bedrock Allow in {stack_name}: {stmt}"
                    )
                    if isinstance(resource, list):
                        assert "*" not in resource, (
                            f"Found '*' in Resource list on Bedrock Allow in {stack_name}: {stmt}"
                        )


# --------------------------------------------------------------------------------------
# 7. Worker Role AgentCore Invocation
# --------------------------------------------------------------------------------------


def test_worker_role_has_one_agentcore_invoke_allow_on_three_live_endpoints() -> None:
    """The worker role has exactly one bedrock-agentcore:InvokeAgentRuntime Allow.

    Its Resource is exactly the 3 live-endpoint cross-stack references (count is 3,
    no wildcard, no runtime/*).
    """
    app_tmpl = template_by_name("AmbientChorusApplication")
    roles = app_tmpl.find_resources(ROLE_TYPE)
    worker_role_id = next(
        lid
        for lid, r in roles.items()
        if "chorus-worker" in str(r["Properties"].get("RoleName", ""))
    )
    stmts = statements_for_role(app_tmpl, worker_role_id)

    invoke_stmts = [
        s
        for s in stmts
        if s.get("Effect") == "Allow"
        and "bedrock-agentcore:InvokeAgentRuntime"
        in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    ]
    assert len(invoke_stmts) == 1, (
        f"Expected exactly 1 InvokeAgentRuntime statement on worker, found {len(invoke_stmts)}"
    )

    grant = invoke_stmts[0]
    resources = grant.get("Resource", [])
    assert isinstance(resources, list), f"Expected Resource list, got {type(resources)}"
    assert len(resources) == 3, f"Expected exactly 3 target resources, got {len(resources)}"

    for res in resources:
        res_str = json.dumps(res)
        assert "*" not in res_str, f"Found wildcard in worker target resource: {res_str}"
        assert "runtime/*" not in res_str, (
            f"Found runtime wildcard in worker target resource: {res_str}"
        )
        assert isinstance(res, dict), f"Expected cross-stack reference dict, got {res}"
        assert "Fn::ImportValue" in res or "Fn::GetAtt" in res, (
            f"Expected Fn::ImportValue or Fn::GetAtt, got {res}"
        )


# --------------------------------------------------------------------------------------
# 8. Non-Agent Lambda Roles Hold No Bedrock or AgentCore Allows
# --------------------------------------------------------------------------------------


WORKER_ROLE_NAME = "chorus-worker-demo"
AGENT_RUNTIME_ROLE_NAMES = frozenset(f"chorus-{agent}-runtime-demo" for agent in AGENTS)


def test_only_the_worker_and_the_three_runtimes_hold_bedrock_authority() -> None:
    """Every other role in the whole application holds no Bedrock authority at all.

    Swept across **every** role in **every** stack rather than against a hand-listed set of
    five role-name substrings. A named list is exactly the shape that silently stops testing
    anything when a role is renamed -- ``chorus-watcher`` and ``chorus-reset`` never matched
    the real ``chorus-commitment-watcher-demo`` and ``chorus-demo-reset-demo``, so those two
    parametrisations were selecting no role and proving nothing. Enumerating instead means a
    role added by a future macro is in scope the day it appears.
    """

    seen: set[str] = set()
    for stack_name, tmpl in all_templates().items():
        roles = tmpl.find_resources(ROLE_TYPE)
        for logical_id, resource in roles.items():
            role_name = resource["Properties"].get("RoleName")
            if not role_name:
                continue
            seen.add(role_name)
            for stmt in statements_for_role(tmpl, logical_id):
                if stmt.get("Effect") != "Allow":
                    continue
                raw = stmt.get("Action", [])
                actions = raw if isinstance(raw, list) else [raw]
                for action in actions:
                    if action.startswith("bedrock-agentcore:"):
                        assert role_name == WORKER_ROLE_NAME, (
                            f"{role_name} in {stack_name} holds AgentCore authority: {stmt}"
                        )
                    elif action.startswith("bedrock:"):
                        assert role_name in AGENT_RUNTIME_ROLE_NAMES, (
                            f"{role_name} in {stack_name} holds Bedrock authority: {stmt}"
                        )

    # The sweep is only meaningful if it actually reached the roles it is about.
    assert WORKER_ROLE_NAME in seen
    assert seen >= AGENT_RUNTIME_ROLE_NAMES
    assert {"chorus-api-demo", "chorus-compiler-demo", "chorus-sender-demo"} <= seen
    assert {"chorus-commitment-watcher-demo", "chorus-demo-reset-demo"} <= seen


# --------------------------------------------------------------------------------------
# 9. Worker Environment and IAM Resource Identity Alignment
# --------------------------------------------------------------------------------------


def test_worker_environment_and_iam_resource_are_same_json_object() -> None:
    """The worker's CHORUS_*_RUNTIME_ARN environment values and the corresponding IAM

    Resource entries are the exact same JSON object.
    """
    app_tmpl = template_by_name("AmbientChorusApplication")

    # Locate worker Lambda function environment variables
    functions = app_tmpl.find_resources(FUNCTION_TYPE)
    worker_fn = next(
        f
        for f in functions.values()
        if "chorus-worker" in str(f["Properties"].get("FunctionName", ""))
    )
    env_vars = worker_fn["Properties"]["Environment"]["Variables"]

    env_monitor = env_vars["CHORUS_MONITOR_RUNTIME_ARN"]
    env_investigator = env_vars["CHORUS_INVESTIGATOR_RUNTIME_ARN"]
    env_action = env_vars["CHORUS_ACTION_RUNTIME_ARN"]

    # Locate worker role's InvokeNamedAgentRuntimesOnly IAM statement
    roles = app_tmpl.find_resources(ROLE_TYPE)
    worker_role_id = next(
        lid
        for lid, r in roles.items()
        if "chorus-worker" in str(r["Properties"].get("RoleName", ""))
    )
    stmts = statements_for_role(app_tmpl, worker_role_id)
    grant = next(s for s in stmts if s.get("Sid") == "InvokeNamedAgentRuntimesOnly")
    iam_resources = grant["Resource"]

    # Match each agent's environment object to the IAM resource entry
    assert env_monitor in iam_resources, "Monitor runtime ARN object not in worker IAM resources"
    assert env_investigator in iam_resources, (
        "Investigator runtime ARN object not in worker IAM resources"
    )
    assert env_action in iam_resources, "Action runtime ARN object not in worker IAM resources"

    assert env_monitor == iam_resources[0]
    assert env_investigator == iam_resources[1]
    assert env_action == iam_resources[2]


# --------------------------------------------------------------------------------------
# 10. Chunk 1 Invariant: No Implicit Grants on Runtime Roles
# --------------------------------------------------------------------------------------


def test_runtime_execution_roles_gain_no_implicit_grants() -> None:
    """No runtime execution role carries implicit grants from agentcore.Runtime L2.

    Explicitly verifies absence of:
    - cloudwatch:PutMetricData
    - logs:CreateLogGroup
    - logs:DescribeLogGroups
    - bedrock-agentcore:GetWorkloadAccessToken*
    - s3:GetBucket*
    - s3:List*
    - s3:GetObject* (wildcards beyond the specific own-prefix s3:GetObject)
    """
    tmpl = template_by_name("AmbientChorusAgents")
    forbidden_prefixes = (
        "cloudwatch:PutMetricData",
        "logs:CreateLogGroup",
        "logs:DescribeLogGroups",
        "bedrock-agentcore:GetWorkloadAccessToken",
        "s3:GetBucket",
        "s3:List",
    )

    for agent in AGENTS:
        stmts = statements_for_role(tmpl, role_logical_id(agent))
        for stmt in stmts:
            if stmt.get("Effect") != "Allow":
                continue
            raw_actions = stmt.get("Action", [])  # absent on a NotAction Deny
            actions = raw_actions if isinstance(raw_actions, list) else [raw_actions]
            for action in actions:
                for prefix in forbidden_prefixes:
                    assert not action.startswith(prefix), (
                        f"Forbidden implicit grant {action} found on {agent} role: {stmt}"
                    )
                assert action != "s3:GetObject*", (
                    f"Forbidden wildcard s3:GetObject* found on {agent} role: {stmt}"
                )


# --------------------------------------------------------------------------------------
# 11. Artifact Scoping: Each Runtime Role Reads Only Its Own Artifact Prefix
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_runtime_role_s3_get_object_scoped_to_own_artifact_prefix(agent: str) -> None:
    """Each runtime role's s3:GetObject names only its own artifact prefix."""
    tmpl = template_by_name("AmbientChorusAgents")
    stmts = statements_for_role(tmpl, role_logical_id(agent))

    get_stmts = [
        s
        for s in stmts
        if s.get("Effect") == "Allow"
        and "s3:GetObject" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    ]

    assert len(get_stmts) == 1, (
        f"Expected exactly 1 s3:GetObject Allow for {agent}, found {len(get_stmts)}"
    )

    grant = get_stmts[0]
    resource_str = json.dumps(grant["Resource"])
    assert f"/{agent}/*" in resource_str, (
        f"Expected /{agent}/* prefix in Resource for {agent}, got {resource_str}"
    )

    # Prove no other agent's prefix is present
    other_agents = [a for a in AGENTS if a != agent]
    for other in other_agents:
        assert f"/{other}/*" not in resource_str, (
            f"Agent {agent}'s s3:GetObject grant contains other agent prefix /{other}/*: "
            f"{resource_str}"
        )
