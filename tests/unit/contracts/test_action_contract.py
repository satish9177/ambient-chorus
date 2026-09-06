"""The Action contract: a mirror of the view, and an output with nowhere to hide.

Two properties carry the security argument here and each has its own section below.

**The input is exactly the view.** ``chorus.contracts.action`` restates the compiled view field
for field because it may import neither ``chorus.privacy`` nor ``chorus.domain``. A restatement
is only safe while it stays a *mirror*: the moment it becomes a projection somebody can add one
private field to it, so the field sets are asserted identical and the enums are asserted to
carry every member the domain does.

**The output has no field for the things it must not do.** No body, no recipient, no evidence
citation, no scope, no destination, no mandate, no case state, no tool call. An agent cannot
propose what it has no field to propose in, and the cheapest way to keep that true is to assert
the absence directly.
"""

from __future__ import annotations

from dataclasses import fields
from enum import StrEnum
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from chorus.contracts.action import (
    ACTION_PROMPT_VERSION,
    MAX_CAVEATS,
    MAX_CITATIONS,
    MAX_CLAIMS,
    ActionCaveatDraft,
    ActionClaimDraft,
    ActionInput,
    ActionProposalDraft,
    ActionRequestDraft,
    ActionToneValue,
    SafeDisclosureScope,
    SafeEvidenceStatus,
    SafeFactType,
    SafeTransformationKind,
)
from chorus.domain.entities import (
    ActionTone,
    DisclosureScope,
    EvidenceStatus,
    FactType,
)
from chorus.ports.records import TransformationKind
from chorus.privacy.compiler import ShareableCaseView

# ---------------------------------------------------------------------------------------
# The input mirror
# ---------------------------------------------------------------------------------------


def test_action_input_is_exactly_the_shareable_view_field_for_field() -> None:
    """The mirror and the compiled artifact have identical field sets.

    This is the test that keeps the restatement honest. A field present here and absent there
    would be a value the Action Agent sees that no compiler gate approved; a field present there
    and absent here would be a silent narrowing that only *looks* like a mirror.
    """

    mirror = set(ActionInput.model_fields)
    compiled = {item.name for item in fields(ShareableCaseView)}

    assert mirror == compiled


def test_action_input_declares_the_view_schema_version_it_mirrors() -> None:
    assert ActionInput.model_fields["schema_version"].default == "shareable-case-view/v2"


@pytest.mark.parametrize(
    ("local", "domain"),
    [
        (SafeFactType, FactType),
        (SafeDisclosureScope, DisclosureScope),
        (SafeEvidenceStatus, EvidenceStatus),
        (SafeTransformationKind, TransformationKind),
        (ActionToneValue, ActionTone),
    ],
)
def test_locally_declared_enums_carry_every_member_of_their_source(
    local: type[StrEnum], domain: type[StrEnum]
) -> None:
    """Re-declared locally, identical in membership.

    A *narrower* enum here would be a second policy decision made in a file with no authority to
    make one -- for example dropping ``UNIT_LOCATION`` because a safe view can never carry it.
    That is true, and it is true because a compiler gate says so; encoding it a second time here
    would put the same rule in two places that can disagree.
    """

    assert {member.value for member in local} == {member.value for member in domain}


def test_the_contract_imports_no_private_module() -> None:
    """The Action contract is the strict case: not even ``chorus.domain``.

    The other two agent contracts reuse the frozen domain enums, which is harmless because that
    module imports only the standard library. This one does not, because its deployment artifact
    ships without ``chorus/domain`` at all -- and what the runtime can import is what decides how
    much it could ever say.
    """

    import chorus.contracts.action as module

    source = module.__file__
    assert source is not None
    text = open(source, encoding="utf-8").read()  # noqa: SIM115
    for forbidden in (
        "chorus.domain",
        "chorus.privacy",
        "chorus.ports",
        "chorus.application",
        "chorus.infrastructure",
        "chorus.contracts.monitor",
        "chorus.contracts.investigation",
    ):
        assert f"import {forbidden}" not in text
        assert f"from {forbidden}" not in text


# ---------------------------------------------------------------------------------------
# The output shape
# ---------------------------------------------------------------------------------------


def _claim(**overrides: object) -> ActionClaimDraft:
    values: dict[str, object] = {
        "claim_id": uuid4(),
        "text": "The elevator was out of service.",
        "export_fact_ids": (uuid4(),),
    }
    values.update(overrides)
    return ActionClaimDraft(**values)  # type: ignore[arg-type]


def _draft(**overrides: object) -> ActionProposalDraft:
    fact = uuid4()
    values: dict[str, object] = {
        "view_id": uuid4(),
        "view_hash": "sha256:" + "a" * 64,
        "case_id": uuid4(),
        "case_version": 1,
        "authorization_version": 1,
        "subject": "Repair request",
        "claims": (_claim(export_fact_ids=(fact,)),),
        "request": ActionRequestDraft(
            requested_action="Please inspect and repair.", request_fact_ids=(fact,)
        ),
        "caveats": (),
        "tone": ActionToneValue.NEUTRAL,
    }
    values.update(overrides)
    return ActionProposalDraft(**values)  # type: ignore[arg-type]


def test_a_minimal_valid_draft_is_accepted() -> None:
    assert _draft().tone is ActionToneValue.NEUTRAL


@pytest.mark.parametrize(
    "absent",
    [
        "body",
        "html_body",
        "text_body",
        "message",
        "recipient",
        "to",
        "from_address",
        "cc",
        "attachments",
        "evidence_ids",
        "safe_evidence_ref_ids",
        "effective_scope",
        "destination",
        "destination_id",
        "purpose",
        "mandate_id",
        "case_state",
        "tools",
    ],
)
def test_the_draft_has_no_field_for_what_the_model_may_not_decide(absent: str) -> None:
    """Absence is the design, so absence is what is asserted.

    Each of these is something a reader might reasonably expect a message-drafting agent to
    carry. None of them exists, and ``extra='forbid'`` means one cannot arrive anyway.
    """

    assert absent not in ActionProposalDraft.model_fields


def test_unknown_fields_are_forbidden_rather_than_ignored() -> None:
    with pytest.raises(ValidationError):
        ActionProposalDraft.model_validate({**_draft().model_dump(mode="json"), "body": "hello"})


def test_request_and_caveat_citations_are_never_empty() -> None:
    """The lower bound of one is the whole of ADR-021 § 1.

    With it, there is no field in which an uncited sentence can be persisted, so the
    factual-premise question disappears. Without it, somebody has to write a classifier.
    """

    with pytest.raises(ValidationError):
        _claim(export_fact_ids=())
    with pytest.raises(ValidationError):
        ActionRequestDraft(requested_action="Please repair.", request_fact_ids=())
    with pytest.raises(ValidationError):
        ActionCaveatDraft(caveat_id=uuid4(), text="Disputed.", export_fact_ids=())


def test_citations_are_bounded_at_ten() -> None:
    with pytest.raises(ValidationError):
        _claim(export_fact_ids=tuple(uuid4() for _ in range(MAX_CITATIONS + 1)))


def test_citations_are_sorted_and_deduplicated_by_the_contract() -> None:
    ids = tuple(uuid4() for _ in range(3))
    claim = _claim(export_fact_ids=tuple(reversed(ids)))

    assert claim.export_fact_ids == tuple(sorted(ids, key=str))


def test_repeated_citation_in_one_set_is_refused() -> None:
    fact = uuid4()
    with pytest.raises(ValidationError):
        _claim(export_fact_ids=(fact, fact))


def test_claim_and_caveat_counts_are_bounded() -> None:
    fact = uuid4()
    with pytest.raises(ValidationError):
        _draft(claims=tuple(_claim(export_fact_ids=(fact,)) for _ in range(MAX_CLAIMS + 1)))
    with pytest.raises(ValidationError):
        _draft(
            caveats=tuple(
                ActionCaveatDraft(caveat_id=uuid4(), text="Disputed.", export_fact_ids=(fact,))
                for _ in range(MAX_CAVEATS + 1)
            )
        )


def test_a_proposal_needs_at_least_one_claim() -> None:
    with pytest.raises(ValidationError):
        _draft(claims=())


def test_duplicate_claim_ids_are_refused() -> None:
    shared = uuid4()
    fact = uuid4()
    with pytest.raises(ValidationError):
        _draft(
            claims=(
                _claim(claim_id=shared, export_fact_ids=(fact,)),
                _claim(claim_id=shared, text="Another.", export_fact_ids=(fact,)),
            )
        )


def test_a_claim_id_and_a_caveat_id_may_not_collide() -> None:
    shared = uuid4()
    fact = uuid4()
    with pytest.raises(ValidationError):
        _draft(
            claims=(_claim(claim_id=shared, export_fact_ids=(fact,)),),
            caveats=(
                ActionCaveatDraft(caveat_id=shared, text="Disputed.", export_fact_ids=(fact,)),
            ),
        )


def test_model_local_uuid_shaped_ids_are_permitted() -> None:
    """The one deliberate difference from the Monitor's ``client_ref`` rule.

    ``claim_id`` and ``caveat_id`` are persisted, name nothing outside their own proposal,
    survive no lookup, and grant nothing -- so the identifier-shape guard that refuses a
    UUID-shaped client reference does not apply to them.
    """

    claim = _claim(claim_id=UUID("3f2a9c11-0b7e-4d18-9a52-1c7f0e6b84d3"))

    assert isinstance(claim.claim_id, UUID)


def test_subject_is_bounded_at_120_characters() -> None:
    with pytest.raises(ValidationError):
        _draft(subject="x" * 121)
    assert len(_draft(subject="x" * 120).subject) == 120


def test_tone_is_a_closed_enum_and_professional_is_not_a_member() -> None:
    with pytest.raises(ValidationError):
        _draft(tone="PROFESSIONAL")


def test_the_prompt_version_is_pinned_to_the_first_reviewed_artifact() -> None:
    assert ACTION_PROMPT_VERSION == "action/v1"
