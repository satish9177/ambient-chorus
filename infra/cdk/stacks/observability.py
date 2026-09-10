"""Base observability for the resources that exist now: Lambda error alarms and one dashboard.

Macro A's observability deliverable (deployment contract §§ 19, 27-30;
[09-observability-errors-and-failures.md](../../../docs/architecture/09-observability-errors-and-failures.md)).
Only what is possible with resources that exist today:

* a Lambda **error alarm** for each currently deployed function -- the five request-path
  functions and the reset function -- so one function error is visible;
* one CloudWatch **dashboard**, ``chorus-{env}``, showing Lambda invocations / errors /
  duration, the watcher DLQ depth, reset errors, and the HTTP API 4xx/5xx counts;
* a reserved, clearly-labelled section for AgentCore runtime metrics -- **deferred to Macro B**,
  because those resources do not exist yet and this stack synthesizes **no** fake AgentCore
  metric or resource.

No notification destination
---------------------------
No accepted SNS topic, email, or pager destination exists (deployment contract § 28), so **no
alarm carries an ``AlarmActions``**. The alarms make failure visible on the dashboard and in the
CloudWatch alarms view; wiring a destination is later work. No contact address appears anywhere
in this stack.

The watcher DLQ-depth alarm is **not** recreated here -- it lives in ``AmbientChorusWatcher``
(one dropped due event is silent from every other surface) and stays exactly as it is. This
stack only references the queue's metric on the dashboard.

Thresholds
----------
Not frozen in the accepted docs, so a minimal defensible baseline: ``Errors >= 1`` over one
5-minute period, missing data non-breaching. It surfaces a real function failure without an
elaborate operational policy; a later batch can tune it against live traffic.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Environment, Stack, Tags
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cloudwatch as cloudwatch
from constructs import Construct

from infra.cdk.config import CdkBuildConfig

# Canary J residual note on AgentCore CloudWatch metric dimensions:
# Canary J must confirm the `Name` dimension endpoint suffix AWS emits for `live`-endpoint traffic;
# if AWS emits only `::DEFAULT`, the alarm's `Name` dimension changes in a one-line follow-up.
# The dashboard already shows both suffixes (`::{endpoint_name}` and `::DEFAULT`).
# Reference the open worker `agentRuntimeArn`/`qualifier` question.

LAMBDA_ERROR_ALARM_THRESHOLD = 1
"""One function error is worth surfacing (deployment contract § 28: "make one function error
visible")."""

ALARM_EVALUATION_PERIODS = 1
ALARM_PERIOD_MINUTES = 5

_DEPLOYED_FUNCTIONS = (
    "api",
    "worker",
    "compiler",
    "sender",
    "commitment-watcher",
    "demo-reset",
    "inbound",
)
"""The functions that exist across the application -- the six request-path functions,
the inbound SES entrypoint, plus the reset function."""


class ChorusObservabilityStack(Stack):
    """``AmbientChorusObservability`` -- Lambda error alarms and the one demo dashboard."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: CdkBuildConfig,
        dead_letter_queue_name: str,
        http_api_id: str | None = None,
        monitor_runtime: agentcore.Runtime | None = None,
        investigator_runtime: agentcore.Runtime | None = None,
        action_runtime: agentcore.Runtime | None = None,
        monitor_live_endpoint: agentcore.RuntimeEndpoint | None = None,
        investigator_live_endpoint: agentcore.RuntimeEndpoint | None = None,
        action_live_endpoint: agentcore.RuntimeEndpoint | None = None,
        env: Environment | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env)
        Tags.of(self).add("Project", config.project)
        Tags.of(self).add("Environment", config.environment)
        Tags.of(self).add("Namespace", config.namespace)
        Tags.of(self).add("DataClass", "NONE")

        self._config = config
        self.function_names = [
            f"chorus-{suffix}-{config.environment}" for suffix in _DEPLOYED_FUNCTIONS
        ]

        self.error_alarms: dict[str, cloudwatch.Alarm] = {
            name: self._lambda_error_alarm(name) for name in self.function_names
        }

        runtimes_and_endpoints = (
            ("monitor", monitor_runtime, monitor_live_endpoint),
            ("investigator", investigator_runtime, investigator_live_endpoint),
            ("action", action_runtime, action_live_endpoint),
        )
        self.runtime_error_alarms: dict[str, cloudwatch.Alarm] = {}
        for agent_name, runtime, endpoint in runtimes_and_endpoints:
            if runtime is not None and endpoint is not None:
                alarm = self._agentcore_runtime_error_alarm(agent_name, runtime, endpoint)
                self.runtime_error_alarms[alarm.alarm_name] = alarm

        self._dashboard = self._build_dashboard(
            dead_letter_queue_name=dead_letter_queue_name,
            http_api_id=http_api_id,
            runtimes_and_endpoints=runtimes_and_endpoints,
        )

        CfnOutput(self, "DashboardName", value=self._dashboard.dashboard_name)
        CfnOutput(
            self,
            "LambdaErrorAlarmNames",
            value=",".join(alarm.alarm_name for alarm in self.error_alarms.values()),
        )
        if self.runtime_error_alarms:
            CfnOutput(
                self,
                "RuntimeErrorAlarmNames",
                value=",".join(alarm.alarm_name for alarm in self.runtime_error_alarms.values()),
            )

    def _lambda_errors_metric(self, function_name: str) -> cloudwatch.Metric:
        return cloudwatch.Metric(
            namespace="AWS/Lambda",
            metric_name="Errors",
            dimensions_map={"FunctionName": function_name},
            statistic="Sum",
            period=Duration.minutes(ALARM_PERIOD_MINUTES),
        )

    def _lambda_error_alarm(self, function_name: str) -> cloudwatch.Alarm:
        return cloudwatch.Alarm(
            self,
            f"{function_name}-errors",
            alarm_name=f"{function_name}-errors",
            alarm_description=(
                f"{function_name} raised an unhandled error -- a deployed CHORUS function failed."
            ),
            metric=self._lambda_errors_metric(function_name),
            threshold=LAMBDA_ERROR_ALARM_THRESHOLD,
            evaluation_periods=ALARM_EVALUATION_PERIODS,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            # No AlarmActions: no accepted notification destination (deployment contract § 28).
        )

    def _agentcore_runtime_error_alarm(
        self,
        agent: str,
        runtime: agentcore.Runtime,
        endpoint: agentcore.RuntimeEndpoint,
    ) -> cloudwatch.Alarm:
        alarm_name = f"chorus-{agent}-runtime-errors-{self._config.environment}"
        deployed_name = getattr(runtime, "agent_runtime_name", None) or f"chorus_{agent}"
        endpoint_name = endpoint.endpoint_name
        metric = runtime.metric(
            "TotalErrors",
            dimensions_map={
                "Name": f"{deployed_name}::{endpoint_name}",
                "Resource": runtime.agent_runtime_arn,
            },
            period=Duration.minutes(ALARM_PERIOD_MINUTES),
            statistic="Sum",
        )
        return cloudwatch.Alarm(
            self,
            alarm_name,
            alarm_name=alarm_name,
            alarm_description=(
                f"AgentCore {agent} runtime ({deployed_name}) raised an unhandled invocation error."
            ),
            metric=metric,
            threshold=1,
            evaluation_periods=ALARM_EVALUATION_PERIODS,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            # No AlarmActions: no accepted notification destination (deployment contract § 28).
        )

    def _build_dashboard(
        self,
        *,
        dead_letter_queue_name: str,
        http_api_id: str | None,
        runtimes_and_endpoints: tuple[
            tuple[str, agentcore.Runtime | None, agentcore.RuntimeEndpoint | None], ...
        ]
        | None = None,
    ) -> cloudwatch.Dashboard:
        dashboard = cloudwatch.Dashboard(
            self,
            "ChorusDashboard",
            dashboard_name=f"chorus-{self._config.environment}",
        )

        def lambda_metrics(metric_name: str, statistic: str) -> list[cloudwatch.Metric]:
            return [
                cloudwatch.Metric(
                    namespace="AWS/Lambda",
                    metric_name=metric_name,
                    dimensions_map={"FunctionName": name},
                    statistic=statistic,
                    period=Duration.minutes(ALARM_PERIOD_MINUTES),
                    label=name,
                )
                for name in self.function_names
            ]

        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Lambda invocations",
                left=lambda_metrics("Invocations", "Sum"),
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="Lambda errors",
                left=lambda_metrics("Errors", "Sum"),
                width=12,
            ),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Lambda duration (p95)",
                left=lambda_metrics("Duration", "p95"),
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="Commitment watcher DLQ depth / reset errors",
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/SQS",
                        metric_name="ApproximateNumberOfMessagesVisible",
                        dimensions_map={"QueueName": dead_letter_queue_name},
                        statistic="Maximum",
                        period=Duration.minutes(ALARM_PERIOD_MINUTES),
                        label="watcher DLQ visible",
                    ),
                    self._lambda_errors_metric(
                        f"chorus-demo-reset-{self._config.environment}"
                    ).with_(label="reset errors"),
                ],
                width=12,
            ),
        )

        if http_api_id is not None:
            dashboard.add_widgets(
                cloudwatch.GraphWidget(
                    title="HTTP API 4xx / 5xx",
                    left=[
                        cloudwatch.Metric(
                            namespace="AWS/ApiGateway",
                            metric_name=code,
                            dimensions_map={"ApiId": http_api_id},
                            statistic="Sum",
                            period=Duration.minutes(ALARM_PERIOD_MINUTES),
                            label=code,
                        )
                        for code in ("4xx", "5xx")
                    ],
                    width=12,
                )
            )

        wired_runtimes = [
            (name, runtime, endpoint)
            for name, runtime, endpoint in (runtimes_and_endpoints or ())
            if runtime is not None and endpoint is not None
        ]
        if not wired_runtimes:
            # No runtime is wired in (an isolated single-stack synthesis). Draw a placeholder
            # rather than a fabricated metric line.
            dashboard.add_widgets(
                cloudwatch.TextWidget(
                    markdown=(
                        "## AgentCore runtimes\n"
                        "Deferred to Macro B. Real metrics attach to deployed runtime ARNs."
                    ),
                    width=24,
                    height=2,
                )
            )
            return dashboard

        for agent_name, runtime, endpoint in wired_runtimes:
            deployed_name = getattr(runtime, "agent_runtime_name", None) or f"chorus_{agent_name}"
            endpoint_name = endpoint.endpoint_name
            title = agent_name.capitalize()

            def line(
                metric_name: str,
                suffix: str,
                statistic: str,
                *,
                runtime: agentcore.Runtime = runtime,
                deployed_name: str = deployed_name,
            ) -> cloudwatch.Metric:
                # Every AgentCore runtime metric shares the same three dimension keys
                # (``Operation``/``Name``/``Resource``) and the ``AWS/Bedrock-AgentCore``
                # namespace -- ``Runtime.metric`` supplies all of that; the ``dimensions_map``
                # override only swaps the ``Name`` endpoint suffix. Each metric is drawn for
                # both ``::{live}`` and ``::DEFAULT`` because whether AWS attributes
                # ``live``-endpoint traffic to the endpoint name or only to ``DEFAULT`` is a
                # canary-J question (see the module note); showing both keeps every invocation
                # visible on the demo dashboard regardless.
                return runtime.metric(
                    metric_name,
                    dimensions_map={
                        "Name": f"{deployed_name}::{suffix}",
                        "Resource": runtime.agent_runtime_arn,
                    },
                    statistic=statistic,
                    period=Duration.minutes(ALARM_PERIOD_MINUTES),
                    label=f"{metric_name} ({suffix})",
                )

            dashboard.add_widgets(
                cloudwatch.GraphWidget(
                    title=f"{title} runtime — invocations / errors / throttles",
                    left=[
                        line(name, suffix, "Sum")
                        for suffix in (endpoint_name, "DEFAULT")
                        for name in ("Invocations", "TotalErrors", "Throttles")
                    ],
                    width=12,
                ),
                cloudwatch.GraphWidget(
                    title=f"{title} runtime latency (p95)",
                    left=[line("Latency", suffix, "p95") for suffix in (endpoint_name, "DEFAULT")],
                    width=12,
                ),
            )
        return dashboard


__all__ = [
    "ALARM_EVALUATION_PERIODS",
    "ALARM_PERIOD_MINUTES",
    "LAMBDA_ERROR_ALARM_THRESHOLD",
    "ChorusObservabilityStack",
]
