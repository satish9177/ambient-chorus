# ADR-027: What a model may propose about a reply, what grounds a commitment, and who alone may verify one

**Status:** Accepted
**Date:** 2026-09-07
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § Commitment, § Case state machine, § Transition contract, § Internal domain events; [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Shareable table mapping, § Transaction boundaries; [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) § External reply and commitment creation, § Resolution semantics; [08-api-design.md](../architecture/08-api-design.md) § Endpoint summary; [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) § Failure matrix; [ADR-020](ADR-020-case-authorization-version.md) § 2
**Depends on:** [ADR-026](ADR-026-inbound-reply-trust-and-correlation.md)

## Context

### The extraction path has no citation and no algorithm

[07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) requires that a commitment's "due time is explicitly supported by the cited reply". The only citation `ProposedCommitment` carries is `source_evidence_id` — *which reply*, never *which words*. Nothing in the contract lets deterministic code check that `due_at` came from the text rather than from the model, and [ADR-021](ADR-021-action-grounding-and-caveats.md) was written because exactly that gap on the outbound side ("lexically supported" had no algorithm) was unenforceable.

The asymmetry is worth naming. On the outbound side a model's prose is grounded against safe facts *the system already published*. Here a model's structured claim is grounded against text *an outside party wrote*. The direction is opposite; the requirement — a token in the output is supported only by a matching token in the source — is identical, and ADR-021 already ships the grammar.

### The Investigator apply cannot run from `ACTIONED`

[08-api-design.md](../architecture/08-api-design.md) says the reply "starts an Investigator operation". But the Phase-5 investigation apply writes fact statuses, `corroboration_source_count`, and the assessment pointer, and takes `INVESTIGATING → READY_FOR_ACTION`. From `ACTIONED` that edge does not exist, and widening it to make one command serve two purposes would mean a reply-triggered run could rewrite the evidence statuses of a case whose message has already been sent.

### Two guards are weaker than the documents they implement

`chorus.domain.state._case_guard` and `COMMITMENT_EDGES` disagree with [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § Transition contract and with invariant 13:

* `VERIFYING → READY_FOR_ACTION` requires only `context.commitment_missed`. No `actor_is_human`.
* `DUE → FULFILLED` and `DUE → MISSED` carry no human guard at all; only `CANCELLED` does.

The documents say both outcomes are the affected contributor's. The code says a system actor may take them. Nothing today constructs those calls — the watcher only takes `PENDING → DUE` — so this is a latent defect rather than a live one, and Phase 9 is the phase that would otherwise write the first caller.

### "Wednesday 10–12" is not a deadline

[demo-plan.md](../plans/demo-plan.md) stages the reply "Technician scheduled Wednesday 10–12." [ADR-021](ADR-021-action-grounding-and-caveats.md) § 6 rejects weekday and relative date constructs outright, precisely because their referent is the reading time. A design that let the model turn "Wednesday" into a UTC instant would be the "model invents a deadline" failure with a plausible cover story.

## Decision

### 1. Extraction is its own operation, over one reply and nothing else

A new `ApplicationOperationKind.EXTRACT_COMMITMENT`, added to `AGENT_INVOKING_OPERATION_KINDS`. It runs on the **Investigator** runtime under its own prompt version `commitment-extraction/v1`, bound by the existing agent handover identity ([ADR-016](ADR-016-agent-operation-handover-identity.md)) with `agent_binding_hash` over `{case_id, evidence_id, evidence_sha256}`.

Its input is the case identifier and the **single** inbound artifact's normalized `extracted_text`, delimited as untrusted data. Not the case, not other evidence, not facts, not mandates, not contributor data. The model that reads a stranger's email is given nothing else to leak.

The full investigation apply is therefore never reachable from `ACTIONED`, and `POST /v1/cases/{case_id}/investigations` keeps its existing state guard unchanged.

`InvestigationAssessmentDraft.proposed_commitments` **is retired as an authority source**. Phase 5 continues to validate its citation and discard it, exactly as `chorus.application.services.investigation_validation` does today; no commitment is ever created from it. There is one producer of commitments and it is this operation.

### 2. The extraction contract: spans, not quotes

`chorus.contracts.commitment.CommitmentExtractionOutput`, `schema_version = "commitment-extraction/v1"`, strict Pydantic, `extra=forbid`:

```text
schema_version, case_id, source_evidence_id
commitments: tuple[ProposedCommitmentDraft, ...]   max 3

ProposedCommitmentDraft:
  obligor_span:     SourceSpan     required
  action_span:      SourceSpan     required
  due_date_span:    SourceSpan     required
  obligor:          str[1..120]    normalized restatement
  action_text:      str[1..500]    normalized restatement
  due_at:           datetime       ADVISORY -- never read for authority
  refusal_detected: bool           ADVISORY

SourceSpan: {start: int >= 0, end: int > start}   character offsets into the exact
                                                   normalized extracted_text the model was given
```

**Spans and not quoted strings.** A quoted string can be fabricated; an offset pair either indexes the stored text or it does not. The text the offsets index is the same value persisted on the `EvidenceItem`, so the citation stays checkable forever, by anyone, with no reconstruction.

**`due_at` is measured and never read.** It exists so an evaluation can score how well the model reasons about dates and so a wrong belief is visible in the answer rather than invisible in the prompt — the same role `SufficiencyDraft.independent_source_count` already plays. The authoritative deadline is derived in § 4 from `due_date_span` alone. A model therefore has no field through which it can invent a deadline.

**What the contract has no field for**, and therefore what the model structurally cannot do: set a status, resolve or transition a case, name a destination, name a verification method, create an evidence status, or address anything outside `source_evidence_id`. The apply command holds no SES port, no compiler port, no scheduler port at the moment it reads model output, and no case-resolution verb.

`MAX_EXTRACTED_COMMITMENTS = 3`.

### 3. Grounding: nine deterministic checks, per proposal

Each proposed commitment is validated independently and rejected independently. A failure drops that proposal and does not discard its siblings. This differs from the whole-proposal rejection an Action draft gets, and the difference is the consequence: an invalid Action proposal would become an external message, while an invalid commitment costs nothing to drop — and dropping a valid sibling with it costs a real follow-up. Every rejection is audited with its code.

1. **Span validity.** Each span lies within `[0, len(extracted_text))`, `start < end`, and `end - start <= 200`. → `SPAN_OUT_OF_RANGE`.
2. **Structural safety.** `obligor` and `action_text` pass `action_grounding.structural_rejections` unchanged — no control, bidi, or invisible characters, no markup, no markdown link, no URL, no `mailto:`, no email address, no phone candidate, no unit pattern, no identifier shape, no quotation mark. → `COMMITMENT_TEXT_UNSAFE`.
3. **Lexical grounding.** `action_text` is grounded by `action_grounding.ground_field` against exactly one supporting text: the reply's normalized `extracted_text`. Every risk token (ISO date, clock time, ordinal, numeric, number word) must be supported by **match equality**, and every proper-name candidate by normalized substring. → `COMMITMENT_UNGROUNDED`. This is the direct answer to a model inventing a commitment: every number and every name in the restatement is one the reply itself contains.
4. **Obligor is asserted, not extracted.** `normalize(obligor)` must equal the safe `display_label` of the destination the correlated execution sent to — a value that comes from the non-secret safe destination configuration and the binding [ADR-026](ADR-026-inbound-reply-trust-and-correlation.md) § 5 recorded, never from the reply and never from the model. → `COMMITMENT_OBLIGOR_MISMATCH`. The "wrong responsible party" failure is closed structurally: the model's value is only ever checked for agreement with a fact correlation already established.
5. **The due date has exactly one permitted form.** `normalize(extracted_text[due_date_span])` must match `^[0-9]{4}-[0-9]{2}-[0-9]{2}$` and be a valid calendar date, and the span must contain no construct on [ADR-021](ADR-021-action-grounding-and-caveats.md) § 6's rejected-date list. → `COMMITMENT_DUE_NOT_ISO`.
6. **Unconditionality.** The **sentence containing `action_span`** — the extent bounded by the nearest `.`, `!`, `?`, or text boundary on each side — must contain none of the frozen conditional-and-refusal tokens, matched as whole normalized words:

   ```text
   if unless subject to pending tentative provided providing assuming
   may might could should would hope hoping try trying attempt attempting
   consider considering look looking review reviewing investigate investigating
   approximately approx around about possibly potentially likely
   cannot can't will not won't unable decline declining refuse refusing
   no not never
   ```

   → `COMMITMENT_NOT_UNCONDITIONAL`. "We'll look into it" fails on `look`; "we may repair elevator B" fails on `may`; "we will repair elevator B by 2026-09-10" passes. Check 5 independently rejects the first two for having no ISO date.
7. **Verification method** is the V1 constant `AFFECTED_CONTRIBUTOR_CONFIRMATION`. The model has no field for it.
8. **Range.** `received_at + 1 hour <= due_at <= received_at + 30 days`. → `COMMITMENT_DUE_OUT_OF_RANGE`. A past date fails the lower bound; there is no separate past-date rule to get wrong.
9. **At most one live commitment per action.** A `PENDING` or `DUE` commitment for the same `{case_id, action_id}` means the existing commitment is returned and nothing is created. → `COMMITMENT_ALREADY_ACTIVE`. This tightens [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md)'s "one per action/due term" to one per action, which is what the schedule generation and the `ACTIONED → VERIFYING` edge already assume. The frozen per-case cap of 20 still applies.

The commitment identifier is assigned by the application and is **UUIDv4**. [ADR-011](ADR-011-monitor-deterministic-identities.md) names `Commitment` explicitly among the entities that stay UUIDv4, and this ADR does not widen that exception: replay safety here does not need a derived identity, because it already has two stronger guarantees. The `CREATE_COMMITMENT` idempotency record is transaction B's own commit proof, so a redelivered apply replays the recorded result rather than running again; and check 9 refuses a second commitment for an action that already has a live one, whatever identifier a second attempt would mint. A derived identity would buy nothing and would put model-influenced text — `action_text` — inside an entity identifier, which [ADR-011](ADR-011-monitor-deterministic-identities.md) forbids in as many words.

`scheduler_name`, `schedule_generation`, and `due_event_id` are derived from `commitment_id` and `generation`, which is deterministic *given the row* and is what [ADR-028](ADR-028-deadline-watcher-and-scheduler-boundary.md) § 4 relies on.

### 4. Deadline semantics, exhaustively

`due_at` is the cited ISO date at **`T23:59:59.999999Z`**.

End of day, because a promise "by 2026-09-10" is kept at any hour of that day and an earlier instant would let a deadline pass before the promise could be. UTC, because no construct that carries a timezone survives check 5 — there is nothing to convert *from*. **[07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md)'s "in UTC after timezone conversion" is superseded**: the grammar removed the conversion rather than specifying it.

| The reply says | Outcome |
|---|---|
| exactly one ISO date, cited by the span | `due_at` = that date `T23:59:59.999999Z` |
| several ISO dates | the span selects one; the others are ignored |
| the span is not an ISO date | `COMMITMENT_DUE_NOT_ISO` |
| a relative expression — "within 3 days", "next week", "tomorrow" | rejected by check 5 |
| a weekday — "Wednesday" | rejected by check 5 |
| `14 January 2030`, `01/14/2030` | rejected by check 5 |
| a clock time — "10:00" | ignored; V1 is day precision only |
| no date at all | no commitment is possible |
| a past date | rejected by check 8 |
| a later reply naming a different date | check 9 refuses; V1 has no reschedule verb (§ 6) |

**V1 has exactly one commitment class:** an unconditional, dated, single-obligor promise. An `ACKNOWLEDGEMENT` or `UNDATED` class was considered and rejected — with no deadline there is no due event, so nothing schedules, nothing verifies, and the status would be one the system displays but cannot check. A status nothing reads is worse than no status.

**Consequence for the demo.** The staged reply text must contain an ISO date. `demo-plan.md` changes from "Technician scheduled Wednesday 10–12." to a reviewed fixture whose plain-text body states an explicit `YYYY-MM-DD`. The demo continues to show a real schedule; it stops showing a date the system invented.

### 5. The commitment state machine

The existing names stand — `PENDING`, `DUE`, `FULFILLED`, `MISSED`, `CANCELLED` — and no name is invented. Two guards are corrected.

| Edge | Actor | Required evidence | Idempotency | Transaction | Case effect |
|---|---|---|---|---|---|
| create `PENDING` | application | an attested inbound artifact and a proposal passing all nine checks | `CREATE_COMMITMENT`, `CASE` partition, key `sha256(evidence_id \| invocation_id)` | B (§ 7) | `ACTIONED → VERIFYING` |
| `PENDING → DUE` | commitment watcher | a due event whose `event_id` equals the row's `due_event_id`, matching namespace, case, and generation, and `now >= due_at` | the durable `due_event_id` plus the compare-and-swap; no idempotency record | D ([ADR-028](ADR-028-deadline-watcher-and-scheduler-boundary.md)) | none |
| `DUE → FULFILLED` | **affected contributor (human)** | explicit endpoint decision; optional safe evidence | `VERIFY_COMMITMENT`, `CASE` partition | F (§ 7) | `VERIFYING → RESOLVED` |
| `DUE → MISSED` | **affected contributor (human)** | explicit endpoint decision | `VERIFY_COMMITMENT`, `CASE` partition | F (§ 7) | `VERIFYING → READY_FOR_ACTION` |
| `PENDING → CANCELLED`, `DUE → CANCELLED` | human | fixed reason code | — | — | none |

`transition_commitment` gains the human guard on `FULFILLED` and `MISSED`, joining the one `CANCELLED` already has. `PENDING → DUE` becomes the **only** system-actor edge in the machine.

**Phase 9 implements no cancellation route.** The two `CANCELLED` edges stay in `COMMITMENT_EDGES` — removing a legal edge is a bigger change than not calling it — and a test asserts that no command in the codebase constructs one. A cancelled commitment would leave the case in `VERIFYING` until a human closes it, and V1 has no endpoint for that.

**There is no `DISPUTED` status.** A dispute has no deterministic consequence the state machine can take, and a status nothing reads misleads.

**The affected contributor** is a contributor owning at least one `ACTIVE` fact in the case, checked deterministically against loaded case facts. It is never a claim in the request body.

### 6. The case transitions Phase 9 owns

| Edge | Durable predicate | `version` | `authorization_version` |
|---|---|---|---|
| `ACTIONED → VERIFYING` | the `Commitment` created in **this transaction** stands `PENDING`; the guard is satisfied by the transaction's own participant and never by a caller-set flag | +1 | unchanged ([ADR-020](ADR-020-case-authorization-version.md) row 9) |
| `VERIFYING → RESOLVED` | the same transaction moves the commitment `DUE → FULFILLED`, and the actor owns an `ACTIVE` case fact | +1 | unchanged (row 10) |
| `VERIFYING → READY_FOR_ACTION` | the same transaction moves the commitment `DUE → MISSED`, and the actor owns an `ACTIVE` case fact | +1 | unchanged (row 10) |
| reply ingestion — **no edge** | an `EvidenceItem` lands in an `ACTIONED`/`VERIFYING` case | +1 | **+1** (new row 13, [ADR-026](ADR-026-inbound-reply-trust-and-correlation.md) § 6) |

Answering the questions directly:

* **A late reply cannot reopen a terminal case.** `RESOLVED` and `CLOSED_UNRESOLVED` refuse ingestion with `REPLY_CASE_TERMINAL`, the same rule the Monitor obeys.
* **An invalid or irrelevant reply cannot change case state.** An uncorrelated reply writes nothing in the case; a correlated but ungrounded reply writes the artifact and takes no edge.
* **A missed commitment reopens action, and only a human's `MISSED` does it.** Time passage produces a verification request and nothing else.
* **No Phase 9 case transition moves the authorization epoch.** Only the evidence write does.
* `ACTIONED → READY_FOR_ACTION` ("response requires another proportionate action") is **not Phase 9**. No command constructs it; a test asserts so.

### 7. Transaction shapes

Counts are asserted arithmetically against the staged plan, as Phase 5, 7, and 8 already are. They are derived here rather than copied from Phase 8.

**B — commitment apply. Six participants**, Core / Shareable / Audit, application:

1. `Commitment` in `PENDING`, create-only, Shareable `NS#n#CASE#k / COMMITMENT#c`;
2. the commitment schedule projection in `PENDING_SCHEDULE`, create-only, Shareable `NS#n#CASE#k / COMMITMENT_SCHEDULE#c`;
3. the guarded case update `ACTIONED → VERIFYING`, conditioned on the exact `version`, `authorization_version`, and `state`, moving `version` only;
4. the successful `EXTRACT_COMMITMENT` agent-invocation record (Core), so a redelivery learns the apply happened rather than spending a second model pass;
5. the `commitment.created` audit event;
6. the completed `CREATE_COMMITMENT` idempotency record, which is this plan's commit proof.

The schedule is created **outside and after** B ([ADR-028](ADR-028-deadline-watcher-and-scheduler-boundary.md)).

**B″ — no valid commitment. Two participants**: the `commitment.rejected` audit event carrying the per-proposal codes, and the completed `CREATE_COMMITMENT` idempotency record carrying the deterministic rejection response — so a redelivered command replays its answer instead of spending a second model pass over a stranger's email.

**`ACTIONED → VERIFYING` is not a transaction of its own.** It is participant 3 of B. The plan reads as though it were separate; it is not, and separating it would create a window in which a case is `VERIFYING` with no commitment.

**F — verification. Five participants**, Core / Shareable / Audit, application:

1. the guarded commitment update `DUE → FULFILLED` or `DUE → MISSED`, conditioned on the exact `version` and `status == DUE`, recording `verified_by_contributor_id`, `verification_evidence_id?`, and `outcome_note?`;
2. the guarded case update `VERIFYING → RESOLVED` or `VERIFYING → READY_FOR_ACTION`, conditioned on the exact `version`, `authorization_version`, and `state`, moving `version` only;
3. the current action pointer — moved to `INVALIDATED` on `MISSED`, conditioned on its exact row version and `proposal_hash`; a `ConditionCheck` on the same row version on `FULFILLED`. **The count does not move between the branches**, the [ADR-025](ADR-025-one-deliberate-ses-attempt.md) rejection/withdrawal precedent;
4. the `commitment.fulfilled` or `commitment.missed` audit event;
5. the completed `VERIFY_COMMITMENT` idempotency record, this plan's commit proof.

Participant 3 exists because [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) requires that "a subsequent action needs a fresh view/proposal/approval". Leaving the pointer live would let a case return to `READY_FOR_ACTION` with a spent proposal still current.

**One audit event per transaction.** There is no separate `case.verifying`, `case.resolved`, or `case.reopened` event: each transaction's single audit participant carries the case entity reference at its new version and a reason code naming the edge, exactly as `action.actioned` does.

**Ambiguous commits** are resolved by `UnitOfWork.resolve_outcome` against each plan's own commit proof, never by a blind retry and never by a second model call. An `EXTRACT_COMMITMENT` operation found `RUNNING` resumes by reading the durable agent-invocation record, and that record is proof only when scope, invocation identity, `agent == INVESTIGATOR`, prompt version, the input hash recomputed from the immutable evidence, and an exact `{COMMITMENT}` or empty result-reference set all verify.

### 8. Verification authority, stated as a closed list

| Source | May satisfy | May mark missed | May resolve the case | May grant `EvidenceStatus.VERIFIED` |
|---|---|---|---|---|
| the extraction model | no | no | no | no |
| a later external reply | no | no | no | no |
| deadline passage / the watcher | no | no | no | no |
| new ambient evidence | no | no | no | no |
| the affected contributor's explicit endpoint decision | **yes** | **yes** | **yes**, through `VERIFYING → RESOLVED` | no |

The allowed verification source set of [ADR-015](ADR-015-evidence-status-and-verification.md) **stays empty**. `VERIFIED` remains unreachable in policy/v1, and the authenticated external-source binding [ADR-026](ADR-026-inbound-reply-trust-and-correlation.md) adds does not change that: it answers who wrote a reply, not what the reply may establish.

This is invariant 13 — `ACTIONED` is not `RESOLVED` — carried through the phase that could most easily erode it.

## Alternatives considered

- **Ground the commitment against the whole reply with no spans.** Rejected: it is the `source_evidence_id`-only citation that made "explicitly supported by the cited reply" unenforceable in the first place, and check 3 alone would then admit a date the reply mentions in an unrelated sentence.
- **Read the model's `due_at` and require it to equal the derived value.** Rejected: it produces an equal-or-worse outcome — the derived value is already authoritative, so the comparison can only add spurious rejections — while leaving a field on the record that looks authoritative and is not.
- **Reject the whole extraction when one proposal fails.** Rejected: the Action precedent does not transfer, because a dropped commitment sends nothing and a dropped *valid* commitment loses a real follow-up.
- **Add an `UNDATED`/`ACKNOWLEDGEMENT` commitment class.** Rejected: no deadline means no due event, no watcher, and no verification — a status the UI shows and the system cannot check.
- **A model-scored confidence field gating acceptance.** Rejected: it is a similarity threshold with a different name, and [ADR-015](ADR-015-evidence-status-and-verification.md) and [ADR-012](ADR-012-candidate-grouping-invariant.md) both already refused one.
- **Let the watcher mark a commitment `MISSED` when the deadline passes with no response.** Rejected outright, and it is the single most tempting shortcut in this phase: time passage is evidence about the clock, not about the elevator. [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) already says so, and § 5 now makes the code say so too.
- **Reuse the Phase-5 investigation apply for reply-triggered runs.** Rejected: it would let a reply rewrite the evidence statuses of a case whose external message has already gone out.
- **Change the demo fixture's grammar instead of the reply text** — for example, admitting weekday dates when a reply is authenticated. Rejected: authentication proves who wrote "Wednesday", not which Wednesday.

## Why chosen

It gives the model exactly one job — point at the words — and gives every consequential value to deterministic code: the obligor comes from the correlation, the deadline comes from a cited ISO date, the identifier comes from a derivation, the status comes from a human. It reuses the grounding grammar Phase 7 already froze and tested rather than inventing a second one. It closes two guard defects while they are still latent. And it leaves the one question this system exists to be careful about — has the promise actually been kept — with the person who would know.

## Consequences

- New: `chorus.contracts.commitment`, `chorus.application.services.commitment_validation`, `chorus.application.commands.extract_commitment_operation`, `chorus.application.commands.apply_commitment`, `chorus.application.commands.verify_commitment`.
- `ApplicationOperationKind` gains `EXTRACT_COMMITMENT`, added to `AGENT_INVOKING_OPERATION_KINDS`.
- `IdempotentCommand` gains `EXTRACT_COMMITMENT`; `CREATE_COMMITMENT` and `VERIFY_COMMITMENT` already exist and are used as specified.
- `chorus.domain.state.transition_commitment` gains `actor_is_human` on `FULFILLED` and `MISSED`; `_case_guard` gains `actor_is_human` on `VERIFYING → READY_FOR_ACTION`.
- `Commitment` identifiers stay UUIDv4; [ADR-011](ADR-011-monitor-deterministic-identities.md)'s replay-identity exception is **not** widened.
- `COMMITMENT_SCHEDULE#c` is added to the Shareable table mapping.
- `ProposedCommitment` in `chorus.contracts.investigation` is unchanged and remains discarded; a test asserts no code path creates a `Commitment` from it.
- Audit gains `commitment.extracted`, `commitment.created`, `commitment.rejected`, `commitment.fulfilled`, `commitment.missed`; the observability document's existing names are reused unchanged.
- `demo-plan.md`'s staged reply text changes to contain an explicit ISO date.
- **SEC-23** is added: *a commitment's obligor is the correlated destination's safe label and its deadline is a cited ISO date; neither is ever a model-authored value.*
- New **T38**: prompt injection in an inbound reply instructing the extractor to produce a commitment, resolve the case, or emit a policy statement. New **T39**: a model marking a commitment satisfied or a case resolved.

## Residual risk

**Span selection among several dates.** A model may cite the wrong ISO date when a reply contains more than one. The consequence is bounded: it can only pick a date the reply actually states, the range check still applies, the commitment is visible before it matters, and one commitment per action caps the damage at one wrong deadline that a human then reports as missed.

**The conditional-token list is a closed list, and language is not.** A promise phrased to avoid every token — "the technician attends 2026-09-10" — passes as unconditional. This is accepted for the same reason [ADR-021](ADR-021-action-grounding-and-caveats.md) accepts its own conservatism: the list can only cause a valid commitment to be dropped or an unhedged sentence to be accepted, and an accepted commitment still requires a human to say it was kept.

**Extraction quality is a model property.** A model that finds no commitment in a reply that contains one costs a follow-up and costs nothing else. The direction of every failure in this ADR is toward doing less.

## Revisit condition

Reopen when a second commitment class is genuinely needed — the trigger is a reviewed corpus of real replies where the dated-unconditional class misses a promise the domain wants tracked — or when a deadline must be changeable, which requires a reschedule verb, a generation increment, and a superseding [ADR-028](ADR-028-deadline-watcher-and-scheduler-boundary.md). Adding a verification source is [ADR-015](ADR-015-evidence-status-and-verification.md)'s revisit condition, not this one, and stays a separate explicit ADR that must state the limit as well as the grant.
