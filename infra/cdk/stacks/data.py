"""Phase 2 data resources: exactly three trust-aligned DynamoDB tables.

The split is a security decision, not a modelling preference. Physical separation is what
makes least-privilege IAM legible: a principal that may read the private Core table has no
grant on the Shareable table at all.

No global or local secondary index is created, because every approved V1 access pattern knows
its community, case, or action ID. Streams stay disabled, and no cache or accelerator is
introduced, so no authorization decision can ever read a stale projection.
"""

from __future__ import annotations

from aws_cdk import RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct

from infra.cdk.config import CdkBuildConfig

TTL_ATTRIBUTE = "expires_at_epoch"
PARTITION_KEY = "PK"
SORT_KEY = "SK"


class ChorusDataStack(Stack):
    """Creates the Core, Shareable, and Audit tables for one environment."""

    def __init__(self, scope: Construct, construct_id: str, *, config: CdkBuildConfig) -> None:
        super().__init__(scope, construct_id)
        Tags.of(self).add("Project", config.project)
        Tags.of(self).add("Environment", config.environment)
        Tags.of(self).add("Namespace", config.namespace)

        self.core_table = self._table(
            construct_id="CoreTable",
            table_name=f"chorus-core-{config.environment}",
            data_class="PRIVATE",
            config=config,
        )
        self.shareable_table = self._table(
            construct_id="ShareableTable",
            table_name=f"chorus-shareable-{config.environment}",
            data_class="SHAREABLE",
            config=config,
        )
        self.audit_table = self._table(
            construct_id="AuditTable",
            table_name=f"chorus-audit-{config.environment}",
            data_class="AUDIT",
            config=config,
        )

    def _table(
        self,
        *,
        construct_id: str,
        table_name: str,
        data_class: str,
        config: CdkBuildConfig,
    ) -> dynamodb.Table:
        table = dynamodb.Table(
            self,
            construct_id,
            table_name=table_name,
            partition_key=dynamodb.Attribute(
                name=PARTITION_KEY, type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name=SORT_KEY, type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=not config.is_disposable
            ),
            time_to_live_attribute=TTL_ATTRIBUTE,
            deletion_protection=not config.is_disposable,
            removal_policy=(
                RemovalPolicy.DESTROY if config.is_disposable else RemovalPolicy.RETAIN
            ),
        )
        Tags.of(table).add("DataClass", data_class)
        return table
