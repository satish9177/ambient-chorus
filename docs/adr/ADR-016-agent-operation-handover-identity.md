# ADR-016: Kind-agnostic agent operation handover identity

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § The agent handover identity, § ApplicationOperation; [08-api-design.md](../architecture/08-api-design.md) § Asynchronous operation pattern; [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) § Complete failure matrix
**Extends:** the Monitor handover design introduced with Phase 3

## Context

### What the handover was built to stop

[04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) records why `monitor_invocation_id` and `monitor_locator_hash` exist:

> a worker delivery is data on a queue and a queue can be wrong, while the operation row cannot be: without them, the *first* delivery for an operation had no durable record to disagree with, so any invocation identity and any subset of the delivered locators were accepted on trust.

`MonitorOperationWorker` enforces seven facts before claiming anything: operation ID, namespace, kind, actor hash, request hash, the operation's own recorded invocation identity, and the digest of the delivered locators. A mismatch claims nothing, invokes nothing, and mutates nothing.

### Why the Investigator cannot use it

The field pair is `MONITOR`-only by frozen invariant. The presence rule reads "required for `kind = MONITOR` before dispatch; `null` for every other kind", and `chorus.domain.entities` enforces it:

```python
if self.kind is not ApplicationOperationKind.MONITOR and self.monitor_invocation_id:
    raise ValueError("only a MONITOR operation carries a Monitor handover identity")
```

So an `INVESTIGATE` operation dispatches a job carrying an `invocation_id` that no durable record authorizes.

### The exposure, stated precisely

It is narrower than the Monitor's was, and it is real. The investigation request hash covers `{case_id, expected_case_version, reason}`, so a job cannot steer *what* the Investigator is shown — unlike the Monitor, whose message locators sat outside its request hash. The conditional `PENDING → RUNNING` claim still bounds duplicate execution for a single operation.

What is not covered is the invocation identity itself. The pre-invocation replay check reads the durable invocation record **by `invocation_id`**. A redelivery presenting a fresh `invocation_id` finds no record, concludes the run has not happened, and calls the model a second time over the same private case. That is a duplicate pass over private text caused by data on a queue — the exact harm the Monitor handover was built to eliminate, arriving through the one command family left without it.

The Action proposal command will have the same gap for `PROPOSE_ACTION`, so fixing it once is cheaper and safer than fixing it twice.

## Decision

**Generalize the handover to every agent-invoking operation kind.**

```python
agent_invocation_id: UUID | None = None
agent_binding_hash: Sha256Digest | None = None
```

| Kind | Handover | Binding hash content |
|---|---|---|
| `MONITOR` | **required** | `monitor_locator_hash(locators)` — the sorted digest of `{message_id, sent_at}`, byte-identical to today |
| `INVESTIGATE` | **required** | canonical digest of `{case_id, expected_case_version, reason}` |
| `PROPOSE_ACTION` | **required** | canonical digest of `{case_id, view_id, view_hash}` |
| `SEND_ACTION` | **null** | invokes no agent |
| `DEMO_DUE` | **null** | invokes no agent |

The invariants are carried over unchanged and generalized only in scope:

| Rule | Statement |
|---|---|
| Presence | required for every agent-invoking kind before dispatch; `null` for every other kind |
| Pairing | both are set or neither is; an operation is never half-bound |
| Content | identifiers and digests only — never a locator list, never message text, never a view body |
| Mutability | immutable for the operation's lifetime; every status transition copies both forward |
| Creation | written by the same transaction that creates the operation and completes its command-idempotency record |
| API exposure | not part of the public operation status response; a poller learns status, not handover identity |

**Every existing Monitor binding check is preserved.** The seven facts a Monitor worker proves before claiming stay seven facts; only two field names change. `monitor_locator_hash()` keeps its name, its docstring, its sorting, and its bytes — it becomes *the* `MONITOR` binding-hash function rather than a differently-named field's contents.

### Storage

`application-operation/v1` becomes `application-operation/v2`, read-old-write-new. A v1 item's `monitor_invocation_id` and `monitor_locator_hash` attributes decode into `agent_invocation_id` and `agent_binding_hash`; v2 items are written under the new attribute names. No stored value changes meaning and no digest is recomputed.

## Alternatives considered

- **Leave `INVESTIGATE` unbound and rely on the operation claim.** Rejected: the claim bounds *execution*, not *invocation identity*, and the replay check is keyed on the identity. It also leaves the same hole waiting for the Action phase.
- **Derive the investigate `invocation_id` as UUIDv5 from `operation_id`.** Rejected: [01-principles-and-invariants.md](../architecture/01-principles-and-invariants.md) confines the UUIDv5 exception to Monitor-derived replay identities and names `ApplicationOperation` among the entities that stay UUIDv4. Widening it for convenience would erode a boundary [ADR-011](ADR-011-monitor-deterministic-identities.md) drew narrowly on purpose.
- **A second `investigate_invocation_id` field beside the Monitor pair.** Rejected: three agent kinds would eventually mean three field pairs and three near-identical binding checks, which is how one of them ends up subtly different from the others.
- **Bind through the request hash alone.** Rejected: the request hash names the *request*, not the invocation. Two legitimate invocations of the same request — an initial run and a later `REOPEN` — share a request shape and must not share an identity.
- **Put the binding in the job and check it against nothing.** That is the status quo the Monitor handover was written to end.

## Why chosen

It closes an identified duplicate-invocation path, closes the Action phase's before it opens, preserves every Monitor security check verbatim, and adds no new concept — it removes an unnecessary `MONITOR` qualifier from a mechanism that was already correct.

## Consequences

- `ApplicationOperation` fields are renamed and the kind invariant is generalized to the agent-invoking set.
- `chorus.application.operations` gains an `agent_binding_hash(kind, ...)` dispatcher; `monitor_locator_hash` is unchanged and becomes the `MONITOR` arm.
- The Monitor worker's binding check renames two references. A test asserting that all seven checks still fire on each of the seven mismatches is part of this change.
- The Core codec reads both attribute spellings and writes the new one.
- A test asserts that a `SEND_ACTION` or `DEMO_DUE` operation carrying a handover is refused at construction, so the null-together rule cannot decay.

## Revisit condition

Revisit if an operation kind ever needs to authorize **more than one** agent invocation — a legitimate second attempt under a new identity, say. The fix is an explicit invocation sequence on the operation, recorded in a superseding ADR, never a nullable binding that a worker may skip.
