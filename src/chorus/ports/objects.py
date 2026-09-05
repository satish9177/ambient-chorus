"""Narrow object-storage port for private evidence bytes and safe export derivatives.

This is deliberately not a storage framework. It models exactly four operations, over exactly
two buckets, with keys the caller cannot choose:

* read one private evidence object, addressed by the typed identifiers of the item that owns it;
* head one export derivative, addressed by the SHA-256 of the bytes it contains;
* create one export derivative at that same content address, only if nothing is there;
* nothing else. There is no list, no delete, no copy, no presign, and no arbitrary key.

**A caller never supplies an object key.** Both key grammars are derived here from typed
identifiers, so an attacker-influenced string cannot become a path segment and a caller cannot
read or write outside the namespace, community, and case it named. The private key is derived
rather than taken from the stored ``EvidenceItem`` so a corrupted stored key cannot redirect a
read; the application compares the two and refuses a disagreement.

**Export objects are content-addressed**
([ADR-018](../../../docs/adr/ADR-018-safe-evidence-and-compile-commit.md)).
Writing the same derivative twice is the same write, which is what makes an ambiguous PUT safe
to repeat and what removes the pending state, the finalization copy, and the compensating
delete along with it. An object written before its compile transaction commits confers no
authority: nothing references it, and it is reachable only through a committed view's opaque
handle.

The port carries plain domain values so infrastructure can implement it without importing the
privacy compiler. The application assembles the compiler's ``SafeEvidenceCandidate`` from what
comes back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from chorus.domain.ids import CaseId, CommunityId, EvidenceItemId, Namespace, Sha256Digest

PRIVATE_OBJECT_VERSION_SEGMENT = "v1"
"""The frozen private-object revision segment; ingestion writes ``.../v1/original``."""

MAX_EVIDENCE_SOURCE_BYTES = 10_000_000
"""The frozen V1 source bound, in bytes exactly.

Not a rounded mebibyte. ``chorus.privacy.compiler`` already refuses a source item above this
exact count at its evidence-safety gate, and a reader that allowed more would let bytes the
compiler will reject reach the decoder first.
"""


def _segment(value: str) -> str:
    """Reject anything that could leave the intended prefix before it becomes a key."""

    if not value or "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("an object key segment is invalid")
    return value


def private_evidence_key(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    evidence_id: EvidenceItemId,
) -> str:
    """Build the frozen private source key. No user-controlled segment exists."""

    return "/".join(
        (
            "ns",
            _segment(namespace.value),
            "community",
            _segment(str(community_id)),
            "case",
            _segment(str(case_id)),
            "evidence",
            _segment(str(evidence_id)),
            PRIVATE_OBJECT_VERSION_SEGMENT,
            "original",
        )
    )


def export_evidence_key(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    derivative_sha256: Sha256Digest,
) -> str:
    """Build the frozen content-addressed export key.

    The address is the digest of the emitted bytes, so two compiles of one photograph name one
    object and an ambiguous PUT is repeatable rather than duplicable.
    """

    digest = derivative_sha256.value.removeprefix("sha256:")
    return "/".join(
        (
            "ns",
            _segment(namespace.value),
            "community",
            _segment(str(community_id)),
            "case",
            _segment(str(case_id)),
            "evidence",
            _segment(digest),
            "content",
        )
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportObjectDescriptor:
    """What a head returns: the frozen metadata, and never the bytes."""

    media_type: str
    byte_length: int
    sha256: Sha256Digest


class ObjectStorePort(Protocol):
    """The complete object surface Phase 6 is permitted to use."""

    async def load_private_evidence(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        evidence_id: EvidenceItemId,
    ) -> bytes:
        """Read one private source object, or raise ``NotFoundError``.

        The key is derived from these identifiers; there is no key parameter. A source larger
        than ``MAX_EVIDENCE_SOURCE_BYTES`` is refused before its bytes are returned.
        """

    async def head_export_evidence(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        derivative_sha256: Sha256Digest,
    ) -> ExportObjectDescriptor | None:
        """Describe the export object at this content address, or ``None`` if absent."""

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
        """**Create** one export derivative at its content address.

        Create-if-absent, not put. An object already at the address raises
        ``PersistenceConflictError`` and is never overwritten -- so two writers racing on the
        same derivative cannot both write, and the loser learns that it lost rather than
        silently clobbering bytes it never compared.

        An ambiguous transport outcome raises ``ExternalDependencyError``; the caller resolves
        it by heading the exact key, never by choosing a different one.
        """
