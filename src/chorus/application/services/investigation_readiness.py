"""The deterministic ``INVESTIGATING -> READY_FOR_ACTION`` predicate, and its compile preflight.

Every term is spelled out here so no part of readiness can be settled by an implementation
judgement, and every term is deterministic:

1. **Validated assessment.** An assessment exists for this case whose ``based_on_case_version``
   equals the case version being transitioned.
2. **Recomputed corroboration.** The case-level independent source count over every ``ACTIVE``
   case fact is at least ``CORROBORATION_MIN``. The number the agent returned is never used.
3. **No material unresolved different-issue finding.** ``linkage_decision`` is ``SAME_ISSUE``
   -- ``DIFFERENT_ISSUES`` blocks and ``UNCERTAIN`` blocks as well, because an ambiguous
   linkage is a missing authorization -- and no validated contradiction carries ``MEDIUM`` or
   ``HIGH`` materiality.
4. **A compilable purpose.** A preflight run of the deterministic privacy compiler returns
   ``ALLOW`` with at least one included fact.

``recommended_case_disposition`` is **not** a term. A model recommending ``READY_FOR_ACTION``
never makes a case ready, and a model recommending ``CONTINUE_INVESTIGATION`` never prevents
one. The only model-originated values the predicate reads are ``linkage_decision`` and
contradiction ``materiality``, both consumed through fixed tables and both able only to block.

The preflight persists nothing
------------------------------
It runs the **existing pure** ``PrivacyCompiler`` -- there is no second policy implementation
here, and there must never be one, because two implementations of an eligibility rule is one
place for them to disagree about what may leave the building.

The compiler is pure by construction: it loads nothing, writes nothing, and returns an artifact
its caller decides what to do with. The caller here decides to do nothing with it. No
``ShareableCaseView``, no current pointer, no history entry, no compile audit row, and no S3
derivative. One boolean survives: whether the result was ``ALLOW`` with at least one included
fact.

It is run against an **ephemeral copy** of the case carrying the freshly recomputed
corroboration count, because gate 17 takes the minimum of the computed count and the stored
one -- and the stored one has not been written yet at the moment readiness is decided. Using
the stale stored value would make a case that has just become corroborated fail its own
readiness check until some later run happened to look again.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid5

from chorus.contracts.investigation import LinkageDecision
from chorus.domain.entities import (
    CommunityCase,
    ContradictionMateriality,
    EvidenceItem,
    EvidenceRoot,
    Purpose,
)
from chorus.domain.facts import Fact, FactStatus, Report
from chorus.domain.ids import IdGenerator, Uuid5Generator
from chorus.domain.mandates import CurrentMandatePointer, DisclosureMandate
from chorus.privacy.compiler import CompileAllow, CompileContext, PrivacyCompiler
from chorus.privacy.policy import (
    CORROBORATION_MIN,
    CompileCommand,
    IntendedUsage,
    Necessity,
    RequestedFact,
    SafeDestination,
)

PREFLIGHT_ID_NAMESPACE = UUID("2f6d1a4c-4f0a-5a3f-9d63-3d5a2f0c7b41")
"""The seed namespace for the ephemeral identifiers a preflight compile mints.

A compile constructs export-fact and view identifiers whatever its caller intends to do with
the result, so the preflight needs an ``IdGenerator``. It is given a deterministic UUIDv5 one
rather than the application's UUIDv4 source, for one reason: nothing the preflight mints is
ever written, so minting from the durable identity source would spend real identifiers on an
artifact that is discarded, and would make two preflights of one unchanged case look different.

This is not the ADR-011 replay-identity exception. Nothing derived here becomes durable state;
these values live inside a discarded object.
"""


class ReadinessReason:
    """The closed ``state_reason_code`` values, evaluated in the frozen order."""

    CORROBORATION_MIN_NOT_MET = "CORROBORATION_MIN_NOT_MET"
    DIFFERENT_ISSUE_UNRESOLVED = "DIFFERENT_ISSUE_UNRESOLVED"
    CONTRADICTION_UNRESOLVED = "CONTRADICTION_UNRESOLVED"
    NO_COMPILABLE_PURPOSE = "NO_COMPILABLE_PURPOSE"
    READY = "EVIDENCE_SUFFICIENT"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadinessOutcome:
    """Whether the case may become ready, and the first term that said otherwise."""

    ready: bool
    reason_code: str
    independent_source_count: int
    linkage_ok: bool
    contradictions_ok: bool
    has_compilable_purpose: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class PreflightInputs:
    """Everything the pure compiler needs, strongly loaded by the caller.

    Deliberately a value rather than a repository handle. The preflight is a *decision*, and a
    decision that could reach storage could also write to it; handing it loaded state instead
    means "persists nothing" is a property of the type rather than a promise in a docstring.
    """

    case: CommunityCase
    community_public_label: str
    facts: tuple[Fact, ...]
    reports: tuple[Report, ...]
    evidence_items: tuple[EvidenceItem, ...]
    evidence_roots: tuple[EvidenceRoot, ...]
    mandates: tuple[DisclosureMandate, ...]
    mandate_pointers: tuple[CurrentMandatePointer, ...]
    destination: SafeDestination
    purpose: Purpose
    requested_at: datetime


def _preflight_id_generator(compile_id: UUID) -> IdGenerator:
    return Uuid5Generator(namespace=compile_id, prefix="investigation-preflight")


def compile_preflight(inputs: PreflightInputs, *, recomputed_source_count: int) -> bool:
    """Return whether some fact of this case could legally be compiled right now.

    Every active case fact is requested as ``OPTIONAL`` with ``CLAIM`` usage. That is the whole
    point of the question being asked: ``REQUIRED`` would make one ineligible fact deny the
    entire compile, which would answer "is this fact exportable" rather than "is *anything*
    exportable". A structural or integrity failure still denies the whole thing, exactly as it
    would at compile time.

    Nothing here is persisted, and nothing here is returned. The result is reduced to one
    boolean at the boundary so no caller can accidentally treat a preflight artifact as a
    compiled view.
    """

    active = tuple(fact for fact in inputs.facts if fact.status is FactStatus.ACTIVE)
    if not active:
        return False
    compile_id = uuid5(
        PREFLIGHT_ID_NAMESPACE,
        f"{inputs.case.namespace.value}:{inputs.case.case_id}:{inputs.case.version}",
    )
    command = CompileCommand(
        compile_id=compile_id,
        namespace=inputs.case.namespace,
        case_id=inputs.case.case_id,
        expected_case_version=inputs.case.version,
        requested_facts=tuple(
            RequestedFact(
                fact_id=fact.fact_id,
                necessity=Necessity.OPTIONAL,
                intended_usage=IntendedUsage.CLAIM,
            )
            for fact in active
        ),
        requested_evidence_ids=(),
        destination=inputs.destination,
        purpose=inputs.purpose,
        requested_at=inputs.requested_at,
    )
    context = CompileContext(
        case=replace(inputs.case, corroboration_source_count=recomputed_source_count),
        community_public_label=inputs.community_public_label,
        facts=inputs.facts,
        reports=inputs.reports,
        evidence_items=inputs.evidence_items,
        evidence_roots=inputs.evidence_roots,
        mandates=inputs.mandates,
        mandate_pointers=inputs.mandate_pointers,
        destination_registry_entry=inputs.destination,
    )
    compiler = PrivacyCompiler(id_generator_factory=_preflight_id_generator)
    result = compiler.compile(command, context)
    if not isinstance(result, CompileAllow):
        return False
    return len(result.included) >= 1


def evaluate_readiness(
    *,
    independent_source_count: int,
    linkage_decision: LinkageDecision,
    contradiction_materialities: tuple[ContradictionMateriality, ...],
    has_compilable_purpose: bool,
) -> ReadinessOutcome:
    """Apply the four terms in the frozen order and name the first one that fails.

    The order is part of the contract, not a convenience: the ``state_reason_code`` a blocked
    case carries has to be the same code on every run, and evaluating the cheapest term first
    or short-circuiting differently would make the recorded reason depend on the arithmetic
    rather than on the case.

    A validated assessment bound to the current case version is the *caller's* precondition --
    it is the thing that produced these arguments -- and the transaction's version condition is
    what proves it still holds at the moment of the write.
    """

    linkage_ok = linkage_decision is LinkageDecision.SAME_ISSUE
    contradictions_ok = not any(
        materiality in {ContradictionMateriality.MEDIUM, ContradictionMateriality.HIGH}
        for materiality in contradiction_materialities
    )
    if independent_source_count < CORROBORATION_MIN:
        reason = ReadinessReason.CORROBORATION_MIN_NOT_MET
    elif not linkage_ok:
        reason = ReadinessReason.DIFFERENT_ISSUE_UNRESOLVED
    elif not contradictions_ok:
        reason = ReadinessReason.CONTRADICTION_UNRESOLVED
    elif not has_compilable_purpose:
        reason = ReadinessReason.NO_COMPILABLE_PURPOSE
    else:
        reason = ReadinessReason.READY
    return ReadinessOutcome(
        ready=reason == ReadinessReason.READY,
        reason_code=reason,
        independent_source_count=independent_source_count,
        linkage_ok=linkage_ok,
        contradictions_ok=contradictions_ok,
        has_compilable_purpose=has_compilable_purpose,
    )
