"""The Phase-6 compile: strongly load, evaluate the pure compiler, persist the answer once.

This is the composition layer around ``chorus.privacy.compiler``, and its whole discipline is
that it decides nothing. It loads state, prepares safe evidence, hands both to the compiler,
and makes the compiler's answer durable. Every disclosure question -- eligibility, scope,
identity, destination, purpose, aggregation, independence, re-identification, necessity,
evidence safety -- is answered inside the frozen 22 gates and nowhere else. There is no second
eligibility engine here, no fallback, and no widening of what the caller asked for.

**One commit point.** A sanitized derivative is written to its content-addressed export key
*before* the transaction and confers no authority until the transaction commits; the DynamoDB
transaction is the sole authorization commit point
([ADR-018](../../../../docs/adr/ADR-018-safe-evidence-and-compile-commit.md)).
An ``ALLOW`` stages eight fixed participants and a ``DENY`` stages three, both independent of
how many facts or evidence items the request named -- because the case-version condition
already covers every mutable authorization input a compile read.

**A denial is durable.** It writes its audit event, its private lineage, and a completed
idempotency record. A denial that left no trace would let a redelivery re-run the compile and
append a second record of one decision, and a conservative stale denial is safe to record
because it grants no authority.

**Nothing here touches Core.** The compiler's only Core write is the send fence, so the case
participates as a check-only condition. A compile that bumped the case version would stale the
very view it had just produced against the exact-version check the proposal validator performs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from chorus.application import observability
from chorus.application.errors import PolicyDeniedError, SendAuthorizationInProgressError
from chorus.application.services.root_closure import evidence_root_ids, resolve_root_closure
from chorus.application.services.safe_evidence import PreparedEvidence, PrepareSafeEvidence
from chorus.application.services.view_records import to_stored_view
from chorus.domain.entities import (
    ActorType,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    CommunityCase,
    EvidenceItem,
    EvidenceRoot,
    Purpose,
)
from chorus.domain.errors import IntegrityError, ValidationError
from chorus.domain.facts import Fact, Report
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    EvidenceItemId,
    ExportFactId,
    FactId,
    IdGenerator,
    Namespace,
    Sha256Digest,
    ViewId,
)
from chorus.domain.mandates import CurrentMandatePointer, DisclosureMandate
from chorus.ports.clock import Clock
from chorus.ports.errors import IdempotencyConflictError, PersistenceConflictError
from chorus.ports.evidence_review import EvidenceReviewRegistryPort
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotencyStatus,
    IdempotentCommand,
)
from chorus.ports.imaging import ACCEPTED_SOURCE_MEDIA_TYPES
from chorus.ports.pagination import PageRequest
from chorus.ports.records import (
    CompileDecisionOutcome,
    CompiledEvidenceRecord,
    CompiledFactRecord,
    CompileItemOutcome,
    CompilerAuditProjection,
    CompilerGateRecord,
    CurrentViewPointer,
    StoredSafeDestination,
    StoredShareableView,
    ViewHistoryLocator,
    ViewPointerExpectation,
)
from chorus.ports.repositories import (
    AuditRepositoryPort,
    CoreRepositoryPort,
    IdempotencyRepositoryPort,
    ShareableRepositoryPort,
)
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import WriteOperation
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork
from chorus.privacy.canonical import hash_value
from chorus.privacy.compiler import (
    CompileAllow,
    CompileContext,
    CompileDeny,
    PrivacyCompiler,
    ShareableCaseView,
)
from chorus.privacy.policy import (
    COMPILER_CONTRACT_VERSION,
    COMPILER_VERSION,
    POLICY_VERSION,
    CompileCommand,
    CompilerAuditDecision,
    CompileReason,
    CompileReasonCode,
    CompilerGate,
    ExcludedFact,
    IncludedFact,
    IntendedUsage,
    Necessity,
    RequestedFact,
    SafeDestination,
)

ALLOW_FIXED_TRANSACTION_PARTICIPANTS = 8
"""The exact participant count of a successful compile, and it does not vary.

1. the immutable view; 2. the current-view pointer, conditionally replaced or created;
3. the immutable view-history locator; 4. the ``compile.allowed`` audit event; 5. the immutable
compiler audit projection; 6. the completed idempotency record, which is also the plan's commit
proof; 7. a check that the case still stands at the expected version; 8. a check that no live
send fence holds the case.

Independent of how many facts or evidence items the request named. There are deliberately no
per-fact and no per-mandate conditions: every authorization-sensitive mutation already bumps
the case version, so participant 7 covers all of them at the cost of one operation. A test
asserts this number against ``len(plan.operations)``, so a silently added participant fails
before anything reaches storage.
"""

DENY_FIXED_TRANSACTION_PARTICIPANTS = 3
"""A denial's participants: the audit event, the private lineage, and the idempotency record.

No view, no history locator, no pointer change, and no safe-evidence reference. A denial never
invalidates a current view that is still valid.
"""

MAX_PAGED_MANDATE_POINTERS = 100


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestedFactInput:
    """One requested fact, in transport terms.

    The privacy package owns ``RequestedFact``, and ``chorus_api`` may not import that
    package -- so the command speaks in plain values and this layer, which may see both,
    does the conversion. A transport that could construct a policy type directly is a
    transport that could construct one the policy did not intend.
    """

    fact_id: FactId
    necessity: str
    intended_usage: str


@dataclass(frozen=True, slots=True, kw_only=True)
class IncludedFactView:
    """One source fact and the export facts it became."""

    fact_id: FactId
    export_fact_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ExcludedFactView:
    """One source fact that did not travel, and the closed codes that say why."""

    fact_id: FactId
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CompileViewCommand:
    """One compile request, carrying exactly what the frozen ``CompileCommand`` names.

    ``compile_id`` is the logical compile identity: it addresses the private audit projection
    and is covered by the request hash. It does not replace the transport idempotency key, and
    a different ``compile_id`` under the same key is a different request.
    """

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    compile_id: UUID
    expected_case_version: int
    requested_facts: tuple[RequestedFactInput, ...]
    requested_evidence_ids: tuple[EvidenceItemId, ...]
    destination: StoredSafeDestination
    """The deployment's registry entry, in the stored shape.

    Stored rather than the compiler's own ``SafeDestination`` for the same reason the result
    is: the API composition root holds this value, and ``chorus_api`` may not import the
    privacy package. The two shapes are field for field identical and a parity test keeps
    them so; the conversion happens here, one layer below the transport.
    """

    purpose: Purpose
    actor_id_hash: Sha256Digest
    idempotency_key: str
    correlation_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CompileViewResult:
    """What an allowed compile returns. A denial is raised, never returned.

    ``view`` is the *stored* shape rather than the compiler's own DTO. The two are field for
    field identical and a parity test keeps them that way, so returning the persisted one
    costs nothing and means a caller above the application never holds a privacy type.

    It is the same on a replay, and that is the point rather than a convenience: a replay
    answers with the artifact that exists, never with one this attempt recomputed. The
    optional type remains because a caller must not assume a body it did not check for.
    """

    compile_id: UUID
    audit_event_id: UUID
    view: StoredShareableView | None
    included: tuple[IncludedFactView, ...]
    excluded: tuple[ExcludedFactView, ...]
    replayed: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class _CompileState:
    """Everything the compiler is given, loaded strongly and revalidated by its repositories."""

    case: CommunityCase
    community_public_label: str
    facts: tuple[Fact, ...]
    reports: tuple[Report, ...]
    evidence_items: tuple[EvidenceItem, ...]
    evidence_roots: tuple[EvidenceRoot, ...]
    mandates: tuple[DisclosureMandate, ...]
    mandate_pointers: tuple[CurrentMandatePointer, ...]


@dataclass(slots=True)
class CompileView:
    """Evaluate the frozen compiler over strongly loaded state, and persist its answer."""

    core: CoreRepositoryPort
    shareable: ShareableRepositoryPort
    audit: AuditRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    compiler: PrivacyCompiler
    evidence: PrepareSafeEvidence
    reviews: EvidenceReviewRegistryPort
    clock: Clock
    ids: IdGenerator
    community_public_label: str
    """The safe building label, supplied by the composition root.

    Not derived from ``Community.name``, which is private operational data. A label that
    travels outward is a deployment decision about what this building is called publicly,
    and reading it off the private record would export a name nobody chose to export.
    """

    async def execute(self, command: CompileViewCommand) -> CompileViewResult:
        scope = CaseScope(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
        )
        key = self._key(command)
        request_hash = _request_hash(command)

        replay = await self._replay(key, request_hash, scope)
        if replay is not None:
            return replay

        now = self.clock.now()
        state = await self._load(scope)
        observability.compile_started(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            case_version=state.case.version,
            correlation_id=command.correlation_id,
            actor_id_hash=command.actor_id_hash,
            requested_facts=len(command.requested_facts),
            requested_evidence=len(command.requested_evidence_ids),
        )
        candidates = await self._prepare_evidence(scope, command, state)

        context = CompileContext(
            case=state.case,
            community_public_label=state.community_public_label,
            facts=state.facts,
            reports=state.reports,
            evidence_items=state.evidence_items,
            evidence_roots=state.evidence_roots,
            mandates=state.mandates,
            mandate_pointers=state.mandate_pointers,
            destination_registry_entry=_destination(command),
            safe_evidence_candidates=tuple(item.candidate for item in candidates),
        )
        result = self.compiler.compile(_compile_command(command, now), context)

        if isinstance(result, CompileAllow):
            return await self._persist_allow(
                command, scope, state, result, candidates, key, request_hash, now
            )
        await self._persist_deny(command, scope, state, result, candidates, key, request_hash, now)
        raise AssertionError("a denial always raises")  # pragma: no cover

    # -- strong load -------------------------------------------------------------------

    async def _load(self, scope: CaseScope) -> _CompileState:
        """Strongly load every authorization input, in the frozen access-pattern shape.

        Nothing here is an eventually consistent read. The case, its facts, its reports, its
        evidence, the root closure, the mandate pointers, and the exact immutable versions
        those pointers name can each deny a compile, and a stale read that allowed one would be
        a disclosure decision made against state that no longer exists.
        """

        case = await self.core.load_case(scope)
        facts = await self.core.load_facts(scope, case.fact_ids)
        reports = await self._reports(scope, case)
        evidence_ids = tuple(
            sorted({item for fact in facts for item in fact.evidence_ids}, key=str)
        )
        evidence_items = await self.core.load_evidence_items(scope, evidence_ids)
        # ADR-017: one closure service, resolved through the root-ID locator. There is no
        # second ancestry traversal in this system, and this call is why.
        evidence_roots = await resolve_root_closure(
            self.core, scope.community_scope, evidence_root_ids(evidence_items)
        )
        pointers, mandates = await self._mandates(scope)
        return _CompileState(
            case=case,
            community_public_label=self.community_public_label,
            facts=facts,
            reports=reports,
            evidence_items=evidence_items,
            evidence_roots=evidence_roots,
            mandates=mandates,
            mandate_pointers=pointers,
        )

    async def _reports(self, scope: CaseScope, case: CommunityCase) -> tuple[Report, ...]:
        loaded = [await self.core.load_report(scope, report_id) for report_id in case.report_ids]
        return tuple(sorted(loaded, key=lambda report: str(report.report_id)))

    async def _mandates(
        self, scope: CaseScope
    ) -> tuple[tuple[CurrentMandatePointer, ...], tuple[DisclosureMandate, ...]]:
        """Load each current pointer and the exact immutable version it names.

        Both, not either. The pointer says which version is current and the record says what
        that version granted; gate 8 exists precisely to refuse the case where the two
        disagree, and it cannot do that unless both are loaded.
        """

        page = await self.core.load_current_mandate_pointers(
            scope, PageRequest(limit=MAX_PAGED_MANDATE_POINTERS)
        )
        pointers = tuple(stored.pointer for stored in page.items)
        mandates = []
        for pointer in pointers:
            mandates.append(
                await self.core.load_mandate_version(scope, pointer.mandate_id, pointer.version)
            )
        return pointers, tuple(mandates)

    # -- safe evidence -----------------------------------------------------------------

    async def _prepare_evidence(
        self,
        scope: CaseScope,
        command: CompileViewCommand,
        state: _CompileState,
    ) -> tuple[PreparedEvidence, ...]:
        """Sanitize and durably store a derivative for every requested item that could have one.

        The filter is structural rather than a policy judgement: an item whose media type has no
        transformation rule, or that carries no curated review, is simply never handed to the
        sanitizer. It then reaches the compiler with no ``SafeEvidenceCandidate``, and gate 20
        turns that absence into ``UNSAFE_EVIDENCE`` -- a disclosure outcome decided where
        disclosure outcomes are decided.

        A source that *is* a candidate but does not decode is the same story: the sanitizer's
        refusal produces no candidate and the gate answers. What is never swallowed is an
        integrity failure -- a stored key that is not the derived one, a byte length that
        disagrees with the record, an export object contradicting its own content address --
        because those say the private record and the object store disagree, which is not a
        policy question and must not be answered as one.
        """

        by_id = {item.evidence_id: item for item in state.evidence_items}
        prepared: list[PreparedEvidence] = []
        for evidence_id in sorted(set(command.requested_evidence_ids), key=str):
            item = by_id.get(evidence_id)
            if item is None or item.media_type not in ACCEPTED_SOURCE_MEDIA_TYPES:
                continue
            review = self.reviews.review_for(evidence_id)
            if review is None or not review.cleared:
                continue
            try:
                prepared.append(await self.evidence.prepare(scope, item, review))
            except ValidationError:
                continue
        return tuple(prepared)

    # -- persistence -------------------------------------------------------------------

    async def _persist_allow(
        self,
        command: CompileViewCommand,
        scope: CaseScope,
        state: _CompileState,
        result: CompileAllow,
        candidates: tuple[PreparedEvidence, ...],
        key: IdempotencyKey,
        request_hash: Sha256Digest,
        now: datetime,
    ) -> CompileViewResult:
        await self.shareable.assert_view_capacity(scope)
        stored_view = to_stored_view(result.view)
        pointer_row = await self.shareable.load_current_view_pointer(scope)
        expectation = (
            None
            if pointer_row is None
            else ViewPointerExpectation(
                row_version=pointer_row.version, view_hash=pointer_row.view_hash
            )
        )
        pointer = CurrentViewPointer(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            view_id=result.view.view_id,
            view_hash=result.view.view_hash,
            case_version=result.view.case_version,
            expires_at=result.view.expires_at,
            version=1 if pointer_row is None else pointer_row.version + 1,
            created_at=now if pointer_row is None else pointer_row.created_at,
            updated_at=now,
        )
        operations: tuple[WriteOperation, ...] = (
            # 1. the immutable view, create-only
            self.shareable.stage_append_view(scope, stored_view),
            # 2. the current pointer, bound to the exact row version and hash that were read.
            #    This is what makes a stale compile fail whole instead of rolling the pointer
            #    backwards: a newer compile moved both, so neither condition holds any more.
            self.shareable.stage_replace_current_view_pointer(scope, pointer, expected=expectation),
            # 3. the immutable history locator
            self.shareable.stage_append_view_history_locator(
                scope,
                ViewHistoryLocator(
                    namespace=scope.namespace,
                    community_id=scope.community_id,
                    case_id=scope.case_id,
                    view_id=result.view.view_id,
                    view_hash=result.view.view_hash,
                    case_version=result.view.case_version,
                    generated_at=result.view.generated_at,
                ),
            ),
            # 4. the small append-only decision event
            self.audit.stage_append_case_event(
                scope,
                self._audit_event(
                    command,
                    audit_event_id=result.audit_event_id,
                    decision=AuditDecision.ALLOW,
                    event_type="compile.allowed",
                    reason_codes=(),
                    included=len(result.included),
                    view=result.view,
                    now=now,
                ),
            ),
            # 5. the immutable private lineage
            self.audit.stage_append_compile_projection(
                scope,
                self._projection(
                    command,
                    state=state,
                    decision=CompileDecisionOutcome.ALLOW,
                    audit_event_id=result.audit_event_id,
                    gates=result.audit_decisions,
                    included=result.included,
                    excluded=result.excluded,
                    reasons=(),
                    candidates=candidates,
                    view=result.view,
                    now=now,
                ),
            ),
            # 6. the completed idempotency record, which is also this plan's commit proof
            self.idempotency.stage_create_completed(
                key,
                request_hash=request_hash,
                result_entity_refs=(
                    EntityRef(entity_type="SHAREABLE_VIEW", entity_id=result.view.view_id.value),
                    EntityRef(
                        entity_type="COMPILER_AUDIT_PROJECTION", entity_id=command.compile_id
                    ),
                ),
                response_status=200,
                now=now,
            ),
            # 7. the case has not moved since it was read
            self.core.stage_require_case_version(
                scope, expected_version=command.expected_case_version
            ),
            # 8. no authorized send is in flight
            self.core.stage_require_no_live_send_fence(scope, now=now),
        )
        plan = TransactionPlan(
            name="compile-view-allow",
            operations=operations,
            audit_required=True,
            commit_proof=self.idempotency.commit_proof(key, request_hash=request_hash),
        )
        replayed = await self._commit(plan, key, request_hash, scope, now=now)
        if replayed:
            # A concurrent twin committed first. The artifact that exists is *its* artifact,
            # and with an ordinary UUIDv4 generator this attempt minted different
            # identifiers -- so returning what was computed locally would hand the caller an
            # ALLOW that was never persisted. Gate 22 says the compiler never does that, so
            # the answer is re-derived from the durable record instead.
            answer = await self._replay(key, request_hash, scope)
            persisted = None if answer is None else answer.view
            if answer is None or persisted is None:  # pragma: no cover - the twin wrote one
                raise IntegrityError("IDEMPOTENCY_RECORD")
            observability.compile_allowed(
                namespace=command.namespace,
                community_id=command.community_id,
                case_id=command.case_id,
                case_version=state.case.version,
                correlation_id=command.correlation_id,
                actor_id_hash=command.actor_id_hash,
                view_id=persisted.view_id.value,
                view_hash=persisted.view_hash,
                included=len(answer.included),
                excluded=len(answer.excluded),
                safe_evidence=len(persisted.safe_evidence_refs),
                replayed=True,
            )
            return answer

        observability.compile_allowed(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            case_version=result.view.case_version,
            correlation_id=command.correlation_id,
            actor_id_hash=command.actor_id_hash,
            view_id=result.view.view_id.value,
            view_hash=result.view.view_hash,
            included=len(result.included),
            excluded=len(result.excluded),
            safe_evidence=len(result.view.safe_evidence_refs),
            replayed=False,
        )
        return CompileViewResult(
            compile_id=command.compile_id,
            audit_event_id=result.audit_event_id,
            view=stored_view,
            included=tuple(
                IncludedFactView(fact_id=entry.fact_id, export_fact_ids=entry.export_fact_ids)
                for entry in result.included
            ),
            excluded=tuple(
                ExcludedFactView(
                    fact_id=entry.fact_id,
                    reason_codes=tuple(code.value for code in entry.reason_codes),
                )
                for entry in result.excluded
            ),
            replayed=False,
        )

    async def _persist_deny(
        self,
        command: CompileViewCommand,
        scope: CaseScope,
        state: _CompileState,
        result: CompileDeny,
        candidates: tuple[PreparedEvidence, ...],
        key: IdempotencyKey,
        request_hash: Sha256Digest,
        now: datetime,
    ) -> None:
        """Make the refusal durable, then raise it.

        Recording a denial is not bookkeeping. Without it a redelivery of the same command would
        re-run the whole compile and append a second record of one decision, and the caller's
        retry would look like a fresh refusal rather than the same one.
        """

        reasons = tuple(dict.fromkeys(reason.code.value for reason in result.reasons))
        operations: tuple[WriteOperation, ...] = (
            self.audit.stage_append_case_event(
                scope,
                self._audit_event(
                    command,
                    audit_event_id=result.audit_event_id,
                    decision=AuditDecision.DENY,
                    event_type="compile.denied",
                    reason_codes=reasons,
                    included=0,
                    view=None,
                    now=now,
                ),
            ),
            self.audit.stage_append_compile_projection(
                scope,
                self._projection(
                    command,
                    state=state,
                    decision=CompileDecisionOutcome.DENY,
                    audit_event_id=result.audit_event_id,
                    gates=result.audit_decisions,
                    included=(),
                    excluded=(),
                    reasons=result.reasons,
                    candidates=candidates,
                    view=None,
                    now=now,
                ),
            ),
            self.idempotency.stage_create_completed(
                key,
                request_hash=request_hash,
                result_entity_refs=(
                    EntityRef(
                        entity_type="COMPILER_AUDIT_PROJECTION", entity_id=command.compile_id
                    ),
                ),
                response_status=422,
                now=now,
            ),
        )
        plan = TransactionPlan(
            name="compile-view-deny",
            operations=operations,
            audit_required=True,
            commit_proof=self.idempotency.commit_proof(key, request_hash=request_hash),
        )
        await self._commit(plan, key, request_hash, scope, now=now)

        observability.compile_denied(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            case_version=state.case.version,
            correlation_id=command.correlation_id,
            actor_id_hash=command.actor_id_hash,
            reason_codes=reasons,
        )
        raise PolicyDeniedError(reasons)

    async def _commit(
        self,
        plan: TransactionPlan,
        key: IdempotencyKey,
        request_hash: Sha256Digest,
        scope: CaseScope,
        *,
        now: datetime,
    ) -> bool:
        """Commit, and say which of three things a conditional failure actually was.

        A rejected plan is a concurrent duplicate of this very command, a live send fence, or a
        genuine stale conflict. They need different answers, so the classification reads state
        rather than inferring from the failure. The replay is checked first, because a duplicate
        that lost the race to its own twin should be told it succeeded even if a fence happened
        to appear afterwards.

        Returns whether the answer came from a twin that had already committed.
        """

        try:
            await self.unit_of_work.commit(plan)
        except PersistenceConflictError:
            record = await self.idempotency.load(key)
            if record is not None and record.status is IdempotencyStatus.COMPLETED:
                if record.request_hash != request_hash:
                    raise IdempotencyConflictError("SHAREABLE_VIEW") from None
                return True
            fence = await self.core.load_send_fence(scope)
            if fence is not None and now < fence.expires_at:
                raise SendAuthorizationInProgressError(("SEND_FENCE_ACTIVE",)) from None
            raise
        return False

    # -- projection and audit ----------------------------------------------------------

    def _projection(
        self,
        command: CompileViewCommand,
        *,
        state: _CompileState,
        decision: CompileDecisionOutcome,
        audit_event_id: UUID,
        gates: tuple[CompilerAuditDecision, ...],
        included: tuple[IncludedFact, ...],
        excluded: tuple[ExcludedFact, ...],
        reasons: tuple[CompileReason, ...],
        candidates: tuple[PreparedEvidence, ...],
        view: ShareableCaseView | None,
        now: datetime,
    ) -> CompilerAuditProjection:
        """Build the private lineage: which source became which export, and why not otherwise.

        This is the record ``ShareableFact`` deliberately does not carry. Every value in it is
        an identifier, a closed code, a version, or a digest -- the record type refuses anything
        else -- so the row can hold private fact identifiers without becoming a second corpus.
        """

        grants = {
            grant.fact_id: grant.max_scope
            for mandate in state.mandates
            for grant in mandate.fact_grants
        }
        included_by_fact = {entry.fact_id: entry for entry in included}
        excluded_by_fact = {entry.fact_id: entry for entry in excluded}
        fact_records = tuple(
            CompiledFactRecord(
                fact_id=requested.fact_id,
                necessity=requested.necessity.value,
                intended_usage=requested.intended_usage.value,
                granted_scope=grants.get(requested.fact_id),
                outcome=(
                    CompileItemOutcome.INCLUDED
                    if requested.fact_id in included_by_fact
                    else CompileItemOutcome.EXCLUDED
                ),
                reason_codes=tuple(
                    dict.fromkeys(
                        code.value for code in excluded_by_fact[requested.fact_id].reason_codes
                    )
                )
                if requested.fact_id in excluded_by_fact
                else (),
                export_fact_ids=tuple(
                    ExportFactId(value)
                    for value in included_by_fact[requested.fact_id].export_fact_ids
                )
                if requested.fact_id in included_by_fact
                else (),
            )
            for requested in sorted(_requested_facts(command), key=lambda item: str(item.fact_id))
        )
        exported = (
            {ref.export_handle_id: ref for ref in view.safe_evidence_refs}
            if view is not None
            else {}
        )
        prepared_by_source = {item.candidate.source_evidence_id: item for item in candidates}
        evidence_records: list[CompiledEvidenceRecord] = []
        for evidence_id in sorted(set(command.requested_evidence_ids), key=str):
            prepared = prepared_by_source.get(evidence_id)
            ref = exported.get(prepared.export_handle_id) if prepared is not None else None
            if ref is not None:
                evidence_records.append(
                    CompiledEvidenceRecord(
                        source_evidence_id=evidence_id,
                        outcome=CompileItemOutcome.INCLUDED,
                        safe_evidence_ref_id=ref.safe_evidence_ref_id,
                        export_handle_id=ref.export_handle_id,
                        derivative_sha256=ref.sha256,
                    )
                )
                continue
            evidence_records.append(
                CompiledEvidenceRecord(
                    source_evidence_id=evidence_id,
                    outcome=CompileItemOutcome.EXCLUDED,
                    reason_codes=(CompileReasonCode.UNSAFE_EVIDENCE.value,),
                )
            )
        return CompilerAuditProjection(
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            compile_id=command.compile_id,
            audit_event_id=audit_event_id,
            requested_at=now,
            created_at=now,
            based_on_case_version=state.case.version,
            compiler_version=COMPILER_VERSION,
            policy_version=POLICY_VERSION,
            destination_id=command.destination.destination_id,
            destination_registry_version=command.destination.registry_version,
            destination_routing_token=command.destination.routing_token,
            purpose=command.purpose,
            decision=decision,
            reason_codes=tuple(dict.fromkeys(reason.code.value for reason in reasons)),
            gates=tuple(
                CompilerGateRecord(
                    gate=int(record.gate),
                    gate_name=CompilerGate(record.gate).name,
                    outcome=record.outcome.value,
                    reason_codes=tuple(dict.fromkeys(code.value for code in record.reason_codes)),
                )
                for record in gates
            ),
            facts=fact_records,
            evidence=tuple(evidence_records),
            view_id=None if view is None else view.view_id,
            view_hash=None if view is None else view.view_hash,
        )

    def _audit_event(
        self,
        command: CompileViewCommand,
        *,
        audit_event_id: UUID,
        decision: AuditDecision,
        event_type: str,
        reason_codes: tuple[str, ...],
        included: int,
        view: ShareableCaseView | None,
        now: datetime,
    ) -> AuditEvent:
        """The small append-only decision event; the lineage lives in the projection."""

        refs = [
            AuditEntityRef(
                entity_type="COMMUNITY_CASE", entity_id=command.case_id.value, version=None
            ),
            AuditEntityRef(
                entity_type="COMPILER_AUDIT_PROJECTION",
                entity_id=command.compile_id,
                version=None,
            ),
        ]
        if view is not None:
            refs.append(
                AuditEntityRef(
                    entity_type="SHAREABLE_VIEW", entity_id=view.view_id.value, version=None
                )
            )
        return AuditEvent(
            audit_event_id=audit_event_id,
            namespace=command.namespace,
            community_id=command.community_id,
            case_id=command.case_id,
            actor_type=ActorType.SYSTEM,
            actor_id_hash=command.actor_id_hash,
            event_type=event_type,
            occurred_at=now,
            correlation_id=command.correlation_id or command.compile_id,
            causation_id=None,
            idempotency_key_hash=_key_hash(command.idempotency_key),
            entity_refs=tuple(refs),
            decision=decision,
            reason_codes=reason_codes,
            safe_details=AuditDetails(count=included, rule_id=None),
            input_hash=None,
            output_hash=None if view is None else view.view_hash,
        )

    # -- idempotency -------------------------------------------------------------------

    async def _replay(
        self, key: IdempotencyKey, request_hash: Sha256Digest, scope: CaseScope
    ) -> CompileViewResult | None:
        """Answer from the durable record, allowed or denied, without recompiling.

        A completed compile is never regenerated: no gate runs again, no identifier is minted,
        and no second object is written. A denial replays as the same denial, which is what
        stops a redelivery from appending a second record of one decision.
        """

        record = await self.idempotency.load(key)
        if record is None:
            return None
        if record.request_hash != request_hash:
            raise IdempotencyConflictError("SHAREABLE_VIEW")
        if record.status is not IdempotencyStatus.COMPLETED:
            return None
        compile_ref = next(
            (
                ref
                for ref in record.result_entity_refs
                if ref.entity_type == "COMPILER_AUDIT_PROJECTION"
            ),
            None,
        )
        if compile_ref is None:  # pragma: no cover - every completed compile records one
            raise IntegrityError("IDEMPOTENCY_RECORD")
        projection = await self.audit.load_compile_projection(scope, compile_ref.entity_id)
        if projection is None:
            raise IntegrityError("COMPILER_AUDIT_PROJECTION")
        if projection.decision is CompileDecisionOutcome.DENY:
            raise PolicyDeniedError(projection.reason_codes)
        view_ref = next(
            (ref for ref in record.result_entity_refs if ref.entity_type == "SHAREABLE_VIEW"),
            None,
        )
        if view_ref is None:  # pragma: no cover - an allowed compile always records its view
            raise IntegrityError("IDEMPOTENCY_RECORD")
        # The artifact is loaded rather than merely proved to exist, because the answer a
        # replay gives has to *be* the persisted view. A locally recomputed one would carry
        # this attempt's identifiers, and gate 22 says an ALLOW is never returned unpersisted.
        stored = await self.shareable.load_view(scope, ViewId(view_ref.entity_id))
        return CompileViewResult(
            compile_id=projection.compile_id,
            audit_event_id=projection.audit_event_id,
            view=stored,
            included=tuple(
                IncludedFactView(
                    fact_id=entry.fact_id,
                    export_fact_ids=tuple(value.value for value in entry.export_fact_ids),
                )
                for entry in projection.facts
                if entry.outcome is CompileItemOutcome.INCLUDED
            ),
            excluded=tuple(
                ExcludedFactView(fact_id=entry.fact_id, reason_codes=entry.reason_codes)
                for entry in projection.facts
                if entry.outcome is CompileItemOutcome.EXCLUDED
            ),
            replayed=True,
        )

    @staticmethod
    def _key(command: CompileViewCommand) -> IdempotencyKey:
        return IdempotencyKey(
            partition=IdempotencyPartition(
                # The Shareable table's case-scoped partition is ``VIEW_CURRENT``. The
                # compiler's Shareable writes are restricted by ``LeadingKeys`` to the two
                # view prefixes, so a record under ``CASE`` would be one the only principal
                # allowed to write it is denied.
                kind=IdempotencyPartitionKind.VIEW_CURRENT,
                namespace=command.namespace,
                case_id=command.case_id,
            ),
            command=IdempotentCommand.COMPILE_VIEW,
            actor_id_hash=command.actor_id_hash,
            key_hash=_key_hash(command.idempotency_key),
        )


def _destination(command: CompileViewCommand) -> SafeDestination:
    """Mirror the stored registry entry into the closed policy type."""

    return SafeDestination(
        destination_id=command.destination.destination_id,
        kind=command.destination.kind,
        registry_version=command.destination.registry_version,
        routing_token=command.destination.routing_token,
        display_label=command.destination.display_label,
    )


def _requested_facts(command: CompileViewCommand) -> tuple[RequestedFact, ...]:
    """Convert transport values into the closed policy type, rejecting anything unknown."""

    return tuple(
        RequestedFact(
            fact_id=item.fact_id,
            necessity=Necessity(item.necessity),
            intended_usage=IntendedUsage(item.intended_usage),
        )
        for item in command.requested_facts
    )


def _compile_command(command: CompileViewCommand, now: datetime) -> CompileCommand:
    """Build the frozen compiler command. The clock is read once, by the caller, and injected."""

    return CompileCommand(
        compile_id=command.compile_id,
        namespace=command.namespace,
        case_id=command.case_id,
        expected_case_version=command.expected_case_version,
        requested_facts=_requested_facts(command),
        requested_evidence_ids=command.requested_evidence_ids,
        destination=_destination(command),
        purpose=command.purpose,
        requested_at=now,
        policy_version=POLICY_VERSION,
        compiler_contract_version=COMPILER_CONTRACT_VERSION,
    )


def _request_hash(command: CompileViewCommand) -> Sha256Digest:
    """Hash the normalized command, ``compile_id`` included.

    ``compile_id`` is inside the hash deliberately. It is the logical compile identity, so two
    requests that differ only in it are different requests, and reusing one transport key across
    them is an ``IDEMPOTENCY_CONFLICT`` rather than a silent second compile under one key.

    Requested facts and evidence are sorted, because the order a caller listed them in is not
    part of what they asked for -- the compiler canonicalizes it anyway.
    """

    return hash_value(
        {
            "namespace": command.namespace,
            "community_id": command.community_id,
            "case_id": command.case_id,
            "compile_id": command.compile_id,
            "expected_case_version": command.expected_case_version,
            "requested_facts": tuple(
                {
                    "fact_id": item.fact_id,
                    "necessity": item.necessity,
                    "intended_usage": item.intended_usage,
                }
                for item in sorted(command.requested_facts, key=lambda item: str(item.fact_id))
            ),
            "requested_evidence_ids": tuple(sorted(command.requested_evidence_ids, key=str)),
            "destination": _destination(command),
            "purpose": command.purpose,
            "policy_version": POLICY_VERSION,
            "compiler_contract_version": COMPILER_CONTRACT_VERSION,
        }
    )


def _key_hash(value: str) -> Sha256Digest:
    """Hash the caller's key, because caller text never enters a storage key."""

    return Sha256Digest(f"sha256:{sha256(f'compile-view\x1f{value}'.encode()).hexdigest()}")
