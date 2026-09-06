"""ADR-020 § 2's bump table, transcribed by hand from the document.

Nothing here imports ``AUTHORIZATION_SENSITIVE_CASE_EDGES`` or
``case_edge_bumps_authorization``. That is the entire point: the production table is the thing
under test, so an oracle derived from it would only prove the table equals itself.

Every entry names the ADR row it came from. Read the table as one rule rather than twelve:

> **Lifecycle progress is not itself disclosure authority.**

Rows 2-5 are the complete set of case-owned inputs the compiler evaluates -- active facts and
their statuses, report linkage, evidence roots, current mandate decisions, and the investigation
results feeding gate 17. Rows 6-12 are lifecycle: they record what has *happened to* the case.
"""

from __future__ import annotations

from chorus.domain.entities import CaseState

NO_STATE_CHANGE_ROWS: tuple[int, ...] = (2, 4, 5)
"""The § 2 rows whose command may move no state at all, taking ``bump_case_authorization``.

Row 2 links a report and its facts into an existing case; row 4 is a mandate decision that
does not move the case out of ``AWAITING_MANDATES``; row 5 is an investigation apply that
leaves readiness where it was. All three change a case-owned compiler input.
"""

ADR_ROW_BY_EDGE: dict[tuple[CaseState, CaseState], int] = {
    # Row 3 -- candidate acceptance; the same transaction creates mandate version 1.
    (CaseState.CANDIDATE, CaseState.AWAITING_MANDATES): 3,
    # Row 4 -- a mandate decision moved a current pointer, version, or terms hash.
    (CaseState.AWAITING_MANDATES, CaseState.INVESTIGATING): 4,
    # Row 5 -- investigation apply; writes fact evidence statuses and the assessment pointer.
    (CaseState.INVESTIGATING, CaseState.READY_FOR_ACTION): 5,
    (CaseState.READY_FOR_ACTION, CaseState.INVESTIGATING): 5,
    # Row 5 as well. § 2 names the two readiness edges explicitly and the machine admits a
    # third with the same cause: readiness lost from ``ACTION_PROPOSED`` is produced either by
    # an investigation apply or by a mandate withdrawal, which are exactly the two commands row
    # 5 and row 4 describe. Classified sensitive by the governing rule, not by convenience --
    # the alternative reading would let a mandate withdrawal leave a proposal's view fresh.
    (CaseState.ACTION_PROPOSED, CaseState.INVESTIGATING): 5,
    # Row 6 -- action proposal apply. The edge that motivated the split.
    (CaseState.READY_FOR_ACTION, CaseState.ACTION_PROPOSED): 6,
    # Row 7 -- sender-return worker; records a send outcome.
    (CaseState.ACTION_PROPOSED, CaseState.ACTIONED): 7,
    # Row 8 -- proposal invalidation; the facts are untouched.
    (CaseState.ACTION_PROPOSED, CaseState.READY_FOR_ACTION): 8,
    # Row 9 -- commitment creation; an external promise in the shareable zone.
    (CaseState.ACTIONED, CaseState.VERIFYING): 9,
    # Row 10 -- contributor verification.
    (CaseState.VERIFYING, CaseState.RESOLVED): 10,
    (CaseState.VERIFYING, CaseState.READY_FOR_ACTION): 10,
    # Row 10 as well: the machine's "another action is needed" edge is the same human outcome
    # recorded one state earlier, and it changes no case-owned disclosure input either.
    (CaseState.ACTIONED, CaseState.READY_FOR_ACTION): 10,
    # Row 11 -- human close, from every allowed source. Disclosure is stopped by the *state*
    # check, not by a counter.
    (CaseState.CANDIDATE, CaseState.CLOSED_UNRESOLVED): 11,
    (CaseState.AWAITING_MANDATES, CaseState.CLOSED_UNRESOLVED): 11,
    (CaseState.INVESTIGATING, CaseState.CLOSED_UNRESOLVED): 11,
    (CaseState.READY_FOR_ACTION, CaseState.CLOSED_UNRESOLVED): 11,
    (CaseState.ACTION_PROPOSED, CaseState.CLOSED_UNRESOLVED): 11,
    (CaseState.ACTIONED, CaseState.CLOSED_UNRESOLVED): 11,
    (CaseState.VERIFYING, CaseState.CLOSED_UNRESOLVED): 11,
    # Row 12 -- terminal reopen. The new evidence that justifies it bumped the epoch when it
    # landed, so the reopen itself does not.
    (CaseState.RESOLVED, CaseState.INVESTIGATING): 12,
    (CaseState.CLOSED_UNRESOLVED, CaseState.INVESTIGATING): 12,
}
"""Which § 2 row each V1 case-write edge comes from, so no expectation is folklore."""

_AUTHORIZATION_SENSITIVE_ROWS: frozenset[int] = frozenset({2, 3, 4, 5})
"""Rows 2-5 bump both counters. Rows 6-12 bump only the OCC version."""

EXPECTED_BUMPS: dict[tuple[CaseState, CaseState], bool] = {
    edge: row in _AUTHORIZATION_SENSITIVE_ROWS for edge, row in ADR_ROW_BY_EDGE.items()
}
"""Whether each edge advances ``authorization_version``, according to the document."""

__all__ = ["ADR_ROW_BY_EDGE", "EXPECTED_BUMPS", "NO_STATE_CHANGE_ROWS"]
