"""The policy/v1 review registry: curated fixture metadata, read only.

It projects the manifest's already-verified review records onto the port the compile path uses.
Nothing is computed here. The fixture loader has already proved the corpus matches its
checksums, so a review reaching this registry is one that was curated against bytes the adapter
verified on load.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.domain.ids import EvidenceItemId
from chorus.infrastructure.fixtures.synthetic_feed import EvidenceFixture
from chorus.ports.evidence_review import EvidenceReviewInput


@dataclass(frozen=True, slots=True)
class FixtureEvidenceReviewRegistry:
    """Wraps the verified fixture set; there is no way to add a review to it."""

    _reviews: dict[EvidenceItemId, EvidenceReviewInput]

    @classmethod
    def from_fixtures(cls, fixtures: tuple[EvidenceFixture, ...]) -> FixtureEvidenceReviewRegistry:
        """Project every entry that carries a review; entries without one simply have none."""

        reviews = {
            fixture.evidence_id: EvidenceReviewInput(
                no_face=fixture.review.no_face,
                no_unit=fixture.review.no_unit,
                no_name=fixture.review.no_name,
                no_health=fixture.review.no_health,
                safe_caption=fixture.review.safe_caption,
                reviewed_by=fixture.review.reviewed_by,
                source_sha256=fixture.sha256,
            )
            for fixture in fixtures
            if fixture.review is not None
        }
        return cls(reviews)

    def review_for(self, evidence_id: EvidenceItemId) -> EvidenceReviewInput | None:
        return self._reviews.get(evidence_id)
