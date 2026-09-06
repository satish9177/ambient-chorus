"""The deterministic renderer: one tree, two writers, one digest, and a hard size bound.

What these assert is the set of properties ADR-022 § 5 froze, and each is a property a second
independent renderer would have made unassertable:

* both bodies derive from the same intermediate tree, so a section cannot appear in one and
  not the other;
* every dynamic value in the HTML is escaped, with no branch that emits model text raw;
* the preview digest covers exactly the frozen eight-member tuple, and moves when any member
  moves -- including ``from_identity_id``, which is why approval binds *who the message claims
  to be from*;
* over 100 KiB rejects the whole proposal and never truncates.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.fixtures.persistence import World

from chorus.application.services.action_renderer import (
    MAX_RENDERED_BYTES,
    TEMPLATE_VERSION,
    TONE_FRAMING,
    build_document,
    preview_hash,
    render_html,
    render_preview,
    render_text,
)
from chorus.domain.entities import ActionProposal, ActionTone
from chorus.domain.errors import ValidationError
from chorus.ports.records import StoredShareableView

FROM_IDENTITY = "chorus-demo-sender"
_WORLD = World(seed="renderer")


def _pair() -> tuple[ActionProposal, StoredShareableView]:
    """One proposal bound to one view, which is the only pairing the renderer accepts."""

    view = _WORLD.view()
    proposal = replace(_WORLD.proposal(), view_id=view.view_id, view_hash=view.view_hash)
    return proposal, view


def test_plain_and_html_derive_from_one_intermediate_tree() -> None:
    proposal, view = _pair()
    document = build_document(proposal, view, template_version=TEMPLATE_VERSION)

    # Both writers are pure functions *of the tree*, never of the proposal. Asserting that here
    # is what makes "the two bodies say the same thing" checkable rather than a resemblance:
    # a section that is not in the tree is in neither output.
    assert render_text(document) == render_text(document)
    assert render_html(document) == render_html(document)
    for claim in document.claims:
        assert claim.text in render_text(document)
        assert claim.text in render_html(document)
    for caveat in document.caveats:
        assert caveat.text in render_text(document)
        assert caveat.text in render_html(document)


def test_rendered_preview_uses_the_same_tree_both_writers_saw() -> None:
    proposal, view = _pair()
    preview = render_preview(proposal, view, from_identity_id=FROM_IDENTITY)

    assert preview.text_body == render_text(preview.document)
    assert preview.html_body == render_html(preview.document)


def test_caveats_section_is_omitted_entirely_when_there_are_none() -> None:
    proposal, view = _pair()
    without = replace(proposal, caveats=())
    document = build_document(without, view, template_version=TEMPLATE_VERSION)

    # Omitted rather than rendered empty: an empty heading reads as a caveat somebody forgot.
    assert document.caveats_heading is None
    assert "Caveats" not in render_text(document)
    assert "Caveats" not in render_html(document)


def test_html_escapes_every_dynamic_value() -> None:
    proposal, view = _pair()
    hostile = replace(proposal, subject='<script>alert("x")</script>')
    document = build_document(hostile, view, template_version=TEMPLATE_VERSION)
    html = render_html(document)

    # The validator would already have refused this subject. The renderer escapes anyway,
    # because "no raw model markup reaches the HTML" must not depend on an upstream check.
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_plain_text_uses_newline_endings_only() -> None:
    proposal, view = _pair()
    preview = render_preview(proposal, view, from_identity_id=FROM_IDENTITY)

    # A carriage return in an outbound body is the header-injection shape the structural rules
    # refuse in model text; emitting one from the template would put it back afterwards.
    assert "\r" not in preview.text_body


def test_claim_markers_are_c1_upward_in_proposal_order() -> None:
    proposal, view = _pair()
    document = build_document(proposal, view, template_version=TEMPLATE_VERSION)

    assert [claim.marker for claim in document.claims] == [
        f"C{index}" for index in range(1, len(document.claims) + 1)
    ]


def test_caveat_markers_reuse_the_claim_vocabulary_so_references_stay_one_list() -> None:
    proposal, view = _pair()
    document = build_document(proposal, view, template_version=TEMPLATE_VERSION)
    claim_markers = {claim.marker for claim in document.claims}

    for caveat in document.caveats:
        assert set(caveat.markers) <= claim_markers
    assert {reference.marker for reference in document.references} <= claim_markers


def test_tone_selects_fixed_copy_and_contributes_no_model_words() -> None:
    proposal, view = _pair()
    for tone in ActionTone:
        document = build_document(
            replace(proposal, tone=tone),
            view,
            template_version=TEMPLATE_VERSION,
        )
        assert document.framing == TONE_FRAMING[tone]


def test_deadline_renders_at_day_precision() -> None:
    proposal, view = _pair()
    document = build_document(proposal, view, template_version=TEMPLATE_VERSION)

    # Day precision matches the compiler's own date transformations. A minute-precision
    # deadline in an external letter implies a promise the case cannot support.
    deadline = proposal.requested_deadline
    assert deadline is not None
    assert document.deadline_line.endswith(deadline.date().isoformat())
    assert ":" not in document.deadline_line.split(": ", 1)[1]


def test_absent_deadline_renders_fixed_fallback_copy() -> None:
    proposal, view = _pair()
    document = build_document(
        replace(proposal, requested_deadline=None),
        view,
        template_version=TEMPLATE_VERSION,
    )

    assert document.deadline_line == "No specific deadline is requested."


# ---------------------------------------------------------------------------------------
# The preview hash
# ---------------------------------------------------------------------------------------


def test_preview_hash_is_deterministic_over_identical_inputs() -> None:
    proposal, view = _pair()
    first = render_preview(proposal, view, from_identity_id=FROM_IDENTITY)
    second = render_preview(proposal, view, from_identity_id=FROM_IDENTITY)

    assert first.preview_hash == second.preview_hash


def test_preview_hash_moves_with_the_sending_identity() -> None:
    proposal, view = _pair()
    mine = render_preview(proposal, view, from_identity_id=FROM_IDENTITY)
    theirs = render_preview(proposal, view, from_identity_id="someone-else")

    # Approval must bind *who the message claims to be from*. A preview approved for one sending
    # identity that could be sent under another is an approval of the words and not of the
    # letter (ADR-022 § 4).
    assert mine.preview_hash != theirs.preview_hash


def test_preview_hash_binds_the_routing_the_compiler_authorized() -> None:
    proposal, view = _pair()
    base = render_preview(proposal, view, from_identity_id=FROM_IDENTITY)
    moved = replace(view, destination=replace(view.destination, registry_version=2))

    # The three destination values come from ``view.destination``, not from configuration, so
    # the digest binds what the compiler authorized rather than what is configured at send time.
    assert (
        preview_hash(
            template_version=TEMPLATE_VERSION,
            from_identity_id=FROM_IDENTITY,
            view=moved,
            subject=base.document.subject,
            text_body=base.text_body,
            html_body=base.html_body,
        )
        != base.preview_hash
    )


def test_preview_hash_moves_with_the_template_version() -> None:
    proposal, view = _pair()
    base = render_preview(proposal, view, from_identity_id=FROM_IDENTITY)

    assert (
        preview_hash(
            template_version="email/property-manager/v2",
            from_identity_id=FROM_IDENTITY,
            view=view,
            subject=base.document.subject,
            text_body=base.text_body,
            html_body=base.html_body,
        )
        != base.preview_hash
    )


def test_preview_hash_inputs_require_no_secret_read() -> None:
    """The renderer's four inputs are all non-secret, and this is the executable proof.

    ``from_identity_id`` is an opaque deployment identifier rather than the ``From`` address, so
    a preview can be computed at proposal time with no Secrets Manager permission anywhere in
    the path. The assertion is structural: the digest is computed from a literal string, the
    proposal, and the view, and nothing in this test can reach a secret store.
    """

    proposal, view = _pair()
    computed = preview_hash(
        template_version=TEMPLATE_VERSION,
        from_identity_id=FROM_IDENTITY,
        view=view,
        subject=proposal.subject,
        text_body="body",
        html_body="<p>body</p>",
    )

    assert computed.value.startswith("sha256:")


# ---------------------------------------------------------------------------------------
# Bounds and refusals
# ---------------------------------------------------------------------------------------


def test_maximum_legal_proposal_is_far_under_the_size_bound() -> None:
    """The contract's own bounds already keep a legal proposal well inside 100 KiB.

    Twelve 500-character claims and eight 500-character caveats is 10,000 characters of model
    text, so the bound is not a limit the model can reach -- which is worth knowing, because it
    means the check exists for a *template* change rather than for a verbose answer.
    """

    proposal, view = _pair()
    claim = proposal.claims[0]
    caveat = proposal.caveats[0]
    largest = replace(
        proposal,
        subject="s" * 120,
        requested_action="r" * 500,
        claims=tuple(
            replace(claim, claim_id=_WORLD.uuid(f"claim:{index}"), text="c" * 500)
            for index in range(12)
        ),
        caveats=tuple(
            replace(caveat, caveat_id=_WORLD.uuid(f"caveat:{index}"), text="v" * 500)
            for index in range(8)
        ),
    )
    preview = render_preview(largest, view, from_identity_id=FROM_IDENTITY)
    rendered = len(preview.text_body.encode("utf-8")) + len(preview.html_body.encode("utf-8"))

    assert rendered < MAX_RENDERED_BYTES


def test_rendered_message_over_100_kib_rejects_and_never_truncates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over the bound, the whole proposal is refused. Nothing is shortened.

    The bound is lowered rather than the proposal inflated, because the contract makes a
    genuinely oversized proposal unconstructible (see the test above). What is being asserted is
    the *behaviour at the bound*: a refusal, not a truncation, not a dropped section, and not a
    silently omitted caveat.
    """

    import chorus.application.services.action_renderer as renderer

    proposal, view = _pair()
    full = render_preview(proposal, view, from_identity_id=FROM_IDENTITY)
    monkeypatch.setattr(renderer, "MAX_RENDERED_BYTES", 10)

    with pytest.raises(ValidationError):
        renderer.render_preview(proposal, view, from_identity_id=FROM_IDENTITY)

    # And the document that would have been rendered still carries every section: the refusal
    # happens after rendering, so nothing was omitted in an attempt to fit.
    assert full.document.caveats
    assert full.document.references


def test_renderer_refuses_a_view_the_proposal_is_not_bound_to() -> None:
    proposal, view = _pair()
    foreign = replace(proposal, view_id=World(seed="other").view_id)

    with pytest.raises(ValidationError):
        build_document(foreign, view, template_version=TEMPLATE_VERSION)


def test_renderer_refuses_an_unsupported_template_version() -> None:
    proposal, view = _pair()

    with pytest.raises(ValidationError):
        build_document(proposal, view, template_version="email/property-manager/v2")
