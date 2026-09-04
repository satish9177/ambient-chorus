"""Persistence-boundary records that are not domain aggregates.

These records are the durable shapes named by the frozen persistence mapping: uniqueness
locks, current pointers, history locators, fences, agent invocation results, and the stored
external-safe view. They are immutable, closed, and built only from domain-safe primitives.

``StoredShareableView`` mirrors the frozen ``ShareableCaseView`` schema field for field. It is
restated here rather than imported because infrastructure and ports must not depend on the
privacy package; a parity test asserts the two field sets never drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from chorus.domain.entities import (
    ActionProposalStatus,
    CaseState,
    DestinationKind,
    DisclosureScope,
    EvidenceStatus,
    FactType,
    MandateStatus,
    Purpose,
)
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    CaseId,
    CommunityId,
    ContributorId,
    DestinationId,
    EvidenceRootId,
    ExecutionId,
    ExportFactId,
    FactId,
    MandateId,
    MessageId,
    Namespace,
    OperationId,
    SafeEvidenceRefId,
    SensitiveStr,
    Sha256Digest,
    ViewId,
)
from chorus.domain.mandates import CurrentMandatePointer
from chorus.domain.time import epoch_micros, epoch_seconds_ceiling, require_utc
from chorus.ports.idempotency import EntityRef

_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
"""Closed shape every safe code persisted by these records must satisfy."""


class AgentName(StrEnum):
    MONITOR = "MONITOR"
    INVESTIGATOR = "INVESTIGATOR"
    ACTION = "ACTION"


class AgentInvocationOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class TransformationKind(StrEnum):
    """Mirror of the compiler transformation kinds carried in a stored safe fact."""

    DIRECT = "DIRECT"
    ANONYMIZED = "ANONYMIZED"
    AGGREGATED = "AGGREGATED"
    GENERALIZED = "GENERALIZED"


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceRootLocator:
    """The immutable address of one evidence root, keyed by its identifier (ADR-017).

    It holds exactly one field beyond its own identity: the ``root_sha256`` the canonical
    ``EVIDENCE_ROOT#`` row lives at. Deliberately not a second copy of the root, so the two
    can never disagree about anything except existence -- and a missing locator fails closed
    with ``INTEGRITY_ERROR`` rather than producing a partial ancestry and an under-count.

    Created in the same transaction as its canonical root, with the same create-only
    condition, so a root is never addressable by content without being addressable by
    identifier as well.
    """

    namespace: Namespace
    community_id: CommunityId
    root_id: EvidenceRootId
    root_sha256: Sha256Digest
    created_at: datetime
    schema_version: str = "evidence-root-locator/v1"

    def __post_init__(self) -> None:
        require_utc(self.created_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelUniquenessLock:
    """Conditional-create lock proving one adapter channel message is ingested once."""

    namespace: Namespace
    community_id: CommunityId
    adapter: str
    channel_message_id_sha256: Sha256Digest
    message_id: MessageId
    content_sha256: Sha256Digest
    created_at: datetime
    schema_version: str = "channel-uniqueness-lock/v1"

    def __post_init__(self) -> None:
        if self.adapter != "SYNTHETIC":
            raise ValueError("unsupported adapter")
        require_utc(self.created_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedSignalProjection:
    """The ambient feed's view of one message that a validated agent proposal linked.

    This is a **mutable display projection**, written inside the same bounded transaction that
    creates the durable reports, facts, and candidate case, so the feed can never show a
    signal for state that was not committed.

    What it is not is as load-bearing as what it is. It is not an authorization artifact, not
    the ownership boundary for a report, not the authority that decides whether a message may
    join a case, and not a permanent one-message-one-case lock. An earlier create-only version
    of this row made the *storage* row the linkage decision: once written, otherwise-valid
    domain state at that address became permanently unreachable, and a refreshed label or a
    later legitimate correction had nowhere to go.

    So it carries a ``version`` and is replaced by a guarded update. Whether Phase-3 Monitor
    may relink an already-linked report is decided *before* any write is staged, by a domain
    rule in the apply service -- and the answer there is no. A later correction or split use
    case, which does have that authority, updates this row like any other display projection.

    It exists at all because the frozen access patterns forbid a scan and require no GSI.
    Without a signal row in the community partition, resolving "which of these feed rows
    belong to a discovered pattern" would need exactly the message-to-case index V1 refuses
    to build.
    """

    namespace: Namespace
    community_id: CommunityId
    message_id: MessageId
    case_id: CaseId
    case_version: int
    label: str
    related_message_count: int
    case_state: CaseState
    detected_at: datetime
    version: int = 1
    schema_version: str = "feed-signal-projection/v2"

    def __post_init__(self) -> None:
        if not 1 <= len(self.label) <= 160:
            raise ValueError("feed signal label length is invalid")
        if self.case_version < 1:
            raise ValueError("case version must be positive")
        if self.version < 1:
            raise ValueError("version must be positive")
        if self.related_message_count < 1:
            raise ValueError("a signal always relates at least its own message")
        require_utc(self.detected_at)

    def same_display_state(self, other: FeedSignalProjection) -> bool:
        """True when two projections describe the identical display state.

        Compared field by field rather than by equality, because three fields are bookkeeping
        rather than display. ``version`` and ``detected_at`` belong to the row; ``case_version``
        records which version of the case the signal was written against, and the feed never
        shows it. Including any of them would make an exact replay look like a change worth
        another write, and would make every case version bump rewrite every signal it has.
        """

        return (
            self.namespace == other.namespace
            and self.community_id == other.community_id
            and self.message_id == other.message_id
            and self.case_id == other.case_id
            and self.label == other.label
            and self.related_message_count == other.related_message_count
            and self.case_state is other.case_state
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentInvocationResult:
    """Durable agent invocation record holding hashes and result refs, never content.

    ``case_id`` is optional because a Monitor run need not produce a case: a batch of
    ordinary neighbourly chatter, or a proposal that did not meet the candidate guard, still
    happened and still has to leave a durable record of what was asked and what came back.
    A case-scoped record lives in its case partition; an unlinked one lives under the
    application operation that ran it.

    ``failure_code`` is a safe closed code and never a message. The record holds hashes,
    versions, and codes -- never raw agent output, prompt text, completion text, or a
    provider response body -- so it can be read freely without becoming a second corpus of
    the private text the invocation was about.
    """

    invocation_id: UUID
    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId | None
    agent_name: AgentName
    prompt_version: str
    input_hash: Sha256Digest
    output_hash: Sha256Digest | None
    outcome: AgentInvocationOutcome
    result_refs: tuple[EntityRef, ...]
    created_at: datetime
    model_profile_hash: Sha256Digest | None = None
    failure_code: str | None = None
    operation_id: OperationId | None = None
    schema_version: str = "agent-invocation-result/v2"

    def __post_init__(self) -> None:
        if not 1 <= len(self.prompt_version) <= 64:
            raise ValueError("prompt_version length is invalid")
        require_utc(self.created_at)
        if self.outcome is AgentInvocationOutcome.SUCCEEDED and self.output_hash is None:
            raise ValueError("a succeeded invocation must record an output hash")
        if self.outcome is AgentInvocationOutcome.SUCCEEDED and self.failure_code is not None:
            raise ValueError("a succeeded invocation cannot carry a failure code")
        if self.outcome is AgentInvocationOutcome.FAILED and self.failure_code is None:
            raise ValueError("a failed invocation must record a safe failure code")
        if self.failure_code is not None and not _SAFE_CODE.fullmatch(self.failure_code):
            raise ValueError("failure code is not a safe closed code")
        if self.case_id is None and self.operation_id is None:
            raise ValueError("an unlinked invocation record must name its operation")
        refs = tuple((ref.entity_type, ref.entity_id) for ref in self.result_refs)
        if len(set(refs)) != len(refs):
            raise ValueError("result references must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitorApplyProgress:
    """How far one validated Monitor answer has been applied.

    The frozen persistence design applies a validated Monitor answer as a sequence of bounded
    transactions rather than one large one, because at the contract maxima a single answer
    implies far more writes than DynamoDB permits in one transaction. That immediately raises
    the question this record answers: if delivery repeats after step three of five, how does
    the second attempt know not to write the first three again?

    It knows because this row advances *inside* each step's own transaction. Step ``k``
    updates ``completed_steps`` to ``k`` under ``version = k - 1``, so the row and the writes
    it describes commit together or not at all, and a resumed attempt reads one item to learn
    exactly where to continue.

    ``plan_hash`` binds the progress to the exact ordered plan it describes. A resumed attempt
    whose plan hashes differently is not a continuation of this one -- the world changed
    underneath it -- so it must not skip steps on the strength of somebody else's progress.
    """

    invocation_id: UUID
    operation_id: OperationId
    namespace: Namespace
    community_id: CommunityId
    input_hash: Sha256Digest
    output_hash: Sha256Digest
    plan_hash: Sha256Digest
    completed_steps: int
    total_steps: int
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "monitor-apply-progress/v1"

    def __post_init__(self) -> None:
        if self.total_steps < 1:
            raise ValueError("a progress record describes at least one step")
        if not 0 <= self.completed_steps <= self.total_steps:
            raise ValueError("completed steps must lie within the plan")
        if self.version < 1:
            raise ValueError("version must be positive")
        require_utc(self.created_at)
        require_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")

    @property
    def is_complete(self) -> bool:
        return self.completed_steps == self.total_steps


class MonitorSnapshotKind(StrEnum):
    """Which of a Monitor invocation's two frozen stages a snapshot holds."""

    MONITOR_INPUT = "MONITOR_INPUT"
    MONITOR_PLAN = "MONITOR_PLAN"


MAX_SNAPSHOT_CHUNK_BYTES = 300_000
"""How many canonical UTF-8 bytes one snapshot chunk may carry.

Comfortably below DynamoDB's 400 KiB item limit, with room left for the envelope, the key,
and the rest of the item's attributes. It is a fixed safe maximum rather than a tuned one:
the point is that no legitimate snapshot can ever produce an item that storage will refuse.
"""

MAX_SNAPSHOT_BYTES = 1_048_576
"""The frozen 1 MiB application payload bound, which a whole snapshot stays within."""

MAX_SNAPSHOT_CHUNKS = 8
"""A hard chunk-count bound, so a manifest can never describe an unbounded read."""

SNAPSHOT_CHUNK_INDEX_WIDTH = 6


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitorSnapshotManifest:
    """The immutable header of one chunked Monitor snapshot.

    A Monitor operation has three durable stages -- frozen input, validated apply plan, and
    apply progress -- and the first two are snapshots so that a partially applied operation
    is *finishable* rather than merely diagnosable. Same ``invocation_id`` therefore always
    means the same ``MonitorInput``, and once the plan snapshot exists the model invocation
    is permanently complete.

    The manifest carries the digest of the whole canonical byte string and the exact number
    of chunks it was cut into, so reassembly is checkable rather than hopeful: a missing
    chunk, a wrong count, or a digest mismatch is an integrity failure, not a shorter
    snapshot. It never carries the content itself.
    """

    invocation_id: UUID
    operation_id: OperationId
    namespace: Namespace
    community_id: CommunityId
    kind: MonitorSnapshotKind
    content_sha256: Sha256Digest
    byte_length: int
    chunk_count: int
    input_hash: Sha256Digest
    prompt_version: str
    created_at: datetime
    expires_at_epoch: int
    output_hash: Sha256Digest | None = None
    plan_hash: Sha256Digest | None = None
    model_profile_hash: Sha256Digest | None = None
    provenance_hash: Sha256Digest | None = None
    """The single digest that chains a validated-plan snapshot to everything that identifies it.

    ``content_sha256`` covers the reassembled document and nothing else, so every scalar the
    manifest restates beside it -- operation, invocation, input hash, output hash, plan hash,
    prompt version, model profile -- would otherwise be an unverified duplicate of a field
    inside that document. Two copies of a value are not a check; they agree by construction
    until something rewrites one of them.

    ``provenance_hash`` is what turns the duplication into a chain: it is the digest of exactly
    those seven values, written on the manifest and recomputed on load from the document. To
    move any one of them, all of manifest scalar, document field, document digest, and this
    hash have to move together -- and the loader still independently recomputes the output hash
    from the stored answer and the plan hash from the stored steps, so a consistent set of
    metadata edits describing content that was not edited fails anyway.
    """
    schema_version: str = "monitor-snapshot-manifest/v1"

    def __post_init__(self) -> None:
        require_utc(self.created_at)
        if not 1 <= self.byte_length <= MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot byte length is outside the frozen payload bound")
        if not 1 <= self.chunk_count <= MAX_SNAPSHOT_CHUNKS:
            raise ValueError("snapshot chunk count is outside the frozen bound")
        if not 1 <= len(self.prompt_version) <= 64:
            raise ValueError("prompt_version length is invalid")
        if self.expires_at_epoch < 0:
            raise ValueError("expires_at_epoch cannot be negative")
        if self.kind is MonitorSnapshotKind.MONITOR_PLAN and (
            self.output_hash is None
            or self.plan_hash is None
            or self.model_profile_hash is None
            or self.provenance_hash is None
        ):
            raise ValueError("a validated-plan snapshot binds its whole provenance")
        if self.kind is MonitorSnapshotKind.MONITOR_INPUT and (
            self.output_hash is not None
            or self.plan_hash is not None
            or self.model_profile_hash is not None
            or self.provenance_hash is not None
        ):
            raise ValueError("a frozen-input snapshot binds neither output nor plan")


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitorSnapshotChunk:
    """One immutable ordered slice of a snapshot's canonical UTF-8 bytes.

    ``content`` is :class:`SensitiveStr` because a snapshot legitimately holds material
    derived from private community text. It lives only in the private Core table, under the
    operation partition, and no read path outside the Monitor use case addresses it.
    """

    invocation_id: UUID
    operation_id: OperationId
    namespace: Namespace
    community_id: CommunityId
    kind: MonitorSnapshotKind
    index: int
    content: SensitiveStr
    expires_at_epoch: int
    schema_version: str = "monitor-snapshot-chunk/v1"

    def __post_init__(self) -> None:
        if not 0 <= self.index < MAX_SNAPSHOT_CHUNKS:
            raise ValueError("snapshot chunk index is outside the frozen bound")
        if len(self.content.reveal().encode("utf-8")) > MAX_SNAPSHOT_CHUNK_BYTES:
            raise ValueError("snapshot chunk exceeds the frozen chunk bound")
        if self.expires_at_epoch < 0:
            raise ValueError("expires_at_epoch cannot be negative")


@dataclass(frozen=True, slots=True, kw_only=True)
class SendFence:
    """Short-lived Core fence that orders mandate revocation against one send attempt."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    execution_id: ExecutionId
    action_id: ActionId
    approval_id: ApprovalId
    view_id: ViewId
    authorization_snapshot_hash: Sha256Digest
    acquired_at: datetime
    expires_at: datetime
    schema_version: str = "send-fence/v1"

    def __post_init__(self) -> None:
        require_utc(self.acquired_at)
        require_utc(self.expires_at)
        if self.expires_at <= self.acquired_at:
            raise ValueError("fence expiry must be after acquisition")

    @property
    def expires_at_micros(self) -> int:
        """Exact expiry used by every fence authorization condition.

        A fence is live while ``now < expires_at`` and expired from ``expires_at`` onwards.
        Comparing whole seconds would round the deadline down and let a takeover -- or a
        mandate revocation -- win up to a second before the fence actually expired, so the
        comparison is made in exact microseconds.
        """

        return epoch_micros(self.expires_at)

    @property
    def expires_at_epoch(self) -> int:
        """DynamoDB TTL cleanup field, deliberately separate from authorization.

        Table TTL removes abandoned fences long after they stop mattering. It is never
        consulted by a condition, so its second granularity cannot affect authorization.
        """

        return epoch_seconds_ceiling(self.expires_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredCurrentMandatePointer:
    """Current mandate pointer plus the persisted status and row version."""

    namespace: Namespace
    community_id: CommunityId
    pointer: CurrentMandatePointer
    status: MandateStatus
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "current-mandate-pointer/v1"

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("version must be positive")
        require_utc(self.created_at)
        require_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class FactMandateAssociation:
    """Immutable proof that one fact was authorized by one exact mandate version."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    fact_id: FactId
    mandate_id: MandateId
    mandate_version: int
    terms_hash: Sha256Digest
    contributor_id: ContributorId
    created_at: datetime
    schema_version: str = "fact-mandate-association/v1"

    def __post_init__(self) -> None:
        if self.mandate_version < 1:
            raise ValueError("mandate version must be positive")
        require_utc(self.created_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredSafeDestination:
    """Address-free destination metadata carried inside a stored safe view."""

    destination_id: DestinationId
    kind: DestinationKind
    registry_version: int
    routing_token: UUID
    display_label: str

    def __post_init__(self) -> None:
        if self.registry_version < 1:
            raise ValueError("destination registry version must be positive")
        if not 1 <= len(self.display_label) <= 120:
            raise ValueError("destination display label length is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredMandateVersionRef:
    mandate_id: UUID
    version: int
    terms_hash: Sha256Digest

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("mandate version must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredShareableFact:
    export_fact_id: ExportFactId
    fact_type: FactType
    safe_text: str
    effective_scope: DisclosureScope
    evidence_status: EvidenceStatus
    contributor_count: int
    transformation: TransformationKind
    transformation_rule_id: str
    safe_evidence_ref_ids: tuple[SafeEvidenceRefId, ...]
    content_hash: Sha256Digest

    def __post_init__(self) -> None:
        if not 1 <= len(self.safe_text) <= 500:
            raise ValueError("safe fact text length is invalid")
        if self.contributor_count < 1:
            raise ValueError("shareable fact requires a contributor")
        if self.effective_scope is DisclosureScope.INTERNAL_ONLY:
            raise ValueError("an internal-only fact can never be stored in the safe zone")
        if len(set(self.safe_evidence_ref_ids)) != len(self.safe_evidence_ref_ids):
            raise ValueError("safe evidence references must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredSafeEvidenceRef:
    safe_evidence_ref_id: SafeEvidenceRefId
    media_type: str
    export_handle_id: UUID
    sha256: Sha256Digest
    caption: str
    created_by_rule_id: str
    content_hash: Sha256Digest

    def __post_init__(self) -> None:
        if not 1 <= len(self.media_type) <= 120:
            raise ValueError("safe evidence media type length is invalid")
        if not 1 <= len(self.caption) <= 300:
            raise ValueError("safe evidence caption length is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredShareableView:
    """Immutable compiled view exactly as persisted in the shareable table."""

    schema_version: str
    view_id: ViewId
    case_id: CaseId
    community_public_label: str
    case_version: int
    policy_version: str
    compiler_version: str
    destination: StoredSafeDestination
    purpose: Purpose
    generated_at: datetime
    expires_at: datetime
    mandate_version_set: tuple[StoredMandateVersionRef, ...]
    authorization_snapshot_hash: Sha256Digest
    shareable_facts: tuple[StoredShareableFact, ...]
    safe_evidence_refs: tuple[StoredSafeEvidenceRef, ...]
    audit_refs: tuple[UUID, ...]
    view_hash: Sha256Digest

    def __post_init__(self) -> None:
        require_utc(self.generated_at)
        require_utc(self.expires_at)
        if self.expires_at <= self.generated_at:
            raise ValueError("view expiry must be after generation")
        if self.case_version < 1:
            raise ValueError("case version must be positive")
        if not self.shareable_facts:
            raise ValueError("a stored view always contains at least one safe fact")
        export_ids = tuple(fact.export_fact_id for fact in self.shareable_facts)
        if len(set(export_ids)) != len(export_ids):
            raise ValueError("export fact IDs must be unique")
        ref_ids = tuple(ref.safe_evidence_ref_id for ref in self.safe_evidence_refs)
        if len(set(ref_ids)) != len(ref_ids):
            raise ValueError("safe evidence reference IDs must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentViewPointer:
    """Strongly read pointer naming the only view that may authorize a new action."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    view_id: ViewId
    view_hash: Sha256Digest
    case_version: int
    expires_at: datetime
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "current-view-pointer/v1"

    def __post_init__(self) -> None:
        if self.case_version < 1 or self.version < 1:
            raise ValueError("versions must be positive")
        require_utc(self.expires_at)
        require_utc(self.created_at)
        require_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ViewHistoryLocator:
    """Immutable safe locator for a compiled view."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    view_id: ViewId
    view_hash: Sha256Digest
    case_version: int
    generated_at: datetime
    schema_version: str = "view-history-locator/v1"

    def __post_init__(self) -> None:
        if self.case_version < 1:
            raise ValueError("case version must be positive")
        require_utc(self.generated_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentActionPointer:
    """Strongly read pointer naming the current proposal bound to a case."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    proposal_hash: Sha256Digest
    view_id: ViewId
    view_hash: Sha256Digest
    case_version: int
    status: ActionProposalStatus
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = "current-action-pointer/v1"

    def __post_init__(self) -> None:
        if self.case_version < 1 or self.version < 1:
            raise ValueError("versions must be positive")
        require_utc(self.created_at)
        require_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionHistoryLocator:
    """Immutable safe locator for an action proposal."""

    namespace: Namespace
    community_id: CommunityId
    case_id: CaseId
    action_id: ActionId
    proposal_hash: Sha256Digest
    created_at: datetime
    schema_version: str = "action-history-locator/v1"

    def __post_init__(self) -> None:
        require_utc(self.created_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class MandatePointerExpectation:
    """Exact current-mandate-pointer state a conditional replace requires."""

    row_version: int
    mandate_version: int

    def __post_init__(self) -> None:
        if self.row_version < 1 or self.mandate_version < 1:
            raise ValueError("expected pointer versions must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class ViewPointerExpectation:
    """Exact current-view-pointer state a conditional replace requires."""

    row_version: int
    view_hash: Sha256Digest

    def __post_init__(self) -> None:
        if self.row_version < 1:
            raise ValueError("expected pointer row version must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionPointerExpectation:
    """Exact current-action-pointer state a conditional replace requires."""

    row_version: int
    proposal_hash: Sha256Digest

    def __post_init__(self) -> None:
        if self.row_version < 1:
            raise ValueError("expected pointer row version must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageFeedEntry:
    """One ambient feed row returned in canonical time order."""

    message_id: MessageId
    sent_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.sent_at)
