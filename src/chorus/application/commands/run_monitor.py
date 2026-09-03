"""The ``RunMonitor`` use case: freeze the input, invoke once, freeze the plan, apply it.

This is the whole orchestration, written out rather than configured. Every trust-zone crossing
is a line you can point at: what was loaded, what was projected, what was sent, what came back,
what was proved about it, and what was written. There is no router, no graph, and no step the
model gets to choose.

Three durable stages, and why the boundaries are where they are
---------------------------------------------------------------
A Monitor operation is not one indivisible act. It is a **frozen input**, then a **validated
apply plan**, then **apply progress** -- three durable stages with one rule joining them:

    a partially applied Monitor operation must never need another model invocation.

So the order is fixed. The exact bounded ``MonitorInput`` is built **once** and snapshotted
before the model sees it. The answer is validated whole, turned into a deterministic ordered
plan, and that plan is snapshotted **before apply step one**. Only then does anything mutate,
and each step advances a progress row inside its own transaction.

Redelivery reads before it builds:

* **an input snapshot exists** -- load it; the context is never rebuilt. That is what makes
  "same ``invocation_id``" mean "same ``MonitorInput``" rather than "same three identifiers
  and whatever the community happens to look like now";
* **a plan snapshot exists** -- load it; the model is never invoked again. The invocation is
  permanently complete from the moment that snapshot lands;
* **progress exists** -- resume at the first incomplete step, and only there.

The last step of every plan is the **finalization step**: the durable record that says this
invocation succeeded, committed in the same transaction that advances progress to complete.
That ordering is the point. ``progress.is_complete`` therefore implies the successful
invocation record exists, so there is no window in which every data write landed and the run
was still recorded as a failure -- and a redelivery that finds the data steps done but the
finalization missing finishes exactly that one step, with no model call and no new mutation.

Context is built here, not supplied
-----------------------------------
The batch the Monitor sees is the newly ingested messages *plus* a bounded window of recent
prior community messages, capped at the frozen fifty. That is what makes discovery converge:
twenty-four messages posted in one request and the same twenty-four posted in four requests of
six have to reach the same conclusion, and they only can if a later batch is allowed to see
what came before it. It is also what keeps a below-threshold observation from being lost --
its messages are still ordinary community messages, so a later run sees them again beside
whatever finally corroborates them.

The cases the Monitor may extend are discovered the same way: the feed signals of exactly
those messages, by direct key, then a bounded strong read of the cases they name. No scan, no
GSI, and no case identifier from the caller -- a client that could name a case would be doing
the discovering.

That whole assembly happens exactly once per invocation identity, which is what the input
snapshot is for. A redelivery must not be shown the case its own first delivery created and
asked to reason about it again.

Retry has exactly one shape. A timeout or a transient runtime failure is retried once with the
*same* invocation identity, because no output was persisted and the second attempt must be the
same request rather than a new one. A contract violation is never retried: the answer was
refused for a reason repeating the request cannot change, and a second invocation would spend
another pass over private text to be told the same thing.

Interruption is not failure
---------------------------
A storage failure part-way through a *frozen* plan is not a verdict on the answer. It leaves
valid committed state and a plan that can still be finished, so it raises
:class:`MonitorApplyInterruptedError`, the worker returns the operation to ``PENDING``, and a
redelivery resumes with zero model calls. A deterministic conflict -- a case that moved to a
version the frozen plan does not expect -- is the opposite: it is non-resumable, it is never
re-planned under the old invocation, and it settles as ``PARTIAL_APPLY_CONFLICT`` with the
already-committed state left exactly as it stands.

Every terminal outcome, success or failure, leaves a durable invocation record carrying hashes
and a safe code -- never output, never prompt text, never a provider response.

Once that record is durable the operation's own ``SUCCEEDED`` transition is independently
replayable: it writes nothing but a status, so a lost response is settled by re-reading the
operation, and a completed plan is never aged out into ``FAILED`` because a status write was
the only thing that went missing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID

from chorus.application import observability
from chorus.application.services.identity import derive_audit_event_id
from chorus.application.services.monitor_apply import (
    MONITOR_LINKABLE_CASE_STATES,
    ApplyStep,
    ApplyStepDescriptor,
    CurrentApplyState,
    GroupIdentity,
    MonitorApplicationPlan,
    MonitorApplyDenial,
    MonitorApplyDeniedError,
    PartialApplyConflictError,
    PlannedCase,
    candidate_audit_event,
    derive_identities,
    intended_case_ids,
    intended_fact_slot_ids,
    intended_message_ids,
    plan_monitor_application,
)
from chorus.application.services.monitor_projection import (
    ProjectionError,
    UnattributableBatchError,
    project_monitor_input,
)
from chorus.application.services.monitor_snapshots import (
    FrozenMonitorInput,
    FrozenMonitorPlan,
    MonitorSnapshots,
    output_hash_of,
)
from chorus.application.services.monitor_validation import (
    ValidatedCandidateGroup,
    ValidatedMonitorOutput,
    validate_monitor_result,
)
from chorus.contracts.common import (
    AGENT_INPUT_SCHEMA_VERSION,
    MONITOR_PROMPT_VERSION,
    AgentInputEnvelope,
    AgentName,
)
from chorus.contracts.monitor import (
    MAX_CANDIDATE_SUMMARIES,
    MAX_MESSAGES_PER_BATCH,
    IssueType,
    MonitorAttachmentDescriptor,
    MonitorCandidateSummary,
    MonitorInput,
)
from chorus.domain.entities import CommunityCase, CommunityMessage
from chorus.domain.errors import DomainError, ValidationError
from chorus.domain.facts import Fact, IncidentOccurrence, LocationArea, ServiceImpact
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    FactId,
    MessageId,
    Namespace,
    OperationId,
    Sha256Digest,
)
from chorus.domain.time import Clock, epoch_seconds_ceiling, format_utc
from chorus.ports.agents import (
    AgentContractViolationError,
    AgentError,
    AgentErrorCode,
    AgentRejection,
    MonitorAgentPort,
    MonitorInvocation,
    MonitorResult,
)
from chorus.ports.ambient import AttachmentCatalogPort
from chorus.ports.errors import NotFoundError, PersistenceError
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotentCommand,
)
from chorus.ports.pagination import PageRequest
from chorus.ports.records import (
    AgentInvocationOutcome,
    AgentInvocationResult,
    FeedSignalProjection,
    MessageFeedEntry,
    MonitorApplyProgress,
)
from chorus.ports.records import AgentName as StoredAgentName
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    IdempotencyRepositoryPort,
)
from chorus.ports.scopes import CaseScope, CommunityScope, OperationScope
from chorus.ports.storage import WriteOperation
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork
from chorus.privacy.canonical import hash_value

MAX_CANDIDATE_SUMMARY_FACTS = 20
MONITOR_APPLY_TRANSACTION = "apply-monitor-output"
MONITOR_FINALIZE_TRANSACTION = "finalize-monitor-output"
POLICY_VERSION = "policy/v1"

FINALIZATION_STEPS = 1
"""How many steps of a Monitor apply plan are the finalization step: exactly one, always.

An apply plan is not "the data writes"; it is the data writes *and* the record that says the
invocation succeeded. Counting the second in ``total_steps`` is what makes
``progress.is_complete`` mean "this invocation is durably finished" rather than "the last row
of user-visible data landed, and something else may or may not have followed it".
"""

SNAPSHOT_RETENTION = timedelta(days=7)
"""How long a frozen input or validated plan outlives the invocation that created it.

The same window as the operation record it lives beside, for the same reason: expiry is
cleanup, and the durable invocation record -- which outlives both -- is what says whether an
agent invocation ever happened.
"""

NO_ATTRIBUTABLE_MESSAGES = "NO_ATTRIBUTABLE_MESSAGES"
"""The safe result code of a batch that had nothing the Monitor could reason about."""

MONITOR_PROJECTION_INVALID = "MONITOR_PROJECTION_INVALID"

RECENT_CONTEXT_WINDOW = timedelta(days=21)
"""How far back a Monitor batch may look for corroborating context.

Long enough that a slow-building pattern -- one complaint a week for three weeks -- is still
visible from the newest message, and short enough that a community's whole history never
becomes the payload. The bound that actually protects the payload is the fifty-message cap;
this one keeps the query from having to reach past anything that could still be relevant.
"""


class MonitorApplyInterruptedError(Exception):
    """A frozen apply plan was interrupted by storage, and the rest of it can still run.

    Deliberately not a :class:`~chorus.ports.errors.PersistenceError` and deliberately not a
    :class:`~chorus.domain.errors.DomainError`, because it is neither a storage verdict nor a
    domain one -- it is a statement about *this operation's lifecycle*: the model has already
    answered, the answer is frozen, some steps are durable, and the remainder is bounded
    deterministic work that a later delivery can finish without spending anything.

    Recording it as a failure would be the expensive mistake. It would abandon valid committed
    state and make the remainder reachable only by a human minting a new invocation, which is
    a second pass over private text for work that has already been paid for.
    """

    __slots__ = ("safe_code",)

    def __init__(self, safe_code: str) -> None:
        super().__init__(safe_code)
        self.safe_code = safe_code


class MonitorProjectionFailedError(DomainError):
    """The batch could not be projected safely, so it was refused rather than trimmed.

    A raw ``ProjectionError`` escaping the use case would reach an at-least-once dispatcher as
    an unmapped exception and strand the operation in ``RUNNING``. This is the closed typed
    translation: a safe code, no quotation, and a worker that can settle the operation.
    """

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__(ValidationError().code, MONITOR_PROJECTION_INVALID)

    @property
    def safe_code(self) -> str:
        return MONITOR_PROJECTION_INVALID


@dataclass(frozen=True, slots=True, kw_only=True)
class RunMonitorCommand:
    """One Monitor run over an explicit, already-persisted batch of messages.

    The *new* messages are named by locator rather than rediscovered by query, so the thing
    the command is about cannot drift between the request and its retry. What the Monitor
    actually reads is then assembled deterministically from those locators exactly once, and
    snapshotted -- so a redelivery is answered from the frozen payload rather than from a
    community that has moved on since.

    There is deliberately no ``candidate_case_ids`` field. Which cases the Monitor may extend
    is discovered from the signals of the messages in its own context window; accepting them
    from a caller would let the caller decide what the discovery is allowed to find.
    """

    namespace: Namespace
    community_id: CommunityId
    operation_id: OperationId
    invocation_id: UUID
    correlation_id: UUID
    actor_id_hash: Sha256Digest
    message_locators: tuple[MessageFeedEntry, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class RunMonitorResult:
    """What one Monitor run changed, in counts and identifiers only."""

    case_ids: tuple[CaseId, ...]
    created_case_ids: tuple[CaseId, ...]
    report_count: int
    fact_count: int
    noise_message_count: int
    policy_like_message_count: int
    skipped_below_threshold: int
    replayed: bool
    noop_reason_code: str | None = None

    @property
    def result_refs(self) -> tuple[UUID, ...]:
        return tuple(case_id.value for case_id in self.case_ids)


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitorContext:
    """The bounded batch and case summaries one invocation is allowed to reason over."""

    messages: tuple[CommunityMessage, ...]
    summaries: tuple[MonitorCandidateSummary, ...]


@dataclass(slots=True)
class RunMonitor:
    """Invoke the Monitor once and apply exactly what deterministic validation allows."""

    core: CoreRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    agent: MonitorAgentPort
    attachments: AttachmentCatalogPort
    snapshots: MonitorSnapshots
    clock: Clock

    async def execute(self, command: RunMonitorCommand) -> RunMonitorResult:
        now = self.clock.now()
        operation_scope = OperationScope(
            namespace=command.namespace, operation_id=command.operation_id
        )

        # Read the frozen stages before building anything. A redelivery that assembled its
        # context first would already have spent the work the snapshot exists to avoid, and
        # would have assembled it from a community that has moved on since.
        frozen_input = await self.snapshots.load_input(operation_scope, command.invocation_id)
        if frozen_input is not None:
            _require_same_command(command, frozen_input)
        replay = await self._replay_recorded_invocation(command, operation_scope, frozen_input)
        if replay is not None:
            return replay

        if frozen_input is None:
            if await self.snapshots.has_plan(operation_scope, command.invocation_id):
                # A plan cannot be re-proved without the input it was reasoned about, and
                # re-deriving that input would be reasoning against a different world. The
                # refusal is made from the manifest alone, so nothing is reassembled to
                # discover it -- and so ``load_plan`` can *require* the input it proves
                # against rather than treating it as optional.
                raise AgentContractViolationError((AgentRejection.ENVELOPE_MISMATCH,))
            built = await self._freeze_input(command, operation_scope, now=now)
            if built is None:
                return self._noop(command, NO_ATTRIBUTABLE_MESSAGES)
            return await self._invoke_and_apply(command, operation_scope, built, now=now)

        frozen_plan = await self.snapshots.load_plan(
            operation_scope, command.invocation_id, frozen_input=frozen_input
        )
        if frozen_plan is not None:
            return await self._resume(command, operation_scope, frozen_input, frozen_plan)

        return await self._invoke_and_apply(command, operation_scope, frozen_input, now=now)

    # -- the first delivery -------------------------------------------------------------

    async def _invoke_and_apply(
        self,
        command: RunMonitorCommand,
        operation_scope: OperationScope,
        frozen: FrozenMonitorInput,
        *,
        now: datetime,
    ) -> RunMonitorResult:
        """Invoke the model once, freeze the plan its answer implies, then apply it."""

        invocation = frozen.invocation
        input_hash = frozen.input_hash
        self._emit_invocation_started(command, invocation.payload, input_hash, attempt=1)

        try:
            result = await self._invoke_with_one_retry(command, invocation, input_hash)
            validated = validate_monitor_result(
                invocation=invocation,
                result=result,
                namespace=command.namespace,
                contributor_by_pseudonym=frozen.contributor_by_pseudonym,
            )
        except AgentError as error:
            await self._record_failed_invocation(
                command=command,
                operation_scope=operation_scope,
                input_hash=input_hash,
                error_code=error.code.value,
                now=now,
            )
            self._emit_agent_failure(command, input_hash, error)
            raise

        output_hash = output_hash_of(result)
        observability.prompt_injection_observed(
            namespace=command.namespace,
            community_id=command.community_id,
            correlation_id=command.correlation_id,
            invocation_id=command.invocation_id,
            observed_count=len(validated.policy_like_message_ids),
        )

        try:
            plan = await self._plan(command, validated, now=now)
        except (AgentError, DomainError, PersistenceError) as error:
            # Planning is pure arithmetic over strongly read state and has written nothing,
            # so this is a verdict on the answer rather than an interruption of it.
            await self._record_failed_invocation(
                command=command,
                operation_scope=operation_scope,
                input_hash=input_hash,
                error_code=_safe_error_code(error),
                now=now,
            )
            self._emit_apply_denial(command, error)
            raise

        frozen_plan = await self.snapshots.freeze_plan(
            operation_scope,
            community_id=command.community_id,
            result=result,
            steps=plan.descriptors,
            planned_at=now,
            input_hash=input_hash,
            output_hash=output_hash,
            prompt_version=MONITOR_PROMPT_VERSION,
            now=now,
            expires_at_epoch=self._snapshot_expiry(now),
        )
        return await self._apply_frozen_plan(
            command, operation_scope, validated, plan, frozen_plan, now=now
        )

    # -- redelivery of an already-answered invocation ------------------------------------

    async def _resume(
        self,
        command: RunMonitorCommand,
        operation_scope: OperationScope,
        frozen_input: FrozenMonitorInput,
        frozen_plan: FrozenMonitorPlan,
    ) -> RunMonitorResult:
        """Finish a frozen plan without invoking anything.

        The validated output is re-derived deterministically from the two snapshots rather
        than trusted as stored application state: validation is a pure function of the frozen
        input and the frozen result, so re-running it costs nothing and keeps the invariant
        that nothing is applied which has not passed the whole-output gates in this process.
        """

        progress = await self.core.load_monitor_progress(operation_scope, command.invocation_id)
        completed = 0 if progress is None else progress.completed_steps
        observability.operation_resumed(
            namespace=command.namespace,
            community_id=command.community_id,
            operation_id=command.operation_id,
            invocation_id=command.invocation_id,
            correlation_id=command.correlation_id,
            completed_steps=completed,
            total_steps=len(frozen_plan.steps) + FINALIZATION_STEPS,
        )

        validated = validate_monitor_result(
            invocation=frozen_input.invocation,
            result=frozen_plan.result,
            namespace=command.namespace,
            contributor_by_pseudonym=frozen_input.contributor_by_pseudonym,
        )
        # The versions the agent was shown are the versions of a world that this invocation's
        # own committed steps have since advanced. Rebinding them to what those steps left
        # behind is what lets the eligibility gate distinguish "we moved this case ourselves"
        # from "somebody else did", which is the whole question on a resume.
        rebound = _rebind_expected_versions(validated, frozen_plan.steps[:completed])
        try:
            plan = await self._plan(command, rebound, now=frozen_plan.planned_at)
        except (AgentError, DomainError, PersistenceError) as error:
            raise _non_resumable(error, completed) from error
        return await self._apply_frozen_plan(
            command,
            operation_scope,
            validated,
            plan,
            frozen_plan,
            now=frozen_plan.planned_at,
        )

    # -- context -------------------------------------------------------------------------

    async def _freeze_input(
        self, command: RunMonitorCommand, operation_scope: OperationScope, *, now: datetime
    ) -> FrozenMonitorInput | None:
        """Assemble the exact bounded payload once and persist it, or report a no-op batch.

        ``None`` means the batch held nothing the Monitor could reason about. That is ambient
        noise rather than an application fault: an unattributable message can only ever
        produce a report nobody owns, so the honest outcome is a successful run that changed
        nothing, not a crashed worker and a stranded operation.
        """

        scope = CommunityScope(namespace=command.namespace, community_id=command.community_id)
        context = await self._build_context(command, scope)
        pseudonyms = await self._load_pseudonyms(scope, context.messages)
        attachments = self._describe_attachments(context.messages)

        try:
            projection = project_monitor_input(
                messages=context.messages,
                pseudonyms=pseudonyms,
                attachments=attachments,
                candidate_summaries=context.summaries,
            )
        except UnattributableBatchError:
            observability.monitor_batch_noop(
                namespace=command.namespace,
                community_id=command.community_id,
                operation_id=command.operation_id,
                invocation_id=command.invocation_id,
                correlation_id=command.correlation_id,
                reason_code=NO_ATTRIBUTABLE_MESSAGES,
            )
            return None
        except ProjectionError as error:
            # A descriptor the application cannot render safely is a genuine integrity
            # failure, and it is translated here rather than allowed to escape as a bare
            # ``ValueError`` that no worker knows how to settle.
            raise MonitorProjectionFailedError() from error

        return await self.snapshots.freeze_input(
            operation_scope,
            community_id=command.community_id,
            invocation=_envelope(command, projection.payload, now=now),
            contributor_by_pseudonym=projection.contributor_by_pseudonym,
            command_message_ids=tuple(
                locator.message_id.value for locator in command.message_locators
            ),
            prompt_version=MONITOR_PROMPT_VERSION,
            now=now,
            expires_at_epoch=self._snapshot_expiry(now),
        )

    async def _build_context(
        self, command: RunMonitorCommand, scope: CommunityScope
    ) -> MonitorContext:
        """Assemble the bounded batch and the cases it may extend."""

        new_messages = await self._load_messages(scope, command.message_locators)
        anchor = min(message.sent_at for message in new_messages)
        prior = await self.core.read_recent_messages(
            scope, before=anchor, limit=MAX_MESSAGES_PER_BATCH
        )

        by_id: dict[MessageId, CommunityMessage] = {
            message.message_id: message for message in new_messages
        }
        horizon = anchor - RECENT_CONTEXT_WINDOW
        for message in prior:
            if message.sent_at >= anchor:
                # The descending query is inclusive of the anchor instant, so it can hand back
                # the newest messages themselves. They are already in the batch.
                continue
            if message.sent_at < horizon:
                continue
            by_id.setdefault(message.message_id, message)
        ordered = sorted(by_id.values(), key=lambda item: (item.sent_at, str(item.message_id)))
        # When the window overflows the frozen bound the *newest* messages are kept, because
        # the batch is about what just happened and older context is what gets sacrificed.
        messages = tuple(ordered[-MAX_MESSAGES_PER_BATCH:])

        summaries = await self._candidate_summaries(scope, messages)
        return MonitorContext(messages=messages, summaries=summaries)

    async def _load_messages(
        self, scope: CommunityScope, locators: tuple[MessageFeedEntry, ...]
    ) -> tuple[CommunityMessage, ...]:
        if not locators:
            raise AgentContractViolationError((AgentRejection.MESSAGE_RESULT_COVERAGE,))
        loaded = [await self.core.load_message(scope, locator) for locator in locators]
        return tuple(sorted(loaded, key=lambda item: (item.sent_at, str(item.message_id))))

    async def _candidate_summaries(
        self, scope: CommunityScope, messages: tuple[CommunityMessage, ...]
    ) -> tuple[MonitorCandidateSummary, ...]:
        """Summarize the eligible cases the messages in this window already point at.

        The signals are fetched by exact message key, so this is a bounded batch get rather
        than a walk of the signal prefix, and the cases are then read strongly: what the agent
        is shown about a case has to be what the case currently is, because the version it
        sees is the version the apply step will insist on.
        """

        signals = await self.core.load_feed_signals(
            scope, tuple(message.message_id for message in messages)
        )
        case_ids = tuple(dict.fromkeys(signal.case_id for signal in signals.values()))
        cases: list[CommunityCase] = []
        for case_id in case_ids:
            case_scope = CaseScope(
                namespace=scope.namespace, community_id=scope.community_id, case_id=case_id
            )
            try:
                case = await self.core.load_case(case_scope)
            except NotFoundError:
                continue
            if case.state not in MONITOR_LINKABLE_CASE_STATES:
                # A terminal case cannot receive an intake-linked report, so offering it as an
                # extension candidate would only invite a proposal the apply gate must refuse.
                continue
            cases.append(case)

        # Most recently touched first, then by identifier, so the twenty the agent is shown are
        # the twenty most likely to still be live and the choice is reproducible.
        cases.sort(key=lambda item: (item.updated_at, str(item.case_id)), reverse=True)
        summaries: list[MonitorCandidateSummary] = []
        for case in cases[:MAX_CANDIDATE_SUMMARIES]:
            case_scope = CaseScope(
                namespace=scope.namespace, community_id=scope.community_id, case_id=case.case_id
            )
            facts = await self.core.read_case_facts(
                case_scope, PageRequest(limit=MAX_CANDIDATE_SUMMARY_FACTS)
            )
            summaries.append(
                MonitorCandidateSummary(
                    case_id=case.case_id.value,
                    case_version=case.version,
                    title=case.title,
                    issue_type=IssueType(case.issue_type),
                    location_area=None,
                    fact_summaries=tuple(_fact_summary(fact) for fact in facts.items),
                )
            )
        return tuple(summaries)

    async def _load_pseudonyms(
        self, scope: CommunityScope, messages: tuple[CommunityMessage, ...]
    ) -> dict[ContributorId, str]:
        contributor_ids = sorted(
            {message.contributor_id for message in messages if message.contributor_id is not None},
            key=str,
        )
        pseudonyms: dict[ContributorId, str] = {}
        for contributor_id in contributor_ids:
            contributor = await self.core.load_contributor(scope, contributor_id)
            pseudonyms[contributor_id] = contributor.pseudonym
        return pseudonyms

    def _describe_attachments(
        self, messages: tuple[CommunityMessage, ...]
    ) -> dict[EvidenceItemId, MonitorAttachmentDescriptor]:
        descriptors: dict[EvidenceItemId, MonitorAttachmentDescriptor] = {}
        for message in messages:
            for evidence_id in message.attachment_ids:
                attachment = self.attachments.describe(evidence_id)
                if attachment is None:
                    continue
                descriptors[evidence_id] = MonitorAttachmentDescriptor(
                    evidence_id=evidence_id.value,
                    media_type=attachment.media_type,
                    safe_caption=attachment.safe_caption,
                )
        return descriptors

    # -- invocation ---------------------------------------------------------------------

    async def _replay_recorded_invocation(
        self,
        command: RunMonitorCommand,
        operation_scope: OperationScope,
        frozen: FrozenMonitorInput | None,
    ) -> RunMonitorResult | None:
        """Answer from the durable record when this invocation already ran.

        Read strongly and read *before* the model is called, which is the whole point: a
        redelivered job that reached the model first would already have spent a second pass
        over private text by the time it discovered the answer existed.

        The input-hash comparison is made against the *frozen* input rather than against a
        freshly assembled one. A rebuilt payload legitimately differs -- the case this run
        created is now among the candidate summaries -- so comparing against it would make
        every honest redelivery look like a different question.
        """

        record = await self.core.load_operation_agent_invocation(
            operation_scope, command.invocation_id
        )
        if record is None:
            return None
        if frozen is not None and record.input_hash != frozen.input_hash:
            # Same invocation identity, different frozen question. Returning the recorded
            # answer would attribute it to a request that was never made.
            raise AgentContractViolationError((AgentRejection.ENVELOPE_MISMATCH,))
        observability.idempotency_replay(
            namespace=command.namespace,
            community_id=command.community_id,
            correlation_id=command.correlation_id,
            actor_id_hash=command.actor_id_hash,
            operation_id=command.operation_id,
            input_hash=record.input_hash,
        )
        if record.outcome is AgentInvocationOutcome.FAILED:
            raise _replayed_failure(record.failure_code)
        return RunMonitorResult(
            case_ids=tuple(CaseId(ref.entity_id) for ref in record.result_refs),
            created_case_ids=(),
            report_count=0,
            fact_count=0,
            noise_message_count=0,
            policy_like_message_count=0,
            skipped_below_threshold=0,
            replayed=True,
        )

    async def _invoke_with_one_retry(
        self,
        command: RunMonitorCommand,
        invocation: MonitorInvocation,
        input_hash: Sha256Digest,
    ) -> MonitorResult:
        """Invoke once, and at most once more for a definitely-retryable failure.

        The retry reuses the same invocation identity, so the durable invocation record still
        describes one logical attempt and a runtime that did answer the first time can be
        recognised as a replay rather than counted twice. This is the *only* retry in the
        stack: the Strands event loop and the Bedrock client are both pinned to a single
        attempt, so one runtime invocation is one model call.

        The second attempt emits its own ``attempt=2`` start event, because "how many passes
        over private text did this invocation actually cost" is a question the logs have to be
        able to answer, and an attempt nobody recorded is one nobody can count.
        """

        try:
            return await self.agent.invoke_monitor(invocation)
        except AgentError as error:
            if not error.retryable:
                raise
        self._emit_invocation_started(command, invocation.payload, input_hash, attempt=2)
        return await self.agent.invoke_monitor(invocation)

    def _emit_invocation_started(
        self,
        command: RunMonitorCommand,
        payload: MonitorInput,
        input_hash: Sha256Digest,
        *,
        attempt: int,
    ) -> None:
        observability.agent_invocation_started(
            namespace=command.namespace,
            community_id=command.community_id,
            operation_id=command.operation_id,
            invocation_id=command.invocation_id,
            correlation_id=command.correlation_id,
            input_hash=input_hash,
            prompt_version=MONITOR_PROMPT_VERSION,
            attempt=attempt,
            message_count=len(payload.messages),
            candidate_summary_count=len(payload.candidate_case_summaries),
        )

    def _emit_agent_failure(
        self, command: RunMonitorCommand, input_hash: Sha256Digest, error: AgentError
    ) -> None:
        if error.code is AgentErrorCode.AGENT_CONTRACT_VIOLATION:
            observability.agent_contract_denied(
                namespace=command.namespace,
                community_id=command.community_id,
                operation_id=command.operation_id,
                invocation_id=command.invocation_id,
                correlation_id=command.correlation_id,
                input_hash=input_hash,
                reason_codes=error.reason_codes,
            )
            return
        observability.agent_invocation_failed(
            namespace=command.namespace,
            community_id=command.community_id,
            operation_id=command.operation_id,
            invocation_id=command.invocation_id,
            correlation_id=command.correlation_id,
            input_hash=input_hash,
            prompt_version=MONITOR_PROMPT_VERSION,
            reason_codes=error.reason_codes,
            retryable=error.retryable,
        )

    def _emit_apply_denial(self, command: RunMonitorCommand, error: Exception) -> None:
        if not isinstance(error, MonitorApplyDeniedError):
            return
        observability.report_link_denied(
            namespace=command.namespace,
            community_id=command.community_id,
            correlation_id=command.correlation_id,
            invocation_id=command.invocation_id,
            reason_code=error.denial.value,
        )

    # -- durable invocation record ------------------------------------------------------

    async def _record_failed_invocation(
        self,
        *,
        command: RunMonitorCommand,
        operation_scope: OperationScope,
        input_hash: Sha256Digest,
        error_code: str,
        now: datetime,
    ) -> None:
        """Persist that this invocation failed, with a safe code and no output.

        Written in its own small transaction under the operation partition, because the point
        of the record is that it survives a failure that left the domain untouched -- and
        because a Monitor run may have no case whose partition it could live in.

        It is deliberately never written for an *interrupted* apply. A ``FAILED`` record says
        "this invocation is over; never run it again", which is exactly wrong for a frozen
        plan whose remaining steps a redelivery is supposed to finish.
        """

        record = AgentInvocationResult(
            invocation_id=command.invocation_id,
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=None,
            operation_id=command.operation_id,
            agent_name=StoredAgentName.MONITOR,
            prompt_version=MONITOR_PROMPT_VERSION,
            input_hash=input_hash,
            output_hash=None,
            outcome=AgentInvocationOutcome.FAILED,
            failure_code=error_code,
            result_refs=(),
            created_at=now,
        )
        await self.core.record_operation_agent_invocation(operation_scope, record)

    def _succeeded_invocation(
        self,
        *,
        command: RunMonitorCommand,
        input_hash: Sha256Digest,
        output_hash: Sha256Digest,
        case_ids: tuple[CaseId, ...],
        now: datetime,
    ) -> AgentInvocationResult:
        """The record that says this invocation is over and what it produced."""

        return AgentInvocationResult(
            invocation_id=command.invocation_id,
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=None,
            operation_id=command.operation_id,
            agent_name=StoredAgentName.MONITOR,
            prompt_version=MONITOR_PROMPT_VERSION,
            input_hash=input_hash,
            output_hash=output_hash,
            outcome=AgentInvocationOutcome.SUCCEEDED,
            result_refs=tuple(
                EntityRef(entity_type="COMMUNITY_CASE", entity_id=case_id.value)
                for case_id in case_ids
            ),
            created_at=now,
        )

    # -- application -------------------------------------------------------------------

    async def _plan(
        self, command: RunMonitorCommand, validated: ValidatedMonitorOutput, *, now: datetime
    ) -> MonitorApplicationPlan:
        """Derive identity, strongly load exactly what the gates read, then compose."""

        identities = derive_identities(
            namespace=command.namespace,
            community_id=command.community_id,
            validated=validated,
        )
        current = await self._load_current_state(command, identities)
        return plan_monitor_application(
            namespace=command.namespace,
            community_id=command.community_id,
            validated=validated,
            identities=identities,
            current=current,
            now=now,
        )

    async def _apply_frozen_plan(
        self,
        command: RunMonitorCommand,
        operation_scope: OperationScope,
        validated: ValidatedMonitorOutput,
        plan: MonitorApplicationPlan,
        frozen: FrozenMonitorPlan,
        *,
        now: datetime,
    ) -> RunMonitorResult:
        """Commit the steps this invocation still owes, and nothing it already committed."""

        total_steps = len(frozen.steps) + FINALIZATION_STEPS
        replayed = await self._commit_steps(
            command=command,
            operation_scope=operation_scope,
            plan=plan,
            frozen=frozen,
            total_steps=total_steps,
            now=now,
        )
        self._emit_linkage_events(command, plan)

        await self._finalize(
            command=command,
            operation_scope=operation_scope,
            plan=plan,
            frozen=frozen,
            total_steps=total_steps,
            now=now,
        )
        applied = RunMonitorResult(
            case_ids=tuple(planned.case_id for planned in plan.cases),
            created_case_ids=tuple(planned.case_id for planned in plan.cases if planned.created),
            report_count=sum(len(planned.new_reports) for planned in plan.cases),
            fact_count=sum(len(planned.new_facts) for planned in plan.cases),
            noise_message_count=len(validated.noise_message_ids),
            policy_like_message_count=len(validated.policy_like_message_ids),
            skipped_below_threshold=plan.skipped_below_threshold,
            replayed=replayed or not plan.has_durable_effect,
        )
        observability.agent_invocation_completed(
            namespace=command.namespace,
            community_id=command.community_id,
            operation_id=command.operation_id,
            invocation_id=command.invocation_id,
            correlation_id=command.correlation_id,
            input_hash=frozen.input_hash,
            output_hash=frozen.output_hash,
            prompt_version=MONITOR_PROMPT_VERSION,
            outcome="REPLAYED" if applied.replayed else "SUCCEEDED",
            counts={
                "cases": len(applied.case_ids),
                "reports": applied.report_count,
                "facts": applied.fact_count,
                "provisional_groups": applied.skipped_below_threshold,
            },
        )
        return applied

    async def _load_current_state(
        self, command: RunMonitorCommand, identities: tuple[GroupIdentity, ...]
    ) -> CurrentApplyState:
        """Strongly read exactly the state the gates and the plan are decided against."""

        scope = CommunityScope(namespace=command.namespace, community_id=command.community_id)
        cases: dict[CaseId, CommunityCase] = {}
        facts: dict[FactId, Fact] = {}
        wanted_slots = intended_fact_slot_ids(identities)
        for case_id in intended_case_ids(identities):
            case_scope = self._case_scope(command, case_id)
            try:
                case = await self.core.load_case(case_scope)
            except NotFoundError:
                continue
            cases[case_id] = case
            known = set(case.fact_ids)
            wanted = tuple(fact_id for fact_id in wanted_slots if fact_id in known)
            for fact in await self.core.load_facts(case_scope, wanted):
                facts[fact.fact_id] = fact

        signals: dict[MessageId, FeedSignalProjection] = await self.core.load_feed_signals(
            scope, intended_message_ids(identities)
        )
        return CurrentApplyState(cases=cases, signals=signals, facts=facts)

    async def _commit_steps(
        self,
        *,
        command: RunMonitorCommand,
        operation_scope: OperationScope,
        plan: MonitorApplicationPlan,
        frozen: FrozenMonitorPlan,
        total_steps: int,
        now: datetime,
    ) -> bool:
        """Execute the tail of the frozen plan, having first proved it is still that tail.

        Resumption re-derives the remaining work rather than replaying stored entities, and
        then *checks* the derivation against the frozen step descriptors. Both halves matter.
        Re-deriving keeps the applied rows a product of validation in this process rather than
        of anything a snapshot claimed; checking keeps the derivation honest, because a plan
        that came out different is a plan built against a world the frozen answer never saw.

        A mismatch is never repaired by re-planning under the old invocation and never by
        asking the model again. It is a conflict, and the committed steps stay committed.
        """

        progress = await self.core.load_monitor_progress(operation_scope, command.invocation_id)
        if progress is not None and progress.output_hash != frozen.output_hash:
            # Somebody else's progress under this invocation identity. Skipping steps on the
            # strength of it would apply a different answer's arithmetic to this one.
            raise AgentContractViolationError((AgentRejection.ENVELOPE_MISMATCH,))
        completed = min(0 if progress is None else progress.completed_steps, len(frozen.steps))
        remaining = frozen.steps[completed:]
        derived = plan.descriptors
        if tuple(item.content for item in derived) != tuple(item.content for item in remaining):
            raise _plan_no_longer_applicable(completed)

        planned_by_case = {planned.case_id: planned for planned in plan.cases}
        for offset, (step, descriptor) in enumerate(zip(plan.steps, remaining, strict=True)):
            await self._commit_step(
                command=command,
                operation_scope=operation_scope,
                plan=plan,
                step=step,
                descriptor=descriptor,
                planned=planned_by_case[step.case_id],
                committed_steps=completed + offset + 1,
                total_steps=total_steps,
                plan_hash=frozen.plan_hash,
                input_hash=frozen.input_hash,
                output_hash=frozen.output_hash,
                now=now,
            )
        return completed > 0

    async def _commit_step(
        self,
        *,
        command: RunMonitorCommand,
        operation_scope: OperationScope,
        plan: MonitorApplicationPlan,
        step: ApplyStep,
        descriptor: ApplyStepDescriptor,
        planned: PlannedCase,
        committed_steps: int,
        total_steps: int,
        plan_hash: Sha256Digest,
        input_hash: Sha256Digest,
        output_hash: Sha256Digest,
        now: datetime,
    ) -> None:
        community_scope = CommunityScope(
            namespace=command.namespace, community_id=command.community_id
        )
        case_scope = self._case_scope(command, step.case_id)
        operations: list[WriteOperation] = []

        if step.case_expected_version is None:
            operations.append(self.core.stage_create_case(case_scope, step.case))
        else:
            operations.append(
                self.core.stage_update_case(
                    case_scope, step.case, expected_version=step.case_expected_version
                )
            )
        operations.extend(
            self.core.stage_create_report(case_scope, report) for report in step.reports
        )
        operations.extend(self.core.stage_create_fact(case_scope, fact) for fact in step.facts)
        for write in step.signals:
            if write.expected_version is None:
                operations.append(self.core.stage_create_feed_signal(community_scope, write.signal))
            else:
                operations.append(
                    self.core.stage_update_feed_signal(
                        community_scope, write.signal, expected_version=write.expected_version
                    )
                )

        # Whether this is the case's first step is read off the *frozen* descriptor, never off
        # the re-derived plan. A resumed attempt legitimately derives a shorter plan in which
        # the first remaining step of a case sits at index zero, and treating that as the
        # first step would append a second audit event for one decision.
        if descriptor.first_for_case:
            operations.append(
                self.core.stage_append_agent_invocation(
                    case_scope,
                    AgentInvocationResult(
                        invocation_id=command.invocation_id,
                        namespace=command.namespace,
                        community_id=command.community_id,
                        case_id=step.case_id,
                        operation_id=command.operation_id,
                        agent_name=StoredAgentName.MONITOR,
                        prompt_version=MONITOR_PROMPT_VERSION,
                        input_hash=input_hash,
                        output_hash=output_hash,
                        outcome=AgentInvocationOutcome.SUCCEEDED,
                        result_refs=(
                            EntityRef(
                                entity_type="COMMUNITY_CASE",
                                entity_id=step.case_id.value,
                                version=planned.final_case.version,
                            ),
                        ),
                        created_at=now,
                    ),
                )
            )
            operations.append(
                self.audit.stage_append_case_event(
                    case_scope,
                    candidate_audit_event(
                        planned=planned,
                        audit_event_id=derive_audit_event_id(
                            namespace=command.namespace,
                            community_id=command.community_id,
                            invocation_id=command.invocation_id,
                            case_id=step.case_id,
                        ),
                        namespace=command.namespace,
                        community_id=command.community_id,
                        actor_id_hash=command.actor_id_hash,
                        correlation_id=command.correlation_id,
                        causation_id=command.invocation_id,
                        input_hash=input_hash,
                        output_hash=output_hash,
                        now=now,
                    ),
                )
            )

        operations.append(
            self._stage_progress(
                operation_scope=operation_scope,
                command=command,
                plan_hash=plan_hash,
                completed_steps=committed_steps,
                total_steps=total_steps,
                input_hash=input_hash,
                output_hash=output_hash,
                now=now,
            )
        )

        key = self._step_idempotency_key(command, committed_steps)
        operations.append(
            self.idempotency.stage_create_completed(
                key,
                request_hash=plan.plan_hash,
                result_entity_refs=(
                    EntityRef(
                        entity_type="COMMUNITY_CASE",
                        entity_id=step.case_id.value,
                        version=step.case.version,
                    ),
                ),
                response_status=202,
                now=now,
            )
        )
        try:
            await self.unit_of_work.commit(
                TransactionPlan(
                    name=MONITOR_APPLY_TRANSACTION,
                    operations=tuple(operations),
                    # Only a case's first step carries its audit event: it is one decision
                    # being recorded once, and the continuation steps are that same decision
                    # still being written down rather than further decisions to audit.
                    audit_required=descriptor.first_for_case,
                    commit_proof=self.idempotency.commit_proof(key, request_hash=plan.plan_hash),
                )
            )
        except PersistenceError as error:
            # The plan is frozen and the remaining steps are bounded deterministic work, so
            # this is an interruption rather than a verdict. Which kind of storage failure it
            # was does not change that: the resumed attempt re-proves the plan against current
            # state and turns a genuine conflict into a conflict then.
            raise MonitorApplyInterruptedError(error.code.value) from error

    def _stage_progress(
        self,
        *,
        operation_scope: OperationScope,
        command: RunMonitorCommand,
        plan_hash: Sha256Digest,
        completed_steps: int,
        total_steps: int,
        input_hash: Sha256Digest,
        output_hash: Sha256Digest,
        now: datetime,
    ) -> WriteOperation:
        """Stage the row that says how far this frozen plan has been applied.

        The hash it binds is the **frozen** plan's, never the re-derived tail's. A resumed
        attempt legitimately derives a shorter plan, and stamping that plan's hash on the row
        would rewrite the progress of a five-step plan as the progress of a two-step one --
        the record would then no longer name the plan it is actually describing.
        """

        progress = MonitorApplyProgress(
            invocation_id=command.invocation_id,
            operation_id=command.operation_id,
            namespace=command.namespace,
            community_id=command.community_id,
            input_hash=input_hash,
            output_hash=output_hash,
            plan_hash=plan_hash,
            completed_steps=completed_steps,
            total_steps=total_steps,
            version=completed_steps,
            created_at=now,
            updated_at=now,
        )
        if completed_steps == 1:
            return self.core.stage_create_monitor_progress(operation_scope, progress)
        return self.core.stage_update_monitor_progress(
            operation_scope, progress, expected_version=completed_steps - 1
        )

    async def _finalize(
        self,
        *,
        command: RunMonitorCommand,
        operation_scope: OperationScope,
        plan: MonitorApplicationPlan,
        frozen: FrozenMonitorPlan,
        total_steps: int,
        now: datetime,
    ) -> None:
        """Commit the last step of the plan: the record that says the invocation succeeded.

        Recording the successful invocation is not an epilogue to the apply, it is the **final
        resumable step of it**, and it is counted in ``total_steps`` for exactly that reason.
        The earlier shape put it outside progress entirely, so a storage failure here left every
        data step durably committed, progress reading "complete", and no successful invocation
        record anywhere -- an operation that had done all its work and was recorded as having
        failed. Inside the progress sequence the two cannot disagree: ``progress.is_complete``
        now *means* the invocation record is durable, because one transaction writes both.

        It carries the same three guarantees every other step carries -- its own idempotency
        key, its own commit proof, and interruption rather than failure when storage refuses --
        so an attempt that dies here becomes a resumable ``PENDING`` operation whose redelivery
        finishes this one step and calls no model at all.

        The proof binds the **frozen** plan hash rather than the re-derived plan's. A redelivery
        that reaches finalization has derived an empty tail, and a proof hashed from that tail
        could never be reconciled with the one the first attempt wrote.
        """

        progress = await self.core.load_monitor_progress(operation_scope, command.invocation_id)
        if progress is not None and progress.completed_steps >= total_steps:
            # Already finalized, so the record is already durable. Nothing to add.
            return

        key = self._step_idempotency_key(command, total_steps)
        case_ids = tuple(planned.case_id for planned in plan.cases)
        operations: tuple[WriteOperation, ...] = (
            self.core.stage_append_operation_agent_invocation(
                operation_scope,
                self._succeeded_invocation(
                    command=command,
                    input_hash=frozen.input_hash,
                    output_hash=frozen.output_hash,
                    case_ids=case_ids,
                    now=now,
                ),
            ),
            self._stage_progress(
                operation_scope=operation_scope,
                command=command,
                plan_hash=frozen.plan_hash,
                completed_steps=total_steps,
                total_steps=total_steps,
                input_hash=frozen.input_hash,
                output_hash=frozen.output_hash,
                now=now,
            ),
            self.idempotency.stage_create_completed(
                key,
                request_hash=frozen.plan_hash,
                result_entity_refs=tuple(
                    EntityRef(entity_type="COMMUNITY_CASE", entity_id=case_id.value)
                    for case_id in case_ids
                ),
                response_status=202,
                now=now,
            ),
        )
        try:
            await self.unit_of_work.commit(
                TransactionPlan(
                    name=MONITOR_FINALIZE_TRANSACTION,
                    operations=operations,
                    audit_required=False,
                    commit_proof=self.idempotency.commit_proof(key, request_hash=frozen.plan_hash),
                )
            )
        except PersistenceError as error:
            raise MonitorApplyInterruptedError(error.code.value) from error

    def _emit_linkage_events(
        self, command: RunMonitorCommand, plan: MonitorApplicationPlan
    ) -> None:
        for planned in plan.cases:
            if not planned.has_durable_effect:
                continue
            emit = (
                observability.candidate_detected if planned.created else observability.report_linked
            )
            emit(
                namespace=command.namespace,
                community_id=command.community_id,
                case_id=planned.case_id,
                case_version=planned.final_case.version,
                correlation_id=command.correlation_id,
                invocation_id=command.invocation_id,
                report_count=len(planned.new_reports),
                fact_count=len(planned.new_facts),
            )

    def _noop(self, command: RunMonitorCommand, reason_code: str) -> RunMonitorResult:
        """A run that changed nothing, said so, and cost no model invocation."""

        return RunMonitorResult(
            case_ids=(),
            created_case_ids=(),
            report_count=0,
            fact_count=0,
            noise_message_count=0,
            policy_like_message_count=0,
            skipped_below_threshold=0,
            replayed=False,
            noop_reason_code=reason_code,
        )

    def _snapshot_expiry(self, now: datetime) -> int:
        return epoch_seconds_ceiling(now + SNAPSHOT_RETENTION)

    def _case_scope(self, command: RunMonitorCommand, case_id: CaseId) -> CaseScope:
        return CaseScope(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=case_id,
        )

    @staticmethod
    def _step_idempotency_key(command: RunMonitorCommand, step_number: int) -> IdempotencyKey:
        """One commit proof per step, because each step is its own transaction.

        The proof is what resolves an ambiguous transport outcome without retrying blindly, so
        it has to name the exact transaction whose fate is in question -- not the apply as a
        whole, of which several transactions may already have committed.
        """

        return IdempotencyKey(
            partition=IdempotencyPartition(
                kind=IdempotencyPartitionKind.COMMUNITY,
                namespace=command.namespace,
                community_id=command.community_id,
            ),
            command=IdempotentCommand.APPLY_MONITOR_OUTPUT,
            actor_id_hash=command.actor_id_hash,
            key_hash=hash_value(f"{command.invocation_id}:step:{step_number}"),
        )


def _require_same_command(command: RunMonitorCommand, frozen: FrozenMonitorInput) -> None:
    """Refuse a redelivery that reuses one invocation identity for different work.

    The frozen input is authoritative about *what the model saw*, so a redelivery is answered
    from it rather than from a rebuilt context -- which means a command naming a different set
    of newly ingested messages would otherwise be silently answered with somebody else's
    payload. The snapshot records which command it was frozen for precisely so that cannot
    happen quietly: two different pieces of work under one invocation identity is a conflict,
    not a second opinion.
    """

    delivered = tuple(
        sorted((locator.message_id.value for locator in command.message_locators), key=str)
    )
    if delivered != frozen.command_message_ids:
        raise AgentContractViolationError((AgentRejection.ENVELOPE_MISMATCH,))


def _rebind_expected_versions(
    validated: ValidatedMonitorOutput, committed: tuple[ApplyStepDescriptor, ...]
) -> ValidatedMonitorOutput:
    """Advance each existing-case group's expected version past this invocation's own steps.

    The version an agent was shown is the version of a world that this invocation has since
    changed by exactly the steps it committed. Comparing the case against the *original*
    expected version on a resume would report our own committed work as somebody else's
    interference, and the operation could never finish.

    Only this invocation's own committed steps move the number. A version the frozen plan does
    not account for is still stale, which is precisely the concurrent-modification case the
    gate exists to catch.
    """

    advanced: dict[UUID, int] = {}
    for descriptor in committed:
        advanced[descriptor.case_id] = advanced.get(descriptor.case_id, 0) + 1
    if not advanced:
        return validated

    groups: list[ValidatedCandidateGroup] = []
    for group in validated.groups:
        steps_taken = (
            0 if group.existing_case_id is None else advanced.get(group.existing_case_id.value, 0)
        )
        if steps_taken == 0 or group.expected_case_version is None:
            groups.append(group)
            continue
        groups.append(
            replace(group, expected_case_version=group.expected_case_version + steps_taken)
        )
    return replace(validated, groups=tuple(groups))


def _plan_no_longer_applicable(completed_steps: int) -> Exception:
    """The derived plan is not the tail of the frozen one; say which kind of failure that is."""

    if completed_steps > 0:
        return PartialApplyConflictError("MONITOR_APPLY_PLAN")
    # Nothing committed, so nothing is partial. The world moved between freezing the plan and
    # starting it, which is the ordinary stale-case refusal.
    return MonitorApplyDeniedError(MonitorApplyDenial.CASE_VERSION_STALE)


def _non_resumable(error: Exception, completed_steps: int) -> Exception:
    """Restate a resume-time planning failure as a partial-apply conflict when work stands."""

    if completed_steps > 0 and isinstance(error, DomainError | PersistenceError):
        return PartialApplyConflictError("MONITOR_APPLY_PLAN")
    return error


def _safe_error_code(error: Exception) -> str:
    """Reduce one closed error to the safe code its durable record may carry."""

    safe = getattr(error, "safe_code", None)
    if isinstance(safe, str):
        return safe
    code = getattr(error, "code", None)
    value = getattr(code, "value", None)
    return value if isinstance(value, str) else "INTERNAL_ERROR"


def _replayed_failure(failure_code: str | None) -> AgentError:
    """Re-raise a recorded failure without turning it into a fresh, retryable one.

    ``retryable`` is forced to ``False`` regardless of what the original failure was. The
    original attempt already used the one licensed retry, and a replayed timeout that
    presented itself as retryable would license a second model call for an invocation that
    has already been answered once.
    """

    return AgentError(
        AgentErrorCode.AGENT_CONTRACT_VIOLATION,
        (failure_code or "AGENT_INVOCATION_FAILED",),
        retryable=False,
    )


def _envelope(
    command: RunMonitorCommand, payload: MonitorInput, *, now: datetime
) -> MonitorInvocation:
    return AgentInputEnvelope[MonitorInput](
        schema_version=AGENT_INPUT_SCHEMA_VERSION,
        invocation_id=command.invocation_id,
        namespace=command.namespace.value,
        agent_name=AgentName.MONITOR,
        case_id=None,
        case_version=None,
        requested_at=now,
        policy_version=POLICY_VERSION,
        payload=payload,
    )


def _fact_summary(fact: Fact) -> str:
    """Describe one existing fact to the Monitor without disclosing a private value.

    Linkage needs shape, not content: what kind of thing was recorded, roughly when, and how
    well supported it is. Health, unit, identity, and quoted text are named by category only,
    so a case summary can never become a second route by which a private value reaches an
    agent that was not given it directly.
    """

    value = fact.value
    if isinstance(value, IncidentOccurrence):
        detail = f"{format_utc(value.occurred_at)} {value.failure_mode.value}"
    elif isinstance(value, LocationArea):
        detail = value.area.value
    elif isinstance(value, ServiceImpact):
        detail = value.impact_code.value
    else:
        detail = "value withheld"
    return f"{fact.fact_type.value}: {detail} ({fact.evidence_status.value})"
