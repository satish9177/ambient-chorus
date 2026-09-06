"""The Phase-7 proposal: prove freshness, invoke once, validate all of it, commit ten writes.

One case in, one immutable proposal and one ``DRAFT`` execution out, **one transaction**. There
is no plan snapshot, no apply-progress row, and no ``RUNNING -> PENDING`` edge, because there is
nothing to resume: either the whole apply commits or none of it does.

The order of operations is the security design
----------------------------------------------
1. strongly read the durable invocation record first, so a redelivery after a committed apply
   answers from it and calls no model;
2. strongly load the case, the current-view pointer, and the exact immutable view it names, and
   prove every freshness fact **before** any model call -- state, expected OCC version,
   authorization epoch agreement across case, view, and pointer, recomputed view hash, expiry,
   policy and compiler versions, destination, purpose, capacity, no live send fence, and no
   live valid ``DRAFT``;
3. project the payload, which is the serialized view and nothing else, and derive its hash;
4. invoke, with exactly one application-owned retry under the same invocation identity, and
   only for a transient class;
5. validate the whole answer or refuse all of it;
6. sample the clock a **second** time and require the view to be unexpired *now*;
7. render the preview deterministically and seal it;
8. commit exactly ten participants, conditioned on the case's exact ``version``,
   ``authorization_version``, and ``state``, and on the current-view pointer's exact identity.

Step 8 never trusts step 2. An entire model invocation sits between the pointer *read* and the
write, so the pointer condition is what makes a compile that landed in that window fail the
proposal whole rather than persist an answer about a view that is no longer current.

Two clock reads, and they answer different questions
----------------------------------------------------
``now`` is read once at entry and is the **canonical artifact instant**: every timestamp and
every derived identifier this command mints is stamped with it, so one apply produces one
coherent set of rows rather than a row per microsecond.

``freshness_now`` is read again in step 6 and is an **authorization freshness sample** and
nothing else. It is never written anywhere. It exists because expiry is the one freshness fact
no storage condition can express -- a `ConditionCheck` can compare a version or a hash, but the
passage of time mutates no row, so a view that was fresh when the model was called and expired
while it answered would otherwise commit against the entry-time reading. Equality at expiry
means expired, exactly as it does everywhere else.

Two counters, and only one is authority
---------------------------------------
The apply moves ``version`` and carries ``authorization_version`` forward unchanged, because
recording that a proposal exists changes no fact, status, mandate, or count. That is what stops
a valid proposal from staling the view that authorized it -- the defect ADR-020 removed, under
which the first send of every case would have failed closed against its own lifecycle write.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from chorus.application import observability
from chorus.application.errors import (
    SendAuthorizationInProgressError,
    StaleAuthorizationError,
)
from chorus.application.services.action_renderer import (
    TEMPLATE_VERSION,
    RenderedPreview,
    render_preview,
)
from chorus.application.services.action_validation import (
    ValidatedProposal,
    validate_action_result,
)
from chorus.application.services.mandate_terms import key_hash
from chorus.contracts.action import (
    ACTION_PROMPT_VERSION,
    ActionInput,
    MandateVersionRefInput,
    SafeDestinationInput,
    SafeDestinationKind,
    SafeDisclosureScope,
    SafeEvidenceRefInput,
    SafeEvidenceStatus,
    SafeFactType,
    SafePurpose,
    SafeTransformationKind,
    ShareableFactInput,
)
from chorus.contracts.common import (
    AGENT_INPUT_SCHEMA_VERSION,
    AgentInputEnvelope,
    AgentName,
)
from chorus.domain.entities import (
    ActionCaveat,
    ActionClaim,
    ActionExecution,
    ActionExecutionState,
    ActionProposal,
    ActionProposalStatus,
    ActorType,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    CaseState,
    CommunityCase,
    Purpose,
)
from chorus.domain.errors import DomainError, IntegrityError, ValidationError
from chorus.domain.ids import (
    ActionId,
    CaseId,
    CommunityId,
    ExecutionId,
    IdGenerator,
    Namespace,
    Sha256Digest,
    ViewId,
)
from chorus.domain.state import CaseTransitionContext, transition_case
from chorus.domain.time import Clock
from chorus.ports.agents import (
    ActionAgentPort,
    ActionInvocation,
    ActionRejection,
    ActionResult,
    AgentContractViolationError,
    AgentError,
    AgentErrorCode,
)
from chorus.ports.errors import (
    CrossCaseViolationError,
    NotFoundError,
    PersistenceConflictError,
)
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotentCommand,
)
from chorus.ports.records import (
    ActionHistoryLocator,
    ActionPointerExpectation,
    AgentInvocationOutcome,
    AgentInvocationResult,
    CurrentActionPointer,
    CurrentViewPointer,
    StoredSafeDestination,
    StoredShareableView,
    ViewPointerExpectation,
)
from chorus.ports.records import AgentName as StoredAgentName
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    IdempotencyRepositoryPort,
    ShareableRepositoryPort,
)
from chorus.ports.scopes import ActionScope, CaseScope
from chorus.ports.storage import WriteOperation
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork
from chorus.privacy.canonical import (
    hash_action_caveat,
    hash_action_claim,
    hash_action_proposal,
    hash_value,
    verify_hash,
)
from chorus.privacy.compiler import POLICY_BUILD_HASH
from chorus.privacy.policy import COMPILER_VERSION, POLICY_VERSION

PROPOSAL_APPLY_TRANSACTION = "apply-action-proposal"

PROPOSAL_FIXED_TRANSACTION_PARTICIPANTS = 10
"""The exact participant count of a successful proposal apply, and it does not vary.

Counted from :meth:`ProposeAction._operations`, which stages exactly these and nothing else:

1. the immutable ``ActionProposal``, create-only;
2. the single ``ActionExecution`` in ``DRAFT``, create-only;
3. the current action pointer, conditionally replaced on the exact previous row version and
   ``proposal_hash``, or created when absent;
4. the immutable action-history locator;
5. a **ConditionCheck** on the current-view pointer's exact identity and row version;
6. the durable successful ``ACTION`` agent-invocation record;
7. the ``action.proposed`` audit event;
8. the completed action-apply idempotency record, which is also this plan's commit proof;
9. the guarded case update ``READY_FOR_ACTION -> ACTION_PROPOSED``, conditioned on the exact
   ``version``, ``authorization_version``, and ``state``;
10. the no-live-send-fence condition.

**Independent of how many claims or caveats the proposal contains**, because claims and caveats
live inside the proposal item rather than as rows of their own. A test asserts this number
against ``len(plan.operations)``, so a silently added participant fails before anything reaches
storage.

Participant 6 is not bookkeeping. ``PROPOSE_ACTION`` is asynchronous, so the worker's
``RUNNING -> SUCCEEDED`` status write happens *after* this transaction and can be lost; the
durable invocation record committed atomically with the proposal is what a redelivery reads to
learn the apply already happened, instead of spending a second model pass over the same view.
The ``ApplicationOperation`` status row is deliberately **not** an eleventh participant: it is
a projection, and recovery through participant 6 proves what committed rather than what a
worker intended.
"""


ACTION_RESULT_REF_TYPES: tuple[str, ...] = ("ACTION_PROPOSAL", "ACTION_EXECUTION")
"""Exactly the result references a successful Action apply writes, and nothing else.

A record carrying a third reference, a foreign type, or only one of these did not come from
this transaction. Recovery reads the identifiers *out* of it, so a malformed reference set is a
malformed answer.
"""


class InvocationProvenance(StrEnum):
    """Why one durable invocation record is not proof for the operation that found it.

    ``outcome == SUCCEEDED`` is not provenance. It says an Action invocation somewhere
    succeeded; it says nothing about *which* view, *which* prompt, or *which* case -- and a
    recovery path that transitions an operation on that basis would let one operation be
    finished by another operation's record.

    Every member is a bounded code carrying no stored value, so it is safe to log and audit.
    """

    INVOCATION_MISMATCH = "RECORD_INVOCATION_MISMATCH"
    SCOPE_MISMATCH = "RECORD_SCOPE_MISMATCH"
    AGENT_MISMATCH = "RECORD_AGENT_MISMATCH"
    PROMPT_VERSION_MISMATCH = "RECORD_PROMPT_VERSION_MISMATCH"
    INPUT_HASH_MISMATCH = "RECORD_INPUT_HASH_MISMATCH"
    RESULT_REFS_MISMATCH = "RECORD_RESULT_REFS_MISMATCH"


@dataclass(frozen=True, slots=True, kw_only=True)
class InvocationExpectation:
    """What a durable ``ACTION`` invocation record must say to be this operation's proof.

    ``input_hash`` is the load-bearing member and it is deliberately **not** taken from the
    record. It is recomputed from independently loaded, view-bound durable data through the
    same canonical schema the invocation-time hash used, so the comparison is between two
    values derived from two sources rather than a value compared with itself.
    """

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    invocation_id: UUID
    input_hash: Sha256Digest


async def expected_action_input_hash(
    shareable: ShareableRepositoryPort,
    scope: CaseScope,
    *,
    view_id: ViewId,
    view_hash: Sha256Digest,
) -> Sha256Digest:
    """Recompute the exact input hash one invocation over this view must have carried.

    The view is immutable and content-addressed, so loading it later yields the same bytes the
    invocation was built from -- and :func:`to_action_input` plus :func:`_input_hash` are the
    same two pure functions the invocation path used. That is what makes this an *independent*
    derivation rather than a restatement.

    A stored view whose hash disagrees with the one the operation is bound to is an integrity
    failure, not a mismatch to be reported: the artifact does not describe the work this
    operation authorized.
    """

    view = await shareable.load_view(scope, view_id)
    if view.view_hash != view_hash or not _view_hash_verifies(view):
        raise IntegrityError("SHAREABLE_VIEW")
    return _input_hash(to_action_input(view))


def invocation_provenance_failures(
    record: AgentInvocationResult, expectation: InvocationExpectation
) -> tuple[str, ...]:
    """Every way this record fails to be the invocation the expectation describes.

    All of them are reported rather than the first, because a record that disagrees in several
    places is a routing or storage defect and an operator wants the whole disagreement.
    """

    failures: list[str] = []
    if record.invocation_id != expectation.invocation_id:
        failures.append(InvocationProvenance.INVOCATION_MISMATCH.value)
    if (
        record.namespace != expectation.namespace
        or record.community_id != expectation.community_id
        or record.case_id != expectation.case_id
    ):
        failures.append(InvocationProvenance.SCOPE_MISMATCH.value)
    if record.agent_name is not StoredAgentName.ACTION:
        failures.append(InvocationProvenance.AGENT_MISMATCH.value)
    if record.prompt_version != ACTION_PROMPT_VERSION:
        failures.append(InvocationProvenance.PROMPT_VERSION_MISMATCH.value)
    if record.input_hash != expectation.input_hash:
        failures.append(InvocationProvenance.INPUT_HASH_MISMATCH.value)
    if record.outcome is AgentInvocationOutcome.SUCCEEDED and not _result_refs_are_exact(record):
        failures.append(InvocationProvenance.RESULT_REFS_MISMATCH.value)
    return tuple(failures)


def _result_refs_are_exact(record: AgentInvocationResult) -> bool:
    """One ``ACTION_PROPOSAL`` reference and one ``ACTION_EXECUTION`` reference. No others."""

    if len(record.result_refs) != 2:
        return False
    if not all(isinstance(ref.entity_id, UUID) for ref in record.result_refs):
        return False
    types = sorted(ref.entity_type for ref in record.result_refs)
    return types == sorted(ACTION_RESULT_REF_TYPES)


class ProposalDenial(StrEnum):
    """Deterministic refusals raised before any model call and before any mutation."""

    STALE_CASE_VERSION = "STALE_CASE_VERSION"
    CASE_NOT_READY = "CASE_NOT_READY"
    NO_CURRENT_VIEW = "NO_CURRENT_VIEW"
    VIEW_NOT_CURRENT = "VIEW_NOT_CURRENT"
    VIEW_EXPIRED = "VIEW_EXPIRED"
    VIEW_HASH_MISMATCH = "VIEW_HASH_MISMATCH"
    STALE_AUTHORIZATION = "STALE_AUTHORIZATION"
    POLICY_VERSION_MISMATCH = "POLICY_VERSION_MISMATCH"
    COMPILER_VERSION_MISMATCH = "COMPILER_VERSION_MISMATCH"
    POLICY_BUILD_MISMATCH = "POLICY_BUILD_MISMATCH"
    DESTINATION_MISMATCH = "DESTINATION_MISMATCH"
    DESTINATION_KIND_MISMATCH = "DESTINATION_KIND_MISMATCH"
    DESTINATION_REGISTRY_VERSION_MISMATCH = "DESTINATION_REGISTRY_VERSION_MISMATCH"
    DESTINATION_ROUTING_TOKEN_MISMATCH = "DESTINATION_ROUTING_TOKEN_MISMATCH"  # noqa: S105
    DESTINATION_LABEL_MISMATCH = "DESTINATION_LABEL_MISMATCH"
    PURPOSE_MISMATCH = "PURPOSE_MISMATCH"
    LIVE_DRAFT_PROPOSAL = "LIVE_DRAFT_PROPOSAL"
    RENDERED_MESSAGE_TOO_LARGE = "RENDERED_MESSAGE_TOO_LARGE"


class ProposalDeniedError(DomainError):
    """A proposal could not legally be made against the case as it now stands."""

    __slots__ = ("denial",)

    def __init__(self, denial: ProposalDenial) -> None:
        super().__init__(ValidationError().code, denial.value)
        self.denial = denial

    @property
    def safe_code(self) -> str:
        return self.denial.value


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposeActionCommand:
    """One proposal request, addressed by case, expected version, and exact view identity."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    operation_id: UUID
    invocation_id: UUID
    correlation_id: UUID
    actor_id_hash: Sha256Digest
    expected_case_version: int
    view_id: ViewId
    view_hash: Sha256Digest
    idempotency_key: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposeActionResult:
    """What one proposal produced, in identifiers, hashes, and counts only."""

    action_id: ActionId
    execution_id: ExecutionId
    case_id: CaseId
    case_version: int
    authorization_version: int
    proposal_hash: Sha256Digest
    preview_hash: Sha256Digest
    claim_count: int
    caveat_count: int
    replayed: bool

    @property
    def result_refs(self) -> tuple[UUID, ...]:
        return (self.action_id.value, self.execution_id.value)


@dataclass(frozen=True, slots=True, kw_only=True)
class _ProposalState:
    """Everything one proposal strongly loaded and proved before invoking anything."""

    case: CommunityCase
    view: StoredShareableView
    view_pointer: CurrentViewPointer
    action_pointer: CurrentActionPointer | None


@dataclass(slots=True)
class ProposeAction:
    """Run one Action invocation and apply its validated proposal atomically."""

    core: CoreRepositoryPort
    shareable: ShareableRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    agent: ActionAgentPort
    clock: Clock
    ids: IdGenerator
    destination: StoredSafeDestination
    """The deployment's **complete** current safe registry entry.

    An identifier alone could not answer the question ADR-020 § 3 asks. The destination
    registry version and the routing token are deployment configuration that changes without
    any case-owned authorization bump, so a view compiled against a superseded registry entry
    stays internally consistent and its ``authorization_snapshot_hash`` still verifies -- and it
    would route an external message by a token nobody currently uses.
    """

    from_identity_id: str
    """Safe deployment configuration, never a secret and never the ``From`` address.

    Held here rather than read at render time because the renderer must not acquire a Secrets
    Manager permission to obtain a value that is not secret (ADR-022 § 4).
    """

    purpose: Purpose = Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE

    policy_version: str = POLICY_VERSION
    compiler_version: str = COMPILER_VERSION
    policy_build_hash: Sha256Digest = POLICY_BUILD_HASH
    """The deployment's current policy configuration, injected rather than imported here.

    They default to this build's own constants, so production wiring states nothing. They are
    fields because they are *configuration*, and because a check against a value the module
    imports directly is a check no test can make fail without editing the module -- which is
    how "the deployment moved out from under an old view" stayed unexercised.
    """

    async def execute(self, command: ProposeActionCommand) -> ProposeActionResult:
        now = self.clock.now()
        scope = CaseScope(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
        )

        # The durable invocation record is read *first*, before anything is proved stale. A
        # redelivery arriving after a successful apply necessarily finds a case one version
        # ahead of the one its job names, and treating that as a stale request would refuse the
        # very redelivery the record exists to answer.
        record = await self.core.load_agent_invocation(scope, command.invocation_id)
        if record is not None:
            return await self._replay(command, scope, record)

        state = await self._load_and_prove(command, scope, now=now)
        payload = to_action_input(state.view)
        invocation = self._envelope(command, payload, now=now)
        input_hash = _input_hash(payload)

        observability.proposal_requested(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            case_version=state.case.version,
            authorization_version=state.case.authorization_version,
            correlation_id=command.correlation_id,
            actor_id_hash=command.actor_id_hash,
            view_id=state.view.view_id.value,
            view_hash=state.view.view_hash,
            fact_count=len(state.view.shareable_facts),
        )
        self._emit_started(command, input_hash, attempt=1)
        try:
            result = await self._invoke_with_one_retry(command, invocation, input_hash)
            validated = validate_action_result(
                invocation=invocation,
                result=result,
                view=state.view,
                namespace=command.namespace,
                destination_id=self.destination.destination_id,
                purpose=self.purpose,
                expected_view_hash=state.view.view_hash.value,
            )
        except AgentError as error:
            await self._record_failed_invocation(
                command=command,
                scope=scope,
                input_hash=input_hash,
                error_code=error.code.value,
                now=now,
            )
            self._emit_agent_failure(command, input_hash, error)
            raise

        observability.proposal_validated(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            invocation_id=command.invocation_id,
            view_hash=state.view.view_hash,
            claim_count=len(validated.claims),
            caveat_count=len(validated.caveats),
            citation_count=len(validated.relied_fact_ids),
        )
        try:
            # The freshness sample, taken immediately before the proposal is staged and used
            # for nothing else. See the module docstring: `now` is the canonical artifact
            # instant, and reusing it here would mean a view that expired during the model call
            # was measured against the moment before the call started.
            self._require_unexpired(command, state, freshness_now=self.clock.now())
            return await self._apply(
                command,
                scope=scope,
                state=state,
                validated=validated,
                deadline=validated.requested_deadline,
                input_hash=input_hash,
                output_hash=_output_hash(result),
                now=now,
            )
        except (DomainError, PersistenceConflictError, StaleAuthorizationError) as error:
            # A stale authorization discovered *after* the model answered is recorded as a
            # durable failed invocation for the same reason every other post-invocation failure
            # is: "this invocation is over" has to survive the failure that made it so, or the
            # next redelivery re-asks the model over a view that is already superseded.
            await self._record_failed_invocation(
                command=command,
                scope=scope,
                input_hash=input_hash,
                error_code=_safe_error_code(error),
                now=now,
            )
            raise

    def _require_unexpired(
        self,
        command: ProposeActionCommand,
        state: _ProposalState,
        *,
        freshness_now: datetime,
    ) -> None:
        """Fail closed when the bound view expired while the model was answering.

        This is the one authorization fact a transaction condition cannot express. The apply
        conditions on the case row's exact ``version``, ``authorization_version``, and
        ``state``, and on the current-view pointer's exact identity -- and all three can be
        satisfied by a view whose ``expires_at`` has simply passed, because the passage of time
        mutates no row for a condition to notice.

        Equality at expiry means expired, matching the mandate and view expiry rule the
        compiler, the pre-invocation check, and the send fence already use.

        The refusal happens **before** anything is staged, so there is no proposal, no ``DRAFT``
        execution, no pointer movement, no history locator, no successful invocation record, and
        no case transition. The model has already been called exactly once and **is not called
        again**: an expired view is not regenerated against a newer one, because a proposal
        nobody authorized against the new view is not this command's to make.
        """

        if freshness_now < state.view.expires_at:
            return
        observability.proposal_stale_rejected(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            reason_codes=(ProposalDenial.VIEW_EXPIRED.value,),
            invoked_model=True,
        )
        raise StaleAuthorizationError((ProposalDenial.VIEW_EXPIRED.value,))

    # -- pre-invocation freshness ----------------------------------------------------------

    async def _load_and_prove(
        self, command: ProposeActionCommand, scope: CaseScope, *, now: datetime
    ) -> _ProposalState:
        """Strongly load and prove every freshness fact, before the model is called at all.

        This is the check that costs nothing when it fails: a stale request refused here spends
        no model invocation and writes nothing. The apply transaction re-proves the two facts
        that can still move under a running model -- the case row and the current-view pointer
        -- and those are the only ones a condition can express.

        No non-current view ever reaches the model, and the private Core case this reads is
        never appended to the payload.
        """

        case = await self.core.load_case(scope)
        if case.version != command.expected_case_version:
            raise ProposalDeniedError(ProposalDenial.STALE_CASE_VERSION)
        if case.state is not CaseState.READY_FOR_ACTION:
            raise ProposalDeniedError(ProposalDenial.CASE_NOT_READY)

        pointer = await self.shareable.load_current_view_pointer(scope)
        if pointer is None:
            raise ProposalDeniedError(ProposalDenial.NO_CURRENT_VIEW)
        if pointer.view_id != command.view_id or pointer.view_hash != command.view_hash:
            raise ProposalDeniedError(ProposalDenial.VIEW_NOT_CURRENT)

        view = await self.shareable.load_view(scope, command.view_id)
        if view.view_hash != command.view_hash or view.view_id != pointer.view_id:
            raise ProposalDeniedError(ProposalDenial.VIEW_NOT_CURRENT)
        if not _view_hash_verifies(view):
            # The persisted artifact does not hash to what its own field claims. That is an
            # integrity failure rather than a policy answer, and it fails closed.
            raise IntegrityError("SHAREABLE_VIEW")
        # Equality at expiry means expired, matching the mandate and view expiry rule the
        # compiler and the send fence already use.
        if now >= view.expires_at:
            raise ProposalDeniedError(ProposalDenial.VIEW_EXPIRED)

        # The three-way authorization comparison. The pointer proves *which* view is current;
        # the strongly read case proves the world has not moved under it. Neither substitutes
        # for the other, and ``case_version`` is deliberately not compared -- it is provenance.
        if not (
            case.authorization_version
            == view.authorization_version
            == pointer.authorization_version
        ):
            raise ProposalDeniedError(ProposalDenial.STALE_AUTHORIZATION)

        self._require_current_configuration(view)
        if view.purpose is not self.purpose:
            raise ProposalDeniedError(ProposalDenial.PURPOSE_MISMATCH)

        fence = await self.core.load_send_fence(scope)
        if fence is not None and now < fence.expires_at:
            raise SendAuthorizationInProgressError(("SEND_FENCE_ACTIVE",))

        await self.shareable.assert_action_capacity(scope)
        action_pointer = await self.shareable.load_current_action_pointer(scope)
        if action_pointer is not None and not await self._pointer_is_replaceable(
            scope, action_pointer
        ):
            # A live valid DRAFT stands. Refused *before* any model call, with nothing written:
            # letting a second model call quietly invalidate the first proposal would discard a
            # human's pending decision without anybody deciding to. Clearing it is Phase 8's
            # explicit reject-or-edit path (ADR-022 § 6).
            raise ProposalDeniedError(ProposalDenial.LIVE_DRAFT_PROPOSAL)

        return _ProposalState(
            case=case, view=view, view_pointer=pointer, action_pointer=action_pointer
        )

    def _require_current_configuration(self, view: StoredShareableView) -> None:
        """Prove the view was compiled by the configuration this deployment runs **now**.

        ADR-020 § 3 puts these values deliberately outside the case authorization epoch: the
        policy build, the compiler version, the destination registry version, and the routing
        token are properties of the deployment, not of any case, and "are re-checked by exact
        equality at proposal and fence time".

        That check cannot be delegated to ``authorization_snapshot_hash``. Verifying the
        snapshot proves the *old view* is internally coherent -- it recomputes to the values it
        was built from -- which is exactly what a view compiled by a superseded policy build
        also does. Integrity of the old artifact is not evidence about the current deployment,
        and treating it as such would let a message route by a token nobody uses any more.

        Every dimension is compared by exact equality and every failure is refused **before**
        any model call, so a stale configuration costs zero invocations.
        """

        if view.policy_version != self.policy_version:
            raise ProposalDeniedError(ProposalDenial.POLICY_VERSION_MISMATCH)
        if view.compiler_version != self.compiler_version:
            raise ProposalDeniedError(ProposalDenial.COMPILER_VERSION_MISMATCH)
        if view.policy_build_hash != self.policy_build_hash:
            # The rule set itself moved. Neither version string has to change for that to
            # happen, which is why the build is hashed rather than named.
            raise ProposalDeniedError(ProposalDenial.POLICY_BUILD_MISMATCH)

        current = self.destination
        if view.destination.destination_id != current.destination_id:
            raise ProposalDeniedError(ProposalDenial.DESTINATION_MISMATCH)
        if view.destination.kind is not current.kind:
            raise ProposalDeniedError(ProposalDenial.DESTINATION_KIND_MISMATCH)
        if view.destination.registry_version != current.registry_version:
            raise ProposalDeniedError(ProposalDenial.DESTINATION_REGISTRY_VERSION_MISMATCH)
        if view.destination.routing_token != current.routing_token:
            raise ProposalDeniedError(ProposalDenial.DESTINATION_ROUTING_TOKEN_MISMATCH)
        if view.destination.display_label != current.display_label:
            # Authoritative because the contract makes it so: the label is one of the two names
            # ADR-021 § 7 lets a proposal use without citing a fact, and it is inside
            # ``preview_hash``. A drifted label is a different letter.
            raise ProposalDeniedError(ProposalDenial.DESTINATION_LABEL_MISMATCH)

    async def _pointer_is_replaceable(
        self, scope: CaseScope, pointer: CurrentActionPointer
    ) -> bool:
        """True only for an ``INVALIDATED`` proposal whose execution is terminal ``FAILED``.

        Both halves are required. An invalidated pointer whose ``DRAFT`` execution is still
        live would leave a second execution beside it, and a failed execution under a still-
        ``DRAFT`` pointer would mean the human's decision has not been recorded yet.
        """

        if pointer.status is not ActionProposalStatus.INVALIDATED:
            return False
        action_scope = ActionScope(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            action_id=pointer.action_id,
        )
        execution = await self.shareable.load_execution(action_scope, pointer.execution_id)
        return execution.state is ActionExecutionState.FAILED

    # -- invocation --------------------------------------------------------------------------

    def _envelope(
        self, command: ProposeActionCommand, payload: ActionInput, *, now: datetime
    ) -> ActionInvocation:
        """Wrap the payload in the common envelope. The payload is the view and nothing else.

        There is no "helpful context" field and no place to put one: the envelope carries
        identifiers, versions, an instant, and the policy version, and the payload is the
        field-for-field view mirror.
        """

        return AgentInputEnvelope[ActionInput](
            schema_version=AGENT_INPUT_SCHEMA_VERSION,
            invocation_id=command.invocation_id,
            namespace=command.namespace.value,
            agent_name=AgentName.ACTION,
            case_id=command.case_id.value,
            case_version=command.expected_case_version,
            requested_at=now,
            policy_version=POLICY_VERSION,
            payload=payload,
        )

    async def _invoke_with_one_retry(
        self,
        command: ProposeActionCommand,
        invocation: ActionInvocation,
        input_hash: Sha256Digest,
    ) -> ActionResult:
        """Invoke once, and at most once more for a definitely-retryable failure.

        The retry reuses the same invocation identity, the same frozen payload, the same view,
        and the same prompt artifact, so the durable record still describes one logical attempt
        and the input hash does not move. It is licensed only for the transient classes the
        adapter marks retryable -- a timeout, a throttle, or a transient provider 5xx.

        Malformed output after a durable failure decision, stale authorization, a contract
        violation, and a persistence conflict that cannot be proven ambiguous are all
        **not** retried, because repeating the request would only produce the same unusable
        answer while spending another pass.
        """

        try:
            return await self.agent.invoke_action(invocation)
        except AgentError as error:
            if not error.retryable:
                raise
        self._emit_started(command, input_hash, attempt=2)
        return await self.agent.invoke_action(invocation)

    # -- the one transaction -----------------------------------------------------------------

    async def _apply(
        self,
        command: ProposeActionCommand,
        *,
        scope: CaseScope,
        state: _ProposalState,
        validated: ValidatedProposal,
        deadline: datetime | None,
        input_hash: Sha256Digest,
        output_hash: Sha256Digest,
        now: datetime,
    ) -> ProposeActionResult:
        """Build the immutable artifacts, render the preview, and commit ten participants."""

        # Minted, never derived. ADR-020/021/022 keep ``ActionProposal.action_id`` and
        # ``ActionExecution.execution_id`` as UUIDv4, and recovery after an ambiguous outcome
        # is answered by the durable invocation record and the apply commit proof rather than
        # by an identity somebody could recompute -- so there is nothing a derivation would buy
        # that is worth minting an identifier the frozen contract does not authorize.
        action_id = self.ids.new(ActionId)
        execution_id = self.ids.new(ExecutionId)
        proposal = self._proposal(
            command,
            action_id=action_id,
            state=state,
            validated=validated,
            deadline=deadline,
            now=now,
        )
        try:
            preview = render_preview(proposal, state.view, from_identity_id=self.from_identity_id)
        except ValidationError as error:
            # Over 100 KiB. The whole proposal is rejected; nothing is truncated, no section is
            # dropped, and no caveat is silently omitted.
            raise ProposalDeniedError(ProposalDenial.RENDERED_MESSAGE_TOO_LARGE) from error
        sealed = self._seal(proposal, preview)

        next_case = transition_case(
            state.case,
            CaseState.ACTION_PROPOSED,
            expected_version=state.case.version,
            reason_code="ACTION_PROPOSED",
            now=now,
            context=CaseTransitionContext(current_view_and_proposal_match=True),
        )
        # The whole point of ADR-020, asserted here rather than assumed: the lifecycle edge
        # moved the OCC version and carried the authorization epoch forward, so the view this
        # proposal is bound to is still fresh against the case that now records it.
        if next_case.authorization_version != state.case.authorization_version:
            raise IntegrityError("COMMUNITY_CASE")

        key = self._key(command, action_id)
        request_hash = _request_hash(command)
        operations = self._operations(
            command,
            scope=scope,
            state=state,
            proposal=sealed,
            execution_id=execution_id,
            next_case=next_case,
            input_hash=input_hash,
            output_hash=output_hash,
            key=key,
            request_hash=request_hash,
            now=now,
        )
        if len(operations) != PROPOSAL_FIXED_TRANSACTION_PARTICIPANTS:  # pragma: no cover
            raise IntegrityError("ACTION_PROPOSAL")
        try:
            await self.unit_of_work.commit(
                TransactionPlan(
                    name=PROPOSAL_APPLY_TRANSACTION,
                    operations=operations,
                    audit_required=True,
                    commit_proof=self.idempotency.commit_proof(key, request_hash=request_hash),
                )
            )
        except PersistenceConflictError:
            # Something the pre-invocation checks proved has moved since: the case row, the
            # current-view pointer, the action pointer, or the send fence. Nothing was
            # persisted -- the transaction fails whole -- and **no second invocation follows**.
            observability.proposal_stale_rejected(
                namespace=command.namespace,
                community_id=command.community_id,
                case_id=command.case_id,
                correlation_id=command.correlation_id,
                reason_codes=(ProposalDenial.STALE_AUTHORIZATION.value,),
                invoked_model=True,
            )
            raise StaleAuthorizationError((ProposalDenial.STALE_AUTHORIZATION.value,)) from None

        observability.proposal_persisted(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            case_version=next_case.version,
            correlation_id=command.correlation_id,
            action_id=action_id.value,
            proposal_hash=sealed.proposal_hash,
            preview_hash=sealed.preview_hash,
            participants=len(operations),
        )
        return ProposeActionResult(
            action_id=action_id,
            execution_id=execution_id,
            case_id=command.case_id,
            case_version=next_case.version,
            authorization_version=next_case.authorization_version,
            proposal_hash=sealed.proposal_hash,
            preview_hash=sealed.preview_hash,
            claim_count=len(sealed.claims),
            caveat_count=len(sealed.caveats),
            replayed=False,
        )

    def _proposal(
        self,
        command: ProposeActionCommand,
        *,
        action_id: ActionId,
        state: _ProposalState,
        validated: ValidatedProposal,
        deadline: datetime | None,
        now: datetime,
    ) -> ActionProposal:
        """Build the immutable proposal with placeholder digests, to be sealed once."""

        claims = tuple(
            self._seal_claim(claim_id, text, citations)
            for claim_id, text, citations in validated.claims
        )
        caveats = tuple(
            self._seal_caveat(caveat_id, text, citations)
            for caveat_id, text, citations in validated.caveats
        )
        return ActionProposal(
            action_id=action_id,
            case_id=command.case_id,
            case_version=state.case.version,
            authorization_version=state.case.authorization_version,
            view_id=state.view.view_id,
            view_hash=state.view.view_hash,
            subject=validated.subject,
            claims=claims,
            requested_action=validated.requested_action,
            requested_deadline=deadline,
            request_fact_ids=tuple(sorted(validated.request_fact_ids, key=str)),
            caveats=caveats,
            tone=validated.tone,
            agent_invocation_id=command.invocation_id,
            prompt_version=ACTION_PROMPT_VERSION,
            preview_hash=_PLACEHOLDER_HASH,
            proposal_hash=_PLACEHOLDER_HASH,
            status=ActionProposalStatus.DRAFT,
            created_at=now,
        )

    @staticmethod
    def _seal_claim(claim_id: UUID, text: str, citations: tuple[UUID, ...]) -> ActionClaim:
        draft = ActionClaim(
            claim_id=claim_id,
            text=text,
            export_fact_ids=tuple(sorted(citations, key=str)),
            claim_hash=_PLACEHOLDER_HASH,
        )
        return replace(draft, claim_hash=hash_action_claim(draft))

    @staticmethod
    def _seal_caveat(caveat_id: UUID, text: str, citations: tuple[UUID, ...]) -> ActionCaveat:
        draft = ActionCaveat(
            caveat_id=caveat_id,
            text=text,
            export_fact_ids=tuple(sorted(citations, key=str)),
            caveat_hash=_PLACEHOLDER_HASH,
        )
        return replace(draft, caveat_hash=hash_action_caveat(draft))

    @staticmethod
    def _seal(proposal: ActionProposal, preview: RenderedPreview) -> ActionProposal:
        """Write the preview digest in, then seal the whole structure with its own hash.

        Order matters: ``proposal_hash`` covers ``preview_hash``, so sealing before the preview
        was written would produce a digest that binds a placeholder.
        """

        with_preview = replace(proposal, preview_hash=preview.preview_hash)
        return replace(with_preview, proposal_hash=hash_action_proposal(with_preview))

    def _operations(
        self,
        command: ProposeActionCommand,
        *,
        scope: CaseScope,
        state: _ProposalState,
        proposal: ActionProposal,
        execution_id: ExecutionId,
        next_case: CommunityCase,
        input_hash: Sha256Digest,
        output_hash: Sha256Digest,
        key: IdempotencyKey,
        request_hash: Sha256Digest,
        now: datetime,
    ) -> tuple[WriteOperation, ...]:
        """The one frozen proposal transaction, in the order the contract states it."""

        action_scope = ActionScope(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            action_id=proposal.action_id,
        )
        previous = state.action_pointer
        expectation = (
            None
            if previous is None
            else ActionPointerExpectation(
                row_version=previous.version, proposal_hash=previous.proposal_hash
            )
        )
        pointer = CurrentActionPointer(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            action_id=proposal.action_id,
            execution_id=execution_id,
            proposal_hash=proposal.proposal_hash,
            view_id=proposal.view_id,
            view_hash=proposal.view_hash,
            case_version=proposal.case_version,
            authorization_version=proposal.authorization_version,
            status=ActionProposalStatus.DRAFT,
            version=1 if previous is None else previous.version + 1,
            created_at=now if previous is None else previous.created_at,
            updated_at=now,
        )
        return (
            # 1. the immutable proposal, create-only
            self.shareable.stage_append_proposal(action_scope, proposal),
            # 2. its single execution, in the one shape ADR-022 made expressible: no approval,
            #    no send key, no rendered hash, no SES token -- none of them exists yet.
            self.shareable.stage_create_execution(
                action_scope,
                ActionExecution(
                    execution_id=execution_id,
                    action_id=proposal.action_id,
                    case_id=proposal.case_id,
                    approval_id=None,
                    proposal_hash=proposal.proposal_hash,
                    view_hash=proposal.view_hash,
                    idempotency_key=None,
                    state=ActionExecutionState.DRAFT,
                    claim_owner_hash=None,
                    rendered_message_hash=None,
                    ses_request_token_hash=None,
                    ses_message_id=None,
                    started_at=None,
                    finished_at=None,
                    failure_code=None,
                    failure_detail_safe=None,
                    reconciled_at=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                ),
            ),
            # 3. the current action pointer, bound to the exact row it is replacing
            self.shareable.stage_replace_current_action_pointer(
                scope, pointer, expected=expectation
            ),
            # 4. the immutable history locator
            self.shareable.stage_append_action_history_locator(
                scope,
                ActionHistoryLocator(
                    namespace=scope.namespace,
                    community_id=scope.community_id,
                    case_id=scope.case_id,
                    action_id=proposal.action_id,
                    proposal_hash=proposal.proposal_hash,
                    created_at=now,
                ),
            ),
            # 5. the current view has not moved while the model was answering. A ConditionCheck
            #    and never a write: the application holds ConditionCheckItem on the view
            #    prefixes and no PutItem, UpdateItem, or DeleteItem there.
            self.shareable.stage_require_current_view_pointer(
                scope,
                expected=ViewPointerExpectation(
                    row_version=state.view_pointer.version,
                    view_hash=state.view_pointer.view_hash,
                    view_id=state.view_pointer.view_id,
                ),
            ),
            # 6. the durable successful invocation record -- what a lost status write recovers
            #    from, holding identifiers, hashes, and provenance and no completion text
            self.core.stage_append_agent_invocation(
                scope,
                AgentInvocationResult(
                    invocation_id=command.invocation_id,
                    namespace=command.namespace,
                    community_id=command.community_id,
                    case_id=command.case_id,
                    operation_id=None,
                    agent_name=StoredAgentName.ACTION,
                    prompt_version=ACTION_PROMPT_VERSION,
                    input_hash=input_hash,
                    output_hash=output_hash,
                    outcome=AgentInvocationOutcome.SUCCEEDED,
                    result_refs=(
                        EntityRef(
                            entity_type="ACTION_PROPOSAL", entity_id=proposal.action_id.value
                        ),
                        EntityRef(entity_type="ACTION_EXECUTION", entity_id=execution_id.value),
                    ),
                    created_at=now,
                ),
            ),
            # 7. the safe audit row, append-only
            self.audit.stage_append_case_event(
                scope, self._audit_event(command, proposal=proposal, next_case=next_case, now=now)
            ),
            # 8. the completed action-apply idempotency record, which is also the commit proof
            self.idempotency.stage_create_completed(
                key,
                request_hash=request_hash,
                result_entity_refs=(
                    EntityRef(entity_type="ACTION_PROPOSAL", entity_id=proposal.action_id.value),
                    EntityRef(entity_type="ACTION_EXECUTION", entity_id=execution_id.value),
                ),
                response_status=202,
                now=now,
            ),
            # 9. the guarded case update: READY_FOR_ACTION -> ACTION_PROPOSED, version N -> N+1,
            #    authorization_version A -> A. Conditioned on the exact version, the exact
            #    authorization version, and the exact state the validator read.
            self.core.stage_update_case(
                scope,
                next_case,
                expected_version=state.case.version,
                expected_authorization_version=state.case.authorization_version,
                expected_state=CaseState.READY_FOR_ACTION,
            ),
            # 10. no authorized send is in flight
            self.core.stage_require_no_live_send_fence(scope, now=now),
        )

    def _audit_event(
        self,
        command: ProposeActionCommand,
        *,
        proposal: ActionProposal,
        next_case: CommunityCase,
        now: datetime,
    ) -> AuditEvent:
        """Identifiers, versions, counts, and closed codes. Never prose and never a body."""

        return AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            actor_type=ActorType.SYSTEM,
            actor_id_hash=command.actor_id_hash,
            event_type="action.proposed",
            occurred_at=now,
            correlation_id=command.correlation_id,
            causation_id=None,
            idempotency_key_hash=key_hash(command.idempotency_key),
            entity_refs=(
                AuditEntityRef(
                    entity_type="ACTION_PROPOSAL",
                    entity_id=proposal.action_id.value,
                    version=None,
                ),
                AuditEntityRef(
                    entity_type="COMMUNITY_CASE",
                    entity_id=command.case_id.value,
                    version=next_case.version,
                ),
            ),
            decision=AuditDecision.ALLOW,
            reason_codes=("ACTION_PROPOSED",),
            safe_details=AuditDetails(count=len(proposal.claims), rule_id=TEMPLATE_VERSION),
            input_hash=proposal.view_hash,
            output_hash=proposal.proposal_hash,
        )

    # -- replay and durable records ----------------------------------------------------------

    async def _replay(
        self,
        command: ProposeActionCommand,
        scope: CaseScope,
        record: AgentInvocationResult,
    ) -> ProposeActionResult:
        """Answer from the durable record. No model call, no mutation, no second proposal.

        Read strongly and read *before* the model is called, which is the whole point: a
        redelivered job that reached the model first would already have spent a second pass over
        the same view by the time it discovered the answer existed.
        """

        await self.verify_invocation_record(command, scope, record)
        observability.proposal_replayed(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            invocation_id=command.invocation_id,
        )
        if record.outcome is AgentInvocationOutcome.FAILED:
            raise _replayed_failure(record.failure_code)
        action_id = next(
            (
                ActionId(ref.entity_id)
                for ref in record.result_refs
                if ref.entity_type == "ACTION_PROPOSAL"
            ),
            None,
        )
        execution_id = next(
            (
                ExecutionId(ref.entity_id)
                for ref in record.result_refs
                if ref.entity_type == "ACTION_EXECUTION"
            ),
            None,
        )
        if action_id is None or execution_id is None:
            raise AgentContractViolationError((ActionRejection.ENVELOPE_MISMATCH,))
        # The answer is read back from the persisted artifact rather than reconstructed. A
        # replay has to describe what actually committed, and this attempt's local state
        # describes a proposal it never wrote.
        proposal = await self.shareable.load_proposal(
            ActionScope(
                namespace=scope.namespace,
                community_id=scope.community_id,
                case_id=scope.case_id,
                action_id=action_id,
            )
        )
        return ProposeActionResult(
            action_id=action_id,
            execution_id=execution_id,
            case_id=command.case_id,
            case_version=proposal.case_version,
            authorization_version=proposal.authorization_version,
            proposal_hash=proposal.proposal_hash,
            preview_hash=proposal.preview_hash,
            claim_count=len(proposal.claims),
            caveat_count=len(proposal.caveats),
            replayed=True,
        )

    async def verify_invocation_record(
        self,
        command: ProposeActionCommand,
        scope: CaseScope,
        record: AgentInvocationResult,
    ) -> AgentInvocationResult:
        """Refuse a durable record that is not this operation's own invocation.

        ``outcome == SUCCEEDED`` is deliberately not enough, and neither is finding a record at
        the expected key: an item could be foreign, malformed, written under a different prompt
        artifact, or -- the one that matters most -- describe an invocation over a *different
        view*. The expected input hash is therefore recomputed from the immutable view this
        command is bound to, through the same canonical schema the invocation-time hash used.

        A disagreement fails closed with an integrity error. It never converts an operation to
        ``SUCCEEDED``, never returns identifiers read out of the record, and never causes a
        model call.
        """

        expectation = InvocationExpectation(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            invocation_id=command.invocation_id,
            input_hash=await expected_action_input_hash(
                self.shareable,
                scope,
                view_id=command.view_id,
                view_hash=command.view_hash,
            ),
        )
        failures = invocation_provenance_failures(record, expectation)
        if failures:
            observability.proposal_stale_rejected(
                namespace=command.namespace,
                community_id=command.community_id,
                case_id=command.case_id,
                correlation_id=command.correlation_id,
                reason_codes=failures,
                invoked_model=False,
            )
            raise IntegrityError("AGENT_INVOCATION")

        if record.outcome is AgentInvocationOutcome.SUCCEEDED:
            proposal_ref = next(
                (ref for ref in record.result_refs if ref.entity_type == "ACTION_PROPOSAL"),
                None,
            )
            execution_ref = next(
                (ref for ref in record.result_refs if ref.entity_type == "ACTION_EXECUTION"),
                None,
            )
            if (
                proposal_ref is None
                or execution_ref is None
                or len(record.result_refs) != 2
                or not isinstance(proposal_ref.entity_id, UUID)
                or not isinstance(execution_ref.entity_id, UUID)
            ):
                raise IntegrityError("AGENT_INVOCATION")

            action_id = ActionId(proposal_ref.entity_id)
            execution_id = ExecutionId(execution_ref.entity_id)
            action_scope = ActionScope(
                namespace=scope.namespace,
                community_id=scope.community_id,
                case_id=scope.case_id,
                action_id=action_id,
            )

            try:
                proposal = await self.shareable.load_proposal(action_scope)
            except (NotFoundError, CrossCaseViolationError):
                raise IntegrityError("ACTION_PROPOSAL") from None

            try:
                execution = await self.shareable.load_execution(action_scope, execution_id)
            except (NotFoundError, CrossCaseViolationError):
                raise IntegrityError("ACTION_EXECUTION") from None

            try:
                view = await self.shareable.load_view(scope, command.view_id)
            except (NotFoundError, CrossCaseViolationError):
                raise IntegrityError("SHAREABLE_VIEW") from None
            if view.view_hash != command.view_hash:
                raise IntegrityError("SHAREABLE_VIEW")

            # D. PROPOSAL BINDING
            if proposal.action_id != action_id:
                raise IntegrityError("ACTION_PROPOSAL")
            if proposal.case_id != command.case_id:
                raise IntegrityError("ACTION_PROPOSAL")
            if proposal.agent_invocation_id != record.invocation_id:
                raise IntegrityError("ACTION_PROPOSAL")
            if proposal.view_id != command.view_id or proposal.view_hash != command.view_hash:
                raise IntegrityError("ACTION_PROPOSAL")
            if proposal.authorization_version != view.authorization_version:
                raise IntegrityError("ACTION_PROPOSAL")
            if proposal.prompt_version != ACTION_PROMPT_VERSION:
                raise IntegrityError("ACTION_PROPOSAL")
            if proposal.status not in set(ActionProposalStatus):
                raise IntegrityError("ACTION_PROPOSAL")
            if hash_action_proposal(proposal) != proposal.proposal_hash:
                raise IntegrityError("ACTION_PROPOSAL")

            # E. EXECUTION BINDING
            if execution.execution_id != execution_id:
                raise IntegrityError("ACTION_EXECUTION")
            if execution.action_id != proposal.action_id:
                raise IntegrityError("ACTION_EXECUTION")
            if execution.case_id != proposal.case_id:
                raise IntegrityError("ACTION_EXECUTION")
            if execution.proposal_hash != proposal.proposal_hash:
                raise IntegrityError("ACTION_EXECUTION")
            if execution.view_hash != proposal.view_hash:
                raise IntegrityError("ACTION_EXECUTION")
            if execution.state not in set(ActionExecutionState):
                raise IntegrityError("ACTION_EXECUTION")
            try:
                execution.require_state_presence()
            except ValueError:
                raise IntegrityError("ACTION_EXECUTION") from None

            # F. MUTUAL RELATIONSHIP & ADR-022 Section 3 current action cross-check
            current_pointer = await self.shareable.load_current_action_pointer(scope)
            if current_pointer is not None and current_pointer.action_id == proposal.action_id:
                if current_pointer.execution_id != execution.execution_id:
                    raise IntegrityError("ACTION_EXECUTION")
                if current_pointer.proposal_hash != proposal.proposal_hash:
                    raise IntegrityError("ACTION_PROPOSAL")
                if current_pointer.view_hash != proposal.view_hash:
                    raise IntegrityError("ACTION_PROPOSAL")

        return record

    async def load_verified_invocation_record(
        self, command: ProposeActionCommand, scope: CaseScope
    ) -> AgentInvocationResult | None:
        """Strongly read this invocation's durable record and prove it belongs here.

        The worker's recovery path calls this rather than reading the record itself, so both
        recovery paths -- in-process replay and post-redelivery status recovery -- run the same
        provenance check against the same independently derived expectation.
        """

        record = await self.core.load_agent_invocation(scope, command.invocation_id)
        if record is None:
            return None
        return await self.verify_invocation_record(command, scope, record)

    async def _record_failed_invocation(
        self,
        *,
        command: ProposeActionCommand,
        scope: CaseScope,
        input_hash: Sha256Digest,
        error_code: str,
        now: datetime,
    ) -> None:
        """Persist that this invocation failed, with a safe code and no output.

        A failed invocation is durable so the pre-invocation replay check can refuse to run it
        again: "this invocation is over" has to survive the failure that made it so, and a
        proposal left with no record would be re-asked over the same view by the next
        redelivery.
        """

        await self.unit_of_work.commit(
            TransactionPlan(
                name="record-action-failure",
                operations=(
                    self.core.stage_append_agent_invocation(
                        scope,
                        AgentInvocationResult(
                            invocation_id=command.invocation_id,
                            namespace=command.namespace,
                            community_id=command.community_id,
                            case_id=command.case_id,
                            operation_id=None,
                            agent_name=StoredAgentName.ACTION,
                            prompt_version=ACTION_PROMPT_VERSION,
                            input_hash=input_hash,
                            output_hash=None,
                            outcome=AgentInvocationOutcome.FAILED,
                            failure_code=error_code,
                            result_refs=(),
                            created_at=now,
                        ),
                    ),
                ),
                audit_required=False,
            )
        )

    # -- observability -------------------------------------------------------------------------

    def _emit_started(
        self, command: ProposeActionCommand, input_hash: Sha256Digest, *, attempt: int
    ) -> None:
        observability.agent_invocation_started(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            invocation_id=command.invocation_id,
            correlation_id=command.correlation_id,
            input_hash=input_hash,
            prompt_version=ACTION_PROMPT_VERSION,
            attempt=attempt,
            message_count=0,
            candidate_summary_count=0,
        )

    def _emit_agent_failure(
        self, command: ProposeActionCommand, input_hash: Sha256Digest, error: AgentError
    ) -> None:
        if error.code is AgentErrorCode.AGENT_CONTRACT_VIOLATION:
            observability.proposal_denied(
                namespace=command.namespace,
                community_id=command.community_id,
                case_id=command.case_id,
                correlation_id=command.correlation_id,
                invocation_id=command.invocation_id,
                reason_codes=error.reason_codes,
            )
            return
        observability.agent_invocation_failed(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            invocation_id=command.invocation_id,
            correlation_id=command.correlation_id,
            input_hash=input_hash,
            prompt_version=ACTION_PROMPT_VERSION,
            reason_codes=error.reason_codes,
            retryable=error.retryable,
        )

    # -- keys -----------------------------------------------------------------------------------

    @staticmethod
    def _key(command: ProposeActionCommand, action_id: ActionId) -> IdempotencyKey:
        """The **action-apply** commit proof, distinct from the route/start reservation.

        Two records under one command family, distinguished by contextual partition and by a
        domain-separated key hash, because they are commit proofs for two different
        transactions: the route's reservation binds the caller's key to one operation and one
        invocation identity, and this one proves the apply committed. Conflating them would make
        a lost apply indistinguishable from a lost dispatch.

        ``action_id`` is passed in rather than recomputed. It is minted once per apply attempt,
        so the proof is addressable by the attempt that staged the plan -- which is the only
        caller that resolves an ambiguous outcome in-process. A *later* delivery does not
        re-derive this key at all: it recovers from the durable ``ACTION`` invocation record
        keyed by the invocation identity (ADR-022 § 10).
        """

        return IdempotencyKey(
            partition=IdempotencyPartition(
                kind=IdempotencyPartitionKind.ACTION,
                namespace=command.namespace,
                action_id=action_id,
            ),
            command=IdempotentCommand.PROPOSE_ACTION,
            actor_id_hash=command.actor_id_hash,
            key_hash=key_hash(f"propose-action\x1f{command.idempotency_key}"),
        )


def to_action_input(view: StoredShareableView) -> ActionInput:
    """Mirror the stored view into the Action contract, field for field.

    Total and mechanical. It computes nothing, drops nothing, and defaults nothing -- a field
    that appeared in the compiled artifact appears here with the same value, because the
    parity test asserts the two field sets are identical and because a payload that differed
    from the hashed view would be a proposal validated against something the human never saw.

    This is also the exact place a "helpful context" field would be added, and the reason there
    is nowhere to add one: every value below comes from the view.
    """

    return ActionInput(
        schema_version="shareable-case-view/v2",
        view_id=view.view_id.value,
        case_id=view.case_id.value,
        community_public_label=view.community_public_label,
        case_version=view.case_version,
        authorization_version=view.authorization_version,
        policy_version=view.policy_version,
        compiler_version=view.compiler_version,
        policy_build_hash=view.policy_build_hash.value,
        destination=SafeDestinationInput(
            destination_id=view.destination.destination_id.value,
            # The local enums are constructed from the stored value rather than passed as a
            # string: ``StrictModel`` disables coercion on purpose, so a runtime that answered
            # with ``"3"`` where an integer belongs is a contract violation rather than a number
            # to be repaired -- and the same strictness applies to this direction.
            kind=SafeDestinationKind(view.destination.kind.value),
            registry_version=view.destination.registry_version,
            routing_token=view.destination.routing_token,
            display_label=view.destination.display_label,
        ),
        purpose=SafePurpose(view.purpose.value),
        generated_at=view.generated_at,
        expires_at=view.expires_at,
        mandate_version_set=tuple(
            MandateVersionRefInput(
                mandate_id=ref.mandate_id, version=ref.version, terms_hash=ref.terms_hash.value
            )
            for ref in view.mandate_version_set
        ),
        authorization_snapshot_hash=view.authorization_snapshot_hash.value,
        shareable_facts=tuple(
            ShareableFactInput(
                export_fact_id=fact.export_fact_id.value,
                fact_type=SafeFactType(fact.fact_type.value),
                safe_text=fact.safe_text,
                effective_scope=SafeDisclosureScope(fact.effective_scope.value),
                evidence_status=SafeEvidenceStatus(fact.evidence_status.value),
                contributor_count=fact.contributor_count,
                transformation=SafeTransformationKind(fact.transformation.value),
                transformation_rule_id=fact.transformation_rule_id,
                safe_evidence_ref_ids=tuple(ref_id.value for ref_id in fact.safe_evidence_ref_ids),
                content_hash=fact.content_hash.value,
            )
            for fact in view.shareable_facts
        ),
        safe_evidence_refs=tuple(
            SafeEvidenceRefInput(
                safe_evidence_ref_id=ref.safe_evidence_ref_id.value,
                media_type=ref.media_type,
                export_handle_id=ref.export_handle_id,
                sha256=ref.sha256.value,
                caption=ref.caption,
                created_by_rule_id=ref.created_by_rule_id,
                content_hash=ref.content_hash.value,
            )
            for ref in view.safe_evidence_refs
        ),
        audit_refs=view.audit_refs,
        view_hash=view.view_hash.value,
    )


_PLACEHOLDER_HASH = Sha256Digest("sha256:" + "0" * 64)
"""A structurally valid digest occupying a field a real hash replaces.

The proposal hash covers every field except itself, so the value is built once with a
placeholder and sealed once with the digest of the rest. The placeholder never reaches storage.
"""


def _view_hash_verifies(view: StoredShareableView) -> bool:
    """Recompute the persisted view's own hash over everything but the hash field."""

    return verify_hash(view, view.view_hash, omit_fields=frozenset({"view_hash"}))


def _input_hash(payload: ActionInput) -> Sha256Digest:
    """The canonical digest of exactly what the Action Agent was shown.

    Over the *payload*, deliberately not the envelope: ``requested_at`` moves between two
    legitimate attempts at one invocation identity, and a hash that moved with it would make the
    licensed retry look like a different question.
    """

    return hash_value(payload.model_dump(mode="json"))


def _output_hash(result: ActionResult) -> Sha256Digest:
    return hash_value(result.output.model_dump(mode="json"))


def _request_hash(command: ProposeActionCommand) -> Sha256Digest:
    return hash_value(
        {
            "schema": "propose-action-request/v1",
            "namespace": command.namespace.value,
            "case_id": str(command.case_id),
            "expected_case_version": command.expected_case_version,
            "view_id": str(command.view_id),
            "view_hash": command.view_hash.value,
        }
    )


def _replayed_failure(failure_code: str | None) -> AgentError:
    """Re-raise a recorded failure without calling the model again."""

    code = failure_code or AgentErrorCode.AGENT_CONTRACT_VIOLATION.value
    if code == AgentErrorCode.AGENT_TIMEOUT.value:
        return AgentError(AgentErrorCode.AGENT_TIMEOUT, (code,), retryable=False)
    if code == AgentErrorCode.AGENT_DEPENDENCY_ERROR.value:
        return AgentError(AgentErrorCode.AGENT_DEPENDENCY_ERROR, (code,), retryable=False)
    return AgentError(AgentErrorCode.AGENT_CONTRACT_VIOLATION, (code,), retryable=False)


def _safe_error_code(error: Exception) -> str:
    safe = getattr(error, "safe_code", None)
    if isinstance(safe, str):
        return safe
    code = getattr(error, "code", None)
    value = getattr(code, "value", None)
    return value if isinstance(value, str) else "INTERNAL_ERROR"


__all__ = [
    "ACTION_RESULT_REF_TYPES",
    "PROPOSAL_APPLY_TRANSACTION",
    "PROPOSAL_FIXED_TRANSACTION_PARTICIPANTS",
    "InvocationExpectation",
    "InvocationProvenance",
    "ProposalDenial",
    "ProposalDeniedError",
    "ProposeAction",
    "ProposeActionCommand",
    "ProposeActionResult",
    "expected_action_input_hash",
    "invocation_provenance_failures",
    "to_action_input",
]
