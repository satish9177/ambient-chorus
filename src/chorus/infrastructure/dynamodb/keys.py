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
    EvidenceRootId,
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


def feed_signal_sort_key(message_id: MessageId) -> str:
    """``MESSAGE_SIGNAL#{message_id}`` in the community partition.

    The signal is keyed by message rather than by case so the ambient feed can resolve which
    of its rows carry a discovered pattern with one bounded query on the partition it is
    already reading. There is no message-to-case index and no scan, by design.
    """

    return _join("MESSAGE_SIGNAL", str(message_id))


def operation_sort_key() -> str:
    return "OPERATION"


def evidence_root_sort_key(root_sha256: Sha256Digest) -> str:
    return _join("EVIDENCE_ROOT", root_sha256.value)


def evidence_root_id_sort_key(root_id: EvidenceRootId) -> str:
    """``EVIDENCE_ROOT_ID#{root_id}`` in the community partition (ADR-017).

    The canonical evidence root is content-addressed, so a ``parent_root_id`` -- which is an
    identifier, and for Phase-3 roots a one-way UUIDv5 -- has no address of its own. This
    locator is that address, and it holds one field: the ``root_sha256`` the canonical row
    lives at. Two direct-key batch gets therefore resolve an ancestry chain with no scan, no
    GSI, and no prefix walk.
    """

    return _join("EVIDENCE_ROOT_ID", str(root_id))


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


def monitor_progress_sort_key(invocation_id: UUID) -> str:
    """``AGENT_PROGRESS#{invocation_id}`` in the application-operation partition.

    Apply progress lives beside the operation rather than beside a case, because one answer
    may touch several cases and may touch none at all. A resumed worker has an operation ID
    in its hand before it has anything else, so this is the one address it can always reach.
    """

    return _join("AGENT_PROGRESS", str(invocation_id))


SNAPSHOT_CHUNK_INDEX_WIDTH = 6


def monitor_snapshot_manifest_sort_key(kind: str, invocation_id: UUID) -> str:
    """``{kind}#{invocation_id}#MANIFEST`` in the application-operation partition.

    The kind leads so the two snapshots one invocation owns -- its frozen input and its
    validated plan -- are distinct addresses rather than one row with a mode flag. Both live
    beside the operation for the same reason apply progress does: a resumed worker holds an
    operation ID before it holds anything else.
    """

    return _join(kind, str(invocation_id), "MANIFEST")


def monitor_snapshot_chunk_sort_key(kind: str, invocation_id: UUID, index: int) -> str:
    """``{kind}#{invocation_id}#CHUNK#{index}`` with a fixed zero-padded width.

    Zero padding keeps the chunk order lexicographic as well as numeric, so the ordering the
    manifest asserts and the ordering the key grammar implies can never disagree.
    """

    if index < 0 or len(str(index)) > SNAPSHOT_CHUNK_INDEX_WIDTH:
        raise ValueError("snapshot chunk index exceeds the fixed key width")
    return _join(kind, str(invocation_id), "CHUNK", str(index).zfill(SNAPSHOT_CHUNK_INDEX_WIDTH))


def fence_partition(namespace: Namespace, case_id: CaseId) -> str:
    """``NS#{namespace}#FENCE#{case_id}`` -- the send fence's own partition.

    The fence is deliberately *not* in the case partition, and the reason is IAM rather than
    modelling. ``dynamodb:LeadingKeys`` constrains the partition key and nothing constrains the
    sort key, so a principal permitted to write ``NS#n#CASE#k`` can write every item in it --
    the case row, its facts, its reports, its evidence, its mandates. Keying the fence
    separately is what makes the frozen "compiler Core write is the fence alone" a permission
    AWS can actually express
    ([ADR-019](../../../../docs/adr/ADR-019-send-fence-partition-isolation.md)).

    It stays in the Core table: this is a partition change, not a new store.
    """

    return _join("NS", namespace.value, "FENCE", str(case_id))


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


def compile_audit_sort_key(compile_id: UUID) -> str:
    """``COMPILE#{compile_id}`` -- one immutable projection per logical compile."""

    return _join("COMPILE", str(compile_id))


def audit_event_sort_key(occurred_at: datetime, audit_event_id: UUID) -> str:
    return _join("EVENT", format_utc(occurred_at), str(audit_event_id))


OPERATION_INVOCATION_SORT_KEY_PREFIX = "AGENT_"
"""Covers both records under an operation partition that name an invocation.

``AGENT_INVOCATION#`` and ``AGENT_PROGRESS#`` share it, so "which invocation owns this
operation" is one bounded range query rather than two.
"""

HISTORY_SORT_KEY_PREFIX = "HISTORY#"
EVENT_SORT_KEY_PREFIX = "EVENT#"
FACT_SORT_KEY_PREFIX = "FACT#"
REPORT_SORT_KEY_PREFIX = "REPORT#"
ASSESSMENT_SORT_KEY_PREFIX = "ASSESSMENT#"
MANDATE_CURRENT_SORT_KEY_PREFIX = "MANDATE_CURRENT#"
COMMITMENT_SORT_KEY_PREFIX = "COMMITMENT#"
MESSAGE_SORT_KEY_PREFIX = "MESSAGE#"
FEED_SIGNAL_SORT_KEY_PREFIX = "MESSAGE_SIGNAL#"
