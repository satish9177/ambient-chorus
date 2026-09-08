"""Read the current action proposal and **regenerate** its preview.

The rendered bodies are never stored. The renderer is a pure function of immutable inputs, so a
stored body could only ever agree with a regenerated one or be a second version of the truth --
and it would put the exact external message text into a table the observability rules forbid it
from reaching in logs (ADR-022 § 3). This query is where the preview comes back.

Regeneration is also a *check*, not merely a convenience. The recomputed digest is compared
against the ``preview_hash`` the proposal committed, so a template change, a configuration
change, or a corrupted stored proposal surfaces here as a mismatch a reader can see rather than
as a silently different preview beside an approval that bound the old one.

What this returns is safe-zone only: identifiers, hashes, versions, counts, the two rendered
bodies, and the proposal's own text. It never returns the recipient address, the raw model
output, the prompt, a private fact, or a compiler exclusion.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.application.services.action_renderer import TEMPLATE_VERSION, render_preview
from chorus.domain.entities import (
    ActionExecutionState,
    ActionProposal,
    ActionProposalStatus,
    Approval,
)
from chorus.domain.ids import ActionId, ApprovalId, ExecutionId, Sha256Digest, ViewId
from chorus.ports.records import CurrentActionPointer, StoredShareableView
from chorus.ports.repositories import ShareableRepositoryPort
from chorus.ports.scopes import ActionScope, CaseScope


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentActionProjection:
    """One case's current proposal, its DRAFT execution state, and the regenerated preview."""

    action_id: ActionId
    execution_id: ExecutionId
    execution_state: ActionExecutionState
    execution_version: int
    """The row's own OCC version, added in Phase 10 so a browser stops having to guess it.

    ``POST .../approvals``, ``POST .../invalidation``, and ``POST .../executions`` all require
    ``expected_execution_version``; before this field, no read returned one.
    """

    approval_id: ApprovalId | None
    """The durable approval binding, surfaced so a browser can recover from a reload (P2-4).

    ``POST .../executions`` requires ``approval_id`` alongside ``execution_id`` and
    ``expected_execution_version``, and before this field no read returned it -- a browser that
    approved a proposal and then reloaded before executing had no way to read back which
    approval it held, because the id existed only in that one POST's response body. It is the
    ``ActionExecution`` row's own field (present from ``APPROVED`` onward per ADR-022 § 1's
    presence table), not a new pointer: ``None`` at ``DRAFT``, and reads back exactly what the
    approval transaction wrote once the execution has moved past it.
    """

    approval_authorization_version: int | None
    """The disclosure-authority epoch recorded on the durable approval, or ``None``.

    Read straight off the ``Approval`` row (its own ``authorization_version`` field, ADR-023
    SS 1) whenever ``approval_id`` is set, so a reader can be told whether the approval a
    browser still holds was made against the case's *current* authorization epoch. ``None``
    before approval, when there is no durable decision to compare. The route turns this into the
    ``approval_authorization_current`` boolean by comparing it against the case's live
    ``authorization_version``; nothing here re-derives send-time authority, which the Phase-8
    fence still owns.
    """

    status: ActionProposalStatus
    view_id: ViewId
    view_hash: Sha256Digest
    case_version: int
    authorization_version: int
    subject: str
    claims: tuple[tuple[str, tuple[str, ...]], ...]
    requested_action: str
    caveats: tuple[tuple[str, tuple[str, ...]], ...]
    tone: str
    proposal_hash: Sha256Digest
    preview_hash: Sha256Digest
    template_version: str
    text_body: str
    html_body: str
    preview_matches_committed_hash: bool
    """Whether the regenerated preview still hashes to what the proposal committed.

    Surfaced rather than asserted, because the honest answer to a mismatch is "show a human
    that this no longer renders to what was approved", not "raise and show them nothing".
    """


@dataclass(slots=True)
class ReadCurrentAction:
    """Load the current proposal and regenerate its preview deterministically."""

    shareable: ShareableRepositoryPort
    from_identity_id: str

    async def execute(self, scope: CaseScope) -> CurrentActionProjection | None:
        pointer = await self.shareable.load_current_action_pointer(scope)
        if pointer is None:
            return None
        action_scope = ActionScope(
            namespace=scope.namespace,
            community_id=scope.community_id,
            case_id=scope.case_id,
            action_id=pointer.action_id,
        )
        proposal = await self.shareable.load_proposal(action_scope)
        view = await self.shareable.load_view(scope, proposal.view_id)
        # Named by the pointer rather than derived from the action: the identities are
        # UUIDv4 and nothing recomputes one from the other.
        execution_id = pointer.execution_id
        execution = await self.shareable.load_execution(action_scope, execution_id)
        approval: Approval | None = None
        if execution.approval_id is not None:
            # The row carries the binding from APPROVED onward; loading it here is what lets the
            # read say whether that decision is still current for the case's authorization epoch
            # (P2), rather than leaving a browser to infer it from unrelated fields.
            approval = await self.shareable.load_approval(action_scope, execution.approval_id)
        return self._project(
            pointer,
            proposal,
            view,
            execution_id,
            execution.state,
            execution.version,
            execution.approval_id,
            None if approval is None else approval.authorization_version,
        )

    def _project(
        self,
        pointer: CurrentActionPointer,
        proposal: ActionProposal,
        view: StoredShareableView,
        execution_id: ExecutionId,
        execution_state: ActionExecutionState,
        execution_version: int,
        approval_id: ApprovalId | None,
        approval_authorization_version: int | None,
    ) -> CurrentActionProjection:
        preview = render_preview(proposal, view, from_identity_id=self.from_identity_id)
        return CurrentActionProjection(
            action_id=proposal.action_id,
            execution_id=execution_id,
            execution_state=execution_state,
            execution_version=execution_version,
            approval_id=approval_id,
            approval_authorization_version=approval_authorization_version,
            status=pointer.status,
            view_id=proposal.view_id,
            view_hash=proposal.view_hash,
            case_version=proposal.case_version,
            authorization_version=proposal.authorization_version,
            subject=proposal.subject,
            claims=tuple(
                (claim.text, tuple(str(value)[:8] for value in claim.export_fact_ids))
                for claim in proposal.claims
            ),
            requested_action=proposal.requested_action,
            caveats=tuple(
                (caveat.text, tuple(str(value)[:8] for value in caveat.export_fact_ids))
                for caveat in proposal.caveats
            ),
            tone=proposal.tone.value,
            proposal_hash=proposal.proposal_hash,
            preview_hash=proposal.preview_hash,
            template_version=TEMPLATE_VERSION,
            text_body=preview.text_body,
            html_body=preview.html_body,
            preview_matches_committed_hash=preview.preview_hash == proposal.preview_hash,
        )


__all__ = ["CurrentActionProjection", "ReadCurrentAction"]
