"""Deterministic validation of one Action proposal, in the frozen twelve-check order.

Schema validity is not truth and not authorization. By the time an answer reaches this module
Pydantic has proved it is *well formed*; everything here proves it is about the exact view that
was actually sent, that every factual token in it was already stated by a cited safe fact, and
that nothing it contains could reach an external recipient uncited.

The order is normative and it is the order the frozen contract states:

1. schema, bounds, and closed enums -- done by the contract type before this module runs;
2. exact case, view, version, destination, purpose, and expiry;
3. recomputed view hash and current-pointer equality;
4. claim- and caveat-ID uniqueness and count;
5. exact export-fact membership, and 1-10 citations on every citation set;
6. every request and caveat carries citations **by schema**, so there is nothing to re-derive;
6a. every relied-upon ``CONTRADICTED`` fact is caveated;
7. foreign or unexpected identifiers are a whole-proposal contract violation;
8. subject 1-120 with no control characters;
9. injection, sensitive, and prohibited prose constructs;
10. lexical grounding: risk-token support and proper-name support;
11. duplicate normalized claim and caveat text;
12. canonical hashes and persistence preconditions -- done by the caller, which owns the
    transaction.

Every failure refuses the **whole** proposal under a bounded :class:`ActionRejection` code.
There is no per-claim salvage, nothing is repaired silently, and a false positive is answered
by a re-proposal rather than by a bypass, an override, or a second model's opinion.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from chorus.application.services.action_grounding import (
    TEMPLATE_COPY_ALLOWLIST,
    GroundingRejection,
    ground_field,
    normalize,
)
from chorus.contracts.action import (
    ACTION_PROMPT_VERSION,
    MAX_CAVEATS,
    MAX_CLAIMS,
    ActionProposalDraft,
)
from chorus.contracts.common import AgentName
from chorus.domain.entities import ActionTone, EvidenceStatus, Purpose
from chorus.domain.ids import DestinationId, Namespace
from chorus.ports.agents import (
    ActionInvocation,
    ActionRejection,
    ActionResult,
    AgentContractViolationError,
)
from chorus.ports.records import StoredShareableFact, StoredShareableView

_GROUNDING_TO_REJECTION: dict[GroundingRejection, ActionRejection] = {
    GroundingRejection.ENCODING_INVALID: ActionRejection.REJECTED_CONSTRUCT,
    GroundingRejection.CONTROL_CHARACTER: ActionRejection.REJECTED_CONSTRUCT,
    GroundingRejection.BIDI_CONTROL: ActionRejection.REJECTED_CONSTRUCT,
    GroundingRejection.INVISIBLE_FORMATTING: ActionRejection.REJECTED_CONSTRUCT,
    GroundingRejection.MARKUP: ActionRejection.REJECTED_CONSTRUCT,
    GroundingRejection.MARKDOWN_LINK: ActionRejection.REJECTED_CONSTRUCT,
    GroundingRejection.URL_PATTERN: ActionRejection.REJECTED_CONSTRUCT,
    GroundingRejection.MAILTO_PATTERN: ActionRejection.MAILTO_PATTERN,
    GroundingRejection.EMAIL_PATTERN: ActionRejection.REJECTED_CONSTRUCT,
    GroundingRejection.PHONE_PATTERN: ActionRejection.PHONE_PATTERN,
    GroundingRejection.UNIT_PATTERN: ActionRejection.REJECTED_CONSTRUCT,
    GroundingRejection.IDENTIFIER_SHAPE: ActionRejection.REJECTED_CONSTRUCT,
    GroundingRejection.QUOTATION: ActionRejection.REJECTED_CONSTRUCT,
    GroundingRejection.SENSITIVE_TERM: ActionRejection.SENSITIVE_TERM,
    GroundingRejection.REJECTED_DATE_CONSTRUCT: ActionRejection.REJECTED_CONSTRUCT,
    GroundingRejection.UNSUPPORTED_TOKEN: ActionRejection.UNSUPPORTED_TOKEN,
    GroundingRejection.UNSUPPORTED_NAME: ActionRejection.UNSUPPORTED_NAME,
}
"""Map the grounding grammar's codes onto the transport-level closed set.

``MAILTO_PATTERN`` and ``PHONE_PATTERN`` keep their own transport codes because the frozen
``ActionRejection`` set names them individually -- they are the two rules whose absence would be
hardest to notice from a generic "rejected construct".
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedProposal:
    """Exactly what deterministic code is prepared to persist from one Action answer.

    Deliberately not the draft itself. A caller holding the draft could reach a field the
    validator declined to bless; a caller holding this holds only values that survived every
    check, in the domain's own types.
    """

    subject: str
    claims: tuple[tuple[UUID, str, tuple[UUID, ...]], ...]
    requested_action: str
    requested_deadline: datetime | None
    """The validated instant itself, not merely whether one was given.

    It is carried because the bound it must satisfy -- strictly after
    ``view.generated_at`` (ADR-021 § 10) -- is checked here, and a caller handed only a
    boolean would have to reach back into the raw draft for the value that was checked.
    """
    request_fact_ids: tuple[UUID, ...]
    caveats: tuple[tuple[UUID, str, tuple[UUID, ...]], ...]
    tone: ActionTone

    @property
    def relied_fact_ids(self) -> frozenset[UUID]:
        """Claim citations union request citations -- the facts the proposal actually leans on.

        Caveat citations are deliberately absent. A caveat citing a contradicted fact does not
        itself create a further obligation, because the only fixed point of a recursive rule
        would be an infinite regress or an arbitrary depth limit (ADR-021 § 3).
        """

        return frozenset(
            {fact_id for _, _, citations in self.claims for fact_id in citations}
            | set(self.request_fact_ids)
        )


class _Rejections:
    """Collects reasons so one pass reports every distinct failure it found.

    Every reason is a bounded code and none carries the offending text. That is what lets a
    rejection be logged, audited, and counted without a redaction rule of its own -- and it is
    why a caller can safely put ``reason_codes`` straight into an operation record.
    """

    __slots__ = ("_reasons",)

    def __init__(self) -> None:
        self._reasons: list[ActionRejection] = []

    def add(self, reason: ActionRejection) -> None:
        self._reasons.append(reason)

    def extend(self, reasons: tuple[ActionRejection, ...]) -> None:
        self._reasons.extend(reasons)

    @property
    def any(self) -> bool:
        return bool(self._reasons)

    def raise_if_any(self) -> None:
        if self._reasons:
            raise AgentContractViolationError(tuple(dict.fromkeys(self._reasons)))


def validate_action_result(
    *,
    invocation: ActionInvocation,
    result: ActionResult,
    view: StoredShareableView,
    namespace: Namespace,
    destination_id: DestinationId,
    purpose: Purpose,
    expected_view_hash: str,
) -> ValidatedProposal:
    """Validate one Action answer end to end, or refuse all of it.

    ``view`` is the exact stored artifact the invocation was built from, loaded strongly by the
    caller. It is passed rather than re-derived so the citation-membership check runs against
    what is *persisted*, not against what this process happened to project.
    """

    rejections = _Rejections()
    draft = result.output

    # -- 1/2. envelope, prompt identity, and the exact artifact this answer is about --------
    _check_envelope(invocation, result, namespace, rejections)
    _check_binding(draft, view, destination_id, purpose, expected_view_hash, rejections)
    # A proposal about a different view cannot be meaningfully grounded against this one, and
    # continuing would produce a long list of citation failures describing one mismatch.
    rejections.raise_if_any()

    # -- 4/5/7. identity, counts, and citation membership -----------------------------------
    known = {fact.export_fact_id.value for fact in view.shareable_facts}
    _check_structure(draft, known, rejections)

    # -- 8. the subject's own bound, restated here rather than trusted from the schema -------
    if not 1 <= len(draft.subject) <= 120:
        rejections.add(ActionRejection.SUBJECT_INVALID)

    # -- 10a. the deadline's lower bound, which only this layer can check --------------------
    _check_deadline(draft, view, rejections)

    # -- 9/10. structural rejection and lexical grounding, on the four prose fields ----------
    by_fact = {fact.export_fact_id.value: fact for fact in view.shareable_facts}
    _check_prose(draft, by_fact, view, rejections)

    # -- 11. duplicate normalized text ------------------------------------------------------
    _check_duplicates(draft, rejections)

    # -- 6a. the LOW-contradiction caveat obligation ----------------------------------------
    _check_contradiction_caveats(draft, by_fact, rejections)

    rejections.raise_if_any()
    return ValidatedProposal(
        subject=draft.subject,
        claims=tuple((claim.claim_id, claim.text, claim.export_fact_ids) for claim in draft.claims),
        requested_action=draft.request.requested_action,
        requested_deadline=draft.request.requested_deadline,
        request_fact_ids=draft.request.request_fact_ids,
        caveats=tuple(
            (caveat.caveat_id, caveat.text, caveat.export_fact_ids) for caveat in draft.caveats
        ),
        tone=ActionTone(draft.tone.value),
    )


def _check_envelope(
    invocation: ActionInvocation,
    result: ActionResult,
    namespace: Namespace,
    rejections: _Rejections,
) -> None:
    """Prove the answer belongs to this invocation, this agent, and the reviewed prompt.

    The prompt version is refused *once, by version*, rather than field by field. A runtime
    serving an older artifact is running text this application did not review, and its answer
    is not a partially usable one.
    """

    if result.prompt_version != ACTION_PROMPT_VERSION:
        rejections.add(ActionRejection.PROMPT_VERSION_MISMATCH)
    if result.agent_name is not AgentName.ACTION:
        rejections.add(ActionRejection.ENVELOPE_MISMATCH)
    if result.invocation_id != invocation.invocation_id:
        rejections.add(ActionRejection.ENVELOPE_MISMATCH)
    if result.namespace != namespace.value or invocation.namespace != namespace.value:
        rejections.add(ActionRejection.ENVELOPE_MISMATCH)
    if result.case_id != invocation.case_id or result.case_version != invocation.case_version:
        rejections.add(ActionRejection.ENVELOPE_MISMATCH)


def _check_binding(
    draft: ActionProposalDraft,
    view: StoredShareableView,
    destination_id: DestinationId,
    purpose: Purpose,
    expected_view_hash: str,
    rejections: _Rejections,
) -> None:
    """Exact case, view, both versions, destination, purpose, and the recomputed hash.

    ``authorization_version`` is the freshness comparison and ``case_version`` is provenance;
    both are checked for exact equality here because the model was *given* both and an answer
    that returned a different one is describing a different artifact either way.
    """

    if draft.case_id != view.case_id.value:
        rejections.add(ActionRejection.VIEW_MISMATCH)
    if draft.view_id != view.view_id.value or draft.view_hash != view.view_hash.value:
        rejections.add(ActionRejection.VIEW_MISMATCH)
    if draft.case_version != view.case_version:
        rejections.add(ActionRejection.VIEW_MISMATCH)
    if draft.authorization_version != view.authorization_version:
        rejections.add(ActionRejection.STALE_VIEW)
    if view.destination.destination_id != destination_id or view.purpose is not purpose:
        rejections.add(ActionRejection.VIEW_MISMATCH)
    if view.view_hash.value != expected_view_hash:
        # The caller recomputed the hash over the stored artifact; a disagreement means the
        # persisted view does not hash to what its own field claims.
        rejections.add(ActionRejection.VIEW_MISMATCH)


def _check_deadline(
    draft: ActionProposalDraft, view: StoredShareableView, rejections: _Rejections
) -> None:
    """``requested_deadline`` must be strictly after ``view.generated_at`` (ADR-021 § 10).

    The contract type owns the *shape* -- a timezone-aware UTC instant -- and stops there,
    because a contract cannot see the view. This layer owns the *comparison*, and it is the
    only layer that can: the bound is a property of the artifact the proposal is made against.

    ``None`` remains legal. The schema permits an absent deadline and the renderer has fixed
    copy for it, so a request that asks for no particular date is a request, not a defect.

    Equality is a rejection. A deadline exactly at the instant the view was generated asks the
    recipient to have acted before they were told, which is not a coherent requirement and is
    the same "equality means expired" direction the view and mandate expiry rules already take.
    """

    deadline = draft.request.requested_deadline
    if deadline is None:
        return
    if deadline <= view.generated_at:
        rejections.add(ActionRejection.DEADLINE_NOT_AFTER_VIEW)


def _check_structure(draft: ActionProposalDraft, known: set[UUID], rejections: _Rejections) -> None:
    """Counts, identifier uniqueness, and exact export-fact membership.

    An identifier that names nothing in this exact view is a whole-proposal ``FOREIGN_IDENTIFIER``
    and never a dropped citation: a model that cited one fact it was never given has shown that
    the rest of its reading is unverified too.
    """

    if not 1 <= len(draft.claims) <= MAX_CLAIMS:
        rejections.add(ActionRejection.OUTPUT_EXCEEDS_BOUNDS)
    if len(draft.caveats) > MAX_CAVEATS:
        rejections.add(ActionRejection.OUTPUT_EXCEEDS_BOUNDS)

    claim_ids = [claim.claim_id for claim in draft.claims]
    caveat_ids = [caveat.caveat_id for caveat in draft.caveats]
    if len(set(claim_ids)) != len(claim_ids) or len(set(caveat_ids)) != len(caveat_ids):
        rejections.add(ActionRejection.DUPLICATE_CLAIM_ID)
    if set(claim_ids) & set(caveat_ids):
        rejections.add(ActionRejection.DUPLICATE_CLAIM_ID)

    citation_sets: list[tuple[UUID, ...]] = [claim.export_fact_ids for claim in draft.claims]
    citation_sets.append(draft.request.request_fact_ids)
    citation_sets.extend(caveat.export_fact_ids for caveat in draft.caveats)
    for citations in citation_sets:
        if not citations:
            # Unreachable through the contract type, which bounds every set at 1..10. Restated
            # because this is the check the frozen order names, and a schema is not the place a
            # security invariant should live alone.
            rejections.add(ActionRejection.EMPTY_CITATION_SET)
        if not 1 <= len(citations) <= 10:
            rejections.add(ActionRejection.OUTPUT_EXCEEDS_BOUNDS)
        if any(fact_id not in known for fact_id in citations):
            rejections.add(ActionRejection.UNKNOWN_EXPORT_FACT_ID)
            rejections.add(ActionRejection.FOREIGN_IDENTIFIER)


def _support_for(
    citations: tuple[UUID, ...], by_fact: Mapping[UUID, StoredShareableFact]
) -> tuple[str, ...]:
    """The ``safe_text`` of exactly the facts this field cited, in citation order."""

    texts: list[str] = []
    for fact_id in citations:
        fact = by_fact.get(fact_id)
        if fact is not None:
            texts.append(fact.safe_text)
    return tuple(texts)


def _check_prose(
    draft: ActionProposalDraft,
    by_fact: Mapping[UUID, StoredShareableFact],
    view: StoredShareableView,
    rejections: _Rejections,
) -> None:
    """Run §5, §6, and §7 over the four model-authored textual fields, and nothing else.

    The typed structural identifiers are never fed to the prose scanner. They are required
    contract fields validated for view and case membership by the checks above; reading
    "identifiers are rejected outright" as a rule about the whole payload would make the
    contract reject its own required identifiers (ADR-021 § 5).

    ``subject`` has no citation field of its own, so its support context is the frozen union of
    every claim's citations, the request's citations, the two view labels, and the reviewed
    template copy (ADR-021 § 8).
    """

    labels = (view.destination.display_label, view.community_public_label)
    template_copy = tuple(sorted(TEMPLATE_COPY_ALLOWLIST))

    subject_citations = tuple(
        dict.fromkeys(
            [fact_id for claim in draft.claims for fact_id in claim.export_fact_ids]
            + list(draft.request.request_fact_ids)
        )
    )
    fields: list[tuple[str, tuple[UUID, ...]]] = [(draft.subject, subject_citations)]
    fields.extend((claim.text, claim.export_fact_ids) for claim in draft.claims)
    fields.append((draft.request.requested_action, draft.request.request_fact_ids))
    fields.extend((caveat.text, caveat.export_fact_ids) for caveat in draft.caveats)

    for text, citations in fields:
        support = _support_for(citations, by_fact)
        outcome = ground_field(
            text,
            token_support=support,
            name_support=(*support, *labels, *template_copy),
        )
        rejections.extend(tuple(_GROUNDING_TO_REJECTION[reason] for reason in outcome.rejections))


def _check_duplicates(draft: ActionProposalDraft, rejections: _Rejections) -> None:
    """Two claims, or two caveats, whose comparison-normalized texts are equal.

    Equal *citation sets* across two claims with different normalized text stay legal: two
    distinct statements may rest on the same fact, and forbidding that would push a model
    toward padding a second claim with an unsupported detail to make it look different.
    """

    claim_texts = [normalize(claim.text) for claim in draft.claims]
    caveat_texts = [normalize(caveat.text) for caveat in draft.caveats]
    if len(set(claim_texts)) != len(claim_texts):
        rejections.add(ActionRejection.DUPLICATE_NORMALIZED_TEXT)
    if len(set(caveat_texts)) != len(caveat_texts):
        rejections.add(ActionRejection.DUPLICATE_NORMALIZED_TEXT)


def _check_contradiction_caveats(
    draft: ActionProposalDraft,
    by_fact: Mapping[UUID, StoredShareableFact],
    rejections: _Rejections,
) -> None:
    """Discharge the obligation ADR-015 § 7 left to this phase.

    ``relied_fact_ids`` is claim citations union request citations. A contradicted fact the
    proposal actually asserts or leans on must be caveated by a caveat citing that exact
    ``export_fact_id``. A contradicted fact that merely sits in the view and which the proposal
    never mentions does **not** force the message to raise a doubt about a claim it declined to
    make -- requiring that would make every proposal introduce material it had chosen to omit.

    ``ShareableFact.evidence_status`` is sufficient because ``CONTRADICTED`` is sticky through
    every compiler aggregation. Contradiction *materiality* is never loaded here: Phase 5 is
    the sole authority that ``MEDIUM`` and ``HIGH`` contradictions block readiness, so anything
    reaching a current view is ``LOW`` by construction and no ``InvestigationAssessment`` is
    read.
    """

    relied = {fact_id for claim in draft.claims for fact_id in claim.export_fact_ids}
    relied |= set(draft.request.request_fact_ids)
    caveated = {fact_id for caveat in draft.caveats for fact_id in caveat.export_fact_ids}
    for fact_id in sorted(relied, key=str):
        fact = by_fact.get(fact_id)
        if fact is None:
            continue
        contradicted = fact.evidence_status is EvidenceStatus.CONTRADICTED
        if contradicted and fact_id not in caveated:
            rejections.add(ActionRejection.CONTRADICTED_FACT_NOT_CAVEATED)


__all__ = ["ValidatedProposal", "validate_action_result"]
