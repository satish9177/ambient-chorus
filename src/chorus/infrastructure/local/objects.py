"""In-memory object store used by tests, local development, and the fault matrix.

It is the object-storage counterpart of ``InMemoryStorageDriver``: the same expectations run
against it and against the S3 adapter, so a behaviour proved here is a behaviour proved about
the contract rather than about one emulator.

Faults are injected rather than simulated by patching. ``fail_puts`` makes that many writes
fail *before* storing anything, and ``ambiguous_next_put`` makes one fail *after* the bytes have
already landed. The second is the only interesting S3 failure, because it is the one a caller
cannot tell from a write that never happened -- and the first is what proves the two are
resolved by the same head-then-repeat rule rather than by inspecting an error class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256

from chorus.domain.ids import CaseId, CommunityId, EvidenceItemId, Namespace, Sha256Digest
from chorus.ports.errors import (
    ExternalDependencyError,
    NotFoundError,
    PersistenceConflictError,
)
from chorus.ports.objects import (
    INBOUND_REPLY_MEDIA_TYPE,
    MAX_EVIDENCE_SOURCE_BYTES,
    MAX_INBOUND_REPLY_BYTES,
    ExportObjectDescriptor,
    export_evidence_key,
    inbound_reply_key,
    private_evidence_key,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class _StoredObject:
    content: bytes
    media_type: str


@dataclass(slots=True)
class InMemoryObjectStore:
    """A dictionary keyed by the same derived keys the S3 adapter writes."""

    private: dict[str, _StoredObject] = field(default_factory=dict)
    export: dict[str, _StoredObject] = field(default_factory=dict)
    fail_puts: int = 0
    ambiguous_next_put: bool = False
    put_calls: int = 0
    head_calls: int = 0

    # -- seeding ------------------------------------------------------------------------

    def seed_private_evidence(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        evidence_id: EvidenceItemId,
        content: bytes,
        media_type: str,
    ) -> str:
        """Place a source object where ingestion would have written it, and return its key."""

        key = private_evidence_key(
            namespace=namespace,
            community_id=community_id,
            case_id=case_id,
            evidence_id=evidence_id,
        )
        self.private[key] = _StoredObject(content=content, media_type=media_type)
        return key

    # -- port ---------------------------------------------------------------------------

    async def load_private_evidence(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        evidence_id: EvidenceItemId,
    ) -> bytes:
        key = private_evidence_key(
            namespace=namespace,
            community_id=community_id,
            case_id=case_id,
            evidence_id=evidence_id,
        )
        stored = self.private.get(key)
        if stored is None:
            raise NotFoundError("PRIVATE_EVIDENCE_OBJECT")
        if len(stored.content) > MAX_EVIDENCE_SOURCE_BYTES:
            raise ExternalDependencyError("PRIVATE_EVIDENCE_OBJECT", retryable=False)
        return stored.content

    async def head_inbound_reply(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        raw_sha256: Sha256Digest,
    ) -> ExportObjectDescriptor | None:
        self.head_calls += 1
        stored = self.private.get(
            inbound_reply_key(
                namespace=namespace,
                community_id=community_id,
                case_id=case_id,
                raw_sha256=raw_sha256,
            )
        )
        if stored is None:
            return None
        return ExportObjectDescriptor(
            media_type=stored.media_type,
            byte_length=len(stored.content),
            sha256=Sha256Digest(f"sha256:{sha256(stored.content).hexdigest()}"),
        )

    async def put_inbound_reply(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        raw_sha256: Sha256Digest,
        content: bytes,
    ) -> None:
        self.put_calls += 1
        if len(content) > MAX_INBOUND_REPLY_BYTES:
            raise ExternalDependencyError("INBOUND_REPLY_OBJECT", retryable=False)
        if self.fail_puts > 0:
            self.fail_puts -= 1
            raise ExternalDependencyError("INBOUND_REPLY_OBJECT")
        key = inbound_reply_key(
            namespace=namespace,
            community_id=community_id,
            case_id=case_id,
            raw_sha256=raw_sha256,
        )
        if key in self.private:
            raise PersistenceConflictError("INBOUND_REPLY_OBJECT")
        self.private[key] = _StoredObject(content=content, media_type=INBOUND_REPLY_MEDIA_TYPE)
        if self.ambiguous_next_put:
            self.ambiguous_next_put = False
            raise ExternalDependencyError("INBOUND_REPLY_OBJECT")

    async def head_export_evidence(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        derivative_sha256: Sha256Digest,
    ) -> ExportObjectDescriptor | None:
        self.head_calls += 1
        key = export_evidence_key(
            namespace=namespace,
            community_id=community_id,
            case_id=case_id,
            derivative_sha256=derivative_sha256,
        )
        stored = self.export.get(key)
        if stored is None:
            return None
        return ExportObjectDescriptor(
            media_type=stored.media_type,
            byte_length=len(stored.content),
            sha256=Sha256Digest(f"sha256:{sha256(stored.content).hexdigest()}"),
        )

    async def put_export_evidence(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        derivative_sha256: Sha256Digest,
        content: bytes,
        media_type: str,
    ) -> None:
        self.put_calls += 1
        if self.fail_puts > 0:
            # Fails *before* writing, so the object is genuinely absent afterwards. That is
            # the case a caller cannot distinguish from an ambiguous write, which is why
            # both resolve through the same head-then-repeat rule.
            self.fail_puts -= 1
            raise ExternalDependencyError("EXPORT_EVIDENCE_OBJECT")
        key = export_evidence_key(
            namespace=namespace,
            community_id=community_id,
            case_id=case_id,
            derivative_sha256=derivative_sha256,
        )
        if key in self.export:
            # The same create-if-absent contract the S3 adapter gets from ``If-None-Match``.
            # Modelling it here is what makes the two adapters interchangeable under the
            # contract suite rather than merely similar.
            raise PersistenceConflictError("EXPORT_EVIDENCE_OBJECT")
        self.export[key] = _StoredObject(content=content, media_type=media_type)
        if self.ambiguous_next_put:
            # The bytes landed and the caller will never learn that from this call. This is the
            # exact shape the head-then-repeat rule exists to resolve.
            self.ambiguous_next_put = False
            raise ExternalDependencyError("EXPORT_EVIDENCE_OBJECT")
