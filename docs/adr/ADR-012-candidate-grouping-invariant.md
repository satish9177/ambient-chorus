# ADR-012: A candidate case is grouped only under an issue type that names a subject

**Status:** Accepted
**Date:** 2026-09-03
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [03-agent-architecture.md](../architecture/03-agent-architecture.md) § Candidate grouping

## Context

### What the architecture said before

Candidate grouping was decided entirely from the model's own answer. A `candidate_link` either named an `existing_case_id` that had appeared in the invocation's own input, or carried a `candidate_group_ref` — an ephemeral, model-local label. Deterministic validation then required only that every link sharing a label agree on `issue_type` and on `proposed_case_title`, and that a link naming an existing case agree with that case's `issue_type`.

An interim amendment added one more rule for `issue_type = OTHER`: members of an `OTHER` group had to carry the same non-missing `location_area`.

### The discovered failure

Three defects, one cause.

**H-3 — the location rule does not discriminate.** `LocationAreaCode` has exactly four members: `LOBBY`, `ELEVATOR_CAB`, `COMMON_AREA`, `BUILDING`. It is an *area kind*, not a place identity — two different buildings in one community share `BUILDING`. An elevator vibration and a water-pressure complaint, both `OTHER`, both `BUILDING`, under one vague title, still merged into one case. The failure reproduced for all four enum values and on both persistence drivers.

**NEW-1 — the rule was a hidden requirement.** `location_area` is optional in `ProposedReport` and the reviewed `monitor/v1` prompt never mentioned it. An answer that followed the prompt exactly, grouped two genuinely related `OTHER` reports, and omitted an optional field was rejected — and because validation is whole-output, the operation settled `FAILED` with **zero durable state for the entire batch**, including reports and classifications that had nothing to do with the refused group.

**NEW-2 — the rule reached only one path.** The check ran when a fresh group gained a second member. It did not run on the existing-case path at all, so a later invocation could append an unrelated `OTHER` report — carrying no `location_area` whatsoever — onto an `OTHER` case that an earlier run had created. Creation was guarded; extension, which is the same merge one batch later, was not. The `MonitorCandidateSummary.location_area` field the model would have needed to compare against is, in any case, always `None`: the application never fills it.

### The cause under all three

The interim rule asked a coarse enum to prove a semantic claim, and then enforced that claim in one of the two places it can be made. Examining the domain model and the Monitor contract for a signal that *could* carry the proof:

| Candidate signal | Why it cannot prove relatedness |
| --- | --- |
| `location_area` | Four-member area *kind*, not a place identity. Not on candidate summaries. Optional on reports. |
| `proposed_case_title` | Free text the model wrote. Agreement proves the model was consistent, not that the incidents are one. |
| `similarity_reasons`, `confidence` | Same — the model's own prose about its own judgement. |
| `candidate_group_ref` | A model-local label. Sharing one *is* the assertion under review; it cannot also be its evidence. |
| Time proximity | Two unrelated complaints minutes apart are ordinary in a live channel. |
| Proposed facts | Anchored to their own report's own messages. Two reports never share a source span, so no fact is common evidence. |
| `issue_type` | The only closed, structured signal in the contract that says what the problem *is*. |

`issue_type` is the only one left, and `OTHER` is defined as its absence: it records that the vocabulary had no word, which is a statement about the vocabulary rather than about the incident.

One further fact makes the consequence sharp. `MIN_REPORTS_FOR_NEW_CANDIDATE` is 2 — **a candidate case is by construction a merge.** There is no such thing as a single-report case from intake, so "an unprovable merge" and "an `OTHER` case exists" are the same event.

## Decision

**Intake groups reports only under an issue type that names a subject.**

`issue_type_names_a_subject(issue_type)` in `chorus.domain.entities` is the whole discriminator: true for every member of the issue vocabulary except `OTHER`. It lives in the domain and takes the durable string rather than the contract enum, so an answer and a case row written months earlier are read by the same rule — and so the apply planner, which may not import the raw agent contract, can enforce it.

### CREATE semantics

A `candidate_group_ref` may name a group with one member under any issue type — a group of one is not a merge. The moment a second link would join a group whose issue type names no subject, the **whole output** is refused with `CANDIDATE_GROUP_UNPROVABLE`.

A one-member `OTHER` group therefore validates and then produces nothing: it does not meet `MIN_REPORTS_FOR_NEW_CANDIDATE`, so no case identifier is derived, nothing durable is written, and its messages stay ordinary community messages that a later run's context window shows the Monitor again. **Intake creates no `OTHER` case.**

### EXTEND semantics

A `candidate_link` naming an existing case whose `issue_type` names no subject is refused with the same `CANDIDATE_GROUP_UNPROVABLE`, in the same pass, for the same reason: the case already holds a report, so appending one is a merge. There is no weaker rule for extension.

Since no `OTHER` case can be created, this is normally unreachable — which is exactly why it is also enforced a second time, against stored state.

### Failure / fail-closed behaviour

* **Validation** refuses the whole answer, never the offending half. A model that proposed one unprovable merge has demonstrated its grouping is unverified throughout.
* **Identity derivation** refuses to derive a `case_id` for a multi-member group whose issue type names no subject. If validation and derivation ever disagreed, the fail-closed half wins.
* **The apply gate** denies `CASE_SUBJECT_UNNAMED` when the *stored* case being extended has an issue type that names no subject. This reads the case row, not the model's summary, so a case that came to exist by any route — an earlier release, a seed, a fixture, a summary that no longer matches the row — still cannot take a further report.
* **Replay** re-runs full validation against the frozen input before finishing a partially applied plan, so a resumed operation is held to the same invariant as a fresh one.

### Alternatives rejected

**Keep `location_area`, and add a place identity to the vocabulary.** A real place identity (building, stack, riser) is not in the domain model, is not in the corpus, and would be model-supplied free text at intake. It relocates the same unverifiable claim into a new field.

**Require an anchored "subject" quotation shared by both members.** Superficially attractive — it reuses the contract's anchored-quotation principle. But the check reduces to string equality between two messages, and unrelated complaints share function words in abundance. Defending it needs a minimum length and a stop-word list, i.e. exactly the arbitrary lexical heuristic this ADR exists to avoid.

**Silently split an unprovable group into singletons instead of refusing.** This repairs a malformed answer, which the agent port forbids by design. It would also hide from operators that the model is proposing merges it cannot justify.

**Trust `candidate_group_ref` and let the Investigator un-merge later.** Nothing downstream un-merges. Reports are contributor-owned and disclosure decisions are taken per case, so a wrong merge files a resident's report under a case that does not describe it, and inflates the apparent pattern that decides whether the case advances.

**Ban grouping for every issue type, not just `OTHER`.** Two `ELEVATOR_FAILURE` reports in one community are about the same named subject, which is what a vocabulary word *is*. Refusing that refuses the product's entire purpose without closing any gap the named type leaves open.

## Consequences

### Externally visible contract changes

* **New rejection code** `CANDIDATE_GROUP_UNPROVABLE` (`AgentRejection`). Surfaces in `reason_codes` on `AGENT_CONTRACT_VIOLATION`.
* **Removed rejection code** `CANDIDATE_GROUP_LOCATION_REQUIRED`, introduced by the interim amendment and never released.
* **New apply denial** `CASE_SUBJECT_UNNAMED` (`MonitorApplyDenial`). Maps to `STATE_TRANSITION_ERROR`, so the frozen error taxonomy is unchanged and the API still answers `409`.
* **Prompt version `monitor/v1` → `monitor/v2`.** The prompt now states the rule the validator enforces. This is the direct remedy for NEW-1: no validator requirement may exist that the prompt does not ask the model to satisfy. The pinned version moves so the change is reviewed rather than absorbed silently, and so a runtime still serving the old text is refused rather than failing every batch it answers.

### Not changed

* `MonitorInput` and `MonitorOutput` keep `monitor-input/v1` and `monitor-output/v1`: no field is added, removed, or retyped. `location_area` stays exactly as it was — optional on `ProposedReport`, present on `MonitorCandidateSummary` — it is simply no longer load-bearing for grouping.
* `IssueType` is unchanged. `MIN_REPORTS_FOR_NEW_CANDIDATE` is unchanged.
* No new field is required of the model.

### Compatibility

* An in-flight operation whose plan snapshot was taken under `monitor/v1` fails re-validation on `PROMPT_VERSION_MISMATCH` at resume. This is intended: the snapshot's answer was produced against instructions this application no longer accepts.
* No stored `OTHER` case exists to migrate — none could have been created outside the defect this ADR closes — but if one is found, the apply gate refuses to grow it rather than silently accepting it.
* Answers that grouped `OTHER` reports and used to be accepted are now refused in full. That is the fix, not a regression.

### Functional cost, and the way forward

Under the V1 vocabulary only `ELEVATOR_FAILURE` names a subject, so intake can currently form cases for elevator problems and nothing else. Genuinely related non-elevator reports stay provisional rather than merging unsafely — false separation, deliberately preferred to false merging.

**The remedy is the vocabulary, not the validator.** To make a class of problem groupable, add a named member to `IssueType` (`WATER_LEAK`, `ACCESS_CONTROL_FAILURE`, …). That grants grouping automatically through `issue_type_names_a_subject`, and it is a change reviewed once, in the open, rather than a judgement re-made per answer from prose. That widening is deliberately **not** part of this repair: it is a product-vocabulary decision with its own corpus and evaluation implications, and bundling it here would have hidden a taxonomy change inside a safety fix.

### Residual risk

A model that mislabels an unrelated report with a *named* issue type in order to merge it defeats this rule, and deterministic code cannot detect the lie — nothing in the input contradicts a plausible typed claim. The rule does not eliminate that risk; it removes the path where merging was the lazy default rather than a specific false assertion, and it makes the false assertion visible in the stored `issue_type`. See [10-security-threat-model.md](../architecture/10-security-threat-model.md).
