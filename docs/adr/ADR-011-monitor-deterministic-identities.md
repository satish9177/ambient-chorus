# ADR-011: Replay-safe deterministic identities for Monitor-derived entities

**Status:** Accepted
**Date:** 2026-09-02
**Deciders:** Ambient CHORUS maintainers and product owner

## Context

[01-principles-and-invariants.md](../architecture/01-principles-and-invariants.md) says entity IDs are UUIDv4 in normal operation and namespace-scoped UUIDv5 only in fixed demo fixtures. [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) says the Monitor apply step uses *deterministic IDs* so that a redelivered invocation completes missing groups without duplicating committed ones.

Both statements cannot hold at once. A Monitor invocation is delivered at least once: an asynchronous dispatcher may redeliver it, a worker may crash between two bounded transactions of one apply, and the one licensed agent retry reuses the same `invocation_id`. If the report, fact, and candidate-case identifiers were minted randomly on each attempt, the second attempt would address different items, every create-only write would succeed, and one incident would become two reports, two facts, and two candidate cases. Detecting that afterwards would require content-matching queries the frozen access patterns do not permit.

The conflict is therefore not stylistic. Random identity for Monitor-derived entities makes the frozen "retry completes missing work without duplicates" property unimplementable within the approved access patterns.

## Decision

Normal application and domain entity identifiers remain UUIDv4, minted through the injected `IdGenerator`.

One explicit exception is accepted. **Monitor-derived replay identities** may be namespace- and community-scoped UUIDv5 derived from canonical authoritative input. The exception covers exactly:

- a Monitor-derived `Report`;
- a candidate `CommunityCase` created by Monitor apply;
- an `EvidenceRoot` (already content-addressed by its own storage key);
- a **Monitor fact slot** — the identity of a `Fact` created by Monitor apply;
- the deterministic Monitor apply progress record and the audit event a Monitor apply writes, where replay requires a stable address.

The exception does **not** extend to `ActionProposal`, `Approval`, `ActionExecution`, `Commitment`, `InvestigationAssessment`, `ApplicationOperation`, `DisclosureMandate`, `Community`, `Contributor`, `CommunityMessage`, `EvidenceItem`, or any ordinary `AuditEvent`. Those keep UUIDv4.

### Derivation rules

1. Every derivation uses a fixed per-family root UUID, so two families built from the same tuple can never collide onto one identifier.
2. The derived name is RFC 8785 canonical JSON, so key order, tuple spelling, and datetime representation cannot change the result.
3. The payload always begins with `namespace` and `community_id`. Two communities that observe byte-identical text derive different identifiers, so a derived identifier is never a cross-tenant address.
4. Sets of identifiers in a derivation payload are sorted, so proposal order cannot change identity.
5. **No LLM wording ever enters a replay identity.** A summary, a title, a confidence string, a reason, a client reference, or a model-chosen typed value must not appear in a derivation payload. Identity comes from authoritative lineage the application itself validated: which contributor, which issue type, which messages, which evidence, which fact type.

### Fact slot identity

A Monitor `Fact` is addressed by a **fact slot**: `(report_id, fact_type, sorted source_message_ids, sorted evidence_ids)`. The typed value is deliberately excluded. Including it made a legitimate re-answer — the same messages read the same way, worded or valued slightly differently — resolve to a second address, so a replay silently doubled the fact count.

Excluding the value means one slot can be re-proposed with different content. That is a conflict, not a merge:

- same slot, byte-identical immutable fact content → replay, no write;
- same slot, materially different content → deterministic `AGENT_OUTPUT_DRIFT` failure. No second fact is created and the first is never overwritten.

Two proposed facts in one output that resolve to the same slot make the output ambiguous and the whole output is refused.

Correction and supersession remain an explicit later deterministic path (`Fact.supersedes_fact_id`), driven by a human or by the Investigator's validated assessment — never by Monitor nondeterminism.

## Alternatives considered

- **Keep UUIDv4 and detect duplicates by content query.** Requires a content index the approved access patterns forbid, and would be an eventually consistent read informing a state-changing decision.
- **Keep UUIDv4 and make the whole apply one transaction.** Cannot hold at the frozen Monitor maxima: 25 reports and 100 facts plus cases, signals, invocation, and audit exceed DynamoDB's 100-operation transaction limit.
- **Derive identity including the model's typed value.** The status quo before this ADR; produces duplicate facts on a valid re-answer (finding H4).
- **Derive identity from `invocation_id`.** Stable within one invocation but not across the licensed retry boundary, and a genuinely new invocation over the same messages would re-create the same reports under new identifiers.
- **Let the model choose identifiers.** Rejected outright: the authority hierarchy places a validated agent proposal below deterministic policy, so a model-chosen durable address would invert it.

## Why chosen

The exception is narrow, named, and testable. It applies only where replay safety is a stated requirement of the frozen persistence design, it is bounded to five entity families, and it derives exclusively from values deterministic code already validated. Everything the model wrote stays out of the address.

## Consequences

- `chorus.application.services.identity` is the single derivation module; a test pins each family's root UUID and each family's exact payload shape.
- A test asserts that no derivation payload contains a summary, title, confidence, or typed value.
- A proposal that changes only its wording writes nothing on replay; a proposal that changes a typed value at a settled slot fails closed rather than duplicating.
- Derived identifiers are not secrets and are not an authorization boundary. Scope validation on every load remains the boundary.

## Revisit condition

Revisit if a later phase needs a Monitor-derived entity whose authoritative lineage is not sufficient to distinguish two legitimate simultaneous instances. The fix is to add an authoritative lineage component to the derivation, recorded in a superseding ADR — never to reintroduce model wording into identity.
