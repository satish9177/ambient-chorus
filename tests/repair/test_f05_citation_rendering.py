"""F05 -- every persisted citation survives into the human-visible preview.

Codex found ``marker_by_fact`` derived from claims alone, which had two consequences the
preview never showed anyone:

* a fact cited **only** by ``request_fact_ids`` had no marker and appeared in no References
  block, so the human approving the message could not see what justified the request;
* a caveat citing a fact no claim cited rendered with **zero** markers, because the caveat's
  marker lookup silently dropped every fact the claim pass had not already mapped.

The repair builds the reference map by first use in the frozen document order -- claims in
proposal order, then the request, then caveats in proposal order -- and renders markers on all
three block kinds. ``ActionProposal`` bounds request and caveat citations at one-to-ten and
never zero (ADR-021 § 1), so "every citation appears" is a total statement, not a best effort.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from chorus.application.services.action_renderer import (
    REFERENCES_HEADING,
    TEMPLATE_VERSION,
    Document,
    build_document,
    render_html,
    render_preview,
    render_text,
)
from chorus.domain.entities import ActionProposal
from tests.repair.pinned import (
    FACT_FOUR,
    FACT_ONE,
    FACT_THREE,
    FACT_TWO,
    FROM_IDENTITY_ID,
    pinned_proposal,
    pinned_view,
)


def _document(proposal: ActionProposal | None = None) -> Document:
    return build_document(
        proposal or pinned_proposal(), pinned_view(), template_version=TEMPLATE_VERSION
    )


def _short(fact_id: UUID) -> str:
    return str(fact_id)[:8]


# ---------------------------------------------------------------------------------------
# The two citations that used to disappear
# ---------------------------------------------------------------------------------------


def test_a_request_only_citation_reaches_the_references_block() -> None:
    """``FACT_THREE`` is cited by the request and by nothing else."""

    document = _document()
    references = {item.short_export_fact_id: item.marker for item in document.references}

    assert _short(FACT_THREE) in references
    assert references[_short(FACT_THREE)] in document.request.markers


def test_a_caveat_only_citation_reaches_the_references_block() -> None:
    """``FACT_FOUR`` is cited by the second caveat and by nothing else."""

    document = _document()
    references = {item.short_export_fact_id: item.marker for item in document.references}

    assert _short(FACT_FOUR) in references
    assert references[_short(FACT_FOUR)] in document.caveats[1].markers


def test_no_caveat_renders_with_an_empty_marker_set() -> None:
    """The pre-repair caveat pass dropped unknown facts, which could empty the whole set."""

    document = _document()

    assert all(caveat.markers for caveat in document.caveats)


def test_the_requested_action_renders_markers() -> None:
    """A citation-bound request that renders no marker shows an unjustified request."""

    document = _document()

    assert document.request.markers
    for marker in document.request.markers:
        assert f"[{marker}]" in render_text(document)
        assert f"[{marker}]</sup>" in render_html(document)


# ---------------------------------------------------------------------------------------
# Completeness, stability, and order
# ---------------------------------------------------------------------------------------


def test_references_contain_every_cited_fact_exactly_once() -> None:
    proposal = pinned_proposal()
    cited = (
        {fact_id for claim in proposal.claims for fact_id in claim.export_fact_ids}
        | set(proposal.request_fact_ids)
        | {fact_id for caveat in proposal.caveats for fact_id in caveat.export_fact_ids}
    )
    references = [item.short_export_fact_id for item in _document().references]

    assert sorted(references) == sorted(_short(fact_id) for fact_id in cited)
    assert len(references) == len(set(references))


def test_a_fact_used_by_claim_request_and_caveat_keeps_one_stable_marker() -> None:
    """``FACT_ONE`` is claimed, requested, and caveated."""

    document = _document()
    references = {item.short_export_fact_id: item.marker for item in document.references}

    assert references[_short(FACT_ONE)] == "C1"
    assert "C1" in document.claims[0].markers
    assert "C1" in document.claims[1].markers
    assert "C1" in document.request.markers
    assert "C1" in document.caveats[0].markers


def test_marker_assignment_is_document_order_with_no_holes() -> None:
    """First use in claims, then the request, then caveats -- and a block that introduces
    nothing consumes no number, so the References list has no gap in it."""

    document = _document()

    assert [item.marker for item in document.references] == ["C1", "C2", "C3"]
    assert [item.short_export_fact_id for item in document.references] == [
        _short(FACT_ONE),
        _short(FACT_THREE),
        _short(FACT_FOUR),
    ]


def test_multiple_request_facts_each_get_a_marker() -> None:
    proposal = replace(
        pinned_proposal(), request_fact_ids=tuple(sorted((FACT_THREE, FACT_FOUR), key=str))
    )
    document = _document(proposal)
    references = {item.short_export_fact_id for item in document.references}

    assert {_short(FACT_THREE), _short(FACT_FOUR)} <= references
    assert document.request.markers == ("C2", "C3")


def test_multiple_caveat_facts_each_get_a_marker() -> None:
    proposal = pinned_proposal()
    proposal = replace(
        proposal,
        caveats=(
            replace(
                proposal.caveats[1],
                export_fact_ids=tuple(sorted((FACT_THREE, FACT_FOUR), key=str)),
            ),
        ),
    )
    document = _document(proposal)
    references = {item.short_export_fact_id for item in document.references}

    assert {_short(FACT_THREE), _short(FACT_FOUR)} <= references
    assert document.caveats[0].markers == ("C2", "C3")


def test_the_references_section_is_rendered_in_both_bodies() -> None:
    preview = render_preview(pinned_proposal(), pinned_view(), from_identity_id=FROM_IDENTITY_ID)

    for body in (preview.text_body, preview.html_body):
        assert REFERENCES_HEADING in body
        for item in preview.document.references:
            assert item.short_export_fact_id in body


@pytest.mark.parametrize("attempt", range(3))
def test_rendering_is_deterministic_across_fresh_inputs(attempt: int) -> None:
    """Rebuilt inputs, not a cached document -- self-equality would prove nothing."""

    first = render_preview(pinned_proposal(), pinned_view(), from_identity_id=FROM_IDENTITY_ID)
    second = render_preview(pinned_proposal(), pinned_view(), from_identity_id=FROM_IDENTITY_ID)

    assert first.text_body == second.text_body
    assert first.html_body == second.html_body
    assert first.preview_hash == second.preview_hash


# ---------------------------------------------------------------------------------------
# Direct tests for Cases A through I (Section 5)
# ---------------------------------------------------------------------------------------


def _check_document_invariants(doc: Document) -> None:
    """Enforce the structural invariants across document, text, and HTML."""
    text = render_text(doc)
    html = render_html(doc)
    k = len(doc.references)
    expected_markers = [f"C{i}" for i in range(1, k + 1)]
    actual_markers = [r.marker for r in doc.references]
    assert actual_markers == expected_markers, "marker sequence must be contiguous C1..CK"

    # Unique fact IDs in references
    short_ids = [r.short_export_fact_id for r in doc.references]
    assert len(short_ids) == len(set(short_ids)), "each fact must appear once in references"

    # Every rendered marker exists in references
    all_inline_markers: set[str] = set()
    for claim in doc.claims:
        all_inline_markers.update(claim.markers)
    all_inline_markers.update(doc.request.markers)
    for caveat in doc.caveats:
        all_inline_markers.update(caveat.markers)

    assert all_inline_markers == set(expected_markers), (
        "all references must appear inline and vice versa"
    )

    # Check text and HTML rendering
    for m in expected_markers:
        assert f"[{m}]" in text
        assert f"[{m}]</sup>" in html
        assert f"[{m}]" in text.split(REFERENCES_HEADING)[0]
        assert f"<dt>[{m}]</dt>" in html


def test_case_a_two_different_claims_cite_same_fact() -> None:
    """Case A: two different claims cite the same fact."""
    base = pinned_proposal()
    proposal = replace(
        base,
        claims=(
            replace(base.claims[0], export_fact_ids=(FACT_ONE,)),
            replace(base.claims[1], export_fact_ids=(FACT_ONE,)),
        ),
        request_fact_ids=(FACT_ONE,),
        caveats=(),
    )
    doc = _document(proposal)
    _check_document_invariants(doc)

    assert doc.claims[0].markers == ("C1",)
    assert doc.claims[1].markers == ("C1",)
    assert [(r.marker, r.short_export_fact_id) for r in doc.references] == [
        ("C1", _short(FACT_ONE))
    ]


def test_case_b_one_claim_cites_a_second_cites_a_and_b() -> None:
    """Case B: one claim cites A, second cites A+B."""
    base = pinned_proposal()
    facts_ab = tuple(sorted((FACT_ONE, FACT_TWO), key=str))
    proposal = replace(
        base,
        claims=(
            replace(base.claims[0], export_fact_ids=(FACT_ONE,)),
            replace(base.claims[1], export_fact_ids=facts_ab),
        ),
        request_fact_ids=(FACT_ONE,),
        caveats=(),
    )
    doc = _document(proposal)
    _check_document_invariants(doc)

    assert doc.claims[0].markers == ("C1",)
    assert doc.claims[1].markers == ("C1", "C2")
    assert [(r.marker, r.short_export_fact_id) for r in doc.references] == [
        ("C1", _short(FACT_ONE)),
        ("C2", _short(FACT_TWO)),
    ]


def test_case_c_two_claims_cite_same_two_facts_in_different_allowed_citation_ordering() -> None:
    """Case C: two claims cite same two facts in different allowed citation ordering."""
    base = pinned_proposal()
    # FACT_TWO introduced first by claim 0, so FACT_TWO gets C1.
    # Claim 1 cites (FACT_ONE, FACT_TWO) sorted by str -> FACT_ONE gets C2.
    # Claim 1 markers are ("C2", "C1").
    facts_ab = tuple(sorted((FACT_ONE, FACT_TWO), key=str))
    proposal = replace(
        base,
        claims=(
            replace(base.claims[0], export_fact_ids=(FACT_TWO,)),
            replace(base.claims[1], export_fact_ids=facts_ab),
        ),
        request_fact_ids=(FACT_TWO,),
        caveats=(),
    )
    doc = _document(proposal)
    _check_document_invariants(doc)

    assert doc.claims[0].markers == ("C1",)
    assert doc.claims[1].markers == ("C2", "C1")
    assert [(r.marker, r.short_export_fact_id) for r in doc.references] == [
        ("C1", _short(FACT_TWO)),
        ("C2", _short(FACT_ONE)),
    ]
    text = render_text(doc)
    html = render_html(doc)
    assert " [C2] [C1]" in text
    assert " <sup>[C2]</sup> <sup>[C1]</sup>" in html


def test_case_d_claim_a_request_a() -> None:
    """Case D: claim A, request A."""
    base = pinned_proposal()
    proposal = replace(
        base,
        claims=(replace(base.claims[0], export_fact_ids=(FACT_ONE,)),),
        request_fact_ids=(FACT_ONE,),
        caveats=(),
    )
    doc = _document(proposal)
    _check_document_invariants(doc)

    assert doc.claims[0].markers == ("C1",)
    assert doc.request.markers == ("C1",)
    assert [(r.marker, r.short_export_fact_id) for r in doc.references] == [
        ("C1", _short(FACT_ONE))
    ]


def test_case_e_claim_a_caveat_a() -> None:
    """Case E: claim A, caveat A."""
    base = pinned_proposal()
    proposal = replace(
        base,
        claims=(replace(base.claims[0], export_fact_ids=(FACT_ONE,)),),
        request_fact_ids=(FACT_ONE,),
        caveats=(replace(base.caveats[0], export_fact_ids=(FACT_ONE,)),),
    )
    doc = _document(proposal)
    _check_document_invariants(doc)

    assert doc.claims[0].markers == ("C1",)
    assert doc.request.markers == ("C1",)
    assert doc.caveats[0].markers == ("C1",)
    assert [(r.marker, r.short_export_fact_id) for r in doc.references] == [
        ("C1", _short(FACT_ONE))
    ]


def test_case_f_claim_a_request_b_caveat_a_and_c() -> None:
    """Case F: claim A, request B, caveat A+C."""
    base = pinned_proposal()
    facts_ac = tuple(sorted((FACT_ONE, FACT_THREE), key=str))
    proposal = replace(
        base,
        claims=(replace(base.claims[0], export_fact_ids=(FACT_ONE,)),),
        request_fact_ids=(FACT_TWO,),
        caveats=(replace(base.caveats[0], export_fact_ids=facts_ac),),
    )
    doc = _document(proposal)
    _check_document_invariants(doc)

    assert doc.claims[0].markers == ("C1",)
    assert doc.request.markers == ("C2",)
    assert doc.caveats[0].markers == ("C1", "C3")
    assert [(r.marker, r.short_export_fact_id) for r in doc.references] == [
        ("C1", _short(FACT_ONE)),
        ("C2", _short(FACT_TWO)),
        ("C3", _short(FACT_THREE)),
    ]


def test_case_g_multiple_caveats_share_same_fact() -> None:
    """Case G: multiple caveats share same fact."""
    base = pinned_proposal()
    proposal = replace(
        base,
        claims=(replace(base.claims[0], export_fact_ids=(FACT_ONE,)),),
        request_fact_ids=(FACT_ONE,),
        caveats=(
            replace(base.caveats[0], export_fact_ids=(FACT_TWO,)),
            replace(base.caveats[1], export_fact_ids=(FACT_TWO,)),
        ),
    )
    doc = _document(proposal)
    _check_document_invariants(doc)

    assert doc.caveats[0].markers == ("C2",)
    assert doc.caveats[1].markers == ("C2",)
    assert [(r.marker, r.short_export_fact_id) for r in doc.references] == [
        ("C1", _short(FACT_ONE)),
        ("C2", _short(FACT_TWO)),
    ]


def test_case_h_request_only_fact() -> None:
    """Case H: request-only fact."""
    base = pinned_proposal()
    proposal = replace(
        base,
        claims=(replace(base.claims[0], export_fact_ids=(FACT_ONE,)),),
        request_fact_ids=(FACT_TWO,),
        caveats=(),
    )
    doc = _document(proposal)
    _check_document_invariants(doc)

    assert doc.claims[0].markers == ("C1",)
    assert doc.request.markers == ("C2",)
    assert [(r.marker, r.short_export_fact_id) for r in doc.references] == [
        ("C1", _short(FACT_ONE)),
        ("C2", _short(FACT_TWO)),
    ]


def test_case_i_caveat_only_fact() -> None:
    """Case I: caveat-only fact."""
    base = pinned_proposal()
    proposal = replace(
        base,
        claims=(replace(base.claims[0], export_fact_ids=(FACT_ONE,)),),
        request_fact_ids=(FACT_ONE,),
        caveats=(replace(base.caveats[0], export_fact_ids=(FACT_THREE,)),),
    )
    doc = _document(proposal)
    _check_document_invariants(doc)

    assert doc.claims[0].markers == ("C1",)
    assert doc.request.markers == ("C1",)
    assert doc.caveats[0].markers == ("C2",)
    assert [(r.marker, r.short_export_fact_id) for r in doc.references] == [
        ("C1", _short(FACT_ONE)),
        ("C2", _short(FACT_THREE)),
    ]
