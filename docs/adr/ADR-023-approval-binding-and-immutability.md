# ADR-023: What a human approval binds, why it is immutable, and where consumption lives

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § Approval, § ActionExecution; [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Shareable table mapping, § Transaction boundaries; [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) § Human approval contract, § Proposal and execution lifecycle; [08-api-design.md](../architecture/08-api-design.md) § Endpoint summary, § Propose, approve, execute; [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) § Complete failure matrix; [10-security-threat-model.md](../architecture/10-security-threat-model.md) § Threat register

## Context

Phase 8 is the first phase in which a human decision becomes an authorization artifact. The frozen contract describes that artifact in four places, and the four disagree in ways that only became checkable once somebody had to write the approval command.

### The approval hash covers fields that change

`chorus.privacy.canonical.hash_approval` is

```python
hash_value(approval, omit_fields=frozenset({"approval_hash"}))
```

and `chorus.domain.entities.Approval` carries `consumed_at`, `version`, and `updated_at`. [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) says `consumed_at` "changes once in the same transaction that claims execution". So the moment an approval is consumed, recomputing its own digest stops matching the digest stored beside it.

Nothing had noticed because nothing had ever recomputed it. Phase 8 is the phase that must: send-time revalidation exists to prove that the approval on file is the approval the human made, and a digest that a legitimate state change invalidates cannot prove anything. An integrity check that fails on the happy path gets deleted by the second person who meets it.

### The approver cannot be represented

`Approval.approver_id` is a `ContributorId`. The approving persona is `case_approver`, and `ApiContainer.contributor_by_actor` has no entry for it — deliberately, because [08-api-design.md](../architecture/08-api-design.md) grants `resident_a..resident_d` mandate and verification decisions and grants the approver "safe preview and approval/execute commands". They are different powers held by different personas.

Storing the approver as a `ContributorId` would require seeding a contributor for a persona that owns no fact and no report. A contributor is a counted thing in this system: `corroboration_source_count`, independence grouping, and mandate ownership are all defined over contributors. Minting one to satisfy a type would put a non-participant into the population that decides whether a case may act at all.

### One-active-approval was specified as a condition that cannot be written

[07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) says concurrent approvals "use a conditional `attribute_not_exists(active approval)`". The approval's key is `NS#n#ACTION#a` / `APPROVAL#{approval_id}`, and `approval_id` is minted per request. `attribute_not_exists` against a key nobody else will choose is true for every caller, always. The stated condition is not a condition.

### Rejection has two contradictory outcomes, one paragraph apart

[07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) § Human approval contract says both:

> Rejection … atomically sets the current action pointer's status to `INVALIDATED`, moves the `DRAFT` execution to `FAILED`, and **returns the case to `READY_FOR_ACTION` if readiness remains**.

> **Rejection leaves the case `ACTION_PROPOSED`** until the human re-proposes or closes.

[ADR-022](ADR-022-action-draft-preview-and-transaction.md) § 6 and [ADR-020](ADR-020-case-authorization-version.md) § 2 row 8 both state the first. The second is the older sentence and it is wrong, but it is wrong in a way that hides a real question the first does not answer: what happens when readiness does *not* remain.

### There is no path back from a definite send failure

[ADR-022](ADR-022-action-draft-preview-and-transaction.md) § 6 permits a new proposal only when `ACTION_CURRENT` names an `INVALIDATED` proposal whose execution is terminal `FAILED`. The failure matrix's remedy for `FAILED/SES_REJECTED` is "create and approve a fresh proposal". But the only path that sets a pointer to `INVALIDATED` is rejection, and rejection is defined over a `DRAFT` execution. After a definite send failure the execution is already `FAILED` and the pointer is still `DRAFT`, so the remedy the failure matrix names is unreachable.

The same hole swallows the question nobody had asked: whether a human who has approved may change their mind before the sender runs.

## Decision

### 1. An approval is immutable, and consumption is a property of the execution

`Approval` records one human decision and is never written twice. `consumed_at` is **removed**.

The execution reaching `SENDING` *is* the consumption, it is one-time by compare-and-swap on the execution's row version, and it is already durable in the state the sender must write anyway. `ActionExecution.approval_id` is set at `APPROVED` and monotonic presence forbids rewriting it, so "which approval authorized this attempt" and "has that approval been used" are answered by one row that one principal writes.

Recording consumption a second time on the approval bought nothing and cost three things: a mutable field inside an authorization digest, a second place the same fact could disagree, and a write grant on the proposal's own partition for whoever performed it ([ADR-024](ADR-024-execution-partition-and-sender-boundary.md) § 1).

`approval/v1` becomes `approval/v2`. No approval rows exist anywhere, so this is a code change with re-cut fixtures.

`hash_approval`'s omit set becomes `{approval_hash, version, created_at, updated_at}`. Those four are storage bookkeeping about the row, not the decision; every remaining field is written once and never changes, so recomputation is meaningful at any later instant. `ShareableRepositoryPort.stage_consume_approval` is removed.

### 2. The frozen `Approval` shape

```text
Approval
  approval_id: ApprovalId
  namespace, community_id, case_id, action_id, execution_id
  proposal_hash: Sha256Digest      # covers preview_hash, and therefore the whole preview tuple
  view_hash: Sha256Digest
  authorization_version: int       # the epoch current when the human decided. Provenance.
  approver_id_hash: Sha256Digest
  approver_assurance: ApproverAssurance
  decision: ApprovalDecision       # APPROVED | REJECTED
  approved_at: datetime
  expires_at: datetime
  approval_hash: Sha256Digest
  request_key_hash: Sha256Digest
  version, created_at, updated_at
  schema_version = "approval/v2"
```

`execution_id` is new and is what makes "consumed once by one execution" a statement about two named rows rather than about a convention.

`request_key_hash` replaces the raw `idempotency_key: str`. Caller text never enters a stored field, for the reason every other command in this repository already hashes it.

`expires_at` is `min(approved_at + 15 minutes, view.expires_at)`, so an approval never outlives the disclosure authority it was made against. Equality at expiry means expired, as it does everywhere else.

### 3. Approval binds transitively, and stores nothing it can derive

The long list of things an approval must bind — `case_id`, `action_id`, `execution_id`, `proposal_hash`, `preview_hash`, `view_id`, `view_hash`, `destination_id`, `destination_registry_version`, `routing_token`, `from_identity_id`, `template_version` — divides cleanly, and the division is the decision:

| Bound by | Values |
|---|---|
| stored on the approval | `case_id`, `action_id`, `execution_id`, `proposal_hash`, `view_hash`, `authorization_version`, approver, decision, `approved_at`, `expires_at` |
| bound **transitively** through `proposal_hash` | `view_id`, `preview_hash`, and — because `preview_hash` is `hash_value({template_version, from_identity_id, destination_id, destination_registry_version, routing_token, subject, text_body, html_body})` — the template version, the sending identity, the whole destination routing triple, and the exact bytes |
| deliberately **not** bound | the recipient address, the `Reply-To` address, the SES configuration set |

Copying a transitively bound value onto the approval would create a second copy of a fact the digest already fixes, and two copies of one fact can disagree. `proposal_hash` is a chain — proposal covers preview, preview covers routing and bytes — and a chain that is verified end to end is stronger than a flat list somebody has to remember to extend.

The three unbound values are unbound because no artifact a human or a model can see may contain them. `from_identity_id` is the opaque handle, and it resolves at deployment configuration time to exactly one `{From address, Reply-To address}` pair; binding the handle is therefore how the approval binds the letterhead without ever naming a mailbox ([ADR-025](ADR-025-one-deliberate-ses-attempt.md) § 6).

### 4. Human identity in V1, stated at its real strength

```text
ApproverAssurance
  DEMO_SHARED_TOKEN
```

One member, because there is one mechanism: a high-entropy shared access token validated against a Secrets Manager hash, after which `X-Chorus-Demo-Actor` selects a fixed persona. That is a **single-presenter demo access control and not authentication of a person.** It does not identify who approved; it identifies that somebody holding the demo token asserted the approver persona.

`approver_id_hash` is `sha256` of the persona identifier — the existing `actor_id_hash` — so an audit trail can distinguish the approver persona from the presenter persona and can never be read as naming a human being.

The enum has one member rather than a reserved second one, because an unreachable value is an invitation to write code that pretends the stronger case exists. Adding `PRODUCTION_AUTHENTICATED` requires the authentication ADR that R26 and T26 already say V1 does not have, and that ADR is what would decide what the value means.

### 5. Approval-time checks, and the one list that is not re-checked

An approval decision is validated against strong reads, before anything is staged:

| # | Check | On failure |
|---:|---|---|
| 1 | scope: the action belongs to this namespace, community, and case | `CrossCaseViolationError` |
| 2 | the current action pointer names this `action_id`, at status `DRAFT`, with this `proposal_hash` | 409 |
| 3 | the proposal loads, `proposal_hash` recomputes, and `preview_hash` matches the body | 409 `STALE_AUTHORIZATION` |
| 4 | the execution is this pointer's `execution_id`, in `DRAFT`, at `expected_execution_version` | 409 |
| 5 | the case is `ACTION_PROPOSED` and `case.authorization_version == proposal.authorization_version` | 409 `STALE_AUTHORIZATION` |
| 6 | the bound view loads, `view_hash` matches the body and the proposal, and `now < view.expires_at` | 409 `STALE_AUTHORIZATION` |
| 7 | current deployment configuration equals the proposal's by exact equality: `policy_version`, `compiler_version`, `policy_build_hash`, `template_version`, `from_identity_id`, and the destination's `destination_id`, `kind`, `registry_version`, `routing_token`, and `display_label` | 409 `STALE_AUTHORIZATION` |

Check 7 is the same list [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) check 2 applies at proposal time, and it is here for the same reason [ADR-020](ADR-020-case-authorization-version.md) § 3 gives: these are deployment-owned, they are not in `authorization_version`, and a verified `proposal_hash` proves the old values are internally coherent rather than still current. Refusing at approval is strictly kinder than letting a human approve a message the send fence will refuse.

**Note what is deliberately absent.** The approval does not check the case's OCC `version` against the proposal's recorded `case_version`. Lifecycle progression moved that number on purpose, and requiring it here would reproduce at approval time the deadlock [ADR-020](ADR-020-case-authorization-version.md) removed from send time.

**None of checks 3 to 7 applies to a `REJECTED` decision.** A human must always be able to say no. Rejecting a proposal that has gone stale is the correct response to a proposal that has gone stale, and a reject path that could be blocked by staleness would leave a case with a proposal nobody can approve and nobody can clear. Rejection runs checks 1, 2, and 4 only.

### 6. The one-decision boundary is the execution's row version

`attribute_not_exists(active approval)` is replaced by the condition that actually holds:

> Every decision moves the `DRAFT` execution out of `DRAFT` under `expected_version`. `DRAFT → APPROVED` and `DRAFT → FAILED` are both guarded compare-and-swaps on the same row, so exactly one of any number of concurrent decisions commits and every other one fails with `PersistenceConflictError`.

Two approvals race: one wins, the other is 409. An approval races a rejection: one wins, the other is 409. A replay under the same `Idempotency-Key` and request hash replays the recorded answer and writes nothing. A second, *different* decision under a *different* key is a conflict, not a correction.

The approval row itself is create-only under `APPROVAL#{approval_id}`, which prevents a decision from being overwritten; it is not, and never was, what prevents a second decision from being made.

### 7. A human cannot edit text, and a stale tab cannot approve

Both follow from the same binding and are stated here so neither is re-derived.

**No edit path exists.** The approval body carries no text field of any kind. An edit is a new proposal: the human rejects, which invalidates the pointer and frees the case, and a new `PROPOSE_ACTION` produces a new `action_id`, a new proposal, a new `preview_hash`, and a new decision. There is no field in which edited text could be submitted, so there is nothing to validate and nothing to render differently from what was approved.

**A stale browser tab is refused three ways over.** It holds an old `proposal_hash` (check 3), an old `expected_execution_version` (check 4), and an old `action_id` in its URL when the proposal has since been replaced (check 2 reads the pointer's current action). The transaction's own conditions repeat checks 2, 4, and 5 as participants, so a tab that passes the reads and loses the race still commits nothing.

### 8. Rejection, invalidation, and withdrawal

Three human verbs, one shared effect on the pointer, and different preconditions. The contradictory sentence in [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) is removed in favour of this table.

| Verb | Route | Legal execution state | Execution effect | Approval row |
|---|---|---|---|---|
| **approve** | `POST …/actions/{action_id}/approvals` with `decision=APPROVED` | `DRAFT` | `DRAFT → APPROVED` | created, `APPROVED` |
| **reject** | the same route with `decision=REJECTED` | `DRAFT` | `DRAFT → FAILED / PROPOSAL_REJECTED` | created, `REJECTED` |
| **withdraw** | `POST …/actions/{action_id}/invalidation` | `APPROVED` | `APPROVED → FAILED / APPROVAL_WITHDRAWN` | none; the original approval stands as the record of what was decided |
| **clear** | the same invalidation route | terminal `FAILED` | none — a `ConditionCheck` asserts it is already `FAILED` at the expected version | none |

Reject, withdraw, and clear all set the current action pointer to `INVALIDATED`, which is the only thing that frees the case for a new proposal under [ADR-022](ADR-022-action-draft-preview-and-transaction.md) § 6.

The invalidation route **refuses `SENDING`, `SENT`, and `SEND_UNKNOWN`**. `SENDING` is refused because a send is in flight and the honest answer to "may I take it back" is that nobody can. `SENT` is refused because a sent message cannot be recalled. `SEND_UNKNOWN` is refused because it is a quarantine: only reconciliation resolves it, to `SENT` or to `FAILED`, and a `FAILED` outcome is then clearable like any other.

**Withdrawal is a race, and the compare-and-swap is the referee.** A human withdrawing at `APPROVED@v` and a sender claiming at `APPROVED@v` are two conditional writes to one row; exactly one commits. If the sender wins, the withdrawal is 409 and the message goes; if the human wins, the sender's claim is 409 and it never renders a payload for SES. There is no window in which both believe they won, and there is deliberately no attempt to make the human always win — that would require holding a lock across an external call.

### 9. The case effect, and what happens when readiness does not remain

Every invalidating verb ends with the case, and the frozen guard is already `proposal_invalidated and readiness_remains`:

- **readiness remains** → `ACTION_PROPOSED → READY_FOR_ACTION`, `version N → N+1`, `authorization_version A → A`. Lifecycle only, exactly as [ADR-020](ADR-020-case-authorization-version.md) § 2 row 8 states.
- **readiness does not remain** → **no case edge is taken by this transaction.** The case stays `ACTION_PROPOSED`, and the participant that would have written it is a `ConditionCheck` on the exact `version`, `authorization_version`, and `state` instead.

The second branch is why the older sentence was not simply wrong. A proposal can be invalidated by a human at a moment when a mandate has since been revoked, and `ACTION_PROPOSED → INVESTIGATING` is the readiness-lost edge — which is authorization-sensitive, is owned by the deterministic readiness reconciliation, and is not something a rejection should mint. Rejection clears the proposal; readiness decides the state; neither does the other's job.

The participant **count does not change between the two branches** — a `PutItem` becomes a `CheckItem` in the same position — so the arithmetic assertion over the staged plan holds for both.

## Alternatives considered

- **Keep `consumed_at` and narrow the hash's omit set to cover it.** Rejected. It fixes the digest and leaves the real problem: whoever writes `consumed_at` needs a write grant on `NS#n#ACTION#a`, which by `dynamodb:LeadingKeys` is a write grant on the immutable proposal in the same partition. That is the [ADR-019](ADR-019-send-fence-partition-isolation.md) defect with a different item, and it would let a compromised sender rewrite the proposal it is about to send.
- **Seed a synthetic contributor for `case_approver`.** Rejected: a contributor is counted by `corroboration_source_count`, by independence grouping, and by mandate ownership. Minting one to satisfy a type would put a non-participant into the population that decides whether a case may act.
- **Store the approver's persona name alongside the hash.** Rejected as a small disclosure for no gain: [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) already calls approver identity operationally sensitive, the hash is stable, and the seed registry can resolve it for an operator who is entitled to.
- **Give the approval its own `active` slot key so `attribute_not_exists` becomes writable** — for example `APPROVAL#{action_id}`. Rejected: it adds a second mutual-exclusion mechanism beside the execution CAS that already provides it, and two locks over one decision is how a future change ends up holding one of them.
- **Bind `destination_id`, `routing_token`, and `from_identity_id` as explicit approval fields.** Rejected: `preview_hash` already covers all of them, `proposal_hash` covers `preview_hash`, and a second copy is a thing that can disagree with the first. The list is verified by *recomputation*, which cannot forget a field, rather than by comparison against a list somebody maintains.
- **Let a rejection re-check freshness like an approval.** Rejected outright: it would let a stale proposal become unrejectable, stranding the case with a proposal nobody can approve and nobody can clear.
- **Let a new proposal supersede a decided one automatically, removing the need for an invalidation verb.** Rejected again, for the reason [ADR-022](ADR-022-action-draft-preview-and-transaction.md) § 6 rejected it: a second command must not discard a human's decision without a human deciding to.
- **Refuse withdrawal after approval.** Rejected: an approval that cannot be withdrawn before anything external has happened makes the fifteen-minute expiry the only way to change one's mind, which is a worse answer than a race whose loser is told plainly.
- **Take `ACTION_PROPOSED → INVESTIGATING` inside the rejection transaction when readiness is lost.** Rejected: that edge bumps `authorization_version`, and a human clearing a draft message is not an authorization event. Two different facts would be committed by one command that only decided one of them.

## Why chosen

Each part removes a second copy of something. Consumption stops being recorded in two places, so the digest over the approval covers only fields that never move. The approver stops being modelled as a kind of person the system counts, so nothing is inflated to satisfy a type. One-active-approval stops being a condition nobody could write and becomes the compare-and-swap that was already doing the work. And the pointer gains the one verb that was missing, so the remedy the failure matrix already promised is reachable.

It also keeps the boundary [ADR-003](ADR-003-action-runtime-isolation.md) and [ADR-022](ADR-022-action-draft-preview-and-transaction.md) drew. The model contributes wording; the human contributes exactly one bit and binds it to a digest chain that reaches the bytes; and no field exists anywhere in the approval path through which edited text could enter.

## Consequences

- `Approval` loses `consumed_at` and `approver_id`, gains `namespace`, `community_id`, `execution_id`, `authorization_version`, `approver_id_hash`, `approver_assurance`, and `request_key_hash`, and becomes `approval/v2`. `ApproverAssurance` is added to `chorus.domain.entities`.
- `hash_approval` omits `{approval_hash, version, created_at, updated_at}`; a test asserts the digest is unchanged by every legal later write to the row, of which there are none.
- `ShareableRepositoryPort.stage_consume_approval` is removed from the port and both adapters. `CaseTransitionContext.approval_consumed` is satisfied by the execution having passed `SENDING`, which `SENT` implies.
- `POST /v1/cases/{case_id}/actions/{action_id}/invalidation` is added to [08-api-design.md](../architecture/08-api-design.md) § Endpoint summary, role `case approver`, guard "current pointer `DRAFT`; execution `DRAFT`, `APPROVED`, or terminal `FAILED`".
- The approval request body is frozen as `{decision, expected_execution_version, execution_id, view_hash, proposal_hash, preview_hash}`. `expected_action_status` is retired: it named a status where the transaction conditions on a row version, and two ways to say "the thing I saw" is one too many.
- [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md)'s "Rejection leaves the case `ACTION_PROPOSED`" sentence is replaced by § 8 and § 9 above.
- New failure-matrix rows: an approval whose deployment configuration moved since the proposal; a rejection of a stale proposal, which succeeds; a withdrawal that loses the race to a sender claim; and an invalidation attempted against `SENDING`, `SENT`, or `SEND_UNKNOWN`.
- Threat register gains **T32**: an approval artifact whose digest no longer verifies after a legal write, making integrity checks unenforceable — mitigated by an immutable approval and an omit set covering only row bookkeeping.
- Named tests gain `test_approval_hash_survives_every_legal_later_write`, `test_second_decision_on_one_draft_conflicts`, `test_stale_tab_cannot_approve_a_replaced_proposal`, `test_rejection_of_a_stale_proposal_succeeds`, `test_withdrawal_and_send_claim_race_has_exactly_one_winner`, and `test_invalidation_after_definite_send_failure_frees_the_case`.

## Revisit condition

Revisit `ApproverAssurance` only through an authentication ADR that states what a production approver identity is, how it is verified, and what an audit record of it may contain. Until then a second member is a claim the system cannot support.

Revisit the transitive binding in § 3 only if a value ever needs approving that `preview_hash` does not cover. The answer then is to put it inside the preview tuple, where recomputation reaches it, never to add a parallel field to the approval.
