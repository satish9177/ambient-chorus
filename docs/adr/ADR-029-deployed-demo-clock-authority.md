# ADR-029: The deployed demo clock — one authoritative logical time the watcher can actually read

**Status:** Accepted
**Date:** 2026-09-08
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md) § IAM notation and resources, § Principal-specific constraints; [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Core table mapping, § Shareable table mapping
**Supersedes:** [ADR-028](ADR-028-deadline-watcher-and-scheduler-boundary.md) § 5 **only** in respect of where the demo logical clock is persisted and which principals may read or move it. Every other part of ADR-028 — the unsigned event, the strong reload, the re-verification of namespace, case, generation, due-event ID, due time and status, the single compare-and-swap, and the scheduler grant narrowing of § 6 — is unchanged and remains in force.
**Depends on:** [ADR-028](ADR-028-deadline-watcher-and-scheduler-boundary.md)

## Context

[ADR-028](ADR-028-deadline-watcher-and-scheduler-boundary.md) § 5 says the deployment supplies
**exactly one** `Clock` to the watcher, that in `demo` it is a logical clock "persisted in the
demo manifest partition and advanced only by `POST /v1/demo/clock/advance`", and that the watcher
never holds two clocks and never chooses between them. Step 4 of the watcher's frozen order
compares the commitment's due time against that clock to refuse an early firing.

Three facts, each true today, make that undeployable:

1. [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Core table
   mapping places the demo manifest at `NS#DEMO` / `DEMO_MANIFEST#{seed_version}` — in the
   **Core** table.
2. The watcher's synthesized role carries `DenyAllCoreAccess`: an explicit
   `Effect: DENY, Action: dynamodb:*` on the Core table ARN and every index under it. An explicit
   deny cannot be overridden by any later allow, so no grant can ever let the watcher read that
   row.
3. `chorus.infrastructure.local.demo_clock.LogicalDemoClock` is process-local and says so in its
   own docstring. In a deployed system the watcher is a separate Lambda from the API that
   advanced the clock, so a process-local clock is not merely non-durable — it is *a different
   clock*, seeded at the module default, in a different process.

The three available exits are all worse than the one below. **Trusting the scheduler event's
timestamp** contradicts ADR-028 § 2–3, which exist precisely because the event is unsigned and
untrusted. **Narrowing the watcher's Core deny** trades the system's single cleanest IAM
sentence — "the watcher cannot reach the private table at all" — for one timestamp. **Reverting
the deployed watcher to `SystemClock`** breaks § 5's "never holds two clocks": the API would
advance logical time, the watcher would read wall time, and the early-firing comparison would
mean two different things on the two paths — the exact defect § 5 was written to prevent.

## Decision

### 1. The logical clock is its own item, in the Shareable table

| Item | Table | PK | SK | Mutability |
|---|---|---|---|---|
| Demo logical clock | **Shareable** | `NS#DEMO#CLOCK` | `DEMO_CLOCK` | forward-only under OCC; reset is the sole backward transition (§ 3) |

The partition key is the **exact literal** `NS#DEMO#CLOCK`. It is not a family of partitions and
there is no wildcard form of it: `DEMO` is the only namespace a deployed clock exists in, because
`Settings.validate_environment_contract` already refuses any other namespace in the `demo`
environment. If a future environment token ever changes the literal, the literal changes with it
and the invariant does not: **no grant in this system authorizes an arbitrary namespace's clock.**

The row carries:

```text
{ logical_time, version, reset_generation, seed_instant, advance_count }
```

and nothing else. No case ID, no community ID, no content, no reference to any private entity:
**a timestamp is not private data**, and locating it in the shareable zone is a statement about
what it is rather than a concession to make a permission fit.

The demo **manifest and reset lock stay in Core** at `NS#DEMO` unchanged. They hold partition
roots, object prefixes, and schedule names — reset-tooling state the watcher has no business
reading. Splitting the clock out of the manifest is the whole change.

### 2. The exact IAM boundary

Every grant below names the single literal partition `NS#DEMO#CLOCK` through
`dynamodb:LeadingKeys`. **There is no `NS#*#CLOCK*` grant anywhere**, and a policy containing one
fails review: a wildcard here would authorize a clock in a namespace no deployment has, which is
the shape of permission that is correct on the day it is written and wrong after the next
namespace exists.

| Principal | Read | Write |
|---|---|---|
| **API (presenter path)** | strongly consistent | the guarded forward-only CAS of § 3, behind `POST /v1/demo/clock/advance` |
| **Commitment watcher** | strongly consistent | **none** — no `PutItem`, `UpdateItem`, `DeleteItem`, or `ConditionCheckItem` on this prefix, in any form |
| **Demo reset principal** | strongly consistent | read/write, and it is the **sole** reset/reseed exception (§ 3), bounded to the `DEMO` namespace and the `demo` environment |
| Worker, compiler, sender, all three agent runtimes, scheduler execution role | — | — |

The watcher's Core deny is **unchanged and total**. Its Shareable authority grows by exactly one
read-only literal partition, and its write authority does not grow at all — `NS#*#CASE#*` remains
the complete set of partitions it may write. The existing shareable write denies extend to the
clock prefix, so the absence of a write grant is backed by an explicit deny as well.

### 3. Reset is the only legitimate backward transition, and it is fenced

Time moves forward. The one exception is reset, which restores the frozen seed instant — and a
mechanism that permits exactly one backward move must also guarantee that nothing from before
that move can act afterwards.

**`reset_generation` is that guarantee.** It is a durable, monotonically increasing epoch that is
**never reused**.

**A normal advance** binds both fences and the forward rule:

```text
condition:  version           == expected_version
        AND reset_generation  == expected_reset_generation
        AND new_logical_time  >  current logical_time
```

All three are condition expressions the store evaluates, not checks a process performs. A clock
that went backwards would make an already-`DUE` commitment look early and a fired schedule look
unfired, so the rule that was a Python `raise` becomes something every concurrent writer is held
to.

**A reset** must, in this order:

1. acquire the existing `DEMO_RESET_LOCK` demo-reset exclusion (it is the same lock that already
   serialises reset; no second lock is introduced);
2. advance `reset_generation` monotonically, never reusing a value;
3. restore `logical_time` to the frozen `seed_instant` and zero `advance_count`;
4. begin a fresh `version` sequence, so that no stale optimistic-concurrency token from the
   previous generation can coincide with a live one;
5. persist all of the above **atomically**, so there is no instant at which the generation has
   moved and the time has not, or the reverse.

**The invariant this buys:** an advance carrying `reset_generation = N` always fails once reset
has produced `N+1`, *even if the numeric `version` happens to repeat*. A command in flight from
the previous demo run cannot move the new run's clock. Version alone could not promise that,
because a fresh sequence necessarily revisits low numbers; the generation is what makes the
version's ambiguity harmless.

### 4. Reading, and failing closed

Clock reads are **strongly consistent**, on every path, always. An eventually consistent read of
the authority a deadline is judged against is a deadline judged against a guess.

If the clock row is **missing, corrupt, or unavailable**, the reader **fails closed**: the
watcher's operation fails typed and no state advances, and the API's advance route returns a
typed error. The three things it must never do:

- **never** fall back to a process-local `SteppableClock` or `LogicalDemoClock`;
- **never** fall back to `SystemClock` in the `demo` environment;
- **never** treat the scheduler event's timestamp as authoritative logical time — the event is
  unsigned and remains so ([ADR-028](ADR-028-deadline-watcher-and-scheduler-boundary.md) § 2–3).

A missed firing is recoverable and visible. A firing judged against a fabricated clock is neither.

### 5. Late scheduler delivery is a successful no-op

Advancing logical time in the browser can cause a commitment to be processed before the real
one-time schedule fires. When the real delivery arrives afterwards, **the existing watcher checks
already make it an idempotent successful no-op** and no new mechanism is added: the watcher
strongly reloads the commitment, re-verifies namespace, case, generation, due-event ID, due time,
and status against the loaded row, and finds a status that is no longer eligible. Its single
compare-and-swap does not run, nothing is written, and the invocation succeeds. That is ADR-028
§ 2–3 behaving exactly as specified, not an accommodation this ADR makes for the demo.

### 6. What does not change

Outside `demo`, the clock is `SystemClock` and no clock row exists; the watcher holds the read
grant in every environment and finds nothing to read in most of them, which is correct — the
grant describes an authority, not an expectation. The watcher still strongly reloads the
commitment and re-verifies every field. The scheduler event remains untrusted; reading an
authoritative clock does not make the event authoritative. Advancing time still reaches
`PENDING → DUE` and stops: both terminal outcomes still require a contributor, so a presenter can
make a deadline arrive and cannot make a promise kept.

## Alternatives considered

- **Narrow the watcher's Core deny to admit `NS#DEMO` reads.** Rejected: it converts a total deny
  into a conditional one, and the condition is correct on the day it is written and wrong after
  the next partition is added to `NS#DEMO`.
- **Pass the clock reading in the scheduler event payload.** Rejected outright by ADR-028 § 2–3.
- **Keep the clock in the manifest row and give the watcher a Core `GetItem` on that one key.**
  Rejected: `dynamodb:LeadingKeys` constrains the partition key, not the sort key, so a grant on
  `NS#DEMO` is a grant on the reset lock and every future item in that partition — the identical
  defect [ADR-019](ADR-019-send-fence-partition-isolation.md) found for the send fence.
- **Version-only optimistic concurrency, without a reset generation.** Rejected: reset restarts
  the version sequence, so an in-flight advance from the previous generation can carry a version
  that is live again. The failure is rare, silent, and moves the clock of a run that did not
  request it.
- **A separate tiny table for the clock.** Rejected: a fourth table needs its own ADR, its own
  IAM row, and its own reset accounting, to hold one item the shareable trust zone already fits.

## Why chosen

It is the smallest change that leaves every existing invariant literally intact. The watcher's
Core deny stays total. The watcher gains no write. The event stays untrusted. One clock is
supplied to one slot, as ADR-028 § 5 requires, and it is now the *same* clock in the API process
and the watcher process — which is what "exactly one clock" was always supposed to mean and could
not mean while it lived in a Python object.

## Consequences

- New Shareable item type at the literal partition `NS#DEMO#CLOCK`; `06-persistence-and-evidence.md`
  § Shareable table mapping gains the row and § Core table mapping's demo manifest row loses the
  clock.
- The clock record gains `reset_generation` and `seed_instant`; the advance command gains
  `expected_reset_generation` beside its existing `expected_version`.
- `LogicalDemoClock` gains a durable repository-backed implementation; the process-local one
  remains the `test`/`development` adapter and is never constructed in `demo`.
- The watcher role gains one read-only statement scoped to the exact literal partition; the API
  role gains read plus the guarded CAS; the reset principal gains read/write. All three are
  asserted from the synthesized template, and a wildcard clock grant fails the assertion.
- The reset boundary ([the deployment contract](../plans/phase-11-deployment-contract.md) § 12)
  performs steps 1–5 of § 3 as part of the bounded `DEMO` purge.
- Deployed watcher decisions become reproducible across process restarts, which is what makes the
  demo's 4:30–5:00 segment provable rather than incidental.
