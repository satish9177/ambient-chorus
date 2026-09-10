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
from aws_cdk import aws_cloudwatch as cloudwatch
from constructs import Construct

from infra.cdk.config import CdkBuildConfig

LAMBDA_ERROR_ALARM_THRESHOLD = 1
"""One function error is worth surfacing (deployment contract § 28: "make one function error
visible")."""

ALARM_EVALUATION_PERIODS = 1
ALARM_PERIOD_MINUTES = 5

_DEPLOYED_FUNCTIONS = ("api", "worker", "compiler", "sender", "commitment-watcher", "demo-reset")
"""The functions that exist after Macro A -- the five request-path functions plus the reset
function. AgentCore runtimes are Macro B and get no metric here."""


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
        self._dashboard = self._build_dashboard(
            dead_letter_queue_name=dead_letter_queue_name, http_api_id=http_api_id
        )

        CfnOutput(self, "DashboardName", value=self._dashboard.dashboard_name)
        CfnOutput(
            self,
            "LambdaErrorAlarmNames",
            value=",".join(alarm.alarm_name for alarm in self.error_alarms.values()),
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

    def _build_dashboard(
        self, *, dead_letter_queue_name: str, http_api_id: str | None
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

        # Reserved for Macro B. No metric is drawn -- the three AgentCore runtimes do not exist
        # yet, and a fabricated ``AgentInvocations`` line would be a lie on the demo dashboard.
        dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown=(
                    "## AgentCore runtime metrics\n"
                    "_Deferred to Macro B._ Monitor / Investigator / Action runtime "
                    "invocation-failure and latency widgets are added when those resources "
                    "are created."
                ),
                width=24,
                height=3,
            )
        )
        return dashboard


__all__ = [
    "ALARM_EVALUATION_PERIODS",
    "ALARM_PERIOD_MINUTES",
    "LAMBDA_ERROR_ALARM_THRESHOLD",
    "ChorusObservabilityStack",
]
