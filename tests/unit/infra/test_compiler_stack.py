"""The compiler's identity, asserted from the synthesized template.

These are the static half of the frozen IAM matrix. A post-deploy canary proving an
``AccessDenied`` cannot exist before Phase 11 deploys anything, but a policy that grants the
wrong thing is visible in the template today -- and a template assertion fails in CI rather
than in an account.

The negative assertions matter more than the positive ones. "The compiler can read Core" is a
grant somebody meant to write; "the compiler cannot invoke Bedrock" is the property a future
change would break silently.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aws_cdk import App, Stack
from aws_cdk.assertions import Template
from infra.cdk.app import build_app
from infra.cdk.stacks.compiler import (
    DENIED_MODEL_ACTIONS,
    DENIED_OBJECT_ACTIONS,
    DENIED_SIDE_EFFECT_ACTIONS,
)


@pytest.fixture(scope="module")
def app() -> App:
    return build_app()


@pytest.fixture(scope="module")
def compiler(app: App) -> Template:
    return Template.from_stack(_stack(app, "AmbientChorusCompiler"))


@pytest.fixture(scope="module")
def data(app: App) -> Template:
    return Template.from_stack(_stack(app, "AmbientChorusData"))


@pytest.fixture(scope="module")
def agents(app: App) -> Template:
    return Template.from_stack(_stack(app, "AmbientChorusAgents"))


def _stack(app: App, name: str) -> Stack:
    """``find_child`` returns an ``IConstruct``; the assertion API wants the stack."""

    stack = app.node.find_child(name)
    assert isinstance(stack, Stack)
    return stack


def statements(template: Template) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def statement(template: Template, sid: str) -> dict[str, Any]:
    for item in statements(template):
        if item.get("Sid") == sid:
            return item
    raise AssertionError(f"no statement with sid {sid}")


def actions(item: dict[str, Any]) -> set[str]:
    value = item["Action"]
    return set(value) if isinstance(value, list) else {value}


# -- the compiler's grants ----------------------------------------------------------------


def test_the_compiler_deploys_no_function_resource(compiler: Template) -> None:
    """Phase 6 synthesizes the identity; Phase 11 deploys the thing that assumes it."""

    assert compiler.find_resources("AWS::Lambda::Function") == {}


def core_statements(template: Template) -> list[dict[str, Any]]:
    """Every statement that names the Core table, allow or deny."""

    return [item for item in statements(template) if "CoreTable" in json.dumps(item["Resource"])]


def leading_keys(item: dict[str, Any]) -> list[str]:
    condition = item.get("Condition", {})
    for operator in ("ForAllValues:StringLike", "ForAnyValue:StringLike", "StringLike"):
        if operator in condition:
            value = condition[operator].get("dynamodb:LeadingKeys", [])
            return value if isinstance(value, list) else [value]
    return []


WRITE_ACTIONS = frozenset(
    {"dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"}
)


def test_the_compiler_reads_core_without_a_partition_restriction(compiler: Template) -> None:
    """``R(all)`` is the frozen statement, and reads are what compiling is made of."""

    read = statement(compiler, "ReadPrivateCore")

    assert actions(read) == {"dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query"}
    assert not actions(read) & WRITE_ACTIONS


def test_the_case_version_guard_is_read_only_transactional_authority(
    compiler: Template,
) -> None:
    """ADR-019. The compiler asserts a case version; it cannot change one.

    ``ConditionCheckItem`` is a distinct action from ``PutItem``, so the guard exists without
    any write grant on case partitions at all -- which is the whole reason the case row can be
    condition-checked by a principal that must never mutate it.
    """

    item = statement(compiler, "ConditionCheckCaseVersion")

    assert actions(item) == {"dynamodb:ConditionCheckItem"}
    assert leading_keys(item) == ["NS#*#CASE#*"]
    assert item["Condition"]["StringEquals"] == {
        "dynamodb:EnclosingOperation": "TransactWriteItems"
    }


def test_the_fence_condition_check_covers_the_fence_partition(compiler: Template) -> None:
    """Compile, mandate decisions and investigation applies all stage this check."""

    item = statement(compiler, "ConditionCheckSendFence")

    assert actions(item) == {"dynamodb:ConditionCheckItem"}
    assert leading_keys(item) == ["NS#*#FENCE#*"]


def test_the_only_core_write_grant_is_the_fence_partition(compiler: Template) -> None:
    """The frozen ``W(fence only)`` sentence, now expressible because the fence moved."""

    item = statement(compiler, "WriteSendFenceOnly")

    assert actions(item) == {"dynamodb:PutItem", "dynamodb:DeleteItem"}
    assert leading_keys(item) == ["NS#*#FENCE#*"]
    assert "dynamodb:UpdateItem" not in actions(item)


# -- negative capability: the point of ADR-019 --------------------------------------------


def test_no_core_write_grant_reaches_a_case_partition(compiler: Template) -> None:
    """The proof, stated as an exhaustive search rather than as a spot check.

    Every Core *allow* is examined; any that grants a write action must be scoped to leading
    keys that exclude case partitions entirely. A future statement that widened the boundary
    would have to pass this, and could not.
    """

    for item in core_statements(compiler):
        if item["Effect"] != "Allow":
            continue
        granted = actions(item) & WRITE_ACTIONS
        if not granted:
            continue
        prefixes = leading_keys(item)
        assert prefixes, f"{item['Sid']} grants {granted} with no LeadingKeys restriction at all"
        assert all(prefix.startswith("NS#*#FENCE#") for prefix in prefixes), (
            f"{item['Sid']} grants {granted} outside the fence partition: {prefixes}"
        )


@pytest.mark.parametrize(
    "action", ["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem"]
)
def test_no_case_partition_write_action_is_granted(compiler: Template, action: str) -> None:
    """Matrix items 1-3: no Put, Delete, or Update reaches ``NS#*#CASE#*``."""

    reaching = [
        item["Sid"]
        for item in core_statements(compiler)
        if item["Effect"] == "Allow"
        and action in actions(item)
        and any(prefix.startswith("NS#*#CASE#") for prefix in leading_keys(item))
    ]

    assert reaching == []


def test_the_compiler_never_holds_update_item_anywhere(compiler: Template) -> None:
    """Acquire is a conditional put and release a conditional delete. No update path exists."""

    granted = {
        action
        for item in statements(compiler)
        if item["Effect"] == "Allow"
        for action in actions(item)
    }

    assert "dynamodb:UpdateItem" not in granted


def test_case_partition_writes_are_denied_outright(compiler: Template) -> None:
    """Defence in depth over the separation, not a substitute for it.

    ``ForAnyValue`` matters: a transaction naming one case-partition item alongside legitimate
    fence items is refused whole rather than permitted because most of its keys were fine.
    """

    item = statement(compiler, "DenyCasePartitionWrites")

    assert item["Effect"] == "Deny"
    assert actions(item) == {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
    }
    assert item["Condition"]["ForAnyValue:StringLike"]["dynamodb:LeadingKeys"] == ["NS#*#CASE#*"]


@pytest.mark.parametrize(
    "entity", ["CASE", "FACT", "REPORT", "EVIDENCE", "MANDATE", "ASSESSMENT", "OPERATION"]
)
def test_fence_writes_cannot_reach_any_private_entity_partition(
    compiler: Template, entity: str
) -> None:
    """Matrix item 6, over the partition prefixes those entities actually live under.

    Facts, reports, evidence, mandates and assessments are all sort keys inside a case
    partition, so excluding ``NS#*#CASE#*`` excludes every one of them; operations have their
    own partition and are excluded by the same reasoning.
    """

    write = statement(compiler, "WriteSendFenceOnly")

    for prefix in leading_keys(write):
        assert not prefix.startswith(f"NS#*#{entity}#")
    assert leading_keys(write) == ["NS#*#FENCE#*"]


def test_no_core_statement_grants_an_unscoped_write(compiler: Template) -> None:
    """Matrix item 7. A write grant with no LeadingKeys is a table-wide write."""

    for item in core_statements(compiler):
        if item["Effect"] != "Allow" or not actions(item) & WRITE_ACTIONS:
            continue
        assert leading_keys(item), f"{item['Sid']} is an unscoped Core write"


def test_the_role_carries_no_managed_policy_that_could_restore_core_writes(
    app: App, compiler: Template
) -> None:
    """Matrix item 8. An attached managed policy would be an allow this file cannot see."""

    roles = compiler.find_resources("AWS::IAM::Role")
    assert len(roles) == 1
    properties = next(iter(roles.values()))["Properties"]

    assert properties.get("ManagedPolicyArns", []) == []
    assert properties.get("PermissionsBoundary") is None
    assert properties.get("Policies", []) == []


def test_the_compiler_reads_private_objects_and_writes_export_objects(
    compiler: Template,
) -> None:
    assert actions(statement(compiler, "ReadPrivateEvidenceObjects")) == {"s3:GetObject"}
    assert actions(statement(compiler, "WriteExportEvidenceObjects")) == {
        "s3:PutObject",
        "s3:GetObject",
    }


def test_the_compiler_holds_one_key_for_reading_and_a_different_one_for_writing(
    compiler: Template,
) -> None:
    """Separate keys are what make the two buckets two boundaries rather than one."""

    decrypt = statement(compiler, "DecryptPrivateEvidence")
    encrypt = statement(compiler, "EncryptExportEvidence")

    assert actions(decrypt) == {"kms:Decrypt", "kms:DescribeKey"}
    assert actions(encrypt) == {"kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"}
    assert "kms:Encrypt" not in actions(decrypt)
    assert "kms:Decrypt" not in actions(encrypt)
    assert decrypt["Resource"] != encrypt["Resource"]


@pytest.mark.parametrize(
    ("sid", "expected"),
    [
        ("DenyModelAccess", DENIED_MODEL_ACTIONS),
        ("DenySideEffects", DENIED_SIDE_EFFECT_ACTIONS),
        ("DenyObjectDeletion", DENIED_OBJECT_ACTIONS),
    ],
)
def test_the_compiler_denies_what_it_must_never_reach(
    compiler: Template, sid: str, expected: tuple[str, ...]
) -> None:
    """Denied, not merely ungranted: an explicit deny survives a later accidental grant."""

    item = statement(compiler, sid)

    assert item["Effect"] == "Deny"
    assert actions(item) == set(expected)


def test_no_blanket_transaction_action_is_granted(compiler: Template) -> None:
    """AWS authorizes a transaction through its members, so the blanket action is not needed.

    Keeping it would have been a permission this role does not require and a place a future
    participant could hide behind.
    """

    granted = {
        action
        for item in statements(compiler)
        if item["Effect"] == "Allow"
        for action in actions(item)
    }

    assert "dynamodb:TransactWriteItems" not in granted
    assert "dynamodb:TransactGetItems" not in granted


def test_the_compiler_cannot_invoke_a_model(compiler: Template) -> None:
    """The one property that would make policy probabilistic if it were ever lost."""

    granted = {
        action
        for item in statements(compiler)
        if item["Effect"] == "Allow"
        for action in actions(item)
    }

    assert not any(action.startswith("bedrock") for action in granted)
    assert not any(action.startswith("ses:") for action in granted)


# -- the buckets --------------------------------------------------------------------------


@pytest.mark.parametrize("logical", ["PrivateEvidenceBucket", "ExportEvidenceBucket"])
def test_each_evidence_bucket_blocks_public_access_and_disables_acls(
    data: Template, logical: str
) -> None:
    buckets = [
        resource
        for name, resource in data.find_resources("AWS::S3::Bucket").items()
        if logical in name
    ]
    assert len(buckets) == 1
    properties = buckets[0]["Properties"]

    assert properties["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert properties["OwnershipControls"]["Rules"] == [{"ObjectOwnership": "BucketOwnerEnforced"}]
    assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
    encryption = properties["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
    assert encryption["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"


def test_the_two_buckets_use_two_different_keys(data: Template) -> None:
    keys = set(data.find_resources("AWS::KMS::Key"))
    assert len(keys) == 2

    encryption = {
        name: resource["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"][0][
            "ServerSideEncryptionByDefault"
        ]["KMSMasterKeyID"]
        for name, resource in data.find_resources("AWS::S3::Bucket").items()
    }
    assert len({json.dumps(value, sort_keys=True) for value in encryption.values()}) == 2


def test_every_bucket_policy_denies_insecure_transport_and_unencrypted_writes(
    data: Template,
) -> None:
    for policy in data.find_resources("AWS::S3::BucketPolicy").values():
        sids = {item.get("Sid") for item in policy["Properties"]["PolicyDocument"]["Statement"]}
        assert "DenyUnencryptedObjectUploads" in sids
        assert "DenyWrongKmsKey" in sids
        effects = {
            item.get("Sid"): item["Effect"]
            for item in policy["Properties"]["PolicyDocument"]["Statement"]
        }
        assert effects["DenyUnencryptedObjectUploads"] == "Deny"
        transport = [
            item
            for item in policy["Properties"]["PolicyDocument"]["Statement"]
            if item.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false"
        ]
        assert transport, "every evidence bucket denies non-TLS access"


def test_only_the_compiler_role_may_write_export_objects(data: Template) -> None:
    """Matrix AI, at the object layer. The identity policies are the other half."""

    statements_found = [
        item
        for policy in data.find_resources("AWS::S3::BucketPolicy").values()
        for item in policy["Properties"]["PolicyDocument"]["Statement"]
        if item.get("Sid") == "OnlyCompilerWritesExportEvidence"
    ]

    assert len(statements_found) == 1
    item = statements_found[0]
    assert item["Effect"] == "Deny"
    principal = item["Condition"]["StringNotLike"]["aws:PrincipalArn"]
    assert any("chorus-compiler" in json.dumps(entry) for entry in principal)


def test_no_bucket_grants_public_read(data: Template) -> None:
    for policy in data.find_resources("AWS::S3::BucketPolicy").values():
        for item in policy["Properties"]["PolicyDocument"]["Statement"]:
            if item["Effect"] != "Allow":
                continue
            principal = json.dumps(item.get("Principal", {}))
            assert '"*"' not in principal
            assert "AWS.*" not in principal


def test_each_bucket_has_a_lifecycle_backstop(data: Template) -> None:
    """The orphan story: an unreferenced derivative is swept, never compensated away."""

    for resource in data.find_resources("AWS::S3::Bucket").values():
        rules = resource["Properties"]["LifecycleConfiguration"]["Rules"]
        assert rules
        assert all(rule["Status"] == "Enabled" for rule in rules)
        assert all(rule["ExpirationInDays"] > 0 for rule in rules)


# -- the agent runtimes, restated against the new buckets ---------------------------------


@pytest.mark.parametrize("role", ["MonitorRuntimeRole", "InvestigatorRuntimeRole"])
def test_no_agent_runtime_can_reach_object_storage(agents: Template, role: str) -> None:
    """Matrix AK and AL. The agents' deny list already covers S3; this pins it in place."""

    denied = {
        action
        for item in statements(agents)
        if item["Effect"] == "Deny"
        for action in actions(item)
    }

    assert {"s3:GetObject", "s3:PutObject", "s3:ListBucket"} <= denied
    assert role in json.dumps(agents.find_resources("AWS::IAM::Role"))


def test_no_agent_runtime_holds_a_kms_grant(agents: Template) -> None:
    """Matrix AJ, AK, AL at the key layer: no key action means no readable object."""

    granted = {
        action
        for item in statements(agents)
        if item["Effect"] == "Allow"
        for action in actions(item)
    }

    assert not any(action.startswith("kms:") for action in granted)
