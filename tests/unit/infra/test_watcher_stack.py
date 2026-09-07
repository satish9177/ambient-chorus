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
from functools import cache
from typing import Any

import pytest
from aws_cdk import App, assertions
from infra.cdk.app import build_app
from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks import ChorusDataStack, ChorusWatcherStack, WatcherBuckets, WatcherTables
from infra.cdk.stacks.watcher import (
    CASE_KEY_PREFIX,
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


def test_no_lambda_function_is_created() -> None:
    """Phase 9 owns the identity and deploys nothing. The function is Phase 11's."""

    assert watcher_template().find_resources("AWS::Lambda::Function") == {}


# ------------------------------------------------------------------------------------------
# The application's narrowed scheduler grant
# ------------------------------------------------------------------------------------------


@cache
def application_statements() -> list[dict[str, Any]]:
    template = build_app().synth().get_stack_by_name("AmbientChorusApplication").template
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

    template = build_app().synth().get_stack_by_name("AmbientChorusSender").template
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
