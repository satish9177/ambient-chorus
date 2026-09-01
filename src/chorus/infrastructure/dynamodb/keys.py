"""Deterministic typed key builders for the frozen three-table grammar.

Every key segment is uppercase ASCII, an already-validated namespace, a lowercase UUID, a
canonical ``sha256:`` digest, a canonical UTC instant, or a zero-padded integer. User text
never enters a key: a channel message ID is hashed, and free-text titles, summaries, names,
and addresses have no key form at all.

``_segment`` is a defence-in-depth assertion. Every caller already passes a typed value, so a
failure here means an upstream invariant broke rather than that a caller supplied bad input.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    AssessmentId,
    CaseId,
    CommitmentId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    ExecutionId,
    FactId,
    MandateId,
    MessageId,
    Namespace,
    OperationId,
    ReportId,
    Sha256Digest,
    ViewId,
)
from chorus.domain.time import format_utc

MAX_KEY_SEGMENT_LENGTH = 256
MANDATE_VERSION_WIDTH = 10


def _segment(value: str) -> str:
    """Validate one key segment; ``#`` is the reserved separator and never appears inside."""

    if not value or len(value) > MAX_KEY_SEGMENT_LENGTH:
        raise ValueError("key segment length is invalid")
    if "#" in value:
        raise ValueError("key segment cannot contain the reserved separator")
    if any(character < " " or character == "\x7f" for character in value):
        raise ValueError("key segment cannot contain control characters")
    return value


def _join(*segments: str) -> str:
    return "#".join(_segment(segment) for segment in segments)


def namespace_partition(namespace: Namespace) -> str:
    """``NS#{namespace}``"""

    return _join("NS", namespace.value)


def community_partition(namespace: Namespace, community_id: CommunityId) -> str:
    """``NS#{namespace}#COMM#{community_id}``"""

    return _join("NS", namespace.value, "COMM", str(community_id))


def case_partition(namespace: Namespace, case_id: CaseId) -> str:
    """``NS#{namespace}#CASE#{case_id}``"""

    return _join("NS", namespace.value, "CASE", str(case_id))


def operation_partition(namespace: Namespace, operation_id: OperationId) -> str:
    """``NS#{namespace}#OPERATION#{operation_id}``"""

    return _join("NS", namespace.value, "OPERATION", str(operation_id))


def view_partition(namespace: Namespace, view_id: ViewId) -> str:
    """``NS#{namespace}#VIEW#{view_id}``"""

    return _join("NS", namespace.value, "VIEW", str(view_id))


def view_current_partition(namespace: Namespace, case_id: CaseId) -> str:
    """``NS#{namespace}#VIEW_CURRENT#{case_id}``"""

    return _join("NS", namespace.value, "VIEW_CURRENT", str(case_id))


def action_partition(namespace: Namespace, action_id: ActionId) -> str:
    """``NS#{namespace}#ACTION#{action_id}``"""

    return _join("NS", namespace.value, "ACTION", str(action_id))


def action_current_partition(namespace: Namespace, case_id: CaseId) -> str:
    """``NS#{namespace}#ACTION_CURRENT#{case_id}``"""

    return _join("NS", namespace.value, "ACTION_CURRENT", str(case_id))


def community_sort_key(community_id: CommunityId) -> str:
    return _join("COMMUNITY", str(community_id))


def contributor_sort_key(contributor_id: ContributorId) -> str:
    return _join("CONTRIBUTOR", str(contributor_id))


def message_sort_key(sent_at: datetime, message_id: MessageId) -> str:
    return _join("MESSAGE", format_utc(sent_at), str(message_id))


def message_sort_key_lower_bound(instant: datetime) -> str:
    """Inclusive lower bound for a canonical time-ordered feed query."""

    return _join("MESSAGE", format_utc(instant))


def message_sort_key_upper_bound(instant: datetime) -> str:
    """Inclusive upper bound covering every message identifier at that exact instant."""

    return f"{_join('MESSAGE', format_utc(instant))}#￿"


def channel_lock_sort_key(adapter: str, channel_message_id_sha256: Sha256Digest) -> str:
    return _join("MESSAGE_KEY", adapter, channel_message_id_sha256.value)


def operation_sort_key() -> str:
    return "OPERATION"


def evidence_root_sort_key(root_sha256: Sha256Digest) -> str:
    return _join("EVIDENCE_ROOT", root_sha256.value)


def case_sort_key() -> str:
    return "CASE"


def report_sort_key(report_id: ReportId) -> str:
    return _join("REPORT", str(report_id))


def fact_sort_key(fact_id: FactId) -> str:
    return _join("FACT", str(fact_id))


def evidence_item_sort_key(evidence_id: EvidenceItemId) -> str:
    return _join("EVIDENCE", str(evidence_id))


def assessment_sort_key(created_at: datetime, assessment_id: AssessmentId) -> str:
    return _join("ASSESSMENT", format_utc(created_at), str(assessment_id))


def mandate_version_sort_key(mandate_id: MandateId, version: int) -> str:
    if version < 1:
        raise ValueError("mandate version must be positive")
    if len(str(version)) > MANDATE_VERSION_WIDTH:
        raise ValueError("mandate version exceeds the fixed key width")
    return _join("MANDATE", str(mandate_id), "VERSION", str(version).zfill(MANDATE_VERSION_WIDTH))


def mandate_current_sort_key(mandate_id: MandateId) -> str:
    return _join("MANDATE_CURRENT", str(mandate_id))


def fact_mandate_sort_key(fact_id: FactId, mandate_id: MandateId) -> str:
    return _join("FACT_MANDATE", str(fact_id), str(mandate_id))


def agent_invocation_sort_key(invocation_id: UUID) -> str:
    return _join("AGENT_INVOCATION", str(invocation_id))


def send_fence_sort_key() -> str:
    return "SEND_FENCE"


def idempotency_sort_key(command: str, actor_id_hash: Sha256Digest, key_hash: Sha256Digest) -> str:
    return _join("IDEMPOTENCY", command, actor_id_hash.value, key_hash.value)


def view_sort_key() -> str:
    return "VIEW"


def current_pointer_sort_key() -> str:
    return "CURRENT"


def view_history_sort_key(generated_at: datetime, view_id: ViewId) -> str:
    return _join("HISTORY", format_utc(generated_at), str(view_id))


def action_sort_key() -> str:
    return "ACTION"


def approval_sort_key(approval_id: ApprovalId) -> str:
    return _join("APPROVAL", str(approval_id))


def execution_sort_key(execution_id: ExecutionId) -> str:
    return _join("EXECUTION", str(execution_id))


def action_history_sort_key(created_at: datetime, action_id: ActionId) -> str:
    return _join("HISTORY", format_utc(created_at), str(action_id))


def commitment_sort_key(commitment_id: CommitmentId) -> str:
    return _join("COMMITMENT", str(commitment_id))


def audit_event_sort_key(occurred_at: datetime, audit_event_id: UUID) -> str:
    return _join("EVENT", format_utc(occurred_at), str(audit_event_id))


HISTORY_SORT_KEY_PREFIX = "HISTORY#"
EVENT_SORT_KEY_PREFIX = "EVENT#"
FACT_SORT_KEY_PREFIX = "FACT#"
REPORT_SORT_KEY_PREFIX = "REPORT#"
ASSESSMENT_SORT_KEY_PREFIX = "ASSESSMENT#"
MANDATE_CURRENT_SORT_KEY_PREFIX = "MANDATE_CURRENT#"
COMMITMENT_SORT_KEY_PREFIX = "COMMITMENT#"
MESSAGE_SORT_KEY_PREFIX = "MESSAGE#"
