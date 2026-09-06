# ADR-022: A DRAFT execution that can exist, a preview the proposal owns, and the ten participants that commit them

**Status:** Accepted
**Date:** 2026-09-05
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md) § IAM notation and resources, § Principal-specific constraints, § Configuration contract; [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § ActionProposal, § ActionExecution; [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Transaction boundaries; [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) § Proposal and execution lifecycle, § Human approval contract, § Deterministic rendering, § Idempotency and ambiguous sends; [08-api-design.md](../architecture/08-api-design.md) § Propose, approve, execute; [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) § Complete failure matrix; [10-security-threat-model.md](../architecture/10-security-threat-model.md) § Threat register

## Context

### The DRAFT execution could not be constructed

[06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) and [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) both require a validated proposal to create one `ActionExecution` in `DRAFT`. `chorus.domain.entities.ActionExecution` makes four fields non-optional that cannot exist before a human has approved anything:

| Field | Why it cannot exist at `DRAFT` |
|---|---|
| `approval_id` | no approval exists; approval is Phase 8 |
| `idempotency_key` | [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) defines it as `sha256(namespace \| action_id \| execution_id \| proposal_hash \| view_hash \| approval_id)`, which needs the approval |
| `rendered_message_hash` | see below |
| `ses_request_token_hash` | minted by the sender immediately before the SES call |

`tests/fixtures/persistence.py` only ever builds executions from `APPROVED` onward, so nothing in the suite had exercised the one shape Phase 7 must write. A frozen entity that cannot express a frozen state is not a small defect; it is the state having been specified in two documents and modelled in neither.

### One hash was doing two jobs

[07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) requires the renderer to hash `{template_version, from_identity_id, destination_id, destination_registry_version, routing_token, subject, text_body, html_body}`, and [08-api-design.md](../architecture/08-api-design.md) has the human submit a `rendered_message_hash` with their approval against a `DRAFT`. [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) puts `rendered_message_hash` on `ActionExecution` and says "only sender mutates transitions after creation".

So the same field was both the artifact a human binds at approval time and the record of the bytes the sender prepared at send time. Those are different facts about different moments, and collapsing them is what forced a hash the sender owns to exist before the sender has run.

### `from_identity_id` did not exist

The string `from_identity_id` appears exactly once in the repository: inside that hash tuple. It is not in [02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md) § Configuration contract, not in `chorus.settings.Settings`, not in the destination registry description, and not in the view — while every other member of the tuple is available in Phase 7. A preview hash cannot be computed from a field nobody has defined.

### The proposal transaction was described as smaller than it is

[06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) froze it as "immutable proposal plus its single execution record in `DRAFT`, current pointer, idempotency, and audit in one **Share/Audit** transaction". That list omits the action history locator its own key grammar defines, omits any condition on the current view, and — being Share/Audit — has no Core participant at all, so it cannot perform the case transition the state machine requires or check the send fence every other authorization-sensitive write checks.

The missing view condition is the serious one. [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) check 3 is a *read* ("current pointer equality") and check 12 says "conditional persistence" without saying conditional on what. Between the read and the write sits an entire model invocation, and a compile committing in that window moves `VIEW_CURRENT` while the Action model is still answering about the old view.

### Re-proposal had no rule

Nothing said what happens to a live `DRAFT` execution when a second proposal is requested. `DRAFT → FAILED` is a legal edge, but no document assigns anyone the job of taking it, so a case could end up holding two live `DRAFT` executions and two candidate proposals for one authorization.

## Decision

### 1. `ActionExecution` fields are present per state, and presence is monotonic

`action-execution/v1` becomes `action-execution/v2`. The four fields above become nullable, governed by an explicit presence table in the style [ADR-016](ADR-016-agent-operation-handover-identity.md) used for the handover pair.

| Field | `DRAFT` | `APPROVED` | `SENDING` | `SENT` | `FAILED` | `SEND_UNKNOWN` |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| `execution_id`, `action_id`, `case_id`, `proposal_hash`, `view_hash`, `state`, `attempt_number`, `version`, timestamps | required | required | required | required | required | required |
| `approval_id` | **null** | required | required | required | may be null | required |
| `idempotency_key` | **null** | required | required | required | may be null | required |
| `ses_request_token_hash` | **null** | **null** | required | required | may be null | required |
| `rendered_message_hash` | **null** | **null** | required | required | may be null | required |
| `started_at` | null | null | required | required | may be null | required |
| `ses_message_id` | null | null | null | required | null | null until reconciled to `SENT` |
| `finished_at` | null | null | null | required | required | required |
| `failure_code` | null | null | null | null | required | null |
| `reconciled_at` | null | null | null | set only by reconciliation | null | set when reconciled |

**Presence is monotonic.** A field that has been set is never unset and never rewritten. The entity refuses a transition that would clear one.

The three "may be null" columns under `FAILED` are the honest ones and the reason the table is not a simple ladder. `DRAFT → FAILED` (the proposal was invalidated or expired) reaches a terminal state having never had an approval, and `APPROVED → FAILED` with `STALE_AUTHORIZATION` reaches it having never rendered anything or contacted SES. Requiring a rendered hash on a failure that happened before rendering would have forced a fabricated digest onto the record of a message that was never built.

`idempotency_key` becomes available exactly when `approval_id` does, because the frozen formula depends on it. Its formula is unchanged.

### 2. The preview hash belongs to the proposal; the rendered hash belongs to the execution

Two fields, two owners, two moments:

| Field | Lives on | Written by | Binds |
|---|---|---|---|
| `preview_hash` | `ActionProposal`, immutable | Phase-7 proposal apply | the exact preview a human is shown and approves |
| `rendered_message_hash` | `ActionExecution` | Phase-8 sender, at `APPROVED → SENDING` | the exact bytes prepared for one SES attempt |

Both are computed by the same deterministic renderer over the same canonical tuple, so a correct system produces the same digest twice and a mismatch is `STALE_AUTHORIZATION` before the SES call. Naming them separately is what makes that comparison *checkable* rather than tautological: with one field, "the sender rendered what the human approved" was a claim nothing could disagree with.

`proposal_hash` covers `preview_hash`, so an approval binding a proposal hash transitively binds the preview.

The approval request body's field is renamed `preview_hash` in [08-api-design.md](../architecture/08-api-design.md). A mismatch remains 409.

### 3. The rendered bodies are not persisted

Only `preview_hash` is stored. Neither `text_body` nor `html_body` is written to any table.

[04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) already states the principle — "the exact rendered body is reconstructible from immutable proposal/view/template version and its hash" — and the renderer is a pure function of immutable inputs, so a stored body could only ever agree with a regenerated one or be a second version of the truth. `GET /v1/cases/{case_id}` regenerates the preview on read.

### 4. `from_identity_id` is safe deployment configuration

```dotenv
CHORUS_SES_FROM_IDENTITY_ID=chorus-demo-sender    # opaque stable identity ID; never the address
```

It is:

- **non-secret**, and therefore ordinary deployment configuration rather than a Secrets Manager read — the same class [02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md) already assigns to the safe destination label, registry version, and routing token;
- an **opaque stable identifier** for the verified sending identity, **not** the `From` email address. The sender resolves the address from its own destination/identity secret; nothing outside the sender ever holds it;
- **available in Phase 7**, which is what makes the preview hash computable at proposal time;
- **identical** on both sides, which is what makes the Phase-8 reconstruction comparison meaningful.

It belongs inside the hash because approval must bind *who the message claims to be from*. A preview a human approved that could be sent under a different sending identity would be an approval of the words and not of the letter.

No agent runtime gains Secrets Manager access. The Action runtime never sees this value at all — it is a renderer input, and the renderer runs in application code.

### 5. Renderer inputs, tree, and hash

**Renderer version:** `email/property-manager/v1`. Documents use the single term **`template_version`** for this value throughout; "renderer version" as a second name for the same thing is retired.

**Inputs, exhaustively:**

1. the validated immutable `ActionProposal`;
2. its exact bound `ShareableCaseView`;
3. `template_version`;
4. `from_identity_id`.

It receives no recipient address, no Core state, no compiler audit projection, no private evidence, no model completion, and no destination secret. It never imports a private type.

**One intermediate document tree; both outputs are pure functions of it.**

```text
Document
  subject: Text
  blocks:
    Greeting
    Framing                                   # fixed template copy, tone-selected
    Section "Evidence-backed observations"
      NumberedList[ ClaimItem{ index, text, marker } ]   # proposal order
    Section "Requested action"
      Paragraph{ requested_action }
      DeadlineLine?                            # day precision, or fixed fallback copy
    Section "Caveats"?                         # omitted entirely when there are none
      BulletList[ CaveatItem{ text, markers } ]
    Section "References"
      DefinitionList[ { marker, short_export_fact_id } ]
    CaseReference
    ClosingNote                                # fixed template copy
```

Claims render in proposal order and markers are `C1..Cn` by that order. Caveat markers reuse the same marker vocabulary, so a caveat citing the fact behind `C2` renders `[C2]` and the References block stays a single list.

**Plain text:** UTF-8, `\n` line endings only, deterministic section order, deterministic blank-line separation. **HTML:** generated from the same tree, every dynamic string escaped by the HTML writer, no raw model markup, no `<script>`, no `<style>`, no link, no remote asset, no attachment, no inline style attribute.

**Hash**, over the canonical mapping, through the existing `chorus.privacy.canonical` authority:

```text
preview_hash = hash_value({
  template_version, from_identity_id,
  destination_id, destination_registry_version, routing_token,
  subject, text_body, html_body,
})
```

`destination_id`, `destination_registry_version`, and `routing_token` come from `view.destination`, so the hash binds the routing the compiler authorized rather than the routing configured at send time.

**A rendered result over 100 KiB rejects the whole proposal.** It never truncates, never drops a section, and never silently omits a caveat.

### 6. Re-proposal does not supersede a live DRAFT

A proposal may be created when **either**:

- **A.** no `ACTION_CURRENT` pointer exists for the case; or
- **B.** `ACTION_CURRENT` names a proposal whose status is `INVALIDATED` and whose execution is terminal `FAILED`.

A request arriving while a valid current `DRAFT` stands is a **conflict**, refused before any model call, with nothing written.

Phase 7 therefore never mutates an existing execution. Invalidation is Phase 8's explicit human path — reject, or request an edit — and that transaction atomically sets the current pointer's status to `INVALIDATED`, moves the `DRAFT` execution to `FAILED`, and returns the case to `READY_FOR_ACTION` if readiness remains. Only after that may a new proposal replace the pointer.

Two live `DRAFT` executions for one case therefore cannot exist, and the case never holds two competing candidate messages. The alternative — letting a new proposal quietly fail the old one — would let a second model call discard a human's pending decision without anyone deciding to.

`MAX_ACTIONS_PER_CASE = 10` continues to bound the total, checked by `assert_action_capacity` before invocation.

### 7. The current-view condition, and the IAM it costs

A new **check-only** repository method:

```text
stage_require_current_view_pointer(scope, *, expected: ViewPointerExpectation) -> CheckItem
```

It conditions on the exact strongly read `view_id`, `view_hash`, and pointer **row version**. It writes nothing and returns a `CheckItem`, never a `PutItem`.

So a compile that installs a newer current view while the Action model is answering causes the proposal transaction to fail whole: no proposal, no `DRAFT` execution, no pointer movement, no case transition. This is the Phase-7 counterpart of the exact-pointer condition the compile already uses to make view-pointer rollback impossible.

**The application must never be given view-mutation authority in order to perform this check.** The IAM consequence is stated explicitly so it cannot be met by the lazy route:

- the application principal **may** hold `dynamodb:ConditionCheckItem` on `NS#*#VIEW_CURRENT#*`, scoped by `dynamodb:LeadingKeys`, and preferably further constrained with `dynamodb:EnclosingOperation` equal to `TransactWriteItems` where the frozen policy can express it;
- the application principal **must not** hold `PutItem`, `UpdateItem`, or `DeleteItem` on the `NS#*#VIEW#*` or `NS#*#VIEW_CURRENT#*` prefixes.

AWS authorizes a transaction through the permission each participant needs, which is what makes a read-only transactional authority expressible at all — the same property [ADR-019](ADR-019-send-fence-partition-isolation.md) relied on for the compiler's read-only case guard. Static negative-capability assertions over the synthesized policy prove that no allow statement grants the application a write to a compiler-owned view prefix, in the manner [ADR-019](ADR-019-send-fence-partition-isolation.md) established.

`*` in the trust matrix of [02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md) is amended accordingly: the application's Shareable access becomes `RW(action/case prefixes) / R + ConditionCheck(view prefixes)`.

### 8. The ten participants of the proposal apply transaction

The transaction is **cross-table Core / Share / Audit** and has exactly ten fixed participants, independent of how many claims or caveats the proposal contains.

| # | Participant | Table | Kind |
|---:|---|---|---|
| 1 | immutable `ActionProposal` | Share | create-only Put |
| 2 | `ActionExecution` in `DRAFT` | Share | create-only Put |
| 3 | `CurrentActionPointer` | Share | conditional Put on `ActionPointerExpectation`, or create-only when absent |
| 4 | `ActionHistoryLocator` | Share | create-only Put |
| 5 | current-view identity check | Share | **ConditionCheck** (§7) |
| 6 | successful `ACTION` `AgentInvocationResult` | Core | create-only Put |
| 7 | `action.proposed` `AuditEvent` | Audit | append-only Put |
| 8 | completed action-apply idempotency record and commit proof | Share | create-only Put |
| 9 | guarded `CommunityCase` update: `READY_FOR_ACTION → ACTION_PROPOSED`, `version N → N+1`, `authorization_version A → A` | Core | conditional Put on exact `version`, `authorization_version`, and `state` |
| 10 | no-live-send-fence condition | Core | **ConditionCheck** |

The count is fixed and asserted arithmetically against the staged plan, in the manner Phase 5 used for the investigation apply and Phase 6 for the compile.

**Participant 6 is not optional, and it is not bookkeeping.** The `PROPOSE_ACTION` operation is asynchronous, so the worker's `RUNNING → SUCCEEDED` status write happens *after* this transaction and can be lost. The durable successful invocation record, committed atomically with the proposal, is what a redelivery reads to learn that the apply already happened. Without it, a lost status write would leave an operation that looks unfinished over state that is complete, and the only way to find out would be a second model pass over the same view.

**The `ApplicationOperation` status row is deliberately *not* participant 11.** It is an application projection, not an authorization artifact, and adding it would put a status write inside the transaction that owns the authorization commit. Recovery through participant 6 is strictly better: it proves what actually committed rather than what a worker intended.

### 9. Two idempotency records, following the Phase-5 shape

| Record | Command family | Partition | Key hash | Written by |
|---|---|---|---|---|
| route/start reservation → the durable `PROPOSE_ACTION` operation | `PROPOSE_ACTION` | `NAMESPACE` | `key_hash(idempotency_key)` | the API route, completed by `complete_start` |
| action-apply commit proof | `PROPOSE_ACTION` | `ACTION` | `key_hash("propose-action\x1f" + idempotency_key)` | participant 8 above |

This is exactly the separation the asynchronous investigation already uses: one command family, two records distinguished by contextual partition and by a domain-separated key hash, because they are commit proofs for two different transactions. `IdempotentCommand.PROPOSE_ACTION` and `IdempotencyPartitionKind.ACTION` both already exist.

Behaviour:

- same route key and same request hash → the same `operation_id` and the same `agent_invocation_id`; **no second model call**, no second proposal, no second `DRAFT` execution;
- same route key, different request hash → `IDEMPOTENCY_CONFLICT` (409), zero mutations;
- an unknown apply-transaction outcome → resolved by reading the commit proof and the durable invocation record **before** any retry, never by re-invoking.

### 10. Operation recovery

If the apply committed but the `RUNNING → SUCCEEDED` status write was lost, a redelivery:

1. proves the seven handover facts against the operation row ([ADR-016](ADR-016-agent-operation-handover-identity.md));
2. strongly loads the durable `ACTION` `AgentInvocationResult` by `invocation_id`;
3. finds a completed record whose input hash matches, concludes the apply is already durable;
4. transitions the operation to `SUCCEEDED`;
5. **invokes no model.**

There is no plan snapshot, no apply-progress record, and no `RUNNING → PENDING` edge. `PROPOSE_ACTION` applies in one transaction and therefore has nothing to resume — the same reasoning [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) already records for `INVESTIGATE`.

## Alternatives considered

- **Create the execution at approval instead of at proposal.** Rejected: [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md), [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md), and [08-api-design.md](../architecture/08-api-design.md) all assume a `DRAFT` exists before approval — the approval body carries `expected_execution_version` — and a `DRAFT` is what the UI shows as a pending action.
- **Fill the DRAFT's absent fields with sentinel digests.** Rejected outright: a placeholder hash in a field whose entire purpose is to bind exact bytes is a lie the storage layer would then be unable to distinguish from a real binding.
- **Keep one `rendered_message_hash` and let Phase 7 write it.** Rejected: it makes "the sender sent what the human approved" unfalsifiable, and it required `from_identity_id` and a send-time token to exist before either was knowable.
- **Persist the rendered bodies so the approval UI cannot drift.** Rejected: the renderer is deterministic over immutable inputs, so a stored body is a second copy that can only agree or be wrong, and it would put the exact external message text into a table [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) forbids it from reaching in logs.
- **Read `from_identity_id` from the destination secret at render time.** Rejected: it would give the application, and eventually the preview query, a Secrets Manager permission it does not otherwise need, to obtain a value that is not secret.
- **Let a new proposal automatically invalidate the current one.** Rejected: a second model call would discard a human's pending decision without a human deciding to, and the mechanics — invalidating a pointer and failing an execution — are exactly the ones Phase 8's explicit reject path already owns.
- **Give the application `UpdateItem` on `VIEW_CURRENT` so it can condition on the row.** Rejected, and named so it is refused once: a condition check must not become a write grant. The compiler is the sole creator of views by IAM and not by convention, and that is worth a distinct action rather than a comment.
- **Add the `ApplicationOperation` status as an eleventh participant.** Rejected: a status projection inside the authorization transaction couples the two, and the durable invocation record already answers the recovery question more truthfully.

## Why chosen

Each part replaces an impossibility with a state that exists. A presence table lets a `DRAFT` be written down. Two hashes let a comparison be made. One configuration value lets the hash be computed. A `ConditionCheck` lets a race be lost safely without handing anybody a write. An enumerated participant list lets the transaction be asserted rather than assumed. And a re-proposal rule lets a human's pending decision survive a second request.

Nothing here reopens a semantic. `EvidenceStatus`, the compiler gates, the send fence's ownership rules, the approval contract, the one-attempt execution rule, and the `SEND_UNKNOWN` quarantine are all untouched.

## Consequences

- `ActionExecution` gains four nullable fields and a state-dependent presence invariant; `action-execution/v1` becomes `/v2`; the codec reads and writes the nullable shape.
- `ActionProposal` gains `preview_hash`, covered by `proposal_hash`; combined with [ADR-021](ADR-021-action-grounding-and-caveats.md)'s structured caveats and [ADR-020](ADR-020-case-authorization-version.md)'s `authorization_version`, the proposal schema becomes `action-proposal/v2` in one change.
- `CHORUS_SES_FROM_IDENTITY_ID` is added to [02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md) § Configuration contract and to `chorus.settings.Settings`; `.env.example` gains a safe placeholder.
- `ShareableRepositoryPort` gains `stage_require_current_view_pointer`, implemented by both the DynamoDB and in-memory adapters and covered by the shared repository contract suite.
- The application role's Shareable statement gains `dynamodb:ConditionCheckItem` on the view prefixes and gains no write there; a static assertion proves it.
- [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Transaction boundaries' proposal row is replaced by the ten-participant list, and is cross-table rather than Share/Audit.
- [08-api-design.md](../architecture/08-api-design.md)'s approval body field is renamed to `preview_hash`.
- A new failure-matrix row records a current-view pointer that moves while the Action model is running: the transaction fails whole, the operation records `STALE_AUTHORIZATION`, and **no automatic second invocation** occurs.
- The Phase-7 test matrix gains `test_draft_execution_round_trips_through_codec`, `test_current_view_pointer_move_during_invocation_persists_nothing`, `test_second_proposal_against_live_draft_conflicts_without_model_call`, `test_lost_operation_status_recovers_from_durable_invocation_record`, and `test_preview_hash_inputs_require_no_secret_read`.

## Revisit condition

Revisit the presence table if a state is ever added to `ActionExecutionState`; the table gains a column, never a nullable field with an implicit rule.

Revisit the separation of `preview_hash` and `rendered_message_hash` only if the sender ever stops re-rendering, which would mean approval no longer binds the bytes actually sent. That is a change to what approval means and needs its own ADR.

Revisit the re-proposal rule only alongside a decision about what happens to a human's pending approval, stated explicitly. An automatic supersede is acceptable only if somebody has decided a pending decision may be discarded, and recorded who.
