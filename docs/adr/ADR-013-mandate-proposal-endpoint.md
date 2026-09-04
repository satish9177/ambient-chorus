# ADR-013: Candidate acceptance is the command that creates mandate proposals

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [08-api-design.md](../architecture/08-api-design.md) § Endpoint summary, § Mandate thread and decision; [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § Transition contract, § DisclosureMandate
**Amended by:** [ADR-014](ADR-014-monitor-proposes-no-disclosure-terms.md), which removes `MandateSuggestion` from the Monitor contract and corrects the ceiling/default wording below

## Context

### The gap

Three frozen statements could not all be true at once.

1. [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) guards `CANDIDATE→AWAITING_MANDATES` with "human/demo accepts candidate; **proposals exist for every participating owner**", and lists `MandateRequested` as an event produced by a "mandate service".
2. [08-api-design.md](../architecture/08-api-design.md)'s endpoint summary — presented as the complete V1 API — contains exactly two mandate routes: `GET /v1/contributors/{contributor_id}/mandates/current` and `POST /v1/cases/{case_id}/mandates/{mandate_id}/decisions`. Neither creates a proposal, and no route accepts a candidate.
3. Phase-3 intake leaves a discovered case in `CANDIDATE` and writes no mandate. `ValidatedMonitorOutput` deliberately discards `MandateSuggestion` after reference validation, so nothing downstream of the Monitor carries it.

So the `PROPOSED` version that `POST .../decisions` requires had no producer, and the case could never leave `CANDIDATE`. The mandate workflow was unreachable through the API.

### Why the suggestion cannot fill it

`MandateSuggestion` is explicitly "never an approval, never widens a policy maximum, and is not authorization of any kind". (This section argued the point but amended no document, which is the gap [ADR-014](ADR-014-monitor-proposes-no-disclosure-terms.md) closes by removing the field outright.) Even if it were carried forward, a proposal built from it would still have to be re-derived and capped by deterministic policy/v1 maximums before persistence — at which point the model's contribution is discarded. A proposal that a model influenced is a proposal a model can shape; the frozen principle is that the LLM proposes and deterministic code decides, and disclosure authority is squarely on the deciding side.

## Decision

**Add one route: `POST /v1/cases/{case_id}/mandates`, presenter-only, and make it the human/demo candidate acceptance.**

Proposal creation and candidate acceptance are the same command, committed in one transaction. That is not a convenience: the frozen guard requires proposals to exist *at the moment* the state changes, and two commands would leave a window in which the case is `CANDIDATE` with live proposals — a state the machine does not describe and no guard covers.

The command:

* strongly loads the case and every active fact it names;
* groups facts by owning contributor and derives, per contributor, a `PROPOSED` mandate version 1 whose grants are the **deterministic least-permissive defaults** for each fact's type and sensitivity, capped by — and normally well below — the policy/v1 maximums for the same fact. The cap and the offer are two different values; [ADR-014](ADR-014-monitor-proposes-no-disclosure-terms.md) records the distinction, which this clause originally stated in a way that read as equality;
* refuses to run unless the case is `CANDIDATE` and matches `expected_case_version`;
* commits, atomically: the immutable version-1 rows, their current pointers, the `CANDIDATE→AWAITING_MANDATES` transition guarded on the case version, a condition-check that no unexpired `SEND_FENCE` exists, one append-only `mandate.requested` audit event, and the command idempotency record that is the transaction's own commit proof.

No fact value, no private summary, and no model text enters the request or the response. The body is `{"expected_case_version": <int>}` and nothing else.

### Contributor participation

A contributor participates when they own at least one `ACTIVE` fact in the case. A case with no participating contributor is refused rather than transitioned: an `AWAITING_MANDATES` case nobody can decide is a dead end, and the guard's "every participating owner" is vacuously satisfied in a way that hides the problem.

### Alternatives rejected

**Fold proposal creation into the Phase-3 Monitor apply plan.** It needs no new route, and it was the closest fit for "application" in the transition table's *Caused by* column. It was rejected because it changes the frozen Monitor apply contract: the ordered step descriptors, `total_steps`, and therefore `plan_hash`, all move, and [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md)'s "Monitor apply" transaction boundary would have to be restated. It also removes the human acceptance the state machine requires — a candidate would arrive already accepted, decided by a model's linkage judgement.

**Materialise the proposal lazily inside the decision transaction.** The read endpoint would compute version 1 deterministically without persisting it, and the decision would write both version 1 and version 2 together. It needs no doc change at all, which is its whole appeal. Rejected because it makes "proposals exist for every participating owner" a statement about a computation rather than about durable state, gives `proposed_at` no meaning independent of the decision that consumed it, and requires `mandate_id` to be derivable by a client before any row exists — a derived identity that [ADR-011](ADR-011-monitor-deterministic-identities.md) grants to Monitor-derived entities and to nothing else.

**Reuse `POST /v1/cases/{case_id}/investigations` with `reason: INITIAL`.** Its guard already admits a candidate case. Rejected because it is the Phase-5 Investigator operation: it returns `202` and an operation to poll, and overloading it would make one route both an asynchronous agent invocation and a synchronous authorization mutation.

## Consequences

### Externally visible contract changes

* **New route** `POST /v1/cases/{case_id}/mandates`. Presenter-only, `Idempotency-Key` required, `200` with the created proposal summaries and the new case version.
* **New audit event type** `mandate.requested`, carrying case, mandate, and contributor identifiers only.
* **No new UI surface.** The Private Mandate Thread already exists as one of the three frozen surfaces; this route is what populates it.

### Not changed

* `DisclosureMandate` gains no field. `MandateStatus` gains no member. The `terms_hash` payload in `chorus.privacy.canonical.mandate_terms_payload` is unchanged, so no previously computed hash moves.
* The two existing mandate routes keep their exact frozen request and response shapes.
* The case state machine gains no edge. `CANDIDATE→AWAITING_MANDATES` already existed; this ADR names the command that causes it.
* `IssueType` is unchanged, and so is the ADR-012 grouping invariant.

### Recorded clarifications

Two frozen sentences are ambiguous in a way that this phase had to resolve, and the resolution is recorded here rather than decided in code.

**"Adjust creates version N+1 with new terms and marks N `SUPERSEDED`"** cannot mean a write to version N, because the same paragraph says "Historical versions never mutate" and the persistence mapping stores a mandate version create-only. `SUPERSEDED` is therefore a **derived** status: a stored version whose status is `PROPOSED` or `APPROVED` and which the current pointer no longer names reads as `SUPERSEDED`. The successor records the relationship durably in `supersedes_version`, and the pointer move is what makes it true. No stored row is ever rewritten.

**The decision edge table** is stated in prose across [04](../architecture/04-domain-state-and-events.md) and [05-privacy-compiler-and-shareable-view.md](../architecture/05-privacy-compiler-and-shareable-view.md) but never tabulated. It is now explicit in `chorus.domain.mandates.MANDATE_DECISION_EDGES`, and every edge the documents do not state is closed rather than permitted — `REFUSED` and `REVOKED` are terminal, `REVOKE` of a never-approved proposal is refused, and re-approving an `APPROVED` mandate is refused rather than minting a version that changes nothing.

### Residual risk

The presenter persona triggers acceptance for the whole case, so one demo actor decides that four residents are asked for mandates. That is the frozen demo access model, not a production authorization design; it is already recorded as residual risk T26 in [10-security-threat-model.md](../architecture/10-security-threat-model.md), and this route does not widen it — the presenter can request a decision and can never make one.
