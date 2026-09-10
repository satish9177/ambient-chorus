"""Base observability: a Lambda error alarm per deployed function, and one dashboard.

Deployment contract §§ 27-30, 40. Only what is possible with resources that exist now -- no
fake AgentCore metric, no invented SNS target, and no sensitive value in a dashboard body.
"""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from aws_cdk import App, Environment, assertions
from infra.cdk.config import CdkBuildConfig
from infra.cdk.stacks.observability import (
    LAMBDA_ERROR_ALARM_THRESHOLD,
    ChorusObservabilityStack,
)

ALARM = "AWS::CloudWatch::Alarm"
DASHBOARD = "AWS::CloudWatch::Dashboard"


@cache
def observability_template() -> assertions.Template:
    app = App()
    config = CdkBuildConfig(environment="demo", namespace="DEMO")
    stack = ChorusObservabilityStack(
        app,
        "AmbientChorusObservability",
        config=config,
        dead_letter_queue_name="chorus-commitment-dlq-demo",
        http_api_id="abc123xyz",
        env=Environment(region="us-east-1"),
    )
    return assertions.Template.from_stack(stack)


def _alarms() -> list[dict[str, Any]]:
    return [r["Properties"] for r in observability_template().find_resources(ALARM).values()]


def test_one_lambda_error_alarm_per_deployed_function_including_reset() -> None:
    alarms = _alarms()
    names = {a["AlarmName"] for a in alarms}
    assert names == {
        "chorus-api-demo-errors",
        "chorus-worker-demo-errors",
        "chorus-compiler-demo-errors",
        "chorus-sender-demo-errors",
        "chorus-commitment-watcher-demo-errors",
        "chorus-demo-reset-demo-errors",
        "chorus-inbound-demo-errors",
    }


def test_every_error_alarm_watches_the_lambda_errors_metric_at_a_minimal_threshold() -> None:
    for alarm in _alarms():
        assert alarm["Namespace"] == "AWS/Lambda"
        assert alarm["MetricName"] == "Errors"
        assert alarm["Threshold"] == LAMBDA_ERROR_ALARM_THRESHOLD
        assert alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
        assert alarm["TreatMissingData"] == "notBreaching"


def test_no_alarm_carries_a_notification_action() -> None:
    """Deployment contract § 28: no accepted SNS / email / pager destination exists, so no
    ``AlarmActions`` -- and no contact address anywhere."""

    for alarm in _alarms():
        assert not alarm.get("AlarmActions")
        assert not alarm.get("OKActions")
        assert not alarm.get("InsufficientDataActions")


def test_there_is_exactly_one_dashboard_named_for_the_environment() -> None:
    built = observability_template()
    built.resource_count_is(DASHBOARD, 1)
    built.has_resource_properties(DASHBOARD, {"DashboardName": "chorus-demo"})


def test_the_isolated_dashboard_draws_no_fake_agentcore_metric() -> None:
    # This helper builds the stack **in isolation** -- no runtimes are passed -- so the
    # AgentCore section is the deferred placeholder, not a fabricated metric. The real
    # per-runtime widgets are asserted against the full app in
    # ``test_agentcore_observability.py``.
    dashboard = next(iter(observability_template().find_resources(DASHBOARD).values()))[
        "Properties"
    ]
    body = json.dumps(dashboard["DashboardBody"])
    assert "AWS/Lambda" in body
    assert "chorus-worker-demo" in body
    assert "chorus-demo-reset-demo" in body
    assert "chorus-commitment-dlq-demo" in body
    assert "AWS/ApiGateway" in body
    # No fabricated AgentCore metric when no runtime is wired in -- the section is simply
    # absent (or a text placeholder), never an invented metric line.
    assert "AgentInvocations" not in body
    assert "AWS/Bedrock-AgentCore" not in body


def test_no_sns_topic_or_subscription_is_invented_here() -> None:
    observability_template().resource_count_is("AWS::SNS::Topic", 0)
    observability_template().resource_count_is("AWS::SNS::Subscription", 0)


def test_the_dashboard_body_carries_no_address_token_or_secret_shaped_string() -> None:
    dashboard = next(iter(observability_template().find_resources(DASHBOARD).values()))[
        "Properties"
    ]
    body = json.dumps(dashboard["DashboardBody"])
    assert "@" not in body  # no email address
    assert "secret" not in body.lower()
    assert "token" not in body.lower()
