"""Explicit Shareable-table item mappings.

Only external-safe values are written here. Private lineage, raw text, contributor identity,
contact values, and object keys have no field in any of these items.
"""

from __future__ import annotations

from typing import Final

from chorus.domain.entities import (
    ActionClaim,
    ActionExecution,
    ActionExecutionState,
    ActionProposal,
    ActionProposalStatus,
    Approval,
    ApprovalDecision,
    Commitment,
    CommitmentStatus,
    DestinationKind,
    DisclosureScope,
    EvidenceStatus,
    FactType,
    Purpose,
)
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CommitmentId,
    ContributorId,
    DestinationId,
    EvidenceItemId,
    ExecutionId,
    ExportFactId,
    SafeEvidenceRefId,
    ViewId,
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
    read_envelope,
)
from chorus.infrastructure.dynamodb.codec_core import build_entity_error
from chorus.ports.records import (
    ActionHistoryLocator,
    CurrentActionPointer,
    CurrentViewPointer,
    StoredMandateVersionRef,
    StoredSafeDestination,
    StoredSafeEvidenceRef,
    StoredShareableFact,
    StoredShareableView,
    TransformationKind,
    ViewHistoryLocator,
)
from chorus.ports.scopes import ActionScope, CaseScope
from chorus.ports.storage import ItemKey, StoredItem, StoredValue, TableName

_SHARE: Final = TableName.SHAREABLE

VIEW_SCHEMA_VERSIONS: Final = frozenset({"shareable-case-view/v1"})
VIEW_POINTER_SCHEMA_VERSIONS: Final = frozenset({"current-view-pointer/v1"})
VIEW_HISTORY_SCHEMA_VERSIONS: Final = frozenset({"view-history-locator/v1"})
PROPOSAL_SCHEMA_VERSIONS: Final = frozenset({"action-proposal/v1"})
APPROVAL_SCHEMA_VERSIONS: Final = frozenset({"approval/v1"})
EXECUTION_SCHEMA_VERSIONS: Final = frozenset({"action-execution/v1"})
ACTION_POINTER_SCHEMA_VERSIONS: Final = frozenset({"current-action-pointer/v1"})
ACTION_HISTORY_SCHEMA_VERSIONS: Final = frozenset({"action-history-locator/v1"})
COMMITMENT_SCHEMA_VERSIONS: Final = frozenset({"commitment/v1"})


def view_key(scope: CaseScope, view_id: ViewId) -> ItemKey:
    return ItemKey(
        table=_SHARE,
        partition_key=keys.view_partition(scope.namespace, view_id),
        sort_key=keys.view_sort_key(),
    )


def encode_view(scope: CaseScope, view: StoredShareableView) -> StoredItem:
    key = view_key(scope, view.view_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.SHAREABLE_VIEW,
        schema_version=view.schema_version,
        key=key,
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=view.case_id,
    )
    item.update(
        {
            "view_id": identifier(view.view_id),
            "community_public_label": view.community_public_label,
            "case_version": view.case_version,
            "policy_version": view.policy_version,
            "compiler_version": view.compiler_version,
            "destination": {
                "destination_id": view.destination.destination_id.value,
                "kind": view.destination.kind.value,
                "registry_version": view.destination.registry_version,
                "routing_token": str(view.destination.routing_token),
                "display_label": view.destination.display_label,
            },
            "purpose": view.purpose.value,
            "generated_at": instant(view.generated_at),
            "expires_at": instant(view.expires_at),
            "mandate_version_set": tuple(
                {
                    "mandate_id": str(ref.mandate_id),
                    "version": ref.version,
                    "terms_hash": ref.terms_hash.value,
                }
                for ref in view.mandate_version_set
            ),
            "authorization_snapshot_hash": view.authorization_snapshot_hash.value,
            "shareable_facts": tuple(
                {
                    "export_fact_id": identifier(fact.export_fact_id),
                    "fact_type": fact.fact_type.value,
                    "safe_text": fact.safe_text,
                    "effective_scope": fact.effective_scope.value,
                    "evidence_status": fact.evidence_status.value,
                    "contributor_count": fact.contributor_count,
                    "transformation": fact.transformation.value,
                    "transformation_rule_id": fact.transformation_rule_id,
                    "safe_evidence_ref_ids": identifiers(fact.safe_evidence_ref_ids),
                    "content_hash": fact.content_hash.value,
                }
                for fact in view.shareable_facts
            ),
            "safe_evidence_refs": tuple(
                {
                    "safe_evidence_ref_id": identifier(ref.safe_evidence_ref_id),
                    "media_type": ref.media_type,
                    "export_handle_id": str(ref.export_handle_id),
                    "sha256": ref.sha256.value,
                    "caption": ref.caption,
                    "created_by_rule_id": ref.created_by_rule_id,
                    "content_hash": ref.content_hash.value,
                }
                for ref in view.safe_evidence_refs
            ),
            "audit_refs": tuple(str(value) for value in view.audit_refs),
            "view_hash": view.view_hash.value,
        }
    )
    return item


def decode_view(item: StoredItem) -> tuple[DecodedScope, StoredShareableView]:
    reader = ItemReader(item, entity_ref="SHAREABLE_VIEW")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.SHAREABLE_VIEW,
        accepted_schema_versions=VIEW_SCHEMA_VERSIONS,
    )
    if scope.case_id is None:
        raise build_entity_error(reader, "scope")
    destination_reader = reader.child(reader.mapping("destination"), "destination")
    try:
        destination_id = DestinationId(destination_reader.text("destination_id"))
    except ValueError as error:
        raise build_entity_error(destination_reader, "destination_id") from error
    destination = build_entity(
        destination_reader.entity_ref,
        StoredSafeDestination,
        destination_id=destination_id,
        kind=destination_reader.enum("kind", DestinationKind),
        registry_version=destination_reader.number("registry_version"),
        routing_token=destination_reader.uuid("routing_token"),
        display_label=destination_reader.text("display_label"),
    )
    destination_reader.finish()

    mandate_refs: list[StoredMandateVersionRef] = []
    for index, raw in enumerate(reader.mappings("mandate_version_set")):
        ref_reader = reader.child(raw, f"mandate_version_set[{index}]")
        mandate_ref = build_entity(
            ref_reader.entity_ref,
            StoredMandateVersionRef,
            mandate_id=ref_reader.uuid("mandate_id"),
            version=ref_reader.number("version"),
            terms_hash=ref_reader.digest("terms_hash"),
        )
        ref_reader.finish()
        mandate_refs.append(mandate_ref)

    facts: list[StoredShareableFact] = []
    for index, raw in enumerate(reader.mappings("shareable_facts")):
        fact_reader = reader.child(raw, f"shareable_facts[{index}]")
        fact = build_entity(
            fact_reader.entity_ref,
            StoredShareableFact,
            export_fact_id=fact_reader.identifier("export_fact_id", ExportFactId),
            fact_type=fact_reader.enum("fact_type", FactType),
            safe_text=fact_reader.text("safe_text"),
            effective_scope=fact_reader.enum("effective_scope", DisclosureScope),
            evidence_status=fact_reader.enum("evidence_status", EvidenceStatus),
            contributor_count=fact_reader.number("contributor_count"),
            transformation=fact_reader.enum("transformation", TransformationKind),
            transformation_rule_id=fact_reader.text("transformation_rule_id"),
            safe_evidence_ref_ids=fact_reader.identifiers(
                "safe_evidence_ref_ids", SafeEvidenceRefId
            ),
            content_hash=fact_reader.digest("content_hash"),
        )
        fact_reader.finish()
        facts.append(fact)

    evidence_refs: list[StoredSafeEvidenceRef] = []
    for index, raw in enumerate(reader.mappings("safe_evidence_refs")):
        evidence_reader = reader.child(raw, f"safe_evidence_refs[{index}]")
        evidence_ref = build_entity(
            evidence_reader.entity_ref,
            StoredSafeEvidenceRef,
            safe_evidence_ref_id=evidence_reader.identifier(
                "safe_evidence_ref_id", SafeEvidenceRefId
            ),
            media_type=evidence_reader.text("media_type"),
            export_handle_id=evidence_reader.uuid("export_handle_id"),
            sha256=evidence_reader.digest("sha256"),
            caption=evidence_reader.text("caption"),
            created_by_rule_id=evidence_reader.text("created_by_rule_id"),
            content_hash=evidence_reader.digest("content_hash"),
        )
        evidence_reader.finish()
        evidence_refs.append(evidence_ref)

    view = build_entity(
        reader.entity_ref,
        StoredShareableView,
        schema_version=schema_version,
        view_id=reader.identifier("view_id", ViewId),
        case_id=scope.case_id,
        community_public_label=reader.text("community_public_label"),
        case_version=reader.number("case_version"),
        policy_version=reader.text("policy_version"),
        compiler_version=reader.text("compiler_version"),
        destination=destination,
        purpose=reader.enum("purpose", Purpose),
        generated_at=reader.instant("generated_at"),
        expires_at=reader.instant("expires_at"),
        mandate_version_set=tuple(mandate_refs),
        authorization_snapshot_hash=reader.digest("authorization_snapshot_hash"),
        shareable_facts=tuple(facts),
        safe_evidence_refs=tuple(evidence_refs),
        audit_refs=reader.uuids("audit_refs"),
        view_hash=reader.digest("view_hash"),
    )
    reader.finish()
    return scope, view


def view_pointer_key(scope: CaseScope) -> ItemKey:
    return ItemKey(
        table=_SHARE,
        partition_key=keys.view_current_partition(scope.namespace, scope.case_id),
        sort_key=keys.current_pointer_sort_key(),
    )


def encode_view_pointer(scope: CaseScope, pointer: CurrentViewPointer) -> StoredItem:
    key = view_pointer_key(scope)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.CURRENT_VIEW_POINTER,
        schema_version=pointer.schema_version,
        key=key,
        namespace=pointer.namespace,
        community_id=pointer.community_id,
        case_id=pointer.case_id,
    )
    item.update(
        {
            "view_id": identifier(pointer.view_id),
            "view_hash": pointer.view_hash.value,
            "case_version": pointer.case_version,
            "expires_at": instant(pointer.expires_at),
            "version": pointer.version,
            "created_at": instant(pointer.created_at),
            "updated_at": instant(pointer.updated_at),
        }
    )
    return item


def decode_view_pointer(item: StoredItem) -> tuple[DecodedScope, CurrentViewPointer]:
    reader = ItemReader(item, entity_ref="CURRENT_VIEW_POINTER")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.CURRENT_VIEW_POINTER,
        accepted_schema_versions=VIEW_POINTER_SCHEMA_VERSIONS,
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    pointer = build_entity(
        reader.entity_ref,
        CurrentViewPointer,
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=scope.case_id,
        view_id=reader.identifier("view_id", ViewId),
        view_hash=reader.digest("view_hash"),
        case_version=reader.number("case_version"),
        expires_at=reader.instant("expires_at"),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, pointer


def view_history_key(scope: CaseScope, locator: ViewHistoryLocator) -> ItemKey:
    return ItemKey(
        table=_SHARE,
        partition_key=keys.view_current_partition(scope.namespace, scope.case_id),
        sort_key=keys.view_history_sort_key(locator.generated_at, locator.view_id),
    )


def encode_view_history(scope: CaseScope, locator: ViewHistoryLocator) -> StoredItem:
    key = view_history_key(scope, locator)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.VIEW_HISTORY_LOCATOR,
        schema_version=locator.schema_version,
        key=key,
        namespace=locator.namespace,
        community_id=locator.community_id,
        case_id=locator.case_id,
    )
    item.update(
        {
            "view_id": identifier(locator.view_id),
            "view_hash": locator.view_hash.value,
            "case_version": locator.case_version,
            "generated_at": instant(locator.generated_at),
        }
    )
    return item


def decode_view_history(item: StoredItem) -> tuple[DecodedScope, ViewHistoryLocator]:
    reader = ItemReader(item, entity_ref="VIEW_HISTORY_LOCATOR")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.VIEW_HISTORY_LOCATOR,
        accepted_schema_versions=VIEW_HISTORY_SCHEMA_VERSIONS,
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    locator = build_entity(
        reader.entity_ref,
        ViewHistoryLocator,
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=scope.case_id,
        view_id=reader.identifier("view_id", ViewId),
        view_hash=reader.digest("view_hash"),
        case_version=reader.number("case_version"),
        generated_at=reader.instant("generated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, locator


def proposal_key(scope: ActionScope) -> ItemKey:
    return ItemKey(
        table=_SHARE,
        partition_key=keys.action_partition(scope.namespace, scope.action_id),
        sort_key=keys.action_sort_key(),
    )


def encode_proposal(scope: ActionScope, proposal: ActionProposal) -> StoredItem:
    key = proposal_key(scope)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.ACTION_PROPOSAL,
        schema_version=proposal.schema_version,
        key=key,
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=proposal.case_id,
    )
    item.update(
        {
            "action_id": identifier(proposal.action_id),
            "case_version": proposal.case_version,
            "view_id": identifier(proposal.view_id),
            "view_hash": proposal.view_hash.value,
            "subject": proposal.subject,
            "claims": tuple(
                {
                    "claim_id": str(claim.claim_id),
                    "text": claim.text,
                    "export_fact_ids": tuple(str(value) for value in claim.export_fact_ids),
                    "claim_hash": claim.claim_hash.value,
                }
                for claim in proposal.claims
            ),
            "requested_action": proposal.requested_action,
            "requested_deadline": optional_instant(proposal.requested_deadline),
            "request_fact_ids": tuple(str(value) for value in proposal.request_fact_ids),
            "caveats": tuple(proposal.caveats),
            "tone": proposal.tone,
            "agent_invocation_id": str(proposal.agent_invocation_id),
            "prompt_version": proposal.prompt_version,
            "proposal_hash": proposal.proposal_hash.value,
            "status": proposal.status.value,
            "created_at": instant(proposal.created_at),
        }
    )
    return item


def decode_proposal(item: StoredItem) -> tuple[DecodedScope, ActionProposal]:
    reader = ItemReader(item, entity_ref="ACTION_PROPOSAL")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.ACTION_PROPOSAL,
        accepted_schema_versions=PROPOSAL_SCHEMA_VERSIONS,
    )
    if scope.case_id is None:
        raise build_entity_error(reader, "scope")
    claims: list[ActionClaim] = []
    for index, raw in enumerate(reader.mappings("claims")):
        claim_reader = reader.child(raw, f"claims[{index}]")
        claim = build_entity(
            claim_reader.entity_ref,
            ActionClaim,
            claim_id=claim_reader.uuid("claim_id"),
            text=claim_reader.text("text"),
            export_fact_ids=claim_reader.uuids("export_fact_ids"),
            claim_hash=claim_reader.digest("claim_hash"),
        )
        claim_reader.finish()
        claims.append(claim)
    proposal = build_entity(
        reader.entity_ref,
        ActionProposal,
        action_id=reader.identifier("action_id", ActionId),
        case_id=scope.case_id,
        case_version=reader.number("case_version"),
        view_id=reader.identifier("view_id", ViewId),
        view_hash=reader.digest("view_hash"),
        subject=reader.text("subject"),
        claims=tuple(claims),
        requested_action=reader.text("requested_action"),
        requested_deadline=reader.optional_instant("requested_deadline"),
        request_fact_ids=reader.uuids("request_fact_ids"),
        caveats=reader.texts("caveats"),
        tone=reader.text("tone"),
        agent_invocation_id=reader.uuid("agent_invocation_id"),
        prompt_version=reader.text("prompt_version"),
        proposal_hash=reader.digest("proposal_hash"),
        status=reader.enum("status", ActionProposalStatus),
        created_at=reader.instant("created_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, proposal


def approval_key(scope: ActionScope, approval_id: ApprovalId) -> ItemKey:
    return ItemKey(
        table=_SHARE,
        partition_key=keys.action_partition(scope.namespace, scope.action_id),
        sort_key=keys.approval_sort_key(approval_id),
    )


def encode_approval(scope: ActionScope, approval: Approval) -> StoredItem:
    key = approval_key(scope, approval.approval_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.APPROVAL,
        schema_version=approval.schema_version,
        key=key,
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=approval.case_id,
    )
    item.update(
        {
            "approval_id": identifier(approval.approval_id),
            "action_id": identifier(approval.action_id),
            "proposal_hash": approval.proposal_hash.value,
            "view_hash": approval.view_hash.value,
            "approver_id": identifier(approval.approver_id),
            "decision": approval.decision.value,
            "approved_at": instant(approval.approved_at),
            "expires_at": instant(approval.expires_at),
            "consumed_at": optional_instant(approval.consumed_at),
            "approval_hash": approval.approval_hash.value,
            "idempotency_key": approval.idempotency_key,
            "version": approval.version,
            "created_at": instant(approval.created_at),
            "updated_at": instant(approval.updated_at),
        }
    )
    return item


def decode_approval(item: StoredItem) -> tuple[DecodedScope, Approval]:
    reader = ItemReader(item, entity_ref="APPROVAL")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.APPROVAL,
        accepted_schema_versions=APPROVAL_SCHEMA_VERSIONS,
    )
    if scope.case_id is None:
        raise build_entity_error(reader, "scope")
    approval = build_entity(
        reader.entity_ref,
        Approval,
        approval_id=reader.identifier("approval_id", ApprovalId),
        action_id=reader.identifier("action_id", ActionId),
        case_id=scope.case_id,
        proposal_hash=reader.digest("proposal_hash"),
        view_hash=reader.digest("view_hash"),
        approver_id=reader.identifier("approver_id", ContributorId),
        decision=reader.enum("decision", ApprovalDecision),
        approved_at=reader.instant("approved_at"),
        expires_at=reader.instant("expires_at"),
        consumed_at=reader.optional_instant("consumed_at"),
        approval_hash=reader.digest("approval_hash"),
        idempotency_key=reader.text("idempotency_key"),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, approval


def execution_key(scope: ActionScope, execution_id: ExecutionId) -> ItemKey:
    return ItemKey(
        table=_SHARE,
        partition_key=keys.action_partition(scope.namespace, scope.action_id),
        sort_key=keys.execution_sort_key(execution_id),
    )


def encode_execution(scope: ActionScope, execution: ActionExecution) -> StoredItem:
    key = execution_key(scope, execution.execution_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.ACTION_EXECUTION,
        schema_version=execution.schema_version,
        key=key,
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=execution.case_id,
    )
    item.update(
        {
            "execution_id": identifier(execution.execution_id),
            "action_id": identifier(execution.action_id),
            "approval_id": identifier(execution.approval_id),
            "proposal_hash": execution.proposal_hash.value,
            "view_hash": execution.view_hash.value,
            "idempotency_key": execution.idempotency_key,
            "state": execution.state.value,
            "attempt_number": execution.attempt_number,
            "rendered_message_hash": execution.rendered_message_hash.value,
            "ses_request_token_hash": execution.ses_request_token_hash.value,
            "ses_message_id": execution.ses_message_id,
            "started_at": optional_instant(execution.started_at),
            "finished_at": optional_instant(execution.finished_at),
            "failure_code": execution.failure_code,
            "failure_detail_safe": execution.failure_detail_safe,
            "reconciled_at": optional_instant(execution.reconciled_at),
            "version": execution.version,
            "created_at": instant(execution.created_at),
            "updated_at": instant(execution.updated_at),
        }
    )
    return item


def decode_execution(item: StoredItem) -> tuple[DecodedScope, ActionExecution]:
    reader = ItemReader(item, entity_ref="ACTION_EXECUTION")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.ACTION_EXECUTION,
        accepted_schema_versions=EXECUTION_SCHEMA_VERSIONS,
    )
    if scope.case_id is None:
        raise build_entity_error(reader, "scope")
    execution = build_entity(
        reader.entity_ref,
        ActionExecution,
        execution_id=reader.identifier("execution_id", ExecutionId),
        action_id=reader.identifier("action_id", ActionId),
        case_id=scope.case_id,
        approval_id=reader.identifier("approval_id", ApprovalId),
        proposal_hash=reader.digest("proposal_hash"),
        view_hash=reader.digest("view_hash"),
        idempotency_key=reader.text("idempotency_key"),
        state=reader.enum("state", ActionExecutionState),
        attempt_number=reader.number("attempt_number"),
        rendered_message_hash=reader.digest("rendered_message_hash"),
        ses_request_token_hash=reader.digest("ses_request_token_hash"),
        ses_message_id=reader.optional_text("ses_message_id"),
        started_at=reader.optional_instant("started_at"),
        finished_at=reader.optional_instant("finished_at"),
        failure_code=reader.optional_text("failure_code"),
        failure_detail_safe=reader.optional_text("failure_detail_safe"),
        reconciled_at=reader.optional_instant("reconciled_at"),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, execution


def action_pointer_key(scope: CaseScope) -> ItemKey:
    return ItemKey(
        table=_SHARE,
        partition_key=keys.action_current_partition(scope.namespace, scope.case_id),
        sort_key=keys.current_pointer_sort_key(),
    )


def encode_action_pointer(scope: CaseScope, pointer: CurrentActionPointer) -> StoredItem:
    key = action_pointer_key(scope)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.CURRENT_ACTION_POINTER,
        schema_version=pointer.schema_version,
        key=key,
        namespace=pointer.namespace,
        community_id=pointer.community_id,
        case_id=pointer.case_id,
    )
    item.update(
        {
            "action_id": identifier(pointer.action_id),
            "proposal_hash": pointer.proposal_hash.value,
            "view_id": identifier(pointer.view_id),
            "view_hash": pointer.view_hash.value,
            "case_version": pointer.case_version,
            "status": pointer.status.value,
            "version": pointer.version,
            "created_at": instant(pointer.created_at),
            "updated_at": instant(pointer.updated_at),
        }
    )
    return item


def decode_action_pointer(item: StoredItem) -> tuple[DecodedScope, CurrentActionPointer]:
    reader = ItemReader(item, entity_ref="CURRENT_ACTION_POINTER")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.CURRENT_ACTION_POINTER,
        accepted_schema_versions=ACTION_POINTER_SCHEMA_VERSIONS,
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    pointer = build_entity(
        reader.entity_ref,
        CurrentActionPointer,
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=scope.case_id,
        action_id=reader.identifier("action_id", ActionId),
        proposal_hash=reader.digest("proposal_hash"),
        view_id=reader.identifier("view_id", ViewId),
        view_hash=reader.digest("view_hash"),
        case_version=reader.number("case_version"),
        status=reader.enum("status", ActionProposalStatus),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, pointer


def action_history_key(scope: CaseScope, locator: ActionHistoryLocator) -> ItemKey:
    return ItemKey(
        table=_SHARE,
        partition_key=keys.action_current_partition(scope.namespace, scope.case_id),
        sort_key=keys.action_history_sort_key(locator.created_at, locator.action_id),
    )


def encode_action_history(scope: CaseScope, locator: ActionHistoryLocator) -> StoredItem:
    key = action_history_key(scope, locator)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.ACTION_HISTORY_LOCATOR,
        schema_version=locator.schema_version,
        key=key,
        namespace=locator.namespace,
        community_id=locator.community_id,
        case_id=locator.case_id,
    )
    item.update(
        {
            "action_id": identifier(locator.action_id),
            "proposal_hash": locator.proposal_hash.value,
            "created_at": instant(locator.created_at),
        }
    )
    return item


def decode_action_history(item: StoredItem) -> tuple[DecodedScope, ActionHistoryLocator]:
    reader = ItemReader(item, entity_ref="ACTION_HISTORY_LOCATOR")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.ACTION_HISTORY_LOCATOR,
        accepted_schema_versions=ACTION_HISTORY_SCHEMA_VERSIONS,
    )
    if scope.community_id is None or scope.case_id is None:
        raise build_entity_error(reader, "scope")
    locator = build_entity(
        reader.entity_ref,
        ActionHistoryLocator,
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=scope.case_id,
        action_id=reader.identifier("action_id", ActionId),
        proposal_hash=reader.digest("proposal_hash"),
        created_at=reader.instant("created_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, locator


def commitment_key(scope: CaseScope, commitment_id: CommitmentId) -> ItemKey:
    return ItemKey(
        table=_SHARE,
        partition_key=keys.case_partition(scope.namespace, scope.case_id),
        sort_key=keys.commitment_sort_key(commitment_id),
    )


def encode_commitment(scope: CaseScope, commitment: Commitment) -> StoredItem:
    key = commitment_key(scope, commitment.commitment_id)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.COMMITMENT,
        schema_version=commitment.schema_version,
        key=key,
        namespace=scope.namespace,
        community_id=scope.community_id,
        case_id=commitment.case_id,
    )
    item.update(
        {
            "commitment_id": identifier(commitment.commitment_id),
            "action_id": optional_identifier(commitment.action_id),
            "source_evidence_id": identifier(commitment.source_evidence_id),
            "obligor": commitment.obligor,
            "action_text": commitment.action_text,
            "due_at": instant(commitment.due_at),
            "verification_method": commitment.verification_method,
            "status": commitment.status.value,
            "scheduler_name": commitment.scheduler_name,
            "schedule_generation": commitment.schedule_generation,
            "due_event_id": str(commitment.due_event_id),
            "verified_by_contributor_id": optional_identifier(
                commitment.verified_by_contributor_id
            ),
            "verification_evidence_id": optional_identifier(commitment.verification_evidence_id),
            "outcome_note": commitment.outcome_note,
            "version": commitment.version,
            "created_at": instant(commitment.created_at),
            "updated_at": instant(commitment.updated_at),
        }
    )
    return item


def decode_commitment(item: StoredItem) -> tuple[DecodedScope, Commitment]:
    reader = ItemReader(item, entity_ref="COMMITMENT")
    scope, schema_version = read_envelope(
        reader,
        expected_type=EntityType.COMMITMENT,
        accepted_schema_versions=COMMITMENT_SCHEMA_VERSIONS,
    )
    if scope.case_id is None:
        raise build_entity_error(reader, "scope")
    commitment = build_entity(
        reader.entity_ref,
        Commitment,
        commitment_id=reader.identifier("commitment_id", CommitmentId),
        case_id=scope.case_id,
        action_id=reader.optional_identifier("action_id", ActionId),
        source_evidence_id=reader.identifier("source_evidence_id", EvidenceItemId),
        obligor=reader.text("obligor"),
        action_text=reader.text("action_text"),
        due_at=reader.instant("due_at"),
        verification_method=reader.text("verification_method"),
        status=reader.enum("status", CommitmentStatus),
        scheduler_name=reader.text("scheduler_name"),
        schedule_generation=reader.number("schedule_generation"),
        due_event_id=reader.uuid("due_event_id"),
        verified_by_contributor_id=reader.optional_identifier(
            "verified_by_contributor_id", ContributorId
        ),
        verification_evidence_id=reader.optional_identifier(
            "verification_evidence_id", EvidenceItemId
        ),
        outcome_note=reader.optional_text("outcome_note"),
        version=reader.number("version"),
        created_at=reader.instant("created_at"),
        updated_at=reader.instant("updated_at"),
        schema_version=schema_version,
    )
    reader.finish()
    return scope, commitment
