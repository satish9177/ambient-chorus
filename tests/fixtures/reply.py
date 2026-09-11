"""A Phase-9 harness: a real send, a real locator, then the boundary and everything after it.

It extends the Phase-8 harness rather than fabricating an execution, so the ``ses_message_id`` a
reply correlates against is the identifier a real send recorded and the locator is the one the
real projection transaction wrote. That is the only way these tests can mean what they say: a
fabricated locator would prove the correlation code runs, not that a reply can reach a case.

The seams are the two Phase 9 adds. ``ScriptedCommitmentExtractor`` answers with extractions no
honest model would produce -- a span outside the text, a fabricated obligor, a promise the reply
never made -- and ``InMemoryDeadlineScheduler`` counts deliberate create requests, which is the
number one of the exit criteria is actually about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from chorus.application.commands.apply_commitment import ApplyCommitment
from chorus.application.commands.create_due_schedule import (
    CreateDueSchedule,
)
from chorus.application.commands.extract_commitment_operation import (
    ExtractCommitment,
    ExtractCommitmentJob,
)
from chorus.application.commands.ingest_external_reply import (
    IngestExternalReply,
    IngestExternalReplyCommand,
    IngestExternalReplyResult,
    RecordReplyRejection,
)
from chorus.application.commands.project_action_outcome import ProjectActionOutcomeCommand
from chorus.application.commands.record_commitment_due import (
    RecordCommitmentDue,
    RecordCommitmentDueCommand,
)
from chorus.application.commands.verify_commitment import (
    VerificationOutcome,
    VerifyCommitment,
    VerifyCommitmentCommand,
)
from chorus.application.operations import extract_commitment_binding_hash
from chorus.application.services.commitment_schedule import due_event
from chorus.application.services.inbound_mail import (
    AttestedInboundReply,
    InboundMailAttester,
    InboundMailEvidenceVerifier,
    address_digest,
    inbound_mail_trust_boundary,
)
from chorus.contracts.commitment import CommitmentExtractionOutput
from chorus.domain.entities import ActionExecution, Commitment, CommitmentStatus
from chorus.domain.ids import (
    CommitmentId,
    ContributorId,
    EvidenceItemId,
    OperationId,
    Sha256Digest,
    Uuid4Generator,
)
from chorus.infrastructure.fixtures.inbound_replies import (
    MANAGER_PROMISE,
    build_raw_message,
    build_receipt_envelope,
    outbound_message_id,
    reviewed_reply,
)
from chorus.infrastructure.local.commitment_agent import (
    LiteralSpanCommitmentExtractor,
    ScriptedCommitmentExtractor,
)
from chorus.infrastructure.local.inbound_mail import (
    InMemoryInboundRawStore,
    LocalInboundMailAuthenticator,
)
from chorus.infrastructure.local.objects import InMemoryObjectStore
from chorus.infrastructure.local.scheduler import InMemoryDeadlineScheduler
from chorus.ports.inbound_mail import InboundMailTransportContext
from chorus.ports.records import CommitmentScheduleProjection, SafeInboundMailConfiguration
from chorus.ports.scheduler import CommitmentDueEvent
from chorus.ports.scopes import ActionScope, CaseScope
from chorus.settings import Environment
from tests.fixtures.action import ACTOR_HASH, FROM_IDENTITY_ID
from tests.fixtures.elevator import NAMESPACE
from tests.fixtures.send import APPROVER_HASH, SendHarness

INBOUND_TRANSPORT = "aws:ses-receipt"
INBOUND_SOURCE_ARN = "arn:aws:ses:us-east-1:000000000000:receipt-rule-set/chorus-test"
FOREIGN_INBOUND_SOURCE_ARN = "arn:aws:ses:us-east-1:000000000000:receipt-rule-set/somebody-else"

MANAGER_ADDRESS = "property-manager@chorus.invalid"
INBOUND_ADDRESS = "chorus-replies@chorus.invalid"
STRANGER_ADDRESS = "stranger@elsewhere.invalid"
"""Reserved ``.invalid`` addresses (RFC 2606), which can never resolve.

They exist here and nowhere in ``src``: the application holds digests of them and no address at
all, which is the property ADR-026 § 3 is about.
"""

FIXTURE_OBJECT_KEY = "inbound/chorus-test/reply-0001"
SCHEDULER_ENVIRONMENT = "test"


@dataclass(slots=True)
class ReplyHarness:
    """A sent case, its locator, and every Phase-9 use case wired over the same driver."""

    send: SendHarness
    objects: InMemoryObjectStore = field(default_factory=InMemoryObjectStore)
    raw_messages: InMemoryInboundRawStore = field(default_factory=InMemoryInboundRawStore)
    scheduler: InMemoryDeadlineScheduler = field(default_factory=InMemoryDeadlineScheduler)
    authenticator: LocalInboundMailAuthenticator | None = None
    trust: tuple[InboundMailAttester, InboundMailEvidenceVerifier] | None = None
    extractor: ScriptedCommitmentExtractor | LiteralSpanCommitmentExtractor | None = None

    def __post_init__(self) -> None:
        if self.authenticator is None:
            self.authenticator = LocalInboundMailAuthenticator(
                environment=Environment.TEST,
                transport=INBOUND_TRANSPORT,
                source_arn=INBOUND_SOURCE_ARN,
            )
        if self.extractor is None:
            self.extractor = LiteralSpanCommitmentExtractor()

    # -- setup -----------------------------------------------------------------------------

    async def prepare_sent(self) -> ActionExecution:
        """Compile, propose, approve, send, and project -- so a real locator exists."""

        await self.send.prepare()
        await self.send.approve()
        await self.send.send()
        execution = await self.send.execution()
        await self.send.project().execute(await self._projection_command(execution))
        return await self.send.execution()

    async def _projection_command(self, execution: ActionExecution) -> ProjectActionOutcomeCommand:
        del execution
        pointer = await self.send.pointer()
        return ProjectActionOutcomeCommand(
            namespace=NAMESPACE,
            community_id=self.send.action.compile.case.community_id,
            case_id=self.send.action.case_id,
            action_id=pointer.action_id,
            execution_id=pointer.execution_id,
            actor_id_hash=ACTOR_HASH,
            correlation_id=uuid4(),
        )

    # -- accessors --------------------------------------------------------------------------

    @property
    def scope(self) -> CaseScope:
        return self.send.scope

    @property
    def destination_label(self) -> str:
        return self.send.action.compile.stored_destination().display_label

    def inbound_config(self) -> SafeInboundMailConfiguration:
        return SafeInboundMailConfiguration(
            destination=self.send.action.compile.stored_destination(),
            destination_address_digest=address_digest(NAMESPACE, MANAGER_ADDRESS),
            inbound_address_digest=address_digest(NAMESPACE, INBOUND_ADDRESS),
        )

    async def action_scope(self) -> ActionScope:
        return await self.send.action_scope()

    # -- the trust boundary --------------------------------------------------------------------

    def inbound_trust(self) -> tuple[InboundMailAttester, InboundMailEvidenceVerifier]:
        """The harness's one attester/verifier pair, so a minted attestation verifies here."""

        if self.trust is None:
            self.trust = inbound_mail_trust_boundary(
                transport=INBOUND_TRANSPORT,
                source_arn=INBOUND_SOURCE_ARN,
                authenticator=self.authenticator,
                raw_messages=self.raw_messages,
                core=self.send.action.compile.core,
                shareable=self.send.action.compile.shareable,
                config=self.inbound_config(),
                namespace=NAMESPACE,
                from_identity_id=FROM_IDENTITY_ID,
            )
        return self.trust

    async def delivery(
        self,
        *,
        fixture_id: str = MANAGER_PROMISE,
        ses_message_id: str | None = None,
        source: str = MANAGER_ADDRESS,
        destination: str = INBOUND_ADDRESS,
        transport: str = INBOUND_TRANSPORT,
        source_arn: str = INBOUND_SOURCE_ARN,
        received_at: datetime | None = None,
        quote_outbound: bool = False,
        object_key: str = FIXTURE_OBJECT_KEY,
        headers_truncated: bool = False,
        oversize: bool = False,
        **verdicts: str,
    ) -> InboundMailTransportContext:
        """Stage one reviewed reply as a delivery, threaded to the real execution.

        ``ses_message_id`` defaults to the identifier the real send recorded, which is what
        makes the correlation the production one rather than a fixture agreeing with itself.
        """

        execution = await self.send.execution()
        resolved = ses_message_id or execution.ses_message_id or "missing"
        reply = reviewed_reply(fixture_id)
        message_id = f"<reply-{uuid4()}@manager.invalid>"
        raw = build_raw_message(
            reply,
            message_id=message_id,
            in_reply_to=outbound_message_id(resolved),
            from_address=source,
            to_address=destination,
            quoted_outbound=(await self.outbound_text()) if quote_outbound else None,
        )
        if oversize:
            raw = raw + b"\n" + b"x" * (256 * 1024)
        self.raw_messages.put(bucket="chorus-local-inbound-fixtures", key=object_key, content=raw)
        envelope = build_receipt_envelope(
            message_id=message_id,
            in_reply_to=outbound_message_id(resolved),
            subject=reply.subject,
            source=source,
            destination=destination,
            received_at=received_at or self.send.action.compile.clock.now(),
            object_key=object_key,
            headers_truncated=headers_truncated,
            **verdicts,  # type: ignore[arg-type]  # only ever the 5 verdict-status strings
        )
        return InboundMailTransportContext(
            transport=transport, source_arn=source_arn, envelope=envelope
        )

    async def outbound_text(self) -> str:
        """Regenerate the exact outbound body, the way ingestion does."""

        from chorus.application.services.action_renderer import render_preview

        action_scope = await self.action_scope()
        proposal = await self.send.action.compile.shareable.load_proposal(action_scope)
        view = await self.send.action.compile.shareable.load_view(
            action_scope.case_scope, proposal.view_id
        )
        return render_preview(proposal, view, from_identity_id=FROM_IDENTITY_ID).text_body

    async def attest(self, **overrides: object) -> AttestedInboundReply:
        attester, _ = self.inbound_trust()
        return await attester.attest(await self.delivery(**overrides))  # type: ignore[arg-type]

    # -- use cases -----------------------------------------------------------------------------

    def ingest(self, *, trusted: bool = True) -> IngestExternalReply:
        return IngestExternalReply(
            core=self.send.action.compile.core,
            audit=self.send.action.compile.audit,
            idempotency=self.send.action.compile.idempotency,
            objects=self.objects,
            unit_of_work=self.send.action.unit_of_work,  # type: ignore[arg-type]
            clock=self.send.action.compile.clock,
            ids=Uuid4Generator(),
            evidence_trust=self.inbound_trust()[1] if trusted else None,
        )

    def record_rejection(self) -> RecordReplyRejection:
        return RecordReplyRejection(
            audit=self.send.action.compile.audit,
            unit_of_work=self.send.action.unit_of_work,  # type: ignore[arg-type]
            clock=self.send.action.compile.clock,
            ids=Uuid4Generator(),
        )

    def apply_commitment(self) -> ApplyCommitment:
        return ApplyCommitment(
            core=self.send.action.compile.core,
            shareable=self.send.action.compile.shareable,
            audit=self.send.action.compile.audit,
            idempotency=self.send.action.compile.idempotency,
            unit_of_work=self.send.action.unit_of_work,  # type: ignore[arg-type]
            clock=self.send.action.compile.clock,
            ids=Uuid4Generator(),
            destination_label=self.destination_label,
            scheduler_environment=SCHEDULER_ENVIRONMENT,
        )

    def extract(self) -> ExtractCommitment:
        assert self.extractor is not None
        return ExtractCommitment(
            core=self.send.action.compile.core,
            agent=self.extractor,
            apply=self.apply_commitment(),
            clock=self.send.action.compile.clock,
            policy_version="policy/v1",
            destination_label=self.destination_label,
        )

    def create_schedule(self) -> CreateDueSchedule:
        return CreateDueSchedule(
            idempotency=self.send.action.compile.idempotency,
            shareable=self.send.action.compile.shareable,
            audit=self.send.action.compile.audit,
            unit_of_work=self.send.action.unit_of_work,  # type: ignore[arg-type]
            scheduler=self.scheduler,
            clock=self.send.action.compile.clock,
            # P1/P2-2: the fixture's own commands never set ``logical_now`` on
            # ``CreateDueScheduleCommand``, so ``demo_schedule_instant`` -- the only caller of
            # ``wall_clock`` -- is never exercised here; the same fixed test clock is reused
            # rather than a second one, since nothing in this suite reads it.
            wall_clock=self.send.action.compile.clock,
            ids=Uuid4Generator(),
            scheduler_environment=SCHEDULER_ENVIRONMENT,
        )

    def watcher(self) -> RecordCommitmentDue:
        return RecordCommitmentDue(
            shareable=self.send.action.compile.shareable,
            audit=self.send.action.compile.audit,
            unit_of_work=self.send.action.unit_of_work,  # type: ignore[arg-type]
            clock=self.send.action.compile.clock,
            ids=Uuid4Generator(),
        )

    def verify(self) -> VerifyCommitment:
        return VerifyCommitment(
            core=self.send.action.compile.core,
            shareable=self.send.action.compile.shareable,
            audit=self.send.action.compile.audit,
            idempotency=self.send.action.compile.idempotency,
            unit_of_work=self.send.action.unit_of_work,  # type: ignore[arg-type]
            clock=self.send.action.compile.clock,
            ids=Uuid4Generator(),
        )

    # -- flows ---------------------------------------------------------------------------------

    async def ingest_reply(self, **overrides: object) -> IngestExternalReplyResult:
        attested = await self.attest(**overrides)
        return await self.ingest().execute(
            IngestExternalReplyCommand(
                attested=attested, actor_id_hash=ACTOR_HASH, correlation_id=uuid4()
            )
        )

    async def extraction_job(
        self, ingested: IngestExternalReplyResult, *, invocation_id: object | None = None
    ) -> ExtractCommitmentJob:
        items = await self.send.action.compile.core.load_evidence_items(
            self.scope, (ingested.evidence_id,)
        )
        evidence_sha256 = items[0].sha256
        return ExtractCommitmentJob(
            operation_id=self._operation_id(),
            namespace=NAMESPACE,
            community_id=self.send.action.compile.case.community_id,
            case_id=self.send.action.case_id,
            action_id=ingested.action_id,
            evidence_id=ingested.evidence_id,
            invocation_id=invocation_id or uuid4(),  # type: ignore[arg-type]
            correlation_id=uuid4(),
            actor_id_hash=ACTOR_HASH,
            request_hash=extract_commitment_binding_hash(
                case_id=self.send.action.case_id,
                evidence_id=ingested.evidence_id,
                evidence_sha256=evidence_sha256,
            ),
            evidence_sha256=evidence_sha256,
        )

    def _operation_id(self) -> OperationId:
        return OperationId(uuid4())

    def binding_hash(
        self, *, evidence_id: EvidenceItemId, evidence_sha256: Sha256Digest
    ) -> Sha256Digest:
        return extract_commitment_binding_hash(
            case_id=self.send.action.case_id,
            evidence_id=evidence_id,
            evidence_sha256=evidence_sha256,
        )

    async def commitment(self, commitment_id: CommitmentId) -> Commitment:
        return await self.send.action.compile.shareable.load_commitment(self.scope, commitment_id)

    async def schedule_projection(
        self, commitment_id: CommitmentId
    ) -> CommitmentScheduleProjection | None:
        return await self.send.action.compile.shareable.load_commitment_schedule(
            self.scope, commitment_id
        )

    async def due_command(
        self,
        commitment: Commitment,
        *,
        generation: int | None = None,
        due_at: datetime | None = None,
        trigger: str = "SCHEDULE",
    ) -> RecordCommitmentDueCommand:
        event: CommitmentDueEvent = due_event(
            namespace=NAMESPACE,
            case_id=commitment.case_id,
            commitment_id=commitment.commitment_id,
            generation=generation or commitment.schedule_generation,
            due_at=due_at or commitment.due_at,
        )
        return RecordCommitmentDueCommand(
            event=event,
            community_id=self.send.action.compile.case.community_id,
            actor_id_hash=APPROVER_HASH,
            correlation_id=uuid4(),
            trigger=trigger,
        )

    async def verification_command(
        self,
        commitment: Commitment,
        *,
        contributor_id: ContributorId,
        outcome: VerificationOutcome = VerificationOutcome.FULFILLED,
        idempotency_key: str = "verify-key-0001",
        expected_version: int | None = None,
    ) -> VerifyCommitmentCommand:
        return VerifyCommitmentCommand(
            namespace=NAMESPACE,
            community_id=self.send.action.compile.case.community_id,
            case_id=self.send.action.case_id,
            commitment_id=commitment.commitment_id,
            contributor_id=contributor_id,
            expected_version=(commitment.version if expected_version is None else expected_version),
            outcome=outcome,
            actor_id_hash=APPROVER_HASH,
            correlation_id=uuid4(),
            idempotency_key=idempotency_key,
        )

    async def affected_contributor(self) -> ContributorId:
        """A contributor owning an ``ACTIVE`` fact in this case, read from durable state."""

        from chorus.domain.facts import FactStatus

        case = await self.send.action.compile.core.load_case(self.scope)
        facts = await self.send.action.compile.core.load_facts(self.scope, case.fact_ids)
        for fact in facts:
            if fact.status is FactStatus.ACTIVE:
                return fact.contributor_id
        raise AssertionError("the fixture case has no active fact")

    def advance(self, delta: timedelta) -> None:
        self.send.advance(delta)

    def past_due(self, commitment: Commitment) -> None:
        """Move the injected clock past the deadline, which is the watcher's step-4 input."""

        now = self.send.action.compile.clock.instant
        if now < commitment.due_at:
            self.advance(commitment.due_at - now + timedelta(seconds=1))


def scripted_extraction(output: CommitmentExtractionOutput) -> ScriptedCommitmentExtractor:
    """An extractor that answers with exactly this output, whatever the reply says."""

    return ScriptedCommitmentExtractor(responder=lambda _invocation: output)


def utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


__all__ = [
    "FIXTURE_OBJECT_KEY",
    "FOREIGN_INBOUND_SOURCE_ARN",
    "INBOUND_ADDRESS",
    "INBOUND_SOURCE_ARN",
    "INBOUND_TRANSPORT",
    "MANAGER_ADDRESS",
    "SCHEDULER_ENVIRONMENT",
    "STRANGER_ADDRESS",
    "CommitmentStatus",
    "ReplyHarness",
    "scripted_extraction",
    "utc",
]
