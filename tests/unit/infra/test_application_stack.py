"""The application principal's boundary, and the one assertion ADR-022 § 7 demanded by name.

The application is the broadest principal in the system, so its boundary has to be *read from
the synthesized policy* rather than argued from repository behaviour. ADR-019 established the
pattern for the compiler; ADR-022 § 7 requires it here, in one sentence:

    the application principal **must not** hold ``PutItem``, ``UpdateItem``, or ``DeleteItem``
    on the ``NS#*#VIEW#*`` or ``NS#*#VIEW_CURRENT#*`` prefixes -- **a condition check must never
    become a write grant**.

That is what :func:`test_application_cannot_write_any_view_prefix` proves, by sweeping every
allow statement in the template rather than by checking the one statement somebody remembered
to write. Do not repeat the ADR-019 mistake of relying only on repository behaviour: a
repository can be changed by anyone, and a policy is what AWS actually enforces.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from aws_cdk import App, assertions
from infra.cdk.app import build_app
from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks import (
    ApplicationBuckets,
    ApplicationTables,
    ChorusApplicationStack,
    ChorusDataStack,
)
from infra.cdk.stacks.application import (
    APPLICATION_SHAREABLE_PREFIXES,
    CONDITION_CHECK_ACTION,
    DENIED_MODEL_ACTIONS,
    DENIED_SEND_ACTIONS,
    DENIED_VIEW_WRITE_ACTIONS,
    VIEW_KEY_PREFIXES,
)

POLICY_TYPE = "AWS::IAM::Policy"

WRITE_ACTIONS = ("dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem")


def _template(**kwargs: object) -> assertions.Template:
    app = App()
    config = CdkBuildConfig()
    data = ChorusDataStack(app, "TestData", config=config)
    stack = ChorusApplicationStack(
        app,
        "TestApplication",
        config=config,
        tables=ApplicationTables(
            core=data.core_table, shareable=data.shareable_table, audit=data.audit_table
        ),
        buckets=ApplicationBuckets(
            private=data.private_evidence_bucket,
            export=data.export_evidence_bucket,
            private_key=data.private_evidence_key,
            export_key=data.export_evidence_key,
        ),
        **kwargs,  # type: ignore[arg-type]
    )
    return assertions.Template.from_stack(stack)


def statements(built: assertions.Template | None = None) -> list[Mapping[str, Any]]:
    policies = (built or _template()).find_resources(POLICY_TYPE)
    found: list[Mapping[str, Any]] = []
    for policy in policies.values():
        found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def statement(sid: str) -> Mapping[str, Any]:
    return next(item for item in statements() if item.get("Sid") == sid)


def actions_of(item: Mapping[str, Any]) -> set[str]:
    action = item["Action"]
    return {action} if isinstance(action, str) else set(action)


SHAREABLE_REFERENCE = "ShareableTable"
"""How the synthesized template names the Shareable table.

The resource is a CloudFormation cross-stack reference rather than a literal ARN, so the sweep
below identifies the table by the export name CDK generates for it. Matching on the *table* is
what makes the sweep mean what it says: the application writes Core broadly and by design, and
demanding a ``LeadingKeys`` constraint there would be asserting something the trust matrix does
not claim.
"""


def targets_shareable(item: Mapping[str, Any]) -> bool:
    return SHAREABLE_REFERENCE in str(item.get("Resource"))


def leading_keys(item: Mapping[str, Any]) -> list[str]:
    conditions = item.get("Condition", {})
    for operator, body in conditions.items():
        if "LeadingKeys" in str(body):
            keys = body["dynamodb:LeadingKeys"]
            assert operator.startswith(("ForAllValues", "ForAnyValue"))
            return list(keys) if isinstance(keys, list) else [keys]
    return []


# ---------------------------------------------------------------------------------------
# The assertion ADR-022 § 7 named
# ---------------------------------------------------------------------------------------


def test_application_cannot_write_any_view_prefix() -> None:
    """A sweep of **every** allow statement, not a check of one remembered statement.

    This is the named Phase-7 security test. A grant that reached a compiler-owned view prefix
    would let the application mint the very view that authorizes its own proposal, which is the
    single thing the key-prefix split exists to prevent.

    The sweep is scoped to the *Shareable table* rather than to every write anywhere, because
    that is where the claim lives. The application writes Core broadly and by design -- cases,
    facts, mandates, operations -- and requiring a ``LeadingKeys`` constraint there would assert
    something the trust matrix does not claim and quietly turn this test into a different one.
    Two conditions are checked for every Shareable write grant: it must be key-scoped at all,
    and none of the prefixes it names may be a view prefix.
    """

    swept = 0
    for item in statements():
        if item["Effect"] != "Allow" or not targets_shareable(item):
            continue
        writes = actions_of(item) & set(WRITE_ACTIONS)
        if not writes:
            continue
        swept += 1
        keys = leading_keys(item)
        assert keys, f"{item.get('Sid')} grants {sorted(writes)} with no LeadingKeys constraint"
        for key in keys:
            assert key not in VIEW_KEY_PREFIXES, (
                f"{item.get('Sid')} grants {sorted(writes)} on the compiler-owned prefix {key}"
            )
    assert swept, "the sweep found no Shareable write grant at all, so it proved nothing"


def test_no_allow_statement_anywhere_pairs_a_write_with_a_view_prefix() -> None:
    """The table-independent half, so a future grant on a differently named resource is caught.

    The sweep above asks "is every Shareable write key-scoped away from the view prefixes";
    this one asks the complementary question -- "does any allow at all put a write and a view
    prefix in the same statement" -- so neither a renamed reference nor a second table can slip
    the guarantee.
    """

    for item in statements():
        if item["Effect"] != "Allow":
            continue
        if not actions_of(item) & set(WRITE_ACTIONS):
            continue
        assert not (set(leading_keys(item)) & set(VIEW_KEY_PREFIXES)), item.get("Sid")


@pytest.mark.parametrize("action", sorted(DENIED_VIEW_WRITE_ACTIONS))
def test_view_partition_writes_are_denied_outright(action: str) -> None:
    """Defence in depth beside the positive split, and an explicit deny cannot be overridden.

    ``ForAnyValue`` is deliberate: a transaction naming *any* view-partition item alongside
    legitimate action items is refused whole, rather than permitted because most of its keys
    were acceptable.
    """

    denied = statement("DenyViewPartitionWrites")

    assert denied["Effect"] == "Deny"
    assert action in denied["Action"]
    assert set(leading_keys(denied)) == set(VIEW_KEY_PREFIXES)


def test_the_application_holds_condition_check_on_the_view_prefixes() -> None:
    """The positive half: it *may* condition on a view, which is what makes ADR-022 § 7 work.

    DynamoDB authorizes a transaction through the permission each participant needs, so a
    ``ConditionCheck`` participant requires exactly this action -- and that is the whole reason
    a read-only transactional authority is expressible instead of having to grant a write.
    """

    allowed = statement("ConditionCheckCurrentViewPointer")

    assert allowed["Effect"] == "Allow"
    assert actions_of(allowed) == {CONDITION_CHECK_ACTION}
    assert set(leading_keys(allowed)) == set(VIEW_KEY_PREFIXES)


def test_the_view_condition_check_is_narrowed_to_transactions() -> None:
    """It is staged only inside the proposal apply, so it never needs to exist standalone."""

    conditions = statement("ConditionCheckCurrentViewPointer")["Condition"]

    assert conditions["StringEquals"]["dynamodb:EnclosingOperation"] == "TransactWriteItems"


# ---------------------------------------------------------------------------------------
# The rest of the boundary
# ---------------------------------------------------------------------------------------


def test_shareable_writes_reach_only_the_action_and_case_prefixes() -> None:
    allowed = statement("WriteActionAndCasePrefixesOnly")

    assert set(leading_keys(allowed)) == set(APPLICATION_SHAREABLE_PREFIXES)
    assert set(WRITE_ACTIONS) <= actions_of(allowed)


def test_the_split_is_complementary_with_the_compilers() -> None:
    """Neither principal can reach the other's prefixes. That is the whole guarantee."""

    from infra.cdk.stacks.compiler import VIEW_KEY_PREFIXES as COMPILER_PREFIXES

    assert set(APPLICATION_SHAREABLE_PREFIXES).isdisjoint(COMPILER_PREFIXES)
    assert set(VIEW_KEY_PREFIXES) == set(COMPILER_PREFIXES)


@pytest.mark.parametrize("action", sorted(DENIED_SEND_ACTIONS))
def test_the_application_can_never_send_email(action: str) -> None:
    """Its breadth of private access is precisely why it never receives a send permission."""

    denied = statement("DenyApplicationSend")

    assert denied["Effect"] == "Deny"
    assert action in denied["Action"]


@pytest.mark.parametrize("action", sorted(DENIED_MODEL_ACTIONS))
def test_the_application_can_never_call_a_model_directly(action: str) -> None:
    """Agents are invoked through runtimes, which pin the prompt and the output schema.

    A direct model grant would let application code send an unreviewed prompt to the same
    model, which is the one path around every artifact-level control the runtimes impose.
    """

    denied = statement("DenyDirectModelAccess")

    assert denied["Effect"] == "Deny"
    assert action in denied["Action"]


def test_agent_invocation_is_scoped_to_named_runtimes_when_supplied() -> None:
    built = _template(
        agent_runtime_arns=(
            "arn:aws:bedrock-agentcore:us-east-1:1:runtime/chorus-monitor",
            "arn:aws:bedrock-agentcore:us-east-1:1:runtime/chorus-action",
        )
    )
    found = [
        item for item in statements(built) if item.get("Sid") == "InvokeNamedAgentRuntimesOnly"
    ]

    assert found
    assert found[0]["Resource"] != "*"
    assert actions_of(found[0]) == {"bedrock-agentcore:InvokeAgentRuntime"}


def test_no_allow_statement_grants_a_wildcard_resource() -> None:
    for item in statements():
        if item["Effect"] != "Allow":
            continue
        assert item.get("Resource") != "*", item.get("Sid")


def test_the_application_stack_is_part_of_the_synthesized_app() -> None:
    assembly = build_app().synth()

    assert "AmbientChorusApplication" in [stack.stack_name for stack in assembly.stacks]
