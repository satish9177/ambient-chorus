"""Committed renderer goldens: literal UTF-8 bytes and one literal preview digest.

Codex found no committed output goldens, so every renderer assertion was of the "render twice
and compare with itself" kind -- which is satisfied by any deterministic function, including a
wrong one. These are the other kind. The expected bytes below were reviewed by hand and pasted
in; nothing here calls the renderer to obtain what it then asserts.

The pinned proposal includes a **request-only** citation and a **caveat-only** citation, and
shares Fact 1 across claims, request, and caveats.

Re-cut note: Phase-7 F05 shared-fact reference-integrity repair.

If one of these fails, the rendered external message changed. That is a reviewed change with
re-cut vectors -- the manner ADR-018 established for a golden that moves -- and never a
regenerated expectation.
"""

from __future__ import annotations

from chorus.application.services.action_renderer import (
    TEMPLATE_VERSION,
    RenderedPreview,
    render_preview,
)
from tests.repair.pinned import FROM_IDENTITY_ID, pinned_proposal, pinned_view

GOLDEN_TEXT = (
    "Hello,\n"
    "\n"
    "Residents of this building have reported a recurring issue. The observations below were "
    "compiled from contributor-authorized facts.\n"
    "\n"
    "Evidence-backed observations\n"
    "1. The elevator was out of service on 2030-01-14. [C1]\n"
    "2. 4 residents reported an impact on access to the building. [C1]\n"
    "\n"
    "Requested action\n"
    "Please inspect and repair the elevator, then confirm the schedule. [C1] [C2]\n"
    "Requested by: 2030-01-27\n"
    "\n"
    "Caveats\n"
    "- Resident counts are aggregated and not independently inspected. [C1]\n"
    "- One reported repair visit is disputed. [C3]\n"
    "\n"
    "References\n"
    "[C1] f1a0b1c2\n"
    "[C2] f3a0b1c2\n"
    "[C3] f4a0b1c2\n"
    "\n"
    "Case reference: 0c0a5e00-0000-4000-8000-000000000002\n"
    "\n"
    "This message was compiled from contributor-authorized, minimum-necessary facts."
)

GOLDEN_HTML = (
    "<h1>Repeated elevator outages at Maple Court</h1>\n"
    "<p>Hello,</p>\n"
    "<p>Residents of this building have reported a recurring issue. The observations below "
    "were compiled from contributor-authorized facts.</p>\n"
    "<h2>Evidence-backed observations</h2>\n"
    "<ol>\n"
    "<li>The elevator was out of service on 2030-01-14. <sup>[C1]</sup></li>\n"
    "<li>4 residents reported an impact on access to the building. <sup>[C1]</sup></li>\n"
    "</ol>\n"
    "<h2>Requested action</h2>\n"
    "<p>Please inspect and repair the elevator, then confirm the schedule. "
    "<sup>[C1]</sup> <sup>[C2]</sup></p>\n"
    "<p>Requested by: 2030-01-27</p>\n"
    "<h2>Caveats</h2>\n"
    "<ul>\n"
    "<li>Resident counts are aggregated and not independently inspected. <sup>[C1]</sup></li>\n"
    "<li>One reported repair visit is disputed. <sup>[C3]</sup></li>\n"
    "</ul>\n"
    "<h2>References</h2>\n"
    "<dl>\n"
    "<dt>[C1]</dt><dd>f1a0b1c2</dd>\n"
    "<dt>[C2]</dt><dd>f3a0b1c2</dd>\n"
    "<dt>[C3]</dt><dd>f4a0b1c2</dd>\n"
    "</dl>\n"
    "<p>Case reference: 0c0a5e00-0000-4000-8000-000000000002</p>\n"
    "<p>This message was compiled from contributor-authorized, minimum-necessary facts.</p>"
)

GOLDEN_PREVIEW_HASH = "sha256:236219dfa605b0ecf6bf1af266c33e0fe34eca7fdf9ecfcdfbfdbcf7c0e8a768"

GOLDEN_TEXT_BYTES = 725
GOLDEN_HTML_BYTES = 1022


def _preview() -> RenderedPreview:
    return render_preview(pinned_proposal(), pinned_view(), from_identity_id=FROM_IDENTITY_ID)


def test_plain_text_bytes_match_the_committed_golden() -> None:
    body = _preview().text_body

    assert body == GOLDEN_TEXT
    assert body.encode("utf-8") == GOLDEN_TEXT.encode("utf-8")
    assert "\r" not in body


def test_html_bytes_match_the_committed_golden() -> None:
    body = _preview().html_body

    assert body == GOLDEN_HTML
    assert body.encode("utf-8") == GOLDEN_HTML.encode("utf-8")
    for forbidden in ("<script", "<style", "href=", "src=", "style="):
        assert forbidden not in body


def test_preview_hash_matches_the_committed_golden() -> None:
    """The digest over the frozen eight-member tuple, pinned as a literal.

    ``template_version``, ``from_identity_id``, and the three destination members come from
    fixed inputs, so this literal binds all eight members and not only the two bodies.
    """

    assert _preview().preview_hash.value == GOLDEN_PREVIEW_HASH
    assert TEMPLATE_VERSION == "email/property-manager/v1"
    assert FROM_IDENTITY_ID == "chorus-demo-sender"


def test_the_golden_byte_lengths_are_pinned_too() -> None:
    """A length is a cheap second witness: a silently dropped section moves it."""

    preview = _preview()

    assert len(preview.text_body.encode("utf-8")) == GOLDEN_TEXT_BYTES
    assert len(preview.html_body.encode("utf-8")) == GOLDEN_HTML_BYTES


def test_the_goldens_include_request_only_and_caveat_only_references() -> None:
    """State the property the bytes encode, so a future re-cut cannot quietly lose it."""

    assert "[C2] f3a0b1c2" in GOLDEN_TEXT
    assert "[C3] f4a0b1c2" in GOLDEN_TEXT
    assert "<dt>[C2]</dt><dd>f3a0b1c2</dd>" in GOLDEN_HTML
    assert "<dt>[C3]</dt><dd>f4a0b1c2</dd>" in GOLDEN_HTML
