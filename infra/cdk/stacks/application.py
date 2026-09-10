"""The application boundary as identity: the request path and the durable worker as two roles.

``02`` § IAM separates the FastAPI request path from the operation worker; the frozen trust
matrix (deployment contract § 8.1) keeps them distinct. This stack synthesizes
``chorus-api-{env}`` and ``chorus-worker-{env}`` -- a shared private-zone data plane, and the
capabilities that genuinely differ split by which principal makes the call. The API reads the
demo bearer-token secret and invokes the worker and the compiler; the worker invokes the three
agent runtimes, the compiler, and the sender, and owns the commitment-schedule grant and the
single ``iam:PassRole``. Neither can create a compiled view -- the compiler is the sole creator
of views by IAM and not by convention.

The application is the broadest principal in the system, and that is exactly why its boundary
has to be written down rather than inferred. It reads and writes the private Core zone, it
creates actions and approvals in the Shareable zone, and it appends to Audit. What it must
**not** be able to do is create or alter a compiled view -- because the compiler is the sole
creator of views by IAM and not by convention.

Three grants are unusual enough to say out loud.

**Shareable is split by key prefix.** ``dynamodb:LeadingKeys`` allows the application to write
only the action and case prefixes. The compiler writes only the two view prefixes. Neither can
reach the other's, so "the application cannot mint a view that authorizes its own proposal" is
a property of the key grammar rather than of anybody's discipline.

**The application holds `ConditionCheckItem` on the view prefixes, and nothing else there.**
The Phase-7 proposal transaction has to be able to refuse to commit against a view that has
moved while the Action model was answering (ADR-022 § 7). AWS authorizes a transaction through
the permission each *participant* needs, so a ``ConditionCheck`` participant requires exactly
this action and a ``Put`` would require ``dynamodb:PutItem`` -- which is what makes a read-only
transactional authority expressible at all. **A condition check must never become a write
grant**, and the alternative -- granting ``UpdateItem`` so the row can be "conditioned on" -- is
named in the ADR so it is refused once rather than proposed repeatedly.

**Bedrock is granted for exactly three inference profiles and denied nowhere else.** The
application invokes agents through AgentCore rather than Bedrock directly, so it holds
``bedrock-agentcore:InvokeAgentRuntime`` on the three runtime ARNs and no raw model action.

SES is denied outright. Its breadth of private access is precisely why it never receives a send
permission: it invokes the sender with an action identifier, never a rendered body and never a
recipient address.
"""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import CfnOutput, Environment, RemovalPolicy, Stack, Tags
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.cdk.config import CdkBuildConfig, DeploymentIdentities
from infra.cdk.lambda_support import (
    ResourceNames,
    api_environment,
    chorus_lambda,
    load_lambda_manifest,
    worker_environment,
)
from infra.cdk.network_support import vpc_eni_policy_statements

VIEW_KEY_PREFIXES = ("NS#*#VIEW#*", "NS#*#VIEW_CURRENT#*")
"""The compiler-owned Shareable partitions. The application reads and condition-checks these
and can never write one."""

APPLICATION_SHAREABLE_PREFIXES = (
    "NS#*#ACTION#*",
    "NS#*#ACTION_CURRENT#*",
    "NS#*#EXECUTION#*",
    "NS#*#OUTBOUND_MESSAGE#*",
    "NS#*#CASE#*",
)
"""The only Shareable partitions the application may write.

``ACTION#`` holds the immutable proposals and approvals; ``ACTION_CURRENT#`` holds the current
action pointer, the action history locators, and the action-apply idempotency record;
``EXECUTION#`` holds the send execution and the send-command records; ``OUTBOUND_MESSAGE#``
holds the immutable correlation locator the action case projection writes at ``SENT``; ``CASE#``
holds commitments, their schedule projections, and their verification requests.

``OUTBOUND_MESSAGE#`` is **not a widening** either. The locator is a participant of the
projection transaction, which is the application worker's and never the sender's -- the sender
is denied that prefix explicitly (ADR-026 § 3). It is a partition of its own rather than a sort
key inside ``NS#n#EXECUTION#a`` because correlation starts from a reply's ``In-Reply-To`` and
has no action identifier yet, so the printed key could only be resolved by the scan or the GSI
that ADR rejects by name.

``EXECUTION#`` is **not a widening** (ADR-024 SS 4). The application already created the
``DRAFT`` execution as participant 2 of the proposal apply and moves it on both human
decisions; this is where a write it already had now lives.

Two principals therefore hold ``PutItem`` over one prefix, and that is the one Phase-8 boundary
IAM does not draw. What keeps them apart is the state machine: the application can only move a
row that is in ``DRAFT`` or ``APPROVED``, and the sender only one in ``APPROVED`` or
``SENDING``, each conditioned on an exact row version. The single overlapping state is the
approval-withdrawal race, which the compare-and-swap resolves with exactly one winner. It is
written down here rather than left implied, because an assertion that it holds is a test over
the transitions and not over a policy.
"""

READ_ACTIONS = ("dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query")
WRITE_ACTIONS = ("dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem")

CONDITION_CHECK_ACTION = "dynamodb:ConditionCheckItem"
"""Read-only transactional authority: assert an item's state without being able to change it."""

TRANSACTION_ONLY = {"StringEquals": {"dynamodb:EnclosingOperation": "TransactWriteItems"}}
"""Narrows a grant to participants of a transaction.

Applied to the view condition check because that participant genuinely never happens
standalone: it is staged only inside the proposal apply. Restricting it costs nothing and
removes a capability that would otherwise exist on its own.
"""

DENIED_VIEW_WRITE_ACTIONS = (
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
)
"""Never writable by this role, and denied rather than merely ungranted.

``ForAnyValue`` is deliberate: a transaction naming *any* view-partition item alongside
legitimate action items is refused whole, rather than being permitted because most of its keys
were acceptable. This is the defence-in-depth half of the guarantee whose positive half is the
``LeadingKeys`` split above -- an explicit deny cannot be overridden by a later grant, so a
future change that accidentally widened the write statement still fails closed.
"""

DENIED_SEND_ACTIONS = (
    "ses:SendEmail",
    "ses:SendRawEmail",
    "sesv2:SendEmail",
    "sesv2:SendBulkEmail",
)
"""The application drafts and approves. Only the sender sends."""

SCHEDULER_ACTIONS = ("scheduler:CreateSchedule", "scheduler:GetSchedule")
"""The application's complete scheduler capability, narrowed by ADR-028 § 6.

**No ``DeleteSchedule`` and no ``UpdateSchedule``.** ``DeadlineSchedulerPort`` has no method for
either, V1 has no reschedule verb, and ``ActionAfterCompletion=DELETE`` handles cleanup -- so a
wider grant would be a permission with no caller and one obvious illegitimate one. A grant wider
than its caller is a grant waiting for a second caller.
"""

DENIED_SCHEDULER_ACTIONS = ("scheduler:DeleteSchedule", "scheduler:UpdateSchedule")
"""Denied rather than merely ungranted, so a later widening still fails closed."""

DENIED_MODEL_ACTIONS = (
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
)
"""Agents are invoked through their runtimes, which pin the prompt and the schema.

A direct model grant would let application code send an unreviewed prompt to the same model,
which is the one path around every artifact-level control the runtimes exist to impose.
"""

# -- I5: the request path and the durable worker are two principals, not one ------------

API_SHAREABLE_WRITE_PREFIXES = (
    "NS#*#ACTION#*",
    "NS#*#ACTION_CURRENT#*",
    "NS#*#EXECUTION#*",
    "NS#*#CASE#*",
)
"""What the FastAPI request path writes in the Shareable zone (deployment contract § 8.1).

``ACTION#`` and ``ACTION_CURRENT#`` -- the approval record and the current-action pointer moved
by ``approve``/``invalidate``; ``EXECUTION#`` -- the ``DRAFT`` execution the same human
decisions advance; ``CASE#`` -- the commitment edge ``verify`` and the demo-clock ``due`` path
write.

**Not ``OUTBOUND_MESSAGE#``.** The correlation locator is written only by the action-case
projection at ``SENT``, which runs in the worker (ADR-026 § 3). The request path never performs
that projection, so it never names that prefix.
"""

WORKER_SHAREABLE_WRITE_PREFIXES = APPLICATION_SHAREABLE_PREFIXES
"""What the asynchronous operation worker writes: the request path's prefixes plus
``OUTBOUND_MESSAGE#`` (the ``SENT`` projection locator). :data:`APPLICATION_SHAREABLE_PREFIXES`
is retained as the name other modules and tests import for this full set."""

DENIED_AGENT_RUNTIME_INVOCATION = ("bedrock-agentcore:InvokeAgentRuntime",)
"""Denied on the **API** role. No request-path route invokes a runtime directly; every
agent-invoking operation is dispatched to the worker and returns 202 (deployment contract
§ 8.1). If a route is ever found that needs it, that is a design change, not a grant to add
quietly."""

DENIED_API_SCHEDULER_ACTIONS = (
    "scheduler:CreateSchedule",
    "scheduler:GetSchedule",
    "scheduler:DeleteSchedule",
    "scheduler:UpdateSchedule",
)
"""Denied on the **API** role, together with the ``scheduler:*`` wildcard. Scheduling a
commitment is a worker operation; the request path holds no scheduler capability at all, and no
``iam:PassRole`` to hand the scheduler its execution role."""

DENIED_PASS_ROLE_ACTIONS = ("iam:PassRole",)
"""Denied on the **API** role. Only the worker passes the scheduler execution role, and only
that one role (deployment contract § 8.1, § 14)."""

DEMO_CLOCK_PARTITION = "NS#DEMO#CLOCK"
"""The **exact literal** partition of the deployed demo clock (ADR-029 § 1-2).

Not a pattern, not a family, and deliberately not ``NS#*#CLOCK*``. ``DEMO`` is the only
namespace a deployed clock exists in -- ``Settings.validate_environment_contract`` already
refuses any other in the ``demo`` environment -- and a wildcard here would authorize a clock in
a namespace no deployment has, which is the shape of permission that is correct on the day it
is written and wrong after the next namespace exists. A policy containing one fails review, and
a template test asserts its absence.
"""

DEMO_CLOCK_READ_ACTION = "dynamodb:GetItem"
"""One item, one direct read. There is no query and no scan of the clock partition."""

DEMO_CLOCK_WRITE_ACTION = "dynamodb:PutItem"
"""The whole of the API's clock write authority, and it is a *conditional* whole-item put.

``PutItem`` rather than ``UpdateItem`` because the storage driver has no attribute-level update
path by design (:mod:`chorus.ports.storage`), and because the guarded forward CAS of ADR-029 § 3
is expressible as three condition expressions on a whole-item put -- the version, the reset
generation, and the strictly-earlier stored reading. Keeping it to ``PutItem`` also leaves the
"no ``dynamodb:UpdateItem`` anywhere" invariant of § 8.8 untouched.
"""

DENIED_CLOCK_WRITE_ACTIONS = (
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:ConditionCheckItem",
)
"""Every form of write the worker must not hold on the clock prefix (ADR-029 § 2).

Denied rather than merely ungranted, so a later widening of the worker's Shareable write
statement still fails closed. ``ConditionCheckItem`` is in the list because a transactional
condition on the clock is still a transaction the clock participates in.
"""

DENIED_SECRET_READ_ACTIONS = ("secretsmanager:GetSecretValue",)
"""Denied on the **worker** role, table-wide. The worker reads no secret: the demo bearer token
is the API's, and the destination registry is the sender's. A single role holding both the
private zone and the demo-token secret is what made that deny unassertable before the split
(deployment contract § 8.1)."""


@dataclass(frozen=True, slots=True)
class ApplicationTables:
    """The three tables the application's grants are scoped to."""

    core: dynamodb.ITable
    shareable: dynamodb.ITable
    audit: dynamodb.ITable


@dataclass(frozen=True, slots=True)
class ApplicationBuckets:
    """The two evidence buckets and the separate keys that actually gate them."""

    private: s3.IBucket
    export: s3.IBucket
    private_key: kms.IKey
    export_key: kms.IKey


class ChorusApplicationStack(Stack):
    """The request path and the durable worker as **two** roles, plus their log groups.

    ``02`` § IAM separates the FastAPI request path from the operation worker, and the frozen
    trust matrix (deployment contract § 8.1) keeps them apart: a single role is the union of
    two, which makes the demo-token deny unassertable and hands the request-path role the
    agent-invoke grant. So this stack synthesizes ``chorus-api-{env}`` and
    ``chorus-worker-{env}`` -- one shared data-plane boundary, and the capabilities that
    genuinely differ split by which principal makes the call. No compute resource is created
    here; the two Lambdas and the demo-token secret are Phase 11's.

    Capability | API (``chorus-api-{env}``) | Worker (``chorus-worker-{env}``)
    - Core: R/W + ConditionCheck on both.
    - Shareable read: all, on both.
    - Shareable write: API -> ``ACTION#`` / ``ACTION_CURRENT#`` / ``EXECUTION#`` / ``CASE#``;
      worker -> the same **plus ``OUTBOUND_MESSAGE#``** (the ``SENT`` projection locator).
    - Shareable view prefixes: ``ConditionCheck`` only, on both.
    - Audit: append, on both. Private S3 + private KMS: yes, on both. Export S3: GetObject +
      Decrypt, on both.
    - Secrets Manager: API reads the demo bearer-token secret and the pagination
      cursor-signing key; worker holds an explicit deny.
    - Lambda invoke: API -> operation worker + compiler; worker -> compiler + sender.
    - AgentCore: API denied; worker invokes the Monitor / Investigator / Action runtimes.
    - Scheduler: API denied outright, including ``iam:PassRole``; worker holds
      ``CreateSchedule`` / ``GetSchedule`` on the one group and ``iam:PassRole`` on the
      scheduler execution role alone.
    - SES and direct Bedrock: denied on both.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: CdkBuildConfig,
        tables: ApplicationTables,
        buckets: ApplicationBuckets,
        agent_runtime_arns: tuple[str, ...] = (),
        scheduler_group_name: str | None = None,
        scheduler_role_arn: str | None = None,
        compiler_function_arn: str | None = None,
        sender_function_arn: str | None = None,
        watcher_alias_arn: str | None = None,
        demo_access_secret_arn: str | None = None,
        cursor_signing_secret_arn: str | None = None,
        destination_registry_secret_arn: str | None = None,
        identities: DeploymentIdentities | None = None,
        offline_synth: bool = True,
        worker_vpc: ec2.IVpc | None = None,
        worker_vpc_subnets: ec2.SubnetSelection | None = None,
        worker_vpc_subnet_arns: list[str] | None = None,
        worker_security_group: ec2.ISecurityGroup | None = None,
        env: Environment | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env)
        Tags.of(self).add("Project", config.project)
        Tags.of(self).add("Environment", config.environment)
        Tags.of(self).add("Namespace", config.namespace)
        Tags.of(self).add("DataClass", "PRIVATE")

        # Deployment identities (secret ARNs, AgentCore runtime targets) come from context; an
        # explicit argument still wins, which is what the split-role template tests inject. The
        # ``worker_function_arn`` is deliberately no longer an argument: the worker Lambda is
        # created **in this stack** below, so the API's grant and environment bind to the
        # created resource directly rather than to a hand-built external ARN (deployment
        # contract SS 15, SS 30).
        identities = identities or DeploymentIdentities(environment=config.environment)
        agent_runtime_arns = agent_runtime_arns or identities.agent_runtime_arns
        demo_access_secret_arn = demo_access_secret_arn or identities.demo_access_secret_arn
        cursor_signing_secret_arn = (
            cursor_signing_secret_arn or identities.cursor_signing_secret_arn
        )
        destination_registry_secret_arn = (
            destination_registry_secret_arn or identities.destination_registry_secret_arn
        )
        # The compiler / sender / watcher-alias ARNs are the **actual resource** references the
        # sibling stacks pass in via ``app.py`` (review P2-2); the deterministic literals here
        # are only the fallback an isolated single-stack synthesis uses.
        region = config.aws_region
        compiler_function_arn = compiler_function_arn or (
            f"arn:aws:lambda:{region}:{self.account}:function:chorus-compiler-{config.environment}"
        )
        sender_function_arn = sender_function_arn or (
            f"arn:aws:lambda:{region}:{self.account}:function:chorus-sender-{config.environment}"
        )
        watcher_alias_arn = watcher_alias_arn or (
            f"arn:aws:lambda:{region}:{self.account}:function:"
            f"chorus-commitment-watcher-{config.environment}:live"
        )
        scheduler_role_arn = scheduler_role_arn or (
            f"arn:aws:iam::{self.account}:role/chorus-scheduler-{config.environment}"
        )

        self.scheduler_group_name = scheduler_group_name or f"chorus-{config.environment}"
        self.scheduler_role_arn = scheduler_role_arn

        self.api_log_group = logs.LogGroup(
            self,
            "ApiLogGroup",
            log_group_name=f"/chorus/{config.environment}/api",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.worker_log_group = logs.LogGroup(
            self,
            "WorkerLogGroup",
            log_group_name=f"/chorus/{config.environment}/worker",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.api_role_name = f"chorus-api-{config.environment}"
        self.api_role = iam.Role(
            self,
            "ApiRole",
            role_name=self.api_role_name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="FastAPI request path: private zone, actions, and the demo-token secret.",
        )
        self.worker_role_name = f"chorus-worker-{config.environment}"
        self.worker_role = iam.Role(
            self,
            "WorkerRole",
            role_name=self.worker_role_name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Operation worker: runtimes, compiler, sender, and commitment schedules.",
        )
        self.api_role_arn_literal = f"arn:aws:iam::{self.account}:role/{self.api_role_name}"
        self.worker_role_arn_literal = f"arn:aws:iam::{self.account}:role/{self.worker_role_name}"

        for role in (self.api_role, self.worker_role):
            self._grant_shared_data_plane(role, tables=tables, buckets=buckets)

        names = ResourceNames.for_config(config)

        # I2 -> compute (deployment contract SS 14). The worker first, so the API's grant and
        # environment below bind to the created resource. Under the pre-existing worker role and
        # worker log group; no secret ARN in its environment (SS 40).
        self.worker_function_name = f"chorus-worker-{config.environment}"
        self.worker_function = chorus_lambda(
            self,
            "WorkerFunction",
            config=config,
            manifest=load_lambda_manifest("worker"),
            role=self.worker_role,
            environment=worker_environment(
                config=config,
                identities=identities,
                names=names,
                compiler_function_arn=compiler_function_arn,
                sender_function_arn=sender_function_arn,
                watcher_alias_arn=watcher_alias_arn,
                scheduler_role_arn=scheduler_role_arn,
            ),
            log_group=self.worker_log_group,
            offline_synth=offline_synth,
            vpc=worker_vpc,
            vpc_subnets=worker_vpc_subnets,
            security_groups=(
                [worker_security_group] if worker_security_group is not None else None
            ),
        )

        # I16 -> VPC attachment (deployment contract §§ 2, 14-17). The **worker** goes inside
        # the isolated network -- it invokes the AgentCore runtimes, which are VPC-only. The
        # **API stays out** (§ 2): it is a request-path front end behind API Gateway, placing it
        # in the VPC buys no boundary and adds ENI cold-start latency to the one component a
        # presenter waits on. So only ``worker_role`` receives ENI permissions, and only when
        # the worker is actually VPC-attached; ``api_role`` never does.
        worker_function_arn_literal = (
            f"arn:aws:lambda:{region}:{self.account}:function:{self.worker_function_name}"
        )
        # The ENI grant names the worker's **deterministic function ARN literal**, not the
        # ``Fn::GetAtt`` -- routing the worker role's policy through the worker function would be
        # a ``role -> function -> role`` cycle.
        if worker_vpc is not None and worker_vpc_subnet_arns is not None:
            for eni_statement in vpc_eni_policy_statements(
                function_arn=worker_function_arn_literal,
                subnet_arns=worker_vpc_subnet_arns,
            ):
                self.worker_role.add_to_policy(eni_statement)

        self.api_function_name = f"chorus-api-{config.environment}"
        self.api_function = chorus_lambda(
            self,
            "ApiFunction",
            config=config,
            manifest=load_lambda_manifest("api"),
            role=self.api_role,
            environment=api_environment(
                config=config,
                names=names,
                worker_function_arn=self.worker_function.function_arn,
                compiler_function_arn=compiler_function_arn,
                watcher_alias_arn=watcher_alias_arn,
                demo_access_secret_arn=demo_access_secret_arn,
                cursor_signing_secret_arn=cursor_signing_secret_arn,
            ),
            log_group=self.api_log_group,
            offline_synth=offline_synth,
        )

        self._grant_api_boundary(
            tables=tables,
            worker_function=self.worker_function,
            compiler_function_arn=compiler_function_arn,
            watcher_alias_arn=watcher_alias_arn,
            demo_access_secret_arn=demo_access_secret_arn,
            cursor_signing_secret_arn=cursor_signing_secret_arn,
            destination_registry_secret_arn=destination_registry_secret_arn,
        )
        self._grant_worker_boundary(
            tables=tables,
            agent_runtime_arns=agent_runtime_arns,
            compiler_function_arn=compiler_function_arn,
            sender_function_arn=sender_function_arn,
        )

        self._create_http_api()
        self._declare_outputs(watcher_alias_arn=watcher_alias_arn)

    # -- shared boundary ---------------------------------------------------------------------

    def _grant_shared_data_plane(
        self, role: iam.Role, *, tables: ApplicationTables, buckets: ApplicationBuckets
    ) -> None:
        """The boundary both principals hold identically: the private zone and the evidence
        objects.

        Every DynamoDB grant names the *underlying* action a transaction participant needs
        rather than a blanket ``dynamodb:TransactWriteItems``. AWS authorizes a transaction
        through its members, so the blanket action would be a permission neither role needs and
        a place for a future participant to hide.
        """

        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadWritePrivateCore",
                effect=iam.Effect.ALLOW,
                actions=[*READ_ACTIONS, *WRITE_ACTIONS, CONDITION_CHECK_ACTION],
                resources=[tables.core.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadShareable",
                effect=iam.Effect.ALLOW,
                actions=list(READ_ACTIONS),
                resources=[tables.shareable.table_arn],
            )
        )
        # ADR-022 § 7. Read-only transactional authority over the compiler-owned view
        # partitions, so the Phase-7 proposal apply (worker) and the approval transaction (API)
        # can refuse to commit against a view that moved while the model was answering -- without
        # either being able to move one itself.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ConditionCheckCurrentViewPointer",
                effect=iam.Effect.ALLOW,
                actions=[CONDITION_CHECK_ACTION],
                resources=[tables.shareable.table_arn],
                conditions={
                    "ForAllValues:StringLike": {"dynamodb:LeadingKeys": list(VIEW_KEY_PREFIXES)},
                    **TRANSACTION_ONLY,
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AppendAudit",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query"],
                resources=[tables.audit.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadWritePrivateEvidenceObjects",
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[buckets.private.arn_for_objects("*")],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadExportEvidenceObjects",
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject"],
                resources=[buckets.export.arn_for_objects("*")],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="UsePrivateEvidenceKey",
                effect=iam.Effect.ALLOW,
                actions=["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                resources=[buckets.private_key.key_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DecryptExportEvidence",
                effect=iam.Effect.ALLOW,
                actions=["kms:Decrypt", "kms:DescribeKey"],
                resources=[buckets.export_key.key_arn],
            )
        )
        # The negative half of the view guarantee, and the statement the ADR-022 static
        # assertion actually reads. Not merely ungranted -- denied, because an explicit deny
        # cannot be overridden by a later grant.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyViewPartitionWrites",
                effect=iam.Effect.DENY,
                actions=list(DENIED_VIEW_WRITE_ACTIONS),
                resources=[tables.shareable.table_arn],
                conditions={
                    "ForAnyValue:StringLike": {"dynamodb:LeadingKeys": list(VIEW_KEY_PREFIXES)}
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyApplicationSend",
                effect=iam.Effect.DENY,
                actions=list(DENIED_SEND_ACTIONS),
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyDirectModelAccess",
                effect=iam.Effect.DENY,
                actions=list(DENIED_MODEL_ACTIONS),
                resources=["*"],
            )
        )

    # -- API request-path boundary --------------------------------------------------------------

    def _grant_api_boundary(
        self,
        *,
        tables: ApplicationTables,
        worker_function: lambda_.IFunction,
        compiler_function_arn: str,
        watcher_alias_arn: str,
        demo_access_secret_arn: str,
        cursor_signing_secret_arn: str,
        destination_registry_secret_arn: str,
    ) -> None:
        """The request path's own capabilities and the denies that keep it a front end.

        It writes the action and case prefixes the human decisions move -- and **not**
        ``OUTBOUND_MESSAGE#``, which only the worker's ``SENT`` projection writes. It invokes
        the operation worker and (synchronously, for the compile route) the compiler, and
        nothing else. It reads the demo bearer-token secret and the pagination cursor-signing
        key, and no other secret (Phase 11 batch 4 repair, P2-5 -- the two are purpose-separated
        identities, so this is two statements naming two exact ARNs, never a shared one). It
        holds no scheduler capability, no ``iam:PassRole``, and no agent-runtime invocation:
        every agent-invoking route dispatches to the worker and returns 202.
        """

        self.api_role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteApiActionAndCasePrefixesOnly",
                effect=iam.Effect.ALLOW,
                actions=[*WRITE_ACTIONS, CONDITION_CHECK_ACTION],
                resources=[tables.shareable.table_arn],
                conditions={
                    "ForAllValues:StringLike": {
                        "dynamodb:LeadingKeys": list(API_SHAREABLE_WRITE_PREFIXES)
                    }
                },
            )
        )
        self.api_role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteOwnApiLogs",
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
                resources=[
                    self.api_log_group.log_group_arn,
                    f"{self.api_log_group.log_group_arn}:log-stream:*",
                ],
            )
        )
        # The worker for every asynchronous operation; the compiler synchronously for the one
        # deterministic compile route. No sender, no watcher, no runtime. The worker resource is
        # the one **created in this stack**, so the grant and the function are provably the same
        # object (deployment contract SS 15) -- not a hand-built ARN a caller could aim
        # elsewhere. The compiler ARN is the deterministic literal the compiler stack also
        # produces, so all three of its uses (this grant, the worker's grant, the two function
        # environments) name one string (SS 17).
        self.api_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeOperationWorkerAndCompilerOnly",
                effect=iam.Effect.ALLOW,
                actions=["lambda:InvokeFunction"],
                resources=[worker_function.function_arn, compiler_function_arn],
            )
        )
        if watcher_alias_arn is not None:
            # ADR-028 § 5 and deployment contract § 8.1. ``POST /v1/demo/clock/advance`` promises
            # the watcher's outcome in its response body, so the request path advances the
            # durable clock and then invokes the watcher **synchronously**. Routing it through
            # the asynchronous worker to avoid this one grant would silently change a frozen
            # endpoint from "here is what the watcher decided" to "a decision was scheduled".
            #
            # The resource is the qualified **``:live`` alias** ARN and nothing else: no
            # unqualified function, no numeric version, no wildcard. Rollback repoints the alias
            # at a published version and this statement does not change (deployment contract
            # § 20). It is a second, separate statement rather than an extra resource on
            # ``InvokeOperationWorkerAndCompilerOnly`` so a template test can assert the watcher
            # authority exactly, and so widening one does not silently widen the other.
            self.api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeCommitmentWatcherLiveAliasOnly",
                    effect=iam.Effect.ALLOW,
                    actions=["lambda:InvokeFunction"],
                    resources=[watcher_alias_arn],
                )
            )
        # ADR-029 § 2. The request path is the **only** principal that may move logical time, and
        # it may do so only through the guarded forward compare-and-swap of § 3. The grant is one
        # action on one exact literal partition; the three fences that make it forward-only are
        # condition expressions the table evaluates, not checks this process performs.
        #
        # ``PutItem`` alone: the storage driver has no attribute-level update path, so the CAS is
        # a conditional whole-item put -- which also leaves § 8.8's "no ``dynamodb:UpdateItem``
        # anywhere" invariant untouched. Reads of the clock are already covered by the
        # table-wide ``ReadShareable`` statement both principals hold.
        self.api_role.add_to_policy(
            iam.PolicyStatement(
                sid="AdvanceDemoClockItemOnly",
                effect=iam.Effect.ALLOW,
                actions=[DEMO_CLOCK_WRITE_ACTION],
                resources=[tables.shareable.table_arn],
                conditions={
                    "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [DEMO_CLOCK_PARTITION]}
                },
            )
        )
        if demo_access_secret_arn is not None:
            self.api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ReadDemoAccessTokenSecretOnly",
                    effect=iam.Effect.ALLOW,
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[demo_access_secret_arn],
                )
            )
        if cursor_signing_secret_arn is not None:
            # P2-5: its own exact secret identity, never folded into the demo access secret
            # above -- the two have unrelated blast radii and unrelated rotation schedules
            # (`chorus.infrastructure.secrets.cursor_signing`). A separate statement so a
            # template test can assert this one grant exactly, and so widening one secret's
            # resource can never silently widen the other's.
            self.api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ReadCursorSigningKeySecretOnly",
                    effect=iam.Effect.ALLOW,
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[cursor_signing_secret_arn],
                )
            )
        if destination_registry_secret_arn is not None:
            # The destination registry is the sender's alone (deployment contract § 8.1). Denied
            # by name so a later grant cannot hand the request path a correspondent address.
            self.api_role.add_to_policy(
                iam.PolicyStatement(
                    sid="DenyApiDestinationRegistrySecret",
                    effect=iam.Effect.DENY,
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[destination_registry_secret_arn],
                )
            )
        self.api_role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyApiAgentRuntimeInvocation",
                effect=iam.Effect.DENY,
                actions=list(DENIED_AGENT_RUNTIME_INVOCATION),
                resources=["*"],
            )
        )
        self.api_role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyApiSchedulerAuthority",
                effect=iam.Effect.DENY,
                actions=[*DENIED_API_SCHEDULER_ACTIONS, "scheduler:*"],
                resources=["*"],
            )
        )
        self.api_role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyApiPassRole",
                effect=iam.Effect.DENY,
                actions=list(DENIED_PASS_ROLE_ACTIONS),
                resources=["*"],
            )
        )

    # -- worker boundary --------------------------------------------------------------------

    def _grant_worker_boundary(
        self,
        *,
        tables: ApplicationTables,
        agent_runtime_arns: tuple[str, ...],
        compiler_function_arn: str | None,
        sender_function_arn: str | None,
    ) -> None:
        """The worker's asynchronous-execution capabilities.

        It writes the same action and case prefixes as the API **plus ``OUTBOUND_MESSAGE#``**,
        the correlation locator its ``SENT`` projection creates (ADR-026 § 3). It invokes the
        three agent runtimes, the compiler, and the sender. It creates and reads commitment
        schedules on the one group and passes the scheduler execution role -- and only that
        role, only to ``scheduler.amazonaws.com``. It reads no secret at all.
        """

        self.worker_role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteActionAndCasePrefixesOnly",
                effect=iam.Effect.ALLOW,
                actions=[*WRITE_ACTIONS, CONDITION_CHECK_ACTION],
                resources=[tables.shareable.table_arn],
                conditions={
                    "ForAllValues:StringLike": {
                        "dynamodb:LeadingKeys": list(WORKER_SHAREABLE_WRITE_PREFIXES)
                    }
                },
            )
        )
        # ADR-029, resolved in Phase 11 batch 4. ``EXTRACT_COMMITMENT`` runs on this principal
        # and supplies ``clock.now()`` as the ``logical_now`` of its ``CreateDueSchedule``
        # request, where the demo mapping ``actual_now + max(10 minutes, logical_due -
        # logical_now)`` turns a logical deadline into a real one-time schedule. Wall time in
        # that slot schedules a thirty-day deadline thirty days out, so the worker genuinely
        # requires authoritative logical time.
        #
        # It gets a **read and only a read**, on one action and one exact literal partition. The
        # table-wide ``ReadShareable`` statement above already reached this row; this statement
        # exists so the authority is *stated and assertable* rather than incidental, and so the
        # deny below has something specific to sit beside.
        self.worker_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadDemoClockItemOnly",
                effect=iam.Effect.ALLOW,
                actions=[DEMO_CLOCK_READ_ACTION],
                resources=[tables.shareable.table_arn],
                conditions={
                    "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [DEMO_CLOCK_PARTITION]}
                },
            )
        )
        # The negative half, and the reason the read above is safe to state. The worker's
        # ``LeadingKeys`` write grant never named the clock prefix, but "not granted" and
        # "denied" are different guarantees, and an explicit deny cannot be overridden by a
        # later allow. ``ForAnyValue``: a transaction naming the clock alongside legitimate
        # action items is refused whole.
        self.worker_role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyWorkerDemoClockWrites",
                effect=iam.Effect.DENY,
                actions=list(DENIED_CLOCK_WRITE_ACTIONS),
                resources=[tables.shareable.table_arn],
                conditions={
                    "ForAnyValue:StringLike": {"dynamodb:LeadingKeys": [DEMO_CLOCK_PARTITION]}
                },
            )
        )
        self.worker_role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteOwnWorkerLogs",
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
                resources=[
                    self.worker_log_group.log_group_arn,
                    f"{self.worker_log_group.log_group_arn}:log-stream:*",
                ],
            )
        )
        if agent_runtime_arns:
            self.worker_role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeNamedAgentRuntimesOnly",
                    effect=iam.Effect.ALLOW,
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=list(agent_runtime_arns),
                )
            )
        downstream = [
            arn for arn in (compiler_function_arn, sender_function_arn) if arn is not None
        ]
        if downstream:
            self.worker_role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeCompilerAndSenderOnly",
                    effect=iam.Effect.ALLOW,
                    actions=["lambda:InvokeFunction"],
                    resources=downstream,
                )
            )
        # ADR-028 § 6. Create and read, on the one schedule group, and nothing else.
        self.worker_role.add_to_policy(
            iam.PolicyStatement(
                sid="CreateAndGetCommitmentSchedulesOnly",
                effect=iam.Effect.ALLOW,
                actions=list(SCHEDULER_ACTIONS),
                resources=[
                    self.format_arn(
                        service="scheduler",
                        resource="schedule",
                        resource_name=f"{self.scheduler_group_name}/*",
                    )
                ],
            )
        )
        if self.scheduler_role_arn:
            # Passing the scheduler execution role is what lets a created schedule invoke the
            # watcher. It is scoped to that one role and one service: a broader ``iam:PassRole``
            # would let the worker hand any role to any target.
            self.worker_role.add_to_policy(
                iam.PolicyStatement(
                    sid="PassSchedulerExecutionRoleOnly",
                    effect=iam.Effect.ALLOW,
                    actions=["iam:PassRole"],
                    resources=[self.scheduler_role_arn],
                    conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
                )
            )
        self.worker_role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyScheduleDeletionAndUpdate",
                effect=iam.Effect.DENY,
                actions=list(DENIED_SCHEDULER_ACTIONS),
                resources=["*"],
            )
        )
        # ``02`` § 8.1: the worker reads no secret. The demo bearer token is the API's and the
        # destination registry is the sender's; a single role holding the private zone and
        # either secret is exactly what the split exists to prevent.
        self.worker_role.add_to_policy(
            iam.PolicyStatement(
                sid="DenyWorkerSecretReads",
                effect=iam.Effect.DENY,
                actions=list(DENIED_SECRET_READ_ACTIONS),
                resources=["*"],
            )
        )

    # -- the frozen ingress: API Gateway HTTP API, payload format 2.0 ----------------------

    def _create_http_api(self) -> None:
        """The one ingress (deployment contract SS 14, SS 24-27).

        An API Gateway v2 **HTTP API** with a single catch-all ``$default`` route wired to the
        API Lambda through a proxy integration whose payload format version is **explicitly
        2.0** -- the production handler is Mangum payload-v2 and depends on nothing implicit. One
        integration, not one CDK route per FastAPI route: FastAPI stays the route authority.

        The Lambda invoke permission API Gateway needs is added by ``HttpLambdaIntegration``,
        scoped by CDK to this API's own source ARN -- so only this HTTP API may invoke the API
        Lambda, and no API Gateway permission touches the worker, compiler, sender, or watcher
        (SS 25).

        **CORS is deferred, deliberately (SS 26).** The deployed browser origin is unknown until
        the hackathon frontend-hosting decision (P4); rather than invent
        ``Access-Control-Allow-Origin: *``, no ``cors_preflight`` is configured here and the
        frozen origin policy is applied when P4 resolves.

        No Lambda Function URL is created -- the frozen ingress is this HTTP API (SS 27).
        """

        integration = apigwv2_integrations.HttpLambdaIntegration(
            "ApiLambdaProxyIntegration",
            self.api_function,
            payload_format_version=apigwv2.PayloadFormatVersion.VERSION_2_0,
        )
        self.http_api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name=f"chorus-{self._environment_token()}",
            description="Ambient CHORUS demo API (FastAPI on Lambda, payload format 2.0).",
            default_integration=integration,
            create_default_stage=True,
        )

    def _environment_token(self) -> str:
        """The ``{env}`` segment recovered from the API role name (``chorus-api-{env}``)."""

        return self.api_role_name.removeprefix("chorus-api-")

    # -- outputs: only what a later deploy/canary step needs (deployment contract SS 31) ---

    def _declare_outputs(self, *, watcher_alias_arn: str) -> None:
        """Safe deployment outputs. No secret value, no address, no token, no credential."""

        CfnOutput(self, "HttpApiEndpoint", value=self.http_api.api_endpoint)
        CfnOutput(self, "HttpApiId", value=self.http_api.http_api_id)
        CfnOutput(self, "ApiFunctionName", value=self.api_function.function_name)
        CfnOutput(self, "ApiFunctionArn", value=self.api_function.function_arn)
        CfnOutput(self, "WorkerFunctionName", value=self.worker_function.function_name)
        CfnOutput(self, "WorkerFunctionArn", value=self.worker_function.function_arn)
        CfnOutput(self, "WatcherLiveAliasArn", value=watcher_alias_arn)
