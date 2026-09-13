"""AgentCore observability and inbound function monitoring assertions (Macro B).

Tests:
- Synthesis of CloudWatch alarms for all 3 AgentCore runtimes (Monitor, Investigator, Action)
  with TotalErrors metric, correct dimensions, and threshold >= 1.
- Per-runtime graph widgets (Invocations, TotalErrors, Throttles, Latency) on the dashboard.
- Inclusion of inbound function Lambda error alarm and dashboard metric.
"""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from aws_cdk import App, Stack, assertions
from infra.cdk.app import build_app

ALARM = "AWS::CloudWatch::Alarm"
DASHBOARD = "AWS::CloudWatch::Dashboard"


@cache
def full_app() -> App:
    """Build the offline demo app with all stacks wired including AgentCore and Inbound."""
    return build_app(offline=True, context={"environment": "demo", "namespace": "DEMO"})


def observability_template() -> assertions.Template:
    app = full_app()
    for child in app.node.children:
        if isinstance(child, Stack) and child.stack_name.endswith("Observability"):
            return assertions.Template.from_stack(child)
    raise RuntimeError("Observability stack not found in app")


def _alarms() -> list[dict[str, Any]]:
    return [r["Properties"] for r in observability_template().find_resources(ALARM).values()]


def test_agentcore_runtime_error_alarms_synthesize_for_all_three_runtimes() -> None:
    """Deployment contract §§ 27-30: each AgentCore runtime has a TotalErrors alarm."""
    alarms = _alarms()
    # The AgentCore metric namespace is "AWS/Bedrock-AgentCore" (with the hyphen) -- confirmed
    # from aws-cdk-lib's own runtime-base.js.
    runtime_alarms = [a for a in alarms if a["Namespace"] == "AWS/Bedrock-AgentCore"]

    assert len(runtime_alarms) == 3
    alarm_names = {a["AlarmName"] for a in runtime_alarms}
    assert alarm_names == {
        "chorus-monitor-runtime-errors-demo",
        "chorus-investigator-runtime-errors-demo",
        "chorus-action-runtime-errors-demo",
    }

    for alarm in runtime_alarms:
        assert alarm["MetricName"] == "TotalErrors"
        assert alarm["Threshold"] >= 1
        assert alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
        assert alarm["EvaluationPeriods"] == 1
        assert alarm["TreatMissingData"] == "notBreaching"
        assert "AlarmActions" not in alarm  # no accepted notification destination (§ 28)

        dimensions = {d["Name"]: d["Value"] for d in alarm.get("Dimensions", [])}
        assert dimensions.get("Operation") == "InvokeAgentRuntime"
        assert "Resource" in dimensions  # the runtime ARN (a cross-stack token)
        # The Name dimension names the **live** endpoint, never "::DEFAULT" -- the alarm must not
        # silently watch the default endpoint while traffic goes through `live` (Macro B brief B).
        name_dim = dimensions.get("Name", "")
        assert name_dim.endswith("::live"), name_dim
        assert "::DEFAULT" not in name_dim


def test_inbound_lambda_error_alarm_synthesizes() -> None:
    """Inbound function has a standard Lambda error alarm."""
    alarms = _alarms()
    inbound_alarm = next(
        (a for a in alarms if a.get("AlarmName") == "chorus-inbound-demo-errors"), None
    )
    assert inbound_alarm is not None
    assert inbound_alarm["Namespace"] == "AWS/Lambda"
    assert inbound_alarm["MetricName"] == "Errors"
    assert inbound_alarm["Threshold"] == 1
    assert inbound_alarm["Dimensions"] == [{"Name": "FunctionName", "Value": "chorus-inbound-demo"}]


def test_dashboard_includes_agentcore_and_inbound_metrics() -> None:
    """Dashboard has real metrics for all runtimes and the inbound function."""
    template = observability_template()
    dashboard = next(iter(template.find_resources(DASHBOARD).values()))["Properties"]
    body = json.dumps(dashboard["DashboardBody"])

    # Inbound Lambda metrics
    assert "chorus-inbound-demo" in body

    # AgentCore runtime metrics
    assert "AWS/Bedrock-AgentCore" in body
    assert "TotalErrors" in body
    assert "Invocations" in body
    assert "Throttles" in body
    assert "Latency" in body

    # Both the live-endpoint suffix and DEFAULT are drawn, so whichever AWS populates is visible.
    assert "::live" in body
    assert "::DEFAULT" in body

    # Verify per-runtime titles
    assert "Monitor runtime" in body
    assert "Investigator runtime" in body
    assert "Action runtime" in body

    # The old "Deferred to Macro B" placeholder is gone.
    assert "Deferred to Macro B" not in body
