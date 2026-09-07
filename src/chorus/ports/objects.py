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

INBOUND_REPLY_MEDIA_TYPE = "message/rfc822"
"""The one media type an inbound reply object is ever stored under."""

MAX_INBOUND_REPLY_BYTES = 256 * 1024
"""The frozen raw-MIME bound for one inbound reply, in bytes exactly (ADR-026 § 4).

Deliberately far below :data:`MAX_EVIDENCE_SOURCE_BYTES`: a resident may upload a photograph,
and a stranger's email is a text message this system refuses attachments in. The cap is
checked before the bytes are parsed and before anything is written, and a reply that exceeds
it is refused whole with ``REPLY_TOO_LARGE`` and its bytes are not retained.
"""

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


def inbound_reply_key(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    case_id: CaseId,
    raw_sha256: Sha256Digest,
) -> str:
    """Build the frozen content-addressed key for one inbound reply's raw MIME.

    Under a ``reply`` prefix of its own rather than beside resident uploads, so the two are
    distinguishable by address and a bucket policy or a lifecycle rule can name one without
    naming the other.

    **Content-addressed**, like an export derivative and unlike a resident upload: the address
    is the digest of the bytes, so writing the same delivery twice is the same write. That is
    what makes the pre-transaction write safe to repeat after an ambiguous outcome, and it is
    why an object written before its transaction commits confers no authority -- nothing
    references it until an ``EvidenceItem`` does (ADR-018's precedent).
    """

    digest = raw_sha256.value.removeprefix("sha256:")
    return "/".join(
        (
            "ns",
            _segment(namespace.value),
            "community",
            _segment(str(community_id)),
            "case",
            _segment(str(case_id)),
            "reply",
            _segment(digest),
            "content",
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

    async def head_inbound_reply(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        raw_sha256: Sha256Digest,
    ) -> ExportObjectDescriptor | None:
        """Describe the stored raw MIME at this content address, or ``None`` if absent."""

    async def put_inbound_reply(
        self,
        *,
        namespace: Namespace,
        community_id: CommunityId,
        case_id: CaseId,
        raw_sha256: Sha256Digest,
        content: bytes,
    ) -> None:
        """**Create** one raw inbound message at its content address, in the private bucket.

        Create-if-absent, exactly as the export derivative is, and for the same reason: a
        redelivery of one message writes the same bytes to the same address, so an ambiguous
        PUT is repeatable rather than duplicable. An object already there raises
        ``PersistenceConflictError`` and is never overwritten -- the caller heads the exact key
        and decides whether what it found is the same object.

        The media type is not a parameter. There is exactly one, ``message/rfc822``, and a
        caller that could choose it could store a reply as something a later reader would try
        to render.
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
