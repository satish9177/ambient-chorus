"""Deterministic durable identity derived from validated inputs.

This module implements the narrow exception accepted in ADR-011. Normal entities keep UUIDv4
from the injected ``IdGenerator``; five Monitor-derived families -- report, fact slot,
candidate case, evidence root, and the replay-bound apply audit row -- are namespace- and
community-scoped UUIDv5 derived from canonical authoritative input, because the frozen
Monitor apply must complete missing work on a redelivery instead of duplicating committed
work, and random identity makes that unimplementable inside the approved access patterns.

Four rules bound the exception, and every one of them is load-bearing:

* **a separate root per family**, so a report and a fact built from the same tuple of values
  can never collide onto one identifier;
* **RFC 8785 canonical bytes**, so the derivation cannot depend on key order, on how a tuple
  was spelled, or on a datetime's incidental representation;
* **namespace and community first in every payload**, so two communities that observe
  byte-identical text derive different identifiers and a derived identifier is never a
  cross-tenant address;
* **no agent wording, ever.** A summary, title, confidence, reason, client reference, or
  model-chosen typed value must not appear in a derivation payload. Identity comes from
  lineage the application validated: which contributor, which issue type, which messages,
  which evidence, which fact type. A test asserts this by construction.

A derived identifier is not a secret and is not an authorization boundary. Scope validation
on every load remains the boundary; determinism here only decides *where* a write lands.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from chorus.domain.entities import FactType
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    EvidenceRootId,
    FactId,
    MessageId,
    Namespace,
    ReportId,
    Sha256Digest,
)
from chorus.privacy.canonical import canonical_bytes

REPORT_ID_NAMESPACE = UUID("76131664-9cda-5dfd-88b9-afec30a23f96")
FACT_ID_NAMESPACE = UUID("daec9844-8586-5179-8b3d-40413ac7b252")
CASE_ID_NAMESPACE = UUID("6bfec9c1-d336-5133-8cda-81f468991036")
EVIDENCE_ROOT_ID_NAMESPACE = UUID("58374526-1750-5c23-a14d-afbc4224f55b")
AUDIT_EVENT_ID_NAMESPACE = UUID("2f8c1a5e-3b7d-5f61-9c04-6f5b8a91d7e3")
"""Fixed derivation roots, one per entity family.

Separate roots mean a report and a fact built from the same tuple of values can never collide
onto one identifier, and they make the derivation family visible in the code rather than
implied by a shared salt.
"""

_DERIVATION_SCHEMA = "durable-identity/v1"


def _derive(root: UUID, payload: dict[str, object]) -> UUID:
    """Hash a canonical payload into a namespace-scoped UUIDv5.

    RFC 8785 canonical bytes are used so the derivation cannot depend on key order, on how a
    tuple was spelled, or on a datetime's incidental representation.
    """

    name = canonical_bytes({"schema": _DERIVATION_SCHEMA, **payload}).decode("utf-8")
    return uuid5(root, name)


def derive_report_id(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    contributor_id: ContributorId,
    issue_type: str,
    source_message_ids: tuple[MessageId, ...],
) -> ReportId:
    """Identify a report by its owner, its issue, and the exact messages it was drawn from.

    Two invocations that read the same messages the same way therefore propose the *same*
    report, and the create-only write turns the second one into a detected replay instead of
    a second report about one incident.
    """

    return ReportId(
        _derive(
            REPORT_ID_NAMESPACE,
            {
                "namespace": namespace.value,
                "community_id": str(community_id),
                "contributor_id": str(contributor_id),
                "issue_type": issue_type,
                "source_message_ids": tuple(sorted(str(value) for value in source_message_ids)),
            },
        )
    )


def derive_fact_slot_id(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    report_id: ReportId,
    fact_type: FactType,
    source_message_ids: tuple[MessageId, ...],
    evidence_ids: tuple[EvidenceItemId, ...],
) -> FactId:
    """Identify the *slot* a fact occupies: whose report, what kind, from which lineage.

    A slot is the question the fact answers -- "what does this report say about its incident
    occurrence, drawn from these messages and this evidence?" -- and not the answer. The
    model's typed value is deliberately excluded, along with its summary and its confidence.

    That exclusion is the whole point. Deriving identity from the typed value made a
    legitimate re-answer resolve to a *second* address: the same messages, read the same way,
    worded or valued a shade differently by a later invocation, produced a duplicate fact and
    doubled the count of what the case knew. Lineage is stable across re-answers; wording is
    not, and wording is the model's.

    One slot can therefore be re-proposed with different content, and that is a conflict
    rather than a merge. Identical content replays and writes nothing; materially different
    content raises :class:`~chorus.ports.agents.AgentOutputDriftError`, which neither creates
    a second fact nor overwrites the first. Correction is an explicit supersession path with
    a human or a validated assessment behind it, and it has its own identity rules.
    """

    return FactId(
        _derive(
            FACT_ID_NAMESPACE,
            {
                "namespace": namespace.value,
                "community_id": str(community_id),
                "report_id": str(report_id),
                "fact_type": fact_type.value,
                "source_message_ids": tuple(sorted(str(item) for item in source_message_ids)),
                "evidence_ids": tuple(sorted(str(item) for item in evidence_ids)),
            },
        )
    )


def derive_candidate_case_id(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    issue_type: str,
    report_ids: tuple[ReportId, ...],
) -> CaseId:
    """Identify a new candidate case by the exact set of reports that formed it.

    A candidate is the claim "these reports are one problem", so the reports *are* its
    identity. Replaying the same validated proposal reproduces the same case; proposing a
    different grouping proposes a different case rather than mutating this one.
    """

    return CaseId(
        _derive(
            CASE_ID_NAMESPACE,
            {
                "namespace": namespace.value,
                "community_id": str(community_id),
                "issue_type": issue_type,
                "report_ids": tuple(sorted(str(value) for value in report_ids)),
            },
        )
    )


def derive_evidence_root_id(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    root_sha256: Sha256Digest,
) -> EvidenceRootId:
    """Identify an evidence origin by the content it is an origin of.

    The root already lives at a content-addressed key, so deriving its identifier from the
    same content keeps a retried ingest writing the identical item rather than a second
    identity for one origin.
    """

    return EvidenceRootId(
        _derive(
            EVIDENCE_ROOT_ID_NAMESPACE,
            {
                "namespace": namespace.value,
                "community_id": str(community_id),
                "root_sha256": root_sha256.value,
            },
        )
    )


def derive_audit_event_id(
    *,
    namespace: Namespace,
    community_id: CommunityId,
    invocation_id: UUID,
    case_id: CaseId,
) -> UUID:
    """Identify the audit row one invocation writes for one case.

    Audit identifiers are ordinarily random, but this row is part of a transaction that a
    redelivered invocation must be able to re-stage byte for byte. A fresh identifier would
    change the plan on every attempt, so the row is named by the invocation and the case it
    records -- which is exactly what it is.

    Namespace and community lead the payload like every other derivation in this module, and
    for the same ADR-011 reason: a derived identifier must never be an address that two
    tenants can both arrive at. Leaving them out worked only because an invocation identity
    happens to be unique today, which makes tenant separation an accident of another
    component's behaviour rather than a property of this one.
    """

    return _derive(
        AUDIT_EVENT_ID_NAMESPACE,
        {
            "namespace": namespace.value,
            "community_id": str(community_id),
            "invocation_id": str(invocation_id),
            "case_id": str(case_id),
        },
    )


MONITOR_DERIVED_ID_ROOTS: dict[str, UUID] = {
    "REPORT": REPORT_ID_NAMESPACE,
    "FACT_SLOT": FACT_ID_NAMESPACE,
    "CANDIDATE_CASE": CASE_ID_NAMESPACE,
    "EVIDENCE_ROOT": EVIDENCE_ROOT_ID_NAMESPACE,
    "MONITOR_APPLY_AUDIT_EVENT": AUDIT_EVENT_ID_NAMESPACE,
}
"""The complete set of families ADR-011 permits to use a derived identifier.

Kept as data so a test can assert the set has not grown, and so a reviewer can see the whole
exception at once rather than inferring it from which functions happen to exist.
"""
