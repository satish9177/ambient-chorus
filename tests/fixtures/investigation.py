"""A Phase 5 harness: one seeded case, the real use cases, and a substitutable agent.

Everything an investigation test needs is assembled from production classes. The only
substitution is the agent itself, which is the point: if the projection, the validator, the
status computation, the readiness predicate, the compile preflight, and the transaction were
stubbed too, a passing test would say nothing about the system that runs.

The case is seeded directly rather than driven through ingestion and the Monitor. That is
deliberate: the adversarial matrix needs a case with an exact shape -- two reporters asserting
byte-identical values, one forwarded evidence root, a chain with a missing locator -- and
producing those shapes by persuading a fake intake model to emit them would test the fake.

The harness is driver-agnostic, so the same scenarios run against the in-memory emulator and
against DynamoDB Local.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid5

from chorus.application.commands.run_investigation import (
    InvestigationReason,
    RunInvestigation,
    RunInvestigationCommand,
)
from chorus.application.commands.run_investigation_operation import (
    InvestigationOperationWorker,
)
from chorus.application.operations import ApplicationOperations, investigate_binding_hash
from chorus.domain.entities import (
    ApplicationOperation,
    ApplicationOperationKind,
    ApplicationOperationStatus,
    CaseState,
    Community,
    CommunityCase,
    CommunityStatus,
    Contributor,
    ContributorStatus,
    DerivationKind,
    DestinationKind,
    DisclosureScope,
    EvidenceItem,
    EvidenceRoot,
    EvidenceStatus,
    ExtractionStatus,
    FactType,
    MalwareScanStatus,
    MandateStatus,
    Purpose,
    SensitivityCategory,
)
from chorus.domain.facts import (
    Fact,
    FactStatus,
    FactValue,
    FailureMode,
    IncidentOccurrence,
    LocationArea,
    LocationAreaCode,
    Report,
    ReportStatus,
)
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    DestinationId,
    EvidenceItemId,
    EvidenceRootId,
    FactId,
    MandateId,
    MessageId,
    Namespace,
    OperationId,
    ReportId,
    SensitiveStr,
    Sha256Digest,
    Uuid5Generator,
)
from chorus.domain.mandates import (
    CurrentMandatePointer,
    DisclosureMandate,
    FactGrant,
    IdentityGrant,
)
from chorus.infrastructure.dynamodb.audit import AuditRepository
from chorus.infrastructure.dynamodb.core import CoreRepository
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.idempotency import IdempotencyRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.ports.agents import (
    InvestigationInvocation,
    InvestigationResult,
    InvestigatorAgentPort,
)
from chorus.ports.operations import InvestigationOperationJob
from chorus.ports.records import EvidenceRootLocator, StoredCurrentMandatePointer
from chorus.ports.retention import AuditRetention
from chorus.ports.scopes import CaseScope, CommunityScope
from chorus.ports.storage import StorageDriver, TableName, WriteOperation
from chorus.ports.unit_of_work import TransactionPlan
from chorus.privacy.canonical import hash_mandate_terms
from chorus.privacy.policy import SafeDestination

NAMESPACE = Namespace("TEST_INVESTIGATION")
CURSOR_SECRET = b"chorus-investigation-test-pagination-secret"
NOW = datetime(2030, 1, 20, 9, 0, 0, tzinfo=UTC)
FIXTURE_ID_NAMESPACE = UUID("58a0f0a6-6f0c-5c8a-9d2c-1a2b3c4d5e6f")
PRESENTER_ACTOR_HASH = Sha256Digest(f"sha256:{sha256(b'presenter_admin').hexdigest()}")
DESTINATION_ID = DestinationId("property_manager:demo")
COMMUNITY_LABEL = "Example Community Building"

DESTINATION = SafeDestination(
    destination_id=DESTINATION_ID,
    kind=DestinationKind.PROPERTY_MANAGER,
    registry_version=1,
    routing_token=UUID("00000000-0000-0000-0000-000000000001"),
    display_label="Property Management",
)


def digest(value: str) -> Sha256Digest:
    return Sha256Digest(f"sha256:{sha256(value.encode('utf-8')).hexdigest()}")


@dataclass(slots=True)
class SteppableClock:
    """A clock that only moves when a test says so."""

    instant: datetime = NOW

    def now(self) -> datetime:
        return self.instant

    def advance(self, *, seconds: int) -> None:
        self.instant += timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True, kw_only=True)
class SeededFact:
    """One fact to seed, named by a stable label rather than by an identifier."""

    label: str
    reporter: str
    value: FactValue
    fact_type: FactType
    sensitivity: SensitivityCategory = SensitivityCategory.GENERAL
    evidence_labels: tuple[str, ...] = ()
    status: FactStatus = FactStatus.ACTIVE
    evidence_status: EvidenceStatus = EvidenceStatus.REPORTED


@dataclass(frozen=True, slots=True, kw_only=True)
class SeededEvidence:
    """One evidence item and the root it belongs to, named by label."""

    label: str
    reporter: str
    root_label: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SeededRoot:
    """One evidence root, optionally forwarded from another."""

    label: str
    parent_label: str | None = None
    write_locator: bool = True
    """Whether the ADR-017 ID locator is written beside the canonical root.

    Always true in ordinary seeding. A test that omits it is modelling a root written before
    ADR-017 landed, which is exactly the backfill case the loader must fail closed on.
    """


SEED_CHUNK = 50
"""How many staged writes one seeding transaction carries, well under the frozen maximum."""


@dataclass(slots=True)
class InvestigationHarness:
    """One seeded community and case plus every Phase 5 use case over one storage driver."""

    driver: StorageDriver
    namespace: Namespace = NAMESPACE
    clock: SteppableClock = field(default_factory=SteppableClock)
    core: CoreRepository = field(init=False)
    audit: AuditRepository = field(init=False)
    idempotency: IdempotencyRepository = field(init=False)
    unit_of_work: StorageUnitOfWork = field(init=False)
    ids: Uuid5Generator = field(init=False)

    def __post_init__(self) -> None:
        cursors = SignedCursorCodec(CURSOR_SECRET)
        self.core = CoreRepository(driver=self.driver, cursors=cursors)
        self.audit = AuditRepository(
            driver=self.driver, cursors=cursors, retention=AuditRetention.demo()
        )
        self.idempotency = IdempotencyRepository(driver=self.driver, table=TableName.CORE)
        self.unit_of_work = StorageUnitOfWork(driver=self.driver)
        self.ids = Uuid5Generator(namespace=FIXTURE_ID_NAMESPACE, prefix=self.namespace.value)

    # -- identity ------------------------------------------------------------------------

    def uuid(self, name: str) -> UUID:
        return uuid5(FIXTURE_ID_NAMESPACE, f"{self.namespace.value}:{name}")

    @property
    def community_id(self) -> CommunityId:
        return CommunityId(self.uuid("community"))

    @property
    def case_id(self) -> CaseId:
        return CaseId(self.uuid("case"))

    def contributor_id(self, pseudonym: str) -> ContributorId:
        return ContributorId(self.uuid(f"contributor:{pseudonym}"))

    def report_id(self, label: str) -> ReportId:
        return ReportId(self.uuid(f"report:{label}"))

    def fact_id(self, label: str) -> FactId:
        return FactId(self.uuid(f"fact:{label}"))

    def evidence_id(self, label: str) -> EvidenceItemId:
        return EvidenceItemId(self.uuid(f"evidence:{label}"))

    def root_id(self, label: str) -> EvidenceRootId:
        return EvidenceRootId(self.uuid(f"root:{label}"))

    def operation_id(self, label: str = "1") -> OperationId:
        return OperationId(self.uuid(f"operation:{label}"))

    def invocation_id(self, label: str = "1") -> UUID:
        return self.uuid(f"invocation:{label}")

    @property
    def community_scope(self) -> CommunityScope:
        return CommunityScope(namespace=self.namespace, community_id=self.community_id)

    @property
    def case_scope(self) -> CaseScope:
        return CaseScope(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
        )

    # -- seeding -------------------------------------------------------------------------

    async def seed(
        self,
        *,
        reporters: tuple[str, ...] = ("resident-a", "resident-b"),
        facts: tuple[SeededFact, ...] | None = None,
        evidence: tuple[SeededEvidence, ...] = (),
        roots: tuple[SeededRoot, ...] = (),
        state: CaseState = CaseState.INVESTIGATING,
        approved_facts: tuple[str, ...] | None = None,
        corroboration_source_count: int = 0,
    ) -> CommunityCase:
        """Create one community, its contributors, and one case with exactly this shape.

        ``approved_facts`` names the fact labels that carry an ``APPROVED`` mandate grant at
        ``ANONYMOUS_CASE``; the default is every seeded fact whose type policy/v1 can export at
        all. A test that wants "no compilable purpose" passes an empty tuple.
        """

        now = self.clock.now()
        seeded_facts = self.default_facts(reporters) if facts is None else facts
        operations: list[WriteOperation] = [
            self.core.stage_create_community(
                self.community_scope.namespace_scope,
                Community(
                    community_id=self.community_id,
                    namespace=self.namespace,
                    name=COMMUNITY_LABEL,
                    timezone="UTC",
                    status=CommunityStatus.ACTIVE,
                    version=1,
                    created_at=now,
                    updated_at=now,
                ),
            )
        ]
        for pseudonym in reporters:
            operations.append(
                self.core.stage_create_contributor(
                    self.community_scope,
                    Contributor(
                        contributor_id=self.contributor_id(pseudonym),
                        community_id=self.community_id,
                        namespace=self.namespace,
                        pseudonym=pseudonym,
                        display_name=SensitiveStr(f"Resident {pseudonym}"),
                        email=SensitiveStr(f"{pseudonym}@example.invalid"),
                        status=ContributorStatus.ACTIVE,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    ),
                )
            )

        for root in roots:
            operations.extend(self._root_operations(root, now=now))
        for item in evidence:
            operations.append(
                self.core.stage_create_evidence_item(
                    self.case_scope, self._evidence_item(item, now=now)
                )
            )

        report_labels = {fact.reporter for fact in seeded_facts}
        for reporter in sorted(report_labels):
            operations.append(
                self.core.stage_create_report(self.case_scope, self.report(reporter, now=now))
            )
        for fact in seeded_facts:
            operations.append(
                self.core.stage_create_fact(self.case_scope, self.fact(fact, now=now))
            )

        case = CommunityCase(
            case_id=self.case_id,
            community_id=self.community_id,
            namespace=self.namespace,
            title="Recurring elevator failure",
            issue_type="ELEVATOR_FAILURE",
            state=state,
            report_ids=tuple(self.report_id(label) for label in sorted(report_labels)),
            fact_ids=tuple(self.fact_id(fact.label) for fact in seeded_facts),
            assessment_id=None,
            current_view_id=None,
            current_action_id=None,
            corroboration_source_count=corroboration_source_count,
            state_reason_code="SEEDED",
            version=1,
            created_at=now,
            updated_at=now,
        )
        operations.append(self.core.stage_create_case(self.case_scope, case))

        grantable = (
            tuple(
                fact.label
                for fact in seeded_facts
                if fact.fact_type
                in {FactType.INCIDENT_OCCURRENCE, FactType.LOCATION_AREA, FactType.SERVICE_IMPACT}
            )
            if approved_facts is None
            else approved_facts
        )
        operations.extend(self._mandate_operations(seeded_facts, grantable, now=now))

        # Committed in bounded chunks rather than one transaction. A case at the frozen
        # hundred-fact ceiling has more rows than DynamoDB's transaction maximum, and seeding
        # is not the thing under test -- the apply is, and the apply's own bound is asserted
        # separately.
        for index in range(0, len(operations), SEED_CHUNK):
            chunk = tuple(operations[index : index + SEED_CHUNK])
            await self.unit_of_work.commit(
                TransactionPlan(
                    name="seed-investigation-case",
                    operations=chunk,
                    audit_required=False,
                )
            )
        return case

    def default_facts(self, reporters: tuple[str, ...]) -> tuple[SeededFact, ...]:
        """One byte-identical location claim per reporter, plus one unique incident each.

        The shape is chosen to make the two halves of ADR-015 visible in one case: the location
        claims group and corroborate, while each reporter's incident instant is theirs alone and
        stays ``REPORTED`` however corroborated the case becomes.
        """

        seeded: list[SeededFact] = []
        for index, reporter in enumerate(reporters):
            seeded.append(
                SeededFact(
                    label=f"location:{reporter}",
                    reporter=reporter,
                    fact_type=FactType.LOCATION_AREA,
                    value=LocationArea(area=LocationAreaCode.ELEVATOR_CAB),
                )
            )
            seeded.append(
                SeededFact(
                    label=f"incident:{reporter}",
                    reporter=reporter,
                    fact_type=FactType.INCIDENT_OCCURRENCE,
                    value=IncidentOccurrence(
                        occurred_at=NOW - timedelta(days=index + 1),
                        failure_mode=FailureMode.STUCK,
                    ),
                )
            )
        return tuple(seeded)

    def report(self, reporter: str, *, now: datetime) -> Report:
        return Report(
            report_id=self.report_id(reporter),
            case_id=self.case_id,
            community_id=self.community_id,
            contributor_id=self.contributor_id(reporter),
            namespace=self.namespace,
            source_message_ids=(MessageId(self.uuid(f"message:{reporter}")),),
            issue_type="ELEVATOR_FAILURE",
            private_summary=SensitiveStr(f"{reporter} reported the lift stuck again."),
            occurred_at=now - timedelta(days=1),
            location_area=LocationAreaCode.ELEVATOR_CAB,
            evidence_ids=(),
            status=ReportStatus.ACTIVE,
            duplicate_of_report_id=None,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def fact(self, seeded: SeededFact, *, now: datetime) -> Fact:
        return Fact(
            fact_id=self.fact_id(seeded.label),
            case_id=self.case_id,
            report_id=self.report_id(seeded.reporter),
            community_id=self.community_id,
            contributor_id=self.contributor_id(seeded.reporter),
            namespace=self.namespace,
            fact_type=seeded.fact_type,
            value=seeded.value,
            sensitivity=seeded.sensitivity,
            evidence_ids=tuple(self.evidence_id(label) for label in seeded.evidence_labels),
            evidence_status=seeded.evidence_status,
            source_message_ids=(MessageId(self.uuid(f"message:{seeded.reporter}")),),
            supersedes_fact_id=None,
            status=seeded.status,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def _root_operations(self, root: SeededRoot, *, now: datetime) -> list[WriteOperation]:
        entity = EvidenceRoot(
            root_id=self.root_id(root.label),
            community_id=self.community_id,
            namespace=self.namespace,
            root_sha256=digest(f"root:{root.label}"),
            media_type="image/jpeg",
            first_observed_at=now - timedelta(days=2),
            derivation_kind=(
                DerivationKind.ORIGINAL if root.parent_label is None else DerivationKind.FORWARDED
            ),
            parent_root_id=(None if root.parent_label is None else self.root_id(root.parent_label)),
            created_at=now - timedelta(days=2),
            updated_at=now - timedelta(days=2),
        )
        operations: list[WriteOperation] = [
            self.core.stage_create_evidence_root(self.community_scope, entity)
        ]
        if root.write_locator:
            operations.append(
                self.core.stage_create_evidence_root_locator(
                    self.community_scope,
                    EvidenceRootLocator(
                        namespace=self.namespace,
                        community_id=self.community_id,
                        root_id=entity.root_id,
                        root_sha256=entity.root_sha256,
                        created_at=now - timedelta(days=2),
                    ),
                )
            )
        return operations

    def _evidence_item(self, seeded: SeededEvidence, *, now: datetime) -> EvidenceItem:
        return EvidenceItem(
            evidence_id=self.evidence_id(seeded.label),
            root_id=self.root_id(seeded.root_label),
            community_id=self.community_id,
            case_id=self.case_id,
            namespace=self.namespace,
            submitted_by_contributor_id=self.contributor_id(seeded.reporter),
            source_message_id=None,
            private_object_key=SensitiveStr(f"private/{seeded.label}"),
            media_type="image/jpeg",
            byte_length=2048,
            sha256=digest(f"evidence:{seeded.label}"),
            captured_at=now - timedelta(days=2),
            uploaded_at=now - timedelta(days=2),
            derived_from_evidence_id=None,
            malware_scan_status=MalwareScanStatus.CLEAN,
            extraction_status=ExtractionStatus.NOT_NEEDED,
            extracted_text=None,
            version=1,
            created_at=now - timedelta(days=2),
            updated_at=now - timedelta(days=2),
        )

    def _mandate_operations(
        self,
        facts: tuple[SeededFact, ...],
        grantable: tuple[str, ...],
        *,
        now: datetime,
    ) -> list[WriteOperation]:
        """One approved mandate per contributor covering that contributor's grantable facts."""

        by_reporter: dict[str, list[SeededFact]] = {}
        for fact in facts:
            by_reporter.setdefault(fact.reporter, []).append(fact)
        operations: list[WriteOperation] = []
        for reporter, owned in sorted(by_reporter.items()):
            grants = tuple(
                FactGrant(
                    fact_id=self.fact_id(fact.label),
                    max_scope=DisclosureScope.ANONYMOUS_CASE,
                    allow_safe_transformation=True,
                )
                for fact in owned
                if fact.label in grantable
            )
            if not grants:
                continue
            mandate_id = MandateId(self.uuid(f"mandate:{reporter}"))
            draft = DisclosureMandate(
                mandate_id=mandate_id,
                version=1,
                case_id=self.case_id,
                community_id=self.community_id,
                contributor_id=self.contributor_id(reporter),
                namespace=self.namespace,
                status=MandateStatus.APPROVED,
                fact_grants=grants,
                identity_grant=IdentityGrant(
                    externally_shareable=False,
                    max_scope=DisclosureScope.ANONYMOUS_CASE,
                ),
                allowed_destination_ids=(DESTINATION_ID,),
                allowed_purposes=(Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,),
                valid_from=now - timedelta(days=1),
                expires_at=now + timedelta(days=30),
                proposed_at=now - timedelta(days=1),
                decided_at=now - timedelta(hours=12),
                revoked_at=None,
                decision_actor_id=self.contributor_id(reporter),
                supersedes_version=None,
                terms_hash=digest("placeholder"),
                created_at=now - timedelta(days=1),
                updated_at=now - timedelta(hours=12),
            )
            mandate = replace(draft, terms_hash=hash_mandate_terms(draft))
            pointer = CurrentMandatePointer(
                mandate_id=mandate_id,
                version=1,
                case_id=self.case_id,
                contributor_id=mandate.contributor_id,
                terms_hash=mandate.terms_hash,
            )
            operations.append(self.core.stage_append_mandate_version(self.case_scope, mandate))
            operations.append(
                self.core.stage_replace_current_mandate_pointer(
                    self.case_scope,
                    StoredCurrentMandatePointer(
                        namespace=self.namespace,
                        community_id=self.community_id,
                        pointer=pointer,
                        status=MandateStatus.APPROVED,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    ),
                    expected=None,
                )
            )
        return operations

    # -- use cases -----------------------------------------------------------------------

    def run_investigation(self, agent: InvestigatorAgentPort) -> RunInvestigation:
        return RunInvestigation(
            core=self.core,
            audit=self.audit,
            idempotency=self.idempotency,
            unit_of_work=self.unit_of_work,
            agent=agent,
            clock=self.clock,
            ids=self.ids,
            community_public_label=COMMUNITY_LABEL,
            destination=DESTINATION,
        )

    @property
    def operations(self) -> ApplicationOperations:
        return ApplicationOperations(
            core=self.core,
            idempotency=self.idempotency,
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            ids=self.ids,
        )

    def worker(self, agent: InvestigatorAgentPort) -> InvestigationOperationWorker:
        return InvestigationOperationWorker(
            operations=self.operations, run_investigation=self.run_investigation(agent)
        )

    # -- commands and jobs ---------------------------------------------------------------

    def command(
        self,
        *,
        expected_case_version: int = 1,
        reason: InvestigationReason = InvestigationReason.INITIAL,
        label: str = "1",
        idempotency_key: str = "investigate-key-0001",
    ) -> RunInvestigationCommand:
        return RunInvestigationCommand(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
            operation_id=self.operation_id(label),
            invocation_id=self.invocation_id(label),
            correlation_id=self.uuid(f"correlation:{label}"),
            actor_id_hash=PRESENTER_ACTOR_HASH,
            expected_case_version=expected_case_version,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    async def bound_operation(
        self,
        *,
        expected_case_version: int = 1,
        reason: InvestigationReason = InvestigationReason.INITIAL,
        label: str = "1",
        kind: ApplicationOperationKind = ApplicationOperationKind.INVESTIGATE,
    ) -> ApplicationOperation:
        """Create the durable operation a worker binds against, carrying its handover."""

        now = self.clock.now()
        binding = investigate_binding_hash(
            case_id=self.case_id,
            expected_case_version=expected_case_version,
            reason=reason.value,
        )
        invokes_agent = kind is not ApplicationOperationKind.SEND_ACTION
        operation = ApplicationOperation(
            operation_id=self.operation_id(label),
            kind=kind,
            namespace=self.namespace,
            actor_id_hash=PRESENTER_ACTOR_HASH,
            case_id=self.case_id,
            request_hash=binding,
            status=ApplicationOperationStatus.PENDING,
            result_refs=(),
            error_code=None,
            expires_at_epoch=int((now + timedelta(days=7)).timestamp()),
            version=1,
            created_at=now,
            updated_at=now,
            agent_invocation_id=self.invocation_id(label) if invokes_agent else None,
            agent_binding_hash=binding if invokes_agent else None,
        )
        await self.unit_of_work.commit(
            TransactionPlan(
                name="seed-operation",
                operations=(
                    self.core.stage_create_operation(
                        self.community_scope.namespace_scope, operation
                    ),
                ),
                audit_required=False,
            )
        )
        return operation

    def job_for(
        self,
        operation: ApplicationOperation,
        *,
        expected_case_version: int = 1,
        reason: InvestigationReason = InvestigationReason.INITIAL,
        label: str = "1",
        idempotency_key: str = "investigate-key-0001",
    ) -> InvestigationOperationJob:
        return InvestigationOperationJob(
            operation_id=operation.operation_id,
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
            invocation_id=self.invocation_id(label),
            correlation_id=self.uuid(f"correlation:{label}"),
            actor_id_hash=PRESENTER_ACTOR_HASH,
            request_hash=operation.request_hash,
            expected_case_version=expected_case_version,
            reason=reason.value,
            idempotency_key=idempotency_key,
        )

    # -- concurrency hooks ---------------------------------------------------------------

    async def bump_case_version(self) -> int:
        """Move the durable case one version forward and answer the version it now holds.

        This is the controlled hook the concurrency tests share. It is a real conditional
        update against the very row an investigation apply guards on, committed through the
        same unit of work, so what it moves is durable state rather than an in-process flag.
        """

        current = await self.case()
        moved = current.version + 1
        await self.unit_of_work.commit(
            TransactionPlan(
                name="external-case-bump",
                operations=(
                    self.core.stage_update_case(
                        self.case_scope,
                        replace(
                            current,
                            version=moved,
                            updated_at=current.updated_at + timedelta(seconds=1),
                        ),
                        expected_version=current.version,
                    ),
                ),
                audit_required=False,
            )
        )
        return moved

    # -- reads ---------------------------------------------------------------------------

    async def case(self) -> CommunityCase:
        return await self.core.load_case(self.case_scope)

    async def facts(self) -> tuple[Fact, ...]:
        case = await self.case()
        return await self.core.load_facts(self.case_scope, case.fact_ids)

    async def status_of(self, label: str) -> EvidenceStatus:
        facts = await self.facts()
        for fact in facts:
            if fact.fact_id == self.fact_id(label):
                return fact.evidence_status
        raise AssertionError(f"no seeded fact labelled {label}")


@dataclass(slots=True)
class BumpsTheCaseMidFlight:
    """An Investigator that moves the case to N+1 between its answer and the apply.

    The invocation *is* the barrier, and it is the only place a barrier can honestly sit. By
    the time the port is called the application has already strongly read the case at version
    N, resolved its closure, projected the payload, and frozen the envelope; when the call
    returns, the apply transaction has not yet run. Bumping here therefore leaves a genuine
    answer *about version N* to be applied to a case that is no longer at version N -- which is
    exactly the read-to-write race the apply transaction's version condition exists to close,
    and which arranging the staleness before the command starts cannot reach at all.
    """

    inner: InvestigatorAgentPort
    harness: InvestigationHarness
    moved_to: int | None = None

    async def invoke_investigator(self, invocation: InvestigationInvocation) -> InvestigationResult:
        result = await self.inner.invoke_investigator(invocation)
        if self.moved_to is None:
            self.moved_to = await self.harness.bump_case_version()
        return result
