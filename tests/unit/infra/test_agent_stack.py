"""The Monitor runtime's boundary, asserted from the synthesized template.

Isolation is an identity property, so it can be proved before anything is deployed. Every
assertion here is about the role: what it may do (invoke one inference profile, write its own
logs, emit its own traces) and what it is explicitly denied.

A static policy assertion is necessary and not sufficient. Post-deploy AccessDenied canaries
are the other half and belong to the deployment phase; this file is what keeps a regression
from reaching that point unnoticed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from aws_cdk import App, assertions
from infra.cdk.app import build_app
from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks import ChorusAgentStack
from infra.cdk.stacks.agents import (
    AGENTCORE_SERVICE_PRINCIPAL,
    DENIED_DATASTORE_ACTIONS,
    DENIED_EVIDENCE_OBJECT_ACTIONS,
    DENIED_OBJECT_MUTATION_ACTIONS,
    DENIED_SIDE_EFFECT_ACTIONS,
    INFERENCE_PROFILE_ARN_CONDITION_KEY,
    NOVA_2_LITE_BASE_MODEL_ID,
    RUNTIME_MODEL_ACTIONS,
    US_INFERENCE_PROFILE_DESTINATION_REGIONS,
)

# P1-2: the runtime roles need BOTH actions -- ``strands`` structured output streams by
# default. Sourced from the stack module so a change there fails these tests loudly.
MODEL_ACTIONS = set(RUNTIME_MODEL_ACTIONS)

POLICY_TYPE = "AWS::IAM::Policy"
ROLE_TYPE = "AWS::IAM::Role"

# Discovered application inference-profile ARNs carry a service-generated suffix -- they are
# never constructed from ``chorus-monitor-demo`` (deployment contract § 4). These stand in for
# that shape in the assertions below.
MONITOR_PROFILE_ARN = (
    "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/chorus-monitor-a1b2c3d4"
)
INVESTIGATOR_PROFILE_ARN = (
    "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/chorus-investigator-e5f6"
)
ACTION_PROFILE_ARN = (
    "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/chorus-action-99887766"
)
PROFILE_ARNS = {
    "monitor_model_profile_arn": MONITOR_PROFILE_ARN,
    "investigator_model_profile_arn": INVESTIGATOR_PROFILE_ARN,
    "action_model_profile_arn": ACTION_PROFILE_ARN,
}


def template(config: CdkBuildConfig | None = None, **kwargs: object) -> assertions.Template:
    app = App()
    stack = ChorusAgentStack(
        app,
        "TestAgents",
        config=config or CdkBuildConfig(),
        **{**PROFILE_ARNS, **kwargs},  # type: ignore[arg-type]
    )
    return assertions.Template.from_stack(stack)


def template_without_profiles() -> assertions.Template:
    app = App()
    stack = ChorusAgentStack(app, "TestAgents", config=CdkBuildConfig())
    return assertions.Template.from_stack(stack)


def statements() -> list[Mapping[str, Any]]:
    policies = template().find_resources(POLICY_TYPE)
    found: list[Mapping[str, Any]] = []
    for policy in policies.values():
        found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def statement(sid: str) -> Mapping[str, Any]:
    return next(item for item in statements() if item.get("Sid") == sid)


def actions_of(sid: str) -> set[str]:
    action = statement(sid)["Action"]
    return {action} if isinstance(action, str) else set(action)


def test_the_stack_creates_exactly_one_role_per_agent_runtime() -> None:
    """Three roles from Phase 7 on: Monitor, Investigator, Action, and nothing else.

    The count is asserted rather than the names, because the failure this catches is a *fourth*
    role appearing -- a shared "agents" role, or a convenience role attached during a later
    phase -- which is how three isolated identities quietly become one.
    """

    template().resource_count_is(ROLE_TYPE, 3)


def test_the_role_is_assumable_only_by_the_agentcore_service() -> None:
    roles = template().find_resources(ROLE_TYPE)
    document = next(iter(roles.values()))["Properties"]["AssumeRolePolicyDocument"]

    principals = [item["Principal"] for item in document["Statement"]]
    assert principals == [{"Service": AGENTCORE_SERVICE_PRINCIPAL}]


def test_the_role_may_invoke_only_its_own_inference_profile() -> None:
    allowed = statement("InvokeMonitorInferenceProfileOnly")

    assert allowed["Effect"] == "Allow"
    # I4 / P1-2: BOTH model actions -- ``strands`` ``structured_output`` -> ``stream`` ->
    # ``converse_stream`` (streaming defaults on), which IAM authorizes through
    # ``bedrock:InvokeModelWithResponseStream``; ``InvokeModel`` covers the non-streaming path.
    assert actions_of("InvokeMonitorInferenceProfileOnly") == MODEL_ACTIONS
    assert allowed["Resource"] == MONITOR_PROFILE_ARN
    assert allowed["Resource"] != "*"
    assert "Condition" not in allowed


# -- I4: Nova 2 Lite through one application inference profile per agent -----------------


def test_no_model_grant_at_all_until_the_profile_arn_is_a_discovered_input() -> None:
    """``_default_profile_arn`` is gone: no name-constructed ARN can become authority.

    With no profile ARN supplied the role synthesizes offline with **no** Bedrock statement,
    and the deploy pipeline must pass the discovered ARN before a runtime can invoke a model.
    """

    for policy in template_without_profiles().find_resources(POLICY_TYPE).values():
        for item in policy["Properties"]["PolicyDocument"]["Statement"]:
            for action in item["Action"] if isinstance(item["Action"], list) else [item["Action"]]:
                assert not action.startswith("bedrock:"), item.get("Sid")


def test_profile_arns_must_be_supplied_together_or_not_at_all() -> None:
    with pytest.raises(ValueError, match="together or not at all"):
        ChorusAgentStack(
            App(),
            "PartialAgents",
            config=CdkBuildConfig(),
            monitor_model_profile_arn=MONITOR_PROFILE_ARN,
        )


@pytest.mark.parametrize(
    ("profile_sid", "fm_sid", "profile_arn"),
    [
        (
            "InvokeMonitorInferenceProfileOnly",
            "InvokeMonitorFoundationModelsViaProfileOnly",
            MONITOR_PROFILE_ARN,
        ),
        (
            "InvokeInvestigatorInferenceProfileOnly",
            "InvokeInvestigatorFoundationModelsViaProfileOnly",
            INVESTIGATOR_PROFILE_ARN,
        ),
        (
            "InvokeActionInferenceProfileOnly",
            "InvokeActionFoundationModelsViaProfileOnly",
            ACTION_PROFILE_ARN,
        ),
    ],
)
def test_each_role_invokes_its_own_profile_and_the_fm_arns_bound_to_it(
    profile_sid: str, fm_sid: str, profile_arn: str
) -> None:
    """ALLOW own application profile; ALLOW the Nova 2 Lite FM ARNs for the three frozen US
    regions, condition-bound to that same profile so it is not a direct FM invocation."""

    profile = statement(profile_sid)
    assert profile["Effect"] == "Allow"
    # P1-2: both actions present in the application-profile statement.
    assert actions_of(profile_sid) == MODEL_ACTIONS
    assert profile["Resource"] == profile_arn

    fm = statement(fm_sid)
    assert fm["Effect"] == "Allow"
    # P1-2: both actions present in the condition-bound foundation-model statement too.
    assert actions_of(fm_sid) == MODEL_ACTIONS
    assert set(fm["Resource"]) == {
        f"arn:aws:bedrock:{region}::foundation-model/{NOVA_2_LITE_BASE_MODEL_ID}"
        for region in US_INFERENCE_PROFILE_DESTINATION_REGIONS
    }
    # No account id in a foundation-model ARN, and all three frozen regions present.
    assert all("111122223333" not in arn for arn in fm["Resource"])
    for region in ("us-east-1", "us-east-2", "us-west-2"):
        assert any(f":{region}::foundation-model/" in arn for arn in fm["Resource"])
    assert fm["Condition"]["StringEquals"][INFERENCE_PROFILE_ARN_CONDITION_KEY] == profile_arn


def test_no_role_receives_another_agents_profile_or_fm_condition() -> None:
    """Monitor's FM grant is bound to Monitor's profile, never Investigator's or Action's."""

    monitor_fm = statement("InvokeMonitorFoundationModelsViaProfileOnly")
    bound = monitor_fm["Condition"]["StringEquals"][INFERENCE_PROFILE_ARN_CONDITION_KEY]
    assert bound == MONITOR_PROFILE_ARN
    assert INVESTIGATOR_PROFILE_ARN not in str(monitor_fm)
    assert ACTION_PROFILE_ARN not in str(monitor_fm)


def test_no_unconditioned_foundation_model_invocation_statement_exists() -> None:
    """Every model allow -- ``InvokeModel`` *or* ``InvokeModelWithResponseStream`` -- either
    names an application profile ARN, or names FM ARNs and is condition-bound to one. No
    statement grants unrestricted foundation-model invocation, streaming included (P1-2)."""

    for item in statements():
        if item["Effect"] != "Allow":
            continue
        actions = set(item["Action"] if isinstance(item["Action"], list) else [item["Action"]])
        if not actions & MODEL_ACTIONS:
            continue
        resources = item["Resource"] if isinstance(item["Resource"], list) else [item["Resource"]]
        if any("foundation-model/" in arn for arn in resources):
            assert INFERENCE_PROFILE_ARN_CONDITION_KEY in str(item.get("Condition")), item["Sid"]
        else:
            assert all("application-inference-profile/" in arn for arn in resources), item["Sid"]


def test_no_unconditioned_streaming_foundation_model_grant() -> None:
    """P1-2 specifically: any statement that grants ``InvokeModelWithResponseStream`` over a
    foundation-model ARN carries the ``bedrock:InferenceProfileArn`` condition."""

    for item in statements():
        if item["Effect"] != "Allow":
            continue
        actions = set(item["Action"] if isinstance(item["Action"], list) else [item["Action"]])
        if "bedrock:InvokeModelWithResponseStream" not in actions:
            continue
        resources = item["Resource"] if isinstance(item["Resource"], list) else [item["Resource"]]
        if any("foundation-model/" in arn for arn in resources):
            bound = item.get("Condition", {}).get("StringEquals", {})
            assert bound.get(INFERENCE_PROFILE_ARN_CONDITION_KEY) is not None, item["Sid"]


def test_converse_is_never_granted() -> None:
    """Streaming *is* granted (P1-2); the ``Converse`` API-name actions never are."""

    for item in statements():
        if item["Effect"] != "Allow":
            continue
        actions = item["Action"] if isinstance(item["Action"], list) else [item["Action"]]
        for action in actions:
            assert action not in {"bedrock:Converse", "bedrock:ConverseStream"}


def test_the_role_writes_only_to_its_own_log_group() -> None:
    allowed = statement("WriteOwnLogsOnly")

    assert allowed["Effect"] == "Allow"
    assert "*" not in [allowed["Resource"]]
    assert set(allowed["Action"]) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams",
    }


@pytest.mark.parametrize("action", sorted(DENIED_DATASTORE_ACTIONS))
def test_every_data_store_action_is_explicitly_denied(action: str) -> None:
    denied = statement("DenyEveryDataStore")

    assert denied["Effect"] == "Deny"
    assert denied["Resource"] == "*"
    assert action in denied["Action"]
    assert not any(str(a).startswith("s3:") for a in denied["Action"])


@pytest.mark.parametrize("action", sorted(DENIED_SIDE_EFFECT_ACTIONS))
def test_every_external_effect_is_explicitly_denied(action: str) -> None:
    denied = statement("DenyEveryExternalEffect")

    assert denied["Effect"] == "Deny"
    assert action in denied["Action"]


def test_the_role_can_never_read_a_dynamodb_table() -> None:
    denied = actions_of("DenyEveryDataStore")

    assert {"dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"} <= denied


def test_the_role_can_never_read_or_write_either_evidence_bucket() -> None:
    """SS 6: the GetObject deny is scoped to the evidence buckets it protects, not to ``*``.

    Read and write on both the private and the export bucket -- and their objects -- are denied
    by name, which is stronger than the wildcard that also blocked the runtime's own artifact.
    """

    denied = statement("DenyEvidenceObjectAccess")

    assert denied["Effect"] == "Deny"
    assert set(denied["Action"]) == set(DENIED_EVIDENCE_OBJECT_ACTIONS)
    assert {"s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"} <= set(
        denied["Action"]
    )
    rendered = str(denied["Resource"])
    assert "chorus-private-evidence" in rendered
    assert "chorus-export-evidence" in rendered
    assert denied["Resource"] != "*" and "*" not in [denied["Resource"]]


def test_the_role_can_never_write_or_list_any_bucket() -> None:
    """``deny_object_mutation`` keeps write and list denied on ``*`` -- read is not, so the
    artifact prefix stays reachable."""

    denied = statement("DenyObjectMutation")

    assert denied["Effect"] == "Deny"
    assert denied["Resource"] == "*"
    assert set(denied["Action"]) == set(DENIED_OBJECT_MUTATION_ACTIONS)
    assert "s3:GetObject" not in denied["Action"]


def test_no_deny_statement_blocks_get_object_on_a_wildcard_resource() -> None:
    """The defect I8 repairs: a blanket ``s3:GetObject`` deny wins over the artifact allow.

    Every Deny that names ``s3:GetObject`` must be scoped to a concrete resource, never ``*``.
    """

    for item in statements():
        if item["Effect"] != "Deny":
            continue
        acts = item["Action"] if isinstance(item["Action"], list) else [item["Action"]]
        if "s3:GetObject" in acts:
            assert item["Resource"] != "*", item.get("Sid")


def test_the_role_can_never_send_email() -> None:
    denied = actions_of("DenyEveryExternalEffect")

    assert {"ses:SendEmail", "ses:SendRawEmail", "sesv2:SendEmail"} <= denied


def test_the_role_can_never_invoke_the_compiler_the_sender_or_another_agent() -> None:
    denied = actions_of("DenyEveryExternalEffect")

    assert "lambda:InvokeFunction" in denied
    assert "bedrock-agentcore:InvokeAgentRuntime" in denied


def test_the_role_can_never_read_a_secret_or_use_an_evidence_key() -> None:
    denied = actions_of("DenyEveryExternalEffect")

    assert "secretsmanager:GetSecretValue" in denied
    assert {"kms:Decrypt", "kms:GenerateDataKey"} <= denied


def test_no_allow_statement_grants_a_wildcard_data_action() -> None:
    for item in statements():
        if item["Effect"] != "Allow":
            continue
        actions = item["Action"] if isinstance(item["Action"], list) else [item["Action"]]
        for action in actions:
            assert not action.startswith("dynamodb:")
            assert not action.startswith("s3:") or item.get("Sid") == "ReadOwnDirectCodeArtifact"
            assert not action.startswith("ses")
            assert action != "*"


def test_the_artifact_grant_is_absent_until_an_artifact_bucket_is_supplied() -> None:
    assert all(item.get("Sid") != "ReadOwnDirectCodeArtifact" for item in statements())


def test_the_artifact_grant_is_scoped_to_the_monitor_prefix_when_supplied() -> None:
    app = App()
    stack = ChorusAgentStack(
        app,
        "TestAgents",
        config=CdkBuildConfig(),
        artifact_bucket_arn="arn:aws:s3:::chorus-agent-artifacts",
    )
    document = assertions.Template.from_stack(stack).find_resources(POLICY_TYPE)
    found = [
        item
        for policy in document.values()
        for item in policy["Properties"]["PolicyDocument"]["Statement"]
        if item.get("Sid") == "ReadOwnDirectCodeArtifact"
    ]

    assert found
    assert found[0]["Resource"] == "arn:aws:s3:::chorus-agent-artifacts/monitor/*"


# -- I8: each runtime reads its own artifact and remains blind to evidence --------------

ARTIFACT_BUCKET = "arn:aws:s3:::chorus-agent-artifacts-development"


def _with_artifacts() -> list[dict[str, Any]]:
    app = App()
    stack = ChorusAgentStack(
        app, "TestAgents", config=CdkBuildConfig(), artifact_bucket_arn=ARTIFACT_BUCKET
    )
    built = assertions.Template.from_stack(stack)
    found: list[dict[str, Any]] = []
    for policy in built.find_resources(POLICY_TYPE).values():
        found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


@pytest.mark.parametrize("prefix", ["monitor", "investigator", "action"])
def test_each_runtime_may_read_its_own_artifact_object(prefix: str) -> None:
    """ALLOW: ``s3:GetObject`` on ``{artifact-bucket}/{agent}/*`` and no deny takes it back.

    The deny split is what makes this true -- a blanket ``s3:GetObject`` deny would win over
    this allow and the runtime could not cold-start (SS 6).
    """

    allows = [
        item
        for item in _with_artifacts()
        if item["Effect"] == "Allow"
        and "s3:GetObject"
        in (item["Action"] if isinstance(item["Action"], list) else [item["Action"]])
        and f"/{prefix}/*" in str(item["Resource"])
    ]
    assert len(allows) == 1
    assert allows[0]["Resource"] == f"{ARTIFACT_BUCKET}/{prefix}/*"

    denies_hitting_getobject = [
        item
        for item in _with_artifacts()
        if item["Effect"] == "Deny"
        and "s3:GetObject"
        in (item["Action"] if isinstance(item["Action"], list) else [item["Action"]])
    ]
    for item in denies_hitting_getobject:
        rendered = str(item["Resource"])
        assert item["Resource"] != "*"
        assert f"/{prefix}/*" not in rendered
        assert "chorus-agent-artifacts" not in rendered


def test_no_runtime_may_read_another_runtimes_artifact() -> None:
    """DENY (by absence of allow): the only ``GetObject`` allow is this agent's own prefix.

    Each role's artifact grant names exactly one ``{agent}/*`` prefix, so a role reaching for
    another agent's object matches no allow at all.
    """

    get_allows = [
        item
        for item in _with_artifacts()
        if item["Effect"] == "Allow"
        and "s3:GetObject"
        in (item["Action"] if isinstance(item["Action"], list) else [item["Action"]])
    ]
    # Three roles, three prefixes, no prefix granted twice.
    resources = sorted(str(item["Resource"]) for item in get_allows)
    assert resources == [
        f"{ARTIFACT_BUCKET}/action/*",
        f"{ARTIFACT_BUCKET}/investigator/*",
        f"{ARTIFACT_BUCKET}/monitor/*",
    ]


def test_no_runtime_may_read_a_private_or_export_evidence_object() -> None:
    """DENY: ``deny_evidence_objects`` covers both evidence buckets and their objects."""

    denied = statement("DenyEvidenceObjectAccess")
    resources = {str(r) for r in denied["Resource"]}
    assert resources == {
        "arn:aws:s3:::chorus-private-evidence-development",
        "arn:aws:s3:::chorus-private-evidence-development/*",
        "arn:aws:s3:::chorus-export-evidence-development",
        "arn:aws:s3:::chorus-export-evidence-development/*",
    }
    assert "s3:GetObject" in denied["Action"]


def test_no_runtime_may_write_delete_or_list_any_object() -> None:
    """DENY: arbitrary S3 write/delete/list stays a wildcard deny."""

    denied = statement("DenyObjectMutation")
    assert denied["Resource"] == "*"
    assert set(denied["Action"]) == {"s3:PutObject", "s3:DeleteObject", "s3:ListBucket"}


def test_the_runtime_log_group_is_dedicated_to_the_monitor() -> None:
    template().has_resource_properties(
        "AWS::Logs::LogGroup",
        {"LogGroupName": "/aws/bedrock-agentcore/chorus-monitor-development"},
    )


def test_the_application_synthesizes_the_agent_stack() -> None:
    assembly = build_app(offline=True).synth()

    assert "AmbientChorusAgents" in [stack.stack_name for stack in assembly.stacks]
