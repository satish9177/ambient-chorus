"""Defence-in-depth: no forbidden authority can be regained through a wildcard.

Codex found the synthesized policies satisfying the intended Phase-7 boundary while some of the
negative assertions were written as "this exact action is not granted here". Those hold today
and would keep holding if a future statement granted ``dynamodb:*`` or ``Action: "*"`` beside
them -- an allow that *contains* the forbidden action without ever naming it.

So these tests assert on the shape a grant can take rather than only on the strings it happens
to use:

* no ``Action: "*"`` anywhere, on either principal;
* no service-level wildcard -- ``dynamodb:*``, ``s3:*``, ``ses:*``, ``bedrock:*``,
  ``secretsmanager:*``, ``scheduler:*``;
* no ``Resource: "*"`` paired with a data or send action;
* no write action reaching a compiler-owned view prefix, wildcard resource included.

This is test coverage only. The IAM architecture is unchanged, and nothing here is a new
boundary -- it is the existing boundary asserted in a way a wildcard cannot slip past.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from infra.cdk.stacks.application import CONDITION_CHECK_ACTION, VIEW_KEY_PREFIXES

from tests.unit.infra import test_action_stack, test_application_stack

FORBIDDEN_SERVICE_WILDCARDS = (
    "dynamodb:*",
    "s3:*",
    "ses:*",
    "sesv2:*",
    "bedrock:*",
    "bedrock-agentcore:*",
    "secretsmanager:*",
    "scheduler:*",
    "kms:*",
    "iam:*",
    "sts:*",
)

DATA_AND_SEND_ACTION_PREFIXES = (
    "dynamodb:",
    "s3:",
    "ses:",
    "sesv2:",
    "secretsmanager:",
    "scheduler:",
)


def _application_statements() -> list[Mapping[str, Any]]:
    return test_application_stack.statements()


def _action_statements() -> list[Mapping[str, Any]]:
    return test_action_stack.statements()


def _allows(items: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [item for item in items if item.get("Effect") == "Allow"]


def _actions(item: Mapping[str, Any]) -> set[str]:
    action = item.get("Action", ())
    if isinstance(action, str):
        return {action}
    return set(action)


def _resources(item: Mapping[str, Any]) -> str:
    return str(item.get("Resource"))


PRINCIPALS = pytest.mark.parametrize(
    "collect",
    [
        pytest.param(_application_statements, id="application"),
        pytest.param(_action_statements, id="action-runtime"),
    ],
)


# ---------------------------------------------------------------------------------------
# Wildcards
# ---------------------------------------------------------------------------------------


@PRINCIPALS
def test_no_allow_statement_grants_every_action(collect: object) -> None:
    """``Action: "*"`` grants every forbidden action without naming one of them."""

    for item in _allows(collect()):  # type: ignore[operator]
        assert "*" not in _actions(item), item.get("Sid")


@PRINCIPALS
def test_no_allow_statement_grants_a_service_wildcard(collect: object) -> None:
    """``dynamodb:*`` is how a table write reappears beside a policy that denies one."""

    for item in _allows(collect()):  # type: ignore[operator]
        overlap = _actions(item) & set(FORBIDDEN_SERVICE_WILDCARDS)
        assert not overlap, (item.get("Sid"), overlap)


@PRINCIPALS
def test_no_allow_statement_pairs_a_data_action_with_a_wildcard_resource(
    collect: object,
) -> None:
    """A wildcard *resource* on a data or send action is the same hole from the other side."""

    for item in _allows(collect()):  # type: ignore[operator]
        if _resources(item) != "*":
            continue
        for action in _actions(item):
            assert not action.startswith(DATA_AND_SEND_ACTION_PREFIXES), (
                item.get("Sid"),
                action,
            )


# ---------------------------------------------------------------------------------------
# The Action runtime keeps no data or send authority at all
# ---------------------------------------------------------------------------------------


def test_the_action_runtime_holds_no_data_or_send_allow_of_any_shape() -> None:
    """ADR-003's static boundary, asserted by action *prefix* rather than by exact string."""

    for item in _allows(_action_statements()):
        for action in _actions(item):
            assert not action.startswith(DATA_AND_SEND_ACTION_PREFIXES), (
                item.get("Sid"),
                action,
            )


RESOURCELESS_ACTION_SERVICES = ("xray:",)
"""The one service whose actions AWS itself defines with no resource to scope to.

``xray:PutTraceSegments`` and ``xray:PutTelemetryRecords`` accept ``Resource: "*"`` and nothing
narrower, so a wildcard there is the API's shape rather than a widened grant. Naming the
exemption explicitly is what keeps it from becoming a hole anybody can widen: every *other*
wildcard resource on this principal still fails.
"""


def test_the_action_runtime_holds_a_wildcard_resource_only_for_tracing() -> None:
    """It reads its own model and writes its own log group. Nothing else needs ``*``."""

    for item in _allows(_action_statements()):
        if _resources(item) != "*":
            continue
        assert all(action.startswith(RESOURCELESS_ACTION_SERVICES) for action in _actions(item)), (
            item.get("Sid"),
            _actions(item),
        )


# ---------------------------------------------------------------------------------------
# The view prefixes stay unwritable, wildcards included
# ---------------------------------------------------------------------------------------


def test_no_application_allow_reaches_a_view_prefix_with_a_write_of_any_shape() -> None:
    """ADR-022 § 7: a condition check must never become a write grant.

    Broadened from the existing sweep in two directions -- a statement whose action set
    *contains* a wildcard, and a statement whose resource is ``*`` and therefore reaches the
    view prefixes without naming them.
    """

    for item in _allows(_application_statements()):
        actions = _actions(item)
        writes = {
            action
            for action in actions
            if action.startswith("dynamodb:")
            and (
                action.endswith(("PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem"))
                or action == "dynamodb:*"
            )
        }
        if not writes:
            continue
        rendered = str(item)
        for prefix in VIEW_KEY_PREFIXES:
            assert prefix not in rendered, (item.get("Sid"), prefix)
        assert _resources(item) != "*", item.get("Sid")


def test_the_application_view_grant_is_condition_check_only() -> None:
    """The one statement that touches a view prefix grants exactly one action."""

    touching = [
        item
        for item in _allows(_application_statements())
        if any(prefix in str(item) for prefix in VIEW_KEY_PREFIXES)
    ]

    assert touching, "the ConditionCheck grant must exist to be constrained"
    for item in touching:
        assert _actions(item) == {CONDITION_CHECK_ACTION}, item.get("Sid")


def test_no_application_allow_grants_a_broad_view_current_write() -> None:
    """``VIEW_CURRENT`` is compiler-owned by IAM and not by convention."""

    for item in _allows(_application_statements()):
        if "VIEW_CURRENT" not in str(item):
            continue
        assert _actions(item) == {CONDITION_CHECK_ACTION}, item.get("Sid")
