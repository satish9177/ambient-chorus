"""Ambient ingestion: store what a channel said, once, and never overwrite it.

Ingestion is the first trust boundary in the system. Everything it accepts is untrusted text
that some person typed, and the only decisions made here are about *identity and integrity* --
never about meaning. Nothing in this module reads a message to decide whether it matters; that
is the Monitor's job, and it happens afterwards behind a validated contract.

Three rules define the behaviour, and each has a failure case that must stay distinguishable:

* **exact replay is success.** The same channel message with the same content returns the
  identifier already stored, so a redelivered batch cannot create a second copy of one
  incident and cannot inflate corroboration later.
* **the same identity with different content is a conflict.** The stored message is never
  overwritten and never versioned into something the sender did not say. The caller gets
  ``IDEMPOTENCY_CONFLICT`` and the original stands.
* **duplicate bytes collapse to one evidence origin.** An attachment is keyed by its content
  hash within the community, so forwarding the same photo twice records two submissions of one
  origin rather than two independent sources.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from chorus.application import observability
from chorus.application.services.identity import derive_evidence_root_id
from chorus.domain.entities import (
    CommunityMessage,
    DerivationKind,
    EvidenceRoot,
    MessageProcessingStatus,
)
from chorus.domain.errors import IntegrityError, ValidationError
from chorus.domain.ids import (
    CommunityId,
    ContributorId,
    EvidenceItemId,
    IdGenerator,
    MessageId,
    Namespace,
    SensitiveStr,
    Sha256Digest,
)
from chorus.domain.time import Clock, format_utc, require_utc
from chorus.ports.errors import IdempotencyConflictError, PersistenceConflictError
from chorus.ports.idempotency import (
    EntityRef,
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotentCommand,
)
from chorus.ports.records import ChannelUniquenessLock
from chorus.ports.repositories import CoreRepositoryPort, IdempotencyRepositoryPort
from chorus.ports.scopes import CommunityScope
from chorus.ports.storage import WriteOperation
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork
from chorus.privacy.canonical import hash_value

MAX_MESSAGES_PER_REQUEST = 25
MAX_MESSAGE_TEXT = 10_000
ADAPTER = "SYNTHETIC"


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestAttachment:
    """One attachment declared by the adapter, identified by its content hash."""

    evidence_id: EvidenceItemId
    media_type: str
    byte_length: int
    sha256: Sha256Digest


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestMessage:
    """One untrusted ambient message exactly as a channel reported it."""

    channel_message_id: str
    contributor_id: ContributorId | None
    sent_at: datetime
    text: str
    attachments: tuple[IngestAttachment, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestMessagesCommand:
    """One ingestion request for one community."""

    namespace: Namespace
    community_id: CommunityId
    actor_id_hash: Sha256Digest
    idempotency_key: str
    messages: tuple[IngestMessage, ...]
    correlation_id: UUID | None = None
    """Ties this command's observability events to the request that issued it.

    Optional because a command is not required to have arrived over HTTP, and an event without
    a correlation identifier is still a true event -- just harder to trace back.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestedMessage:
    """What happened to one message in the request."""

    channel_message_id: str
    message_id: MessageId
    replay: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestMessagesResult:
    """Per-message outcomes plus the counts an operation record and a log may carry."""

    messages: tuple[IngestedMessage, ...]
    accepted_count: int
    replayed_count: int
    evidence_roots_created: int


@dataclass(slots=True)
class IngestMessages:
    """Persist ambient messages idempotently through the Phase 2 persistence ports."""

    core: CoreRepositoryPort
    idempotency: IdempotencyRepositoryPort
    unit_of_work: UnitOfWork
    clock: Clock
    ids: IdGenerator

    async def execute(self, command: IngestMessagesCommand) -> IngestMessagesResult:
        _validate_request(command)
        scope = CommunityScope(namespace=command.namespace, community_id=command.community_id)
        now = self.clock.now()

        # A strong read, once per command: the community must exist and every named
        # contributor must belong to it. Ingesting for an unknown community would create a
        # partition nothing owns; attributing a message to a foreign contributor would give
        # someone else's account ownership of a report and of the mandate that follows it.
        await self.core.load_community(scope.namespace_scope, command.community_id)
        for contributor_id in sorted(
            {message.contributor_id for message in command.messages if message.contributor_id},
            key=str,
        ):
            await self.core.load_contributor(scope, contributor_id)

        outcomes: list[IngestedMessage] = []
        roots_created = 0
        for message in command.messages:
            outcome, created = await self._ingest_one(command, scope, message, now=now)
            outcomes.append(outcome)
            roots_created += created

        replayed = sum(1 for outcome in outcomes if outcome.replay)
        observability.message_ingested(
            namespace=command.namespace,
            community_id=command.community_id,
            correlation_id=command.correlation_id,
            actor_id_hash=command.actor_id_hash,
            accepted=len(outcomes) - replayed,
            replayed=replayed,
        )
        return IngestMessagesResult(
            messages=tuple(outcomes),
            accepted_count=len(outcomes) - replayed,
            replayed_count=replayed,
            evidence_roots_created=roots_created,
        )

    async def _ingest_one(
        self,
        command: IngestMessagesCommand,
        scope: CommunityScope,
        message: IngestMessage,
        *,
        now: datetime,
    ) -> tuple[IngestedMessage, int]:
        content_sha256 = content_digest(message)
        channel_digest = channel_identity_digest(message.channel_message_id)

        existing = await self._classify_existing(
            scope,
            channel_digest,
            content_sha256,
            correlation_id=command.correlation_id,
            actor_id_hash=command.actor_id_hash,
        )
        if existing is not None:
            return IngestedMessage(
                channel_message_id=message.channel_message_id,
                message_id=existing,
                replay=True,
            ), 0

        message_id = self.ids.new(MessageId)
        operations, roots_created = await self._build_operations(
            command,
            scope,
            message,
            message_id=message_id,
            content_sha256=content_sha256,
            channel_digest=channel_digest,
            now=now,
        )
        key = self._idempotency_key(command, message)
        plan = TransactionPlan(
            name="ingest-message",
            operations=operations,
            audit_required=False,
            commit_proof=self.idempotency.commit_proof(key, request_hash=content_sha256),
        )
        try:
            await self.unit_of_work.commit(plan)
        except PersistenceConflictError:
            # Another attempt reached the lock first. Re-classify rather than assume: the
            # winner may have stored the identical message (a replay) or a different one
            # under the same channel identity (a conflict), and those are not the same event.
            replayed = await self._classify_existing(
                scope,
                channel_digest,
                content_sha256,
                correlation_id=command.correlation_id,
                actor_id_hash=command.actor_id_hash,
            )
            if replayed is None:
                raise
            return IngestedMessage(
                channel_message_id=message.channel_message_id,
                message_id=replayed,
                replay=True,
            ), 0
        return IngestedMessage(
            channel_message_id=message.channel_message_id,
            message_id=message_id,
            replay=False,
        ), roots_created

    async def _classify_existing(
        self,
        scope: CommunityScope,
        channel_digest: Sha256Digest,
        content_sha256: Sha256Digest,
        *,
        correlation_id: UUID | None,
        actor_id_hash: Sha256Digest,
    ) -> MessageId | None:
        """Return the stored identifier for an exact replay, or fail a content conflict."""

        lock = await self.core.load_channel_lock(
            scope, adapter=ADAPTER, channel_message_id_sha256=channel_digest
        )
        if lock is None:
            return None
        if lock.content_sha256 != content_sha256:
            # One channel identifier, two different bodies. The stored message stands, and the
            # event records only that it happened -- neither body is written down anywhere.
            observability.message_conflict(
                namespace=scope.namespace,
                community_id=scope.community_id,
                correlation_id=correlation_id,
                actor_id_hash=actor_id_hash,
                reason_code="CHANNEL_CONTENT_MISMATCH",
            )
            raise IdempotencyConflictError("COMMUNITY_MESSAGE")
        return lock.message_id

    async def _build_operations(
        self,
        command: IngestMessagesCommand,
        scope: CommunityScope,
        message: IngestMessage,
        *,
        message_id: MessageId,
        content_sha256: Sha256Digest,
        channel_digest: Sha256Digest,
        now: datetime,
    ) -> tuple[tuple[WriteOperation, ...], int]:
        operations: list[WriteOperation] = []
        roots_created = 0
        for attachment in message.attachments:
            root = await self._evidence_root(scope, attachment, now=now)
            if root is not None:
                operations.append(self.core.stage_create_evidence_root(scope, root))
                roots_created += 1

        stored = self._message_entity(
            command,
            message,
            message_id=message_id,
            content_sha256=content_sha256,
            now=now,
        )
        operations.append(self.core.stage_create_message(scope, stored))
        operations.append(
            self.core.stage_create_channel_lock(
                scope,
                ChannelUniquenessLock(
                    namespace=command.namespace,
                    community_id=command.community_id,
                    adapter=ADAPTER,
                    channel_message_id_sha256=channel_digest,
                    message_id=message_id,
                    content_sha256=content_sha256,
                    created_at=now,
                ),
            )
        )
        operations.append(
            self.idempotency.stage_create_completed(
                self._idempotency_key(command, message),
                request_hash=content_sha256,
                result_entity_refs=(
                    EntityRef(entity_type="COMMUNITY_MESSAGE", entity_id=message_id.value),
                ),
                response_status=202,
                now=now,
            )
        )
        return tuple(operations), roots_created

    async def _evidence_root(
        self, scope: CommunityScope, attachment: IngestAttachment, *, now: datetime
    ) -> EvidenceRoot | None:
        """Return the root to create, or ``None`` when this content already has one."""

        existing = await self.core.load_evidence_root(scope, attachment.sha256)
        if existing is not None:
            if existing.media_type != attachment.media_type:
                # One content hash claiming two media types means the declared metadata does
                # not describe the bytes. Failing closed keeps a mislabelled origin from
                # becoming the anchor for independence calculations.
                raise IntegrityError("EVIDENCE_ROOT")
            return None
        return EvidenceRoot(
            root_id=derive_evidence_root_id(
                namespace=scope.namespace,
                community_id=scope.community_id,
                root_sha256=attachment.sha256,
            ),
            community_id=scope.community_id,
            namespace=scope.namespace,
            root_sha256=attachment.sha256,
            media_type=attachment.media_type,
            first_observed_at=now,
            derivation_kind=DerivationKind.ORIGINAL,
            parent_root_id=None,
            created_at=now,
            updated_at=now,
        )

    def _message_entity(
        self,
        command: IngestMessagesCommand,
        message: IngestMessage,
        *,
        message_id: MessageId,
        content_sha256: Sha256Digest,
        now: datetime,
    ) -> CommunityMessage:
        try:
            return CommunityMessage(
                message_id=message_id,
                community_id=command.community_id,
                namespace=command.namespace,
                channel_message_id=message.channel_message_id,
                contributor_id=message.contributor_id,
                sent_at=message.sent_at,
                received_at=now,
                raw_text=SensitiveStr(message.text),
                attachment_ids=tuple(item.evidence_id for item in message.attachments),
                content_sha256=content_sha256,
                ingestion_idempotency_key=command.idempotency_key,
                processing_status=MessageProcessingStatus.NEW,
                version=1,
                created_at=now,
                updated_at=now,
            )
        except ValueError as error:
            raise ValidationError("COMMUNITY_MESSAGE") from error

    @staticmethod
    def _idempotency_key(command: IngestMessagesCommand, message: IngestMessage) -> IdempotencyKey:
        """Bind one durable record to one channel message inside one client request.

        Scoping per message rather than per batch is what lets a partially delivered batch be
        re-sent safely: each message resolves to its own recorded outcome instead of the whole
        request being classified by whichever message happened to be first.
        """

        return IdempotencyKey(
            partition=IdempotencyPartition(
                kind=IdempotencyPartitionKind.COMMUNITY,
                namespace=command.namespace,
                community_id=command.community_id,
            ),
            command=IdempotentCommand.INGEST_MESSAGE,
            actor_id_hash=command.actor_id_hash,
            key_hash=_digest(f"{command.idempotency_key}\x1f{message.channel_message_id}"),
        )


def content_digest(message: IngestMessage) -> Sha256Digest:
    """Hash everything a replay must reproduce exactly.

    Attachment provenance is inside the digest: re-sending the same words with a different
    photo is a different message, and treating it as a replay would silently keep the first
    photo while reporting success for the second.
    """

    parts = [
        ADAPTER,
        message.channel_message_id,
        "" if message.contributor_id is None else str(message.contributor_id),
        require_utc(message.sent_at).isoformat(timespec="microseconds"),
        message.text,
    ]
    for attachment in sorted(message.attachments, key=lambda item: item.sha256.value):
        parts.extend(
            [
                str(attachment.evidence_id),
                attachment.media_type,
                str(attachment.byte_length),
                attachment.sha256.value,
            ]
        )
    return _digest("\x1f".join(parts))


def channel_identity_digest(channel_message_id: str) -> Sha256Digest:
    """Hash the channel identifier, because user-controlled text never enters a key."""

    return _digest(channel_message_id)


def _digest(value: str) -> Sha256Digest:
    return Sha256Digest(f"sha256:{sha256(value.encode('utf-8')).hexdigest()}")


def _validate_request(command: IngestMessagesCommand) -> None:
    if not 1 <= len(command.messages) <= MAX_MESSAGES_PER_REQUEST:
        raise ValidationError("INGEST_REQUEST")
    if not 8 <= len(command.idempotency_key) <= 128:
        raise ValidationError("IDEMPOTENCY_KEY")
    if not command.idempotency_key.isprintable() or not command.idempotency_key.isascii():
        raise ValidationError("IDEMPOTENCY_KEY")
    channel_ids = tuple(message.channel_message_id for message in command.messages)
    if len(set(channel_ids)) != len(channel_ids):
        raise ValidationError("INGEST_REQUEST")
    for message in command.messages:
        if not 1 <= len(message.channel_message_id) <= 160:
            raise ValidationError("CHANNEL_MESSAGE_ID")
        if not 1 <= len(message.text) <= MAX_MESSAGE_TEXT:
            raise ValidationError("MESSAGE_TEXT")
        try:
            require_utc(message.sent_at)
        except ValueError as error:
            raise ValidationError("SENT_AT") from error
        evidence_ids = tuple(item.evidence_id for item in message.attachments)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValidationError("ATTACHMENTS")
        for attachment in message.attachments:
            if attachment.byte_length < 1 or not 1 <= len(attachment.media_type) <= 120:
                raise ValidationError("ATTACHMENTS")


def monitor_operation_identity(command: IngestMessagesCommand) -> tuple[Sha256Digest, Sha256Digest]:
    """Derive the idempotency key hash and request hash for the Monitor operation.

    Both are computed from the *authoritative normalized command*, and neither from anything
    persisting it produced. An earlier version hashed the generated message identifiers, which
    made an exact replay hash identically only by accident of ordering and made a genuinely
    different request touching the same messages look like the same command.

    It lives beside the command rather than in the route because canonicalization is an
    application concern: the transport layer may not reach the privacy package, and a hash the
    transport computed for itself would be a second, drifting definition of "the same request".

    The messages are **sorted** before they are hashed, and so are each message's attachment
    descriptors. The endpoint takes a batch, and Monitor processing canonicalizes and orders it
    anyway, so two requests carrying the same messages in a different array order are the same
    command -- and a client that shuffled its array on a retry would otherwise be told its own
    request conflicts with itself. The sort key is the immutable channel identity of a message
    and the identifier of an attachment, neither of which the request may restate differently
    without genuinely being a different request.
    """

    key_hash = hash_value({"schema": "monitor-operation-key/v1", "key": command.idempotency_key})
    request_hash = hash_value(
        {
            "schema": "monitor-operation-request/v1",
            "namespace": command.namespace.value,
            "community_id": str(command.community_id),
            "messages": [
                _canonical_message(message)
                for message in sorted(
                    command.messages, key=lambda item: (ADAPTER, item.channel_message_id)
                )
            ],
        }
    )
    return key_hash, request_hash


def _canonical_message(message: IngestMessage) -> dict[str, object]:
    """One normalized message, with its attachments in a stable identifier order."""

    return {
        "adapter": ADAPTER,
        "channel_message_id": message.channel_message_id,
        "contributor_id": (None if message.contributor_id is None else str(message.contributor_id)),
        "sent_at": format_utc(message.sent_at.astimezone(UTC)),
        "text": message.text,
        "attachments": [
            {
                "evidence_id": str(attachment.evidence_id),
                "media_type": attachment.media_type,
                "byte_length": attachment.byte_length,
                "sha256": attachment.sha256.value,
            }
            for attachment in sorted(message.attachments, key=lambda item: str(item.evidence_id))
        ],
    }
