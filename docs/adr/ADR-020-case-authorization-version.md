# ADR-020: The case authorization version, and why lifecycle progress is not disclosure authority

**Status:** Accepted
**Date:** 2026-09-05
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § CommunityCase, § Transition contract, § Case state machine; [05-privacy-compiler-and-shareable-view.md](../architecture/05-privacy-compiler-and-shareable-view.md) § ShareableCaseView schema, § Canonical serialization and hashes, § Freshness and send authorization fence; [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Shareable table mapping, § Transaction boundaries; [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) § Deterministic proposal validation, § Human approval contract; [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) § Complete failure matrix; [10-security-threat-model.md](../architecture/10-security-threat-model.md) § Threat register (T08)

## Context

### One counter was answering two questions

`CommunityCase.version` is the optimistic-concurrency token. Every guarded write reads it, conditions on it, and increments it — `chorus.domain.state.transition_case` sets `version=case.version + 1` on every edge in the table, and `bump_case_authorization` does the same for an authorization-sensitive change of no state.

That same integer is also the *freshness epoch* for disclosure. `ShareableCaseView.case_version` records it, `authorization_snapshot_hash` covers it, `ActionProposal.case_version` binds it, and the send fence compares it. [05-privacy-compiler-and-shareable-view.md](../architecture/05-privacy-compiler-and-shareable-view.md) says why:

> `authorization_snapshot_hash` covers the current case version, current policy build hash, and **all evaluated current mandate pointer/version/terms hashes**… This makes a relevant authorization change stale even if the current visible content would coincidentally look the same.

Two questions — *has this row moved under me?* and *has what I am allowed to disclose changed?* — were being answered by one number. For every command written before Phase 7 those questions had the same answer, so nothing was wrong. Phase 7 is the first command where they diverge.

### The divergence, stated exactly

The frozen case machine contains `READY_FOR_ACTION → ACTION_PROPOSED`, guarded by "current allowed view and valid proposal hashes", caused by the application, and the Phase-7 plan lists "case transition to proposed" among its tasks. That transition is a Core write, so it increments `version`.

The proposal it records is bound to a view compiled at version *N*:

```text
compile at case version N   -> view.case_version = N, snapshot over case_version = N
propose                     -> validator proves current case version == N; proposal binds N
transition                  -> READY_FOR_ACTION -> ACTION_PROPOSED, version := N+1
approve                     -> binds proposal_hash and view_hash; passes
acquire send fence          -> passes case_version = N; current case is N+1
                            -> STALE_AUTHORIZATION; FAILED; nothing is ever sent
```

The failure matrix in [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) already names this outcome for a case version that moves after a compile, and it is correct to name it — for a *real* authorization change. Here the only thing that changed is that the case now knows a proposal exists.

The remedy the same table offers, "recompile, re-propose, reapprove", happens to work, because the second proposal starts from `ACTION_PROPOSED` and moves no version. So the frozen design yields **"the first send always fails and the second always succeeds"**, which is not a design; it is a deadlock nobody had reached yet.

### The hazard was already known, and the exemption was written too narrowly

[04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § CommunityCase says of the compiler:

> The compiler never writes the Core case row, never sets this field, and never bumps `CommunityCase.version` — its only Core write is the send fence, and a compile that bumped the case version would immediately stale the very view it had just produced against the exact-version check the Action proposal validator performs.

That sentence identifies precisely this failure mode and solves it for the one component that can avoid writing the case row. The proposal cannot avoid it: recording that an action has been proposed *is* a lifecycle fact about the case, and the state machine says where it lives.

### A second, independent inconsistency in the same area

[06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Transaction boundaries froze the proposal as

> immutable proposal plus its single execution record in `DRAFT`, current pointer, idempotency, and audit in one **Share/Audit** transaction.

A Share/Audit transaction has no Core participant, so the frozen transaction structurally cannot perform the transition the state machine requires of it. Whichever way that is read — the transition is in the transaction, or it is a separate write — the version moves and the view goes stale.

## Decision

**Split the counter. `CommunityCase` carries two monotonic integers, and they answer different questions.**

```python
version: int  # optimistic concurrency / lifecycle row version
authorization_version: int  # monotonic epoch of case-owned disclosure authority
```

Both start at `1` when a case is created and both are strictly positive for the life of the case. Neither is ever decremented, reset, or reused.

| Counter | Question it answers | Who reads it |
|---|---|---|
| `version` | has this row moved since I read it? | every guarded case write's `expected_version` |
| `authorization_version` | has what this case may disclose changed since the view was compiled? | the compiler snapshot, the proposal validator, the send fence |

### 1. The central invariant

> **Lifecycle progress is not itself disclosure authority.**

A case moving through its state machine records what has *happened to* the case. It does not change which facts exist, what their statuses are, which mandates authorize them, or what a compiled view was allowed to say. Only a change to one of those may invalidate a view.

Concretely, for the transition that motivated this ADR:

```text
READY_FOR_ACTION -> ACTION_PROPOSED
    version               N -> N+1
    authorization_version A -> A
```

A valid proposal therefore does not stale the view that authorized it.

### 2. The bump table, exhaustive for V1

Every path that writes a `CommunityCase` row in V1 appears here. A path not in this table does not exist; adding one requires extending this table in the same change.

| # | Command / path | State effect | `version` | `authorization_version` | Why |
|---:|---|---|:---:|:---:|---|
| 1 | Monitor apply — case creation | `→ CANDIDATE` | starts at 1 | starts at 1 | a new case has a new authority epoch |
| 2 | Monitor apply — link a report and its facts into an existing case | unchanged | +1 | **+1** | new active facts and new report linkage change what the compiler evaluates and what independence counts |
| 3 | Candidate acceptance ([ADR-013](ADR-013-mandate-proposal-endpoint.md)) | `CANDIDATE → AWAITING_MANDATES` | +1 | **+1** | the same transaction creates mandate version 1 and its current pointers |
| 4 | Mandate decision — `APPROVE`/`ADJUST`/`REFUSE`/`REVOKE` | `AWAITING_MANDATES → INVESTIGATING`, or none | +1 | **+1** | a current mandate pointer, version, or terms hash moved |
| 5 | Investigation apply | `INVESTIGATING → READY_FOR_ACTION`, `READY_FOR_ACTION → INVESTIGATING`, or none | +1 | **+1** | writes fact `evidence_status` results, `corroboration_source_count`, and the assessment pointer, all of which the compiler reads |
| 6 | **Action proposal apply** | `READY_FOR_ACTION → ACTION_PROPOSED` | +1 | unchanged | records that a proposal exists; changes no fact, status, mandate, or count |
| 7 | Sender-return worker | `ACTION_PROPOSED → ACTIONED` | +1 | unchanged | records a send outcome |
| 8 | Proposal invalidation | `ACTION_PROPOSED → READY_FOR_ACTION` | +1 | unchanged | withdraws a proposal; the facts are untouched |
| 9 | Commitment creation | `ACTIONED → VERIFYING` | +1 | unchanged | records an external promise in the shareable zone |
| 10 | Contributor verification | `VERIFYING → RESOLVED` or `VERIFYING → READY_FOR_ACTION` | +1 | unchanged | records a human verification outcome |
| 11 | Human close | any allowed `→ CLOSED_UNRESOLVED` | +1 | unchanged | terminal lifecycle; disclosure is stopped by the **state** check, not by a counter |
| 12 | Terminal reopen | `RESOLVED`/`CLOSED_UNRESOLVED → INVESTIGATING` | +1 | unchanged | the new evidence that justifies a reopen bumped the authorization epoch when it landed |

**Read the table as one rule rather than twelve.** Rows 2–5 are the complete set of case-owned inputs the compiler evaluates: active facts and their values, statuses and evidence statuses; report linkage; evidence roots and safety; current mandate decisions, versions, revocations, and expiry corrections; and the investigation results that feed gate 17. Rows 6–12 are lifecycle.

An authorization-sensitive command increments **both**, exactly once, in the one transaction that makes the change. A lifecycle-only transition increments `version` and copies `authorization_version` forward unchanged.

### 3. What does *not* live in this counter

Three classes of change can invalidate a view and are deliberately **not** expressed as an authorization bump, because they are not case-owned:

- **Deployment and policy configuration** — the policy build hash, the compiler version, the destination registry version, and the routing token. These are already covered individually inside `authorization_snapshot_hash` and are re-checked by exact equality at proposal and fence time. A configuration change that touched every case's row would be a fan-out write to say something that is true of the deployment, not of any case.
- **Time.** Mandate expiry and view expiry are governed by the injected clock against `expires_at`, with equality at expiry meaning expired. A clock does not mutate a row, and a counter that had to advance to express the passage of time would be a scheduled write pretending to be a decision.
- **Case state itself.** That is the whole point of the split, and it is why the fence must check state explicitly (§6).

### 4. Both counters travel, and only one is authority

`ShareableCaseView` and `ActionProposal` each carry both:

| Field | Meaning |
|---|---|
| `case_version` | the Core OCC version observed at compile / proposal time. **Provenance only.** It answers "which row revision was this artifact built beside", and nothing consults it for authorization. |
| `authorization_version` | the epoch the artifact is valid against. **This is the freshness comparison.** |

`CurrentViewPointer` and `CurrentActionPointer` also carry `authorization_version`, so a caller can perform the exact staleness comparison from a single strongly read pointer without loading the artifact behind it.

**An old artifact's `case_version` is never a requirement that the Core row stop advancing.** Reading it that way is exactly the defect this ADR removes.

### 5. `authorization_snapshot_hash`

The coarse case term inside the snapshot becomes `case.authorization_version`. Everything else is unchanged: `case_id`, `corroboration_source_count`, `policy_build_hash`, `compiler_version`, `destination`, `purpose`, the evaluated current mandate pointers including optional excluded candidates, and the per-candidate `{fact_id, version, status, evidence_status}` tuples all stay exactly as Phase 6 computes them.

The comprehensive snapshot is **not** weakened. The coarse term is retargeted from a number that moves for two reasons to a number that moves for one, and every fine-grained binding beside it is untouched.

`case_version` may remain in the view as provenance, and does; it is simply no longer the epoch.

Golden view hashes change as a direct, reviewed consequence of this ADR. They are re-cut at implementation, in the manner [ADR-018](ADR-018-safe-evidence-and-compile-commit.md) established for a dependency bump that moves a golden: a reviewed change with re-cut vectors, never a floating expectation.

### 6. The freshness contract, restated for every consumer

**The compiler's compile transaction** keeps its single check-only Core participant and gains a second condition inside it:

```text
ConditionCheck on the case row:
    version               == the version the compile strongly loaded
    authorization_version == the authorization version the compile strongly loaded
```

Both conditions live in **one** participant. The compiler still never writes the case row, so requiring the exact OCC version costs it nothing and keeps its mid-flight race protection exactly as Phase 6 shipped it. The `ALLOW` transaction therefore remains **eight fixed participants** and the `DENY` three.

**The Action proposal validator** (pre-invocation, all reads strong) requires:

```text
case.state                 == READY_FOR_ACTION
case.version               == command.expected_case_version
case.authorization_version == view.authorization_version
pointer.view_id/view_hash  == the exact current view
now                        <  view.expires_at
```

**The Action proposal apply transaction** conditions its Core case update on the exact loaded `version`, the exact loaded `authorization_version`, and `state == READY_FOR_ACTION`, and writes `version + 1` with `authorization_version` unchanged. That is how a proposal advances lifecycle state without minting or altering disclosure authority.

**The send fence** (Phase 8) validates:

- the case exists and its **current state** is the exact state permitted for a send;
- `case.authorization_version` equals the proposal's and the view's;
- the view, proposal, and approval hashes and pointers;
- policy and compiler versions;
- the destination registry version and routing token;
- the mandate snapshot;
- expiry, against the injected clock.

It **must not** require that the current `CommunityCase.version` equal the proposal's pre-transition `case_version`, because lifecycle progression intentionally moved that number. The state check and the authorization-version check are separate obligations and neither substitutes for the other: the state check is what stops a closed, resolved, or already-actioned case from sending, and the authorization check is what stops a stale one.

## Alternatives considered

- **Leave one counter and exempt the `ACTION_PROPOSED` transition from bumping it.** Rejected: `version` is the optimistic-concurrency token, and a guarded write that does not move it lets a concurrent reader believe nothing happened. It trades a disclosure bug for a concurrency bug.
- **Leave one counter and teach the fence that "N or N+1 is acceptable when the difference is this proposal's own transition."** Rejected outright: it is the workaround the Phase-7 review forbade. It makes staleness depend on reconstructing *why* a number moved, which is precisely the question a counter cannot answer, and the tolerance would have to widen again the first time any other lifecycle edge appeared on the send path.
- **Remove `case_version` from `authorization_snapshot_hash` entirely and rely on the fine-grained bindings.** Genuinely smaller — one field and one check — and rejected because the coarse term is a deliberate catch-all. Report linkage and evidence-root changes have no per-candidate tuple of their own, and a snapshot that only covers what someone remembered to enumerate is weaker than one with a backstop. Naming the backstop correctly is worth more than deleting it.
- **Defer the `ACTION_PROPOSED` transition to approval, or to `SENT`.** Rejected: it moves the bump rather than removing it, so the fence sees the same mismatch one step later; and it contradicts the frozen `ACTION_PROPOSED → ACTIONED` edge, the "rejection leaves the case `ACTION_PROPOSED`" rule, and the approval UI's premise that a proposal is visible case state.
- **Make `ACTION_PROPOSED` a projection derived from the current action pointer rather than a case state.** Rejected as far larger than the problem: `CaseState` drives readiness reconciliation, Monitor linkage eligibility, mandate mutability, and close guards, and dissolving one member of it into a pointer would put the same fact in two places that can disagree.
- **Derive `authorization_version` as a hash rather than a counter.** Rejected: a monotonic integer is comparable, orderable in an audit trail, and expressible as a DynamoDB numeric condition. A hash would answer "different" without answering "newer", and a stale artifact could not be distinguished from a corrupted one.

## Why chosen

It separates two questions that were being answered by one number, and it does so by naming a distinction the documents already drew but could not enforce — [05-privacy-compiler-and-shareable-view.md](../architecture/05-privacy-compiler-and-shareable-view.md) already lists exactly which changes are authorization-sensitive, and a case state change is not among them. This is the same shape as [ADR-015](ADR-015-evidence-status-and-verification.md) making the verified-source rule executable and [ADR-019](ADR-019-send-fence-partition-isolation.md) making `W(fence only)` checkable: the sentence was already written and the mechanism could not express it.

It also retires a special case. The compiler's exemption existed because a compile that bumped the case version would stale its own view; under two counters that hazard is gone for every writer, and the compiler abstains from the case row for the plain reason that it has no business writing it.

## Consequences

- `CommunityCase` gains `authorization_version`; `chorus.domain.state.transition_case` gains an explicit per-edge authorization-bump decision from the table above, and `bump_case_authorization` always increments both.
- `ShareableCaseView` gains `authorization_version` (`shareable-case-view/v1` → `/v2`), which changes `view_hash` and therefore every golden view hash. Re-cut at implementation as a reviewed consequence.
- `CurrentViewPointer` (`/v1` → `/v2`), `CurrentActionPointer` (`/v1` → `/v2`), and `ActionProposal` (`/v1` → `/v2`, jointly with [ADR-021](ADR-021-action-grounding-and-caveats.md)) gain `authorization_version`. `ViewHistoryLocator` and `ActionHistoryLocator` are display locators and are unchanged; their `case_version` stays provenance.
- `authorization_snapshot_hash` swaps one term. No other field of it moves.
- The compile transaction's `stage_require_case_version` becomes a two-condition `CheckItem` in one participant; `ALLOW` stays at eight participants and `DENY` at three, and the arithmetic assertion over the staged plan is unchanged.
- The Action proposal apply gains a guarded Core case update conditioned on `version`, `authorization_version`, and `state` ([ADR-022](ADR-022-action-draft-preview-and-transaction.md) § the ten participants).
- The Phase-8 fence contract gains an explicit case-**state** requirement and explicitly drops any OCC-version equality requirement.
- **No V1 data has been deployed to AWS**, so every schema bump above is a code change with re-cut fixtures and no migration tooling. A reader encountering an item without `authorization_version` **fails closed** with an integrity error; it must never default, infer, or guess a lower value, because a guessed-low epoch is a view that looks fresher than it is.
- `04:§CommunityCase`'s compiler-only exemption paragraph is replaced by a reference to this ADR.

## Revisit condition

Revisit if a *lifecycle* transition is ever proposed that genuinely changes what a case may disclose. The fix is a new row in §2's table with its reason stated, never a blanket "state changes bump authorization" rule, and never a fence that tolerates a range of versions.

Revisit the placement of `authorization_version` in the snapshot only through a superseding ADR. Removing the coarse term is defensible only alongside an enumeration proving that every case-owned authorization input has its own fine-grained binding — which is not true today for report linkage or evidence-root changes.
