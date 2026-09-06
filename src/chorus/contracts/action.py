"""The Action Coordinator runtime contract: one compiled safe view in, one cited draft out.

The Action Agent is the only agent whose payload crosses the private/external boundary in the
outward direction, and its contract is written accordingly. The input is the serialized
``ShareableCaseView`` **and nothing else**: no case row, no report, no fact identifier, no
evidence, no mandate record, no compiler exclusion, no conversational history, no retrieval
tool, and no recipient address. Every string it may read has already survived twenty-two
compiler gates and the compiler's own sensitive-value scanner.

Why this file restates the view
-------------------------------
``chorus.contracts`` is frozen as self-contained public-safe primitives, so it may import
neither ``chorus.privacy`` -- which owns ``ShareableCaseView`` -- nor ``chorus.domain``, whose
enums the other two agent contracts happily reuse. The Action contract is the strict case, so
the closed enums are re-declared here as local literals and the view shape is restated field
for field.

That restatement is a **mirror**, not a second lossy projection. A projection would be a place
where somebody later decides that one more private field would be helpful; a mirror has a
parity test asserting that its field set and the compiled view's are identical, so the only way
to add a field to the Action input is to add it to the artifact every compiler gate already
approved.

What the model may write back
-----------------------------
Every substantive field it authors is citation-bound: one to ten ``export_fact_id`` values on
every claim, on the request, and on every caveat, and **never zero** (ADR-021 § 1). That is not
strictness for its own sake -- it is what removes the factual-premise classifier. There is no
sentence CHORUS must parse to decide whether a request contains an assertion, because there is
no field in which an uncited sentence can be persisted.

There is deliberately no field for a rendered body, a recipient, a subject line the renderer
would not check, an evidence identifier, a scope, a destination, a mandate, or a case state. An
agent cannot propose what it has no field to propose in.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from chorus.contracts.common import StrictModel, require_utc_datetime

ACTION_INPUT_SCHEMA_VERSION: Final[Literal["action-input/v1"]] = "action-input/v1"
ACTION_OUTPUT_SCHEMA_VERSION: Final[Literal["action-output/v1"]] = "action-output/v1"

ACTION_PROMPT_VERSION: Final = "action/v1"
"""The only Action prompt identity this contract version accepts.

``action/v1`` names the first reviewed Action artifact -- the prompt text *together with* the
:class:`ActionProposalDraft` schema, because the runtime hands the model both in one call. No
Action runtime artifact has ever existed, so there is no earlier version to bump away from.
"""

MAX_CLAIMS: Final = 12
MAX_CAVEATS: Final = 8
MAX_CITATIONS: Final = 10
MIN_CITATIONS: Final = 1
"""The frozen output bounds. ``MIN_CITATIONS`` is one and applies to all three citation sets."""

MAX_VIEW_FACTS: Final = 100
MAX_VIEW_EVIDENCE_REFS: Final = 20
MAX_VIEW_MANDATE_REFS: Final = 100
MAX_VIEW_AUDIT_REFS: Final = 10
"""Input bounds, sized to the compiler's own per-case ceilings rather than guessed."""

Sha256Str = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
SafeTextStr = Annotated[str, StringConstraints(min_length=1, max_length=500)]
SubjectStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]
"""1-120 Unicode characters. The bound is the contract's, not a renderer preference: a subject
is the one model string a mail client shows before anybody has read anything."""
LabelStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]
CaptionStr = Annotated[str, StringConstraints(min_length=1, max_length=300)]
MediaTypeStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]
RuleIdStr = Annotated[str, StringConstraints(min_length=1, max_length=80)]
VersionStr = Annotated[str, StringConstraints(min_length=1, max_length=64)]
DestinationIdStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]


class SafeFactType(StrEnum):
    """The fact vocabulary, re-declared locally because contracts import no domain module.

    Every member of the domain enum appears, including the ones a safe view can never carry:
    ``UNIT_LOCATION`` and ``HEALTH_DETAIL`` are hard-internal by compiler gate and would deny a
    compile long before this contract saw them. They are listed so the mirror is a mirror --
    an enum with a *narrower* set would be a second policy decision made here, in a file that
    has no authority to make one.
    """

    INCIDENT_OCCURRENCE = "INCIDENT_OCCURRENCE"
    SERVICE_IMPACT = "SERVICE_IMPACT"
    LOCATION_AREA = "LOCATION_AREA"
    IDENTITY_ATTRIBUTE = "IDENTITY_ATTRIBUTE"
    UNIT_LOCATION = "UNIT_LOCATION"
    HEALTH_DETAIL = "HEALTH_DETAIL"
    MANAGEMENT_STATEMENT = "MANAGEMENT_STATEMENT"
    CONTRADICTION = "CONTRADICTION"
    COMMITMENT_TERM = "COMMITMENT_TERM"
    EVIDENCE_DESCRIPTION = "EVIDENCE_DESCRIPTION"


class SafeDisclosureScope(StrEnum):
    INTERNAL_ONLY = "INTERNAL_ONLY"
    AGGREGATE_ONLY = "AGGREGATE_ONLY"
    ANONYMOUS_CASE = "ANONYMOUS_CASE"
    NAMED_CASE = "NAMED_CASE"
    EXTERNAL_ACTION = "EXTERNAL_ACTION"


class SafeEvidenceStatus(StrEnum):
    """A fact's resolved evidence status, and the one signal the caveat obligation reads.

    ``CONTRADICTED`` is sticky through every compiler aggregation, so a transformed fact whose
    inputs included a contradicted one arrives contradicted too. Contradiction *materiality* is
    deliberately absent from this contract: Phase 5 is the sole authority that ``MEDIUM`` and
    ``HIGH`` contradictions block readiness, so anything reaching a current view is ``LOW`` by
    construction and Phase 7 does not re-derive that judgement (ADR-021 § 3).
    """

    REPORTED = "REPORTED"
    CORROBORATED = "CORROBORATED"
    VERIFIED = "VERIFIED"
    CONTRADICTED = "CONTRADICTED"
    UNKNOWN = "UNKNOWN"


class SafeTransformationKind(StrEnum):
    DIRECT = "DIRECT"
    ANONYMIZED = "ANONYMIZED"
    AGGREGATED = "AGGREGATED"
    GENERALIZED = "GENERALIZED"


class SafeDestinationKind(StrEnum):
    PROPERTY_MANAGER = "PROPERTY_MANAGER"


class SafePurpose(StrEnum):
    REQUEST_ELEVATOR_REPAIR_AND_RESPONSE = "REQUEST_ELEVATOR_REPAIR_AND_RESPONSE"


class ActionToneValue(StrEnum):
    """The closed register vocabulary (ADR-021 § 11).

    Consumed only by the deterministic renderer to select fixed template copy. No member widens
    a scope, names an identity, chooses a destination, or changes which facts may travel.
    """

    NEUTRAL = "NEUTRAL"
    COLLABORATIVE = "COLLABORATIVE"
    FIRM = "FIRM"


class SafeDestinationInput(StrictModel):
    """Address-free routing metadata. ``routing_token`` is random and rotated, never derived
    from the recipient address, and ``display_label`` is a public name rather than a mailbox."""

    destination_id: DestinationIdStr
    kind: SafeDestinationKind
    registry_version: Annotated[int, Field(ge=1)]
    routing_token: UUID
    display_label: LabelStr


class MandateVersionRefInput(StrictModel):
    """One relied-upon mandate as an opaque triple: identity, version, and terms digest.

    No contributor identifier, no status, no granted scope, and no terms text. The Action Agent
    can see *that* an authorization was relied upon and can learn nothing about whose it was.
    """

    mandate_id: UUID
    version: Annotated[int, Field(ge=1)]
    terms_hash: Sha256Str


class ShareableFactInput(StrictModel):
    """One externally safe fact, exactly as the compiler produced it.

    ``safe_text`` is the *only* text the model may draw a factual token from, and it is data
    rather than instruction: a fact whose text is written as a command to a system is still a
    fact about what somebody wrote.
    """

    export_fact_id: UUID
    fact_type: SafeFactType
    safe_text: SafeTextStr
    effective_scope: SafeDisclosureScope
    evidence_status: SafeEvidenceStatus
    contributor_count: Annotated[int, Field(ge=1)]
    transformation: SafeTransformationKind
    transformation_rule_id: RuleIdStr
    safe_evidence_ref_ids: Annotated[tuple[UUID, ...], Field(max_length=MAX_VIEW_EVIDENCE_REFS)]
    content_hash: Sha256Str


class SafeEvidenceRefInput(StrictModel):
    """An opaque handle to a sanitized derivative. It carries no URL and no private key."""

    safe_evidence_ref_id: UUID
    media_type: MediaTypeStr
    export_handle_id: UUID
    sha256: Sha256Str
    caption: CaptionStr
    created_by_rule_id: RuleIdStr
    content_hash: Sha256Str


class ActionInput(StrictModel):
    """The complete Action runtime payload: a field-for-field mirror of the compiled view.

    The field set is asserted equal to ``chorus.privacy.compiler.ShareableCaseView``'s by a
    parity test, so this is a restatement rather than a projection. Nothing is added here and
    nothing is dropped -- in particular the application's strongly read Core case, which it must
    load to enforce freshness, is **never** appended to this payload.
    """

    schema_version: Literal["shareable-case-view/v2"] = "shareable-case-view/v2"
    view_id: UUID
    case_id: UUID
    community_public_label: LabelStr
    case_version: Annotated[int, Field(ge=1)]
    """Provenance only. Nothing in this contract or downstream reads it for authorization."""
    authorization_version: Annotated[int, Field(ge=1)]
    """The disclosure-authority epoch this view is valid against (ADR-020)."""
    policy_version: VersionStr
    compiler_version: VersionStr
    policy_build_hash: Sha256Str
    """The policy build that compiled this view. Safe deployment configuration, not a secret."""
    destination: SafeDestinationInput
    purpose: SafePurpose
    generated_at: datetime
    expires_at: datetime
    mandate_version_set: Annotated[
        tuple[MandateVersionRefInput, ...], Field(max_length=MAX_VIEW_MANDATE_REFS)
    ]
    authorization_snapshot_hash: Sha256Str
    shareable_facts: Annotated[
        tuple[ShareableFactInput, ...], Field(min_length=1, max_length=MAX_VIEW_FACTS)
    ]
    safe_evidence_refs: Annotated[
        tuple[SafeEvidenceRefInput, ...], Field(max_length=MAX_VIEW_EVIDENCE_REFS)
    ]
    audit_refs: Annotated[tuple[UUID, ...], Field(max_length=MAX_VIEW_AUDIT_REFS)]
    view_hash: Sha256Str

    @model_validator(mode="after")
    def validate_view(self) -> Self:
        require_utc_datetime(self.generated_at)
        require_utc_datetime(self.expires_at)
        if self.expires_at <= self.generated_at:
            raise ValueError("view expiry must be after generation")
        export_ids = tuple(fact.export_fact_id for fact in self.shareable_facts)
        if len(set(export_ids)) != len(export_ids):
            raise ValueError("export fact IDs must be unique")
        ref_ids = tuple(ref.safe_evidence_ref_id for ref in self.safe_evidence_refs)
        if len(set(ref_ids)) != len(ref_ids):
            raise ValueError("safe evidence reference IDs must be unique")
        return self


CitationSet = Annotated[tuple[UUID, ...], Field(min_length=MIN_CITATIONS, max_length=MAX_CITATIONS)]
"""One to ten ``export_fact_id`` values. **Never zero, on any of the three fields.**

The lower bound is the whole of ADR-021 § 1. With it, "please repair the elevator" is still a
normative request that asserts nothing -- and it still names the safe facts that justify
asking, because a request without a reason is one the recipient cannot evaluate. Without it,
somebody has to write a classifier that decides which sentences contain a premise, and the only
honest implementations of that are an unspecified parser or a second model.
"""


def _sorted_unique(values: tuple[UUID, ...], field_name: str) -> tuple[UUID, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} must not repeat a citation")
    return tuple(sorted(values, key=str))


class ActionClaimDraft(StrictModel):
    """One factual external statement and the facts it rests on.

    ``claim_id`` is model-local within this output and deliberately UUID-shaped. That is the one
    place the Action contract differs from the Monitor's ``client_ref`` rule: it is persisted, it
    names nothing outside its own proposal, it survives no lookup, and it grants nothing, so the
    identifier-shape guard that refuses a UUID-shaped client reference does not apply.
    """

    claim_id: UUID
    text: SafeTextStr
    export_fact_ids: CitationSet

    @model_validator(mode="after")
    def validate_citations(self) -> Self:
        object.__setattr__(
            self, "export_fact_ids", _sorted_unique(self.export_fact_ids, "export_fact_ids")
        )
        return self


class ActionCaveatDraft(StrictModel):
    """One qualifying statement, structured exactly as a claim is.

    Bare-string caveats are gone: the immutable artifact a human approves has to carry the
    caveat-to-fact proof the validator relied on, or Phase-8 revalidation cannot re-check it and
    the renderer cannot cite it (ADR-021 § 2).
    """

    caveat_id: UUID
    text: SafeTextStr
    export_fact_ids: CitationSet

    @model_validator(mode="after")
    def validate_citations(self) -> Self:
        object.__setattr__(
            self, "export_fact_ids", _sorted_unique(self.export_fact_ids, "export_fact_ids")
        )
        return self


class ActionRequestDraft(StrictModel):
    """What the message asks for, and the facts that justify asking.

    ``requested_action`` is normative preference rather than a factual claim -- it asserts
    nothing -- and it is still citation-bound. ``requested_deadline`` carries no lexical
    obligation because it is a typed UTC instant rather than prose; the human preview is the
    check on whether the date asked for is reasonable (ADR-021 § 10).
    """

    requested_action: SafeTextStr
    requested_deadline: datetime | None = None
    request_fact_ids: CitationSet

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.requested_deadline is not None:
            require_utc_datetime(self.requested_deadline)
        object.__setattr__(
            self, "request_fact_ids", _sorted_unique(self.request_fact_ids, "request_fact_ids")
        )
        return self


class ActionProposalDraft(StrictModel):
    """The whole of what the Action Agent may return.

    What is absent is the design. There is no ``body``, no ``html``, no ``recipient``, no
    ``to``, no ``from``, no attachment, no evidence-identifier citation field, no scope, no
    destination, no purpose, no mandate, no case state, and no tool call. The model contributes
    wording; every factual token in that wording must already exist in a compiled safe fact it
    was given.
    """

    schema_version: Literal["action-output/v1"] = ACTION_OUTPUT_SCHEMA_VERSION
    view_id: UUID
    view_hash: Sha256Str
    case_id: UUID
    case_version: Annotated[int, Field(ge=1)]
    authorization_version: Annotated[int, Field(ge=1)]
    subject: SubjectStr
    claims: Annotated[tuple[ActionClaimDraft, ...], Field(min_length=1, max_length=MAX_CLAIMS)]
    request: ActionRequestDraft
    caveats: Annotated[tuple[ActionCaveatDraft, ...], Field(max_length=MAX_CAVEATS)] = ()
    tone: ActionToneValue

    @model_validator(mode="after")
    def validate_draft(self) -> Self:
        claim_ids = tuple(claim.claim_id for claim in self.claims)
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("claim IDs must be unique within one proposal")
        caveat_ids = tuple(caveat.caveat_id for caveat in self.caveats)
        if len(set(caveat_ids)) != len(caveat_ids):
            raise ValueError("caveat IDs must be unique within one proposal")
        if set(claim_ids) & set(caveat_ids):
            # Not a schema nicety: the renderer's markers and the References block are keyed by
            # these, and one identifier naming two structures makes a citation ambiguous.
            raise ValueError("a claim ID and a caveat ID must not collide")
        return self


__all__ = [
    "ACTION_INPUT_SCHEMA_VERSION",
    "ACTION_OUTPUT_SCHEMA_VERSION",
    "ACTION_PROMPT_VERSION",
    "MAX_CAVEATS",
    "MAX_CITATIONS",
    "MAX_CLAIMS",
    "MIN_CITATIONS",
    "ActionCaveatDraft",
    "ActionClaimDraft",
    "ActionInput",
    "ActionProposalDraft",
    "ActionRequestDraft",
    "ActionToneValue",
    "MandateVersionRefInput",
    "SafeDestinationInput",
    "SafeDestinationKind",
    "SafeDisclosureScope",
    "SafeEvidenceRefInput",
    "SafeEvidenceStatus",
    "SafeFactType",
    "SafePurpose",
    "SafeTransformationKind",
    "ShareableFactInput",
]
