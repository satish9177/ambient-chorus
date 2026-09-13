"""The local runnable composition root.

[11-frontend-and-demo.md § The local composition
root](../../../docs/architecture/11-frontend-and-demo.md#the-local-composition-root) freezes what
this wires and what it must never reach:

    storage        in-memory driver, or DynamoDB Local     Not: deployed DynamoDB
    object store   infrastructure/local/objects            Not: S3
    agents         infrastructure/local/*_agent fakes       Not: AgentCore
    sender         infrastructure/local/sender outbox       Not: SES
    scheduler      infrastructure/local/scheduler           Not: EventBridge Scheduler
    inbound        infrastructure/local/inbound_mail        Not: SES inbound
    clock          LogicalDemoClock                         Not: SystemClock
    dispatcher     InProcessOperationDispatcher              Not: worker Lambda

It requires no AWS credentials, makes no network call, and reaches no SES, AgentCore,
EventBridge, S3, or deployed DynamoDB -- :func:`build_local_container` refuses to construct
outside ``test``, ``development``, or ``demo`` (:data:`ALLOWED_ENVIRONMENTS`), so the fakes
cannot be assembled in a deployed process by configuration accident.

This module wires production use cases together, exactly the way ``tests/fixtures`` does --
but it is not itself a test fixture. It is imported by :mod:`chorus_api.asgi`, so it lives under
``src/chorus``, and everything it points at is a shipped local adapter, never a harness.

The frozen ``elevator/v1`` corpus is the only seed this composition knows. A future seed version
would need its own predicted-case-identity logic (see :mod:`chorus.composition.demo_reset`) and
is out of Phase 10's scope.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from chorus_api.dependencies import ApiContainer, DemoActor, InboundReplySurface

from chorus.application.commands.apply_commitment import ApplyCommitment
from chorus.application.commands.approve_action import ApproveAction
from chorus.application.commands.compile_view import CompileView
from chorus.application.commands.create_due_schedule import CreateDueSchedule
from chorus.application.commands.decide_mandate import DecideMandate
from chorus.application.commands.extract_commitment_operation import (
    ExtractCommitment,
    ExtractCommitmentOperationWorker,
)
from chorus.application.commands.ingest_external_reply import (
    IngestExternalReply,
    RecordReplyRejection,
)
from chorus.application.commands.ingest_messages import IngestMessages
from chorus.application.commands.invalidate_action import InvalidateAction
from chorus.application.commands.project_action_outcome import ProjectActionOutcome
from chorus.application.commands.propose_action import ProposeAction
from chorus.application.commands.propose_action_operation import ProposeActionOperationWorker
from chorus.application.commands.propose_mandates import ProposeMandates
from chorus.application.commands.reconcile_send_outcome import ReconcileSendOutcome
from chorus.application.commands.record_commitment_due import RecordCommitmentDue
from chorus.application.commands.run_investigation import RunInvestigation
from chorus.application.commands.run_investigation_operation import InvestigationOperationWorker
from chorus.application.commands.run_monitor import RunMonitor
from chorus.application.commands.run_monitor_operation import MonitorOperationWorker
from chorus.application.commands.send_action import SendAction
from chorus.application.commands.send_action_operation import SendActionOperationWorker
from chorus.application.commands.verify_commitment import VerifyCommitment
from chorus.application.operations import ApplicationOperations
from chorus.application.queries.audit_page import ReadCaseAudit
from chorus.application.queries.case_surface import ReadCaseSurface
from chorus.application.queries.current_action import ReadCurrentAction
from chorus.application.queries.feed import ReadAmbientFeed
from chorus.application.queries.investigation import ReadInvestigation
from chorus.application.queries.mandates import ReadMandateThread
from chorus.application.services.action_renderer import TEMPLATE_VERSION
from chorus.application.services.inbound_mail import (
    InboundMailAttester,
    address_digest,
    inbound_mail_trust_boundary,
)
from chorus.application.services.monitor_snapshots import MonitorSnapshots
from chorus.application.services.safe_evidence import PrepareSafeEvidence
from chorus.application.services.send_authorization import SendAuthorization
from chorus.composition.demo_reset import (
    PERSONA_BY_PSEUDONYM,
    DemoResetService,
    NamespaceStorePurge,
    demo_message_id_generator,
    predict_demo_case_id,
)
from chorus.domain.entities import ActionExecution, DestinationKind, Purpose
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    DestinationId,
    Namespace,
    Uuid4Generator,
    Uuid5Generator,
)
from chorus.domain.time import SystemClock
from chorus.infrastructure.dynamodb.audit import AuditRepository
from chorus.infrastructure.dynamodb.core import CoreRepository
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.idempotency import IdempotencyRepository
from chorus.infrastructure.dynamodb.shareable import ShareableRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.fixtures.inbound_delivery import FixtureReplyDeliverySource
from chorus.infrastructure.fixtures.review_registry import FixtureEvidenceReviewRegistry
from chorus.infrastructure.fixtures.synthetic_feed import SyntheticAmbientAdapter
from chorus.infrastructure.imaging.sanitizer import sanitize_image
from chorus.infrastructure.local.action_agent import CautiousFakeActionAgent
from chorus.infrastructure.local.commitment_agent import LiteralSpanCommitmentExtractor
from chorus.infrastructure.local.demo_clock import LogicalDemoClock
from chorus.infrastructure.local.dispatch import InProcessOperationDispatcher
from chorus.infrastructure.local.inbound_mail import (
    InMemoryInboundRawStore,
    LocalInboundMailAuthenticator,
)
from chorus.infrastructure.local.investigator_agent import CautiousFakeInvestigatorAgent
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.infrastructure.local.monitor_agent import LexicalFakeMonitorAgent
from chorus.infrastructure.local.objects import InMemoryObjectStore
from chorus.infrastructure.local.scheduler import InMemoryDeadlineScheduler
from chorus.infrastructure.local.sender import FilesystemOutboxSender, demo_registry
from chorus.ports.imaging import SafeImage
from chorus.ports.records import SafeInboundMailConfiguration, StoredSafeDestination
from chorus.ports.retention import AuditRetention
from chorus.ports.scopes import ActionScope, CaseScope
from chorus.ports.storage import StorageDriver, TableName
from chorus.privacy.compiler import POLICY_BUILD_HASH, PrivacyCompiler
from chorus.privacy.policy import COMPILER_VERSION, POLICY_VERSION, SafeDestination
from chorus.settings import Environment, Settings, audit_retention_for

ALLOWED_ENVIRONMENTS = frozenset({Environment.TEST, Environment.DEVELOPMENT, Environment.DEMO})

DEMO_COMPOSITION_NAMESPACE = Namespace("DEMO")
"""Fixed for this composition: the frozen reset contract requires the request's own namespace
field to read exactly ``"DEMO"``, and the community it seeds has to live in the namespace that
request names. ``Settings.namespace`` is deployment-wide configuration for concerns this single-
purpose demo container does not have."""

MANAGER_ADDRESS = "property-manager@chorus.invalid"
INBOUND_ADDRESS = "chorus-replies@chorus.invalid"
INBOUND_TRANSPORT = "aws:ses-receipt"
INBOUND_SOURCE_ARN = "arn:aws:ses:us-east-1:000000000000:receipt-rule-set/chorus-demo-local"
FIXTURE_INBOUND_OBJECT_BUCKET = "chorus-local-inbound-fixtures"


class _Sanitizer:
    """The real sanitizer behind the port; local composition exercises production code."""

    def sanitize(self, source: bytes, *, declared_media_type: str) -> SafeImage:
        return sanitize_image(source, declared_media_type=declared_media_type)


@dataclass(slots=True)
class LocalComposition:
    """Everything :func:`build_local_container` assembled, for a CLI or a test to reach into."""

    settings: Settings
    driver: StorageDriver
    container: ApiContainer
    demo_clock: LogicalDemoClock
    dispatcher: InProcessOperationDispatcher
    adapter: SyntheticAmbientAdapter
    scheduler: InMemoryDeadlineScheduler
    """The local one-time deadline scheduler. Exposed so the hero smoke can prove a newly
    accepted commitment causes exactly one scheduler request and a replay causes none."""
    commitment_extractor: LiteralSpanCommitmentExtractor
    """The local commitment-extraction stand-in. Exposed so a recovery test can prove a retried
    extraction re-uses the durable invocation record rather than calling the model again."""


def build_local_container(
    settings: Settings, *, storage: StorageDriver | None = None
) -> ApiContainer:
    """Build the Phase 10 local composition. Convenience wrapper around :func:`build_local`."""

    return build_local(settings, storage=storage).container


def build_local(settings: Settings, *, storage: StorageDriver | None = None) -> LocalComposition:
    if settings.environment not in ALLOWED_ENVIRONMENTS:
        raise RuntimeError(
            "the local composition root refuses to build outside test, development, or demo "
            f"(got {settings.environment.value!r})"
        )

    driver = storage or InMemoryStorageDriver()
    if not isinstance(driver, NamespaceStorePurge):
        raise RuntimeError(
            "the Phase 10 local composition requires a storage driver that can purge one "
            "namespace for demo reset (InMemoryStorageDriver); DynamoDB Local namespace "
            "cleanup is out of scope until Phase 11"
        )
    # A fresh random secret per process, never embedded key material: pagination cursors need
    # only be internally consistent for this process's own lifetime.
    cursors = SignedCursorCodec(secrets.token_bytes(32))
    core = CoreRepository(driver=driver, cursors=cursors)
    shareable = ShareableRepository(driver=driver, cursors=cursors)
    audit = AuditRepository(driver=driver, cursors=cursors, retention=_audit_retention(settings))
    idempotency_core = IdempotencyRepository(driver=driver, table=TableName.CORE)
    idempotency_shareable = IdempotencyRepository(driver=driver, table=TableName.SHAREABLE)
    unit_of_work = StorageUnitOfWork(driver=driver)

    namespace = DEMO_COMPOSITION_NAMESPACE
    adapter = SyntheticAmbientAdapter()
    community_id: CommunityId = adapter.community.community_id
    demo_case_id: CaseId = predict_demo_case_id(
        adapter, namespace=namespace, community_id=community_id
    )

    demo_clock = LogicalDemoClock(instant=adapter.logical_clock_start)
    ids = Uuid4Generator()
    objects = InMemoryObjectStore()

    destination_id = DestinationId(settings.destination_id)
    destination = StoredSafeDestination(
        destination_id=destination_id,
        kind=DestinationKind.PROPERTY_MANAGER,
        registry_version=settings.destination_registry_version,
        routing_token=settings.destination_routing_token,
        display_label=settings.destination_display_label,
    )

    contributor_by_actor = {
        DemoActor(actor): adapter.contributor_ids_by_pseudonym[pseudonym]
        for pseudonym, actor in PERSONA_BY_PSEUDONYM.items()
    }

    reviews = FixtureEvidenceReviewRegistry.from_fixtures(adapter.evidence_fixtures)
    compiler = PrivacyCompiler(
        id_generator_factory=lambda compile_id: Uuid5Generator(
            namespace=compile_id, prefix="compile"
        )
    )
    evidence = PrepareSafeEvidence(objects=objects, sanitizer=_Sanitizer(), ids=ids)

    demo_message_ids = demo_message_id_generator()
    ingest_messages = IngestMessages(
        core=core,
        idempotency=idempotency_core,
        unit_of_work=unit_of_work,
        clock=demo_clock,
        ids=demo_message_ids,
    )
    operations = ApplicationOperations(
        core=core,
        idempotency=idempotency_core,
        unit_of_work=unit_of_work,
        clock=demo_clock,
        ids=ids,
    )
    read_feed = ReadAmbientFeed(core=core, attachments=adapter)
    propose_mandates = ProposeMandates(
        core=core,
        audit=audit,
        idempotency=idempotency_core,
        unit_of_work=unit_of_work,
        clock=demo_clock,
        ids=ids,
    )
    decide_mandate = DecideMandate(
        core=core,
        audit=audit,
        idempotency=idempotency_core,
        unit_of_work=unit_of_work,
        clock=demo_clock,
        ids=ids,
    )
    read_mandate_thread = ReadMandateThread(core=core, clock=demo_clock)

    compile_view = CompileView(
        core=core,
        shareable=shareable,
        audit=audit,
        idempotency=idempotency_shareable,
        unit_of_work=unit_of_work,
        compiler=compiler,
        evidence=evidence,
        reviews=reviews,
        clock=demo_clock,
        ids=ids,
        community_public_label=adapter.community.public_label,
    )
    read_current_action = ReadCurrentAction(
        shareable=shareable, from_identity_id=settings.ses_from_identity_id
    )
    approve_action = ApproveAction(
        core=core,
        shareable=shareable,
        audit=audit,
        idempotency=idempotency_shareable,
        unit_of_work=unit_of_work,
        clock=demo_clock,
        ids=ids,
        destination=destination,
        from_identity_id=settings.ses_from_identity_id,
        policy_version=POLICY_VERSION,
        compiler_version=COMPILER_VERSION,
        policy_build_hash=POLICY_BUILD_HASH,
    )
    invalidate_action = InvalidateAction(
        core=core,
        shareable=shareable,
        audit=audit,
        idempotency=idempotency_shareable,
        unit_of_work=unit_of_work,
        clock=demo_clock,
        ids=ids,
    )

    investigation_destination = SafeDestination(
        destination_id=destination.destination_id,
        kind=destination.kind,
        registry_version=destination.registry_version,
        routing_token=destination.routing_token,
        display_label=destination.display_label,
    )
    run_investigation = RunInvestigation(
        core=core,
        audit=audit,
        idempotency=idempotency_core,
        unit_of_work=unit_of_work,
        agent=CautiousFakeInvestigatorAgent(),
        clock=demo_clock,
        ids=ids,
        community_public_label=adapter.community.public_label,
        destination=investigation_destination,
    )
    investigator_worker = InvestigationOperationWorker(
        operations=operations, run_investigation=run_investigation
    )

    propose_action = ProposeAction(
        core=core,
        shareable=shareable,
        audit=audit,
        idempotency=idempotency_shareable,
        unit_of_work=unit_of_work,
        agent=CautiousFakeActionAgent(),
        clock=demo_clock,
        ids=ids,
        destination=destination,
        from_identity_id=settings.ses_from_identity_id,
        purpose=_PURPOSE,
    )
    proposer_worker = ProposeActionOperationWorker(
        operations=operations, propose_action=propose_action
    )

    registry = demo_registry(
        destination_id=settings.destination_id,
        registry_version=settings.destination_registry_version,
        routing_token=settings.destination_routing_token,
        display_label=settings.destination_display_label,
        address=MANAGER_ADDRESS,
        identity_id=settings.ses_from_identity_id,
        from_address="chorus@chorus.invalid",
        reply_to_address=INBOUND_ADDRESS,
    )
    sender = FilesystemOutboxSender(directory=settings.local_data_dir / "outbox")
    authorization = SendAuthorization(
        core=core,
        shareable=shareable,
        clock=demo_clock,
        policy_version=POLICY_VERSION,
        compiler_version=COMPILER_VERSION,
        policy_build_hash=POLICY_BUILD_HASH,
        purpose=_PURPOSE,
    )
    send_action = SendAction(
        shareable=shareable,
        audit=audit,
        idempotency=idempotency_shareable,
        unit_of_work=unit_of_work,
        authorization=authorization,
        sender=sender,
        registry=registry,
        clock=demo_clock,
        ids=ids,
        destination=destination,
        from_identity_id=settings.ses_from_identity_id,
        configuration_set=settings.ses_configuration_set,
        template_version=_TEMPLATE_VERSION,
    )
    project_outcome = ProjectActionOutcome(
        core=core,
        shareable=shareable,
        audit=audit,
        idempotency=idempotency_shareable,
        unit_of_work=unit_of_work,
        clock=demo_clock,
        ids=ids,
        destination=destination,
    )
    reconcile_outcome = ReconcileSendOutcome(
        shareable=shareable,
        core=core,
        audit=audit,
        idempotency=idempotency_shareable,
        unit_of_work=unit_of_work,
        clock=demo_clock,
        ids=ids,
        configuration_set=settings.ses_configuration_set,
        evidence_trust=None,
    )
    sender_worker = SendActionOperationWorker(
        operations=operations,
        send_action=send_action,
        shareable=shareable,
        project=project_outcome,
        reconcile=reconcile_outcome,
    )

    # -- Phase 9: inbound reply, commitment extraction, verification, demo clock -----------

    raw_messages = InMemoryInboundRawStore()
    authenticator = LocalInboundMailAuthenticator(
        environment=settings.environment,
        transport=INBOUND_TRANSPORT,
        source_arn=INBOUND_SOURCE_ARN,
    )
    inbound_config = SafeInboundMailConfiguration(
        destination=destination,
        destination_address_digest=address_digest(namespace, MANAGER_ADDRESS),
        inbound_address_digest=address_digest(namespace, INBOUND_ADDRESS),
    )
    attester, evidence_verifier = inbound_mail_trust_boundary(
        transport=INBOUND_TRANSPORT,
        source_arn=INBOUND_SOURCE_ARN,
        authenticator=authenticator,
        raw_messages=raw_messages,
        core=core,
        shareable=shareable,
        config=inbound_config,
        namespace=namespace,
        from_identity_id=settings.ses_from_identity_id,
    )
    ingest_reply = IngestExternalReply(
        core=core,
        audit=audit,
        idempotency=idempotency_shareable,
        objects=objects,
        unit_of_work=unit_of_work,
        clock=demo_clock,
        ids=Uuid4Generator(),
        evidence_trust=evidence_verifier,
    )
    record_rejection = RecordReplyRejection(
        audit=audit, unit_of_work=unit_of_work, clock=demo_clock, ids=Uuid4Generator()
    )

    apply_commitment = ApplyCommitment(
        core=core,
        shareable=shareable,
        audit=audit,
        idempotency=idempotency_shareable,
        unit_of_work=unit_of_work,
        clock=demo_clock,
        ids=Uuid4Generator(),
        destination_label=destination.display_label,
        scheduler_environment=settings.scheduler_environment,
    )
    scheduler = InMemoryDeadlineScheduler()
    create_due_schedule = CreateDueSchedule(
        idempotency=idempotency_shareable,
        shareable=shareable,
        audit=audit,
        unit_of_work=unit_of_work,
        scheduler=scheduler,
        clock=demo_clock,
        # P1/P2-2: real wall-clock time for the one arithmetic step that computes when a real
        # EventBridge Scheduler resource should fire -- never ``demo_clock``, which is what the
        # deployed composition also does (``functions/worker/composition.py``). The in-memory
        # scheduler does not care what instant it is handed, but this keeps local and deployed
        # composition expressing the identical split rather than one being a coincidence of
        # ``demo_clock`` also being reachable here.
        wall_clock=SystemClock(),
        ids=Uuid4Generator(),
        scheduler_environment=settings.scheduler_environment,
    )
    commitment_extractor = LiteralSpanCommitmentExtractor()
    extract_commitment = ExtractCommitment(
        core=core,
        agent=commitment_extractor,
        apply=apply_commitment,
        clock=demo_clock,
        policy_version=settings.policy_version,
        destination_label=destination.display_label,
        schedule=create_due_schedule,
        schedule_commitments=shareable,
    )
    extractor_worker = ExtractCommitmentOperationWorker(
        operations=operations, extract=extract_commitment
    )

    verify_commitment = VerifyCommitment(
        core=core,
        shareable=shareable,
        audit=audit,
        idempotency=idempotency_shareable,
        unit_of_work=unit_of_work,
        clock=demo_clock,
        ids=Uuid4Generator(),
    )
    record_commitment_due = RecordCommitmentDue(
        shareable=shareable,
        audit=audit,
        unit_of_work=unit_of_work,
        clock=demo_clock,
        ids=Uuid4Generator(),
    )

    demo_replies = FixtureReplyDeliverySource(
        namespace=namespace,
        community_id=community_id,
        case_id=demo_case_id,
        transport=INBOUND_TRANSPORT,
        source_arn=INBOUND_SOURCE_ARN,
        manager_address=MANAGER_ADDRESS,
        inbound_address=INBOUND_ADDRESS,
        raw_messages=raw_messages,
        resolve_sent_message_id=_resolve_sent_message_id(shareable),
        clock=demo_clock,
    )

    dispatcher = InProcessOperationDispatcher(
        worker=MonitorOperationWorker(
            operations=operations,
            run_monitor=RunMonitor(
                core=core,
                audit=audit,
                idempotency=idempotency_core,
                unit_of_work=unit_of_work,
                agent=LexicalFakeMonitorAgent(),
                attachments=adapter,
                snapshots=MonitorSnapshots(core=core, unit_of_work=unit_of_work),
                clock=demo_clock,
            ),
        ),
        investigator=investigator_worker,
        proposer=proposer_worker,
        sender=sender_worker,
        extractor=extractor_worker,
    )

    reset_service = DemoResetService(
        settings_environment=settings.environment.value,
        adapter=adapter,
        driver=driver,
        core=core,
        shareable=shareable,
        unit_of_work=unit_of_work,
        ingest_messages=ingest_messages,
        message_ids=demo_message_ids,
        scheduler=scheduler,
        outbox_dir=settings.local_data_dir / "outbox",
        objects=objects,
        demo_clock=demo_clock,
        clock=demo_clock,
        namespace=namespace,
        community_id=community_id,
        destination=destination,
        demo_case_id=demo_case_id,
    )

    container = ApiContainer(
        namespace=namespace,
        community_id=community_id,
        destination_id=destination_id,
        destination=destination,
        contributor_by_actor=contributor_by_actor,
        ingest_messages=ingest_messages,
        read_feed=read_feed,
        operations=operations,
        propose_mandates=propose_mandates,
        decide_mandate=decide_mandate,
        read_mandate_thread=read_mandate_thread,
        compile_view=compile_view,
        read_current_action=read_current_action,
        approve_action=approve_action,
        invalidate_action=invalidate_action,
        verify_commitment=verify_commitment,
        inbound_replies=_inbound_reply_surface(
            attester, ingest_reply, record_rejection, demo_replies
        ),
        record_commitment_due=record_commitment_due,
        demo_clock=demo_clock,
        commitments=shareable,
        dispatcher=dispatcher,
        reset_demo=reset_service,
        investigation=ReadInvestigation(core=core, audit=audit, shareable=shareable),
        audit_page=ReadCaseAudit(audit=audit),
        case_surface=ReadCaseSurface(core=core, shareable=shareable, audit=audit),
    )
    return LocalComposition(
        settings=settings,
        driver=driver,
        container=container,
        demo_clock=demo_clock,
        dispatcher=dispatcher,
        adapter=adapter,
        scheduler=scheduler,
        commitment_extractor=commitment_extractor,
    )


def _inbound_reply_surface(
    attester: InboundMailAttester,
    ingest: IngestExternalReply,
    record_rejection: RecordReplyRejection,
    demo_replies: FixtureReplyDeliverySource,
) -> InboundReplySurface:
    return InboundReplySurface(
        attester=attester,
        ingest=ingest,
        record_rejection=record_rejection,
        demo_replies=demo_replies,
    )


def _resolve_sent_message_id(
    shareable: ShareableRepository,
) -> Callable[[CaseScope], Awaitable[str]]:
    async def resolve(scope: CaseScope) -> str:
        pointer = await shareable.load_current_action_pointer(scope)
        if pointer is None:
            raise RuntimeError("no current action pointer exists for the demo case")
        action_scope = ActionScope(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            action_id=pointer.action_id,
        )
        execution: ActionExecution = await shareable.load_execution(
            action_scope, pointer.execution_id
        )
        if execution.ses_message_id is None:
            raise RuntimeError("the demo case has no sent message to correlate a reply against")
        return execution.ses_message_id

    return resolve


def _audit_retention(settings: Settings) -> AuditRetention:
    return audit_retention_for(settings.environment)


_PURPOSE = Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE
_TEMPLATE_VERSION = TEMPLATE_VERSION


__all__ = ["ALLOWED_ENVIRONMENTS", "LocalComposition", "build_local", "build_local_container"]
