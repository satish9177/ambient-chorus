# ADR-017: An immutable root-ID locator for EvidenceRoot ancestry

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Core table mapping, § Access patterns, § Upload, provenance, and duplicate handling; [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) § Complete failure matrix

## Context

### Two frozen statements that cannot both be satisfied

[06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) fixes the evidence-root address as content-addressed:

```text
NS#{namespace}#COMM#{community_id}   EVIDENCE_ROOT#{root_sha256}
```

The same document and [09-observability-errors-and-failures.md](../architecture/09-observability-errors-and-failures.md) both require forward chains to collapse to the earliest root by "deterministic root traversal". `chorus.domain.facts.collapse_evidence_root` implements exactly that: it walks `parent_root_id` to the earliest ancestor, rejecting cycles and ancestry that crosses community or namespace.

But `parent_root_id` is an `EvidenceRootId`, and there is **no address by `root_id`**. The only repository method is `load_evidence_root(scope, root_sha256)`. The same document forbids a scan and requires no GSI. So a chain the domain function is written to walk cannot be loaded in order to walk it.

For roots created by Phase-3 ingest the identifier is UUIDv5 over `{namespace, community_id, root_sha256}`, which is one-way. Knowing a `parent_root_id` does not yield the `root_sha256` needed to address it.

### Why this is not hypothetical

The frozen elevator fixture builds a real chain: a `FORWARDED` root whose `parent_root_id` names the original, and `test_forwarded_photo_counts_as_one_root` depends on it. Phase 3 happens to create only `ORIGINAL` roots today, so the gap is latent in live data and live in the fixture corpus — the one place evaluation scenario 4 is actually proved.

The Investigator's independent-source recomputation is the first consumer that must resolve the closure from persistence. The privacy compiler's gate 17 is the second.

### The rejected shortcut

An earlier proposal was to declare V1 collapse to be item-to-root only and re-cut the fixture's `FORWARDED` root into two items sharing one `ORIGINAL`. That is rejected. Item-level sharing covers the *duplicate bytes* case and not the *derived evidence* case, and `DerivationKind` distinguishes `ORIGINAL`, `FORWARDED`, and `TRANSFORMED` precisely because the design intends to tell them apart. Threat T13 and risk R10 are both about manufactured corroboration, and removing the mechanism that detects one of its two shapes to fit a key grammar is fixing the wrong thing.

## Decision

**Add an immutable root-ID locator item. Preserve `DerivationKind.FORWARDED` and `parent_root_id` semantics exactly.**

| Item | PK | SK | Value | Mutability |
|---|---|---|---|---|
| Evidence root (canonical) | `NS#n#COMM#c` | `EVIDENCE_ROOT#{root_sha256}` | the `EvidenceRoot` | immutable |
| **Evidence root ID locator** | `NS#n#COMM#c` | `EVIDENCE_ROOT_ID#{root_id}` | `{root_sha256}` | **immutable, create-only** |

The locator holds one field. It is not a second copy of the root, so the two can never disagree about anything except existence, and a missing locator fails closed rather than producing a partial answer.

### Writing

The locator is created in the **same transaction** as its canonical root, with the same create-only condition. A conditional failure means the pair already exists, and the writer proves it is the same pair by comparing the stored `root_sha256` with the one it just computed — a differing value is `INTEGRITY_ERROR`, never a second opinion. This mirrors the channel-uniqueness lock and the snapshot-manifest patterns already in use.

### Reading

A new port method loads roots by identifier:

```python
async def load_evidence_roots_by_id(
    self, scope: CommunityScope, root_ids: tuple[EvidenceRootId, ...]
) -> tuple[EvidenceRoot, ...]:
```

It is implemented as a bounded `BatchGetItem` on the exact locator keys, then a bounded `BatchGetItem` on the exact canonical keys those locators name. **Two direct-key batch gets. No scan, no GSI, no prefix walk.** Every loaded row is revalidated against the scope after deserialization, as every other repository read is.

### Traversal

The application resolves the transitive closure before counting. `collapse_evidence_root` stays a pure domain function over a supplied set and is **not modified**:

```text
seen := {}
frontier := { item.root_id for item in the case's evidence items }
loads := 0
while frontier:
    rows := load_evidence_roots_by_id(scope, frontier)
    a missing locator or a missing canonical row -> IntegrityError, quoting nothing
    seen |= rows
    frontier := { r.parent_root_id for r in rows if r.parent_root_id is not None } - seen.ids
    loads += 1
    if loads > MAX_ROOT_ANCESTRY_LOADS: raise IntegrityError
```

`MAX_ROOT_ANCESTRY_LOADS` is an operational bound of the same character as the Monitor prompt module's fence-derivation limit: the loop terminates naturally when no new parent is discovered, and cycles are already rejected downstream, so exceeding the bound is a bug rather than a scenario. It is **not** a policy threshold and it decides nothing about evidence.

## Alternatives considered

- **Remove forwarded root chains and re-cut the fixture.** Rejected — see § The rejected shortcut. It deletes a detection mechanism to fit a key.
- **A sparse GSI on `root_id`.** Rejected: [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) states no GSI is required by approved V1 access patterns and reserves any future GSI for an admin case list via its own ADR. A two-get locator costs less and keeps the access-pattern statement true.
- **Store the parent's `root_sha256` on the child instead of `parent_root_id`.** Rejected: it changes the meaning of an immutable persisted field, invalidates the domain function's signature and its tests, and makes the chain addressable only by content — so a `TRANSFORMED` root whose content legitimately differs from its parent's would still need a second lookup.
- **Denormalize the whole ancestry chain onto each root.** Rejected: a root is immutable, so a later-discovered ancestor could never be recorded, and it duplicates state that must not be able to disagree with itself.
- **Resolve ancestry by re-deriving the UUIDv5.** Impossible — the derivation is one-way, and it would only ever work for roots created by one code path.

## Why chosen

It preserves every frozen semantic — `DerivationKind`, `parent_root_id`, the domain traversal function, and `test_forwarded_photo_counts_as_one_root` — while satisfying "no scan, no GSI" with two direct-key gets. The added item is one immutable field written in an existing transaction, and it fails closed when absent.

## Consequences

- The DynamoDB key module gains `evidence_root_id_sort_key(root_id)`; a codec and key test pin the grammar.
- The core repository port gains `load_evidence_roots_by_id` and `stage_create_evidence_root_locator`.
- The ingestion command stages the locator beside every root it creates. Existing ingest replay and conflict tests must pass unchanged; one new test asserts the locator is created exactly once per root.
- A new `chorus.application.services.root_closure` module owns the bounded traversal. The compiler adapter will use the same service rather than writing a second one.
- `chorus.domain.facts.collapse_evidence_root` is **not modified**.
- The elevator fixture keeps its `FORWARDED` root and gains locator rows.
- The no-scan test continues to hold: both reads are direct-key batch gets.
- **Backfill.** Roots written before this ADR have no locator, and the loader fails closed on a missing one. In `DEMO` this is settled by `chorus-demo reset`, which re-seeds from the manifest. No migration tooling ships in V1; the failure is a loud `INTEGRITY_ERROR` rather than a silent under-count, which is the correct direction for a corroboration input.

## Revisit condition

Revisit if evidence derivation ever becomes deep or high-fanout enough that per-case closure resolution stops being a bounded pair of batch gets. The fix is a stored closure computed at write time, recorded in a superseding ADR — never a scan and never a relaxation of the traversal.
