# ADR-019: The send fence gets its own Core partition

**Status:** Accepted
**Date:** 2026-09-05
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md) § IAM notation and resources, § Principal-specific constraints; [05-privacy-compiler-and-shareable-view.md](../architecture/05-privacy-compiler-and-shareable-view.md) § Freshness and send authorization fence; [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Key grammar, § Core table mapping, § Transaction boundaries; [10-security-threat-model.md](../architecture/10-security-threat-model.md) § Threat register; [12-evaluation-and-testing.md](../architecture/12-evaluation-and-testing.md) § IAM boundary tests

## Context

### A guarantee that was written down and could not be enforced

[02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md) states the compiler's Core access as an IAM property:

```text
Compiler Lambda | Core: R(all) / W(fence only) | ...
```

Phase 6 is the first phase to synthesize the compiler role, and synthesizing it made the sentence checkable. It was not true.

The fence was keyed inside the case partition:

```text
PK = NS#{namespace}#CASE#{case_id}
SK = SEND_FENCE
```

So were the case row and everything the case owns:

```text
SK CASE              SK FACT#{fact_id}        SK REPORT#{report_id}
SK EVIDENCE#{id}     SK MANDATE#{id}#VERSION#{n}
SK MANDATE_CURRENT#{id}   SK ASSESSMENT#{created_at}#{id}
```

`dynamodb:LeadingKeys` filters on the **partition key**. DynamoDB exposes no condition key that constrains a sort key. A grant of `dynamodb:PutItem` and `dynamodb:DeleteItem` over `NS#*#CASE#*` — the narrowest grant that could still let the compiler write a fence — therefore authorized writing and deleting *every item in every case partition*: the case row, its facts, its reports, its evidence, its mandate versions, its current mandate pointers, and its assessments.

The effective answer to "can compiler credentials mutate the CASE row" was **yes**, against a document that said the compiler's only Core write was the fence.

### The architecture already knew why

[06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) says of the Shareable table:

> Entity-type partition prefixes deliberately support IAM `dynamodb:LeadingKeys`; **IAM cannot safely authorize by sort-key prefix.**

That is exactly the constraint the fence's placement ran into. The Shareable table was keyed to respect it; Core was not, and the fence is the one Core item whose writer is supposed to be separable from the rest of its partition.

### Why this is not a residual risk to accept

[10-security-threat-model.md](../architecture/10-security-threat-model.md) lists a **compromised compiler** as an in-scope actor (T24), and the compiler is described there as "a high-value trusted component". The whole point of granting it `W(fence only)` was to bound what a compromise of that component could reach. Recording the broad write as accepted residual risk would remove the bound while keeping the sentence that promised it.

### Why `ConditionCheckItem` is part of the answer but not all of it

DynamoDB authorizes `TransactWriteItems` through the permission each **participant** needs: `dynamodb:ConditionCheckItem` for a `ConditionCheck`, and `dynamodb:PutItem` / `UpdateItem` / `DeleteItem` for the write kinds. The four are distinct members of `TransactWriteItem` in the API and distinct actions in IAM.

That distinction lets the compile transaction's **case-version guard** exist with no write grant on case partitions at all — a `ConditionCheck` is read-only authority. It does nothing for the fence, which needs a real `PutItem` and `DeleteItem`, indistinguishable by IAM from the same operations against `CASE` while the two share a partition.

## Decision

**Move the send fence to its own Core partition.**

| | Partition key | Sort key |
|---|---|---|
| Old | `NS#{namespace}#CASE#{case_id}` | `SEND_FENCE` |
| **New** | `NS#{namespace}#FENCE#{case_id}` | `SEND_FENCE` |

The fence **stays in the Core table**. Only its partition placement changes. No new table is introduced.

### What is unchanged

Everything except the physical address:

- **Send-fence semantics are unchanged.** Acquire takes an absent or expired fence; the same `execution_id` replays its own live fence; a replay does **not** extend the original expiry; a different execution meeting a live fence conflicts.
- **Ownership is unchanged.** The compiler remains the fence authority and exposes the same typed `AcquireSendAuthorizationFence` / `ReleaseSendAuthorizationFence` boundary.
- **Expiry semantics are unchanged**, including exact-microsecond comparison against `expires_at_micros` and the rule that equality at the deadline is expired. The TTL attribute is still never what a condition compares.
- **Acquisition replay is unchanged.**
- **Stale release is unchanged**: release is conditioned on the holder's `execution_id`, so a stale process cannot clear another execution's fence.
- **Transaction semantics are unchanged.** Compile, mandate decisions and investigation applies each still stage a no-live-fence `CheckItem`, and the participant counts do not move. `TransactWriteItems` spans partitions and tables within one account and Region, so a guard in another partition is a physical difference and not a semantic one.
- **No Phase-8 behaviour moves into Phase 6.** The sender is still the only caller of acquire and release, and it still does not exist.

### What follows in IAM

With the fence separately addressable, the frozen sentence becomes expressible:

| Grant | Actions | Leading keys |
|---|---|---|
| Read Core | `GetItem`, `BatchGetItem`, `Query` | unrestricted — `R(all)` |
| Case-version guard | `ConditionCheckItem` | `NS#*#CASE#*`, restricted to `EnclosingOperation = TransactWriteItems` |
| Fence guard | `ConditionCheckItem` | `NS#*#FENCE#*`, same restriction |
| Fence mutation | `PutItem`, `DeleteItem` | `NS#*#FENCE#*` |
| Case-partition writes | `PutItem`, `UpdateItem`, `DeleteItem` | **explicitly denied** for `NS#*#CASE#*` |

`dynamodb:UpdateItem` is granted nowhere: acquire is a conditional put and release a conditional delete, so there is no update path to authorize. The blanket `dynamodb:TransactWriteItems` action is not granted either, because AWS authorizes a transaction through its members and a blanket grant would be a permission this role does not need.

### Migration

**None is required.** V1 has not been deployed, so no stored fence exists at the old address. Development and test data may be reset freely.

`chorus-demo reset` — Phase-11 work that does not exist yet — **must include the `NS#{namespace}#FENCE#{case_id}` roots** in its manifest alongside the case partitions it already clears. A reset that cleared a case and left its fence would leave an unexpired fence guarding a case that no longer exists, and the next authorization-sensitive mutation would be refused for up to sixty seconds by a fence nobody holds.

## Alternatives considered

- **Move fence ownership out of the compiler.** Rejected: [ADR-002](ADR-002-deterministic-privacy-compiler.md) and [05-privacy-compiler-and-shareable-view.md](../architecture/05-privacy-compiler-and-shareable-view.md) make the compiler the fence authority, and relocating a side-effect owner is a far larger change than relocating a key.
- **A separate fence table.** Rejected: a table is a bigger unit than the problem. The trust split the three tables express is private / shareable / audit, and a fence is private state; adding a fourth table would blur that boundary to solve a partition-key problem.
- **Restrict the write with `dynamodb:Attributes`.** Rejected as insufficient. It filters attribute *names*, not values, so an attacker could still overwrite `SK=CASE` with a fence-shaped item; and `DeleteItem` carries no attributes, so the case row would remain deletable.
- **Document the broad Core write as accepted residual risk.** Rejected: T24 names a compromised compiler as in scope, and this boundary is what bounds that compromise. Accepting the risk would keep the promise and remove the guarantee.
- **Deny-only, leaving the fence in the case partition.** Rejected as the sole fix. An explicit deny of case-partition writes does work, and it is kept here as defence in depth — but relying on a deny to carve a hole out of an over-broad allow means the allow still describes the wrong boundary, and a future statement that reordered or narrowed the deny would silently restore it.

## Why chosen

It is the smallest change that makes an already-accepted sentence true, and it uses the pattern this repository already chose for the same reason one table over. Nothing about the fence's behaviour, ownership, or lifecycle moves; one key builder does.

## Consequences

- `chorus.infrastructure.dynamodb.keys` gains `fence_partition(namespace, case_id)`. `codec_fence.send_fence_key` is the single authority every fence path resolves through, so load, acquire, release and the no-live-fence condition move together or not at all.
- The compiler role's Core statements become read, two `ConditionCheckItem` grants, one fence-scoped write grant, and an explicit deny of case-partition writes.
- Static CDK tests assert the **negative** capability by exhaustive search over the synthesized statements rather than by spot check: no Core allow that grants a write action may name leading keys outside the fence prefix.
- The frozen per-case DynamoDB item collection no longer contains the fence. No access pattern read it by prefix query — every fence read is a direct get — so no query changes.
- [12-evaluation-and-testing.md](../architecture/12-evaluation-and-testing.md)'s post-deploy compiler canary gains a case-write probe: an attempt to put or delete a case-partition item under the compiler role must return `AccessDenied`.

## Revisit condition

Revisit if DynamoDB ever gains a condition key that constrains sort keys, which would make entity-type partition prefixes an optimisation rather than a security requirement. Until then, any Core item whose writer must be separable from the rest of its case is keyed the way the fence now is, and that requirement is decided when the item is designed rather than when its role is synthesized.
