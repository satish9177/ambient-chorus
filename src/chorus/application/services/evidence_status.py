"""Deterministic evidence-status computation and the downgrade-only ladder (ADR-015).

``EvidenceStatus`` is the one lifecycle enum in the domain with **no edge set**. It is a pure
classification recomputed from deterministic inputs every time an assessment is applied, in the
manner of the derived ``SUPERSEDED`` and ``EXPIRED`` mandate statuses: there is no ordering for
a defect to walk, and no stored "current status" constraining what the next honest
recomputation may say.

Everything here is pure over loaded state. It takes no repository, performs no I/O, and reads
no clock, so the same case always classifies the same way and a test can pin the whole rule
without a driver.

Two quantities, one function
----------------------------
Case corroboration and fact corroboration are computed by the *same* frozen independence
function over *different* fact sets, and they must never be substituted for one another:

* **case** corroboration runs over every ``ACTIVE`` fact of the case. It sets
  ``CommunityCase.corroboration_source_count``, feeds the readiness guard, and is rechecked by
  compiler gate 17;
* **fact** corroboration runs over one exact canonical claim group. It sets that fact's
  ``evidence_status``, which travels outward on ``ShareableFact``.

A case can be corroborated while most of its facts remain ``REPORTED``. That is the correct and
expected shape: several people reporting an elevator problem corroborates *the case*, while the
specific minute one of them was trapped remains one person's account.

Grouping is canonical bytes, never meaning
------------------------------------------
``fact_support_key`` is ``(fact_type, RFC 8785 hash of the stored typed value)``. No semantic
similarity, no embeddings, no fuzzy time window, no LLM equivalence. Different typed values are
different claims, full stop.

That is decidable, reproducible, and impossible for a model to influence, because the value it
groups on is the *stored* typed value deterministic Monitor validation already accepted. The
cost is real and accepted: exact equality means a free-text impact summary will essentially
never group across contributors. The alternative is a similarity threshold, and a threshold
that makes more facts look corroborated is a threshold that makes weaker evidence look
stronger.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.domain.entities import EvidenceItem, EvidenceRoot, EvidenceStatus, FactType
from chorus.domain.facts import Fact, FactStatus, Report, independent_source_count
from chorus.domain.ids import FactId, Sha256Digest
from chorus.privacy.canonical import hash_value
from chorus.privacy.policy import CORROBORATION_MIN

ALLOWED_VERIFICATION_SOURCES: frozenset[str] = frozenset()
"""The allowed verification source set for policy/v1: **empty**, deliberately and completely.

A ``CLEAN`` malware scan is not a member. A clean scan is a statement about *bytes* -- a file
was accepted into storage without matching a known-bad signature -- and never about the world.
A photograph of an out-of-service sign that passes a scan proves a photograph exists; it does
not prove the elevator failed, when it failed, or that the submitter was in the building.
Treating scan status as verification would let anyone who can attach a clean file mint the
strongest status in the system, inverting the authority hierarchy.

The one other candidate, an authenticated reply from the property manager, is not a member
either, because V1 cannot currently *prove* that provenance: ``EvidenceItem`` carries no
durable binding to an approved destination, and a bare "this was a reply" flag records that
something was called a reply without recording who sent it, which is not authentication.

Two consequences follow and both are intended: deterministically computed ``VERIFIED`` is
unreachable, and every model-proposed ``VERIFIED`` is downgraded. This is a complete policy/v1
outcome rather than a deferral -- V1 never claims to have verified anything, which is an honest
description of what V1 can establish. Adding a member is a separate, explicit ADR that must
state the *limit* as well as the grant; it never happens as a side effect of a later phase
adding an authentication binding, because "who wrote this" and "what may this establish" are
different questions.
"""

_LADDER: tuple[EvidenceStatus, ...] = (
    EvidenceStatus.VERIFIED,
    EvidenceStatus.CORROBORATED,
    EvidenceStatus.REPORTED,
    EvidenceStatus.UNKNOWN,
)
"""The confidence order for non-contradiction statuses, strongest first.

``CONTRADICTED`` is deliberately absent. It is off the ladder entirely and is neither raisable
nor lowerable: a model that proposed the contradiction does not then get to soften its
consequence, and a model that did not propose one cannot invent the consequence by naming the
status.
"""

_LADDER_RANK: dict[EvidenceStatus, int] = {status: rank for rank, status in enumerate(_LADDER)}


class StatusReason:
    """The closed reason codes an ``EvidenceFinding`` may carry.

    Codes only. A reason never carries the fact's value, the model's rationale, or any part of
    the private text the classification was about.
    """

    CONTRADICTION_CITED = "CONTRADICTION_CITED"
    MULTIPLE_INDEPENDENT_SOURCES = "MULTIPLE_INDEPENDENT_SOURCES"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    MODEL_LOWERED_STATUS = "MODEL_LOWERED_STATUS"


EVIDENCE_STATUS_OVERCLAIM_DOWNGRADED = "EVIDENCE_STATUS_OVERCLAIM_DOWNGRADED"
"""The audit reason recorded when a model proposed a status stronger than the computed one.

Not a contract violation and not a rejection. An over-confident status is a *judgement* about
real, correctly cited facts -- the citations are valid, the contradictions may be sound, the
gaps may be exactly right -- so the finding is downgraded, the overclaim is audited, and the
assessment persists unchanged in every other respect. Discarding a whole skeptical assessment
over one field would spend another pass over private text to be told most of the same thing.
"""


def fact_support_key(fact: Fact) -> tuple[FactType, Sha256Digest]:
    """The exact deterministic claim a fact makes: its type and its canonical value digest.

    Two facts corroborate one another only when this key is identical, which means two
    contributors asserted the byte-identical closed value after RFC 8785 canonicalization.
    """

    return (fact.fact_type, hash_value(fact.value))


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedStatus:
    """One fact's final status, why it has it, and whether the model overreached."""

    fact_id: FactId
    computed: EvidenceStatus
    proposed: EvidenceStatus | None
    resolved: EvidenceStatus
    reason_code: str
    overclaimed: bool


def compute_statuses(
    *,
    facts: tuple[Fact, ...],
    reports: tuple[Report, ...],
    evidence_items: tuple[EvidenceItem, ...],
    roots: tuple[EvidenceRoot, ...],
    contradicted_fact_ids: frozenset[FactId],
) -> dict[FactId, EvidenceStatus]:
    """Classify every ``ACTIVE`` fact of one case. Three outcomes, in this exact order.

    ``CONTRADICTED`` is evaluated first, so it outranks every other outcome. A management
    statement could one day be verified *as uttered* and contradicted *as to truth* at the same
    time, and ``EvidenceStatus`` holds one value; rendering the stronger label beside a
    statement the case's own evidence contradicts would be the most misleading thing this
    system is capable of emitting, and it travels outward on ``ShareableFact``.

    ``VERIFIED`` is never produced, because the allowed verification source set is empty.
    ``UNKNOWN`` is never produced either: it is reachable only as a *resolved* status, when the
    Investigator legitimately lowers a computed value through the ladder. A future deterministic
    ``UNKNOWN``-producing condition requires an explicit documented rule in a superseding ADR,
    never an implementation judgement.
    """

    active = tuple(fact for fact in facts if fact.status is FactStatus.ACTIVE)
    groups: dict[tuple[FactType, Sha256Digest], list[Fact]] = {}
    for fact in active:
        groups.setdefault(fact_support_key(fact), []).append(fact)

    corroborated_keys: set[tuple[FactType, Sha256Digest]] = set()
    for key, group in groups.items():
        group_count = independent_source_count(tuple(group), reports, evidence_items, roots)
        if group_count >= CORROBORATION_MIN:
            corroborated_keys.add(key)

    statuses: dict[FactId, EvidenceStatus] = {}
    for fact in active:
        if fact.fact_id in contradicted_fact_ids:
            statuses[fact.fact_id] = EvidenceStatus.CONTRADICTED
        elif fact_support_key(fact) in corroborated_keys:
            statuses[fact.fact_id] = EvidenceStatus.CORROBORATED
        else:
            statuses[fact.fact_id] = EvidenceStatus.REPORTED
    return statuses


def _computed_reason(computed: EvidenceStatus) -> str:
    if computed is EvidenceStatus.CONTRADICTED:
        return StatusReason.CONTRADICTION_CITED
    if computed is EvidenceStatus.CORROBORATED:
        return StatusReason.MULTIPLE_INDEPENDENT_SOURCES
    return StatusReason.SINGLE_SOURCE


def resolve_status(
    fact_id: FactId, computed: EvidenceStatus, proposed: EvidenceStatus | None
) -> ResolvedStatus:
    """Apply the downgrade-only ladder: the model may lower, and may never raise (SEC-21).

    ::

        resolved(f) = CONTRADICTED                        if computed(f) is CONTRADICTED
                    = weaker_of(computed(f), proposed(f)) otherwise

    One rule does two jobs the plan asked for separately -- "preserve ``UNKNOWN``" and "the
    model cannot grant ``VERIFIED``" are the same sentence read in two directions.

    A proposed ``CONTRADICTED`` is inert. It is not on the ladder, it names no cited facts, and
    there is nothing a validator could check it against; ``contradictions[]`` is the only path
    to that status. Conversely a fact inside a validated contradiction resolves to
    ``CONTRADICTED`` **regardless** of what its finding proposed, including ``REPORTED`` and
    ``UNKNOWN``, because the ladder does not apply to ``CONTRADICTED`` in either direction.
    """

    if computed is EvidenceStatus.CONTRADICTED:
        return ResolvedStatus(
            fact_id=fact_id,
            computed=computed,
            proposed=proposed,
            resolved=EvidenceStatus.CONTRADICTED,
            reason_code=StatusReason.CONTRADICTION_CITED,
            overclaimed=False,
        )
    if proposed is None or proposed is EvidenceStatus.CONTRADICTED:
        return ResolvedStatus(
            fact_id=fact_id,
            computed=computed,
            proposed=proposed,
            resolved=computed,
            reason_code=_computed_reason(computed),
            overclaimed=False,
        )
    if _LADDER_RANK[proposed] > _LADDER_RANK[computed]:
        # Weaker on the ladder: the model is being more careful than the arithmetic, which is
        # exactly the influence a skeptic is allowed to have.
        return ResolvedStatus(
            fact_id=fact_id,
            computed=computed,
            proposed=proposed,
            resolved=proposed,
            reason_code=StatusReason.MODEL_LOWERED_STATUS,
            overclaimed=False,
        )
    return ResolvedStatus(
        fact_id=fact_id,
        computed=computed,
        proposed=proposed,
        resolved=computed,
        reason_code=_computed_reason(computed),
        # Equal is not an overclaim; only strictly stronger is, and a strictly stronger
        # proposal is discarded and audited rather than allowed to move anything.
        overclaimed=_LADDER_RANK[proposed] < _LADDER_RANK[computed],
    )
