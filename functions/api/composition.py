"""Wire the deployed request path: what a route may reach, and what it provably cannot.

The API is the broadest principal in the system, so its boundary is drawn twice -- once in IAM
and once here, by what this root constructs. Three of those constructions are the whole point,
and each one exists because the deployed API's policy makes the local arrangement impossible:

* **the compiler is invoked, not called.** The API holds an explicit
  ``Deny dynamodb:PutItem/UpdateItem/DeleteItem`` on the view prefixes, so an in-process
  :class:`~chorus.application.commands.compile_view.CompileView` here could only fail, and only
  in an account. The compile route reaches
  :class:`~chorus.application.compile_contract.RemoteCompileView` instead;
* **the worker is dispatched to, not run.** Every agent-invoking operation is handed over with
  ``InvocationType="Event"`` and the route returns ``202``. The API holds no
  ``bedrock-agentcore:InvokeAgentRuntime`` -- it is denied outright -- so there is no route that
  could invoke a runtime even by mistake;
* **the watcher is invoked synchronously.** ``POST /v1/demo/clock/advance`` promises the
  watcher's outcome in its response body, so it advances the durable clock and then invokes the
  watcher's ``live`` alias with ``InvocationType="RequestResponse"``. Routing it through the
  async worker to avoid that one grant would change what the endpoint promises.

What is deliberately absent
----------------------------
No SES client. No Bedrock or AgentCore client. No scheduler client. No destination registry --
the address secret is the sender's and this principal is denied it by name. No privacy compiler,
no image sanitizer, and no safe-evidence service: those live where views are made.

The one secret it does read is the demo access token's **digest**, through one
``secretsmanager:GetSecretValue`` on one ARN, and the token itself never enters an environment
variable, a log line, or a response.

Two things fail closed at construction rather than at first use
----------------------------------------------------------------
``CHORUS_DYNAMODB_ENDPOINT`` must be unset in a deployed composition. The setting's own default
is now ``None`` (the deployed value); ``api_settings`` still refuses to construct if a
deployment *does* set it, because a local DynamoDB endpoint in an account is a request path
pointed at nothing. The demo access secret ARN, the cursor-signing secret ARN, and the worker /
compiler / watcher function ARNs must all be present. All are asserted here (deployment
contract § 14).
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus_api.dependencies import ApiContainer, DemoActor, RequestLogicalTime

from chorus.application.commands.approve_action import ApproveAction
from chorus.application.commands.decide_mandate import DecideMandate
from chorus.application.commands.ingest_messages import IngestMessages
from chorus.application.commands.invalidate_action import InvalidateAction
from chorus.application.commands.propose_mandates import ProposeMandates
from chorus.application.commands.verify_commitment import VerifyCommitment
from chorus.application.compile_contract import RemoteCompileView
from chorus.application.dispatch import RemoteOperationDispatcher
from chorus.application.operations import ApplicationOperations
from chorus.application.queries.audit_page import ReadCaseAudit
from chorus.application.queries.case_surface import ReadCaseSurface
from chorus.application.queries.current_action import ReadCurrentAction
from chorus.application.queries.feed import ReadAmbientFeed
from chorus.application.queries.investigation import ReadInvestigation
from chorus.application.queries.mandates import ReadMandateThread
from chorus.application.watcher_contract import RemoteRecordCommitmentDue
from chorus.domain.entities import DestinationKind
from chorus.domain.ids import (
    CommunityId,
    ContributorId,
    DestinationId,
    IdGenerator,
    Namespace,
    Uuid4Generator,
)
from chorus.infrastructure.dynamodb.audit import AuditRepository
from chorus.infrastructure.dynamodb.client import create_dynamodb_client
from chorus.infrastructure.dynamodb.core import CoreRepository
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.demo_clock import DynamoDbDemoClockStore
from chorus.infrastructure.dynamodb.demo_reset_store import DynamoDbDemoManifestRegistrar
from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver
from chorus.infrastructure.dynamodb.idempotency import IdempotencyRepository
from chorus.infrastructure.dynamodb.shareable import ShareableRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.fixtures.synthetic_feed import SyntheticAmbientAdapter
from chorus.infrastructure.lambdas.invoker import (
    AsynchronousLambdaInvoker,
    SynchronousLambdaInvoker,
    create_lambda_client,
)
from chorus.infrastructure.lambdas.transport_budgets import (
    API_DOWNSTREAM_CONNECT_TIMEOUT_SECONDS,
    API_DOWNSTREAM_READ_TIMEOUT_SECONDS,
)
from chorus.infrastructure.persistent_clock import PersistentDemoClock, ScopedLogicalClock
from chorus.infrastructure.secrets.cursor_signing import load_cursor_signing_key
from chorus.infrastructure.secrets.demo_access import (
    SecretsManagerDemoAccess,
    create_secrets_client,
)
from chorus.ports.records import StoredSafeDestination
from chorus.ports.retention import AuditRetention
from chorus.ports.storage import TableName
from chorus.privacy.compiler import POLICY_BUILD_HASH
from chorus.privacy.policy import COMPILER_VERSION, POLICY_VERSION
from chorus.settings import Settings


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiSettings:
    """Everything the deployed request path needs, and nothing it could decide policy from."""

    region: str
    namespace: str
    community_id: CommunityId
    core_table: str
    shareable_table: str
    audit_table: str
    destination: StoredSafeDestination
    from_identity_id: str
    demo_access_secret_arn: str
    worker_function_arn: str
    compiler_function_arn: str
    watcher_function_arn: str
    """The watcher's **``:live`` alias** ARN. Never the unqualified function and never a numeric
    version: rollback repoints the alias, and the API's single ``lambda:InvokeFunction`` grant
    names the alias exactly (deployment contract § 8.1)."""

    cursor_signing_secret_arn: str
    """The one Secrets Manager identity every pagination cursor's HMAC key is drawn from.

    Read once, synchronously, when the request path is composed
    (:func:`build_api_container`), and never regenerated per cold start (Phase 11 batch 4
    repair, P2-5) -- see :mod:`chorus.infrastructure.secrets.cursor_signing`.
    """


def api_settings(settings: Settings) -> ApiSettings:
    """Map process configuration onto the request path's settings, refusing what is unsafe.

    ``CHORUS_DYNAMODB_ENDPOINT`` is asserted unset. Its default is ``None`` (the deployed
    value); a deployment that *sets* it -- pointing the request path at a local endpoint -- is
    refused here rather than at the first read.
    """

    if settings.dynamodb_endpoint is not None:
        raise ValueError("a deployed API must not configure a DynamoDB endpoint")
    return ApiSettings(
        region=settings.aws_region,
        namespace=settings.namespace,
        community_id=CommunityId(SyntheticAmbientAdapter().community.community_id.value),
        core_table=settings.core_table,
        shareable_table=settings.shareable_table,
        audit_table=settings.audit_table,
        destination=StoredSafeDestination(
            destination_id=DestinationId(settings.destination_id),
            kind=DestinationKind.PROPERTY_MANAGER,
            registry_version=settings.destination_registry_version,
            routing_token=settings.destination_routing_token,
            display_label=settings.destination_display_label,
        ),
        from_identity_id=settings.ses_from_identity_id,
        demo_access_secret_arn=_require(
            settings.demo_access_secret_arn, "the demo access secret ARN"
        ),
        worker_function_arn=_require(settings.worker_function_arn, "the worker function ARN"),
        compiler_function_arn=_require(settings.compiler_function_arn, "the compiler function ARN"),
        watcher_function_arn=_require(settings.watcher_function_arn, "the watcher live alias ARN"),
        cursor_signing_secret_arn=_require(
            settings.cursor_signing_secret_arn, "the cursor signing secret ARN"
        ),
    )


def _require(value: str | None, what: str) -> str:
    """Refuse a missing deployment value at construction, not at the first request."""

    if not value:
        raise ValueError(f"a deployed API needs {what}")
    return value


def build_api_container(settings: ApiSettings, *, ids: IdGenerator | None = None) -> ApiContainer:
    """Construct everything a deployed route may reach, over deployed adapters."""

    namespace = Namespace(settings.namespace)
    generator = ids or Uuid4Generator()
    scope = ScopedLogicalClock()

    driver = DynamoDbStorageDriver(
        client=create_dynamodb_client(region_name=settings.region),
        table_names={
            TableName.CORE: settings.core_table,
            TableName.SHAREABLE: settings.shareable_table,
            TableName.AUDIT: settings.audit_table,
        },
    )
    secrets_client = create_secrets_client(region_name=settings.region)
    # One blocking read, made exactly once per execution environment, before anything is built
    # against the codec it feeds -- an unreadable or malformed secret fails this composition
    # closed rather than falling back to a key generated fresh for the occasion (P2-5).
    cursors = SignedCursorCodec(
        secret=load_cursor_signing_key(
            client=secrets_client, secret_id=settings.cursor_signing_secret_arn
        )
    )
    core = CoreRepository(driver=driver, cursors=cursors)
    shareable = ShareableRepository(driver=driver, cursors=cursors)
    audit = AuditRepository(driver=driver, cursors=cursors, retention=AuditRetention.demo())
    idempotency_core = IdempotencyRepository(driver=driver, table=TableName.CORE)
    idempotency_shareable = IdempotencyRepository(driver=driver, table=TableName.SHAREABLE)
    unit_of_work = StorageUnitOfWork(driver=driver)
    adapter = SyntheticAmbientAdapter()

    # One client for every downstream invoke on the request path (compiler compile, watcher
    # clock-advance, and the async worker dispatch), pinned to the API's caller-specific
    # transport budget so a slow compiler or watcher raises a typed dependency failure well
    # before API Gateway's 30 s ceiling terminates this Lambda (review P2-9).
    lambda_client = create_lambda_client(
        region_name=settings.region,
        connect_timeout=API_DOWNSTREAM_CONNECT_TIMEOUT_SECONDS,
        read_timeout=API_DOWNSTREAM_READ_TIMEOUT_SECONDS,
    )
    clock_store = DynamoDbDemoClockStore(driver=driver, namespace=namespace)
    # Deployed demo only: the request path is where every OPERATION partition is first created
    # (``reserve_start`` / ``complete_start`` in the routes), so the reset-inventory marker is
    # appended to that same ``create-operation`` transaction here -- a crash can never leave an
    # OPERATION partition durable and unregistered (final completion repair A1). ``None`` in
    # every other namespace, and the transaction is then byte-for-byte unchanged.
    partition_registrar = (
        DynamoDbDemoManifestRegistrar(driver=driver, namespace=namespace)
        if namespace.value == "DEMO"
        else None
    )

    return ApiContainer(
        namespace=namespace,
        community_id=settings.community_id,
        destination_id=settings.destination.destination_id,
        destination=settings.destination,
        # The deployed request path resolves personas from the seeded corpus, exactly as the
        # local one does: the mapping is configuration and never a request field.
        contributor_by_actor=_personas(adapter),
        ingest_messages=IngestMessages(
            core=core,
            idempotency=idempotency_core,
            unit_of_work=unit_of_work,
            clock=scope,
            ids=generator,
        ),
        read_feed=ReadAmbientFeed(core=core, attachments=adapter),
        operations=ApplicationOperations(
            core=core,
            idempotency=idempotency_core,
            unit_of_work=unit_of_work,
            clock=scope,
            ids=generator,
            partition_registrar=partition_registrar,
        ),
        propose_mandates=ProposeMandates(
            core=core,
            audit=audit,
            idempotency=idempotency_core,
            unit_of_work=unit_of_work,
            clock=scope,
            ids=generator,
        ),
        decide_mandate=DecideMandate(
            core=core,
            audit=audit,
            idempotency=idempotency_core,
            unit_of_work=unit_of_work,
            clock=scope,
            ids=generator,
        ),
        read_mandate_thread=ReadMandateThread(core=core, clock=scope),
        # Not a ``CompileView``. The deployed API cannot write a view partition, so the compile
        # route crosses a Lambda boundary to the one principal that can.
        compile_view=RemoteCompileView(
            invoker=SynchronousLambdaInvoker(
                client=lambda_client, function_name=settings.compiler_function_arn
            )
        ),
        read_current_action=ReadCurrentAction(
            shareable=shareable, from_identity_id=settings.from_identity_id
        ),
        approve_action=ApproveAction(
            core=core,
            shareable=shareable,
            audit=audit,
            idempotency=idempotency_shareable,
            unit_of_work=unit_of_work,
            clock=scope,
            ids=generator,
            destination=settings.destination,
            from_identity_id=settings.from_identity_id,
            policy_version=POLICY_VERSION,
            compiler_version=COMPILER_VERSION,
            policy_build_hash=POLICY_BUILD_HASH,
        ),
        invalidate_action=InvalidateAction(
            core=core,
            shareable=shareable,
            audit=audit,
            idempotency=idempotency_shareable,
            unit_of_work=unit_of_work,
            clock=scope,
            ids=generator,
        ),
        verify_commitment=VerifyCommitment(
            core=core,
            shareable=shareable,
            audit=audit,
            idempotency=idempotency_shareable,
            unit_of_work=unit_of_work,
            clock=scope,
            ids=generator,
        ),
        # Deliberately ``None``: the inbound reply boundary is ADR-030's, and its transport,
        # its attester, and its receipt decoding are a later batch. A route that finds it
        # absent answers 503 rather than accepting an unauthenticated delivery.
        inbound_replies=None,
        record_commitment_due=RemoteRecordCommitmentDue(
            invoker=SynchronousLambdaInvoker(
                client=lambda_client, function_name=settings.watcher_function_arn
            )
        ),
        demo_clock=PersistentDemoClock(store=clock_store, scope=scope),
        commitments=shareable,
        dispatcher=RemoteOperationDispatcher(
            invoker=AsynchronousLambdaInvoker(
                client=lambda_client, function_name=settings.worker_function_arn
            )
        ),
        # Deliberately ``None``: reset is a dedicated principal behind its own confirmation,
        # and the request path is not it (deployment contract § 12).
        reset_demo=None,
        investigation=ReadInvestigation(core=core, audit=audit, shareable=shareable),
        audit_page=ReadCaseAudit(audit=audit),
        case_surface=ReadCaseSurface(core=core, shareable=shareable, audit=audit),
        access=SecretsManagerDemoAccess(
            client=secrets_client,
            secret_id=settings.demo_access_secret_arn,
        ),
        logical_time=RequestLogicalTime(store=clock_store, scope=scope),
    )


def _personas(adapter: SyntheticAmbientAdapter) -> dict[DemoActor, ContributorId]:
    """Map each seeded pseudonym onto the persona that may act as it."""

    from chorus.composition.demo_reset import (
        PERSONA_BY_PSEUDONYM,
    )

    return {
        DemoActor(actor): adapter.contributor_ids_by_pseudonym[pseudonym]
        for pseudonym, actor in PERSONA_BY_PSEUDONYM.items()
    }


__all__ = ["ApiSettings", "api_settings", "build_api_container"]
