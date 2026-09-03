"""Durable status for work that outlives the HTTP request that asked for it.

An ``ApplicationOperation`` exists so an agent invocation can be started by an API request,
executed by a worker, and polled by a browser without any of the three holding a connection
open. It is a status projection and nothing more: it carries identifiers, hashes, a status, and
an error code, never a command payload, never agent output, and never message text.

It is deliberately not a workflow engine. There is no step list, no scheduler, no compensation,
and no branching. The transitions are the four the frozen model names, plus one narrow
``RUNNING -> PENDING`` edge for a Monitor operation whose validated apply plan is already
frozen and whose remaining work is bounded deterministic writes -- see
:data:`_ALLOWED_TRANSITIONS` for why that edge is not a general retry.

Exclusivity is a condition, not a token
---------------------------------------
Every transition is a *single* conditional write on the operation row, guarded by its own
version. It is deliberately not a ``TransactWriteItems`` call: DynamoDB treats a repeated
client request token as an idempotent replay for ten minutes, so two byte-identical claims
made at the same injected instant -- which is exactly what two workers racing on one
redelivered job produce -- would both be told they succeeded, and both would invoke the model.
A bare conditional write has no token, so the condition is evaluated for every caller and
exactly one of them wins.

Nothing here sleeps, jitters, or randomises to break a tie. A race that is only usually won by
one party is not exclusivity.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from chorus.application import observability
from chorus.domain.entities import (
    ApplicationOperation,
    ApplicationOperationKind,
    ApplicationOperationStatus,
)
from chorus.domain.errors import StateTransitionError
from chorus.domain.ids import CaseId, IdGenerator, Namespace, OperationId, Sha256Digest
from chorus.domain.time import Clock, epoch_seconds_ceiling, format_utc
from chorus.ports.errors import (
    IdempotencyConflictError,
    NotFoundError,
    PersistenceConflictError,
)
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyFailedFinal,
    IdempotencyInProgress,
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotencyRecord,
    IdempotencyReplay,
    IdempotencyStarted,
    IdempotentCommand,
)
from chorus.ports.records import MessageFeedEntry
from chorus.ports.repositories import CoreRepositoryPort, IdempotencyRepositoryPort
from chorus.ports.scopes import NamespaceScope
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork
from chorus.privacy.canonical import hash_value

OPERATION_TTL = timedelta(days=7)
"""The frozen demo retention for an operation record.

Expiry is cleanup only. Whether an agent invocation happened is recorded by the durable
invocation item, which outlives this projection.
"""

MAX_WORKER_EXECUTION_WINDOW = timedelta(minutes=5)
"""How long a ``RUNNING`` operation may go without finishing before it is presumed lost.

It has to be comfortably longer than the whole timeout hierarchy an operation can legitimately
spend: two agent invocations at the configured agent timeout, plus the bounded apply steps,
plus slack. Anything shorter would declare a working worker dead and hand its operation to a
recovery path while it was still going.

It is not a lease and there is no lease framework here. Nothing renews it, nothing extends it,
and expiry grants nobody the right to start the work again -- it grants exactly one right, to
record that this attempt is over.
"""

OPERATION_STALE_ERROR_CODE = "OPERATION_STALE"

MONITOR_LOCATOR_SCHEMA = "monitor-locator-set/v1"


def monitor_locator_hash(locators: tuple[MessageFeedEntry, ...]) -> Sha256Digest:
    """Digest the exact set of new messages one Monitor operation is authorized to run over.

    This is the second half of the Monitor handover identity, and it exists because the first
    half is not enough on its own. A caller that keeps a valid ``operation_id``, actor, and
    request hash can still deliver a *different* message set under them -- fewer locators, an
    extra one, someone else's -- and a worker with nothing to compare against would build a
    frozen input from whatever arrived. The digest is written on the operation before the job
    is dispatched, so the durable record, not the delivery, decides what the set is.

    It covers each message's identifier **and** its ``sent_at``, because the instant is not
    decoration: the earliest new message is the anchor of the recent-context window, so moving
    one would change what the Monitor is shown without changing which messages it was given.

    It is deliberately **order-insensitive**. The endpoint takes a batch, the request hash
    already treats two orderings of the same messages as one command, and Monitor processing
    canonicalizes the order anyway -- so a client whose retry shuffled its array is making the
    same request, not a different one. Sorting on immutable identity is what makes that true
    without letting any other difference through.

    The digest carries identifiers and instants only. No message text and no attachment content
    enters it, and it is never reversible into the locator list it names.
    """

    ordered = sorted(
        (
            (str(locator.message_id), format_utc(locator.sent_at.astimezone(UTC)))
            for locator in locators
        ),
    )
    return hash_value(
        {
            "schema": MONITOR_LOCATOR_SCHEMA,
            "locators": [
                {"message_id": message_id, "sent_at": sent_at} for message_id, sent_at in ordered
            ],
        }
    )


OPERATION_ENTITY_TYPE = "APPLICATION_OPERATION"
INVOCATION_ENTITY_TYPE = "AGENT_INVOCATION"

_TERMINAL = frozenset({ApplicationOperationStatus.SUCCEEDED, ApplicationOperationStatus.FAILED})

_ALLOWED_TRANSITIONS: frozenset[tuple[ApplicationOperationStatus, ApplicationOperationStatus]] = (
    frozenset(
        {
            (ApplicationOperationStatus.PENDING, ApplicationOperationStatus.RUNNING),
            (ApplicationOperationStatus.PENDING, ApplicationOperationStatus.FAILED),
            (ApplicationOperationStatus.RUNNING, ApplicationOperationStatus.SUCCEEDED),
            (ApplicationOperationStatus.RUNNING, ApplicationOperationStatus.FAILED),
            (ApplicationOperationStatus.RUNNING, ApplicationOperationStatus.PENDING),
        }
    )
)
"""The five edges an operation may take, and the narrow reason there is a fifth.

``RUNNING -> PENDING`` exists because a Monitor operation whose validated plan is already
snapshotted is *finishable*: the model has answered, the answer is frozen, and the only work
left is bounded deterministic writes against durable state. Recording such an operation as
``FAILED`` would abandon valid committed state and make the remainder reachable only by a
human minting a new invocation -- a second pass over private text for work already paid for.

It is deliberately not a general retry edge, and it is deliberately not a new status. Callers
reach it through :meth:`ApplicationOperations.release_for_resume`, which refuses anything that
is not a Monitor operation, and the use case only asks for it when a frozen plan exists with
steps still outstanding. Everything else that ends an attempt still ends it.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class StartedOperation:
    """One operation plus the invocation identity bound to it, and whether it is a replay.

    ``invocation_id`` is the identity of the *agent call* the operation will make, and it must
    survive a replay: three identical requests under one idempotency key have to reach one model
    invocation, which they only can if the second and third recover the first one's invocation
    identity instead of minting their own. It is recorded twice on purpose -- in the idempotency
    record, which is what a replay reads, and on the operation row itself as
    ``monitor_invocation_id``, which is what a *worker* binds a delivered job against. The two
    are written by the same transaction, so they cannot disagree.
    """

    operation: ApplicationOperation
    invocation_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class StartReservation:
    """A claimed command key whose owning attempt has not yet created its operation.

    It is deliberately durable before anything else happens and deliberately *not* terminal.
    A crash between the reservation and the completing transaction leaves a record a same-hash
    retry recognises as its own unfinished attempt, which it may safely continue -- everything
    in between is replay-safe by construction.
    """

    key: IdempotencyKey
    record: IdempotencyRecord
    request_hash: Sha256Digest


@dataclass(slots=True)
class ApplicationOperations:
    """Create and advance operation records through the Phase 2 persistence ports."""

    core: CoreRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    clock: Clock
    ids: IdGenerator

    async def create(
        self,
        *,
        namespace: Namespace,
        kind: ApplicationOperationKind,
        actor_id_hash: Sha256Digest,
        request_hash: Sha256Digest,
        case_id: CaseId | None = None,
        monitor_invocation_id: UUID | None = None,
        monitor_locator_hash: Sha256Digest | None = None,
    ) -> ApplicationOperation:
        """Create a ``PENDING`` operation the caller may immediately return and poll."""

        operation = self._new_operation(
            namespace=namespace,
            kind=kind,
            actor_id_hash=actor_id_hash,
            request_hash=request_hash,
            case_id=case_id,
            monitor_invocation_id=monitor_invocation_id,
            monitor_locator_hash=monitor_locator_hash,
            now=self.clock.now(),
        )
        scope = NamespaceScope(namespace=namespace)
        await self.unit_of_work.commit(
            TransactionPlan(
                name="create-operation",
                operations=(self.core.stage_create_operation(scope, operation),),
                audit_required=False,
            )
        )
        return operation

    # -- the two-phase start ---------------------------------------------------------------

    async def reserve_start(
        self,
        *,
        namespace: Namespace,
        command: IdempotentCommand,
        actor_id_hash: Sha256Digest,
        key_hash: Sha256Digest,
        request_hash: Sha256Digest,
        correlation_id: UUID | None = None,
    ) -> StartReservation | StartedOperation:
        """Claim the command key *before* the caller mutates anything, or answer from the record.

        This is the half of command idempotency that has to happen first. A route that ingested
        messages and only then discovered its key belonged to a different request would already
        have written that different request's messages, and returning ``409`` afterwards would
        report a conflict over state the conflict says was never accepted. So the key is claimed
        against the normalized request hash before the first mutation, and a hash that disagrees
        is refused here, with nothing written.

        The claim is a *reservation*, not a completion, and that distinction is what keeps a
        crash recoverable. An ``IN_PROGRESS`` record under the same key **and the same hash** is
        this very request arriving again after an attempt that did not finish; it resumes,
        because everything between here and the completing transaction is replay-safe. Treating
        it as a conflict would be the old defect in a new place: a caller retrying its own
        identical request would be permanently refused for having once been interrupted.
        """

        key = self._start_key(namespace, command, actor_id_hash, key_hash)
        try:
            outcome = await self.idempotency.begin(
                key, request_hash=request_hash, now=self.clock.now()
            )
        except IdempotencyConflictError:
            observability.idempotency_conflict(
                namespace=namespace,
                community_id=None,
                correlation_id=correlation_id,
                actor_id_hash=actor_id_hash,
            )
            raise
        match outcome:
            case IdempotencyStarted(record=record) | IdempotencyInProgress(record=record):
                return StartReservation(key=key, record=record, request_hash=request_hash)
            case IdempotencyReplay(record=record):
                observability.idempotency_replay(
                    namespace=namespace,
                    community_id=None,
                    correlation_id=correlation_id,
                    actor_id_hash=actor_id_hash,
                )
                return await self._recover_recorded(namespace, record)
            case IdempotencyFailedFinal():
                # Nothing in this command family writes a terminal failure record, so one can
                # only mean the key was reused by a different command. There is no outcome to
                # replay and no attempt to resume.
                raise PersistenceConflictError(OPERATION_ENTITY_TYPE)
            case _:  # pragma: no cover - the outcome union is closed
                raise AssertionError("unreachable idempotency outcome")

    async def complete_start(
        self,
        reservation: StartReservation,
        *,
        namespace: Namespace,
        kind: ApplicationOperationKind,
        actor_id_hash: Sha256Digest,
        case_id: CaseId | None = None,
        monitor_locator_hash: Sha256Digest | None = None,
        correlation_id: UUID | None = None,
    ) -> StartedOperation:
        """Create the operation and complete the reservation in one transaction.

        The operation row and the completed record commit together or not at all, so there is
        never a completed key naming an operation that does not exist, and never an operation
        the key cannot lead a retry back to.

        A ``MONITOR`` operation is created already carrying its handover identity -- the
        invocation it authorizes and the digest of the exact new-message set it may be run
        over -- because the worker has to be able to refuse a misrouted *first* delivery, and a
        first delivery is precisely the one with no other durable record to disagree with.

        The answer is read back from the record rather than assumed. Two callers can hold the
        same reservation, and the losing one may still be told its ambiguous write committed --
        the record did reach the version its proof named, just not by its hand. Reading the
        record makes the durable binding, not the local optimism, decide what is returned.
        """

        now = self.clock.now()
        invocation_id = self.ids.new_uuid()
        is_monitor = kind is ApplicationOperationKind.MONITOR
        operation = self._new_operation(
            namespace=namespace,
            kind=kind,
            actor_id_hash=actor_id_hash,
            request_hash=reservation.request_hash,
            case_id=case_id,
            monitor_invocation_id=invocation_id if is_monitor else None,
            monitor_locator_hash=monitor_locator_hash if is_monitor else None,
            now=now,
        )
        refs = (
            EntityRef(
                entity_type=OPERATION_ENTITY_TYPE,
                entity_id=operation.operation_id.value,
                version=operation.version,
            ),
            EntityRef(entity_type=INVOCATION_ENTITY_TYPE, entity_id=invocation_id),
        )
        plan = TransactionPlan(
            name="create-operation",
            operations=(
                self.core.stage_create_operation(NamespaceScope(namespace=namespace), operation),
                self.idempotency.stage_complete(
                    reservation.record,
                    result_entity_refs=refs,
                    response_status=202,
                    now=now,
                ),
            ),
            audit_required=False,
            commit_proof=self.idempotency.completion_proof(reservation.record),
        )
        # A conditional failure means somebody else completed this reservation. Whatever they
        # created is the answer for both of us, and the read below is where that is settled.
        with suppress(PersistenceConflictError):
            await self.unit_of_work.commit(plan)
        record = await self.idempotency.load(reservation.key)
        if record is None or record.request_hash != reservation.request_hash:
            raise PersistenceConflictError(OPERATION_ENTITY_TYPE)
        recovered = await self._recover_recorded(namespace, record)
        if recovered.operation.operation_id != operation.operation_id:
            observability.idempotency_replay(
                namespace=namespace,
                community_id=None,
                correlation_id=correlation_id,
                actor_id_hash=actor_id_hash,
            )
            return recovered
        return StartedOperation(operation=operation, invocation_id=invocation_id, replayed=False)

    async def _recover_recorded(
        self, namespace: Namespace, record: IdempotencyRecord
    ) -> StartedOperation:
        """Answer from a completed record: the authoritative key-to-operation binding."""

        if not record.result_entity_refs:
            # A record with no result reference cannot name the operation it stands for. There
            # is nothing to return and nothing to create, so the caller polls.
            raise PersistenceConflictError(OPERATION_ENTITY_TYPE)
        return await self._recover(namespace, record.result_entity_refs)

    async def _recover(self, namespace: Namespace, refs: tuple[EntityRef, ...]) -> StartedOperation:
        operation_id = _ref_id(refs, OPERATION_ENTITY_TYPE)
        invocation_id = _ref_id(refs, INVOCATION_ENTITY_TYPE)
        if operation_id is None or invocation_id is None:
            raise PersistenceConflictError(OPERATION_ENTITY_TYPE)
        operation = await self.load(namespace=namespace, operation_id=OperationId(operation_id))
        return StartedOperation(operation=operation, invocation_id=invocation_id, replayed=True)

    @staticmethod
    def _start_key(
        namespace: Namespace,
        command: IdempotentCommand,
        actor_id_hash: Sha256Digest,
        key_hash: Sha256Digest,
    ) -> IdempotencyKey:
        return IdempotencyKey(
            partition=IdempotencyPartition(
                kind=IdempotencyPartitionKind.NAMESPACE, namespace=namespace
            ),
            command=command,
            actor_id_hash=actor_id_hash,
            key_hash=key_hash,
        )

    def _new_operation(
        self,
        *,
        namespace: Namespace,
        kind: ApplicationOperationKind,
        actor_id_hash: Sha256Digest,
        request_hash: Sha256Digest,
        case_id: CaseId | None,
        now: datetime,
        monitor_invocation_id: UUID | None = None,
        monitor_locator_hash: Sha256Digest | None = None,
    ) -> ApplicationOperation:
        return ApplicationOperation(
            operation_id=self.ids.new(OperationId),
            kind=kind,
            namespace=namespace,
            actor_id_hash=actor_id_hash,
            case_id=case_id,
            request_hash=request_hash,
            status=ApplicationOperationStatus.PENDING,
            result_refs=(),
            error_code=None,
            expires_at_epoch=epoch_seconds_ceiling(now + OPERATION_TTL),
            version=1,
            created_at=now,
            updated_at=now,
            monitor_invocation_id=monitor_invocation_id,
            monitor_locator_hash=monitor_locator_hash,
        )

    async def load(
        self, *, namespace: Namespace, operation_id: OperationId
    ) -> ApplicationOperation:
        return await self.core.load_operation(NamespaceScope(namespace=namespace), operation_id)

    async def find(
        self, *, namespace: Namespace, operation_id: OperationId
    ) -> ApplicationOperation | None:
        try:
            return await self.load(namespace=namespace, operation_id=operation_id)
        except NotFoundError:
            return None

    async def claim(self, operation: ApplicationOperation) -> ApplicationOperation:
        """Move ``PENDING`` to ``RUNNING``, or refuse.

        Asynchronous invocation may deliver the same operation twice. The version condition
        means exactly one delivery wins the claim; the other sees a conflict and stops rather
        than invoking the model a second time on the same private payload.
        """

        return await self._transition(operation, ApplicationOperationStatus.RUNNING)

    async def succeed(
        self, operation: ApplicationOperation, *, result_refs: tuple[UUID, ...]
    ) -> ApplicationOperation:
        return await self._transition(
            operation, ApplicationOperationStatus.SUCCEEDED, result_refs=result_refs
        )

    async def fail(
        self, operation: ApplicationOperation, *, error_code: str
    ) -> ApplicationOperation:
        return await self._transition(
            operation, ApplicationOperationStatus.FAILED, error_code=error_code
        )

    async def release_for_resume(self, operation: ApplicationOperation) -> ApplicationOperation:
        """Return an interrupted Monitor operation to ``PENDING`` so a redelivery finishes it.

        Guarded by the operation's own version like every other transition, so a recovery
        worker that has already declared this attempt over keeps its terminal record and this
        caller loses the race rather than resurrecting it.

        Restricted to ``MONITOR`` by kind rather than by convention. Every other operation
        family either has an external side effect or has no frozen plan to resume, and for
        those "put it back in the queue" would mean re-running work whose repeat is not free.
        """

        if operation.kind is not ApplicationOperationKind.MONITOR:
            raise StateTransitionError(str(operation.operation_id))
        return await self._transition(operation, ApplicationOperationStatus.PENDING)

    def is_terminal(self, operation: ApplicationOperation) -> bool:
        return operation.status in _TERMINAL

    def is_stale(self, operation: ApplicationOperation, *, now: datetime) -> bool:
        """True when a ``RUNNING`` operation has outlived any legitimate worker.

        Deliberately conservative and deliberately one-directional: it says only that this
        attempt cannot still be in flight, never that the work should be started again.
        """

        return (
            operation.status is ApplicationOperationStatus.RUNNING
            and now - operation.updated_at > MAX_WORKER_EXECUTION_WINDOW
        )

    async def abandon_if_stale(
        self, operation: ApplicationOperation
    ) -> ApplicationOperation | None:
        """Record that a lost worker's attempt is over, or leave the operation alone.

        The transition is ``RUNNING -> FAILED`` under the row's own version, so two recovery
        workers arriving together produce exactly one terminal transition and the loser sees
        a conflict. The original worker, if it was merely slow rather than lost, later finds
        its own expected version gone and cannot overwrite the terminal state either.

        No model invocation is ever launched from here. A ``RUNNING`` operation is ambiguous
        by construction -- the worker may have called the model already -- so the only safe
        automatic act is to stop pretending it is still running. Starting the work again is a
        human decision, made through the command and idempotency rules like any other.
        """

        now = self.clock.now()
        if not self.is_stale(operation, now=now):
            return None
        try:
            return await self.fail(operation, error_code=OPERATION_STALE_ERROR_CODE)
        except PersistenceConflictError:
            return None

    async def _transition(
        self,
        operation: ApplicationOperation,
        target: ApplicationOperationStatus,
        *,
        result_refs: tuple[UUID, ...] = (),
        error_code: str | None = None,
    ) -> ApplicationOperation:
        if (operation.status, target) not in _ALLOWED_TRANSITIONS:
            raise StateTransitionError(str(operation.operation_id))
        now: datetime = self.clock.now()
        updated = replace(
            operation,
            status=target,
            result_refs=result_refs,
            error_code=error_code,
            version=operation.version + 1,
            updated_at=now,
        )
        # A single conditional write rather than a transaction. See the module docstring: a
        # transaction's deterministic client request token makes two byte-identical claims at
        # one instant both succeed, which is the opposite of what a claim is for.
        await self.core.apply_operation_transition(
            NamespaceScope(namespace=operation.namespace),
            updated,
            expected_version=operation.version,
        )
        return updated


def _ref_id(refs: tuple[EntityRef, ...], entity_type: str) -> UUID | None:
    for ref in refs:
        if ref.entity_type == entity_type:
            return ref.entity_id
    return None
