"""RFC 8785 canonical serialization and authorization hash primitives."""

from __future__ import annotations

import hmac
import unicodedata
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from uuid import UUID

import rfc8785

from chorus.domain.entities import ActionCaveat, ActionClaim, ActionProposal, Approval
from chorus.domain.ids import Sha256Digest, UUIDIdentifier
from chorus.domain.mandates import DisclosureMandate
from chorus.domain.time import format_utc


def _normalize_string(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    normalized.encode("utf-8", errors="strict")
    return normalized


def to_canonical_primitive(value: object, *, omit_fields: frozenset[str] = frozenset()) -> object:
    """Convert a closed typed object into the restricted JCS value domain."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        raise TypeError("floating-point values are forbidden in authorization artifacts")
    if isinstance(value, str):
        return _normalize_string(value)
    if isinstance(value, UUIDIdentifier):
        return str(value)
    if isinstance(value, Sha256Digest):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return format_utc(value)
    if isinstance(value, Enum):
        return _normalize_string(str(value.value))
    if isinstance(value, tuple | list):
        return [to_canonical_primitive(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical maps require string keys")
            normalized_key = _normalize_string(key)
            if normalized_key in result:
                raise TypeError("canonical maps cannot contain duplicate NFC-normalized keys")
            result[normalized_key] = to_canonical_primitive(item)
        return result
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: to_canonical_primitive(getattr(value, item.name))
            for item in fields(value)
            if item.name not in omit_fields
        }
    raise TypeError(f"unsupported canonical value type: {type(value).__name__}")


def canonical_bytes(value: object, *, omit_fields: frozenset[str] = frozenset()) -> bytes:
    """Serialize with the maintained RFC 8785 implementation selected for Phase 1."""

    primitive = to_canonical_primitive(value, omit_fields=omit_fields)
    return rfc8785.dumps(primitive)  # type: ignore[arg-type]


def hash_value(value: object, *, omit_fields: frozenset[str] = frozenset()) -> Sha256Digest:
    """Hash canonical UTF-8 bytes using the frozen digest representation."""

    digest = sha256(canonical_bytes(value, omit_fields=omit_fields)).hexdigest()
    return Sha256Digest(f"sha256:{digest}")


def verify_hash(
    value: object, expected: Sha256Digest, *, omit_fields: frozenset[str] = frozenset()
) -> bool:
    """Constant-time verification of an authorization artifact hash."""

    actual = hash_value(value, omit_fields=omit_fields)
    return hmac.compare_digest(actual.value, expected.value)


def mandate_terms_payload(mandate: DisclosureMandate) -> dict[str, object]:
    """Return exactly the immutable authorization terms covered by terms_hash."""

    return {
        "mandate_id": mandate.mandate_id,
        "version": mandate.version,
        "case_id": mandate.case_id,
        "community_id": mandate.community_id,
        "contributor_id": mandate.contributor_id,
        "namespace": mandate.namespace,
        "fact_grants": tuple(sorted(mandate.fact_grants, key=lambda grant: str(grant.fact_id))),
        "identity_grant": mandate.identity_grant,
        "allowed_destination_ids": tuple(sorted(mandate.allowed_destination_ids, key=str)),
        "allowed_purposes": tuple(sorted(mandate.allowed_purposes, key=str)),
        "valid_from": mandate.valid_from,
        "expires_at": mandate.expires_at,
        "supersedes_version": mandate.supersedes_version,
    }


def hash_mandate_terms(mandate: DisclosureMandate) -> Sha256Digest:
    return hash_value(mandate_terms_payload(mandate))


def hash_action_claim(claim: ActionClaim) -> Sha256Digest:
    return hash_value(claim, omit_fields=frozenset({"claim_hash"}))


def hash_action_caveat(caveat: ActionCaveat) -> Sha256Digest:
    """Seal one structured caveat exactly as a claim is sealed.

    Through the same canonical authority rather than a second recipe, so a caveat and a claim
    of identical text and citations hash by identical rules and neither can drift into a
    weaker one (ADR-021 § 2).
    """

    return hash_value(caveat, omit_fields=frozenset({"caveat_hash"}))


def hash_action_proposal(proposal: ActionProposal) -> Sha256Digest:
    """Seal the whole proposal over every immutable field except its own digest.

    Because the dataclass is walked field by field, this automatically covers the structured
    caveats, ``authorization_version``, and ``preview_hash`` -- so an approval that binds
    ``proposal_hash`` transitively binds the preview a human was shown.
    """

    return hash_value(proposal, omit_fields=frozenset({"proposal_hash"}))


APPROVAL_HASH_OMITTED_FIELDS: frozenset[str] = frozenset(
    {"approval_hash", "version", "created_at", "updated_at"}
)
"""Row bookkeeping, and nothing else (ADR-023 SS 1).

The omit set used to be ``{approval_hash}`` alone, which was correct only for as long as
nobody recomputed the digest. Phase 8 is the phase that must: send-time revalidation exists to
prove that the approval on file is the approval the human made, and a digest that a legitimate
state change invalidates cannot prove anything.

Every field outside this set is written once and never changes, so recomputation is meaningful
at any later instant. The four inside it describe the *row* -- which revision it is and when it
was touched -- rather than the decision, and an approval is never written twice anyway, so the
set is a statement of what the digest is *about* rather than a licence to move anything.
"""


def hash_approval(approval: Approval) -> Sha256Digest:
    """Seal one human decision over every field except row bookkeeping."""

    return hash_value(approval, omit_fields=APPROVAL_HASH_OMITTED_FIELDS)
