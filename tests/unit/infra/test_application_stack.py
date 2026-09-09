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

import json
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
    DEMO_CLOCK_PARTITION,
    DENIED_CLOCK_WRITE_ACTIONS,
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


# =====================================================================================
# I5 -- the request path and the durable worker are two principals, not one
# =====================================================================================

ROLE_TYPE = "AWS::IAM::Role"

WORKER_FN = "arn:aws:lambda:us-east-1:111122223333:function:chorus-worker-demo"
COMPILER_FN = "arn:aws:lambda:us-east-1:111122223333:function:chorus-compiler-demo"
SENDER_FN = "arn:aws:lambda:us-east-1:111122223333:function:chorus-sender-demo"
RUNTIME_ARNS = (
    "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/chorus_monitor-abc",
    "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/chorus_investigator-def",
    "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/chorus_action-ghi",
)
DEMO_SECRET = "arn:aws:secretsmanager:us-east-1:111122223333:secret:chorus-demo-access-AbCdEf"
CURSOR_SECRET = "arn:aws:secretsmanager:us-east-1:111122223333:secret:chorus-cursor-signing-QrS"
DEST_SECRET = "arn:aws:secretsmanager:us-east-1:111122223333:secret:chorus-demo-destination-XyZ"
SCHEDULER_ROLE_ARN = "arn:aws:iam::111122223333:role/chorus-scheduler-demo"
WATCHER_ALIAS = "arn:aws:lambda:us-east-1:111122223333:function:chorus-commitment-watcher-demo:live"


def _split_template() -> assertions.Template:
    return _template(
        agent_runtime_arns=RUNTIME_ARNS,
        scheduler_group_name="chorus-demo",
        scheduler_role_arn=SCHEDULER_ROLE_ARN,
        worker_function_arn=WORKER_FN,
        compiler_function_arn=COMPILER_FN,
        sender_function_arn=SENDER_FN,
        watcher_alias_arn=WATCHER_ALIAS,
        demo_access_secret_arn=DEMO_SECRET,
        cursor_signing_secret_arn=CURSOR_SECRET,
        destination_registry_secret_arn=DEST_SECRET,
    )


def _role_statements(built: assertions.Template, logical_prefix: str) -> list[Mapping[str, Any]]:
    """Every policy statement attached to the role whose default policy logical id starts with
    ``logical_prefix`` (``ApiRole`` / ``WorkerRole``)."""

    found: list[Mapping[str, Any]] = []
    for logical_id, policy in built.find_resources(POLICY_TYPE).items():
        if logical_id.startswith(logical_prefix):
            found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def _api(built: assertions.Template) -> list[Mapping[str, Any]]:
    return _role_statements(built, "ApiRole")


def _worker(built: assertions.Template) -> list[Mapping[str, Any]]:
    return _role_statements(built, "WorkerRole")


def _find(items: list[Mapping[str, Any]], sid: str) -> Mapping[str, Any]:
    return next(item for item in items if item.get("Sid") == sid)


def _allowed_actions(items: list[Mapping[str, Any]]) -> set[str]:
    out: set[str] = set()
    for item in items:
        if item["Effect"] == "Allow":
            out |= actions_of(item)
    return out


def test_the_stack_synthesizes_exactly_two_roles_named_api_and_worker() -> None:
    built = _split_template()
    built.resource_count_is(ROLE_TYPE, 2)
    names = {
        str(role["Properties"]["RoleName"]) for role in built.find_resources(ROLE_TYPE).values()
    }
    assert names == {"chorus-api-development", "chorus-worker-development"}


# -- API: ALLOW the request path's capabilities ----------------------------------------


def test_api_holds_the_shared_private_zone_boundary() -> None:
    api = _api(_split_template())
    allowed = _allowed_actions(api)
    assert {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:ConditionCheckItem"} <= allowed
    assert {"s3:GetObject", "s3:PutObject"} <= allowed
    assert {"kms:Decrypt", "kms:GenerateDataKey"} <= allowed


def test_api_writes_the_action_and_case_prefixes_but_not_outbound_message() -> None:
    grant = _find(_api(_split_template()), "WriteApiActionAndCasePrefixesOnly")
    keys = set(leading_keys(grant))
    assert keys == {
        "NS#*#ACTION#*",
        "NS#*#ACTION_CURRENT#*",
        "NS#*#EXECUTION#*",
        "NS#*#CASE#*",
    }
    assert "NS#*#OUTBOUND_MESSAGE#*" not in keys


def test_api_invokes_the_worker_and_the_compiler_and_nothing_else() -> None:
    grant = _find(_api(_split_template()), "InvokeOperationWorkerAndCompilerOnly")
    assert actions_of(grant) == {"lambda:InvokeFunction"}
    assert set(grant["Resource"]) == {WORKER_FN, COMPILER_FN}
    assert SENDER_FN not in grant["Resource"]


def test_api_reads_the_demo_access_token_secret_only() -> None:
    grant = _find(_api(_split_template()), "ReadDemoAccessTokenSecretOnly")
    assert grant["Effect"] == "Allow"
    assert actions_of(grant) == {"secretsmanager:GetSecretValue"}
    assert grant["Resource"] == DEMO_SECRET


def test_api_reads_the_cursor_signing_key_secret_only() -> None:
    """P2-5: its own exact secret identity -- a separate statement from the demo access token
    grant above, naming a different ARN, so widening one can never silently widen the other."""

    grant = _find(_api(_split_template()), "ReadCursorSigningKeySecretOnly")
    assert grant["Effect"] == "Allow"
    assert actions_of(grant) == {"secretsmanager:GetSecretValue"}
    assert grant["Resource"] == CURSOR_SECRET
    assert grant["Resource"] != DEMO_SECRET


# -- API: DENY / no grant of the worker-only capabilities ------------------------------


def test_api_has_no_agentcore_invocation_grant_and_denies_it() -> None:
    api = _api(_split_template())
    assert "bedrock-agentcore:InvokeAgentRuntime" not in _allowed_actions(api)
    deny = _find(api, "DenyApiAgentRuntimeInvocation")
    assert deny["Effect"] == "Deny"
    assert "bedrock-agentcore:InvokeAgentRuntime" in actions_of(deny)


def test_api_has_no_scheduler_grant_and_denies_scheduler_creation() -> None:
    api = _api(_split_template())
    assert not any(a.startswith("scheduler:") for a in _allowed_actions(api))
    deny = _find(api, "DenyApiSchedulerAuthority")
    assert deny["Effect"] == "Deny"
    assert {"scheduler:CreateSchedule", "scheduler:*"} <= actions_of(deny)


def test_api_has_no_pass_role_grant_and_denies_it() -> None:
    api = _api(_split_template())
    assert "iam:PassRole" not in _allowed_actions(api)
    deny = _find(api, "DenyApiPassRole")
    assert deny["Effect"] == "Deny"
    assert actions_of(deny) == {"iam:PassRole"}


def test_api_cannot_read_the_destination_registry_secret() -> None:
    deny = _find(_api(_split_template()), "DenyApiDestinationRegistrySecret")
    assert deny["Effect"] == "Deny"
    assert actions_of(deny) == {"secretsmanager:GetSecretValue"}
    assert deny["Resource"] == DEST_SECRET


# -- Worker: ALLOW the exact worker capabilities --------------------------------------


def test_worker_invokes_the_three_named_runtimes() -> None:
    grant = _find(_worker(_split_template()), "InvokeNamedAgentRuntimesOnly")
    assert actions_of(grant) == {"bedrock-agentcore:InvokeAgentRuntime"}
    assert set(grant["Resource"]) == set(RUNTIME_ARNS)


def test_worker_invokes_the_compiler_and_the_sender() -> None:
    grant = _find(_worker(_split_template()), "InvokeCompilerAndSenderOnly")
    assert actions_of(grant) == {"lambda:InvokeFunction"}
    assert set(grant["Resource"]) == {COMPILER_FN, SENDER_FN}
    assert WORKER_FN not in grant["Resource"]


def test_worker_writes_the_outbound_message_locator_prefix() -> None:
    grant = _find(_worker(_split_template()), "WriteActionAndCasePrefixesOnly")
    assert "NS#*#OUTBOUND_MESSAGE#*" in set(leading_keys(grant))


def test_worker_creates_and_gets_schedules_and_passes_the_scheduler_role_alone() -> None:
    worker = _worker(_split_template())
    create = _find(worker, "CreateAndGetCommitmentSchedulesOnly")
    assert actions_of(create) == {"scheduler:CreateSchedule", "scheduler:GetSchedule"}
    assert "chorus-demo/*" in str(create["Resource"])

    passrole = _find(worker, "PassSchedulerExecutionRoleOnly")
    assert actions_of(passrole) == {"iam:PassRole"}
    assert passrole["Resource"] == SCHEDULER_ROLE_ARN
    assert passrole["Condition"]["StringEquals"]["iam:PassedToService"] == "scheduler.amazonaws.com"

    deny = _find(worker, "DenyScheduleDeletionAndUpdate")
    assert actions_of(deny) == {"scheduler:DeleteSchedule", "scheduler:UpdateSchedule"}


# -- Worker: DENY / no grant of the demo-token secret --------------------------------


def test_worker_reads_no_secret_and_denies_the_demo_token_read() -> None:
    worker = _worker(_split_template())
    assert not any(a.startswith("secretsmanager:") for a in _allowed_actions(worker))
    deny = _find(worker, "DenyWorkerSecretReads")
    assert deny["Effect"] == "Deny"
    assert actions_of(deny) == {"secretsmanager:GetSecretValue"}
    assert deny["Resource"] == "*"


# -- Both: the earlier negative boundaries are retained on each role -----------------


@pytest.mark.parametrize("collect", [_api, _worker], ids=["api", "worker"])
def test_each_role_retains_the_view_send_and_model_denies(
    collect: object,
) -> None:
    items = collect(_split_template())  # type: ignore[operator]
    view = _find(items, "DenyViewPartitionWrites")
    assert view["Effect"] == "Deny"
    assert set(leading_keys(view)) == set(VIEW_KEY_PREFIXES)

    send = _find(items, "DenyApplicationSend")
    assert send["Effect"] == "Deny"
    assert "ses:SendEmail" in actions_of(send)

    model = _find(items, "DenyDirectModelAccess")
    assert model["Effect"] == "Deny"
    assert "bedrock:InvokeModel" in actions_of(model)


@pytest.mark.parametrize("collect", [_api, _worker], ids=["api", "worker"])
def test_neither_role_can_write_a_view_prefix(collect: object) -> None:
    for item in collect(_split_template()):  # type: ignore[operator]
        if item["Effect"] != "Allow":
            continue
        if not actions_of(item) & set(WRITE_ACTIONS):
            continue
        assert not (set(leading_keys(item)) & set(VIEW_KEY_PREFIXES)), item.get("Sid")


def test_offline_synth_grants_no_lambda_invoke_or_secret_read() -> None:
    """With no function or secret ARNs supplied (the state ``app.py`` synthesizes today),
    neither conditional grant appears -- the split is asserted, deployed by nobody."""

    built = _template()
    for items in (_api(built), _worker(built)):
        allowed = _allowed_actions(items)
        assert "lambda:InvokeFunction" not in allowed
        assert "secretsmanager:GetSecretValue" not in allowed


# -- Phase 11 batch 4: the API -> watcher resolution ----------------------------------


def test_the_api_may_invoke_the_watcher_live_alias_and_only_that() -> None:
    """The exact resolution of the ``POST /v1/demo/clock/advance`` contradiction.

    That endpoint's frozen response carries ``watcher_outcome`` and ``commitment_status``, so it
    promises the watcher's *decision* rather than that one was scheduled. Serving that promise
    means one synchronous invocation, which needs one grant -- and this asserts it is exactly
    one, on exactly the ``:live`` alias.
    """

    grant = _find(_api(_split_template()), "InvokeCommitmentWatcherLiveAliasOnly")
    assert grant["Effect"] == "Allow"
    assert actions_of(grant) == {"lambda:InvokeFunction"}
    assert grant["Resource"] == WATCHER_ALIAS


def test_the_watcher_grant_names_the_alias_and_never_the_bare_function() -> None:
    """Rollback repoints the alias at a published version; no policy statement changes.

    An unqualified function ARN or a numeric version would both break that, so the assertion is
    on the literal rather than on "it mentions the watcher somewhere".
    """

    resources: list[str] = []
    for item in _api(_split_template()):
        if item["Effect"] != "Allow" or "lambda:InvokeFunction" not in actions_of(item):
            continue
        resource = item["Resource"]
        resources.extend(resource if isinstance(resource, list) else [resource])

    watcher = [value for value in resources if "commitment-watcher" in value]
    assert watcher == [WATCHER_ALIAS]
    assert all(not value.endswith(":1") for value in watcher)
    assert all("*" not in value for value in watcher)


def test_the_worker_may_not_invoke_the_watcher() -> None:
    """The demo-clock route is the request path's, and the schedule is the scheduler role's."""

    for item in _worker(_split_template()):
        if item["Effect"] != "Allow" or "lambda:InvokeFunction" not in actions_of(item):
            continue
        resource = item["Resource"]
        values = resource if isinstance(resource, list) else [resource]
        assert all("commitment-watcher" not in value for value in values)


# -- Phase 11 batch 4: the demo clock (ADR-029) ---------------------------------------


def test_only_the_api_may_move_the_demo_clock() -> None:
    """One action, one exact literal partition, and the forward rule is a stored condition."""

    grant = _find(_api(_split_template()), "AdvanceDemoClockItemOnly")
    assert grant["Effect"] == "Allow"
    assert actions_of(grant) == {"dynamodb:PutItem"}
    assert leading_keys(grant) == [DEMO_CLOCK_PARTITION]
    assert SHAREABLE_REFERENCE in json.dumps(grant["Resource"])


def test_the_worker_reads_the_demo_clock_and_can_never_write_it() -> None:
    """The § 14 resolution: ``EXTRACT_COMMITMENT`` needs authoritative logical time, read-only."""

    worker = _worker(_split_template())
    read = _find(worker, "ReadDemoClockItemOnly")
    assert read["Effect"] == "Allow"
    assert actions_of(read) == {"dynamodb:GetItem"}
    assert leading_keys(read) == [DEMO_CLOCK_PARTITION]

    deny = _find(worker, "DenyWorkerDemoClockWrites")
    assert deny["Effect"] == "Deny"
    assert actions_of(deny) == set(DENIED_CLOCK_WRITE_ACTIONS)
    assert deny["Condition"]["ForAnyValue:StringLike"]["dynamodb:LeadingKeys"] == [
        DEMO_CLOCK_PARTITION
    ]


def test_no_role_holds_a_wildcard_clock_grant() -> None:
    """ADR-029 § 2: there is no ``NS#*#CLOCK*`` anywhere, and a policy with one fails review."""

    for items in (_api(_split_template()), _worker(_split_template())):
        for item in items:
            for key in leading_keys(item):
                if "CLOCK" in key:
                    assert key == DEMO_CLOCK_PARTITION


def test_the_demo_clock_partition_is_the_exact_deployed_literal() -> None:
    assert DEMO_CLOCK_PARTITION == "NS#DEMO#CLOCK"
