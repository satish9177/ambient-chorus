"""Phase 2 data resources: exactly three trust-aligned DynamoDB tables.

The split is a security decision, not a modelling preference. Physical separation is what
makes least-privilege IAM legible: a principal that may read the private Core table has no
grant on the Shareable table at all.

No global or local secondary index is created, because every approved V1 access pattern knows
its community, case, or action ID. Streams stay disabled, and no cache or accelerator is
introduced, so no authorization decision can ever read a stale projection.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.cdk.config import CdkBuildConfig

TTL_ATTRIBUTE = "expires_at_epoch"
PARTITION_KEY = "PK"
SORT_KEY = "SK"

PRIVATE_EVIDENCE_RETENTION_DAYS = 30
EXPORT_EVIDENCE_RETENTION_DAYS = 14
"""Demo lifecycle backstops.

The export rule is also the orphan story. A derivative written for a compile that was then
denied is an unreferenced object -- nothing points at it and no current view ever can -- so
it is swept by this rule rather than by a compensating delete, which would itself be a write
that could fail (ADR-018).
"""


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

        self.private_evidence_key = self._key(
            construct_id="PrivateEvidenceKey",
            alias=f"alias/chorus-private-evidence-{config.environment}",
            description="Private community evidence at rest",
            config=config,
        )
        self.export_evidence_key = self._key(
            construct_id="ExportEvidenceKey",
            alias=f"alias/chorus-export-evidence-{config.environment}",
            description="Compiler-created export-safe derivatives at rest",
            config=config,
        )
        self.private_evidence_bucket = self._bucket(
            construct_id="PrivateEvidenceBucket",
            bucket_name=f"chorus-private-evidence-{config.environment}",
            encryption_key=self.private_evidence_key,
            data_class="PRIVATE",
            expiration_days=PRIVATE_EVIDENCE_RETENTION_DAYS,
            config=config,
        )
        self.export_evidence_bucket = self._bucket(
            construct_id="ExportEvidenceBucket",
            bucket_name=f"chorus-export-evidence-{config.environment}",
            encryption_key=self.export_evidence_key,
            data_class="EXPORT_SAFE",
            expiration_days=EXPORT_EVIDENCE_RETENTION_DAYS,
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

    def _key(
        self,
        *,
        construct_id: str,
        alias: str,
        description: str,
        config: CdkBuildConfig,
    ) -> kms.Key:
        """One customer-managed key per bucket, never one key for both.

        Separate keys are what make the trust split enforceable rather than described: a
        principal holding ``s3:GetObject`` on the private bucket still reads nothing without
        the private key's ``kms:Decrypt``, and the Action, Monitor, and Investigator roles hold
        neither. One shared key would collapse two boundaries into one grant.
        """

        key = kms.Key(
            self,
            construct_id,
            alias=alias,
            description=description,
            enable_key_rotation=True,
            removal_policy=(
                RemovalPolicy.DESTROY if config.is_disposable else RemovalPolicy.RETAIN
            ),
            pending_window=Duration.days(7) if config.is_disposable else Duration.days(30),
        )
        return key

    def _bucket(
        self,
        *,
        construct_id: str,
        bucket_name: str,
        encryption_key: kms.Key,
        data_class: str,
        expiration_days: int,
        config: CdkBuildConfig,
    ) -> s3.Bucket:
        """One evidence bucket with every frozen control on, and a TLS deny of its own.

        ``enforce_ssl`` adds the deny for insecure transport; ACLs are disabled outright through
        bucket-owner-enforced ownership, so an object's own grants can never widen access past
        the bucket policy. Versioning is on because an application-immutable object still needs
        a recovery story, and public access is blocked in all four ways rather than relying on
        the account-level setting being right.
        """

        bucket = s3.Bucket(
            self,
            construct_id,
            bucket_name=bucket_name,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=encryption_key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            versioned=True,
            public_read_access=False,
            removal_policy=(
                RemovalPolicy.DESTROY if config.is_disposable else RemovalPolicy.RETAIN
            ),
            auto_delete_objects=False,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="demo-backstop-expiry",
                    enabled=True,
                    expiration=Duration.days(expiration_days),
                    noncurrent_version_expiration=Duration.days(expiration_days),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
        )
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyUnencryptedObjectUploads",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject"],
                resources=[bucket.arn_for_objects("*")],
                conditions={"StringNotEquals": {"s3:x-amz-server-side-encryption": "aws:kms"}},
            )
        )
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyWrongKmsKey",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject"],
                resources=[bucket.arn_for_objects("*")],
                conditions={
                    "StringNotEquals": {
                        "s3:x-amz-server-side-encryption-aws-kms-key-id": encryption_key.key_arn
                    }
                },
            )
        )
        Tags.of(bucket).add("DataClass", data_class)
        return bucket
