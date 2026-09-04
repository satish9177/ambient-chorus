# ADR-015: Deterministic evidence status, the verified-source rule, and contradiction materiality

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [01-principles-and-invariants.md](../architecture/01-principles-and-invariants.md) § Testable security invariants; [03-agent-architecture.md](../architecture/03-agent-architecture.md) § Investigator / Skeptic Agent → Output; [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § Core enums, § CommunityCase, § InvestigationAssessment; [05-privacy-compiler-and-shareable-view.md](../architecture/05-privacy-compiler-and-shareable-view.md) § Exact evaluation order (gate 17); [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) § Complete failure matrix

## Context

### The rule that was named but never written

[03-agent-architecture.md](../architecture/03-agent-architecture.md) says deterministic validation "forbids a `VERIFIED` status without an allowed verification source", and the Phase-5 entry of [implementation-plan.md](../plans/implementation-plan.md) lists "evidence status rules including verified-source rule" among its tasks. A repository-wide search returns those two sentences and the enum member. Nothing defines an allowed verification source, nothing defines the other four statuses, and nothing says whether "forbids" means reject or downgrade.

`EvidenceStatus` is the only lifecycle enum in the domain with no frozen semantics. `CaseState`, `ActionExecutionState`, and `CommitmentStatus` each have an explicit edge set in `chorus.domain.state`; `EvidenceStatus` has an enum declaration and no rules. [12-evaluation-and-testing.md](../architecture/12-evaluation-and-testing.md) nonetheless sets an *evidence-status accuracy* target of at least 0.90 against expectations nobody has written down, and [05-privacy-compiler-and-shareable-view.md](../architecture/05-privacy-compiler-and-shareable-view.md) requires pairwise compiler coverage across a status dimension whose values mean nothing yet.

### Why the obvious verification answer is wrong

The tempting reading is that a `CLEAN` malware scan makes evidence verified — it is the only per-evidence quality signal V1 has, and [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) already gates export on it. That reading is rejected.

A clean scan is a statement about **bytes**, not about the world. It says a file was accepted into storage without matching a known-bad signature. A photograph of an out-of-service sign that passes a scan proves a photograph exists — not that the elevator failed, not when, and not that the submitter was in the building. Treating scan status as verification would let anyone who can attach a clean file mint the strongest status in the system, inverting the authority hierarchy in [01-principles-and-invariants.md](../architecture/01-principles-and-invariants.md): untrusted evidence sitting at the bottom would be producing the top-level claim.

### Why V1 has no verification source at all

The one candidate is the property manager's reply, which [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) ingests as private evidence bound to an approved destination. It is the only V1 input that does not originate from a resident's own assertion.

**But V1 cannot currently prove that provenance, and Phase 5 must not pretend otherwise.** `EvidenceItem` requires `submitted_by_contributor_id` and carries no durable binding to an approved destination — no destination identity, no registry version, no routing token, nothing that says *which* authenticated party authored the bytes. Adding a bare `provenance: EXTERNAL_REPLY` enum would record that something was called a reply without recording who sent it, which is not authentication; and the only way to make such an item satisfy the existing required-owner field would be to model management as a resident contributor, which is a falsehood written into private storage.

Inventing that binding now would be inventing later-phase persistence semantics inside Phase 5, under a schedule, to make a status reachable that Phase 5 has no use for.

### The second gap: nothing says how a model's status proposal is consumed

`evidence_findings[].proposed_status` is a model output. [01-principles-and-invariants.md](../architecture/01-principles-and-invariants.md) says LLMs propose and deterministic code decides, but the frozen documents never say what deciding looks like here. Two readings were live: reject an assessment containing an unsupported `VERIFIED`, or downgrade the finding and keep the rest. The first discards a whole valid skeptical assessment — contradictions, alternatives, gaps and all — over one over-confident field.

### The third gap: case corroboration was being read as fact corroboration

[05-privacy-compiler-and-shareable-view.md](../architecture/05-privacy-compiler-and-shareable-view.md) and SEC-11 define corroboration at **case** scope: unique contributors plus collapsed evidence roots, minimum two. That value drives readiness and compiler gate 17. It says nothing about whether any *individual* fact is corroborated, and an earlier draft of this ADR conflated the two — marking every contributing fact `CORROBORATED` because the case reached two sources. That is wrong in the dangerous direction: it would attach `CORROBORATED` to a detail exactly one person ever asserted, and `ShareableFact.evidence_status` carries that label outward.

### The fourth gap: two model fields claimed the same authority

`InvestigationAssessmentDraft` carries both `contradictions[]` and `evidence_findings[].proposed_status`, whose enum includes `CONTRADICTED`. Two fields able to produce one outcome is two chances to disagree, and it leaves undefined what happens when they do — a `proposed_status` of `CONTRADICTED` with no corresponding contradiction entry names no cited facts at all, so it asserts a conflict without saying with what.

## Decision

### 1. Evidence status is recomputed, never transitioned

`EvidenceStatus` gets **no edge set**. It is a pure classification recomputed from deterministic inputs each time an assessment is applied, in the manner of the derived `SUPERSEDED` and `EXPIRED` mandate statuses in [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md). There is no ordering for a defect to walk, and no stored "current status" constraining what the next honest recomputation may say.

### 2. The status ladder: the model may lower, never raise

Non-contradiction statuses carry a confidence order:

```text
VERIFIED  >  CORROBORATED  >  REPORTED  >  UNKNOWN
```

Resolution is:

```text
resolved(f) = CONTRADICTED                        if computed(f) is CONTRADICTED
            = weaker_of(computed(f), proposed(f)) otherwise
```

The model can push a fact toward `UNKNOWN` and can never push it toward `CORROBORATED` or `VERIFIED`. One rule does two jobs the plan asked for separately: "preserve `UNKNOWN`/`UNCERTAIN`" and "the model cannot grant `VERIFIED`" are the same sentence read in two directions.

`CONTRADICTED` is deliberately off the ladder and is neither raisable nor lowerable. A model that proposed the contradiction does not then get to soften its consequence.

### 3. Deterministic computation, frozen

For each `ACTIVE` fact `f` in the case:

```text
if f.fact_id appears in a validated contradictions[] entry:
    CONTRADICTED
elif f's exact canonical support group reaches CORROBORATION_MIN independent sources:
    CORROBORATED
else:
    REPORTED
```

That is the whole of it. Two statuses are deliberately absent from the deterministic result:

- **`VERIFIED` is unreachable**, because the allowed verification source set is empty (§4).
- **`UNKNOWN` is never produced by deterministic computation.** It is reachable only as a *resolved* status, when the Investigator legitimately lowers `CORROBORATED` or `REPORTED` to `UNKNOWN` through the ladder in §2.

An earlier draft carried a fifth branch — `UNKNOWN` when "deterministic inputs are incomplete in a way that did not rise to `IntegrityError`" — and it is removed. It described no condition anyone could implement twice the same way, which would have made the difference between `REPORTED` and `UNKNOWN` an implementation judgement rather than a rule. Incomplete lineage already has an answer: it raises `IntegrityError`, quotes nothing, and applies nothing. **A future deterministic `UNKNOWN`-producing condition requires an explicit documented rule in a superseding ADR, never an implementation judgement.**

`CONTRADICTED` is evaluated first, so it outranks every other outcome. A `MANAGEMENT_STATEMENT` could one day be *verified as uttered* and *contradicted as to truth* at the same time — that is evaluation scenario 5 — and `EvidenceStatus` holds one value. Rendering `VERIFIED` beside a statement the case's own evidence contradicts would be the most misleading label this system is capable of emitting, and `ShareableFact.evidence_status` carries it outward. Established provenance belongs in the finding's rationale and the audit trail, not in a promoted status.

### 4. The verified-source rule

**A `CLEAN` malware scan is not a verification source.**

**The allowed verification source set for policy/v1 is EMPTY.** No stored artifact carries an immutable, authenticated binding to a non-resident party, so no deterministic computation can produce `VERIFIED`.

Two consequences follow, and both are intended:

1. **Deterministically computed `VERIFIED` is unreachable.** No fact acquires it by any path.
2. **Every model-proposed `VERIFIED` is downgraded** to the computed status, audited with `EVIDENCE_STATUS_OVERCLAIM_DOWNGRADED`, and the assessment persists unchanged in every other respect.

This satisfies the frozen rule exactly. "Forbids a `VERIFIED` status without an allowed verification source" is fully honoured by an empty source set — indeed it is honoured in the strongest available form, because there is no source to mis-evaluate and no predicate branch a defect could make true.

**This is a complete and acceptable policy/v1 outcome, not a deferral.** V1 never claims to have verified anything, which is an honest description of what V1 can actually establish.

### 5. Fact corroboration is not case corroboration

Two distinct quantities, computed by the same frozen function over different fact sets, and they must never be substituted for one another.

**Case corroboration** — unchanged, and the only one with authority over the case:

```text
case_count = independent_source_count(all ACTIVE case facts, reports, items, resolved_roots)
```

It sets `CommunityCase.corroboration_source_count`, feeds the readiness guard's `independent_source_count >= 2` term, and is rechecked by compiler gate 17.

**Fact corroboration** — earned only by independent support for the *same exact deterministic claim*:

```text
fact_support_key(f) = (f.fact_type, hash_value(f.value))
```

where `hash_value` is the frozen RFC 8785 canonicalization in `chorus.privacy.canonical`. For each exact support-key group:

```text
group_count = independent_source_count(facts in that exact group,
                                       reports, items, resolved_roots)

group_count >= CORROBORATION_MIN  ->  CORROBORATED
otherwise                         ->  REPORTED
```

**No semantic similarity. No embeddings. No LLM grouping. No fuzzy threshold. Different typed values are different claims.**

Grouping by canonical bytes rather than by meaning is the whole point: it is decidable, reproducible, and impossible for a model to influence, because the value it groups on is the *stored* typed value that deterministic Monitor validation already accepted. Two contributors corroborate a fact only when they asserted the identical closed value, and duplicate reporters or duplicate roots collapse inside the group exactly as they collapse at case level, because it is the same function.

A case can be corroborated while most of its individual facts remain `REPORTED`. That is the correct and expected shape: several people reporting an elevator problem corroborates *the case*, while the specific minute one of them was trapped remains one person's account.

### 6. `contradictions[]` is the only path to `CONTRADICTED`

Deterministic code does **not** discover contradictions. It cannot read two statements and conclude they conflict; nothing in this system does semantic entailment. **The Investigator proposes the contradiction — that is model judgement, and it is exactly what the Investigator exists to supply.**

Deterministic validation then verifies only what it can actually verify:

- every cited `statement_fact_id` exists;
- every cited fact belongs to **this** case;
- citation cardinality and entity type are correct (2 to 10 `FactId` values, no duplicates);
- the output schema is well formed and `materiality` is a member of the closed enum.

Once a contradiction passes those checks, application code applies its **conservative consequence** deterministically: every cited fact resolves to `CONTRADICTED`.

**`contradictions[]` is the only model field whose validated content can cause a fact to resolve to `CONTRADICTED`.** `evidence_findings[].proposed_status = CONTRADICTED` has, by itself, **no authority** and must not change any fact's status: it names no cited facts, so it asserts a conflict without saying with what, and it is a claim the validator has nothing to check. Conversely, a fact appearing in a validated `contradictions[]` entry resolves to `CONTRADICTED` **regardless** of what `evidence_findings[].proposed_status` says for it — including `REPORTED` and `UNKNOWN`, because the ladder does not apply to `CONTRADICTED`.

One authority path, verified once, with a fixed consequence. The redundant field is not removed from the schema — it is one member of a closed status enum the model must still be able to express in its reasoning — but it is inert.

**The direction of effect is the invariant.** An accepted contradiction can only make the system *more* conservative: it can lower a fact's status, and it can block readiness. It can never, by itself, grant readiness, grant `VERIFIED`, widen a disclosure scope, authorize an identity, select a destination, or confer any other authority. A model that invents contradictions degrades the system's usefulness and cannot degrade its safety.

This is the same class of authority as `linkage_decision`: validated model judgement, consumed through a fixed rule, able only to block.

### 7. Contradiction materiality governs readiness

| Materiality | Effect on readiness |
|---|---|
| `LOW` | **nonfatal** — may coexist with readiness, on the condition that the contradicted facts are caveated downstream |
| `MEDIUM` | **blocks** readiness |
| `HIGH` | **blocks** readiness |

This makes the "ready only if nonfatal and caveated" row of [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) executable:

```text
contradictions_ok := not any(c.materiality in {MEDIUM, HIGH}
                             for c in assessment.contradictions)
```

The caveat obligation is discharged by the Action phase, not by Phase 5: contradicted facts carry `evidence_status=CONTRADICTED` into `ShareableFact`, and the Action proposal validator is where caveating them becomes mandatory. Phase 5 records the obligation; it does not pre-implement a later phase to satisfy it.

#### Reading an `investigation-assessment/v1` row through the v2 model

Readers accept `investigation-assessment/v1` and `investigation-assessment/v2`; writers emit **v2 only**, and nothing in Phase 5 produces a v1 row. A v1 row flattened contradictions into a single `contradiction_fact_ids` tuple, so **it recorded no contradiction materiality at all** — the field this section makes readiness depend on. A decoder therefore has to answer a question the stored bytes do not.

**Legacy contradiction materiality decodes as `HIGH`.** Where a v1 row cites two or more facts, its flat tuple is carried forward as exactly one `AssessmentContradiction` at `materiality=HIGH`, with the fixed description code `LEGACY_V1_CONTRADICTION_MATERIALITY_UNRECORDED`. Fewer than two cited facts names no conflict and carries forward nothing, because the flat tuple had no cardinality rule and a single ID is not a contradiction.

This is a **compatibility interpretation, not a reconstruction**:

- It fails closed. Per the table above `LOW` is the one value that does *not* block, so reading an unrecorded materiality as `LOW` would let a case whose materiality nobody recorded reach `READY_FOR_ACTION` on a value the decoder invented. `HIGH` is chosen over `MEDIUM` — both block today — because it is the reading that stays blocking if the table is ever loosened at `MEDIUM`, so the interpretation cannot silently become permissive by a later edit elsewhere. Blocking is recoverable by a fresh investigation; an external message sent on an invented materiality is not. This is the same direction as the "inflation is fail-safe" reasoning under *Materiality deflation* below.
- **No historical text or materiality is reconstructed.** The description is a fixed code, never recovered prose, so the row visibly says *this materiality was never recorded* rather than impersonating a judgement the model of the day never made.
- It grants the decoder no new authority. Materiality is still only ever *written* by a validated v2 assessment; the v1 path chooses how to read bytes that are already absent, and cannot raise, lower, or edit anything a v2 row recorded.
- It is not a migration. No v1 row is rewritten, backfilled, or upgraded in place; the interpretation is applied on read, and the decoded entity reports `schema_version = investigation-assessment/v2` because that is the shape it now has in memory.

`tests/unit/persistence/test_investigation_records.py` asserts exactly this behaviour — the `HIGH` materiality, the fixed description code, and the fewer-than-two-citations case.

### 8. An unsupported `VERIFIED` is downgraded, not rejected

A model-proposed `VERIFIED` resolves to the computed status, emits audit reason `EVIDENCE_STATUS_OVERCLAIM_DOWNGRADED` carrying the fact ID and both statuses, and the assessment persists.

This is deliberately different from the whole-output rejection a cited-ID violation triggers. An invented or foreign identifier proves the answer is not about the input it was given, so nothing in it can be trusted. An over-confident status is a *judgement* about real, correctly cited facts — the citations are valid, the contradictions may be sound, the gaps may be exactly right. Discarding all of that would spend another pass over private text to be told most of the same thing, and would let one field veto a skeptic that was otherwise working.

## Alternatives considered

- **`CLEAN` scan verifies.** Rejected: a statement about bytes is not a statement about the world, and it would let anyone attaching a clean file mint the strongest status in the system.
- **Add an `EvidenceItem` provenance flag in Phase 5 to make `VERIFIED` reachable.** Rejected: a bare enum records that something was *called* a reply without recording which authenticated destination sent it, so it is not authentication; and satisfying the existing required `submitted_by_contributor_id` would mean modelling management as a resident contributor, writing a falsehood into private storage.
- **Treat the empty source set as a temporary placeholder.** Rejected: an empty set is a complete answer to "forbids `VERIFIED` without an allowed source". Writing it down as provisional would invite a later phase to add a source as a matter of course rather than as a reviewed decision about what may be verified.
- **Reject any assessment containing an unsupported `VERIFIED`.** Rejected: it discards a valid skeptical assessment over one field, and re-asking spends another pass over private text for the same answer.
- **Keep the "incomplete inputs produce `UNKNOWN`" branch.** Rejected: it names no implementable condition, so two honest implementations would classify differently. Incomplete lineage already fails closed with `IntegrityError`.
- **Mark a fact `CORROBORATED` because its case reached two independent sources.** Rejected: it attaches `CORROBORATED` to details exactly one person ever asserted, and that label travels outward on `ShareableFact`.
- **Group facts for corroboration by semantic similarity or a time window.** Rejected outright: both require a threshold nobody has approved, both are model- or parameter-influenced, and both make corroboration irreproducible. Canonical-byte equality is decidable and unmanipulable.
- **Let `evidence_findings[].proposed_status = CONTRADICTED` set the status.** Rejected: it names no cited facts, so there is nothing to validate and nothing a reader could audit the claim against.
- **Remove `CONTRADICTED` from the `proposed_status` enum entirely.** Rejected: the model must be able to state its reading of a fact, and an enum missing the value would push that reading into free text where no validator can see it. Making the field inert is cheaper and clearer than making the vocabulary lie.
- **`VERIFIED` outranks `CONTRADICTED`.** Rejected: the resulting external label would be actively misleading.
- **Let the model set status freely and gate only at compile.** Rejected: `evidence_status` is persisted on the `Fact` and shown on the private investigation surface long before any compile, so the wrong label would already be the system's answer.

## Why chosen

It grants no status the system cannot deterministically justify; it makes the model's only accepted status influence a weakening one; it keeps a good assessment when one field overreaches; it separates the case-level quantity that carries authority from the fact-level label that travels externally; it gives `CONTRADICTED` exactly one verified path; and it invents nothing — the only constant it uses, `CORROBORATION_MIN = 2`, is already frozen, and the only grouping it uses is the RFC 8785 canonicalization Phase 1 already ships.

## Consequences

- A new `chorus.application.services.evidence_status` module owns computation, support-key grouping, and ladder resolution. It is pure over loaded state and takes no repository.
- `chorus.domain.facts` gains an additive `IndependenceResult` and `independent_sources()`; `independent_source_count()` becomes a thin wrapper. **No existing behaviour or test changes.** The case-level and fact-level computations call the same function with different fact sets.
- `chorus.application` imports `chorus.privacy.canonical.hash_value` for the support key — a permitted direction already precedented by the mandate proposal command.
- **`EvidenceItem` is not modified.** No provenance field, no provenance enum, no codec change, and no external-reply identifier list in the Investigator input.
- `proposed_commitments[].source_evidence_id` is validated in Phase 5 for existence and case membership only. The "must be an authenticated external reply" requirement of [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) is checked when the commitment is created, alongside the binding that makes it checkable.
- SEC-11 is unchanged. **SEC-21** is added: *a model-proposed evidence status may lower a fact's status and may never raise it.*
- The assessment codec accepts both `investigation-assessment/v1` and `/v2` and emits only `/v2`. Per §7 a v1 row's unrecorded contradiction materiality is read as `HIGH` under a fixed description code, which blocks readiness rather than guessing a permissive value; no stored row is rewritten and no historical text is reconstructed.
- Audit gains `EVIDENCE_STATUS_OVERCLAIM_DOWNGRADED`; observability gains `evidence.status.downgraded` and per-status counts on `investigation.applied`.
- `FactType.CONTRADICTION` and the compiler rule `p1.contradiction.safe.v1` are recorded as **reserved and currently unreachable in V1**: no producer exists, Phase 5 creates no facts, and contradictions are carried by the assessment and by `evidence_status=CONTRADICTED` on affected existing facts.
- The evidence-status accuracy metric in [12-evaluation-and-testing.md](../architecture/12-evaluation-and-testing.md) becomes measurable against §3, with `VERIFIED` expected zero times in V1.

## Residual risk

**Fact-level `CORROBORATED` will be rare, and that is a deliberate cost.** Exact canonical equality means a free-text `SERVICE_IMPACT` summary will essentially never group across contributors, and an `INCIDENT_OCCURRENCE` groups only when two reporters' `occurred_at` values are byte-identical after canonical UTC formatting. In practice `LOCATION_AREA`, a four-member closed enum, is the type most likely to corroborate. The system will therefore show a corroborated *case* composed largely of `REPORTED` *facts*.

This is accepted rather than mitigated. Case corroboration is the quantity with authority — it drives readiness and compiler gate 17 — and it is unaffected. The alternative is a similarity threshold, and a threshold that makes more facts look corroborated is a threshold that makes weaker evidence look stronger, which is the exact failure this ADR exists to prevent.

**Materiality deflation.** A model labelling a serious contradiction `LOW` can let a case reach `READY_FOR_ACTION` that a `MEDIUM` label would have blocked. Four things stand between that and a wrong external message, none of them the model: the deterministic terms must still pass (at least two independent case sources, `SAME_ISSUE`, a compilable purpose); the contradicted facts still carry `CONTRADICTED` outward; the Action validator must caveat them; and a human must approve the rendered preview. Inflation is fail-safe — it only blocks. Accepted rather than resolved, because deterministic materiality scoring is a similarity threshold nobody has approved.

**Fabricated contradictions.** A model can degrade usefulness by inventing contradictions among validly cited same-case facts, lowering statuses and blocking readiness. Per §6 the direction of effect is one-way, so this costs availability and never safety, and it is visible on the private investigation surface for a human to close or reopen.

## Revisit condition

The allowed verification source set stays empty until an ADR adds a member to it, and **adding one is never automatic**.

A later phase will need an immutable authenticated external-source binding for its own reasons — commitment validation requires knowing which approved destination authored a reply, and [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) already demands it. That binding must record the approved destination identity (destination ID, registry version, and routing token as they stood at ingestion) durably on the stored evidence, so "which authenticated party authored this" is answerable from the record rather than asserted by a flag. It must **not** model management as a resident contributor; if the existing required-owner field cannot express a non-resident author, the field, not the truth, is what changes.

**Merely adding that binding does not make the source eligible to grant `EvidenceStatus.VERIFIED`.** Authentication answers *who wrote this*; verification answers *what may this establish*, and the second does not follow from the first. Adding an allowed verification source is a separate, explicit ADR and policy decision that must review exactly which claim the source is permitted to verify — and must state the limit, not only the grant. The obvious candidate limit, should such an ADR ever be written, is that an authenticated management reply may verify a `MANAGEMENT_STATEMENT` **only as to what management itself stated**, never as to the underlying condition, and never where the reply's collapsed evidence root already belongs to the fact-owner's own lineage, because a party may not verify itself.

Every later source is a new named entry with its own provenance requirement and its own superseding ADR. The set is widened by adding members, never by relaxing what membership requires.

A future deterministic `UNKNOWN`-producing condition likewise requires an explicit documented rule in a superseding ADR.
