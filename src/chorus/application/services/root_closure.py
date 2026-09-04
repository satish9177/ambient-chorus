"""Resolve the transitive evidence-root ancestry a case's evidence depends on (ADR-017).

``collapse_evidence_root`` is a *pure domain function over a supplied set*: it walks
``parent_root_id`` to the earliest ancestor and rejects cycles and cross-community ancestry.
It is not modified here and it never loads anything. What it needs is the set, and this module
is the only thing that builds it.

The traversal is deliberately boring. Start from the roots the case's own evidence items name,
load them by identifier, take whichever parents are newly discovered, and repeat until nothing
new appears. Every load is a direct-key pair of batch gets through the root-ID locator, so the
frozen access-pattern statement -- no scan, no GSI, no prefix walk -- holds by construction.

Two failures are indistinguishable from a *shorter* answer, and both must therefore be loud:
an absent locator and a root the locator names but storage does not hold. Both are
``IntegrityError``, quote nothing, and stop the caller before any count is computed. An
under-counted independence result would silently make manufactured corroboration look real,
which is the exact failure ADR-017 exists to prevent.
"""

from __future__ import annotations

from chorus.domain.entities import EvidenceItem, EvidenceRoot
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import EvidenceRootId
from chorus.ports.errors import NotFoundError
from chorus.ports.limits import BATCH_GET_MAX_KEYS
from chorus.ports.repositories import CoreRepositoryPort
from chorus.ports.scopes import CommunityScope

MAX_ROOT_ANCESTRY_LOADS = 16
"""How many batch rounds one closure resolution may take before it is a bug.

An operational bound of the same character as the Monitor prompt module's fence-derivation
limit, and deliberately **not** a policy threshold: it decides nothing about evidence, grants
nothing, and denies nothing that a correct chain would need. The loop terminates naturally
when a round discovers no new parent, and a cycle is rejected downstream by
``collapse_evidence_root``, so exceeding this is a defect rather than a scenario.

Sixteen rounds is far beyond any derivation depth V1 can produce -- ingestion creates only
``ORIGINAL`` roots and the fixture corpus chains one ``FORWARDED`` root onto one parent.
"""

MAX_ROOT_CLOSURE_SIZE = BATCH_GET_MAX_KEYS
"""The largest closure one case may resolve, bounded by the direct-key batch limit.

Each round is one ``BatchGetItem`` pair, so a frontier can never exceed the batch bound; the
whole closure is capped as well, because a case that reached more distinct origins than a
single batch can address has outgrown the frozen per-case limits and must fail closed rather
than start paging an authorization input.
"""


def _distinct(root_ids: tuple[EvidenceRootId, ...]) -> tuple[EvidenceRootId, ...]:
    """Deduplicate while keeping a deterministic order, so two runs load the same keys."""

    seen: set[EvidenceRootId] = set()
    ordered: list[EvidenceRootId] = []
    for root_id in root_ids:
        if root_id not in seen:
            seen.add(root_id)
            ordered.append(root_id)
    return tuple(ordered)


def evidence_root_ids(items: tuple[EvidenceItem, ...]) -> tuple[EvidenceRootId, ...]:
    """The distinct roots a case's evidence items directly name, in a stable order."""

    return _distinct(tuple(item.root_id for item in items))


async def resolve_root_closure(
    core: CoreRepositoryPort,
    scope: CommunityScope,
    root_ids: tuple[EvidenceRootId, ...],
) -> tuple[EvidenceRoot, ...]:
    """Load every root reachable from ``root_ids`` by ``parent_root_id``, or fail closed.

    Returns the closure sorted by identifier so the same case always produces the same tuple,
    which is what lets an independence recomputation be compared across two runs.
    """

    frontier = _distinct(root_ids)
    if not frontier:
        return ()
    resolved: dict[EvidenceRootId, EvidenceRoot] = {}
    for _ in range(MAX_ROOT_ANCESTRY_LOADS):
        if len(resolved) + len(frontier) > MAX_ROOT_CLOSURE_SIZE:
            raise IntegrityError("EVIDENCE_ROOT_CLOSURE")
        try:
            loaded = await core.load_evidence_roots_by_id(scope, frontier)
        except NotFoundError as error:
            # A repository that answers "absent" for a key the closure requires is the same
            # failure as a missing locator, and it is translated rather than propagated so a
            # caller sees one integrity outcome instead of two shapes of the same problem.
            raise IntegrityError("EVIDENCE_ROOT") from error
        for root in loaded:
            resolved[root.root_id] = root
        frontier = _distinct(
            tuple(
                root.parent_root_id
                for root in loaded
                if root.parent_root_id is not None and root.parent_root_id not in resolved
            )
        )
        if not frontier:
            return tuple(sorted(resolved.values(), key=lambda root: str(root.root_id)))
    raise IntegrityError("EVIDENCE_ROOT_ANCESTRY")
