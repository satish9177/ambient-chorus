"""The defects an independent review reproduced, each asserted at its own failure boundary.

Every test here failed before the repair it names and passes after it, and none of them mocks
away the boundary being tested. The ambiguous-claim tests drive the **real**
:class:`chorus.infrastructure.dynamodb.unit_of_work.StorageUnitOfWork` and its commit-proof
resolver over a driver that loses one transaction's outcome the way a network does -- the write
does not land and the caller is told nothing. The revocation tests commit a **real**
``DecideMandate(REVOKE)``. The worker tests go through
:class:`chorus.application.commands.send_action_operation.SendActionOperationWorker` and the
durable operation record rather than around either.

The number almost all of them end on is ``ScriptedSender.call_count``. That is the property the
phase is about -- at most one deliberate SES attempt per approved execution -- and it cannot be
inferred from a durable state, because a definite failure that reached SES and one that never
did leave the same ``FAILED`` row.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.fixtures.action import FROM_IDENTITY_ID
from tests.fixtures.send import SendHarness

from chorus.application.commands.approve_action import ApprovalDenial, ApprovalDeniedError
from chorus.application.commands.decide_mandate import DecideMandate, DecideMandateCommand
from chorus.application.commands.send_action import SendActionResult, SendFailureReason
from chorus.application.services.action_authorization import SEND_FENCE_LIFETIME
from chorus.application.services.action_renderer import TEMPLATE_VERSION
from chorus.domain.entities import (
    ActionExecutionState,
    ApplicationOperationStatus,
    ApprovalDecision,
    CaseState,
    MandateStatus,
    Purpose,
)
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CaseId,
    CommunityId,
    DestinationId,
    ExecutionId,
    Namespace,
    Sha256Digest,
    ViewId,
)
from chorus.domain.mandates import IdentityGrant, MandateDecision
from chorus.infrastructure.compiler.send_authorization import (
    ACQUIRE_OPERATION,
    FENCE_PAYLOAD_SCHEMA,
    RELEASE_OPERATION,
    RELEASE_PAYLOAD_SCHEMA,
    CompilerSendAuthorization,
)
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.local.sender import ScriptedSender
from chorus.ports.errors import (
    ExternalDependencyError,
    IdempotencyConflictError,
    PersistenceError,
    PersistenceErrorCode,
    UnknownTransactionOutcomeError,
)
from chorus.ports.records import SendFence
from chorus.ports.scopes import CaseScope
from chorus.ports.send_authorization import (
    SendAuthorizationDenied,
    SendAuthorizationRequest,
)
from chorus.ports.sender import SesAccepted, SesEmailRequest, SesOutcome
from chorus.ports.storage import (
    DeleteItem,
    ItemKey,
    PutItem,
    QueryRequest,
    QueryResult,
    StorageDriver,
    StoredItem,
    WriteOperation,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------------------
# Doubles that lose an outcome rather than fabricate one
# ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class LosingOutcomeDriver:
    """Delegate everything, and lose the outcome of the transactions a predicate selects.

    "Lose" means what it means in production: the write is **not** applied and the caller is
    told :class:`UnknownTransactionOutcomeError`, which is the one thing a caller cannot
    distinguish from a write that landed. The real unit of work then resolves it by reading a
    commit proof, which is the code path the duplicate-send defect lived in.
    """

    inner: StorageDriver
    lose: Callable[[tuple[WriteOperation, ...]], bool]
    losses: int = 0

    async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
        return await self.inner.get_item(key, consistent=consistent)

    async def batch_get_items(
        self, keys: tuple[ItemKey, ...], *, consistent: bool
    ) -> tuple[StoredItem, ...]:
        return await self.inner.batch_get_items(keys, consistent=consistent)

    async def query(self, request: QueryRequest) -> QueryResult:
        return await self.inner.query(request)

    async def write_item(self, operation: PutItem | DeleteItem) -> None:
        await self.inner.write_item(operation)

    async def transact_write(
        self, operations: tuple[WriteOperation, ...], *, client_request_token: str
    ) -> None:
        if self.lose(operations):
            self.losses += 1
            raise UnknownTransactionOutcomeError("TRANSACTION")
        await self.inner.transact_write(operations, client_request_token=client_request_token)


def _writes_an_execution(operations: tuple[WriteOperation, ...]) -> bool:
    """True for the plans that move an execution row -- a claim and a send result both do."""

    return any(
        isinstance(item, PutItem) and "#EXECUTION#" in item.key.partition_key for item in operations
    )


def _losing_send(send_harness: SendHarness, sender: ScriptedSender) -> Any:
    """A send use case whose execution writes are committed through a lossy driver.

    The unit of work is the **production** one, so the commit proof read, the single retry, and
    the ambiguity classification are all real; only the transport under it forgets.
    """

    use_case = send_harness.send_action(sender=sender)
    use_case.unit_of_work = StorageUnitOfWork(
        driver=LosingOutcomeDriver(
            inner=send_harness.action.compile.driver, lose=_writes_an_execution
        )
    )
    return use_case


class HookedCore:
    """Delegate every Core call, and run one callback immediately before fence acquisition.

    This is the seam the review's revocation race lives at: the instant after a sender has
    claimed and before it holds the ordering window. Everything else is the real repository.
    """

    def __init__(self, inner: Any, before_acquire: Callable[[], Awaitable[None]]) -> None:
        self._inner = inner
        self._before_acquire = before_acquire
        self.fired = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def acquire_send_fence(self, scope: CaseScope, fence: SendFence) -> SendFence:
        if not self.fired:
            self.fired = True
            await self._before_acquire()
        acquired: SendFence = await self._inner.acquire_send_fence(scope, fence)
        return acquired


@dataclass(slots=True)
class DelayingRegistry:
    """A registry whose resolutions take time on the injected clock.

    The two lookups a send awaits between its fence check and its SES call are exactly the
    window ADR-025 SS 8's clock re-read exists to close, and a registry that advanced no clock
    could not open it.
    """

    inner: Any
    harness: SendHarness
    destination_delay: timedelta = timedelta()
    identity_delay: timedelta = timedelta()

    async def resolve_destination(self, **kwargs: Any) -> Any:
        self.harness.advance(self.destination_delay)
        return await self.inner.resolve_destination(**kwargs)

    async def resolve_sending_identity(self, identity_id: str) -> Any:
        self.harness.advance(self.identity_delay)
        return await self.inner.resolve_sending_identity(identity_id)


@dataclass(slots=True)
class AlwaysFailingUnitOfWork:
    """Commit everything except the named plans, which fail on **every** attempt.

    ``RecordingUnitOfWork.fail_by_name`` pops its entry, so it can only fail once. A lost
    projection has to stay lost across the first attempt and be repairable on a later one.
    """

    inner: Any
    names: frozenset[str] = field(default_factory=frozenset)
    attempts: int = 0

    async def commit(self, plan: Any) -> None:
        if plan.name in self.names:
            self.attempts += 1
            raise PersistenceError(PersistenceErrorCode.PERSISTENCE_CONFLICT, plan.name)
        await self.inner.commit(plan)

    async def resolve_outcome(self, plan: Any) -> object:
        return await self.inner.resolve_outcome(plan)


@dataclass(slots=True)
class RevokingSender:
    """A sender that tries to revoke a mandate from **inside** the SES call.

    The only moment a send fence is provably live is while the attempt it authorizes is in
    flight, so the mirror ordering can only be asserted from here.
    """

    harness: SendHarness
    refusals: list[BaseException] = field(default_factory=list)
    calls: list[SesEmailRequest] = field(default_factory=list)

    async def send(self, request: SesEmailRequest) -> SesOutcome:
        self.calls.append(request)
        try:
            await _revoke_one_mandate(self.harness)
        except Exception as error:
            self.refusals.append(error)
        return SesAccepted(message_id="ses-1")

    @property
    def call_count(self) -> int:
        return len(self.calls)


async def _revoke_one_mandate(send_harness: SendHarness) -> None:
    """Commit a real ``DecideMandate(REVOKE)`` against the case this send is about."""

    scope = send_harness.action.scope
    compile_harness = send_harness.action.compile
    mandate = compile_harness.fixture.context.mandates[0]
    await DecideMandate(
        core=compile_harness.core,
        audit=compile_harness.audit,
        idempotency=compile_harness.idempotency,
        unit_of_work=compile_harness.unit_of_work,
        clock=compile_harness.clock,
        ids=compile_harness.ids,
    ).execute(
        DecideMandateCommand(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            mandate_id=mandate.mandate_id,
            actor_contributor_id=mandate.contributor_id,
            actor_id_hash=Sha256Digest("sha256:" + "c" * 64),
            expected_version=mandate.version,
            decision=MandateDecision.REVOKE,
            fact_grants=(),
            identity_grant=IdentityGrant(
                externally_shareable=False, max_scope=mandate.identity_grant.max_scope
            ),
            expires_at=None,
            idempotency_key=f"revoke-{uuid4()}",
            destination_id=compile_harness.stored_destination().destination_id,
        )
    )


# ---------------------------------------------------------------------------------------
# R1-R3: a shared commit proof does not say who owns the claim
# ---------------------------------------------------------------------------------------


async def test_r1_a_lost_claim_outcome_after_a_foreign_send_makes_no_second_call(
    send_harness: SendHarness,
) -> None:
    """R1. Worker A sends; worker B's claim outcome is lost. Exactly **one** SES call.

    Before the repair, B's ambiguous claim was resolved against a commit proof keyed on the
    *execution*, so B read A's proof, concluded its own claim had committed, and called SES. Two
    deliberate attempts for one approved message.

    A shared proof says the execution was claimed. It does not say **which attempt** claimed it,
    and only the second question authorizes a sender.
    """

    await send_harness.prepare()
    await send_harness.approve()
    sender = ScriptedSender(default=SesAccepted(message_id="ses-1"))

    winner = await send_harness.send_action(sender=sender).execute(
        await send_harness.send_command(idempotency_key="send-key-a")
    )
    assert winner.state is ActionExecutionState.SENT
    assert sender.call_count == 1

    # Worker B still believes the row is APPROVED@1, and loses its claim's outcome.
    stale = replace(
        await send_harness.send_command(idempotency_key="send-key-b"),
        expected_execution_version=1,
    )
    outcome: SendActionResult | BaseException
    try:
        outcome = await _losing_send(send_harness, sender).execute(stale)
    except Exception as error:
        outcome = error

    assert sender.call_count == 1
    if isinstance(outcome, SendActionResult):
        assert outcome.ses_call_made is False


async def test_r1_barrier_race_one_worker_commits_the_claim_the_other_is_told_nothing(
    send_harness: SendHarness,
) -> None:
    """R1, at the exact interleaving the review reproduced.

    Worker A commits the claim and is then lost before it can record its result, so the row
    stands at ``SENDING`` with A's owner on it and one SES call made. Worker B arrives, its own
    claim transaction is genuinely lost -- not rejected, not conflicted -- and the shared proof
    A wrote is sitting right there. B must ask durable state who owns the claim, discover that
    it does not, and stop.
    """

    await send_harness.prepare()
    await send_harness.approve()
    send_harness.action.unit_of_work.fail_by_name["send-action-result"] = PersistenceError(
        PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME, "ACTION_EXECUTION"
    )
    worker_a = ScriptedSender(default=SesAccepted(message_id="ses-1"))
    with pytest.raises(PersistenceError):
        await send_harness.send(sender=worker_a)

    stranded = await send_harness.execution()
    assert stranded.state is ActionExecutionState.SENDING
    owner = stranded.claim_owner_hash
    assert owner is not None
    assert worker_a.call_count == 1

    worker_b = ScriptedSender(default=SesAccepted(message_id="ses-2"))
    stale = replace(
        await send_harness.send_command(idempotency_key="send-key-b"),
        expected_execution_version=1,
    )
    with contextlib.suppress(Exception):
        # A refusal is an acceptable answer here; the call count is the assertion.
        await _losing_send(send_harness, worker_b).execute(stale)

    assert worker_b.call_count == 0
    assert (await send_harness.execution()).claim_owner_hash == owner


async def test_r2_a_claim_that_did_not_commit_leaves_the_execution_safely_claimable(
    send_harness: SendHarness,
) -> None:
    """R2. Nothing committed, so the row stands at ``APPROVED`` and a later attempt may claim.

    The lost outcome is the only thing that happened: no proof exists, the row never moved, and
    the frozen recovery table says retrying the same claim under the same key is safe.
    """

    await send_harness.prepare()
    await send_harness.approve()
    first = ScriptedSender(default=SesAccepted(message_id="never"))

    with pytest.raises(PersistenceError):
        await _losing_send(send_harness, first).execute(await send_harness.send_command())

    assert first.call_count == 0
    assert (await send_harness.execution()).state is ActionExecutionState.APPROVED

    second = ScriptedSender(default=SesAccepted(message_id="ses-2"))
    result = await send_harness.send(sender=second)

    assert second.call_count == 1
    assert result.state is ActionExecutionState.SENT


async def test_r3_a_foreign_claim_owner_is_never_overwritten_and_never_sends(
    send_harness: SendHarness,
) -> None:
    """R3. A ``SENDING`` row another attempt owns is refused, and its owner is left intact."""

    await send_harness.prepare()
    await send_harness.approve()
    send_harness.action.unit_of_work.fail_by_name["send-action-result"] = PersistenceError(
        PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME, "ACTION_EXECUTION"
    )
    first = ScriptedSender(default=SesAccepted(message_id="ses-1"))
    with pytest.raises(PersistenceError):
        await send_harness.send(sender=first)
    owner = (await send_harness.execution()).claim_owner_hash
    assert owner is not None

    second = ScriptedSender(default=SesAccepted(message_id="ses-2"))
    result = await send_harness.send(sender=second)

    assert second.call_count == 0
    assert result.state is ActionExecutionState.SENDING
    assert result.ses_call_made is False
    assert (await send_harness.execution()).claim_owner_hash == owner


async def test_the_claim_owner_differs_between_two_attempts_on_one_execution(
    send_harness: SendHarness,
) -> None:
    """Every other derivation on the send path is shared; this one must not be.

    ``ses_request_token_hash`` and the send idempotency key are pure functions of durable
    values, so two workers compute them identically -- which is precisely why neither can
    answer "who claimed this".
    """

    from chorus.application.services.ses_message import send_claim_owner_hash

    await send_harness.prepare()
    await send_harness.approve()
    command = await send_harness.send_command()
    owners = {
        send_claim_owner_hash(
            namespace=command.namespace,
            action_id=command.action_id,
            execution_id=command.execution_id,
            claim_nonce=uuid4(),
        )
        for _ in range(4)
    }

    assert len(owners) == 4


# ---------------------------------------------------------------------------------------
# R4-R5: the close-validation / fence-acquisition race
# ---------------------------------------------------------------------------------------


async def test_r4_a_revocation_committed_before_the_send_denies_it(
    send_harness: SendHarness,
) -> None:
    """R4, the plain case: a real ``DecideMandate(REVOKE)``, then a send that must not happen."""

    await send_harness.prepare()
    await send_harness.approve()
    command = await send_harness.send_command()
    await _revoke_one_mandate(send_harness)

    sender = ScriptedSender(default=SesAccepted(message_id="never"))
    result = await send_harness.send_action(sender=sender).execute(command)

    assert sender.call_count == 0
    assert result.state is ActionExecutionState.FAILED
    assert SendFailureReason.STALE_AUTHORIZATION.value in result.reason_codes

    mandate = send_harness.action.compile.fixture.context.mandates[0]
    pointer = await send_harness.action.compile.core.load_current_mandate_pointer(
        send_harness.action.scope, mandate.mandate_id
    )
    live = await send_harness.action.compile.core.load_mandate_version(
        send_harness.action.scope, mandate.mandate_id, pointer.pointer.version
    )
    assert live.status is MandateStatus.REVOKED


async def test_r4_a_revocation_landing_between_the_claim_and_the_fence_denies_the_send(
    send_harness: SendHarness,
) -> None:
    """R4, at the seam. The revocation commits after the claim and before the fence exists.

    That instant is exactly the window the review exploited: the case had been validated, the
    fence was not yet held, and nothing looked again. The repair revalidates **inside** the
    fence, so the revocation is committed and visible by the time the decision is made.
    """

    await send_harness.prepare()
    await send_harness.approve()
    command = await send_harness.send_command()

    authorization = send_harness.authorization()
    authorization.core = HookedCore(authorization.core, lambda: _revoke_one_mandate(send_harness))
    use_case = send_harness.send_action()
    use_case.authorization = authorization
    sender = ScriptedSender(default=SesAccepted(message_id="never"))
    use_case.sender = sender

    result = await use_case.execute(command)

    assert sender.call_count == 0
    assert result.state is ActionExecutionState.FAILED
    assert SendFailureReason.STALE_AUTHORIZATION.value in result.reason_codes
    assert (await send_harness.action.compile.core.load_send_fence(send_harness.scope)) is None


async def test_r5_a_fence_held_first_refuses_the_revocation_for_its_window(
    send_harness: SendHarness,
) -> None:
    """R5, the mirror ordering. A live fence refuses a mandate decision; the message still goes.

    ADR-025 SS 5: if the fence commits first the revocation gets a retryable conflict for at
    most sixty seconds and the sent message cannot be recalled. The repair must not have
    inverted that -- a fence that no longer blocked a revocation would be a fence doing nothing.
    """

    await send_harness.prepare()
    await send_harness.approve()
    sender = RevokingSender(harness=send_harness)

    result = await send_harness.send_action(sender=sender).execute(
        await send_harness.send_command()
    )

    assert sender.call_count == 1
    assert result.state is ActionExecutionState.SENT
    assert sender.refusals, "a live fence must refuse a concurrent mandate decision"
    mandate = send_harness.action.compile.fixture.context.mandates[0]
    pointer = await send_harness.action.compile.core.load_current_mandate_pointer(
        send_harness.action.scope, mandate.mandate_id
    )
    assert pointer.pointer.version == mandate.version, "the revocation must not have committed"


# ---------------------------------------------------------------------------------------
# R6-R7: the clock crossing expiry during resolution
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("delayed", ["destination", "identity"])
async def test_r6_r7_a_resolution_that_crosses_the_fence_expiry_calls_nothing(
    send_harness: SendHarness, delayed: str
) -> None:
    """R6 and R7. The clock is re-read after **every** awaited resolution, just before SES.

    Before the repair the clock was sampled once, before two awaited registry lookups, and the
    time that passed between them reached SES unnoticed. Two awaits are two windows, and a check
    that closed only the first would be a check somebody had to remember to move when the second
    was added -- so both are parametrised over one assertion.
    """

    await send_harness.prepare()
    await send_harness.approve()
    over = SEND_FENCE_LIFETIME + timedelta(seconds=1)
    registry = DelayingRegistry(
        inner=send_harness.default_registry(),
        harness=send_harness,
        destination_delay=over if delayed == "destination" else timedelta(),
        identity_delay=over if delayed == "identity" else timedelta(),
    )
    sender = ScriptedSender(default=SesAccepted(message_id="never"))

    result = await send_harness.send_action(sender=sender, registry=registry).execute(
        await send_harness.send_command()
    )

    assert sender.call_count == 0
    assert result.state is ActionExecutionState.FAILED
    assert SendFailureReason.FENCE_EXPIRED.value in result.reason_codes


# ---------------------------------------------------------------------------------------
# R8 and worker recovery
# ---------------------------------------------------------------------------------------


async def test_r8_a_sent_execution_with_a_lost_projection_is_repaired_without_sending(
    send_harness: SendHarness,
) -> None:
    """R8. The send is durable and the case is not, so a replay still owes the case its edge.

    Before the repair the worker swallowed the projection failure, recorded the operation
    ``SUCCEEDED``, and every later delivery returned early on that terminal status. The case
    stayed ``ACTION_PROPOSED`` forever with a ``SENT`` execution beside it.
    """

    await send_harness.prepare()
    await send_harness.approve()
    job = await send_harness.send_job()

    sender = ScriptedSender(default=SesAccepted(message_id="ses-1"))
    worker = send_harness.worker(sender=sender)
    worker.project.unit_of_work = AlwaysFailingUnitOfWork(  # type: ignore[assignment]
        inner=send_harness.action.unit_of_work, names=frozenset({"project-action-outcome"})
    )
    stalled = await worker.execute(job)

    assert sender.call_count == 1
    assert (await send_harness.execution()).state is ActionExecutionState.SENT
    assert stalled.status is not ApplicationOperationStatus.SUCCEEDED
    case = await send_harness.action.compile.core.load_case(send_harness.action.scope)
    assert case.state is CaseState.ACTION_PROPOSED

    replay = ScriptedSender(default=SesAccepted(message_id="never"))
    finished = await send_harness.worker(sender=replay).execute(job)

    assert replay.call_count == 0
    assert finished.status is ApplicationOperationStatus.SUCCEEDED
    repaired = await send_harness.action.compile.core.load_case(send_harness.action.scope)
    assert repaired.state is CaseState.ACTIONED


async def test_a_terminal_operation_still_repairs_a_projection_it_owes(
    send_harness: SendHarness,
) -> None:
    """A ``SUCCEEDED`` operation is not proof the case moved, so a replay checks the case.

    This is the second half of the same defect: an operation that reached a terminal status
    before the projection landed -- by an older code path, or because another process settled
    it -- must still be answerable by the case rather than by the operation record.
    """

    await send_harness.prepare()
    await send_harness.approve()
    job = await send_harness.send_job()

    sender = ScriptedSender(default=SesAccepted(message_id="ses-1"))
    await send_harness.worker(sender=sender).execute(job)
    assert (
        await send_harness.action.compile.core.load_case(send_harness.action.scope)
    ).state is CaseState.ACTIONED

    # A third delivery over a terminal operation and an already-projected case moves nothing
    # and calls nothing.
    again = ScriptedSender(default=SesAccepted(message_id="never"))
    await send_harness.worker(sender=again).execute(job)

    assert again.call_count == 0
    final = await send_harness.action.compile.core.load_case(send_harness.action.scope)
    assert final.state is CaseState.ACTIONED


async def test_the_worker_resumes_an_uncommitted_ambiguous_claim(
    send_harness: SendHarness,
) -> None:
    """A ``RUNNING`` operation over an ``APPROVED`` execution is resumable, never stranded.

    Nothing committed, so there is nothing to be conservative about -- and reading ``APPROVED``
    is *positive evidence* that no claim was taken, because a claim that committed leaves the
    row at ``SENDING`` carrying its owner.
    """

    await send_harness.prepare()
    await send_harness.approve()
    job = await send_harness.send_job()

    first = ScriptedSender(default=SesAccepted(message_id="never"))
    stranding = send_harness.worker(sender=first)
    stranding.send_action.unit_of_work = StorageUnitOfWork(
        driver=LosingOutcomeDriver(
            inner=send_harness.action.compile.driver, lose=_writes_an_execution
        )
    )
    stalled = await stranding.execute(job)

    assert first.call_count == 0
    assert stalled.status is ApplicationOperationStatus.RUNNING
    assert (await send_harness.execution()).state is ActionExecutionState.APPROVED

    second = ScriptedSender(default=SesAccepted(message_id="ses-2"))
    resumed = await send_harness.worker(sender=second).execute(job)

    assert second.call_count == 1
    assert resumed.status is ApplicationOperationStatus.SUCCEEDED
    assert (await send_harness.execution()).state is ActionExecutionState.SENT


async def test_the_worker_replay_over_a_committed_claim_never_sends_again(
    send_harness: SendHarness,
) -> None:
    """A ``SENDING`` row inside its window is left exactly alone, and nothing is sent."""

    await send_harness.prepare()
    await send_harness.approve()
    job = await send_harness.send_job()

    send_harness.action.unit_of_work.fail_by_name["send-action-result"] = PersistenceError(
        PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME, "ACTION_EXECUTION"
    )
    first = ScriptedSender(default=SesAccepted(message_id="ses-1"))
    await send_harness.worker(sender=first).execute(job)
    assert first.call_count == 1
    assert (await send_harness.execution()).state is ActionExecutionState.SENDING

    second = ScriptedSender(default=SesAccepted(message_id="never"))
    await send_harness.worker(sender=second).execute(job)

    assert second.call_count == 0
    assert (await send_harness.execution()).state is ActionExecutionState.SENDING


# ---------------------------------------------------------------------------------------
# R9-R10: approval recovery
# ---------------------------------------------------------------------------------------


async def test_r9_a_committed_approval_whose_receipt_was_lost_replays_successfully(
    send_harness: SendHarness,
) -> None:
    """R9. The decision is durable; only the caller's receipt was lost.

    Before the repair the identical retry re-ran the checks, found the execution no longer
    ``DRAFT``, and answered ``EXECUTION_NOT_DRAFT`` -- telling a human their approval had failed
    while it sat committed one row away.
    """

    await send_harness.prepare()
    command = await send_harness.approval_command()
    send_harness.action.unit_of_work.fail_by_name["approve-action-complete"] = PersistenceError(
        PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME, "IDEMPOTENCY"
    )
    with pytest.raises(PersistenceError):
        await send_harness.approve_action().execute(command)

    committed = await send_harness.execution()
    assert committed.state is ActionExecutionState.APPROVED

    replayed = await send_harness.approve_action().execute(command)

    assert replayed.replayed is True
    assert replayed.execution_state is ActionExecutionState.APPROVED
    assert replayed.execution_version == committed.version
    assert replayed.approval_id == committed.approval_id


async def test_r10_an_approval_transaction_that_did_not_commit_is_safely_retried(
    send_harness: SendHarness,
) -> None:
    """R10. No proof exists, the execution is still ``DRAFT@1``, and the retry decides once."""

    await send_harness.prepare()
    command = await send_harness.approval_command()
    send_harness.action.unit_of_work.fail_by_name["approve-action"] = PersistenceError(
        PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME, "APPROVAL"
    )
    with pytest.raises(PersistenceError):
        await send_harness.approve_action().execute(command)
    assert (await send_harness.execution()).state is ActionExecutionState.DRAFT

    result = await send_harness.approve_action().execute(command)

    assert result.replayed is False
    assert (await send_harness.execution()).state is ActionExecutionState.APPROVED


async def test_an_ambiguous_approval_transaction_that_committed_is_not_decided_again(
    send_harness: SendHarness,
) -> None:
    """R9's sibling: the transaction's own outcome was lost, and it had committed.

    The unit of work resolves that from domain 2's proof and the command finishes. What this
    asserts is that the *next* identical arrival replays rather than deciding again, and that
    both answers name one approval.
    """

    await send_harness.prepare()
    command = await send_harness.approval_command()
    use_case = send_harness.approve_action()
    use_case.unit_of_work = StorageUnitOfWork(
        driver=LosingOutcomeDriver(
            inner=send_harness.action.compile.driver,
            lose=lambda operations: any(
                isinstance(item, PutItem) and item.key.sort_key.startswith("APPROVAL#")
                for item in operations
            ),
        )
    )
    with pytest.raises(PersistenceError):
        await use_case.execute(command)

    second = await send_harness.approve_action().execute(command)

    assert (await send_harness.execution()).state is ActionExecutionState.APPROVED
    third = await send_harness.approve_action().execute(command)
    assert third.replayed is True
    assert third.approval_id == second.approval_id


async def test_the_same_approval_key_carrying_a_different_decision_is_a_conflict(
    send_harness: SendHarness,
) -> None:
    """A second, different decision under one key is a conflict and never a correction."""

    await send_harness.prepare()
    await send_harness.approve(idempotency_key="approve-key-x")

    with pytest.raises(IdempotencyConflictError):
        await send_harness.approve_action().execute(
            await send_harness.approval_command(
                decision=ApprovalDecision.REJECTED, idempotency_key="approve-key-x"
            )
        )


async def test_recovery_refuses_a_proof_whose_artifacts_do_not_bind_this_request(
    send_harness: SendHarness,
) -> None:
    """Recovery verifies what it replays, so a request naming another execution answers nothing.

    A commit proof says a transaction committed. It does not say the rows it names are the rows
    this caller asked about, and a recovery that answered without checking would be a way to
    read one approval's outcome by presenting another's request.
    """

    await send_harness.prepare()
    command = await send_harness.approval_command()
    send_harness.action.unit_of_work.fail_by_name["approve-action-complete"] = PersistenceError(
        PersistenceErrorCode.UNKNOWN_TRANSACTION_OUTCOME, "IDEMPOTENCY"
    )
    with pytest.raises(PersistenceError):
        await send_harness.approve_action().execute(command)

    foreign = replace(command, execution_id=ExecutionId(uuid4()))
    with pytest.raises((ApprovalDeniedError, IdempotencyConflictError)):
        await send_harness.approve_action().execute(foreign)

    assert (await send_harness.execution()).state is ActionExecutionState.APPROVED


# ---------------------------------------------------------------------------------------
# R11-R12: approval-time configuration validation
# ---------------------------------------------------------------------------------------


async def test_r11_a_sending_identity_change_before_approval_is_refused(
    send_harness: SendHarness,
) -> None:
    """R11. ``from_identity_id`` is inside ``preview_hash``, so approval regenerates and compares.

    Before the repair, approval checked only values stored on the *view*, and the sending
    identity is not one of them. A human could approve a letterhead the deployment no longer
    had, and only the send fence would ever say so.
    """

    await send_harness.prepare()
    approve = send_harness.approve_action()
    approve.from_identity_id = "chorus-demo-sender-rotated"

    with pytest.raises(ApprovalDeniedError) as raised:
        await approve.execute(await send_harness.approval_command())

    assert raised.value.denial is ApprovalDenial.PREVIEW_BINDING_MOVED
    assert (await send_harness.execution()).state is ActionExecutionState.DRAFT


async def test_r12_a_template_version_change_before_approval_is_refused(
    send_harness: SendHarness,
) -> None:
    """R12. The template version is inside the same digest and is refused the same way."""

    await send_harness.prepare()
    approve = send_harness.approve_action()
    approve.template_version = f"{TEMPLATE_VERSION}-next"

    with pytest.raises(ApprovalDeniedError) as raised:
        await approve.execute(await send_harness.approval_command())

    assert raised.value.denial is ApprovalDenial.PREVIEW_BINDING_MOVED
    assert (await send_harness.execution()).state is ActionExecutionState.DRAFT


async def test_an_unchanged_deployment_still_approves(send_harness: SendHarness) -> None:
    """The regeneration must pass on the happy path, or it is a check nobody could satisfy."""

    await send_harness.prepare()
    approve = send_harness.approve_action()
    assert approve.from_identity_id == FROM_IDENTITY_ID

    result = await approve.execute(await send_harness.approval_command())

    assert result.execution_state is ActionExecutionState.APPROVED


async def test_a_rejection_is_never_blocked_by_a_moved_preview_binding(
    send_harness: SendHarness,
) -> None:
    """A human must always be able to say no, including to a proposal nothing can send.

    Checks 3 to 7b are all approval-only for one reason, and the new one inherits it: a reject
    path that staleness could block would leave a case holding a proposal nobody can approve
    and nobody can clear.
    """

    await send_harness.prepare()
    approve = send_harness.approve_action()
    approve.from_identity_id = "chorus-demo-sender-rotated"

    result = await approve.execute(
        await send_harness.approval_command(decision=ApprovalDecision.REJECTED)
    )

    assert result.decision is ApprovalDecision.REJECTED
    assert (await send_harness.execution()).state is ActionExecutionState.FAILED


# ---------------------------------------------------------------------------------------
# R13: the deployed authorization boundary, exercised end to end
# ---------------------------------------------------------------------------------------


def _decode_request(payload: dict[str, Any]) -> SendAuthorizationRequest:
    """The mirror of ``encode_authorization_request``, so the loopback crosses a real wire.

    Written out here rather than shared with production: a decoder built from the encoder would
    only prove the two agree with each other, and this one exists to prove the encoding carries
    every field the authority actually reads.
    """

    return SendAuthorizationRequest(
        namespace=Namespace(payload["namespace"]),
        community_id=CommunityId(UUID(payload["community_id"])),
        case_id=CaseId(UUID(payload["case_id"])),
        action_id=ActionId(UUID(payload["action_id"])),
        execution_id=ExecutionId(UUID(payload["execution_id"])),
        approval_id=ApprovalId(UUID(payload["approval_id"])),
        proposal_hash=Sha256Digest(payload["proposal_hash"]),
        view_id=ViewId(UUID(payload["view_id"])),
        view_hash=Sha256Digest(payload["view_hash"]),
        authorization_version=payload["authorization_version"],
        policy_version=payload["policy_version"],
        compiler_version=payload["compiler_version"],
        policy_build_hash=Sha256Digest(payload["policy_build_hash"]),
        destination_id=DestinationId(payload["destination_id"]),
        destination_registry_version=payload["destination_registry_version"],
        routing_token=UUID(payload["routing_token"]),
        purpose=Purpose(payload["purpose"]),
        authorization_snapshot_hash=Sha256Digest(payload["authorization_snapshot_hash"]),
        requested_at=datetime.fromisoformat(payload["requested_at"]),
    )


@dataclass(slots=True)
class LoopbackCompilerInvoker:
    """Serialize the request over the wire shape, answer from the real authority, serialize back.

    This is the deployed topology with the Lambda taken out and nothing else changed: the send
    holds no Core handle, its whole question crosses an encode/decode boundary, and the answer
    comes back as one of the two shapes the adapter is allowed to parse. A stub returning a
    ``SendAuthorizationGranted`` object directly would prove the send works and say nothing about
    whether the boundary can carry it.
    """

    authority: Any
    calls: list[str] = field(default_factory=list)

    async def invoke(self, *, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(operation)
        if operation == RELEASE_OPERATION:
            assert payload["schema"] == RELEASE_PAYLOAD_SCHEMA
            await self.authority.release(
                CaseScope(
                    namespace=Namespace(payload["namespace"]),
                    community_id=CommunityId(UUID(payload["community_id"])),
                    case_id=CaseId(UUID(payload["case_id"])),
                ),
                ExecutionId(UUID(payload["execution_id"])),
            )
            return {"outcome": "RELEASED"}
        assert payload["schema"] == FENCE_PAYLOAD_SCHEMA
        outcome = await self.authority.authorize(_decode_request(payload))
        if isinstance(outcome, SendAuthorizationDenied):
            return {"outcome": "DENIED", "reason_codes": list(outcome.reason_codes)}
        fence = outcome.fence
        return {
            "outcome": "GRANTED",
            "replayed": outcome.replayed,
            "fence": {
                "namespace": fence.namespace.value,
                "community_id": str(fence.community_id),
                "case_id": str(fence.case_id),
                "execution_id": str(fence.execution_id),
                "action_id": str(fence.action_id),
                "approval_id": str(fence.approval_id),
                "view_id": str(fence.view_id),
                "authorization_snapshot_hash": fence.authorization_snapshot_hash.value,
                "acquired_at": fence.acquired_at.isoformat(),
                "expires_at": fence.expires_at.isoformat(),
            },
        }


async def test_r13_a_send_over_the_compiler_boundary_succeeds_with_no_core_handle(
    send_harness: SendHarness,
) -> None:
    """R13. The deployed authorization shape carries a real send, end to end.

    The send command holds a :class:`CompilerSendAuthorization` and therefore no Core repository
    of any kind -- which is the only shape a role carrying ``Deny dynamodb:*`` on Core can run.
    Every case-side check still happens; it happens on the far side of an encode/decode boundary.
    """

    await send_harness.prepare()
    await send_harness.approve()
    invoker = LoopbackCompilerInvoker(authority=send_harness.authorization())
    use_case = send_harness.send_action()
    use_case.authorization = CompilerSendAuthorization(invoker=invoker)
    sender = ScriptedSender(default=SesAccepted(message_id="ses-deployed"))
    use_case.sender = sender

    result = await use_case.execute(await send_harness.send_command())

    assert result.state is ActionExecutionState.SENT
    assert sender.call_count == 1
    assert invoker.calls == [ACQUIRE_OPERATION, RELEASE_OPERATION]
    assert not hasattr(use_case, "core")
    assert (await send_harness.action.compile.core.load_send_fence(send_harness.scope)) is None


async def test_a_denial_carried_over_the_compiler_boundary_still_fails_closed(
    send_harness: SendHarness,
) -> None:
    """A revocation denies identically through the wire shape, and calls nothing."""

    await send_harness.prepare()
    await send_harness.approve()
    command = await send_harness.send_command()
    await _revoke_one_mandate(send_harness)

    invoker = LoopbackCompilerInvoker(authority=send_harness.authorization())
    use_case = send_harness.send_action()
    use_case.authorization = CompilerSendAuthorization(invoker=invoker)
    sender = ScriptedSender(default=SesAccepted(message_id="never"))
    use_case.sender = sender

    result = await use_case.execute(command)

    assert sender.call_count == 0
    assert result.state is ActionExecutionState.FAILED
    assert SendFailureReason.STALE_AUTHORIZATION.value in result.reason_codes


async def test_the_compiler_boundary_never_turns_an_unusable_answer_into_a_grant(
    send_harness: SendHarness,
) -> None:
    """An unreachable authority and an authorizing one must never be the same value.

    A boundary that defaulted on a malformed answer would be a boundary through which a broken
    compiler authorizes every send.
    """

    await send_harness.prepare()
    await send_harness.approve()

    class Nonsense:
        async def invoke(self, *, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {"outcome": "MAYBE"}

    use_case = send_harness.send_action()
    use_case.authorization = CompilerSendAuthorization(invoker=Nonsense())
    sender = ScriptedSender(default=SesAccepted(message_id="never"))
    use_case.sender = sender

    with pytest.raises(ExternalDependencyError):
        await use_case.execute(await send_harness.send_command())

    assert sender.call_count == 0


async def test_a_fence_granted_for_another_execution_is_refused_not_used(
    send_harness: SendHarness,
) -> None:
    """A well-formed grant about a different execution is not this send's authority."""

    await send_harness.prepare()
    await send_harness.approve()
    inner = LoopbackCompilerInvoker(authority=send_harness.authorization())

    class Substituting:
        async def invoke(self, *, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            body = await inner.invoke(operation=operation, payload=payload)
            if operation == ACQUIRE_OPERATION and body.get("outcome") == "GRANTED":
                body["fence"]["execution_id"] = str(uuid4())
            return body

    use_case = send_harness.send_action()
    use_case.authorization = CompilerSendAuthorization(invoker=Substituting())
    sender = ScriptedSender(default=SesAccepted(message_id="never"))
    use_case.sender = sender

    with pytest.raises(ExternalDependencyError):
        await use_case.execute(await send_harness.send_command())

    assert sender.call_count == 0
