"""Deterministic persistence fixtures shared by the unit, contract, and property suites.

A ``World`` is one fully populated namespace/community/case/action coordinate. Building a
second world with a different seed or namespace produces structurally identical records that
belong to a *different* scope, which is exactly what the cross-case and cross-namespace denial
tests need.

Every identifier is derived with UUID5 from the world seed, so a failing test names the same
record on every run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid5

from chorus.domain.entities import (
    ActionClaim,
    ActionExecution,
    ActionExecutionState,
    ActionProposal,
    ActionProposalStatus,
    ActorType,
    ApplicationOperation,
    ApplicationOperationKind,
    ApplicationOperationStatus,
    Approval,
    ApprovalDecision,
    AssessmentAlternative,
    AuditDecision,
    AuditDetails,
    AuditEntityRef,
    AuditEvent,
    CaseState,
    Commitment,
    CommitmentStatus,
    Community,
    CommunityCase,
    CommunityMessage,
    CommunityStatus,
    Contributor,
    ContributorStatus,
    DerivationKind,
    DestinationKind,
    DisclosureScope,
    EvidenceFinding,
    EvidenceItem,
    EvidenceRoot,
    EvidenceStatus,
    ExtractionStatus,
    FactType,
    InvestigationAssessment,
    MalwareScanStatus,
    MandateStatus,
    MessageProcessingStatus,
    Purpose,
    SensitivityCategory,
)
from chorus.domain.facts import (
    Fact,
    FactStatus,
    FailureMode,
    IncidentOccurrence,
    LocationAreaCode,
    Report,
    ReportStatus,
)
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    AssessmentId,
    CaseId,
    CommitmentId,
    CommunityId,
    ContributorId,
    DestinationId,
    EvidenceItemId,
    EvidenceRootId,
    ExecutionId,
    ExportFactId,
    FactId,
    MandateId,
    MessageId,
    Namespace,
    OperationId,
    ReportId,
    SafeEvidenceRefId,
    SensitiveStr,
    Sha256Digest,
    ViewId,
)
from chorus.domain.mandates import (
    CurrentMandatePointer,
    DisclosureMandate,
    FactGrant,
    IdentityGrant,
)
from chorus.domain.time import epoch_seconds_ceiling
from chorus.infrastructure.dynamodb.audit import AuditRepository
from chorus.infrastructure.dynamodb.core import CoreRepository
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.idempotency import IdempotencyRepository
from chorus.infrastructure.dynamodb.shareable import ShareableRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotentCommand,
)
from chorus.ports.limits import ORDINARY_IDEMPOTENCY_TTL_SECONDS
from chorus.ports.records import (
    ActionHistoryLocator,
    AgentInvocationOutcome,
    AgentInvocationResult,
    AgentName,
    ChannelUniquenessLock,
    CompileDecisionOutcome,
    CompiledEvidenceRecord,
    CompiledFactRecord,
    CompileItemOutcome,
    CompilerAuditProjection,
    CompilerGateRecord,
    CurrentActionPointer,
    CurrentViewPointer,
    EvidenceRootLocator,
    FactMandateAssociation,
    FeedSignalProjection,
    MonitorApplyProgress,
    MonitorSnapshotChunk,
    MonitorSnapshotKind,
    MonitorSnapshotManifest,
    SendFence,
    StoredCurrentMandatePointer,
    StoredMandateVersionRef,
    StoredSafeDestination,
    StoredSafeEvidenceRef,
    StoredShareableFact,
    StoredShareableView,
    TransformationKind,
    ViewHistoryLocator,
)
from chorus.ports.retention import AuditRetention
from chorus.ports.scopes import (
    ActionScope,
    CaseScope,
    CommunityScope,
    NamespaceScope,
    OperationScope,
)
from chorus.ports.storage import (
    ItemKey,
    KeyAbsent,
    PutItem,
    StorageDriver,
    StoredItem,
    TableName,
)

FIXTURE_UUID_NAMESPACE = UUID("6b9b7f4e-2a71-5a2f-9a06-2a3f0dbf4a11")
NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


@dataclass(slots=True)
class MovableClock:
    """A deterministic clock a test can step forward explicitly.

    ``FixedClock`` cannot express "and then ten minutes passed", which adapter-window
    behaviour needs. Time still only moves when a test says so.
    """

    instant: datetime

    def now(self) -> datetime:
        return self.instant

    def advance(self, *, seconds: int) -> None:
        self.instant += timedelta(seconds=seconds)


DEMO_RETENTION = AuditRetention.demo()
"""The deployment retention Phase 2 targets; durable environments write no TTL at all."""
NAMESPACE = Namespace("TEST_PERSISTENCE")
OTHER_NAMESPACE = Namespace("TEST_PERSISTENCE_ALT")

CURSOR_SECRET = b"chorus-test-pagination-secret-000000"
OTHER_CURSOR_SECRET = b"chorus-test-pagination-secret-111111"

DESTINATION_ID = DestinationId("property_manager:demo")


def digest(value: str) -> Sha256Digest:
    """Build a canonical digest deterministically from a label."""

    return Sha256Digest(f"sha256:{sha256(value.encode()).hexdigest()}")


@dataclass(frozen=True, slots=True, kw_only=True)
class World:
    """One deterministic namespace/community/case/action coordinate."""

    seed: str = "primary"
    namespace: Namespace = NAMESPACE

    # -- identifiers -----------------------------------------------------------------

    def uuid(self, name: str) -> UUID:
        return uuid5(FIXTURE_UUID_NAMESPACE, f"{self.namespace.value}:{self.seed}:{name}")

    @property
    def community_id(self) -> CommunityId:
        return CommunityId(self.uuid("community"))

    @property
    def case_id(self) -> CaseId:
        return CaseId(self.uuid("case"))

    @property
    def action_id(self) -> ActionId:
        return ActionId(self.uuid("action"))

    @property
    def view_id(self) -> ViewId:
        return ViewId(self.uuid("view"))

    @property
    def contributor_id(self) -> ContributorId:
        return ContributorId(self.uuid("contributor"))

    @property
    def report_id(self) -> ReportId:
        return ReportId(self.uuid("report"))

    @property
    def fact_id(self) -> FactId:
        return FactId(self.uuid("fact"))

    @property
    def evidence_id(self) -> EvidenceItemId:
        return EvidenceItemId(self.uuid("evidence"))

    @property
    def evidence_root_id(self) -> EvidenceRootId:
        return EvidenceRootId(self.uuid("evidence-root"))

    @property
    def mandate_id(self) -> MandateId:
        return MandateId(self.uuid("mandate"))

    @property
    def approval_id(self) -> ApprovalId:
        return ApprovalId(self.uuid("approval"))

    @property
    def execution_id(self) -> ExecutionId:
        return ExecutionId(self.uuid("execution"))

    @property
    def commitment_id(self) -> CommitmentId:
        return CommitmentId(self.uuid("commitment"))

    @property
    def operation_id(self) -> OperationId:
        return OperationId(self.uuid("operation"))

    @property
    def message_id(self) -> MessageId:
        return MessageId(self.uuid("message"))

    @property
    def assessment_id(self) -> AssessmentId:
        return AssessmentId(self.uuid("assessment"))

    # -- scopes ----------------------------------------------------------------------

    @property
    def operation_scope(self) -> OperationScope:
        return OperationScope(namespace=self.namespace, operation_id=self.operation_id)

    @property
    def namespace_scope(self) -> NamespaceScope:
        return NamespaceScope(namespace=self.namespace)

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

    @property
    def action_scope(self) -> ActionScope:
        return ActionScope(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
            action_id=self.action_id,
        )

    # -- core entities ---------------------------------------------------------------

    def community(self, *, version: int = 1) -> Community:
        return Community(
            community_id=self.community_id,
            namespace=self.namespace,
            name="Example Community Building",
            timezone="UTC",
            status=CommunityStatus.ACTIVE,
            version=version,
            created_at=NOW - timedelta(days=10),
            updated_at=NOW - timedelta(days=10) + timedelta(minutes=version),
        )

    def contributor(self, *, version: int = 1) -> Contributor:
        return Contributor(
            contributor_id=self.contributor_id,
            community_id=self.community_id,
            namespace=self.namespace,
            pseudonym="resident-a",
            display_name=SensitiveStr("Resident A"),
            email=SensitiveStr("resident-a@example.invalid"),
            status=ContributorStatus.ACTIVE,
            version=version,
            created_at=NOW - timedelta(days=9),
            updated_at=NOW - timedelta(days=9) + timedelta(minutes=version),
        )

    def message(self, *, index: int = 0, version: int = 1) -> CommunityMessage:
        text = f"Synthetic ambient message {index}"
        sent_at = NOW - timedelta(hours=24 - index)
        return CommunityMessage(
            message_id=MessageId(self.uuid(f"message:{index}")),
            community_id=self.community_id,
            namespace=self.namespace,
            channel_message_id=f"synthetic-{self.seed}-{index}",
            contributor_id=self.contributor_id,
            sent_at=sent_at,
            received_at=sent_at + timedelta(seconds=1),
            raw_text=SensitiveStr(text),
            attachment_ids=(),
            content_sha256=digest(text),
            ingestion_idempotency_key=f"ingest-{self.seed}-{index}",
            processing_status=MessageProcessingStatus.NEW,
            version=version,
            created_at=sent_at,
            updated_at=sent_at + timedelta(minutes=version),
        )

    def channel_lock(self, *, index: int = 0) -> ChannelUniquenessLock:
        message = self.message(index=index)
        return ChannelUniquenessLock(
            namespace=self.namespace,
            community_id=self.community_id,
            adapter="SYNTHETIC",
            channel_message_id_sha256=digest(message.channel_message_id),
            message_id=message.message_id,
            content_sha256=message.content_sha256,
            created_at=NOW - timedelta(hours=2),
        )

    def feed_signal(self, *, index: int = 0) -> FeedSignalProjection:
        message = self.message(index=index)
        return FeedSignalProjection(
            namespace=self.namespace,
            community_id=self.community_id,
            message_id=message.message_id,
            case_id=self.case_id,
            case_version=2,
            label="Recurring elevator failures",
            related_message_count=6,
            case_state=CaseState.CANDIDATE,
            detected_at=NOW - timedelta(hours=1),
            version=1,
        )

    def monitor_progress(
        self, *, completed_steps: int = 1, version: int = 1
    ) -> MonitorApplyProgress:
        return MonitorApplyProgress(
            invocation_id=self.uuid("progress-invocation"),
            operation_id=self.operation_id,
            namespace=self.namespace,
            community_id=self.community_id,
            input_hash=digest(f"progress-input:{self.seed}"),
            output_hash=digest(f"progress-output:{self.seed}"),
            plan_hash=digest(f"progress-plan:{self.seed}"),
            completed_steps=completed_steps,
            total_steps=3,
            version=version,
            created_at=NOW - timedelta(minutes=4),
            updated_at=NOW - timedelta(minutes=3),
        )

    def monitor_snapshot_manifest(
        self, kind: MonitorSnapshotKind = MonitorSnapshotKind.MONITOR_PLAN
    ) -> MonitorSnapshotManifest:
        """The header of one immutable frozen-stage snapshot under an operation."""

        is_plan = kind is MonitorSnapshotKind.MONITOR_PLAN
        return MonitorSnapshotManifest(
            invocation_id=self.uuid("snapshot-invocation"),
            operation_id=self.operation_id,
            namespace=self.namespace,
            community_id=self.community_id,
            kind=kind,
            content_sha256=digest(f"snapshot-content:{self.seed}"),
            byte_length=2_048,
            chunk_count=1,
            input_hash=digest(f"snapshot-input:{self.seed}"),
            output_hash=digest(f"snapshot-output:{self.seed}") if is_plan else None,
            plan_hash=digest(f"snapshot-plan:{self.seed}") if is_plan else None,
            model_profile_hash=digest(f"snapshot-model:{self.seed}") if is_plan else None,
            provenance_hash=digest(f"snapshot-provenance:{self.seed}") if is_plan else None,
            prompt_version="monitor/v1",
            created_at=NOW - timedelta(minutes=5),
            expires_at_epoch=epoch_seconds_ceiling(NOW) + ORDINARY_IDEMPOTENCY_TTL_SECONDS,
        )

    def monitor_snapshot_chunk(
        self, kind: MonitorSnapshotKind = MonitorSnapshotKind.MONITOR_PLAN
    ) -> MonitorSnapshotChunk:
        """One ordered slice of a snapshot's canonical bytes."""

        return MonitorSnapshotChunk(
            invocation_id=self.uuid("snapshot-invocation"),
            operation_id=self.operation_id,
            namespace=self.namespace,
            community_id=self.community_id,
            kind=kind,
            index=0,
            content=SensitiveStr('{"schema":"monitor-validated-plan/v1"}'),
            expires_at_epoch=epoch_seconds_ceiling(NOW) + ORDINARY_IDEMPOTENCY_TTL_SECONDS,
        )

    def unlinked_agent_invocation(self) -> AgentInvocationResult:
        """A Monitor run that produced no case, recorded under its operation instead."""

        return AgentInvocationResult(
            invocation_id=self.uuid("unlinked-invocation"),
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=None,
            operation_id=self.operation_id,
            agent_name=AgentName.MONITOR,
            prompt_version="monitor/v1",
            input_hash=digest(f"unlinked-input:{self.seed}"),
            output_hash=None,
            model_profile_hash=digest(f"profile:{self.seed}"),
            outcome=AgentInvocationOutcome.FAILED,
            failure_code="AGENT_CONTRACT_VIOLATION",
            result_refs=(),
            created_at=NOW - timedelta(hours=5),
        )

    def operation(self, *, version: int = 1) -> ApplicationOperation:
        return ApplicationOperation(
            operation_id=self.operation_id,
            kind=ApplicationOperationKind.MONITOR,
            namespace=self.namespace,
            actor_id_hash=digest(f"actor:{self.seed}"),
            case_id=self.case_id,
            request_hash=digest(f"request:{self.seed}"),
            status=ApplicationOperationStatus.PENDING,
            result_refs=(),
            error_code=None,
            expires_at_epoch=int((NOW + timedelta(days=1)).timestamp()),
            version=version,
            created_at=NOW - timedelta(minutes=10),
            updated_at=NOW - timedelta(minutes=10) + timedelta(minutes=version),
            agent_invocation_id=self.uuid("monitor-invocation"),
            agent_binding_hash=digest(f"monitor-locators:{self.seed}"),
        )

    def evidence_root(self) -> EvidenceRoot:
        return EvidenceRoot(
            root_id=self.evidence_root_id,
            community_id=self.community_id,
            namespace=self.namespace,
            root_sha256=digest(f"root:{self.seed}"),
            media_type="image/jpeg",
            first_observed_at=NOW - timedelta(days=2),
            derivation_kind=DerivationKind.ORIGINAL,
            parent_root_id=None,
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2),
        )

    def case(
        self, *, version: int = 1, fact_ids: tuple[FactId, ...] | None = None
    ) -> CommunityCase:
        return CommunityCase(
            case_id=self.case_id,
            community_id=self.community_id,
            namespace=self.namespace,
            title="Recurring elevator failures",
            issue_type="ELEVATOR_FAILURE",
            state=CaseState.READY_FOR_ACTION,
            report_ids=(self.report_id,),
            fact_ids=(self.fact_id,) if fact_ids is None else fact_ids,
            assessment_id=None,
            current_view_id=None,
            current_action_id=None,
            corroboration_source_count=4,
            state_reason_code="EVIDENCE_SUFFICIENT",
            version=version,
            created_at=NOW - timedelta(days=3),
            updated_at=NOW - timedelta(days=3) + timedelta(minutes=version),
        )

    def report(self, *, version: int = 1) -> Report:
        return Report(
            report_id=self.report_id,
            case_id=self.case_id,
            community_id=self.community_id,
            contributor_id=self.contributor_id,
            namespace=self.namespace,
            source_message_ids=(self.message_id,),
            issue_type="ELEVATOR_FAILURE",
            private_summary=SensitiveStr("Synthetic private report"),
            occurred_at=NOW - timedelta(days=1),
            location_area=LocationAreaCode.ELEVATOR_CAB,
            evidence_ids=(self.evidence_id,),
            status=ReportStatus.ACTIVE,
            duplicate_of_report_id=None,
            version=version,
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2) + timedelta(minutes=version),
        )

    def fact(self, *, fact_id: FactId | None = None, version: int = 1) -> Fact:
        return Fact(
            fact_id=self.fact_id if fact_id is None else fact_id,
            case_id=self.case_id,
            report_id=self.report_id,
            community_id=self.community_id,
            contributor_id=self.contributor_id,
            namespace=self.namespace,
            fact_type=FactType.INCIDENT_OCCURRENCE,
            value=IncidentOccurrence(
                occurred_at=NOW - timedelta(days=1),
                failure_mode=FailureMode.OUT_OF_SERVICE,
            ),
            sensitivity=SensitivityCategory.GENERAL,
            evidence_ids=(),
            evidence_status=EvidenceStatus.CORROBORATED,
            source_message_ids=(self.message_id,),
            supersedes_fact_id=None,
            status=FactStatus.ACTIVE,
            version=version,
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2) + timedelta(minutes=version),
        )

    def evidence_root_locator(self) -> EvidenceRootLocator:
        return EvidenceRootLocator(
            namespace=self.namespace,
            community_id=self.community_id,
            root_id=self.evidence_root_id,
            root_sha256=digest(f"root:{self.seed}"),
            created_at=NOW - timedelta(days=1),
        )

    def evidence_item(self, *, version: int = 1) -> EvidenceItem:
        return EvidenceItem(
            evidence_id=self.evidence_id,
            root_id=self.evidence_root_id,
            community_id=self.community_id,
            case_id=self.case_id,
            namespace=self.namespace,
            submitted_by_contributor_id=self.contributor_id,
            source_message_id=None,
            private_object_key=SensitiveStr(f"private/{self.evidence_id}"),
            media_type="image/jpeg",
            byte_length=2048,
            sha256=digest(f"evidence:{self.seed}"),
            captured_at=NOW - timedelta(days=1),
            uploaded_at=NOW - timedelta(days=1),
            derived_from_evidence_id=None,
            malware_scan_status=MalwareScanStatus.CLEAN,
            extraction_status=ExtractionStatus.NOT_NEEDED,
            extracted_text=None,
            version=version,
            created_at=NOW - timedelta(days=1),
            updated_at=NOW - timedelta(days=1) + timedelta(minutes=version),
        )

    def assessment(self, *, index: int = 0) -> InvestigationAssessment:
        return InvestigationAssessment(
            assessment_id=AssessmentId(self.uuid(f"assessment:{index}")),
            case_id=self.case_id,
            based_on_case_version=1,
            agent_invocation_id=self.uuid(f"invocation:{index}"),
            linkage_decision="LINKED_TO_EXISTING_CASE",
            findings=(
                EvidenceFinding(
                    fact_id=self.fact_id,
                    evidence_status=EvidenceStatus.CORROBORATED,
                    reason_code="MULTIPLE_INDEPENDENT_SOURCES",
                ),
            ),
            contradictions=(),
            alternative_explanations=(
                AssessmentAlternative(
                    description="Scheduled maintenance",
                    cited_report_ids=(self.report_id,),
                    cited_fact_ids=(),
                    cited_evidence_ids=(),
                ),
            ),
            independent_source_count=4,
            is_corroborated=True,
            recommended_disposition="READY_FOR_ACTION",
            assessment_hash=digest(f"assessment:{self.seed}:{index}"),
            created_at=NOW - timedelta(hours=6 - index),
        )

    # -- mandates --------------------------------------------------------------------

    def mandate(
        self, *, version: int = 1, status: MandateStatus = MandateStatus.APPROVED
    ) -> DisclosureMandate:
        valid_from = NOW - timedelta(days=2)
        return DisclosureMandate(
            mandate_id=self.mandate_id,
            version=version,
            case_id=self.case_id,
            community_id=self.community_id,
            contributor_id=self.contributor_id,
            namespace=self.namespace,
            status=status,
            fact_grants=(
                FactGrant(
                    fact_id=self.fact_id,
                    max_scope=DisclosureScope.EXTERNAL_ACTION,
                    allow_safe_transformation=True,
                ),
            ),
            identity_grant=IdentityGrant(
                externally_shareable=False,
                max_scope=DisclosureScope.ANONYMOUS_CASE,
            ),
            allowed_destination_ids=(DESTINATION_ID,),
            allowed_purposes=(Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,),
            valid_from=valid_from,
            expires_at=NOW + timedelta(days=2),
            proposed_at=valid_from,
            decided_at=valid_from + timedelta(minutes=5),
            revoked_at=NOW - timedelta(hours=1) if status is MandateStatus.REVOKED else None,
            decision_actor_id=self.contributor_id,
            supersedes_version=None if version == 1 else version - 1,
            terms_hash=digest(f"terms:{self.seed}:{version}"),
            created_at=valid_from,
            updated_at=valid_from + timedelta(minutes=5),
        )

    def mandate_pointer(
        self,
        *,
        mandate_version: int = 1,
        row_version: int = 1,
        status: MandateStatus = MandateStatus.APPROVED,
    ) -> StoredCurrentMandatePointer:
        return StoredCurrentMandatePointer(
            namespace=self.namespace,
            community_id=self.community_id,
            pointer=CurrentMandatePointer(
                mandate_id=self.mandate_id,
                version=mandate_version,
                case_id=self.case_id,
                contributor_id=self.contributor_id,
                terms_hash=digest(f"terms:{self.seed}:{mandate_version}"),
            ),
            status=status,
            version=row_version,
            created_at=NOW - timedelta(days=2),
            updated_at=NOW - timedelta(days=2) + timedelta(minutes=row_version),
        )

    def fact_mandate(self) -> FactMandateAssociation:
        return FactMandateAssociation(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
            fact_id=self.fact_id,
            mandate_id=self.mandate_id,
            mandate_version=1,
            terms_hash=digest(f"terms:{self.seed}:1"),
            contributor_id=self.contributor_id,
            created_at=NOW - timedelta(days=1),
        )

    # -- agent invocation and send fence ---------------------------------------------

    def agent_invocation(self, *, index: int = 0) -> AgentInvocationResult:
        return AgentInvocationResult(
            invocation_id=self.uuid(f"invocation:{index}"),
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
            agent_name=AgentName.INVESTIGATOR,
            prompt_version="investigator/v1",
            input_hash=digest(f"agent-input:{self.seed}:{index}"),
            output_hash=digest(f"agent-output:{self.seed}:{index}"),
            outcome=AgentInvocationOutcome.SUCCEEDED,
            result_refs=(
                EntityRef(
                    entity_type="INVESTIGATION_ASSESSMENT", entity_id=self.uuid("assessment")
                ),
            ),
            created_at=NOW - timedelta(hours=5),
        )

    def send_fence(
        self,
        *,
        execution_id: ExecutionId | None = None,
        acquired_at: datetime | None = None,
        lifetime: timedelta = timedelta(minutes=5),
    ) -> SendFence:
        acquired = NOW if acquired_at is None else acquired_at
        return SendFence(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
            execution_id=self.execution_id if execution_id is None else execution_id,
            action_id=self.action_id,
            approval_id=self.approval_id,
            view_id=self.view_id,
            authorization_snapshot_hash=digest(f"snapshot:{self.seed}"),
            acquired_at=acquired,
            expires_at=acquired + lifetime,
        )

    # -- shareable records -----------------------------------------------------------

    def destination(self) -> StoredSafeDestination:
        return StoredSafeDestination(
            destination_id=DESTINATION_ID,
            kind=DestinationKind.PROPERTY_MANAGER,
            registry_version=1,
            routing_token=self.uuid("routing-token"),
            display_label="Property Management",
        )

    def view(self, *, view_id: ViewId | None = None, index: int = 0) -> StoredShareableView:
        identity = self.view_id if view_id is None else view_id
        ref_id = SafeEvidenceRefId(self.uuid(f"safe-evidence:{index}"))
        return StoredShareableView(
            schema_version="shareable-case-view/v1",
            view_id=identity,
            case_id=self.case_id,
            community_public_label="Example Community Building",
            case_version=1,
            policy_version="policy/v1",
            compiler_version="compiler/v1",
            destination=self.destination(),
            purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
            generated_at=NOW - timedelta(hours=12 - index),
            expires_at=NOW + timedelta(days=1),
            mandate_version_set=(
                StoredMandateVersionRef(
                    mandate_id=self.uuid("mandate"),
                    version=1,
                    terms_hash=digest(f"terms:{self.seed}:1"),
                ),
            ),
            authorization_snapshot_hash=digest(f"snapshot:{self.seed}"),
            shareable_facts=(
                StoredShareableFact(
                    export_fact_id=ExportFactId(self.uuid(f"export-fact:{index}")),
                    fact_type=FactType.INCIDENT_OCCURRENCE,
                    safe_text="The elevator was out of service on four separate days.",
                    effective_scope=DisclosureScope.EXTERNAL_ACTION,
                    evidence_status=EvidenceStatus.CORROBORATED,
                    contributor_count=4,
                    transformation=TransformationKind.AGGREGATED,
                    transformation_rule_id="aggregate-incidents/v1",
                    safe_evidence_ref_ids=(ref_id,),
                    content_hash=digest(f"safe-fact:{self.seed}:{index}"),
                ),
            ),
            safe_evidence_refs=(
                StoredSafeEvidenceRef(
                    safe_evidence_ref_id=ref_id,
                    media_type="image/png",
                    export_handle_id=self.uuid(f"export-handle:{index}"),
                    sha256=digest(f"derivative:{self.seed}:{index}"),
                    caption="A reviewed elevator out-of-service photo is available.",
                    created_by_rule_id="evidence-derivative/v1",
                    content_hash=digest(f"safe-evidence:{self.seed}:{index}"),
                ),
            ),
            audit_refs=(self.uuid(f"audit-ref:{index}"),),
            view_hash=digest(f"view:{self.seed}:{index}"),
        )

    def compile_projection(
        self, *, decision: CompileDecisionOutcome = CompileDecisionOutcome.ALLOW
    ) -> CompilerAuditProjection:
        """One compile's private lineage, in both the allowed and the denied shape."""

        allowed = decision is CompileDecisionOutcome.ALLOW
        item_outcome = CompileItemOutcome.INCLUDED if allowed else CompileItemOutcome.EXCLUDED
        return CompilerAuditProjection(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
            compile_id=self.uuid("compile"),
            audit_event_id=self.uuid("compile-audit-event"),
            requested_at=NOW,
            created_at=NOW,
            based_on_case_version=1,
            compiler_version="compiler/1.1.0",
            policy_version="policy/v1",
            destination_id=DESTINATION_ID,
            destination_registry_version=1,
            destination_routing_token=self.uuid("routing-token"),
            purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
            decision=decision,
            reason_codes=() if allowed else ("STALE_CASE_VERSION",),
            gates=(
                CompilerGateRecord(
                    gate=1,
                    gate_name="REQUEST_SCHEMA",
                    outcome="PASSED",
                ),
                CompilerGateRecord(
                    gate=14,
                    gate_name="DISCLOSURE_SCOPE",
                    outcome="EXCLUDED",
                    reason_codes=("INTERNAL_ONLY",),
                ),
            ),
            facts=(
                CompiledFactRecord(
                    fact_id=self.fact_id,
                    necessity="OPTIONAL",
                    intended_usage="CLAIM",
                    granted_scope=DisclosureScope.ANONYMOUS_CASE,
                    outcome=item_outcome,
                    reason_codes=() if allowed else ("INTERNAL_ONLY",),
                    export_fact_ids=(
                        (ExportFactId(self.uuid("export-fact:0")),) if allowed else ()
                    ),
                    transformation_rule_id="p1.incident.anonymous.v1" if allowed else None,
                ),
            ),
            evidence=(
                CompiledEvidenceRecord(
                    source_evidence_id=self.evidence_id,
                    outcome=item_outcome,
                    reason_codes=() if allowed else ("UNSAFE_EVIDENCE",),
                    safe_evidence_ref_id=(
                        SafeEvidenceRefId(self.uuid("safe-evidence:0")) if allowed else None
                    ),
                    export_handle_id=self.uuid("export-handle:0") if allowed else None,
                    derivative_sha256=(digest(f"derivative:{self.seed}:0") if allowed else None),
                ),
            ),
            view_id=self.view_id if allowed else None,
            view_hash=digest(f"view:{self.seed}:0") if allowed else None,
        )

    def view_pointer(self, *, version: int = 1, index: int = 0) -> CurrentViewPointer:
        return CurrentViewPointer(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
            view_id=self.view_id,
            view_hash=digest(f"view:{self.seed}:{index}"),
            case_version=1,
            expires_at=NOW + timedelta(days=1),
            version=version,
            created_at=NOW - timedelta(hours=12),
            updated_at=NOW - timedelta(hours=12) + timedelta(minutes=version),
        )

    def view_history(self, *, index: int = 0) -> ViewHistoryLocator:
        return ViewHistoryLocator(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
            view_id=ViewId(self.uuid(f"view:{index}")),
            view_hash=digest(f"view:{self.seed}:{index}"),
            case_version=1,
            generated_at=NOW - timedelta(hours=24 - index),
        )

    def proposal(self, *, index: int = 0) -> ActionProposal:
        export_fact_id = self.uuid(f"export-fact:{index}")
        return ActionProposal(
            action_id=self.action_id,
            case_id=self.case_id,
            case_version=1,
            view_id=self.view_id,
            view_hash=digest(f"view:{self.seed}:{index}"),
            subject="Repeated elevator outages at Example Community Building",
            claims=(
                ActionClaim(
                    claim_id=self.uuid(f"claim:{index}"),
                    text="The elevator was out of service on four separate days.",
                    export_fact_ids=(export_fact_id,),
                    claim_hash=digest(f"claim:{self.seed}:{index}"),
                ),
            ),
            requested_action="Inspect and repair the elevator, then confirm the schedule.",
            requested_deadline=NOW + timedelta(days=7),
            request_fact_ids=(export_fact_id,),
            caveats=("Reported by residents; not independently inspected.",),
            tone="NEUTRAL",
            agent_invocation_id=self.uuid(f"invocation:action:{index}"),
            prompt_version="action/v1",
            proposal_hash=digest(f"proposal:{self.seed}:{index}"),
            status=ActionProposalStatus.DRAFT,
            created_at=NOW - timedelta(hours=4 - index),
        )

    def approval(self, *, version: int = 1, consumed: bool = False) -> Approval:
        approved_at = NOW - timedelta(hours=3)
        return Approval(
            approval_id=self.approval_id,
            action_id=self.action_id,
            case_id=self.case_id,
            proposal_hash=digest(f"proposal:{self.seed}:0"),
            view_hash=digest(f"view:{self.seed}:0"),
            approver_id=self.contributor_id,
            decision=ApprovalDecision.APPROVED,
            approved_at=approved_at,
            expires_at=approved_at + timedelta(hours=24),
            consumed_at=approved_at + timedelta(minutes=5) if consumed else None,
            approval_hash=digest(f"approval:{self.seed}"),
            idempotency_key=f"approve-{self.seed}",
            version=version,
            created_at=approved_at,
            updated_at=approved_at + timedelta(minutes=version),
        )

    def execution(
        self,
        *,
        state: ActionExecutionState = ActionExecutionState.APPROVED,
        version: int = 1,
    ) -> ActionExecution:
        started = NOW - timedelta(hours=2)
        return ActionExecution(
            execution_id=self.execution_id,
            action_id=self.action_id,
            case_id=self.case_id,
            approval_id=self.approval_id,
            proposal_hash=digest(f"proposal:{self.seed}:0"),
            view_hash=digest(f"view:{self.seed}:0"),
            idempotency_key=f"send-{self.seed}",
            state=state,
            rendered_message_hash=digest(f"rendered:{self.seed}"),
            ses_request_token_hash=digest(f"ses-token:{self.seed}"),
            ses_message_id=None,
            started_at=started,
            finished_at=None,
            failure_code=None,
            failure_detail_safe=None,
            reconciled_at=None,
            version=version,
            created_at=started,
            updated_at=started + timedelta(minutes=version),
        )

    def action_pointer(self, *, version: int = 1, index: int = 0) -> CurrentActionPointer:
        return CurrentActionPointer(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
            action_id=self.action_id,
            proposal_hash=digest(f"proposal:{self.seed}:{index}"),
            view_id=self.view_id,
            view_hash=digest(f"view:{self.seed}:{index}"),
            case_version=1,
            status=ActionProposalStatus.DRAFT,
            version=version,
            created_at=NOW - timedelta(hours=4),
            updated_at=NOW - timedelta(hours=4) + timedelta(minutes=version),
        )

    def action_history(self, *, index: int = 0) -> ActionHistoryLocator:
        return ActionHistoryLocator(
            namespace=self.namespace,
            community_id=self.community_id,
            case_id=self.case_id,
            action_id=ActionId(self.uuid(f"action:{index}")),
            proposal_hash=digest(f"proposal:{self.seed}:{index}"),
            created_at=NOW - timedelta(hours=24 - index),
        )

    def commitment(
        self,
        *,
        commitment_id: CommitmentId | None = None,
        version: int = 1,
        index: int = 0,
    ) -> Commitment:
        return Commitment(
            commitment_id=self.commitment_id if commitment_id is None else commitment_id,
            case_id=self.case_id,
            action_id=self.action_id,
            source_evidence_id=self.evidence_id,
            obligor="Property Management",
            action_text="Inspect and repair the elevator",
            due_at=NOW + timedelta(days=3 + index),
            verification_method="Residents confirm normal elevator operation",
            status=CommitmentStatus.PENDING,
            scheduler_name=f"chorus-test-commitment-{index}",
            schedule_generation=1,
            due_event_id=self.uuid(f"due-event:{index}"),
            verified_by_contributor_id=None,
            verification_evidence_id=None,
            outcome_note=None,
            version=version,
            created_at=NOW - timedelta(hours=1),
            updated_at=NOW - timedelta(hours=1) + timedelta(minutes=version),
        )

    # -- audit -----------------------------------------------------------------------

    def audit_event(
        self,
        *,
        index: int = 0,
        case_scoped: bool = True,
        event_type: str = "CASE_STATE_CHANGED",
    ) -> AuditEvent:
        return AuditEvent(
            audit_event_id=self.uuid(f"audit-event:{case_scoped}:{index}"),
            namespace=self.namespace,
            community_id=self.community_id if case_scoped else None,
            case_id=self.case_id if case_scoped else None,
            actor_type=ActorType.SYSTEM,
            actor_id_hash=digest(f"actor:{self.seed}"),
            event_type=event_type,
            occurred_at=NOW - timedelta(minutes=60 - index),
            correlation_id=self.uuid(f"correlation:{index}"),
            causation_id=None,
            idempotency_key_hash=None,
            entity_refs=(
                AuditEntityRef(
                    entity_type="COMMUNITY_CASE",
                    entity_id=self.uuid("case"),
                    version=1,
                ),
            ),
            decision=AuditDecision.ALLOW,
            reason_codes=("STATE_TRANSITION_ALLOWED",),
            safe_details=AuditDetails(count=1, rule_id="case-state/v1"),
            input_hash=digest(f"audit-input:{self.seed}:{index}"),
            output_hash=None,
        )

    # -- idempotency -----------------------------------------------------------------

    def idempotency_key(
        self,
        *,
        command: IdempotentCommand = IdempotentCommand.APPLY_INVESTIGATION,
        kind: IdempotencyPartitionKind = IdempotencyPartitionKind.CASE,
        key_seed: str = "default",
    ) -> IdempotencyKey:
        partition = IdempotencyPartition(
            kind=kind,
            namespace=self.namespace,
            community_id=self.community_id if kind is IdempotencyPartitionKind.COMMUNITY else None,
            case_id=self.case_id if kind is IdempotencyPartitionKind.CASE else None,
            action_id=self.action_id if kind is IdempotencyPartitionKind.ACTION else None,
        )
        return IdempotencyKey(
            partition=partition,
            command=command,
            actor_id_hash=digest(f"actor:{self.seed}"),
            key_hash=digest(f"idempotency:{self.seed}:{key_seed}"),
        )


PRIMARY = World()
OTHER_CASE = World(seed="secondary")
OTHER_NAMESPACE_WORLD = World(seed="primary", namespace=OTHER_NAMESPACE)


@dataclass(frozen=True, slots=True, kw_only=True)
class Repositories:
    """Every repository bound to one driver, exactly as a composition root would build it."""

    driver: StorageDriver
    cursors: SignedCursorCodec
    core: CoreRepository
    shareable: ShareableRepository
    audit: AuditRepository
    idempotency: IdempotencyRepository
    unit_of_work: StorageUnitOfWork


def build_repositories(
    driver: StorageDriver,
    *,
    secret: bytes = CURSOR_SECRET,
    audit_retention: AuditRetention = DEMO_RETENTION,
) -> Repositories:
    """Compose the Phase 2 repositories over one storage driver.

    Retention defaults to the demo policy because that is the deployment the frozen plan
    targets; a durable-environment test passes ``AuditRetention.durable()`` explicitly.
    """

    cursors = SignedCursorCodec(secret)
    return Repositories(
        driver=driver,
        cursors=cursors,
        core=CoreRepository(driver=driver, cursors=cursors),
        shareable=ShareableRepository(driver=driver, cursors=cursors),
        audit=AuditRepository(driver=driver, cursors=cursors, retention=audit_retention),
        idempotency=IdempotencyRepository(driver=driver, table=TableName.CORE),
        unit_of_work=StorageUnitOfWork(driver=driver),
    )


def relocated(item: StoredItem, key: ItemKey) -> PutItem:
    """Place a foreign record at an address the caller is entitled to read.

    A real attacker does not politely store their row where nobody looks; this models the
    dangerous case where the key looks right and only the body betrays the wrong scope. The
    key attributes are rewritten because DynamoDB derives an item's address from them.
    """

    return PutItem(
        key=key,
        item={**item, "PK": key.partition_key, "SK": key.sort_key},
        condition=KeyAbsent(),
    )
