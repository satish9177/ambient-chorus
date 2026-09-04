"""Building mandate versions, and describing facts to the person who owns them.

Two jobs live here, and they are together because they are two halves of the same boundary.

**Sealing.** A mandate version's ``terms_hash`` covers its own terms and excludes itself, so a
version is built with a placeholder and then sealed by hashing what was built. Doing it in one
place means no caller can construct a version whose hash covers something other than the terms
beside it, and the hash always comes from the one frozen canonicalization module rather than
from an ad-hoc serialization at the call site.

**Wording.** A contributor deciding a mandate has to be told what they are deciding about, and
the honest answer -- the fact's own value -- is exactly the private text the whole system exists
to keep from travelling. So a fact is described from its *closed typed* fields only: an enum, a
calendar date, a media kind. Every free-text field in the fact union is unreachable from this
module. ``summary``, ``detail``, ``display_name``, ``unit_label``, ``statement``,
``action_text``, ``description`` -- none of them is read, and a fact type that carries nothing
but free text is described by what it *is* rather than by what it says.

That is a deliberate loss of fidelity. A contributor sees "a health detail you shared" rather
than the detail. The alternative is a private value crossing into a DTO that a read model
serializes, and from there into a log line, a browser cache, or a screenshot -- for a fact whose
policy ceiling is ``INTERNAL_ONLY`` precisely because it must never travel.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from chorus.domain.entities import DisclosureScope, FactType
from chorus.domain.facts import (
    EvidenceDescription,
    Fact,
    IncidentOccurrence,
    LocationArea,
    ServiceImpact,
)
from chorus.domain.ids import CaseId, ContributorId, MandateId, Sha256Digest
from chorus.domain.mandates import DisclosureMandate, FactGrant, IdentityGrant, MandateDecision
from chorus.privacy.canonical import hash_mandate_terms, hash_value

PLACEHOLDER_TERMS_HASH = Sha256Digest("sha256:" + "0" * 64)
"""The value a version carries between construction and sealing.

Never persisted: :func:`seal` replaces it, and every write path seals. It exists because
``DisclosureMandate`` requires a well-formed digest to be constructible at all, and the digest
cannot be computed until the object it covers exists.
"""


def seal(mandate: DisclosureMandate) -> DisclosureMandate:
    """Return the version with its canonical ``terms_hash`` computed over its own terms."""

    return replace(mandate, terms_hash=hash_mandate_terms(mandate))


# ---------------------------------------------------------------------------------------
# Contributor-facing wording
# ---------------------------------------------------------------------------------------

_LOCKED_BY_POLICY = "policy/v1 never shares this outside the building."

_GENERIC_WORDING: dict[FactType, str] = {
    FactType.IDENTITY_ATTRIBUTE: "Your name.",
    FactType.UNIT_LOCATION: "Your apartment or unit.",
    FactType.HEALTH_DETAIL: "A health detail you shared.",
    FactType.MANAGEMENT_STATEMENT: "A statement attributed to building management.",
    FactType.CONTRADICTION: "A recorded contradiction between statements.",
    FactType.COMMITMENT_TERM: "A follow-up action somebody promised.",
}
"""Wording for fact types described entirely by what they are.

Each of these carries free text as its substance -- a name, a unit label, a health detail, a
quoted statement -- so there is nothing safe to quote back. The sentence names the category and
stops there.
"""

_IMPACT_WORDING: dict[str, str] = {
    "DELAY": "You were delayed.",
    "TRAPPED": "Someone was trapped.",
    "ACCESS_BLOCKED": "Your access was blocked.",
    "OTHER": "Another impact you described.",
}

_AREA_WORDING: dict[str, str] = {
    "LOBBY": "in the lobby",
    "ELEVATOR_CAB": "in the elevator cab",
    "COMMON_AREA": "in a common area",
    "BUILDING": "in the building",
}

_MEDIA_WORDING: dict[str, str] = {
    "IMAGE": "A photo you attached.",
    "EMAIL": "An email you attached.",
    "TEXT": "A text document you attached.",
}


def contributor_wording(fact: Fact) -> str:
    """Describe one fact to its owner using only closed typed fields.

    Every branch reads an enum member or a calendar date. No branch reads a free-text field,
    and the fallback is a category sentence rather than anything derived from the value, so a
    fact type added later is described safely by default instead of leaking until somebody
    notices.
    """

    value = fact.value
    if isinstance(value, IncidentOccurrence):
        day = value.occurred_at.date().isoformat()
        return f"An elevator incident you reported on {day}."
    if isinstance(value, ServiceImpact):
        return _IMPACT_WORDING[value.impact_code.value]
    if isinstance(value, LocationArea):
        return f"Where it happened: {_AREA_WORDING[value.area.value]}."
    if isinstance(value, EvidenceDescription):
        return _MEDIA_WORDING[value.media_kind.value]
    return _GENERIC_WORDING[fact.fact_type]


def locked_reason(ceiling: DisclosureScope) -> str | None:
    """Explain, in one fixed sentence, why a fact can never leave the building."""

    return _LOCKED_BY_POLICY if ceiling is DisclosureScope.INTERNAL_ONLY else None


# ---------------------------------------------------------------------------------------
# Command identity
# ---------------------------------------------------------------------------------------


def decision_request_hash(
    *,
    case_id: CaseId,
    mandate_id: MandateId,
    contributor_id: ContributorId,
    expected_version: int,
    decision: MandateDecision,
    fact_grants: tuple[FactGrant, ...],
    identity_grant: IdentityGrant,
    expires_at: datetime | None,
) -> Sha256Digest:
    """Hash everything that makes two decision requests the same command.

    Fact grants are **sorted** before hashing, for the same reason the canonical terms payload
    sorts them: a client is free to order a JSON array however it likes, and a retry that
    shuffled the array would otherwise be told its own request conflicts with itself. The sort
    key is the fact identifier, which a request cannot restate differently without genuinely
    being a different request.

    The actor is not in the hash because it is already in the idempotency *key*: a key is scoped
    to ``{namespace, command_type, actor}``, so two actors can never collide under one key and
    putting the actor in both places would only make the same statement twice.
    """

    return hash_value(
        {
            "schema": "mandate-decision-request/v1",
            "case_id": case_id,
            "mandate_id": mandate_id,
            "contributor_id": contributor_id,
            "expected_version": expected_version,
            "decision": decision,
            "fact_grants": tuple(sorted(fact_grants, key=lambda grant: str(grant.fact_id))),
            "identity_grant": identity_grant,
            "expires_at": expires_at,
        }
    )


def proposal_request_hash(*, case_id: CaseId, expected_case_version: int) -> Sha256Digest:
    """Hash the whole candidate-acceptance command, which is exactly these two values."""

    return hash_value(
        {
            "schema": "mandate-proposal-request/v1",
            "case_id": case_id,
            "expected_case_version": expected_case_version,
        }
    )


def key_hash(value: str) -> Sha256Digest:
    """Hash a client-supplied idempotency key, because caller text never enters a storage key."""

    return hash_value({"schema": "mandate-command-key/v1", "key": value})
