"""Invoke the Investigator once and apply exactly what deterministic validation allows.

One case in, one assessment out, **one transaction**. There is no frozen-input snapshot, no
validated-plan snapshot, no apply-progress row, and no plan provenance chain, because there is
nothing to resume: either the whole apply commits or none of it does, so a partially applied
investigation does not exist. That is why the ``RUNNING -> PENDING`` operation edge is Monitor
only.

The order of operations is the security design
----------------------------------------------
1. strongly load the case and prove it is still at the version the caller expected;
2. strongly load its reports, facts, evidence, and the *resolved* evidence-root closure;
3. project the bounded payload and derive its input hash;
4. strongly read the durable invocation record **before** any model call -- a completed record
   with a matching input hash replays its outcome and calls no model, and a differing hash is a
   conflict;
5. invoke, with exactly one application-owned retry under the same invocation identity;
6. validate the whole answer or refuse all of it;
7. recompute independence, evidence statuses, readiness, and the compile preflight -- all
   deterministic, none of them reading a number the model returned;
8. commit one transaction guarded on the case version.

Step 7 never trusts step 5. The model's ``sufficiency`` count, its duplicate-evidence groups,
and its recommended disposition are recorded or discarded; the authoritative count comes from
stored contributors and collapsed roots, the statuses from the frozen classification, and the
transition from the existing case-transition guard.

Concurrency is settled three times over
---------------------------------------
The caller's ``expected_case_version`` is checked at request; the case version travels in the
agent envelope and is re-proved against the answer; and the apply transaction is conditional on
the case row's version. If the case changes while the model is running, the transaction fails
and there is no assessment row, no fact-status update, and no transition -- the operation fails
safely rather than attaching a reading of an older world to the current one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from chorus.application import observability
from chorus.application.services.evidence_status import (
    EVIDENCE_STATUS_OVERCLAIM_DOWNGRADED,
    ResolvedStatus,
    compute_statuses,
    resolve_status,
)
from chorus.application.services.investigation_projection import (
    InvestigationProjectionError,
    project_investigation_input,
)
from chorus.application.services.investigation_readiness import (
    PreflightInputs,
    ReadinessOutcome,
    compile_preflight,
    evaluate_readiness,
)
from chorus.application.services.investigation_validation import (
    ValidatedInvestigation,
    validate_investigation_result,
)
from chorus.application.services.mandate_terms import key_hash
from chorus.application.services.root_closure import evidence_root_ids, resolve_root_closure
from chorus.contracts.common import (
    AGENT_INPUT_SCHEMA_VERSION,
    INVESTIGATOR_PROMPT_VERSION,
    AgentInputEnvelope,
    AgentName,
)
from chorus.contracts.investigation import InvestigationInput
from chorus.domain.entities import (
    ActorType,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    CaseState,
    CommunityCase,
    EvidenceItem,
    EvidenceRoot,
    EvidenceStatus,
    InvestigationAssessment,
    Purpose,
)
from chorus.domain.entities import EvidenceFinding as StoredEvidenceFinding
from chorus.domain.errors import DomainError, ValidationError
from chorus.domain.facts import Fact, FactStatus, Report, independent_sources
from chorus.domain.ids import (
    AssessmentId,
    CaseId,
    CommunityId,
    ContributorId,
    FactId,
    IdGenerator,
    Namespace,
    OperationId,
    Sha256Digest,
)
from chorus.domain.mandates import CurrentMandatePointer, DisclosureMandate
from chorus.domain.state import (
    CaseTransitionContext,
    bump_case_authorization,
    transition_case,
)
from chorus.domain.time import Clock
from chorus.ports.agents import (
    AgentContractViolationError,
    AgentError,
    AgentErrorCode,
    InvestigationInvocation,
    InvestigationRejection,
    InvestigationResult,
    InvestigatorAgentPort,
)
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotentCommand,
)
from chorus.ports.limits import MAX_PAGE_SIZE, TRANSACTION_MAX_OPERATIONS
from chorus.ports.pagination import PageRequest
from chorus.ports.records import (
    AgentInvocationOutcome,
    AgentInvocationResult,
)
from chorus.ports.records import AgentName as StoredAgentName
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    IdempotencyRepositoryPort,
)
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import WriteOperation
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork
from chorus.privacy.canonical import hash_value
from chorus.privacy.policy import POLICY_VERSION, SafeDestination

INVESTIGATION_APPLY_TRANSACTION = "apply-investigation"

INVESTIGATION_FIXED_TRANSACTION_PARTICIPANTS = 6
"""The participants every investigation apply stages regardless of how many facts it touches.

Counted from :meth:`RunInvestigation._operations`, which stages exactly these and nothing else:

1. the immutable assessment, create-only;
2. the guarded case update carrying state, version, corroboration count, and assessment pointer;
3. the durable successful agent-invocation record;
4. the safe audit event;
5. the condition-check asserting no live send fence holds this case;
6. the idempotency record, which is also this plan's commit proof.

It is a **derived** number and never a guessed one: the bound below subtracts it from
DynamoDB's transaction maximum, and a test builds the real maximum-sized plan and asserts the
arithmetic against ``len(plan.operations)``. If a participant is ever added, that test fails
before anything reaches storage.
"""

MAX_INVESTIGATION_FACT_UPDATES = (
    TRANSACTION_MAX_OPERATIONS - INVESTIGATION_FIXED_TRANSACTION_PARTICIPANTS
)
"""How many fact-status updates one atomic investigation apply may carry.

Derived, not chosen. A validated answer that would move more facts than this is rejected
**before any mutation**: the alternative is a transaction DynamoDB refuses whole, which would
be the same refusal arriving later and with less to say about why.

Only facts whose *resolved* status actually differs from the stored one are counted, so a case
at the frozen hundred-fact ceiling is bounded by how much changed rather than by how large it
is.
"""


class InvestigationReason(StrEnum):
    """Why this investigation was asked for; part of the operation's binding identity."""

    INITIAL = "INITIAL"
    NEW_EVIDENCE = "NEW_EVIDENCE"
    REOPEN = "REOPEN"


class InvestigationApplyDenial(StrEnum):
    """Deterministic refusals raised after validation and before any mutation."""

    STALE_CASE_VERSION = "STALE_CASE_VERSION"
    CASE_NOT_INVESTIGABLE = "CASE_NOT_INVESTIGABLE"
    TRANSACTION_BOUND_EXCEEDED = "TRANSACTION_BOUND_EXCEEDED"
    PROJECTION_INVALID = "INVESTIGATION_PROJECTION_INVALID"


class InvestigationApplyDeniedError(DomainError):
    """A validated answer could not legally be applied to the case as it now stands."""

    __slots__ = ("denial",)

    def __init__(self, denial: InvestigationApplyDenial) -> None:
        super().__init__(ValidationError().code, denial.value)
        self.denial = denial

    @property
    def safe_code(self) -> str:
        return self.denial.value


INVESTIGABLE_STATES: frozenset[CaseState] = frozenset(
    {
        CaseState.AWAITING_MANDATES,
        CaseState.INVESTIGATING,
        CaseState.READY_FOR_ACTION,
        CaseState.ACTION_PROPOSED,
        CaseState.ACTIONED,
        CaseState.VERIFYING,
    }
)
"""Where an assessment may be recorded at all.

``CANDIDATE`` is excluded because no mandate has been requested yet, so there is nothing to
assess the disclosure of and no contributor has been asked anything. The two terminal states
are excluded because the state machine reopens a terminal case only through an explicit human
reopen command, and an investigation is not one.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class RunInvestigationCommand:
    """One investigation of one case, addressed by identity and version."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    operation_id: OperationId
    invocation_id: UUID
    correlation_id: UUID
    actor_id_hash: Sha256Digest
    expected_case_version: int
    reason: InvestigationReason
    idempotency_key: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RunInvestigationResult:
    """What one investigation changed, in counts and identifiers only."""

    assessment_id: AssessmentId
    case_id: CaseId
    case_version: int
    case_state: CaseState
    independent_source_count: int
    is_corroborated: bool
    fact_updates: int
    contradiction_count: int
    downgraded_count: int
    state_reason_code: str
    replayed: bool

    @property
    def result_refs(self) -> tuple[UUID, ...]:
        return (self.assessment_id.value,)


@dataclass(frozen=True, slots=True, kw_only=True)
class InvestigationCaseState:
    """Everything one investigation strongly loaded, in one value."""

    case: CommunityCase
    reports: tuple[Report, ...]
    facts: tuple[Fact, ...]
    evidence_items: tuple[EvidenceItem, ...]
    evidence_roots: tuple[EvidenceRoot, ...]
    pseudonyms: dict[ContributorId, str]


@dataclass(slots=True)
class RunInvestigation:
    """Run one Investigator invocation and apply its validated assessment atomically."""

    core: CoreRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    agent: InvestigatorAgentPort
    clock: Clock
    ids: IdGenerator
    community_public_label: str
    destination: SafeDestination
    purpose: Purpose = Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE

    async def execute(self, command: RunInvestigationCommand) -> RunInvestigationResult:
        now = self.clock.now()
        scope = self._scope(command)

        # The durable invocation record is read *first*, before the case is checked for
        # staleness and before anything is projected. A redelivery that arrives after a
        # successful apply necessarily finds a case one version ahead of the one its job names,
        # and treating that as a stale request would refuse the very redelivery the record
        # exists to answer.
        case = await self.core.load_case(scope)
        record = await self.core.load_agent_invocation(scope, command.invocation_id)
        if record is not None:
            await self._prove_same_question(command, scope, case, record)
            return self._replay(command, case, record)

        loaded = await self._load(command, scope, case)
        try:
            projection = project_investigation_input(
                case=loaded.case,
                reports=loaded.reports,
                facts=loaded.facts,
                evidence_items=loaded.evidence_items,
                pseudonyms=loaded.pseudonyms,
                prior_assessment=await self._prior_assessment(scope, loaded.case),
            )
        except InvestigationProjectionError as error:
            # Translated to a closed typed error so an unmapped exception cannot escape into an
            # at-least-once dispatcher and strand the operation in RUNNING.
            raise InvestigationApplyDeniedError(
                InvestigationApplyDenial.PROJECTION_INVALID
            ) from error

        invocation = self._envelope(command, projection.payload, now=now)
        input_hash = _input_hash(projection.payload)

        self._emit_started(command, projection.payload, input_hash, attempt=1)
        try:
            result = await self._invoke_with_one_retry(command, invocation, input_hash)
            validated = validate_investigation_result(
                invocation=invocation, result=result, namespace=command.namespace
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

        try:
            return await self._apply(
                command,
                scope=scope,
                loaded=loaded,
                validated=validated,
                input_hash=input_hash,
                output_hash=_output_hash(result),
                now=now,
            )
        except (DomainError, PersistenceConflictError) as error:
            await self._record_failed_invocation(
                command=command,
                scope=scope,
                input_hash=input_hash,
                error_code=_safe_error_code(error),
                now=now,
            )
            raise

    # -- loading -------------------------------------------------------------------------

    def _scope(self, command: RunInvestigationCommand) -> CaseScope:
        return CaseScope(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
        )

    async def _load(
        self, command: RunInvestigationCommand, scope: CaseScope, case: CommunityCase
    ) -> InvestigationCaseState:
        """Strongly load everything the deterministic recomputation reads.

        The case version is re-proved here rather than trusted from the request. A caller's
        ``expected_case_version`` was checked when the operation was created, and the case may
        have moved since; a run that reasoned over newer state under an older expectation would
        produce an assessment bound to a version that never existed.
        """

        if case.version != command.expected_case_version:
            raise InvestigationApplyDeniedError(InvestigationApplyDenial.STALE_CASE_VERSION)
        if case.state not in INVESTIGABLE_STATES:
            raise InvestigationApplyDeniedError(InvestigationApplyDenial.CASE_NOT_INVESTIGABLE)

        reports = await self._all_reports(scope)
        facts = await self.core.load_facts(scope, case.fact_ids)
        evidence_ids = tuple(
            dict.fromkeys(evidence_id for fact in facts for evidence_id in fact.evidence_ids)
        )
        evidence_items = await self.core.load_evidence_items(scope, evidence_ids)
        roots = await resolve_root_closure(
            self.core, scope.community_scope, evidence_root_ids(evidence_items)
        )
        pseudonyms = await self._pseudonyms(scope, reports, evidence_items)
        return InvestigationCaseState(
            case=case,
            reports=reports,
            facts=facts,
            evidence_items=evidence_items,
            evidence_roots=roots,
            pseudonyms=pseudonyms,
        )

    async def _all_reports(self, scope: CaseScope) -> tuple[Report, ...]:
        """Load every report of the case by identifier, bounded by the frozen page size."""

        collected: list[Report] = []
        cursor = None
        while True:
            page = await self.core.read_case_reports(
                scope, PageRequest(limit=MAX_PAGE_SIZE, cursor=cursor)
            )
            collected.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                return tuple(collected)

    async def _pseudonyms(
        self,
        scope: CaseScope,
        reports: tuple[Report, ...],
        evidence_items: tuple[EvidenceItem, ...],
    ) -> dict[ContributorId, str]:
        contributor_ids = tuple(
            dict.fromkeys(
                (
                    *(report.contributor_id for report in reports),
                    *(item.submitted_by_contributor_id for item in evidence_items),
                )
            )
        )
        pseudonyms: dict[ContributorId, str] = {}
        for contributor_id in contributor_ids:
            contributor = await self.core.load_contributor(scope.community_scope, contributor_id)
            pseudonyms[contributor_id] = contributor.pseudonym
        return pseudonyms

    async def _prior_assessment(
        self, scope: CaseScope, case: CommunityCase
    ) -> InvestigationAssessment | None:
        if case.assessment_id is None:
            return None
        return await self.core.load_current_assessment(scope, case.assessment_id)

    # -- invocation ----------------------------------------------------------------------

    def _envelope(
        self, command: RunInvestigationCommand, payload: InvestigationInput, *, now: datetime
    ) -> InvestigationInvocation:
        return AgentInputEnvelope[InvestigationInput](
            schema_version=AGENT_INPUT_SCHEMA_VERSION,
            invocation_id=command.invocation_id,
            namespace=command.namespace.value,
            agent_name=AgentName.INVESTIGATOR,
            case_id=command.case_id.value,
            case_version=command.expected_case_version,
            requested_at=now,
            policy_version=POLICY_VERSION,
            payload=payload,
        )

    async def _prove_same_question(
        self,
        command: RunInvestigationCommand,
        scope: CaseScope,
        case: CommunityCase,
        record: AgentInvocationResult,
    ) -> None:
        """Refuse a recorded answer that belongs to a different question.

        This can only be checked while the case is *still* at the version the command names,
        because the payload is a pure function of the case at one version and rebuilding it
        against a moved case would be constructing a different question in order to compare
        against it. So the comparison runs on the one path where it is meaningful -- a
        redelivery that arrives before the apply committed -- and is skipped on the ordinary
        post-apply redelivery, where the operation's own ``agent_binding_hash`` is what already
        proves the job and the invocation identity name the same work.
        """

        if case.version != command.expected_case_version:
            return
        loaded = await self._load(command, scope, case)
        projection = project_investigation_input(
            case=loaded.case,
            reports=loaded.reports,
            facts=loaded.facts,
            evidence_items=loaded.evidence_items,
            pseudonyms=loaded.pseudonyms,
            prior_assessment=await self._prior_assessment(scope, loaded.case),
        )
        if _input_hash(projection.payload) != record.input_hash:
            # Same invocation identity, different question. Returning the recorded answer would
            # attribute it to a request that was never made.
            raise AgentContractViolationError((InvestigationRejection.ENVELOPE_MISMATCH,))

    def _replay(
        self,
        command: RunInvestigationCommand,
        case: CommunityCase,
        record: AgentInvocationResult,
    ) -> RunInvestigationResult:
        """Answer from the durable record. No model call, no mutation, no second pass.

        Read strongly and read *before* the model is called, which is the whole point: a
        redelivered job that reached the model first would already have spent a second pass over
        a private case by the time it discovered the answer existed.
        """

        observability.idempotency_replay(
            namespace=command.namespace,
            community_id=command.community_id,
            correlation_id=command.correlation_id,
            actor_id_hash=command.actor_id_hash,
            input_hash=record.input_hash,
        )
        if record.outcome is AgentInvocationOutcome.FAILED:
            raise _replayed_failure(record.failure_code)
        assessment_id = next((AssessmentId(ref.entity_id) for ref in record.result_refs), None)
        if assessment_id is None:
            raise AgentContractViolationError((InvestigationRejection.ENVELOPE_MISMATCH,))
        return RunInvestigationResult(
            assessment_id=assessment_id,
            case_id=command.case_id,
            case_version=case.version,
            case_state=case.state,
            independent_source_count=case.corroboration_source_count,
            is_corroborated=case.corroboration_source_count >= 2,
            fact_updates=0,
            contradiction_count=0,
            downgraded_count=0,
            state_reason_code=case.state_reason_code,
            replayed=True,
        )

    async def _invoke_with_one_retry(
        self,
        command: RunInvestigationCommand,
        invocation: InvestigationInvocation,
        input_hash: Sha256Digest,
    ) -> InvestigationResult:
        """Invoke once, and at most once more for a definitely-retryable failure.

        The retry reuses the same invocation identity and the same frozen payload, so the
        durable record still describes one logical attempt. This is the *only* retry in the
        stack: the Strands event loop and the Bedrock client are both pinned to a single
        attempt, so one runtime invocation is one model call.
        """

        try:
            return await self.agent.invoke_investigator(invocation)
        except AgentError as error:
            if not error.retryable:
                raise
        self._emit_started(command, invocation.payload, input_hash, attempt=2)
        return await self.agent.invoke_investigator(invocation)

    # -- deterministic consequence -------------------------------------------------------

    async def _apply(
        self,
        command: RunInvestigationCommand,
        *,
        scope: CaseScope,
        loaded: InvestigationCaseState,
        validated: ValidatedInvestigation,
        input_hash: Sha256Digest,
        output_hash: Sha256Digest,
        now: datetime,
    ) -> RunInvestigationResult:
        """Recompute everything, decide readiness, and commit one transaction."""

        active_facts = tuple(fact for fact in loaded.facts if fact.status is FactStatus.ACTIVE)
        independence = independent_sources(
            active_facts, loaded.reports, loaded.evidence_items, loaded.evidence_roots
        )
        contradicted = validated.contradicted_fact_ids
        computed = compute_statuses(
            facts=loaded.facts,
            reports=loaded.reports,
            evidence_items=loaded.evidence_items,
            roots=loaded.evidence_roots,
            contradicted_fact_ids=contradicted,
        )
        resolutions = tuple(
            resolve_status(fact_id, status, validated.proposed_statuses.get(fact_id))
            for fact_id, status in computed.items()
        )
        by_fact = {resolution.fact_id: resolution for resolution in resolutions}

        pointers = await self._mandate_pointers(scope)
        readiness = evaluate_readiness(
            independent_source_count=independence.count,
            linkage_decision=validated.linkage_decision,
            contradiction_materialities=tuple(
                contradiction.materiality for contradiction in validated.contradictions
            ),
            has_compilable_purpose=compile_preflight(
                PreflightInputs(
                    case=loaded.case,
                    community_public_label=self.community_public_label,
                    facts=loaded.facts,
                    reports=loaded.reports,
                    evidence_items=loaded.evidence_items,
                    evidence_roots=loaded.evidence_roots,
                    mandates=await self._mandates(pointers, scope),
                    mandate_pointers=pointers,
                    destination=self.destination,
                    purpose=self.purpose,
                    requested_at=now,
                ),
                recomputed_source_count=independence.count,
            ),
        )

        changed = tuple(
            replace(
                fact,
                evidence_status=by_fact[fact.fact_id].resolved,
                version=fact.version + 1,
                updated_at=now,
            )
            for fact in active_facts
            if fact.fact_id in by_fact
            and by_fact[fact.fact_id].resolved is not fact.evidence_status
        )
        if len(changed) > MAX_INVESTIGATION_FACT_UPDATES:
            # Refused before any mutation. A transaction DynamoDB would reject whole is the
            # same refusal arriving later with less to say about why.
            raise InvestigationApplyDeniedError(InvestigationApplyDenial.TRANSACTION_BOUND_EXCEEDED)
        expected_versions = {fact.fact_id: fact.version for fact in active_facts}

        assessment = self._assessment(
            command,
            case=loaded.case,
            validated=validated,
            resolutions=resolutions,
            independent_source_count=independence.count,
            now=now,
        )
        next_case = self._next_case(
            loaded.case,
            assessment=assessment,
            readiness=readiness,
            independent_source_count=independence.count,
            now=now,
        )

        key = self._key(command)
        request_hash = _request_hash(command)
        operations = self._operations(
            command,
            scope=scope,
            assessment=assessment,
            changed_facts=changed,
            expected_versions=expected_versions,
            next_case=next_case,
            previous_version=loaded.case.version,
            input_hash=input_hash,
            output_hash=output_hash,
            readiness=readiness,
            resolutions=resolutions,
            contradiction_count=len(validated.contradictions),
            key=key,
            request_hash=request_hash,
            now=now,
        )
        await self.unit_of_work.commit(
            TransactionPlan(
                name=INVESTIGATION_APPLY_TRANSACTION,
                operations=operations,
                audit_required=True,
                commit_proof=self.idempotency.commit_proof(key, request_hash=request_hash),
            )
        )

        downgraded = tuple(item for item in resolutions if item.overclaimed)
        self._emit_applied(command, next_case, readiness, independence.count, resolutions)
        for item in downgraded:
            observability.evidence_status_downgraded(
                namespace=command.namespace,
                community_id=command.community_id,
                case_id=command.case_id,
                correlation_id=command.correlation_id,
                fact_id=item.fact_id,
                computed_status=item.computed.value,
                proposed_status=("" if item.proposed is None else item.proposed.value),
            )
        for contradiction in validated.contradictions:
            observability.contradiction_recorded(
                namespace=command.namespace,
                community_id=command.community_id,
                case_id=command.case_id,
                correlation_id=command.correlation_id,
                materiality=contradiction.materiality.value,
                cited_fact_count=len(contradiction.statement_fact_ids),
            )
        return RunInvestigationResult(
            assessment_id=assessment.assessment_id,
            case_id=command.case_id,
            case_version=next_case.version,
            case_state=next_case.state,
            independent_source_count=independence.count,
            is_corroborated=independence.count >= 2,
            fact_updates=len(changed),
            contradiction_count=len(validated.contradictions),
            downgraded_count=len(downgraded),
            state_reason_code=next_case.state_reason_code,
            replayed=False,
        )

    async def _mandates(
        self, pointers: tuple[CurrentMandatePointer, ...], scope: CaseScope
    ) -> tuple[DisclosureMandate, ...]:
        """Load the exact immutable version each current pointer names.

        Every fact the preflight asks about is authorized by a mandate version, and the pointer
        is what decides which version that is. Loading the version the pointer names -- rather
        than the newest one, or the one a fact happens to reference -- is what makes gate 8's
        pointer-integrity check meaningful when the real compile runs later.
        """

        return tuple(
            [
                await self.core.load_mandate_version(scope, pointer.mandate_id, pointer.version)
                for pointer in pointers
            ]
        )

    async def _mandate_pointers(self, scope: CaseScope) -> tuple[CurrentMandatePointer, ...]:
        collected: list[CurrentMandatePointer] = []
        cursor = None
        while True:
            page = await self.core.load_current_mandate_pointers(
                scope, PageRequest(limit=MAX_PAGE_SIZE, cursor=cursor)
            )
            collected.extend(stored.pointer for stored in page.items)
            cursor = page.next_cursor
            if cursor is None:
                return tuple(collected)

    def _assessment(
        self,
        command: RunInvestigationCommand,
        *,
        case: CommunityCase,
        validated: ValidatedInvestigation,
        resolutions: tuple[ResolvedStatus, ...],
        independent_source_count: int,
        now: datetime,
    ) -> InvestigationAssessment:
        """Build the immutable assessment, then seal it with its own canonical hash.

        ``independent_source_count`` is the recomputed value and never the number the agent
        returned; the entity refuses a row where ``is_corroborated`` disagrees with it.
        """

        findings = tuple(
            StoredEvidenceFinding(
                fact_id=item.fact_id,
                evidence_status=item.resolved,
                reason_code=item.reason_code,
            )
            for item in sorted(resolutions, key=lambda item: str(item.fact_id))
        )
        draft = InvestigationAssessment(
            assessment_id=self.ids.new(AssessmentId),
            case_id=command.case_id,
            based_on_case_version=case.version,
            agent_invocation_id=command.invocation_id,
            linkage_decision=validated.linkage_decision.value,
            findings=findings,
            contradictions=validated.contradictions,
            alternative_explanations=validated.alternative_explanations,
            independent_source_count=independent_source_count,
            is_corroborated=independent_source_count >= 2,
            recommended_disposition=validated.recommended_disposition.value,
            assessment_hash=_PLACEHOLDER_HASH,
            created_at=now,
        )
        return replace(
            draft,
            assessment_hash=hash_value(draft, omit_fields=frozenset({"assessment_hash"})),
        )

    def _next_case(
        self,
        case: CommunityCase,
        *,
        assessment: InvestigationAssessment,
        readiness: ReadinessOutcome,
        independent_source_count: int,
        now: datetime,
    ) -> CommunityCase:
        """Move the case exactly as far as the deterministic guard allows, and no further.

        Four shapes, all decided by the existing transition service rather than here:

        * ``INVESTIGATING`` and ready -- the readiness transition, guarded by every term;
        * ``READY_FOR_ACTION`` and no longer ready -- readiness is lost and the case returns to
          ``INVESTIGATING``;
        * anything else -- the assessment is recorded and the case version moves so every view
          and proposal bound to the old version goes stale, but the state does not change.

        The last case is an authorization-sensitive change of no state, which
        :func:`transition_case` deliberately cannot express: every pair in its table is a real
        edge with a real guard, and adding a self-edge for convenience would put an unguarded
        pair into a table whose entire value is that every pair in it is guarded.
        """

        context = CaseTransitionContext(
            validated_assessment=True,
            independent_source_count=independent_source_count,
            no_material_different_issue=readiness.linkage_ok and readiness.contradictions_ok,
            has_compilable_purpose=readiness.has_compilable_purpose,
            readiness_lost=not readiness.ready,
        )
        if case.state is CaseState.INVESTIGATING and readiness.ready:
            moved = transition_case(
                case,
                CaseState.READY_FOR_ACTION,
                expected_version=case.version,
                reason_code=readiness.reason_code,
                now=now,
                context=context,
            )
        elif case.state is CaseState.READY_FOR_ACTION and not readiness.ready:
            moved = transition_case(
                case,
                CaseState.INVESTIGATING,
                expected_version=case.version,
                reason_code=readiness.reason_code,
                now=now,
                context=context,
            )
        else:
            moved = bump_case_authorization(
                case,
                expected_version=case.version,
                reason_code=readiness.reason_code,
                now=now,
            )
        return replace(
            moved,
            corroboration_source_count=independent_source_count,
            assessment_id=assessment.assessment_id,
        )

    # -- the one transaction -------------------------------------------------------------

    def _operations(
        self,
        command: RunInvestigationCommand,
        *,
        scope: CaseScope,
        assessment: InvestigationAssessment,
        changed_facts: tuple[Fact, ...],
        expected_versions: dict[FactId, int],
        next_case: CommunityCase,
        previous_version: int,
        input_hash: Sha256Digest,
        output_hash: Sha256Digest,
        readiness: ReadinessOutcome,
        resolutions: tuple[ResolvedStatus, ...],
        contradiction_count: int,
        key: IdempotencyKey,
        request_hash: Sha256Digest,
        now: datetime,
    ) -> tuple[WriteOperation, ...]:
        """The one frozen investigation transaction, in the order the contract states it.

        Six fixed participants plus one guarded update per changed fact. The count is asserted
        by test against ``INVESTIGATION_FIXED_TRANSACTION_PARTICIPANTS``, so the derived bound
        and the plan can never drift apart.
        """

        fact_updates = tuple(
            self.core.stage_update_fact(
                scope, fact, expected_version=expected_versions[fact.fact_id]
            )
            for fact in changed_facts
        )
        return (
            # 1. the immutable assessment, create-only
            self.core.stage_append_assessment(scope, assessment),
            # 2. the guarded case update: state, version, corroboration count, and the
            #    current-assessment pointer, all in one row so they cannot disagree
            self.core.stage_update_case(scope, next_case, expected_version=previous_version),
            # 3. the durable successful invocation record
            self.core.stage_append_agent_invocation(
                scope,
                AgentInvocationResult(
                    invocation_id=command.invocation_id,
                    namespace=command.namespace,
                    community_id=command.community_id,
                    case_id=command.case_id,
                    operation_id=None,
                    agent_name=StoredAgentName.INVESTIGATOR,
                    prompt_version=INVESTIGATOR_PROMPT_VERSION,
                    input_hash=input_hash,
                    output_hash=output_hash,
                    outcome=AgentInvocationOutcome.SUCCEEDED,
                    result_refs=(
                        EntityRef(
                            entity_type="INVESTIGATION_ASSESSMENT",
                            entity_id=assessment.assessment_id.value,
                        ),
                    ),
                    created_at=now,
                ),
            ),
            # 4. the safe audit row, append-only
            self.audit.stage_append_case_event(
                scope,
                self._audit_event(
                    command,
                    assessment=assessment,
                    next_case=next_case,
                    readiness=readiness,
                    resolutions=resolutions,
                    contradiction_count=contradiction_count,
                    fact_updates=len(changed_facts),
                    now=now,
                ),
            ),
            # 5. no authorized send may be in flight while the case's evidence moves
            self.core.stage_require_no_live_send_fence(scope, now=now),
            # 6. the idempotency record, which is also this plan's commit proof
            self.idempotency.stage_create_completed(
                key,
                request_hash=request_hash,
                result_entity_refs=(
                    EntityRef(
                        entity_type="INVESTIGATION_ASSESSMENT",
                        entity_id=assessment.assessment_id.value,
                    ),
                    EntityRef(
                        entity_type="COMMUNITY_CASE",
                        entity_id=command.case_id.value,
                        version=next_case.version,
                    ),
                ),
                response_status=202,
                now=now,
            ),
            *fact_updates,
        )

    def _audit_event(
        self,
        command: RunInvestigationCommand,
        *,
        assessment: InvestigationAssessment,
        next_case: CommunityCase,
        readiness: ReadinessOutcome,
        resolutions: tuple[ResolvedStatus, ...],
        contradiction_count: int,
        fact_updates: int,
        now: datetime,
    ) -> AuditEvent:
        """Identifiers, versions, counts, and closed codes. Never a rationale and never a value.

        The overclaim code is present whenever any finding proposed a status stronger than the
        computed one, which is the audited half of "a model-proposed status may lower and may
        never raise". What is deliberately absent is the rationale text, the fact values, and
        the model's own count: the assessment row holds the first in the private zone, and the
        audit table has a wider read audience and a different retention.
        """

        codes = [readiness.reason_code]
        if contradiction_count:
            codes.append("CONTRADICTION_RECORDED")
        if any(item.overclaimed for item in resolutions):
            codes.append(EVIDENCE_STATUS_OVERCLAIM_DOWNGRADED)
        return AuditEvent(
            audit_event_id=self.ids.new_uuid(),
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            actor_type=ActorType.SYSTEM,
            actor_id_hash=command.actor_id_hash,
            event_type=observability.EventName.INVESTIGATION_APPLIED,
            occurred_at=now,
            correlation_id=command.correlation_id,
            causation_id=None,
            idempotency_key_hash=key_hash(command.idempotency_key),
            entity_refs=(
                AuditEntityRef(
                    entity_type="INVESTIGATION_ASSESSMENT",
                    entity_id=assessment.assessment_id.value,
                    version=None,
                ),
                AuditEntityRef(
                    entity_type="COMMUNITY_CASE",
                    entity_id=command.case_id.value,
                    version=next_case.version,
                ),
            ),
            decision=AuditDecision.ALLOW if readiness.ready else AuditDecision.NONE,
            reason_codes=tuple(dict.fromkeys(codes)),
            safe_details=AuditDetails(count=fact_updates, rule_id=None),
            input_hash=None,
            output_hash=assessment.assessment_hash,
        )

    # -- durable invocation record --------------------------------------------------------

    async def _record_failed_invocation(
        self,
        *,
        command: RunInvestigationCommand,
        scope: CaseScope,
        input_hash: Sha256Digest,
        error_code: str,
        now: datetime,
    ) -> None:
        """Persist that this invocation failed, with a safe code and no output.

        A failed invocation is durable so the pre-invocation replay check can refuse to run it
        again: "this invocation is over" has to survive the failure that made it so, and an
        investigation left with no record would be re-asked over the same private case by the
        next redelivery.
        """

        record = AgentInvocationResult(
            invocation_id=command.invocation_id,
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            operation_id=None,
            agent_name=StoredAgentName.INVESTIGATOR,
            prompt_version=INVESTIGATOR_PROMPT_VERSION,
            input_hash=input_hash,
            output_hash=None,
            outcome=AgentInvocationOutcome.FAILED,
            failure_code=error_code,
            result_refs=(),
            created_at=now,
        )
        await self.unit_of_work.commit(
            TransactionPlan(
                name="record-investigation-failure",
                operations=(self.core.stage_append_agent_invocation(scope, record),),
                audit_required=False,
            )
        )

    # -- observability ---------------------------------------------------------------------

    def _emit_started(
        self,
        command: RunInvestigationCommand,
        payload: InvestigationInput,
        input_hash: Sha256Digest,
        *,
        attempt: int,
    ) -> None:
        observability.agent_invocation_started(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            invocation_id=command.invocation_id,
            correlation_id=command.correlation_id,
            input_hash=input_hash,
            prompt_version=INVESTIGATOR_PROMPT_VERSION,
            attempt=attempt,
            message_count=len(payload.facts),
            candidate_summary_count=len(payload.evidence),
        )

    def _emit_agent_failure(
        self, command: RunInvestigationCommand, input_hash: Sha256Digest, error: AgentError
    ) -> None:
        if error.code is AgentErrorCode.AGENT_CONTRACT_VIOLATION:
            observability.agent_contract_denied(
                namespace=command.namespace,
                community_id=command.community_id,
                case_id=command.case_id,
                invocation_id=command.invocation_id,
                correlation_id=command.correlation_id,
                input_hash=input_hash,
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
            prompt_version=INVESTIGATOR_PROMPT_VERSION,
            reason_codes=error.reason_codes,
            retryable=error.retryable,
        )

    def _emit_applied(
        self,
        command: RunInvestigationCommand,
        next_case: CommunityCase,
        readiness: ReadinessOutcome,
        independent_source_count: int,
        resolutions: tuple[ResolvedStatus, ...],
    ) -> None:
        counts = {status.value: 0 for status in EvidenceStatus}
        for item in resolutions:
            counts[item.resolved.value] += 1
        observability.investigation_applied(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            case_version=next_case.version,
            correlation_id=command.correlation_id,
            reason_code=readiness.reason_code,
            status_counts=counts,
        )
        observability.evidence_independence_computed(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            correlation_id=command.correlation_id,
            independent_source_count=independent_source_count,
        )

    # -- keys ------------------------------------------------------------------------------

    @staticmethod
    def _key(command: RunInvestigationCommand) -> IdempotencyKey:
        return IdempotencyKey(
            partition=IdempotencyPartition(
                kind=IdempotencyPartitionKind.CASE,
                namespace=command.namespace,
                case_id=command.case_id,
            ),
            command=IdempotentCommand.APPLY_INVESTIGATION,
            actor_id_hash=command.actor_id_hash,
            key_hash=key_hash(f"investigate\x1f{command.idempotency_key}"),
        )


_PLACEHOLDER_HASH = Sha256Digest("sha256:" + "0" * 64)
"""A structurally valid digest occupying the field the real hash replaces.

The assessment hash covers every field except itself, so the value has to be built once with a
placeholder and sealed once with the digest of the rest. The placeholder never reaches storage.
"""


def _input_hash(payload: InvestigationInput) -> Sha256Digest:
    """The canonical digest of exactly what the Investigator was shown.

    Over the *payload*, deliberately not the envelope: ``requested_at`` moves between two
    legitimate attempts at one invocation identity, and a hash that moved with it would make
    the licensed retry look like a different question.
    """

    return hash_value(payload.model_dump(mode="json"))


def _output_hash(result: InvestigationResult) -> Sha256Digest:
    return hash_value(result.output.model_dump(mode="json"))


def _request_hash(command: RunInvestigationCommand) -> Sha256Digest:
    return hash_value(
        {
            "schema": "investigation-request/v1",
            "namespace": command.namespace.value,
            "case_id": str(command.case_id),
            "expected_case_version": command.expected_case_version,
            "reason": command.reason.value,
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
