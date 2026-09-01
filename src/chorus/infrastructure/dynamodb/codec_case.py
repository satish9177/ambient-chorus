"""Explicit Core-table item mappings for case-owned private entities."""

from __future__ import annotations

from typing import Final

from chorus.domain.entities import (
    EvidenceFinding,
    EvidenceItem,
    EvidenceStatus,
    ExtractionStatus,
    FactType,
    InvestigationAssessment,
    MalwareScanStatus,
    SensitivityCategory,
)
from chorus.domain.facts import (
    CommitmentTerm,
    Contradiction,
    EvidenceDescription,
    EvidenceMediaKind,
    Fact,
    FactStatus,
    FactValue,
    FailureMode,
    HealthDetail,
    IdentityAttribute,
    ImpactCode,
    IncidentOccurrence,
    LocationArea,
    LocationAreaCode,
    ManagementStatement,
    Report,
    ReportStatus,
    ServiceImpact,
    SubjectRelation,
    UnitLocation,
)
from chorus.domain.ids import (
    AssessmentId,
    ContributorId,
    EvidenceItemId,
    EvidenceRootId,
    FactId,
    MessageId,
    ReportId,
)
from chorus.infrastructure.dynamodb import keys
from chorus.infrastructure.dynamodb.codec import (
    DecodedScope,
    EntityType,
    ItemReader,
    build_entity,
    envelope,
    identifier,
    identifiers,
    instant,
    optional_identifier,
    optional_instant,
    optional_sensitive,
    read_envelope,
    sensitive,
)
from chorus.infrastructure.dynamodb.codec_core import build_entity_error
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import ItemKey, StoredItem, StoredValue, TableName

_CORE: Final = TableName.CORE

REPORT_SCHEMA_VERSIONS: Final = frozenset({"report/v1"})
FACT_SCHEMA_VERSIONS: Final = frozenset({"fact/v1"})
EVIDENCE_ITEM_SCHEMA_VERSIONS: Final = frozenset({"evidence-item/v1"})
ASSESSMENT_SCHEMA_VERSIONS: Final = frozenset({"investigation-assessment/v1"})


def report_key(scope: CaseScope, report_id: ReportId) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.case_partition(scope.namespace, scope.case_id),
        sort_key=keys.report_sort_key(report_id),
    )


def encode_report(scope: CaseScope, report: Report) -> StoredItem:
    key = report_key(scope, report.report_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.REPORT,
        schema_version=report.schema_version,
        key=key,
        namespace=report.namespace,
        community_id=report.community_id,
        case_id=report.case_id,
    )
    item.update(
        {
            "report_id": identifier(report.report_id),
            "contributor_id": identifier(report.contributor_id),
            "source_message_ids": identifiers(report.source_message_ids),
            "issue_type": report.issue_type,
            "private_summary": sensitive(report.private_summary),
            "occurred_at": optional_instant(report.occurred_at),
            "location_area": None if report.location_area is None else report.location_area.value,
            "evidence_ids": identifiers(report.evidence_ids),
            "status": report.status.value,
            "duplicate_of_report_id": optional_identifier(report.duplicate_of_report_id),
            "version": report.version,
            "created_at": instant(report.created_at),
            "updated_at": instant(report.updated_at),
        }
    )
    return item


def decode_report(item: StoredItem) -> tuple[DecodedScope, Report]:
    reader = ItemReader(item, entity_ref="REPORT")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.REPORT,
        accepted_schema_versions=REPORT_SCHEMA_VERSIONS,
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    location_value = reader.optional_text("location_area")
    try:
        location_area = None if location_value is None else LocationAreaCode(location_value)
    except ValueError as error:
        raise build_entity_error(reader, "location_area") from error
    report = build_entity(
        reader.entity_ref,
        Report,
        report_id=reader.identifier("report_id", ReportId),
        case_id=scope.case_id,
        community_id=scope.community_id,
        contributor_id=reader.identifier("contributor_id", ContributorId),
        namespace=scope.namespace,
        source_message_ids=reader.identifiers("source_message_ids", MessageId),
        issue_type=reader.text("issue_type"),
        private_summary=reader.sensitive("private_summary"),
        occurred_at=reader.optional_instant("occurred_at"),
        location_area=location_area,
        evidence_ids=reader.identifiers("evidence_ids", EvidenceItemId),
        status=reader.enum("status", ReportStatus),
        duplicate_of_report_id=reader.optional_identifier("duplicate_of_report_id", ReportId),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, report


def _encode_fact_value(value: FactValue) -> dict[str, StoredValue]:
    match value:
        case IncidentOccurrence():
            return {
                "kind": FactType.INCIDENT_OCCURRENCE.value,
                "occurred_at": instant(value.occurred_at),
                "failure_mode": value.failure_mode.value,
                "equipment": value.equipment,
            }
        case ServiceImpact():
            return {
                "kind": FactType.SERVICE_IMPACT.value,
                "impact_code": value.impact_code.value,
                "summary": value.summary,
            }
        case LocationArea():
            return {"kind": FactType.LOCATION_AREA.value, "area": value.area.value}
        case IdentityAttribute():
            return {
                "kind": FactType.IDENTITY_ATTRIBUTE.value,
                "display_name": value.display_name,
            }
        case UnitLocation():
            return {"kind": FactType.UNIT_LOCATION.value, "unit_label": value.unit_label}
        case HealthDetail():
            return {
                "kind": FactType.HEALTH_DETAIL.value,
                "subject_relation": value.subject_relation.value,
                "detail": value.detail,
            }
        case ManagementStatement():
            return {
                "kind": FactType.MANAGEMENT_STATEMENT.value,
                "statement": value.statement,
                "speaker_org": value.speaker_org,
                "stated_at": instant(value.stated_at),
            }
        case Contradiction():
            return {
                "kind": FactType.CONTRADICTION.value,
                "statement_fact_ids": identifiers(value.statement_fact_ids),
                "summary": value.summary,
            }
        case CommitmentTerm():
            return {
                "kind": FactType.COMMITMENT_TERM.value,
                "obligor": value.obligor,
                "action_text": value.action_text,
                "due_at": instant(value.due_at),
                "verification_method": value.verification_method,
            }
        case EvidenceDescription():
            return {
                "kind": FactType.EVIDENCE_DESCRIPTION.value,
                "description": value.description,
                "media_kind": value.media_kind.value,
            }
        case _:  # pragma: no cover - the union above is closed and exhaustive
            raise AssertionError("unreachable fact value variant")


def _decode_fact_value(reader: ItemReader) -> FactValue:
    kind = reader.enum("kind", FactType)
    value: FactValue
    match kind:
        case FactType.INCIDENT_OCCURRENCE:
            value = build_entity(
                reader.entity_ref,
                IncidentOccurrence,
                occurred_at=reader.instant("occurred_at"),
                failure_mode=reader.enum("failure_mode", FailureMode),
                equipment=reader.text("equipment"),
            )
        case FactType.SERVICE_IMPACT:
            value = build_entity(
                reader.entity_ref,
                ServiceImpact,
                impact_code=reader.enum("impact_code", ImpactCode),
                summary=reader.text("summary"),
            )
        case FactType.LOCATION_AREA:
            value = build_entity(
                reader.entity_ref, LocationArea, area=reader.enum("area", LocationAreaCode)
            )
        case FactType.IDENTITY_ATTRIBUTE:
            value = build_entity(
                reader.entity_ref, IdentityAttribute, display_name=reader.text("display_name")
            )
        case FactType.UNIT_LOCATION:
            value = build_entity(
                reader.entity_ref, UnitLocation, unit_label=reader.text("unit_label")
            )
        case FactType.HEALTH_DETAIL:
            value = build_entity(
                reader.entity_ref,
                HealthDetail,
                subject_relation=reader.enum("subject_relation", SubjectRelation),
                detail=reader.text("detail"),
            )
        case FactType.MANAGEMENT_STATEMENT:
            value = build_entity(
                reader.entity_ref,
                ManagementStatement,
                statement=reader.text("statement"),
                speaker_org=reader.text("speaker_org"),
                stated_at=reader.instant("stated_at"),
            )
        case FactType.CONTRADICTION:
            value = build_entity(
                reader.entity_ref,
                Contradiction,
                statement_fact_ids=reader.identifiers("statement_fact_ids", FactId),
                summary=reader.text("summary"),
            )
        case FactType.COMMITMENT_TERM:
            value = build_entity(
                reader.entity_ref,
                CommitmentTerm,
                obligor=reader.text("obligor"),
                action_text=reader.text("action_text"),
                due_at=reader.instant("due_at"),
                verification_method=reader.text("verification_method"),
            )
        case FactType.EVIDENCE_DESCRIPTION:
            value = build_entity(
                reader.entity_ref,
                EvidenceDescription,
                description=reader.text("description"),
                media_kind=reader.enum("media_kind", EvidenceMediaKind),
            )
        case _:  # pragma: no cover - StrEnum membership is validated above
            raise AssertionError("unreachable fact value discriminator")
    reader.finish()
    return value


def fact_key(scope: CaseScope, fact_id: FactId) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.case_partition(scope.namespace, scope.case_id),
        sort_key=keys.fact_sort_key(fact_id),
    )


def encode_fact(scope: CaseScope, fact: Fact) -> StoredItem:
    key = fact_key(scope, fact.fact_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.FACT,
        schema_version=fact.schema_version,
        key=key,
        namespace=fact.namespace,
        community_id=fact.community_id,
        case_id=fact.case_id,
    )
    item.update(
        {
            "fact_id": identifier(fact.fact_id),
            "report_id": identifier(fact.report_id),
            "contributor_id": identifier(fact.contributor_id),
            "fact_type": fact.fact_type.value,
            "value": _encode_fact_value(fact.value),
            "sensitivity": fact.sensitivity.value,
            "evidence_ids": identifiers(fact.evidence_ids),
            "evidence_status": fact.evidence_status.value,
            "source_message_ids": identifiers(fact.source_message_ids),
            "supersedes_fact_id": optional_identifier(fact.supersedes_fact_id),
            "status": fact.status.value,
            "version": fact.version,
            "created_at": instant(fact.created_at),
            "updated_at": instant(fact.updated_at),
        }
    )
    return item


def decode_fact(item: StoredItem) -> tuple[DecodedScope, Fact]:
    reader = ItemReader(item, entity_ref="FACT")
    scope, schema_version = read_envelope(
        reader, expected_type=EntityType.FACT, accepted_schema_versions=FACT_SCHEMA_VERSIONS
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    value_reader = reader.child(reader.mapping("value"), "value")
    fact = build_entity(
        reader.entity_ref,
        Fact,
        fact_id=reader.identifier("fact_id", FactId),
        case_id=scope.case_id,
        report_id=reader.identifier("report_id", ReportId),
        community_id=scope.community_id,
        contributor_id=reader.identifier("contributor_id", ContributorId),
        namespace=scope.namespace,
        fact_type=reader.enum("fact_type", FactType),
        value=_decode_fact_value(value_reader),
        sensitivity=reader.enum("sensitivity", SensitivityCategory),
        evidence_ids=reader.identifiers("evidence_ids", EvidenceItemId),
        evidence_status=reader.enum("evidence_status", EvidenceStatus),
        source_message_ids=reader.identifiers("source_message_ids", MessageId),
        supersedes_fact_id=reader.optional_identifier("supersedes_fact_id", FactId),
        status=reader.enum("status", FactStatus),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, fact


def evidence_item_key(scope: CaseScope, evidence_id: EvidenceItemId) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.case_partition(scope.namespace, scope.case_id),
        sort_key=keys.evidence_item_sort_key(evidence_id),
    )


def encode_evidence_item(scope: CaseScope, evidence: EvidenceItem) -> StoredItem:
    key = evidence_item_key(scope, evidence.evidence_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.EVIDENCE_ITEM,
        schema_version=evidence.schema_version,
        key=key,
        namespace=evidence.namespace,
        community_id=evidence.community_id,
        case_id=evidence.case_id,
    )
    item.update(
        {
            "evidence_id": identifier(evidence.evidence_id),
            "root_id": identifier(evidence.root_id),
            "submitted_by_contributor_id": identifier(evidence.submitted_by_contributor_id),
            "source_message_id": optional_identifier(evidence.source_message_id),
            "private_object_key": sensitive(evidence.private_object_key),
            "media_type": evidence.media_type,
            "byte_length": evidence.byte_length,
            "sha256": evidence.sha256.value,
            "captured_at": optional_instant(evidence.captured_at),
            "uploaded_at": instant(evidence.uploaded_at),
            "derived_from_evidence_id": optional_identifier(evidence.derived_from_evidence_id),
            "malware_scan_status": evidence.malware_scan_status.value,
            "extraction_status": evidence.extraction_status.value,
            "extracted_text": optional_sensitive(evidence.extracted_text),
            "version": evidence.version,
            "created_at": instant(evidence.created_at),
            "updated_at": instant(evidence.updated_at),
        }
    )
    return item


def decode_evidence_item(item: StoredItem) -> tuple[DecodedScope, EvidenceItem]:
    reader = ItemReader(item, entity_ref="EVIDENCE_ITEM")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.EVIDENCE_ITEM,
        accepted_schema_versions=EVIDENCE_ITEM_SCHEMA_VERSIONS,
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    evidence = build_entity(
        reader.entity_ref,
        EvidenceItem,
        evidence_id=reader.identifier("evidence_id", EvidenceItemId),
        root_id=reader.identifier("root_id", EvidenceRootId),
        community_id=scope.community_id,
        case_id=scope.case_id,
        namespace=scope.namespace,
        submitted_by_contributor_id=reader.identifier("submitted_by_contributor_id", ContributorId),
        source_message_id=reader.optional_identifier("source_message_id", MessageId),
        private_object_key=reader.sensitive("private_object_key"),
        media_type=reader.text("media_type"),
        byte_length=reader.number("byte_length"),
        sha256=reader.digest("sha256"),
        captured_at=reader.optional_instant("captured_at"),
        uploaded_at=reader.instant("uploaded_at"),
        derived_from_evidence_id=reader.optional_identifier(
            "derived_from_evidence_id", EvidenceItemId
        ),
        malware_scan_status=reader.enum("malware_scan_status", MalwareScanStatus),
        extraction_status=reader.enum("extraction_status", ExtractionStatus),
        extracted_text=reader.optional_sensitive("extracted_text"),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, evidence


def assessment_key(scope: CaseScope, assessment: InvestigationAssessment) -> ItemKey:
    return ItemKey(
        table=_CORE,
        partition_key=keys.case_partition(scope.namespace, scope.case_id),
        sort_key=keys.assessment_sort_key(assessment.created_at, assessment.assessment_id),
    )


def encode_assessment(scope: CaseScope, assessment: InvestigationAssessment) -> StoredItem:
    key = assessment_key(scope, assessment)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.INVESTIGATION_ASSESSMENT,
        schema_version=assessment.schema_version,
        key=key,
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=assessment.case_id,
    )
    item.update(
        {
            "assessment_id": identifier(assessment.assessment_id),
            "based_on_case_version": assessment.based_on_case_version,
            "agent_invocation_id": identifier(assessment.agent_invocation_id),
            "linkage_decision": assessment.linkage_decision,
            "findings": tuple(
                {
                    "fact_id": identifier(finding.fact_id),
                    "evidence_status": finding.evidence_status.value,
                    "reason_code": finding.reason_code,
                }
                for finding in assessment.findings
            ),
            "contradiction_fact_ids": identifiers(assessment.contradiction_fact_ids),
            "alternative_explanations": tuple(assessment.alternative_explanations),
            "independent_source_count": assessment.independent_source_count,
            "is_corroborated": assessment.is_corroborated,
            "recommended_disposition": assessment.recommended_disposition,
            "assessment_hash": assessment.assessment_hash.value,
            "created_at": instant(assessment.created_at),
        }
    )
    return item


def decode_assessment(item: StoredItem) -> tuple[DecodedScope, InvestigationAssessment]:
    reader = ItemReader(item, entity_ref="INVESTIGATION_ASSESSMENT")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.INVESTIGATION_ASSESSMENT,
        accepted_schema_versions=ASSESSMENT_SCHEMA_VERSIONS,
    )
    if scope.case_id is None:
        raise build_entity_error(reader, "scope")
    findings: list[EvidenceFinding] = []
    for index, raw in enumerate(reader.mappings("findings")):
        finding_reader = reader.child(raw, f"findings[{index}]")
        finding = build_entity(
            finding_reader.entity_ref,
            EvidenceFinding,
            fact_id=finding_reader.identifier("fact_id", FactId),
            evidence_status=finding_reader.enum("evidence_status", EvidenceStatus),
            reason_code=finding_reader.text("reason_code"),
        )
        finding_reader.finish()
        findings.append(finding)
    assessment = build_entity(
        reader.entity_ref,
        InvestigationAssessment,
        assessment_id=reader.identifier("assessment_id", AssessmentId),
        case_id=scope.case_id,
        based_on_case_version=reader.number("based_on_case_version"),
        agent_invocation_id=reader.uuid("agent_invocation_id"),
        linkage_decision=reader.text("linkage_decision"),
        findings=tuple(findings),
        contradiction_fact_ids=reader.identifiers("contradiction_fact_ids", FactId),
        alternative_explanations=reader.texts("alternative_explanations"),
        independent_source_count=reader.number("independent_source_count"),
        is_corroborated=reader.flag("is_corroborated"),
        recommended_disposition=reader.text("recommended_disposition"),
        assessment_hash=reader.digest("assessment_hash"),
        created_at=reader.instant("created_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, assessment
