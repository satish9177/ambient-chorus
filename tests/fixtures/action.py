"""A Phase-7 harness: compile a real view, then wire the proposal use case over it.

The proposal path is only meaningful against a view a real compile produced, so this harness
extends the Phase-6 one rather than fabricating a stored view. Everything the Action validator
checks -- the pointer, the recomputed hash, the authorization epoch, the destination, the
purpose, the expiry -- is therefore checked against an artifact twenty-two gates approved.

The scripted agent is the interesting part. It answers with whatever a test hands it, including
answers no honest model would produce, and it can run a callback *while it is notionally
running* -- which is the only way to move durable state mid-invocation and exercise the one race
the pre-invocation checks cannot see.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

from chorus.application.commands.propose_action import (
    ProposeAction,
    ProposeActionCommand,
)
from chorus.application.commands.propose_action_operation import ProposeActionOperationWorker
from chorus.application.operations import (
    ApplicationOperations,
    propose_action_binding_hash,
    propose_action_request_hash,
)
from chorus.application.queries.current_action import ReadCurrentAction
from chorus.application.services.mandate_terms import key_hash
from chorus.contracts.action import (
    ActionCaveatDraft,
    ActionClaimDraft,
    ActionInput,
    ActionProposalDraft,
    ActionRequestDraft,
    ActionToneValue,
    ShareableFactInput,
)
from chorus.domain.entities import ApplicationOperationKind, CaseState, Purpose
from chorus.domain.ids import (
    ActionId,
    CaseId,
    IdGenerator,
    OperationId,
    Sha256Digest,
    Uuid4Generator,
    Uuid5Generator,
    ViewId,
)
from chorus.domain.state import CaseTransitionContext, transition_case
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.local.action_agent import ScriptedActionAgent
from chorus.ports.demo_clock import DemoClockStorePort
from chorus.ports.idempotency import IdempotentCommand
from chorus.ports.operations import ProposeActionOperationJob
from chorus.ports.records import StoredShareableView
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import StorageDriver
from chorus.ports.unit_of_work import TransactionPlan
from tests.fixtures.compile import CompileHarness, harness_uuid, photo_bytes
from tests.fixtures.elevator import NAMESPACE, NOW

FROM_IDENTITY_ID = "chorus-demo-sender"
ACTOR_HASH = Sha256Digest("sha256:" + "a" * 64)


def _local(view_id: UUID, label: str) -> UUID:
    return uuid5(UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff"), f"{view_id}:{label}")


def first_fact_with_status(payload: ActionInput, status: str) -> ShareableFactInput | None:
    """The first safe fact carrying ``status``, or ``None``.

    Used by the contradiction tests so they select a genuinely ``CONTRADICTED`` fact the
    *compiler* produced, rather than rewriting a view and hoping the status is where they left
    it. Export facts are sorted by identifier, so positional indexing would be a coincidence.
    """

    return next(
        (fact for fact in payload.shareable_facts if fact.evidence_status.value == status), None
    )


def grounded_draft(
    payload: ActionInput,
    *,
    subject: str = "Repair request",
    caveat_for_contradicted: bool = True,
    fact: ShareableFactInput | None = None,
) -> ActionProposalDraft:
    """The narrowest proposal the contract admits, grounded by construction.

    Each claim's text **is** a cited fact's own ``safe_text``, which is the only wording
    guaranteed to survive the grounding grammar without the fixture having to guess at it. A
    test that wants an ungrounded answer edits one field of this rather than assembling a whole
    draft, so what it is testing is visible in one line.

    ``fact`` selects which safe fact the proposal relies on. It defaults to the first, and the
    contradiction tests pass one explicitly because whether a caveat is *required* depends on
    which fact the proposal actually leans on.
    """

    fact = fact if fact is not None else payload.shareable_facts[0]
    claims = (
        ActionClaimDraft(
            claim_id=_local(payload.view_id, "claim:0"),
            text=fact.safe_text,
            export_fact_ids=(fact.export_fact_id,),
        ),
    )
    caveats: tuple[ActionCaveatDraft, ...] = ()
    if caveat_for_contradicted and fact.evidence_status.value == "CONTRADICTED":
        caveats = (
            ActionCaveatDraft(
                caveat_id=_local(payload.view_id, "caveat:0"),
                text="This observation is disputed by other reports in the same case.",
                export_fact_ids=(fact.export_fact_id,),
            ),
        )
    return ActionProposalDraft(
        view_id=payload.view_id,
        view_hash=payload.view_hash,
        case_id=payload.case_id,
        case_version=payload.case_version,
        authorization_version=payload.authorization_version,
        subject=subject,
        claims=claims,
        request=ActionRequestDraft(
            requested_action="Please inspect and repair, then confirm the schedule.",
            requested_deadline=NOW + timedelta(days=7),
            request_fact_ids=(fact.export_fact_id,),
        ),
        caveats=caveats,
        tone=ActionToneValue.NEUTRAL,
    )


@dataclass(slots=True)
class RecordingUnitOfWork:
    """The real unit of work, keeping each plan so a test can count its participants.

    The count has to be read off the *staged plan* rather than off the constant it is compared
    against, or the assertion would only prove the module is self-consistent. Wrapping is used
    rather than patching because ``StorageUnitOfWork`` has ``slots``, and a wrapper is honest
    about being a test seam anyway.
    """

    inner: StorageUnitOfWork
    plans: list[TransactionPlan] = field(default_factory=list)
    fail_next: list[Exception] = field(default_factory=list)
    """Exceptions to raise instead of committing, one per entry, in order.

    Used by the race tests to make a transaction fail the way a lost condition would, at the
    exact point where a real conflict happens.
    """

    before_commit: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    """Callbacks to run *between* a command's reads and its write, one per entry, in order.

    This is the only seam that can express the race that matters: a caller whose reads all
    passed, and whose durable world then moved before its transaction landed. Without it a test
    can only ever exercise the cheap read-time refusal, which proves that the reads work and
    says nothing about whether the transaction's own conditions do.
    """

    fail_by_name: dict[str, Exception] = field(default_factory=dict)
    """Exceptions keyed by transaction name, raised instead of committing that plan.

    Positional ``fail_next`` cannot express "let the claim commit and lose the *result*", which
    is the exact shape a crashed sender leaves behind -- a ``SENDING`` row nothing ever
    finished. Naming the plan makes that scenario constructible without counting commits.
    """

    async def commit(self, plan: TransactionPlan) -> None:
        self.plans.append(plan)
        if self.before_commit:
            await self.before_commit.pop(0)()
        named = self.fail_by_name.pop(plan.name, None)
        if named is not None:
            raise named
        if self.fail_next:
            raise self.fail_next.pop(0)
        await self.inner.commit(plan)

    async def resolve_outcome(self, plan: TransactionPlan) -> object:
        return await self.inner.resolve_outcome(plan)

    def plan(self, name: str) -> TransactionPlan:
        """The one plan committed under ``name``; a second would be its own failure."""

        found = [item for item in self.plans if item.name == name]
        assert len(found) == 1, f"expected exactly one {name} plan, saw {len(found)}"
        return found[0]


@dataclass(slots=True)
class ActionHarness:
    """The compile harness, a real current view, and the proposal use case wired over both."""

    driver: StorageDriver
    compile: CompileHarness = field(init=False)
    agent: ScriptedActionAgent = field(init=False)
    view: StoredShareableView = field(init=False)
    unit_of_work: RecordingUnitOfWork = field(init=False)
    invocation_id: UUID = field(default_factory=uuid4)
    ids: IdGenerator = field(default_factory=Uuid4Generator)
    """The **production** generator, deliberately.

    ``ActionProposal.action_id`` and ``ActionExecution.execution_id`` are UUIDv4 and nothing
    derives one from the other, so a harness minting deterministic identifiers would hide the
    exact property the proposal path has to hold. Randomness also removes the collision this
    field previously had to work around: two proposals in one test can no longer produce the
    same create-only audit event by construction.
    """

    def __post_init__(self) -> None:
        self.compile = CompileHarness(driver=self.driver)
        self.unit_of_work = RecordingUnitOfWork(inner=self.compile.unit_of_work)
        self.agent = ScriptedActionAgent(
            responder=lambda invocation: grounded_draft(invocation.payload)
        )

    # -- setup ---------------------------------------------------------------------------

    async def prepare(self) -> StoredShareableView:
        """Seed the fixture, compile one real view, and move the case to ``READY_FOR_ACTION``.

        The case transition is done through the real transition service rather than by writing a
        row, because the proposal apply conditions on the exact ``version``,
        ``authorization_version``, and ``state`` -- and a hand-written row could carry a
        combination the state machine would never produce.
        """

        raw = photo_bytes()
        await self.compile.seed(evidence_items=self.compile.align_photo_digest(raw), photo=raw)
        result = await self.compile.compile_view().execute(self.compile.command())
        assert result.view is not None
        self.view = result.view
        await self._make_ready()
        return self.view

    async def _make_ready(self) -> None:
        case = await self.compile.core.load_case(self.scope)
        if case.state is CaseState.READY_FOR_ACTION:
            return
        ready = transition_case(
            case,
            CaseState.READY_FOR_ACTION,
            expected_version=case.version,
            reason_code="EVIDENCE_SUFFICIENT",
            now=NOW,
            context=CaseTransitionContext(
                validated_assessment=True,
                independent_source_count=case.corroboration_source_count,
                no_material_different_issue=True,
                has_compilable_purpose=True,
            ),
        )
        await self.compile.unit_of_work.commit(
            TransactionPlan(
                name="action-harness-ready",
                operations=(
                    self.compile.core.stage_update_case(
                        self.scope, ready, expected_version=case.version
                    ),
                ),
                audit_required=False,
            )
        )

    # -- scope ----------------------------------------------------------------------------

    @property
    def scope(self) -> CaseScope:
        return self.compile.scope

    @property
    def case_id(self) -> CaseId:
        return self.compile.case.case_id

    async def action_id(self) -> ActionId:
        """The action identity of the current proposal, **read** rather than derived.

        ``ActionProposal.action_id`` is UUIDv4 (ADR-020/021/022 authorize no derivation), so
        the only honest way for a test to learn it is the durable pointer the apply wrote.
        """

        pointer = await self.compile.shareable.load_current_action_pointer(self.scope)
        assert pointer is not None, "no current action pointer exists yet"
        return pointer.action_id

    async def case(self) -> object:
        return await self.compile.core.load_case(self.scope)

    # -- use case --------------------------------------------------------------------------

    def propose_action(
        self,
        *,
        ids: IdGenerator | None = None,
        freshness_clock: DemoClockStorePort | None = None,
    ) -> ProposeAction:
        return ProposeAction(
            core=self.compile.core,
            shareable=self.compile.shareable,
            audit=self.compile.audit,
            idempotency=self.compile.idempotency,
            unit_of_work=self.unit_of_work,  # type: ignore[arg-type]
            agent=self.agent,
            clock=self.compile.clock,
            ids=ids or self.ids,
            destination=self.compile.stored_destination(),
            from_identity_id=FROM_IDENTITY_ID,
            purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
            freshness_clock=freshness_clock,
        )

    def read_current_action(self) -> ReadCurrentAction:
        return ReadCurrentAction(
            shareable=self.compile.shareable, from_identity_id=FROM_IDENTITY_ID
        )

    async def command(
        self,
        *,
        expected_case_version: int | None = None,
        view_id: ViewId | None = None,
        view_hash: Sha256Digest | None = None,
        idempotency_key: str = "propose-key-0001",
        invocation_id: UUID | None = None,
    ) -> ProposeActionCommand:
        case = await self.compile.core.load_case(self.scope)
        return ProposeActionCommand(
            namespace=NAMESPACE,
            community_id=self.compile.case.community_id,
            case_id=self.case_id,
            operation_id=harness_uuid("operation:1"),
            invocation_id=invocation_id or self.invocation_id,
            correlation_id=harness_uuid("correlation:1"),
            actor_id_hash=ACTOR_HASH,
            expected_case_version=(
                expected_case_version if expected_case_version is not None else case.version
            ),
            view_id=view_id or self.view.view_id,
            view_hash=view_hash or self.view.view_hash,
            idempotency_key=idempotency_key,
        )

    # -- operations ------------------------------------------------------------------------

    def operations(self) -> ApplicationOperations:
        return ApplicationOperations(
            core=self.compile.core,
            idempotency=self.compile.idempotency,
            unit_of_work=self.compile.unit_of_work,
            clock=self.compile.clock,
            ids=Uuid5Generator(namespace=harness_uuid("operation-ids"), prefix="op"),
        )

    def worker(self) -> ProposeActionOperationWorker:
        return ProposeActionOperationWorker(
            operations=self.operations(), propose_action=self.propose_action()
        )

    def binding(self) -> Sha256Digest:
        return propose_action_binding_hash(
            case_id=self.case_id,
            view_id=self.view.view_id.value,
            view_hash=self.view.view_hash,
        )

    async def request_hash(self, *, expected_case_version: int | None = None) -> Sha256Digest:
        """The route's HTTP request identity, which covers ``expected_case_version``."""

        case = await self.compile.core.load_case(self.scope)
        return propose_action_request_hash(
            case_id=self.case_id,
            expected_case_version=(
                expected_case_version if expected_case_version is not None else case.version
            ),
            view_id=self.view.view_id.value,
            view_hash=self.view.view_hash,
        )

    async def start_operation(
        self,
        *,
        idempotency_key: str = "propose-key-0001",
        expected_case_version: int | None = None,
    ) -> object:
        """Create the durable operation exactly as the route does, handover and all."""

        operations = self.operations()
        binding = self.binding()
        reserved = await operations.reserve_start(
            namespace=NAMESPACE,
            command=IdempotentCommand.PROPOSE_ACTION,
            actor_id_hash=ACTOR_HASH,
            key_hash=_key_hash(idempotency_key),
            request_hash=await self.request_hash(expected_case_version=expected_case_version),
        )
        started = await operations.complete_start(
            reserved,  # type: ignore[arg-type]
            namespace=NAMESPACE,
            kind=ApplicationOperationKind.PROPOSE_ACTION,
            actor_id_hash=ACTOR_HASH,
            case_id=self.case_id,
            agent_binding_hash=binding,
        )
        self.invocation_id = started.invocation_id
        return started

    async def job(
        self,
        started: object,
        *,
        idempotency_key: str = "propose-key-0001",
        expected_case_version: int | None = None,
        invocation_id: UUID | None = None,
    ) -> ProposeActionOperationJob:
        case = await self.compile.core.load_case(self.scope)
        operation = started.operation  # type: ignore[attr-defined]
        return ProposeActionOperationJob(
            operation_id=OperationId(operation.operation_id.value),
            namespace=NAMESPACE,
            community_id=self.compile.case.community_id,
            case_id=self.case_id,
            invocation_id=invocation_id or started.invocation_id,  # type: ignore[attr-defined]
            correlation_id=harness_uuid("correlation:1"),
            actor_id_hash=ACTOR_HASH,
            request_hash=operation.request_hash,
            expected_case_version=(
                expected_case_version if expected_case_version is not None else case.version
            ),
            view_id=self.view.view_id,
            view_hash=self.view.view_hash,
            idempotency_key=idempotency_key,
        )


def _key_hash(value: str) -> Sha256Digest:
    """The route's own key derivation, restated so the harness starts a real operation."""

    return key_hash(f"propose-action-start{value}")


def replace_fact_status(view: StoredShareableView, status: object) -> StoredShareableView:
    """Return the same view with its first safe fact carrying ``status``.

    Used by the contradiction tests. The view hash deliberately is **not** recomputed: a caller
    that needs a coherent artifact re-seals it, and a caller testing the integrity check wants
    exactly this incoherence.
    """

    first, *rest = view.shareable_facts
    return replace(
        view,
        shareable_facts=(replace(first, evidence_status=status), *rest),  # type: ignore[arg-type]
    )
