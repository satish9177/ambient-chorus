"""Category J: the synthesized data stack matches the frozen storage decisions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from aws_cdk import App, assertions
from infra.cdk.app import build_app
from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks import ChorusDataStack
from infra.cdk.stacks.data import TTL_ATTRIBUTE

from chorus.infrastructure.dynamodb.codec import ATTR_EXPIRES_AT_EPOCH
from chorus.settings import Environment, audit_retention_for

TABLE_TYPE = "AWS::DynamoDB::Table"


def template(config: CdkBuildConfig) -> assertions.Template:
    app = App()
    stack = ChorusDataStack(app, "TestData", config=config)
    return assertions.Template.from_stack(stack)


def tables(config: CdkBuildConfig) -> Mapping[str, Mapping[str, Any]]:
    return template(config).find_resources(TABLE_TYPE)


def properties(config: CdkBuildConfig) -> dict[str, Mapping[str, Any]]:
    return {
        resource["Properties"]["TableName"]: resource["Properties"]
        for resource in tables(config).values()
    }


def test_exactly_three_tables_are_created() -> None:
    template(CdkBuildConfig()).resource_count_is(TABLE_TYPE, 3)


def test_table_names_follow_the_frozen_convention() -> None:
    assert set(properties(CdkBuildConfig(environment="development"))) == {
        "chorus-core-development",
        "chorus-shareable-development",
        "chorus-audit-development",
    }
    assert set(properties(CdkBuildConfig(environment="production"))) == {
        "chorus-core-production",
        "chorus-shareable-production",
        "chorus-audit-production",
    }


@pytest.mark.parametrize(
    "table_name",
    ["chorus-core-development", "chorus-shareable-development", "chorus-audit-development"],
)
def test_every_table_uses_the_frozen_key_schema(table_name: str) -> None:
    table = properties(CdkBuildConfig())[table_name]

    assert table["KeySchema"] == [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ]
    assert sorted(table["AttributeDefinitions"], key=lambda item: item["AttributeName"]) == [
        {"AttributeName": "PK", "AttributeType": "S"},
        {"AttributeName": "SK", "AttributeType": "S"},
    ]


@pytest.mark.parametrize(
    "table_name",
    ["chorus-core-development", "chorus-shareable-development", "chorus-audit-development"],
)
def test_every_table_is_on_demand_encrypted_and_ttl_enabled(table_name: str) -> None:
    table = properties(CdkBuildConfig())[table_name]

    assert table["BillingMode"] == "PAY_PER_REQUEST"
    assert table["SSESpecification"] == {"SSEEnabled": True}
    assert table["TimeToLiveSpecification"] == {
        "AttributeName": "expires_at_epoch",
        "Enabled": True,
    }


@pytest.mark.parametrize(
    "table_name",
    ["chorus-core-development", "chorus-shareable-development", "chorus-audit-development"],
)
def test_no_secondary_index_stream_or_accelerator_is_created(table_name: str) -> None:
    table = properties(CdkBuildConfig())[table_name]

    assert "GlobalSecondaryIndexes" not in table
    assert "LocalSecondaryIndexes" not in table
    assert "StreamSpecification" not in table
    assert "KinesisStreamSpecification" not in table


def test_no_cache_cluster_or_extra_resource_type_is_created() -> None:
    resources = template(CdkBuildConfig()).to_json()["Resources"]
    created = {resource["Type"] for resource in resources.values()}

    assert created == {TABLE_TYPE}


def test_a_durable_environment_protects_its_data() -> None:
    stack = tables(CdkBuildConfig(environment="production"))

    for resource in stack.values():
        assert resource["Properties"]["DeletionProtectionEnabled"] is True
        assert resource["Properties"]["PointInTimeRecoverySpecification"] == {
            "PointInTimeRecoveryEnabled": True
        }
        assert resource["DeletionPolicy"] == "Retain"


def test_a_disposable_environment_can_be_torn_down() -> None:
    stack = tables(CdkBuildConfig(environment="development"))

    for resource in stack.values():
        assert resource["Properties"]["DeletionProtectionEnabled"] is False
        assert resource["DeletionPolicy"] == "Delete"


def test_each_table_is_tagged_with_its_trust_zone() -> None:
    by_name = properties(CdkBuildConfig())

    def data_class(table_name: str) -> str:
        tags = {tag["Key"]: tag["Value"] for tag in by_name[table_name]["Tags"]}
        assert tags["Project"] == "ambient-chorus"
        assert tags["Namespace"] == "LOCAL"
        return str(tags["DataClass"])

    assert data_class("chorus-core-development") == "PRIVATE"
    assert data_class("chorus-shareable-development") == "SHAREABLE"
    assert data_class("chorus-audit-development") == "AUDIT"


def test_the_application_synthesizes_every_declared_stack() -> None:
    assembly = build_app().synth()

    assert {stack.stack_name for stack in assembly.stacks} == {
        "AmbientChorusFoundation",
        "AmbientChorusData",
        "AmbientChorusAgents",
    }


def test_environments_outside_the_disposable_set_stay_protected() -> None:
    assert CdkBuildConfig(environment="development").is_disposable is True
    assert CdkBuildConfig(environment="test").is_disposable is True
    assert CdkBuildConfig(environment="staging").is_disposable is False
    assert CdkBuildConfig(environment="production").is_disposable is False
    assert CdkBuildConfig(environment="anything-else").is_disposable is False


def test_the_ttl_attribute_stays_configured_in_every_environment() -> None:
    """Table configuration is environment-independent; what an item carries is not.

    Enabling TTL on a table cannot expire an item that has no TTL attribute, so the table
    keeps the same shape everywhere and the retention policy decides whether an audit item
    carries ``expires_at_epoch`` at all. Those two are asserted together here because getting
    the split wrong in either direction would silently expire an audit trail.
    """

    for environment in ("development", "test", "demo"):
        for table in properties(CdkBuildConfig(environment=environment)).values():
            assert table["TimeToLiveSpecification"] == {
                "AttributeName": TTL_ATTRIBUTE,
                "Enabled": True,
            }


def test_only_the_demo_environment_writes_an_audit_ttl_value() -> None:
    assert audit_retention_for(Environment.DEMO).ttl_seconds is not None
    assert audit_retention_for(Environment.DEVELOPMENT).ttl_seconds is None
    assert audit_retention_for(Environment.TEST).ttl_seconds is None


def test_the_stack_ttl_attribute_is_the_one_the_codec_writes() -> None:
    """A rename on either side would disable expiry silently rather than loudly."""

    assert TTL_ATTRIBUTE == ATTR_EXPIRES_AT_EPOCH
