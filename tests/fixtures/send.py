"""A Phase-8 harness: a real compile, a real proposal, then the decision and the send.

It extends the Phase-7 harness rather than fabricating a proposal, so every hash a decision
binds and every hash a send re-derives came out of the production authorities that produced
them. The approval an approver makes here is the approval the sender verifies, digest for
digest, and that is the only way these tests can mean what they say.

The scripted sender is the load-bearing seam. It answers with outcomes no live service would
produce on demand -- an ambiguous transport failure, a definite rejection, an exception nobody
enumerated -- and, more importantly, it **counts deliberate calls**. That count is the number
the whole phase is about, and a test that inferred it from a durable state could not tell a
definite failure that reached SES from one that never did.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from uuid import uuid4

from chorus.application.commands.approve_action import (
    ApproveAction,
    ApproveActionCommand,
    ApproveActionResult,
)
from chorus.application.commands.invalidate_action import (
    InvalidateAction,
    InvalidateActionCommand,
    InvalidateActionResult,
)
from chorus.application.commands.project_action_outcome import ProjectActionOutcome
from chorus.application.commands.reconcile_from_ses_event import (
    ReconcileFromSesEvent,
    ReconcileFromSesEventCommand,
)
from chorus.application.commands.reconcile_send_outcome import ReconcileSendOutcome
from chorus.application.commands.send_action import (
    SendAction,
    SendActionCommand,
    SendActionResult,
)
from chorus.application.commands.send_action_operation import SendActionOperationWorker
from chorus.application.operations import ApplicationOperations, StartReservation
from chorus.application.services.action_authorization import (
    send_request_hash,
    send_start_key_hash,
)
from chorus.application.services.action_renderer import TEMPLATE_VERSION
from chorus.application.services.send_authorization import SendAuthorization
from chorus.application.services.ses_events import (
    CONFIGURATION_SET_TAG,
    EXECUTION_TAG_NAME,
    AttestedSesEventEvidence,
    SesEventAttester,
    SesEventEvidenceVerifier,
    ses_event_trust_boundary,
)
from chorus.application.services.ses_message import execution_tag_value
from chorus.domain.entities import (
    ActionExecution,
    ActionProposal,
    ApplicationOperationKind,
    ApprovalDecision,
    ApproverAssurance,
    Purpose,
)
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    ExecutionId,
    OperationId,
    Sha256Digest,
    Uuid4Generator,
    Uuid5Generator,
)
from chorus.infrastructure.local.sender import (
    InMemoryDestinationRegistry,
    ScriptedSender,
    demo_registry,
)
from chorus.ports.idempotency import IdempotentCommand
from chorus.ports.operations import SendActionOperationJob
from chorus.ports.records import (
    CurrentActionPointer,
    StoredSafeDestination,
    StoredShareableView,
)
from chorus.ports.scopes import ActionScope, CaseScope
from chorus.ports.sender import DestinationRegistryPort, EmailSenderPort
from chorus.ports.ses_events import SesEventTransportAuthenticator, SesEventTransportContext
from chorus.privacy.compiler import POLICY_BUILD_HASH
from chorus.privacy.policy import COMPILER_VERSION, POLICY_VERSION
from tests.fixtures.action import ACTOR_HASH, FROM_IDENTITY_ID, ActionHarness
from tests.fixtures.compile import harness_uuid
from tests.fixtures.elevator import NAMESPACE

CONFIGURATION_SET = "chorus-test"
APPROVER_HASH = Sha256Digest("sha256:" + "b" * 64)
"""A distinct actor hash from the presenter's, so an audit trail can tell them apart."""

EVENT_TRANSPORT = "aws:sns"
EVENT_SOURCE_ARN = "arn:aws:sns:us-east-1:000000000000:chorus-test-ses-events"
"""The event destination this fixture's deployment configured. Phase 11 builds the real one."""

FOREIGN_SOURCE_ARN = "arn:aws:sns:us-east-1:000000000000:somebody-elses-topic"


@dataclass(slots=True)
class ScriptedTransportAuthenticator:
    """Stands in for the Phase-11 transport authenticator, and stands in for nothing else.

    The real one verifies an SNS signature against the topic's signing certificate, or reads an
    EventBridge invocation identity. Neither resource exists in Phase 8, so what the tests need
    is a seam that can answer *yes* and *no* on demand -- the same role
    :class:`chorus.infrastructure.local.sender.ScriptedSender` plays for SES itself.

    It deliberately lives in the fixtures and not in ``src``: an always-yes authenticator that
    shipped in the application would be indistinguishable, at the call site, from a real one.
    """

    accepts: bool = True
    calls: int = 0

    async def authenticate(self, context: SesEventTransportContext) -> bool:
        self.calls += 1
        return self.accepts


def ses_event_envelope(
    *,
    event_type: str = "Delivery",
    configuration_set: str = CONFIGURATION_SET,
    execution_tag: str,
    message_id: str | None = "0100016a-proof",
) -> dict[str, object]:
    """The shape SES publishes to a configuration-set event destination, tags and all."""

    mail: dict[str, object] = {
        "timestamp": "2026-09-06T12:00:00.000Z",
        "tags": {
            CONFIGURATION_SET_TAG: [configuration_set],
            EXECUTION_TAG_NAME: [execution_tag],
        },
    }
    if message_id is not None:
        mail["messageId"] = message_id
    return {"eventType": event_type, "mail": mail}


@dataclass(slots=True)
class SendHarness:
    """A proposed case, plus every Phase-8 use case wired over the same driver."""

    action: ActionHarness
    sender: ScriptedSender = field(default_factory=ScriptedSender)
    authenticator: SesEventTransportAuthenticator | None = field(
        default_factory=ScriptedTransportAuthenticator
    )
    """The transport authority the SES event adapter is built over, or ``None`` for none wired.

    ``None`` is the honest Phase-8 deployment: no authenticator exists, so nothing can mint
    evidence and a quarantine stays a quarantine.
    """

    trust: tuple[SesEventAttester, SesEventEvidenceVerifier] | None = None
    """One boundary per harness, built on first use so both halves share one key."""

    registry: DestinationRegistryPort | None = None
    """Defaults to a registry that agrees with the compile fixture's own routing triple.

    Built lazily rather than at construction, because the triple belongs to the *view* the
    compiler authorized and the fixture has not compiled one yet when the harness is made. A
    default registry that disagreed would make every send fail with a routing denial, which is
    a real failure mode -- and would hide every other one behind it.
    """

    # -- setup -----------------------------------------------------------------------------

    async def prepare(self) -> StoredShareableView:
        """Compile a real view, make the case ready, and apply one real proposal."""

        view = await self.action.prepare()
        command = await self.action.command()
        await self.action.propose_action().execute(command)
        return view

    # -- accessors --------------------------------------------------------------------------

    @property
    def scope(self) -> CaseScope:
        return self.action.scope

    async def pointer(self) -> CurrentActionPointer:
        pointer = await self.action.compile.shareable.load_current_action_pointer(self.action.scope)
        assert pointer is not None, "no current action pointer exists"
        return pointer

    async def action_scope(self) -> ActionScope:
        pointer = await self.pointer()
        return ActionScope(
            namespace=NAMESPACE,
            community_id=self.action.compile.case.community_id,
            case_id=self.action.case_id,
            action_id=pointer.action_id,
        )

    async def proposal(self) -> ActionProposal:
        return await self.action.compile.shareable.load_proposal(await self.action_scope())

    async def execution(self) -> ActionExecution:
        pointer = await self.pointer()
        return await self.action.compile.shareable.load_execution(
            await self.action_scope(), pointer.execution_id
        )

    # -- use cases ---------------------------------------------------------------------------

    def approve_action(self, *, destination: StoredSafeDestination | None = None) -> ApproveAction:
        """``destination`` overrides the deployment's *current* registry entry.

        That is the value ADR-023 check 7 compares the view against by exact equality, so
        substituting it is how a test moves deployment configuration out from under an
        already-committed proposal without rewriting the immutable artifact.
        """

        return ApproveAction(
            core=self.action.compile.core,
            shareable=self.action.compile.shareable,
            audit=self.action.compile.audit,
            idempotency=self.action.compile.idempotency,
            unit_of_work=self.action.unit_of_work,  # type: ignore[arg-type]
            clock=self.action.compile.clock,
            ids=Uuid4Generator(),
            destination=destination or self.action.compile.stored_destination(),
            from_identity_id=FROM_IDENTITY_ID,
            policy_version=POLICY_VERSION,
            compiler_version=COMPILER_VERSION,
            policy_build_hash=POLICY_BUILD_HASH,
        )

    def invalidate_action(self) -> InvalidateAction:
        return InvalidateAction(
            core=self.action.compile.core,
            shareable=self.action.compile.shareable,
            audit=self.action.compile.audit,
            idempotency=self.action.compile.idempotency,
            unit_of_work=self.action.unit_of_work,  # type: ignore[arg-type]
            clock=self.action.compile.clock,
            ids=Uuid4Generator(),
        )

    def authorization(self) -> SendAuthorization:
        return SendAuthorization(
            core=self.action.compile.core,
            shareable=self.action.compile.shareable,
            clock=self.action.compile.clock,
            policy_version=POLICY_VERSION,
            compiler_version=COMPILER_VERSION,
            policy_build_hash=POLICY_BUILD_HASH,
            purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
        )

    def send_action(
        self,
        *,
        sender: EmailSenderPort | None = None,
        registry: DestinationRegistryPort | None = None,
        destination: StoredSafeDestination | None = None,
        from_identity_id: str = FROM_IDENTITY_ID,
        template_version: str = TEMPLATE_VERSION,
    ) -> SendAction:
        """Every deployment-owned value the send re-checks is overridable here.

        The four of them -- the destination entry, the sending identity handle, the template
        version, and the registry the recipient resolves through -- are exactly the late
        changes ADR-025 SS 5 enumerates, and each has to be movable independently for its own
        failure code to be provable rather than merely plausible.
        """

        return SendAction(
            shareable=self.action.compile.shareable,
            audit=self.action.compile.audit,
            idempotency=self.action.compile.idempotency,
            unit_of_work=self.action.unit_of_work,  # type: ignore[arg-type]
            authorization=self.authorization(),
            sender=sender or self.sender,
            registry=registry or self.registry or self.default_registry(),
            clock=self.action.compile.clock,
            ids=Uuid4Generator(),
            destination=destination or self.action.compile.stored_destination(),
            from_identity_id=from_identity_id,
            configuration_set=CONFIGURATION_SET,
            template_version=template_version,
        )

    # -- the SES event trust boundary --------------------------------------------------------

    def ses_trust(self) -> tuple[SesEventAttester, SesEventEvidenceVerifier]:
        """The harness's one attester/verifier pair, so a minted attestation verifies here."""

        if self.trust is None:
            self.trust = ses_event_trust_boundary(
                transport=EVENT_TRANSPORT,
                source_arn=EVENT_SOURCE_ARN,
                authenticator=self.authenticator,
            )
        return self.trust

    def execution_tag(self, execution_id: ExecutionId) -> str:
        return execution_tag_value(
            namespace=self.action.compile.case.namespace, execution_id=execution_id
        )

    def delivery(
        self,
        *,
        envelope: dict[str, object],
        transport: str = EVENT_TRANSPORT,
        source_arn: str = EVENT_SOURCE_ARN,
    ) -> SesEventTransportContext:
        return SesEventTransportContext(
            transport=transport, source_arn=source_arn, envelope=envelope
        )

    async def attest(
        self,
        *,
        execution_tag: str,
        event_type: str = "Delivery",
        configuration_set: str = CONFIGURATION_SET,
        message_id: str | None = "0100016a-proof",
        transport: str = EVENT_TRANSPORT,
        source_arn: str = EVENT_SOURCE_ARN,
    ) -> AttestedSesEventEvidence:
        """Mint evidence the way the adapter does -- through the boundary, never around it."""

        attester, _ = self.ses_trust()
        return await attester.attest(
            self.delivery(
                envelope=ses_event_envelope(
                    event_type=event_type,
                    configuration_set=configuration_set,
                    execution_tag=execution_tag,
                    message_id=message_id,
                ),
                transport=transport,
                source_arn=source_arn,
            )
        )

    def ses_event_caller(self) -> ReconcileFromSesEvent:
        """The trusted adapter: the attesting half plus the reconciliation command."""

        attester, _ = self.ses_trust()
        return ReconcileFromSesEvent(reconcile=self.reconcile(), attester=attester)

    async def ses_event_command(
        self,
        execution_id: ExecutionId,
        delivery: SesEventTransportContext,
        *,
        actor_id_hash: Sha256Digest = APPROVER_HASH,
    ) -> ReconcileFromSesEventCommand:
        pointer = await self.pointer()
        return ReconcileFromSesEventCommand(
            namespace=self.action.compile.case.namespace,
            community_id=self.action.compile.case.community_id,
            case_id=self.action.case_id,
            action_id=pointer.action_id,
            execution_id=execution_id,
            actor_id_hash=actor_id_hash,
            correlation_id=uuid4(),
            delivery=delivery,
        )

    def reconcile(self, *, trusted: bool = True) -> ReconcileSendOutcome:
        """``trusted=False`` is a deployment with no event boundary wired at all."""

        return ReconcileSendOutcome(
            shareable=self.action.compile.shareable,
            core=self.action.compile.core,
            audit=self.action.compile.audit,
            idempotency=self.action.compile.idempotency,
            unit_of_work=self.action.unit_of_work,  # type: ignore[arg-type]
            clock=self.action.compile.clock,
            ids=Uuid4Generator(),
            configuration_set=CONFIGURATION_SET,
            evidence_trust=self.ses_trust()[1] if trusted else None,
        )

    def project(self, *, destination: StoredSafeDestination | None = None) -> ProjectActionOutcome:
        """``destination`` overrides the registry entry recorded on the outbound locator.

        Address-free, like the entry the approval binds. It is overridable for the same reason
        the approval's is: the locator records the registry as it stood *at the send*, and a
        test that moves it is testing exactly that.
        """

        return ProjectActionOutcome(
            core=self.action.compile.core,
            shareable=self.action.compile.shareable,
            audit=self.action.compile.audit,
            idempotency=self.action.compile.idempotency,
            unit_of_work=self.action.unit_of_work,  # type: ignore[arg-type]
            clock=self.action.compile.clock,
            ids=Uuid4Generator(),
            destination=destination or self.action.compile.stored_destination(),
        )

    def operations(self) -> ApplicationOperations:
        return ApplicationOperations(
            core=self.action.compile.core,
            idempotency=self.action.compile.idempotency,
            unit_of_work=self.action.compile.unit_of_work,
            clock=self.action.compile.clock,
            ids=Uuid5Generator(namespace=harness_uuid("send-operation-ids"), prefix="op"),
        )

    def worker(self, *, sender: EmailSenderPort | None = None) -> SendActionOperationWorker:
        return SendActionOperationWorker(
            operations=self.operations(),
            send_action=self.send_action(sender=sender),
            shareable=self.action.compile.shareable,
            project=self.project(),
            reconcile=self.reconcile(),
        )

    # -- commands ------------------------------------------------------------------------------

    async def approval_command(
        self,
        *,
        decision: ApprovalDecision = ApprovalDecision.APPROVED,
        expected_execution_version: int | None = None,
        execution_id: ExecutionId | None = None,
        proposal_hash: Sha256Digest | None = None,
        view_hash: Sha256Digest | None = None,
        preview_hash: Sha256Digest | None = None,
        action_id: ActionId | None = None,
        idempotency_key: str = "approve-key-0001",
    ) -> ApproveActionCommand:
        """The frozen approval body, defaulted to exactly what the human would have seen."""

        pointer = await self.pointer()
        proposal = await self.proposal()
        execution = await self.execution()
        return ApproveActionCommand(
            namespace=NAMESPACE,
            community_id=self.action.compile.case.community_id,
            case_id=self.action.case_id,
            action_id=action_id or pointer.action_id,
            decision=decision,
            expected_execution_version=(
                execution.version
                if expected_execution_version is None
                else expected_execution_version
            ),
            execution_id=execution_id or pointer.execution_id,
            view_hash=view_hash or proposal.view_hash,
            proposal_hash=proposal_hash or proposal.proposal_hash,
            preview_hash=preview_hash or proposal.preview_hash,
            approver_id_hash=APPROVER_HASH,
            approver_assurance=ApproverAssurance.DEMO_SHARED_TOKEN,
            correlation_id=uuid4(),
            idempotency_key=idempotency_key,
        )

    async def approve(self, **overrides: object) -> ApproveActionResult:
        return await self.approve_action().execute(
            await self.approval_command(**overrides)  # type: ignore[arg-type]
        )

    async def reject(self, **overrides: object) -> ApproveActionResult:
        return await self.approve_action().execute(
            await self.approval_command(decision=ApprovalDecision.REJECTED, **overrides)  # type: ignore[arg-type]
        )

    async def invalidation_command(
        self,
        *,
        expected_execution_version: int | None = None,
        proposal_hash: Sha256Digest | None = None,
        idempotency_key: str = "invalidate-key-0001",
    ) -> InvalidateActionCommand:
        pointer = await self.pointer()
        execution = await self.execution()
        return InvalidateActionCommand(
            namespace=NAMESPACE,
            community_id=self.action.compile.case.community_id,
            case_id=self.action.case_id,
            action_id=pointer.action_id,
            expected_execution_version=(
                execution.version
                if expected_execution_version is None
                else expected_execution_version
            ),
            proposal_hash=proposal_hash or pointer.proposal_hash,
            actor_id_hash=APPROVER_HASH,
            correlation_id=uuid4(),
            idempotency_key=idempotency_key,
        )

    async def invalidate(self, **overrides: object) -> InvalidateActionResult:
        return await self.invalidate_action().execute(
            await self.invalidation_command(**overrides)  # type: ignore[arg-type]
        )

    async def send_command(
        self,
        *,
        approval_id: ApprovalId | None = None,
        expected_execution_version: int | None = None,
        idempotency_key: str = "send-key-0001",
    ) -> SendActionCommand:
        pointer = await self.pointer()
        execution = await self.execution()
        resolved = approval_id or execution.approval_id
        assert resolved is not None, "the execution carries no approval to send under"
        return SendActionCommand(
            namespace=NAMESPACE,
            community_id=self.action.compile.case.community_id,
            case_id=self.action.case_id,
            action_id=pointer.action_id,
            execution_id=pointer.execution_id,
            approval_id=resolved,
            expected_execution_version=(
                execution.version
                if expected_execution_version is None
                else expected_execution_version
            ),
            actor_id_hash=ACTOR_HASH,
            correlation_id=uuid4(),
            idempotency_key=idempotency_key,
        )

    async def send(
        self, *, sender: EmailSenderPort | None = None, **overrides: object
    ) -> SendActionResult:
        return await self.send_action(sender=sender).execute(
            await self.send_command(**overrides)  # type: ignore[arg-type]
        )

    async def send_job(self, *, idempotency_key: str = "send-key-0001") -> SendActionOperationJob:
        """Create the durable ``SEND_ACTION`` operation exactly as the route does, and its job.

        The worker recovery tests have to go *through* the worker, and the worker's first act is
        to load the operation the job names. A fabricated job would exercise the binding check
        and nothing else.
        """

        command = await self.send_command(idempotency_key=idempotency_key)
        request_hash = send_request_hash(
            case_id=command.case_id,
            action_id=command.action_id,
            execution_id=command.execution_id,
            approval_id=command.approval_id,
            expected_execution_version=command.expected_execution_version,
        )
        operations = self.operations()
        reserved = await operations.reserve_start(
            namespace=NAMESPACE,
            command=IdempotentCommand.SEND_ACTION,
            actor_id_hash=ACTOR_HASH,
            key_hash=send_start_key_hash(idempotency_key),
            request_hash=request_hash,
        )
        started = reserved
        if isinstance(reserved, StartReservation):
            started = await operations.complete_start(
                reserved,
                namespace=NAMESPACE,
                kind=ApplicationOperationKind.SEND_ACTION,
                actor_id_hash=ACTOR_HASH,
                case_id=self.action.case_id,
            )
        return SendActionOperationJob(
            operation_id=OperationId(started.operation.operation_id.value),  # type: ignore[union-attr]
            namespace=NAMESPACE,
            community_id=self.action.compile.case.community_id,
            case_id=self.action.case_id,
            action_id=command.action_id,
            execution_id=command.execution_id,
            approval_id=command.approval_id,
            correlation_id=command.correlation_id,
            actor_id_hash=ACTOR_HASH,
            request_hash=started.operation.request_hash,  # type: ignore[union-attr]
            expected_execution_version=command.expected_execution_version,
            idempotency_key=idempotency_key,
        )

    # -- world manipulation ---------------------------------------------------------------------

    def advance(self, delta: timedelta) -> None:
        """Move the injected clock. Expiry is the one fact no storage condition can express."""

        self.action.compile.clock.instant = self.action.compile.clock.instant + delta

    def default_registry(self) -> InMemoryDestinationRegistry:
        """The one allowlisted entry, matching the destination the view was compiled against."""

        return registry_for(
            self.action.compile.stored_destination(),
            address="property-manager@chorus.invalid",
        )

    def rotated_destination(self, **overrides: object) -> StoredSafeDestination:
        """The deployment's entry with one field moved, for a late-change test."""

        return replace(self.action.compile.stored_destination(), **overrides)  # type: ignore[arg-type]

    async def approved_execution(self, **overrides: object) -> ActionExecution:
        """Approve and return the execution the sender will find, for the send tests."""

        await self.approve(**overrides)
        return await self.execution()


def registry_for(
    destination: StoredSafeDestination, *, address: str
) -> InMemoryDestinationRegistry:
    """A registry whose one entry matches ``destination`` exactly.

    Built from a stored entry rather than defaulted, so a test that moves the routing triple
    can hand the sender a registry that agrees with the *new* configuration while the view
    still names the old one -- which is what makes the denial come from the binding rather
    than from a lookup miss.
    """

    base = demo_registry()
    return replace(
        base,
        destination=replace(
            base.destination,
            destination_id=destination.destination_id,
            kind=destination.kind,
            registry_version=destination.registry_version,
            routing_token=destination.routing_token,
            display_label=destination.display_label,
            address=address,
        ),
    )
