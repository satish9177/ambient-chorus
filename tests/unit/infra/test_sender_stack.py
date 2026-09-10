"""The sender's identity, asserted from the synthesized template.

The negative assertions matter more than the positive ones. "The sender can put an execution"
is a grant somebody meant to write; "the sender cannot write the proposal it is about to send"
is the property a future change would break silently -- and it is the entire Phase-8 guarantee.

These are the static half. A post-deploy canary proving an ``AccessDenied`` cannot exist before
Phase 11 deploys anything, but a policy that grants the wrong thing is visible in the template
today, and a template assertion fails in CI rather than in an account.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import pytest
from aws_cdk import App, Stack, assertions
from infra.cdk.app import build_app
from infra.cdk.stacks.sender import (
    EXECUTION_KEY_PREFIX,
    FORBIDDEN_WRITE_PREFIXES,
    SES_SEND_ACTION,
)

POLICY_TYPE = "AWS::IAM::Policy"
WRITE_ACTIONS = ("dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem")

ADDRESS_SHAPED = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
"""What a synthesized template must never contain anywhere (ADR-024 SS 5).

A template is a build artifact that gets read, diffed, and attached to a pull request, so an
email address in one is a value that has left Secrets Manager and entered version control. This
is the reason the SES grant is deliberately **not** narrowed by ``ses:Recipients``.
"""


@pytest.fixture(scope="module")
def app() -> App:
    return build_app(offline=True)


def _stack(app: App, name: str) -> Stack:
    stack = app.node.find_child(name)
    assert isinstance(stack, Stack)
    return stack


@pytest.fixture(scope="module")
def sender(app: App) -> assertions.Template:
    return assertions.Template.from_stack(_stack(app, "AmbientChorusSender"))


def statements(built: assertions.Template) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    for policy in built.find_resources(POLICY_TYPE).values():
        found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def statement(built: assertions.Template, sid: str) -> Mapping[str, Any]:
    return next(item for item in statements(built) if item.get("Sid") == sid)


def actions_of(item: Mapping[str, Any]) -> set[str]:
    action = item["Action"]
    return {action} if isinstance(action, str) else set(action)


def leading_keys(item: Mapping[str, Any]) -> list[str]:
    for _operator, body in item.get("Condition", {}).items():
        if "LeadingKeys" in str(body):
            keys = body["dynamodb:LeadingKeys"]
            return list(keys) if isinstance(keys, list) else [keys]
    return []


def targets(item: Mapping[str, Any], reference: str) -> bool:
    return reference in str(item.get("Resource"))


# ---------------------------------------------------------------------------------------
# The named ADR-024 assertions
# ---------------------------------------------------------------------------------------


def test_sender_cannot_write_any_action_or_view_prefix(sender: assertions.Template) -> None:
    """A sweep of **every** allow statement, not a check of one remembered statement.

    The positive half is that ``NS#*#EXECUTION#*`` is the only Shareable write the sender holds.
    The negative half is that no allow anywhere in this role reaches the partition holding the
    immutable proposal and the immutable approval -- because a sender that could put
    ``SK=ACTION`` could replace the proposal, recompute a matching ``preview_hash`` and
    ``proposal_hash``, render the replacement, and pass every check in the chain while
    describing a message nobody approved (T33).
    """

    swept = 0
    for item in statements(sender):
        if item["Effect"] != "Allow" or not targets(item, "ShareableTable"):
            continue
        writes = actions_of(item) & set(WRITE_ACTIONS)
        if not writes:
            continue
        swept += 1
        keys = leading_keys(item)
        assert keys, f"{item.get('Sid')} grants {sorted(writes)} with no LeadingKeys constraint"
        assert keys == [EXECUTION_KEY_PREFIX], (
            f"{item.get('Sid')} writes {keys}, not the execution prefix alone"
        )
    assert swept == 1, "expected exactly one Shareable write grant on the sender role"


def test_the_forbidden_prefixes_are_denied_by_for_any_value(
    sender: assertions.Template,
) -> None:
    """``ForAnyValue`` is deliberate, and it is what makes the deny whole-transaction.

    A transaction naming *any* proposal- or approval-partition item alongside legitimate
    execution items is refused entire, rather than permitted because most of its keys were
    acceptable.
    """

    deny = statement(sender, "DenyProposalApprovalViewAndCaseWrites")

    assert deny["Effect"] == "Deny"
    assert actions_of(deny) == set(WRITE_ACTIONS)
    assert set(leading_keys(deny)) == set(FORBIDDEN_WRITE_PREFIXES)
    assert any(operator.startswith("ForAnyValue") for operator in deny["Condition"])


# -- P1 (Phase 11 batch 4 repair): the sender reads the same clock, and cannot move it -------


def test_the_demo_clock_partition_is_explicitly_denied_to_the_sender(
    sender: assertions.Template,
) -> None:
    """No new *read* grant: ``ReadShareable`` is already unrestricted over the whole table, so
    it already reaches ``NS#DEMO#CLOCK``. What is new is the explicit write refusal."""

    deny = statement(sender, "DenyProposalApprovalViewAndCaseWrites")
    assert "NS#DEMO#CLOCK" in leading_keys(deny)


def test_the_sender_read_shareable_grant_is_unrestricted_and_needed_no_new_statement(
    sender: assertions.Template,
) -> None:
    read = statement(sender, "ReadShareable")
    assert read["Effect"] == "Allow"
    assert "Condition" not in read, "an unrestricted read must carry no LeadingKeys narrowing"


def test_no_clock_grant_on_the_sender_is_a_wildcard(sender: assertions.Template) -> None:
    """ADR-029 § 2: there is no ``NS#*#CLOCK*``, and a policy containing one fails review."""

    for item in statements(sender):
        for block in ("ForAllValues:StringLike", "ForAnyValue:StringLike"):
            keys = item.get("Condition", {}).get(block, {}).get("dynamodb:LeadingKeys", [])
            for key in keys if isinstance(keys, list) else [keys]:
                if "CLOCK" in key:
                    assert key == "NS#DEMO#CLOCK"


def test_the_sender_holds_no_update_item_and_no_blanket_transaction_action(
    sender: assertions.Template,
) -> None:
    """Every execution write is a conditional whole-record put.

    ``dynamodb:TransactWriteItems`` is not granted because AWS authorizes a transaction through
    the permission each *participant* needs, so the blanket action would be a permission this
    role does not need and a place for a future participant to hide.
    """

    for item in statements(sender):
        if item["Effect"] != "Allow":
            continue
        granted = actions_of(item)
        assert "dynamodb:UpdateItem" not in granted
        assert "dynamodb:TransactWriteItems" not in granted
        assert "dynamodb:DeleteItem" not in granted


def test_the_sender_has_no_allow_reaching_the_core_table(sender: assertions.Template) -> None:
    """Core is denied in total, not merely ungranted.

    The sender resolves its recipient from an allowlisted registry in configuration and reaches
    the fence only through the compiler's typed operation, so it has no reason to read a case, a
    fact, a mandate, or even the fence row.
    """

    for item in statements(sender):
        if item["Effect"] == "Allow":
            assert not targets(item, "CoreTable"), f"{item.get('Sid')} reaches Core"

    deny = statement(sender, "DenyAllCoreAccess")
    assert deny["Effect"] == "Deny"
    assert actions_of(deny) == {"dynamodb:*"}


def test_the_sender_cannot_invoke_a_model_or_a_scheduler(sender: assertions.Template) -> None:
    """The sender transmits an already-approved message. It never asks anything what to say."""

    model = statement(sender, "DenySenderModelAccess")
    scheduler = statement(sender, "DenySenderScheduler")

    assert model["Effect"] == "Deny"
    assert "bedrock:*" in actions_of(model)
    assert "bedrock-agentcore:*" in actions_of(model)
    assert scheduler["Effect"] == "Deny"
    assert "scheduler:*" in actions_of(scheduler)

    for item in statements(sender):
        if item["Effect"] != "Allow":
            continue
        for action in actions_of(item):
            assert not action.startswith(("bedrock", "scheduler"))


def test_the_sender_cannot_read_or_write_either_evidence_bucket(
    sender: assertions.Template,
) -> None:
    """Even compromised rendering cannot fetch private details."""

    deny = statement(sender, "DenyEvidenceObjects")

    assert deny["Effect"] == "Deny"
    assert actions_of(deny) == {"s3:*"}
    for item in statements(sender):
        if item["Effect"] != "Allow":
            continue
        for action in actions_of(item):
            assert not action.startswith("s3:")


def test_the_ses_grant_uses_the_real_action_prefix(sender: assertions.Template) -> None:
    """``ses:SendEmail``, not ``sesv2:``.

    SES v2's ``SendEmail`` API authorizes under the ``ses:`` action name. The ``sesv2:`` entries
    in the existing deny lists name no real IAM action and are inert, which is harmless in a
    deny and would be a **silent hole** in an allow.
    """

    grant = statement(sender, "SendThroughConfiguredIdentityOnly")

    assert grant["Effect"] == "Allow"
    assert actions_of(grant) == {SES_SEND_ACTION}
    assert SES_SEND_ACTION == "ses:SendEmail"
    for action in actions_of(grant):
        assert not action.startswith("sesv2:")


def test_the_ses_grant_is_not_narrowed_by_recipient_or_from_address(
    sender: assertions.Template,
) -> None:
    """The omission is a decision rather than an oversight.

    Those condition values are email addresses, and the single-recipient rule is enforceable in
    code against a registry the sender already has to read -- so the rule lives there and no
    address lives here.
    """

    grant = statement(sender, "SendThroughConfiguredIdentityOnly")

    conditions = str(grant.get("Condition", {}))
    assert "ses:Recipients" not in conditions
    assert "ses:FromAddress" not in conditions


def test_synthesized_template_contains_no_address_shaped_string(app: App) -> None:
    """Swept over **every** stack, not just the sender's.

    The claim is about the build artifact as a whole: an address anywhere in it has left
    Secrets Manager and entered version control, whichever template happened to carry it.
    """

    for name in (
        "AmbientChorusFoundation",
        "AmbientChorusData",
        "AmbientChorusAgents",
        "AmbientChorusCompiler",
        "AmbientChorusApplication",
        "AmbientChorusSender",
    ):
        rendered = json.dumps(assertions.Template.from_stack(_stack(app, name)).to_json())
        matches = [
            found
            for found in ADDRESS_SHAPED.findall(rendered)
            # AWS service principals are ``lambda.amazonaws.com``-shaped, not address-shaped;
            # they contain no ``@`` and so cannot match. Anything that does match is an address.
            if "@" in found
        ]
        assert matches == [], f"{name} contains address-shaped strings: {matches}"


def test_the_sender_writes_only_its_own_log_group(sender: assertions.Template) -> None:
    grant = statement(sender, "WriteOwnSenderLogs")

    assert grant["Effect"] == "Allow"
    assert actions_of(grant) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams",
    }


def test_the_configuration_set_is_defined_so_events_can_reconcile(
    sender: assertions.Template,
) -> None:
    """Defined and never sent through in Phase 8.

    It is what makes ``SEND_UNKNOWN -> SENT`` on positive evidence possible at all: an event
    from a *different* configuration set is refused by the reconciliation command.
    """

    sender.resource_count_is("AWS::SES::ConfigurationSet", 1)


def test_the_application_may_write_the_execution_prefix(app: App) -> None:
    """Not a widening: the application already created and moved the execution (ADR-024 SS 4).

    Two principals hold ``PutItem`` over one prefix, and that is the one Phase-8 boundary IAM
    does not draw. What keeps them apart is the state machine, which is asserted over the
    transitions rather than over a policy -- see the domain state tests.
    """

    from infra.cdk.stacks.application import APPLICATION_SHAREABLE_PREFIXES

    template = assertions.Template.from_stack(_stack(app, "AmbientChorusApplication"))
    grant = statement(template, "WriteActionAndCasePrefixesOnly")

    assert EXECUTION_KEY_PREFIX in APPLICATION_SHAREABLE_PREFIXES
    assert EXECUTION_KEY_PREFIX in leading_keys(grant)
