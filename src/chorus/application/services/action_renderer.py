"""``email/property-manager/v1``: one document tree, two renderings, one preview hash.

The renderer is a pure function of four immutable inputs and nothing else:

1. the validated immutable :class:`~chorus.domain.entities.ActionProposal`;
2. its exact bound ``StoredShareableView``;
3. ``template_version``;
4. ``from_identity_id``.

It receives no recipient address, no Core state, no ``InvestigationAssessment``, no compiler
audit projection, no private evidence, no model completion, and no destination secret. It reads
no secret at all, which is the point of ``from_identity_id`` being ordinary deployment
configuration rather than a Secrets Manager lookup (ADR-022 § 4).

One tree, two writers
---------------------
:func:`build_document` produces the intermediate tree; :func:`render_text` and
:func:`render_html` are both pure functions *of that tree* and never of the proposal. That is
what makes "the plain text and the HTML say the same thing" checkable rather than asserted: two
independent renderers over the same inputs would be two places for a section to go missing, and
a test could only compare them for resemblance. Here a section that is not in the tree is in
neither output, and a test asserts both writers consume the same node list.

Escaping is the HTML writer's job and it applies to every dynamic value without exception. No
model text is ever emitted raw, there is no ``<script>`` or ``<style>``, no inline style
attribute, no link, no remote asset, and no attachment.

Size
----
A rendered result over 100 KiB **rejects the whole proposal**. It never truncates, never drops
a section, and never silently omits a caveat -- a message shortened to fit is a message nobody
approved.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from uuid import UUID

from chorus.domain.entities import ActionProposal, ActionTone
from chorus.domain.errors import ValidationError
from chorus.domain.ids import Sha256Digest
from chorus.ports.records import StoredShareableView
from chorus.privacy.canonical import hash_value

TEMPLATE_VERSION = "email/property-manager/v1"
"""The single term for this renderer's identity.

Documents used to carry "renderer version" as a second name for the same value; that spelling
is retired. It is inside ``preview_hash``, so a template change makes every previously approved
preview visibly different rather than quietly re-rendered.
"""

MAX_RENDERED_BYTES = 100 * 1024

TONE_FRAMING: dict[ActionTone, str] = {
    ActionTone.NEUTRAL: (
        "Residents of this building have reported a recurring issue. "
        "The observations below were compiled from contributor-authorized facts."
    ),
    ActionTone.COLLABORATIVE: (
        "Residents of this building would like to work with you on a recurring issue. "
        "The observations below were compiled from contributor-authorized facts."
    ),
    ActionTone.FIRM: (
        "Residents of this building have reported a recurring issue that remains unresolved. "
        "The observations below were compiled from contributor-authorized facts."
    ),
}
"""Fixed template copy, selected by tone and never written by the model.

The tone chooses which of three reviewed sentences appears. It contributes no words of its own,
so a model cannot reach the recipient through the register field.
"""

CLOSING_NOTE = "This message was compiled from contributor-authorized, minimum-necessary facts."
GREETING = "Hello,"
OBSERVATIONS_HEADING = "Evidence-backed observations"
REQUEST_HEADING = "Requested action"
CAVEATS_HEADING = "Caveats"
REFERENCES_HEADING = "References"
NO_DEADLINE_COPY = "No specific deadline is requested."


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaimItem:
    index: int
    text: str
    markers: tuple[str, ...]

    @property
    def marker(self) -> str:
        """First marker, for backward compatibility when single citation."""
        return self.markers[0] if self.markers else ""


@dataclass(frozen=True, slots=True, kw_only=True)
class CaveatItem:
    text: str
    markers: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestItem:
    """The requested action and the markers of the facts that justify asking.

    The request carries markers for the same reason a claim does. ADR-021 section 1 removed the
    factual-premise classifier by making the request citation-bound and **never** zero-cited, so
    a rendered request with no reference marker would show the reader a request whose stated
    justification had been dropped between validation and the page.
    """

    text: str
    markers: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceItem:
    """One marker and the short export-fact identifier it stands for.

    The short identifier is **renderer output**, not model text: the renderer emits it from a
    typed contract field the proposal already carries. The ADR-021 ban on identifier shapes is a
    rule about model prose, and it does not apply to the reference list the template builds for
    itself.
    """

    marker: str
    short_export_fact_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Document:
    """The single intermediate tree both writers consume.

    Deliberately a closed value with no rendering behaviour on it. A node that knew how to draw
    itself in two formats would be the two-renderer problem again, one class down.
    """

    subject: str
    greeting: str
    framing: str
    observations_heading: str
    claims: tuple[ClaimItem, ...]
    request_heading: str
    request: RequestItem
    deadline_line: str
    caveats_heading: str | None
    caveats: tuple[CaveatItem, ...]
    references_heading: str
    references: tuple[ReferenceItem, ...]
    case_reference: str
    closing_note: str


def _marker(index: int) -> str:
    """``C1..Cn`` by first-use order of cited facts."""

    return f"C{index}"


def build_document(
    proposal: ActionProposal, view: StoredShareableView, *, template_version: str
) -> Document:
    """Build the one intermediate tree, in frozen section order.

    Reference markers represent cited FACTS in first-use order across:
    1. claims in proposal order (within each claim, citation order)
    2. requested_action citations
    3. caveats in proposal order (within each caveat, citation order)

    **Every persisted citation survives into the preview.** A fact cited more than once keeps
    the single marker its first use assigned it, which is what makes the References block one
    list rather than a list naming the same fact twice or leaving dangling markers.

    The ``Caveats`` section is **omitted entirely** when there are none rather than rendered
    empty, because an empty heading reads as a caveat somebody forgot to write.
    """

    if template_version != TEMPLATE_VERSION:
        raise ValidationError("unsupported template version")
    if proposal.view_id != view.view_id or proposal.view_hash != view.view_hash:
        # The renderer is only ever handed the proposal's own bound view. Refusing here means a
        # mis-wired composition root fails loudly rather than producing a preview describing a
        # view the human never approved against.
        raise ValidationError("proposal is not bound to this view")

    marker_by_fact: dict[str, str] = {}
    next_marker = 1

    # 1. claims in proposal order; within each claim, citation order
    for claim in proposal.claims:
        for fact_id in claim.export_fact_ids:
            key = str(fact_id)
            if key not in marker_by_fact:
                marker_by_fact[key] = _marker(next_marker)
                next_marker += 1

    # 2. requested_action citations
    for fact_id in proposal.request_fact_ids:
        key = str(fact_id)
        if key not in marker_by_fact:
            marker_by_fact[key] = _marker(next_marker)
            next_marker += 1

    # 3. caveats in proposal order; within each caveat, citation order
    for caveat in proposal.caveats:
        for fact_id in caveat.export_fact_ids:
            key = str(fact_id)
            if key not in marker_by_fact:
                marker_by_fact[key] = _marker(next_marker)
                next_marker += 1

    claims = tuple(
        ClaimItem(
            index=index,
            text=claim.text,
            markers=_markers_for(claim.export_fact_ids, marker_by_fact),
        )
        for index, claim in enumerate(proposal.claims, start=1)
    )
    request = RequestItem(
        text=proposal.requested_action,
        markers=_markers_for(proposal.request_fact_ids, marker_by_fact),
    )
    caveats = tuple(
        CaveatItem(
            text=caveat.text,
            markers=_markers_for(caveat.export_fact_ids, marker_by_fact),
        )
        for caveat in proposal.caveats
    )
    references = tuple(
        ReferenceItem(marker=marker, short_export_fact_id=fact_id[:8])
        for fact_id, marker in marker_by_fact.items()
    )
    deadline_line = (
        NO_DEADLINE_COPY
        if proposal.requested_deadline is None
        # Day precision, matching the day precision the compiler's own date transformations
        # use. A minute-precision deadline in an external letter implies a promise the case
        # cannot support.
        else f"Requested by: {proposal.requested_deadline.date().isoformat()}"
    )
    return Document(
        subject=proposal.subject,
        greeting=GREETING,
        framing=TONE_FRAMING[proposal.tone],
        observations_heading=OBSERVATIONS_HEADING,
        claims=claims,
        request_heading=REQUEST_HEADING,
        request=request,
        deadline_line=deadline_line,
        caveats_heading=CAVEATS_HEADING if caveats else None,
        caveats=caveats,
        references_heading=REFERENCES_HEADING,
        references=references,
        case_reference=f"Case reference: {proposal.case_id}",
        closing_note=CLOSING_NOTE,
    )


def _markers_for(fact_ids: tuple[UUID, ...], marker_by_fact: Mapping[str, str]) -> tuple[str, ...]:
    """The markers one block cites, in citation order, each appearing once.

    Citations are sorted and unique on the proposal, so this order is deterministic. The
    de-duplication matters when two cited facts were both introduced by the same earlier block:
    the reader should see ``[C2]`` once rather than twice.
    """

    markers = [marker_by_fact[str(fact_id)] for fact_id in fact_ids]
    return tuple(dict.fromkeys(markers))


def render_text(document: Document) -> str:
    """UTF-8 plain text with ``\\n`` line endings only and deterministic blank-line separation.

    ``\\r`` never appears. A carriage return in an outbound message body is the header-injection
    shape the structural rules refuse in model text, and emitting one from the template would
    put it back after the check that removed it.
    """

    lines: list[str] = [document.greeting, "", document.framing, "", document.observations_heading]
    for claim in document.claims:
        markers = "".join(f" [{marker}]" for marker in claim.markers)
        lines.append(f"{claim.index}. {claim.text}{markers}")
    request_markers = "".join(f" [{marker}]" for marker in document.request.markers)
    lines.extend(
        (
            "",
            document.request_heading,
            f"{document.request.text}{request_markers}",
            document.deadline_line,
        )
    )
    if document.caveats_heading is not None:
        lines.extend(("", document.caveats_heading))
        for caveat in document.caveats:
            markers = "".join(f" [{marker}]" for marker in caveat.markers)
            lines.append(f"- {caveat.text}{markers}")
    lines.extend(("", document.references_heading))
    for reference in document.references:
        lines.append(f"[{reference.marker}] {reference.short_export_fact_id}")
    lines.extend(("", document.case_reference, "", document.closing_note))
    return "\n".join(lines)


def render_html(document: Document) -> str:
    """The same tree as escaped HTML.

    Every dynamic value passes through :func:`html.escape` with ``quote=True``. There is no
    branch that emits model text raw, no attribute whose value comes from the proposal, and no
    element that could load or navigate anywhere.
    """

    parts: list[str] = [
        f"<h1>{escape(document.subject, quote=True)}</h1>",
        f"<p>{escape(document.greeting, quote=True)}</p>",
        f"<p>{escape(document.framing, quote=True)}</p>",
        f"<h2>{escape(document.observations_heading, quote=True)}</h2>",
        "<ol>",
    ]
    for claim in document.claims:
        markers = "".join(f" <sup>[{escape(marker, quote=True)}]</sup>" for marker in claim.markers)
        parts.append(f"<li>{escape(claim.text, quote=True)}{markers}</li>")
    request_markers = "".join(
        f" <sup>[{escape(marker, quote=True)}]</sup>" for marker in document.request.markers
    )
    parts.extend(
        (
            "</ol>",
            f"<h2>{escape(document.request_heading, quote=True)}</h2>",
            f"<p>{escape(document.request.text, quote=True)}{request_markers}</p>",
            f"<p>{escape(document.deadline_line, quote=True)}</p>",
        )
    )
    if document.caveats_heading is not None:
        parts.extend((f"<h2>{escape(document.caveats_heading, quote=True)}</h2>", "<ul>"))
        for caveat in document.caveats:
            markers = "".join(
                f" <sup>[{escape(marker, quote=True)}]</sup>" for marker in caveat.markers
            )
            parts.append(f"<li>{escape(caveat.text, quote=True)}{markers}</li>")
        parts.append("</ul>")
    parts.extend((f"<h2>{escape(document.references_heading, quote=True)}</h2>", "<dl>"))
    for reference in document.references:
        parts.append(
            f"<dt>[{escape(reference.marker, quote=True)}]</dt>"
            f"<dd>{escape(reference.short_export_fact_id, quote=True)}</dd>"
        )
    parts.extend(
        (
            "</dl>",
            f"<p>{escape(document.case_reference, quote=True)}</p>",
            f"<p>{escape(document.closing_note, quote=True)}</p>",
        )
    )
    return "\n".join(parts)


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderedPreview:
    """The two bodies and the digest that binds them, for one proposal and one view.

    The bodies are returned rather than stored. ``preview_hash`` is the only thing persisted:
    the renderer is a pure function of immutable inputs, so a stored body could only ever agree
    with a regenerated one or be a second version of the truth -- and it would put the exact
    external message text into a table the observability rules forbid it from reaching in logs.
    """

    document: Document
    text_body: str
    html_body: str
    preview_hash: Sha256Digest


def preview_hash(
    *,
    template_version: str,
    from_identity_id: str,
    view: StoredShareableView,
    subject: str,
    text_body: str,
    html_body: str,
) -> Sha256Digest:
    """The canonical preview digest, over exactly the frozen eight-member tuple.

    ``destination_id``, ``destination_registry_version``, and ``routing_token`` come from
    ``view.destination`` rather than from configuration, so the hash binds the routing the
    *compiler authorized* rather than whatever happens to be configured when the sender runs.

    ``from_identity_id`` is in the tuple because approval must bind *who the message claims to
    be from*: a preview a human approved that could be sent under a different sending identity
    would be an approval of the words and not of the letter.
    """

    return hash_value(
        {
            "template_version": template_version,
            "from_identity_id": from_identity_id,
            "destination_id": view.destination.destination_id,
            "destination_registry_version": view.destination.registry_version,
            "routing_token": view.destination.routing_token,
            "subject": subject,
            "text_body": text_body,
            "html_body": html_body,
        }
    )


def render_preview(
    proposal: ActionProposal,
    view: StoredShareableView,
    *,
    from_identity_id: str,
    template_version: str = TEMPLATE_VERSION,
) -> RenderedPreview:
    """Render both bodies from one tree and seal them with the preview digest.

    Raises :class:`ValidationError` when the rendered result exceeds 100 KiB. It never truncates
    and never drops a section: the caller's only correct response is to reject the whole
    proposal, because a shortened message is not the message that was validated.
    """

    document = build_document(proposal, view, template_version=template_version)
    text_body = render_text(document)
    html_body = render_html(document)
    rendered_bytes = len(text_body.encode("utf-8")) + len(html_body.encode("utf-8"))
    if rendered_bytes > MAX_RENDERED_BYTES:
        raise ValidationError("rendered message exceeds the frozen size bound")
    return RenderedPreview(
        document=document,
        text_body=text_body,
        html_body=html_body,
        preview_hash=preview_hash(
            template_version=template_version,
            from_identity_id=from_identity_id,
            view=view,
            subject=document.subject,
            text_body=text_body,
            html_body=html_body,
        ),
    )


__all__ = [
    "CLOSING_NOTE",
    "MAX_RENDERED_BYTES",
    "NO_DEADLINE_COPY",
    "REFERENCES_HEADING",
    "REQUEST_HEADING",
    "TEMPLATE_VERSION",
    "TONE_FRAMING",
    "CaveatItem",
    "ClaimItem",
    "Document",
    "ReferenceItem",
    "RenderedPreview",
    "RequestItem",
    "build_document",
    "preview_hash",
    "render_html",
    "render_preview",
    "render_text",
]
