"""Category J: the watcher's synthesized role is the smallest in the system, and provably so.

A negative-capability sweep in the manner ADR-019 established and ADR-024 extended: the claims
are about what the role *cannot* do, so they are read off the synthesized policy rather than
argued from the repository. Everything asserted here is checkable long before anything is
deployed, which is the whole point of synthesizing a principal a phase does not deploy.

The watcher's trust-matrix row was **wrong** until Phase 9. It read
``Share: R/W(commitment/case projection)``, which mislocated the case row: the case lives in
**Core**, which the watcher is denied outright, and the commitment, its schedule projection, and
the verification-request item all live in the Shareable ``NS#n#CASE#k`` partition. The watcher
takes no case edge in either table
([ADR-028](../../../docs/adr/ADR-028-deadline-watcher-and-scheduler-boundary.md) § 6).
"""

from __future__ import annotations

import json
import re
from functools import cache
from typing import Any

import pytest
from aws_cdk import App, assertions
from infra.cdk.app import build_app
from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks import ChorusDataStack, ChorusWatcherStack, WatcherBuckets, WatcherTables
from infra.cdk.stacks.watcher import (
    CASE_KEY_PREFIX,
    DEMO_CLOCK_PARTITION,
    DLQ_DEPTH_ALARM_THRESHOLD,
    FORBIDDEN_WRITE_PREFIXES,
)

POLICY_TYPE = "AWS::IAM::Policy"


@cache
def watcher_template() -> assertions.Template:
    app = App()
    config = CdkBuildConfig()
    data = ChorusDataStack(app, "TestData", config=config)
    stack = ChorusWatcherStack(
        app,
        "TestWatcher",
        config=config,
        tables=WatcherTables(
            core=data.core_table, shareable=data.shareable_table, audit=data.audit_table
        ),
        buckets=WatcherBuckets(
            private=data.private_evidence_bucket, export=data.export_evidence_bucket
        ),
    )
    return assertions.Template.from_stack(stack)


def statements() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for resource in watcher_template().find_resources(POLICY_TYPE).values():
        found.extend(resource["Properties"]["PolicyDocument"]["Statement"])
    return found


def statement(sid: str) -> dict[str, Any]:
    for item in statements():
        if item.get("Sid") == sid:
            return item
    raise AssertionError(f"no statement with Sid {sid}")


def actions_of(item: dict[str, Any]) -> set[str]:
    action = item["Action"]
    return {action} if isinstance(action, str) else set(action)


def all_allowed_actions() -> set[str]:
    allowed: set[str] = set()
    for item in statements():
        if item["Effect"] == "Allow":
            allowed |= actions_of(item)
    return allowed


def test_the_watcher_writes_only_the_shareable_case_partition() -> None:
    write = statement("WriteCasePartitionOnly")

    assert actions_of(write) == {"dynamodb:PutItem", "dynamodb:ConditionCheckItem"}
    assert write["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"] == [
        CASE_KEY_PREFIX
    ]
    assert "dynamodb:UpdateItem" not in actions_of(write)
    assert "dynamodb:DeleteItem" not in actions_of(write)


def test_every_other_shareable_prefix_is_denied_by_for_any_value() -> None:
    """A transaction naming *any* item outside the case partition is refused whole."""

    deny = statement("DenyNonCasePartitionWrites")

    assert deny["Effect"] == "Deny"
    assert deny["Condition"]["ForAnyValue:StringLike"]["dynamodb:LeadingKeys"] == list(
        FORBIDDEN_WRITE_PREFIXES
    )
    assert "NS#*#OUTBOUND_MESSAGE#*" in FORBIDDEN_WRITE_PREFIXES


def test_core_is_denied_in_total() -> None:
    """Not merely ungranted -- denied, over the whole table. The watcher takes no case edge."""

    deny = statement("DenyAllCoreAccess")

    assert deny["Effect"] == "Deny"
    assert actions_of(deny) == {"dynamodb:*"}


@pytest.mark.parametrize(
    "sid",
    [
        "DenyWatcherModelAccess",
        "DenyWatcherSend",
        "DenyWatcherScheduler",
        "DenyEvidenceObjects",
    ],
)
def test_the_four_denies_are_explicit(sid: str) -> None:
    assert statement(sid)["Effect"] == "Deny"


def test_the_role_holds_no_bedrock_ses_scheduler_or_s3_allow() -> None:
    """The negative-capability sweep, stated as one assertion over every allowed action."""

    allowed = all_allowed_actions()

    assert not any(action.startswith("bedrock") for action in allowed)
    assert not any(action.startswith("ses") for action in allowed)
    assert not any(action.startswith("scheduler") for action in allowed)
    assert not any(action.startswith("s3:") for action in allowed)


def test_the_role_holds_no_core_table_action_at_all() -> None:
    """Every allowed DynamoDB statement names the Shareable or the Audit table, never Core."""

    core_arn_fragment = "core"
    for item in statements():
        if item["Effect"] != "Allow":
            continue
        if not any(action.startswith("dynamodb:") for action in actions_of(item)):
            continue
        rendered = json.dumps(item["Resource"])
        assert core_arn_fragment not in rendered.lower()


def test_the_schedule_group_and_encrypted_dead_letter_queue_exist() -> None:
    built = watcher_template()

    built.resource_count_is("AWS::Scheduler::ScheduleGroup", 1)
    built.resource_count_is("AWS::SQS::Queue", 1)
    built.resource_count_is("AWS::KMS::Key", 1)
    built.has_resource_properties(
        "AWS::SQS::Queue", {"MessageRetentionPeriod": assertions.Match.any_value()}
    )


def test_one_dropped_due_event_alarms() -> None:
    """A dead-lettered due event is silent from every other surface: the case just sits."""

    watcher_template().has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "Threshold": DLQ_DEPTH_ALARM_THRESHOLD,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        },
    )


def test_the_watcher_function_version_and_live_alias_exist() -> None:
    """Phase 11 batch 5: the watcher Lambda, a published Version, and an Alias named ``live``
    (deployment contract SS 21-23, SS 46). Under the pre-existing watcher role and log group.
    """

    built = watcher_template()
    functions = built.find_resources("AWS::Lambda::Function")
    assert len(functions) == 1
    props = next(iter(functions.values()))["Properties"]
    assert props["Runtime"] == "python3.12"
    assert props["Architectures"] == ["x86_64"]
    assert props["Handler"] == "functions.commitment_watcher.handler.handler"
    assert props["FunctionName"] == "chorus-commitment-watcher-development"
    assert props["Role"]["Fn::GetAtt"][0].startswith("WatcherRole")
    assert props["LoggingConfig"]["LogGroup"]["Ref"].startswith("WatcherLogGroup")

    built.resource_count_is("AWS::Lambda::Version", 1)
    aliases = built.find_resources("AWS::Lambda::Alias")
    assert len(aliases) == 1
    alias = next(iter(aliases.values()))["Properties"]
    assert alias["Name"] == "live"
    version_logical = alias["FunctionVersion"]["Fn::GetAtt"][0]
    function_logical = next(iter(functions))
    assert alias["FunctionName"]["Ref"] == function_logical
    version = built.find_resources("AWS::Lambda::Version")[version_logical]
    assert version["Properties"]["FunctionName"]["Ref"] == function_logical


def test_the_scheduler_invoke_and_the_alias_are_one_resource() -> None:
    """SS 46: the scheduler role's ``lambda:InvokeFunction`` Resource is a ``Ref`` to the
    actual ``Alias`` resource, not a re-typed lookalike ARN string."""

    built = watcher_template()
    alias_logical = next(iter(built.find_resources("AWS::Lambda::Alias")))
    grant = _scheduler_statement("InvokeCommitmentWatcherLiveAliasOnly")
    assert grant["Resource"] == {"Ref": alias_logical}


# ------------------------------------------------------------------------------------------
# The application's narrowed scheduler grant
# ------------------------------------------------------------------------------------------


@cache
def application_statements() -> list[dict[str, Any]]:
    template = (
        build_app(offline=True).synth().get_stack_by_name("AmbientChorusApplication").template
    )
    found: list[dict[str, Any]] = []
    for resource in template["Resources"].values():
        if resource["Type"] == POLICY_TYPE:
            found.extend(resource["Properties"]["PolicyDocument"]["Statement"])
    return found


def application_statement(sid: str) -> dict[str, Any]:
    for item in application_statements():
        if item.get("Sid") == sid:
            return item
    raise AssertionError(f"no application statement with Sid {sid}")


def test_the_application_may_create_and_get_a_schedule_and_nothing_else() -> None:
    """A grant wider than its caller is a grant waiting for a second caller (ADR-028 § 6)."""

    allow = application_statement("CreateAndGetCommitmentSchedulesOnly")

    assert actions_of(allow) == {"scheduler:CreateSchedule", "scheduler:GetSchedule"}
    assert "scheduler:DeleteSchedule" not in actions_of(allow)
    assert "scheduler:UpdateSchedule" not in actions_of(allow)


def test_schedule_deletion_and_update_are_denied_outright() -> None:
    deny = application_statement("DenyScheduleDeletionAndUpdate")

    assert deny["Effect"] == "Deny"
    assert actions_of(deny) == {"scheduler:DeleteSchedule", "scheduler:UpdateSchedule"}


def test_the_scheduler_grant_names_the_one_schedule_group() -> None:
    allow = application_statement("CreateAndGetCommitmentSchedulesOnly")

    assert "chorus-development/*" in json.dumps(allow["Resource"])


def test_pass_role_names_the_scheduler_execution_role_alone() -> None:
    """A broader ``iam:PassRole`` would let the application hand any role to any target."""

    allow = application_statement("PassSchedulerExecutionRoleOnly")

    rendered = json.dumps(allow["Resource"])
    assert "chorus-scheduler-development" in rendered
    assert allow["Condition"]["StringEquals"]["iam:PassedToService"] == "scheduler.amazonaws.com"


def test_the_application_may_write_the_outbound_message_locator_prefix() -> None:
    """The projection writes the locator, so the grant names its partition explicitly."""

    allow = application_statement("WriteActionAndCasePrefixesOnly")

    prefixes = allow["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]
    assert "NS#*#OUTBOUND_MESSAGE#*" in prefixes


def test_the_sender_is_denied_the_outbound_message_locator_prefix() -> None:
    """A sender that could write a locator could point a reply at an execution it chose."""

    template = build_app(offline=True).synth().get_stack_by_name("AmbientChorusSender").template
    denies = [
        item
        for resource in template["Resources"].values()
        if resource["Type"] == POLICY_TYPE
        for item in resource["Properties"]["PolicyDocument"]["Statement"]
        if item.get("Sid") == "DenyProposalApprovalViewAndCaseWrites"
    ]

    assert len(denies) == 1
    prefixes = denies[0]["Condition"]["ForAnyValue:StringLike"]["dynamodb:LeadingKeys"]
    assert "NS#*#OUTBOUND_MESSAGE#*" in prefixes


# =====================================================================================
# I14 -- the EventBridge Scheduler execution role: exact minimum, and a scoped trust
# =====================================================================================

ROLE_TYPE = "AWS::IAM::Role"


def _scheduler_role() -> dict[str, Any]:
    roles = watcher_template().find_resources(ROLE_TYPE)
    return next(
        dict(role)
        for role in roles.values()
        if "chorus-scheduler" in str(role["Properties"].get("RoleName", ""))
    )


def _scheduler_role_statements() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for logical_id, policy in watcher_template().find_resources(POLICY_TYPE).items():
        if logical_id.startswith("SchedulerExecutionRole"):
            found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def _scheduler_statement(sid: str) -> dict[str, Any]:
    for item in _scheduler_role_statements():
        if item.get("Sid") == sid:
            return item
    raise AssertionError(f"no scheduler-role statement with Sid {sid}")


def test_the_scheduler_role_trust_is_scoped_to_account_and_the_schedule_group_arn() -> None:
    """P1-1: the confused-deputy boundary is the exact **schedule-group** ARN -- not an
    individual schedule ARN and never a wildcard schedule name."""

    statement = _scheduler_role()["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]

    assert statement["Principal"] == {"Service": "scheduler.amazonaws.com"}
    assert statement["Action"] == "sts:AssumeRole"

    account = statement["Condition"]["StringEquals"]["aws:SourceAccount"]
    assert account == {"Ref": "AWS::AccountId"} or "AWS::AccountId" in json.dumps(account)

    source_arn = json.dumps(statement["Condition"]["ArnEquals"]["aws:SourceArn"])
    assert "schedule-group/chorus-development" in source_arn
    assert "schedule/chorus-development/" not in source_arn  # no individual-schedule prefix
    assert "*" not in source_arn


def test_the_scheduler_role_invokes_the_watcher_live_alias_only() -> None:
    """P2-1 / SS 46: the invoke resource is a ``Ref`` to the actual ``AWS::Lambda::Alias``
    named ``live``, so rollback repoints that alias with no schedule or IAM edit. Never the
    unqualified function, ``$LATEST``, or a bare version -- and never a re-typed lookalike ARN.
    """

    built = watcher_template()
    alias_logical, alias = next(iter(built.find_resources("AWS::Lambda::Alias").items()))
    assert alias["Properties"]["Name"] == "live"

    grant = _scheduler_statement("InvokeCommitmentWatcherLiveAliasOnly")
    assert grant["Effect"] == "Allow"
    assert actions_of(grant) == {"lambda:InvokeFunction"}
    assert grant["Resource"] == {"Ref": alias_logical}


def test_the_scheduler_role_grants_no_unqualified_or_versioned_watcher_invoke() -> None:
    built = watcher_template()
    alias_logical = next(iter(built.find_resources("AWS::Lambda::Alias")))
    for item in _scheduler_role_statements():
        if item["Effect"] != "Allow" or "lambda:InvokeFunction" not in actions_of(item):
            continue
        # the alias resource, and only the alias resource -- never a joined function ARN, a
        # numeric version, or ``$LATEST``
        assert item["Resource"] == {"Ref": alias_logical}
        resource = json.dumps(item["Resource"])
        assert "$LATEST" not in resource
        assert not re.search(r"-watcher-development:\d", resource)


def test_the_scheduler_role_sends_to_the_dead_letter_queue_only() -> None:
    grant = _scheduler_statement("SendDroppedDueEventToDeadLetterQueueOnly")

    assert grant["Effect"] == "Allow"
    assert actions_of(grant) == {"sqs:SendMessage"}
    assert grant["Resource"] != "*"
    assert "WatcherDeadLetterQueue" in str(grant["Resource"])


def test_the_scheduler_role_uses_exactly_the_dlq_key_operations() -> None:
    grant = _scheduler_statement("UseDeadLetterQueueKeyForEncryptedSendOnly")

    assert grant["Effect"] == "Allow"
    assert actions_of(grant) == {"kms:GenerateDataKey", "kms:Decrypt"}
    assert grant["Resource"] != "*"
    assert "WatcherDeadLetterKey" in str(grant["Resource"])


def test_the_scheduler_role_has_no_other_service_capability() -> None:
    """ALLOW is exactly {lambda:InvokeFunction, sqs:SendMessage, kms:GenerateDataKey,
    kms:Decrypt} -- no DynamoDB, Bedrock, SES, AgentCore, Secrets Manager, broad Lambda
    invoke, general SQS, or general KMS."""

    allowed: set[str] = set()
    for item in _scheduler_role_statements():
        if item["Effect"] == "Allow":
            allowed |= actions_of(item)
    assert allowed == {
        "lambda:InvokeFunction",
        "sqs:SendMessage",
        "kms:GenerateDataKey",
        "kms:Decrypt",
    }
    for forbidden in ("dynamodb:", "bedrock", "ses", "sesv2", "secretsmanager:", "scheduler:"):
        assert not any(action.startswith(forbidden) for action in allowed)
    assert "kms:*" not in allowed and "sqs:*" not in allowed and "lambda:*" not in allowed


def test_the_scheduler_role_was_trust_only_before_and_now_carries_one_policy() -> None:
    assert len(_scheduler_role_statements()) == 3


# -- the worker side of I14: application worker Scheduler + PassRole -------------------


def test_the_worker_pass_role_condition_binds_the_scheduler_service() -> None:
    passrole = application_statement("PassSchedulerExecutionRoleOnly")

    assert actions_of(passrole) == {"iam:PassRole"}
    assert "chorus-scheduler-development" in json.dumps(passrole["Resource"])
    assert passrole["Condition"]["StringEquals"]["iam:PassedToService"] == "scheduler.amazonaws.com"


def test_the_api_role_holds_no_scheduler_authority_and_no_pass_role() -> None:
    template = (
        build_app(offline=True).synth().get_stack_by_name("AmbientChorusApplication").template
    )
    api_allows: set[str] = set()
    for logical_id, resource in template["Resources"].items():
        if resource["Type"] != POLICY_TYPE or not logical_id.startswith("ApiRole"):
            continue
        for item in resource["Properties"]["PolicyDocument"]["Statement"]:
            if item["Effect"] != "Allow":
                continue
            action = item["Action"]
            api_allows |= {action} if isinstance(action, str) else set(action)

    assert not any(a.startswith("scheduler:") for a in api_allows)
    assert "iam:PassRole" not in api_allows


# -- ADR-029: the watcher reads one clock and can never move it ------------------------


def test_the_watcher_may_strongly_read_the_demo_clock_item() -> None:
    """Without this the deployed watcher cannot perform step 4 of its own frozen order.

    Its Core deny is total, so the demo manifest is unreachable; and a process-local clock in a
    separate Lambda is a *different* clock from the one the API advanced. One read grant, on one
    exact literal partition, is what makes "exactly one clock" true across processes.
    """

    grant = statement("ReadDemoClockItemOnly")
    assert grant["Effect"] == "Allow"
    assert actions_of(grant) == {"dynamodb:GetItem"}
    assert grant["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"] == [
        DEMO_CLOCK_PARTITION
    ]


def test_the_watcher_holds_no_write_of_any_form_on_the_clock() -> None:
    """ADR-029 § 2: no ``PutItem``, ``UpdateItem``, ``DeleteItem``, or ``ConditionCheckItem``.

    A watcher that could move logical time could make its own early-firing check pass, so the
    absence is backed by the explicit deny rather than left to the grant's narrowness.
    """

    deny = statement("DenyNonCasePartitionWrites")
    assert deny["Effect"] == "Deny"
    assert (
        DEMO_CLOCK_PARTITION in deny["Condition"]["ForAnyValue:StringLike"]["dynamodb:LeadingKeys"]
    )

    for item in statements():
        if item["Effect"] != "Allow":
            continue
        condition = item.get("Condition", {}).get("ForAllValues:StringLike", {})
        keys = condition.get("dynamodb:LeadingKeys", [])
        if DEMO_CLOCK_PARTITION in keys:
            assert actions_of(item) == {"dynamodb:GetItem"}


def test_no_clock_grant_anywhere_is_a_wildcard() -> None:
    """ADR-029 § 2: there is no ``NS#*#CLOCK*``, and a policy containing one fails review."""

    for item in statements():
        for block in ("ForAllValues:StringLike", "ForAnyValue:StringLike"):
            keys = item.get("Condition", {}).get(block, {}).get("dynamodb:LeadingKeys", [])
            for key in keys if isinstance(keys, list) else [keys]:
                if "CLOCK" in key:
                    assert key == DEMO_CLOCK_PARTITION


def test_the_clock_partition_is_the_exact_deployed_literal() -> None:
    assert DEMO_CLOCK_PARTITION == "NS#DEMO#CLOCK"
