"""The safe-evidence review, as an input the application is handed.

A review is an *input artifact*, not a judgement the system makes. That distinction is what
this port exists to make structural: the compile path receives reviews through a read-only
registry with no write method at all, so no code path in V1 can create, amend, or infer one.

In policy/v1 the only implementation is backed by curated fixture metadata, verified against
the source bytes' own digest
([ADR-018](../../../docs/adr/ADR-018-safe-evidence-and-compile-commit.md)).
Adding a real review authority means a new actor, a new write path, and a new mandate question,
and it requires its own ADR.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from chorus.domain.ids import EvidenceItemId, Sha256Digest


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceReviewInput:
    """One curated clearance decision, bound to the exact bytes it was made about.

    ``source_sha256`` is the binding. Without it a review would be attached to an evidence
    identifier, and an identifier can outlive the bytes it named; with it, a clearance can only
    ever apply to the file somebody actually looked at.

    ``reviewed_by`` is curation provenance and is deliberately a plain string rather than a
    ``ContributorId``. Recording a resident identifier here would write into private storage
    the falsehood that a person authorized an export review, which is the same category of
    error ADR-015 refused when it declined to model management as a contributor. It grants no
    authority and is not a verification source.
    """

    no_face: bool
    no_unit: bool
    no_name: bool
    no_health: bool
    safe_caption: str
    reviewed_by: str
    source_sha256: Sha256Digest

    def __post_init__(self) -> None:
        if not 1 <= len(self.safe_caption) <= 300:
            raise ValueError("safe caption length is invalid")
        if not 1 <= len(self.reviewed_by) <= 120:
            raise ValueError("reviewer provenance length is invalid")

    @property
    def cleared(self) -> bool:
        """True only when every frozen clearance holds. Any false flag fails closed."""

        return self.no_face and self.no_unit and self.no_name and self.no_health


class EvidenceReviewRegistryPort(Protocol):
    """Read-only lookup. There is deliberately no method that creates a review."""

    def review_for(self, evidence_id: EvidenceItemId) -> EvidenceReviewInput | None:
        """Return the curated review for this evidence item, or ``None`` when it has none."""
