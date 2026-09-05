"""Turn one approved private image into one durable, content-addressed safe derivative.

This is the composition step ADR-018 describes, and it is the only place in the system that
writes to the export bucket. It does four things in order and nothing else:

1. derive the private key and prove it equals the key the stored ``EvidenceItem`` records;
2. read the source bytes;
3. sanitize them under the frozen profile;
4. put the derivative at its content address, or prove the one already there is the same object.

**It decides nothing about disclosure.** Whether this evidence may leave the building is the
compiler's evidence-safety gate, and whether it was cleared for export is the fixture review.
This module answers only "what are the safe bytes, and are they durable" -- which is why it can
run before the compiler without pre-empting it. A derivative written for a compile that is then
denied is an unreferenced object; harmless, and swept by lifecycle.

**The create-or-verify rule lives here rather than in an adapter** so the in-memory store and
the S3 adapter cannot disagree about it. Both implement raw head and put; the semantics that
make an ambiguous write safe are written once, above the port.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from chorus.domain.entities import EvidenceItem
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import IdGenerator, Sha256Digest
from chorus.ports.errors import ExternalDependencyError, PersistenceConflictError
from chorus.ports.evidence_review import EvidenceReviewInput
from chorus.ports.imaging import ImageSanitizerPort, SafeImage
from chorus.ports.objects import (
    ExportObjectDescriptor,
    ObjectStorePort,
    private_evidence_key,
)
from chorus.ports.scopes import CaseScope
from chorus.privacy.policy import SafeEvidenceCandidate

PHOTO_TRANSFORMATION_RULE_ID = "p1.evidence.photo.v1"
"""The one registered evidence transformation policy/v1 has."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedEvidence:
    """One durable safe derivative and the compiler input it becomes.

    The candidate is what the compiler evaluates; ``byte_length`` is carried beside it for
    the private audit projection, which records the size of what was written without
    recording where it was written.
    """

    candidate: SafeEvidenceCandidate
    byte_length: int

    @property
    def derivative_sha256(self) -> Sha256Digest:
        return self.candidate.derivative_sha256

    @property
    def export_handle_id(self) -> UUID:
        return self.candidate.export_handle_id


class SafeEvidenceRefusedError(IntegrityError):
    """The source cannot become a safe derivative, for a reason that is not policy.

    A missing review, a review that does not clear, a stored key that disagrees with its
    derivation, or a source whose bytes do not match their recorded digest. Each is a statement
    that the private record and the object store disagree, which is an integrity problem rather
    than a disclosure decision -- so it never reaches the compiler as an exclusion reason.
    """

    __slots__ = ()


@dataclass(slots=True)
class PrepareSafeEvidence:
    """Compose one safe derivative from one private object, deterministically."""

    objects: ObjectStorePort
    sanitizer: ImageSanitizerPort
    ids: IdGenerator

    async def prepare(
        self,
        scope: CaseScope,
        item: EvidenceItem,
        review: EvidenceReviewInput | None,
    ) -> PreparedEvidence:
        """Produce the durable derivative for one reviewed evidence item.

        Every refusal is closed: an absent review, an uncleared review, a review bound to
        different bytes, or a stored key that is not the one this scope derives.
        """

        if review is None:
            raise SafeEvidenceRefusedError("SAFE_EVIDENCE:review_absent")
        if not review.cleared:
            raise SafeEvidenceRefusedError("SAFE_EVIDENCE:review_not_cleared")
        if review.source_sha256 != item.sha256:
            # The review describes bytes other than the ones stored. Exporting under it would
            # attach a human clearance to a file nobody cleared.
            raise SafeEvidenceRefusedError("SAFE_EVIDENCE:review_source_mismatch")

        expected_key = private_evidence_key(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            evidence_id=item.evidence_id,
        )
        if item.private_object_key.reveal() != expected_key:
            # The key is derived from typed identifiers and then compared with the stored one,
            # so a corrupted or tampered record cannot redirect this read to another object.
            raise SafeEvidenceRefusedError("SAFE_EVIDENCE:object_key_mismatch")

        source = await self.objects.load_private_evidence(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            evidence_id=item.evidence_id,
        )
        if len(source) != item.byte_length:
            raise SafeEvidenceRefusedError("SAFE_EVIDENCE:source_length_mismatch")

        safe = self.sanitizer.sanitize(source, declared_media_type=item.media_type)
        await self._store(scope, safe)

        return PreparedEvidence(
            candidate=SafeEvidenceCandidate(
                source_evidence_id=item.evidence_id,
                export_handle_id=self.ids.new_uuid(),
                derivative_sha256=safe.sha256,
                caption=review.safe_caption,
                human_reviewed=True,
                transformation_rule_id=PHOTO_TRANSFORMATION_RULE_ID,
            ),
            byte_length=safe.byte_length,
        )

    async def _store(self, scope: CaseScope, safe: SafeImage) -> None:
        """Create the object, or prove the one already there is the same object.

        The write is a *conditional create*, so this is not a head-then-put pair: two compilers
        sanitizing the same photograph would both observe an empty key and both write, and that
        window is exactly what a content-addressed store must not have. The precondition closes
        it inside the service, so at most one writer creates the object and every other writer is
        told one is already there.

        Three outcomes, each with one answer:

        * created -- nothing further to do;
        * already there -- read the exact key, prove it is byte-identical, reuse it;
        * unknown -- read the exact key, because the bytes may or may not have landed and the
          address is the only thing that can settle it.

        A disagreement at a content address is corruption rather than a race, so it is an
        integrity failure and never a silent reuse.
        """

        try:
            await self._put(scope, safe)
        except PersistenceConflictError:
            # The precondition refused the write: an object already occupies this address.
            await self._require_existing_matches(scope, safe)
        except ExternalDependencyError:
            # Unknown outcome. The bytes may have landed and this process cannot tell.
            resolved = await self._head(scope, safe.sha256)
            if resolved is not None:
                self._require_consistent(resolved, safe)
                return
            # Definitely absent, so the identical conditional create is the same write and is
            # safe to repeat exactly once. A second failure is a dependency outage, not a race,
            # and it propagates rather than becoming a retry loop.
            try:
                await self._put(scope, safe)
            except PersistenceConflictError:
                # A concurrent writer created it between that head and this retry.
                await self._require_existing_matches(scope, safe)

    async def _put(self, scope: CaseScope, safe: SafeImage) -> None:
        await self.objects.put_export_evidence(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            derivative_sha256=safe.sha256,
            content=safe.content,
            media_type=safe.media_type,
        )

    async def _require_existing_matches(self, scope: CaseScope, safe: SafeImage) -> None:
        """Prove the object that blocked this create is the one this compile would have made."""

        existing = await self._head(scope, safe.sha256)
        if existing is None:
            # The service refused the create because something was there, and a read at the same
            # address then found nothing. That is not a race this system can reason about.
            raise SafeEvidenceRefusedError("SAFE_EVIDENCE:export_object_vanished")
        self._require_consistent(existing, safe)

    async def _head(
        self, scope: CaseScope, derivative_sha256: Sha256Digest
    ) -> ExportObjectDescriptor | None:
        return await self.objects.head_export_evidence(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            derivative_sha256=derivative_sha256,
        )

    @staticmethod
    def _require_consistent(existing: ExportObjectDescriptor, safe: SafeImage) -> None:
        if (
            existing.byte_length != safe.byte_length
            or existing.media_type != safe.media_type
            or existing.sha256 != safe.sha256
        ):
            raise SafeEvidenceRefusedError("SAFE_EVIDENCE:export_object_inconsistent")
