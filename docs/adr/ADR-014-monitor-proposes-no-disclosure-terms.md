# ADR-014: The Monitor proposes no disclosure terms

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [03-agent-architecture.md](../architecture/03-agent-architecture.md) § Monitor / Intake Agent → Output; [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § DisclosureMandate; [ADR-013](ADR-013-mandate-proposal-endpoint.md) § Decision, § Why the suggestion cannot fill it
**Extends:** [ADR-013](ADR-013-mandate-proposal-endpoint.md)

## Context

### What the Phase-4 freeze gate found

Three authoritative statements about `MandateSuggestion` could not all be true at once, and the disagreement survived Phase 4 because no document was made to answer for it.

1. **[03-agent-architecture.md](../architecture/03-agent-architecture.md) still asks for it.** `MonitorOutput` is documented as containing `mandate_suggestions[]: {report_client_ref, fact_client_refs[], suggested_max_scope, suggested_purpose}`, and the paragraph beneath it says "A mandate suggestion creates only a `PROPOSED` mandate; it can never produce approval." That sentence is a *ceiling on an effect*, and it presumes the effect: it says a suggestion produces a proposal and stops there.

2. **[ADR-013](ADR-013-mandate-proposal-endpoint.md) decided a different producer.** Version 1 of every mandate is derived by the candidate-acceptance command from the case's own `ACTIVE` facts and the deterministic policy/v1 tables. ADR-013 argued in its Context that the suggestion could not fill the gap — but that argument sat in a Context section, its `Amends` list did not name 03-agent-architecture.md, and the contract field was left standing.

3. **The runtime never asks for it.** The pinned `monitor/v2` prompt in `runtimes/monitor/prompt.py` enumerates what the model must return and never mentions a mandate suggestion. `chorus.contracts.monitor.MonitorOutput` nevertheless declares the field, `_validate_auxiliary_references` reference-checks it, and `ValidatedMonitorOutput` then drops it on the floor.

So the field is asked for by nothing, produced by nothing the deployment runs, and consumed by nothing. It exists only as a hole in a schema that a model is handed.

### Why "harmless and unused" is the wrong reading

A field in a structured-output schema is not documentation; it is part of the instruction. Strands hands `MonitorOutput`'s JSON schema to the model, so `suggested_max_scope: DisclosureScope` tells the model, in the only language the schema speaks, that choosing a disclosure scope is among the things it is being asked to do. The prompt's "you are proposing, and something else decides" and the schema's "here is where you write down how far this may travel" are two instructions pointing in different directions, and the one nobody reviewed is the one written in the schema.

That is the same defect ADR-012 named as NEW-1 in the opposite direction: a validator requirement the prompt never stated. Here it is a prompt-adjacent requirement the reviewed text never stated, and it is worse in one respect — the intended answer to it is *nothing at all*, which no part of the artifact says.

### The question that had to be answered explicitly

Does the frozen architecture *require* Phase 4 to consume a validated `MandateSuggestion`? Read literally, 03-agent-architecture.md says a suggestion creates a `PROPOSED` mandate, so a literal reading says yes, and Phase 4 would be in violation. ADR-013 says no, but amended the wrong documents to make that stick. The disagreement is real, it is not a Phase-4 implementation defect, and it is resolved here rather than by picking whichever reading the code already matched.

## Decision

**The Monitor proposes no disclosure terms. `MandateSuggestion` and `MonitorOutput.mandate_suggestions` are removed from the contract, and the pinned prompt version moves `monitor/v2` → `monitor/v3`.**

Nothing replaces the field. Proposed mandate terms have exactly one producer — the candidate-acceptance command of ADR-013 — and exactly one source of scope: the deterministic policy/v1 tables in `chorus.privacy.policy`, applied to the fact's own type and sensitivity.

### Why removal rather than redefinition

A suggestion that reached the proposal builder would have to be one of three things, and all three are worse than nothing.

**A widening input** is refused outright: it is disclosure authority handed to a model, and it is the thing the whole privacy design exists to prevent.

**A narrowing-only input** — proposed scope becomes the lesser of the deterministic default and the model's suggestion — is safe in the disclosure direction and still wrong. It makes what a contributor is shown depend on a model's reading of their neighbours' messages, so two residents with identical facts can be asked different questions for reasons nobody can reconstruct; and a message crafted to push every suggestion to `INTERNAL_ONLY` becomes a cheap denial of service on the mandate thread, which is the same asymmetry the prompt's own fence design already refused to accept.

**An advisory field carried but never acted on** is what exists today, and it is the state this ADR was written to end: it keeps the misleading instruction in the schema and buys nothing.

### Why the prompt version moves

The reviewed artifact a runtime serves is its prompt text *and* the structured-output model it asks for; `runtimes/monitor/agent.py` passes `MonitorOutput` to `structured_output` in the same call that renders the prompt. Changing the schema changes what the runtime asks the model to produce, so it changes the artifact's identity.

This is the precedent ADR-012 set and the reason it gave: "the pinned version moves so the change is reviewed rather than absorbed silently, and so a runtime still serving the old text is refused rather than failing every batch it answers." That reason applies exactly. A deployed `monitor/v2` runtime whose pinned `MonitorOutput` still declares the field can return it, and `StrictModel`'s `extra='forbid'` would reject the parse with a schema error on every batch. Under `monitor/v3` the application refuses that runtime once, by version, with `PROMPT_VERSION_MISMATCH`.

The prompt *text* is unchanged. It never asked for a suggestion, which is precisely what made the field indefensible.

## Consequences

### Contract and runtime changes

* `chorus.contracts.monitor.MandateSuggestion` is deleted. `MonitorOutput.mandate_suggestions` is deleted, along with the output-level uniqueness rule that a report carries at most one suggestion.
* `MONITOR_PROMPT_VERSION` moves to `monitor/v3` in `chorus.contracts.common`, `runtimes/monitor/prompt.py`, and `runtimes/monitor/runtime.toml`. All three are asserted equal by the existing tests.
* `MONITOR_OUTPUT_SCHEMA_VERSION` stays `monitor-output/v1`. The schema literal names the *envelope shape* the application parses, and every consumer of it moves in lockstep with the prompt version that is already checked one line later; a second version axis for the same change would have to be kept in step by hand for no additional guarantee.
* `_validate_auxiliary_references` in `chorus.application.services.monitor_validation` no longer reference-checks suggestions. An answer that supplies the field is now refused earlier and harder — at parse, by `extra='forbid'` — rather than at reference validation.
* The local `build_lexical_output` stand-in no longer emits suggestions.

### Phase-3 impact and revalidation

* **Monitor schema: changed.** **Monitor prompt text: unchanged.** **Monitor validation: changed** (one reference check removed). **Apply plan: unchanged** — `ValidatedMonitorOutput` never carried the field, so no step descriptor, no `total_steps`, and no `plan_hash` moves, and the frozen "Monitor apply" transaction boundary in [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) is untouched.
* **Phase 3 requires re-validation of its agent-contract surface, not of its behaviour.** Nothing Phase 3 durably writes is derived from a suggestion, so no stored report, fact, case, feed signal, or audit row changes shape or value. What must be re-run is the Monitor contract and intake suite, plus the live synthetic eval gate against a redeployed `monitor/v3` runtime.
* **Deployment is not backward compatible, deliberately.** A `monitor/v2` runtime must be redeployed. Until it is, every invocation is refused by the prompt-version check rather than half-accepted.
* **Monitor plan snapshots written under `monitor/v2` are no longer replayable.** `verify` already fails closed on `MONITOR_PLAN:prompt_version`, which is the explicit failure this bump buys instead of an opaque parse error inside a stored document. Snapshots are per-invocation working state in S3, not durable domain state; an unreplayable one means the invocation is re-run, not that anything is lost.

### Recorded clarification: a policy ceiling is not a proposed grant

The freeze gate surfaced a second, related confusion in the same area, and it is settled here because the sentences that state it are sentences this ADR is already amending.

**A version-1 `PROPOSED` grant is never the policy maximum.** Two independent values exist per fact and they are not the same value:

* `policy_maximum_scope(fact_type, sensitivity)` — the widest scope policy/v1 will ever let a contributor *grant* for that fact. It is a cap on decisions, checked at every decision and re-derived by the compiler at gates 14, 15, 18 and 19.
* `proposed_scope(fact_type, sensitivity)` — what version 1 actually *offers*, which is the least-permissive useful default beneath that cap. Only `INCIDENT_OCCURRENCE`, `SERVICE_IMPACT`, `LOCATION_AREA` and `CONTRADICTION` are offered above `INTERNAL_ONLY`, and they are offered at `ANONYMOUS_CASE`. `EVIDENCE_DESCRIPTION` has an `EXTERNAL_ACTION` ceiling and is still offered `INTERNAL_ONLY`, because exporting a photograph is a choice to be made rather than arrived at by accepting a default.

So a general `INCIDENT_OCCURRENCE` fact, whose ceiling is `EXTERNAL_ACTION`, is proposed at `ANONYMOUS_CASE`. Approving that proposal authorizes `ANONYMOUS_CASE` and nothing wider. `APPROVE` means "yes, exactly this" and is refused when the submitted terms differ from the stored version 1 in any grant, flag, or expiry; reaching the ceiling is an `ADJUST`, which is a separate decision word, a separate audit row, and a deliberate act by the fact's owner.

Two frozen sentences said otherwise and are corrected:

* [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § DisclosureMandate said "Its grants are the deterministic policy/v1 maximums for each fact's type and sensitivity; a contributor may only narrow them." Both halves were wrong, and the second contradicted the same document's own decision section, which describes raising a photo description from `INTERNAL_ONLY` to `EXTERNAL_ACTION` as an ordinary adjustment.
* [ADR-013](ADR-013-mandate-proposal-endpoint.md) § Decision said "grants are the **deterministic policy/v1 maximums** … with least-permissive proposed defaults", which is self-contradictory in one clause. It meant *capped by* the maximums and *set to* the defaults.

No code changes for this clarification. `chorus.privacy.policy.proposed_scope` and `chorus.privacy.mandates.build_proposed_grants` already implemented exactly what is written above; the documents were the defect.

### Not changed

* `DisclosureMandate`, `MandateStatus`, `FactGrant`, `IdentityGrant`, `MANDATE_DECISION_EDGES`, and the `terms_hash` payload in `chorus.privacy.canonical.mandate_terms_payload` are all untouched. No previously computed mandate hash moves.
* The three mandate routes keep their exact frozen request and response shapes.
* The case state machine gains and loses no edge, and `IssueType` and the ADR-012 grouping invariant are unaffected.
* The Monitor's other outputs — `sensitive_signals[]` in particular — are unchanged. Flagging that a message contains a health detail is an observation about text the model was given; choosing how far that detail may travel is not, and the distinction is the line this ADR draws.

### Residual risk

Removing the field removes the only place the Monitor could express "this fact looks like something the contributor may want to share". Nothing replaces that signal, and the mandate thread is therefore a flat list of every owned fact at its deterministic default rather than a curated starting point. That is accepted: a longer list is a worse experience and a better authorization surface, and the contributor is the only party this system lets curate it.
