# ADR-028: The deadline watcher — an unsigned event, a re-checked commitment, and the schedule the demo does not fake

**Status:** Accepted
**Date:** 2026-09-07
**Deciders:** Ambient CHORUS maintainers and product owner
**Amends:** [02-trust-iam-deployment-configuration.md](../architecture/02-trust-iam-deployment-configuration.md) § IAM notation and resources, § Principal-specific constraints; [04-domain-state-and-events.md](../architecture/04-domain-state-and-events.md) § Commitment; [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Shareable table mapping, § Transaction boundaries; [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) § Scheduler flow, § Demo clock without scheduler theater; [ADR-006](ADR-006-eventbridge-commitment-scheduling.md); [implementation-plan.md](../plans/implementation-plan.md) § Phase 9 exit criteria
**Depends on:** [ADR-027](ADR-027-commitment-extraction-grounding-and-authority.md)

## Context

Three things about the watcher are unresolved in the frozen documents.

**"The same signed `CommitmentDueEvent`."** [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) § Demo clock says the demo route invokes the watcher with a *signed* event. Nothing defines the signature, the key, or the verifier. Two readings are live: either the signature is load-bearing, in which case a second attestation boundary needs designing, or it is decorative, in which case the word must go — a signature that exists in prose and not in code is the worst of both.

**The entity requires what the transaction attaches afterwards.** `Commitment.scheduler_name`, `schedule_generation`, and `due_event_id` are all non-optional, while [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md) § Transaction boundaries says to "write commitment `PENDING` … then conditionally attach scheduler name/generation". And `PENDING_SCHEDULE`, which § Scheduler flow describes as the visible unscheduled state, is not a member of `CommitmentStatus`.

**The exit criterion requires a live deploy in a phase that has none.** Phase 9's exit criteria say "live reply creates one real future schedule", while Phase 11 owns every live AWS resource in this system — the compiler, the three runtimes, and the sender all use the same static-now split.

## Decision

### 1. There is no scheduler port until there is one, and then it has four methods and no more

`chorus.ports.scheduler`:

```python
class DeadlineSchedulerPort(Protocol):
    async def create_due_schedule(self, request: DueScheduleRequest) -> ScheduleOutcome: ...
    async def describe_schedule(self, name: str) -> ScheduleDescription | None: ...
```

`ScheduleOutcome` is `ScheduleCreated | ScheduleAlreadyExists | ScheduleCreateFailed(reason_code)`.

There is **no `delete_schedule` and no `update_schedule`.** `ActionAfterCompletion=DELETE` handles cleanup, and V1 has no reschedule verb ([ADR-027](ADR-027-commitment-extraction-grounding-and-authority.md) § 4 and § 5), so a method to change a deadline would be a method with no legitimate caller and one obvious illegitimate one.

`DueScheduleRequest` carries `{schedule_name, client_token, at_utc, payload}` and nothing else. All four are derived, none is passed in:

```text
schedule_name = "chorus-{env}-{namespace_hash8}-{commitment_id}-{generation}"
client_token  = uuidv5(SCHEDULE_TOKEN_NAMESPACE, commitment_id | generation)
at_utc        = the demo mapping of § 5, or due_at
payload       = CommitmentDueEvent
```

`CommitmentDueEvent` is frozen exactly as [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) prints it — `commitment-due/v1`, with `event_id = uuidv5(DUE_EVENT_NAMESPACE, commitment_id | generation)`, `namespace`, `case_id`, `commitment_id`, `expected_generation`, and `logical_due_at`. Schedule configuration is likewise unchanged: one-time `at(...)`, UTC, flexible window `OFF`, `ActionAfterCompletion=DELETE`, maximum event age 1 hour, maximum retry attempts 3, encrypted standard SQS DLQ.

### 2. The event is not signed, and it is not trusted either

**The word "signed" is removed** from § Demo clock. The event carries no MAC and needs none, because the watcher grants it exactly one power: naming which commitment to load. Every other field is re-verified against the strongly-loaded row before anything moves.

Adding a second HMAC boundary here would be theatre, and the contrast with [ADR-026](ADR-026-inbound-reply-trust-and-correlation.md) is the argument. There, the attester exists because the payload carries a value **nothing durable can confirm** — a stranger's authorship. Here, every field is a value the commitment row already holds, so re-reading it is strictly stronger than verifying a signature over it. What keeps a stranger from invoking the watcher is IAM: only the scheduler execution role and the demo-clock route may invoke the function, and a caller who could do that could already do worse.

### 3. The watcher's due check, in order

1. Strong-load the commitment by `{namespace, case_id, commitment_id}` from the Shareable table. Not found → success no-op, `commitment.replayed / WATCHER_UNKNOWN_COMMITMENT`.
2. Verify `namespace`, `case_id`, `schedule_generation == expected_generation`, `due_event_id == event.event_id`, and `logical_due_at == due_at`. Any mismatch → success no-op, `WATCHER_STALE_GENERATION`. **The commitment is not changed and the schedule is not recreated.**
3. `status != PENDING` → success no-op, `WATCHER_REPLAY`. This one branch covers duplicate scheduler delivery, late delivery, the demo clock racing the real schedule, and a commitment a human already satisfied.
4. `clock.now() < due_at` → success no-op, `WATCHER_EARLY`. An early firing is not an error and is not rescheduled; the real one-time schedule fires again at its own time.
5. Otherwise transaction **D**.

**Transaction D — three participants**, Shareable / Audit:

1. the guarded commitment update `PENDING → DUE`, conditioned on the exact `version`, `status == PENDING`, and `due_event_id == event.event_id`;
2. the verification-request projection, create-only, Shareable `NS#n#CASE#k / VERIFICATION_REQUEST#{commitment_id}#{generation}` — this is the durable form of the `VerificationRequested` event, and being create-only is what makes "exactly one verification request" a property rather than a hope;
3. the `commitment.due` audit event.

There is no idempotency record: the commitment row conditioned on its own `due_event_id` *is* the proof, and a fourth participant would be a second answer to a question already settled. The watcher takes **no case edge** — `ACTIONED → VERIFYING` happened at creation ([ADR-027](ADR-027-commitment-extraction-grounding-and-authority.md) § 7, transaction B) — so the watcher never writes a case row, in either table.

### 4. The schedule is created after the commitment, and its absence is visible

`scheduler_name`, `schedule_generation`, and `due_event_id` are **derived at creation**, not attached afterwards: all three are deterministic functions of `commitment_id` and `generation = 1`, which are known before any AWS call. The entity's non-optional fields are therefore satisfiable by transaction B, and [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md)'s "attach scheduler name/generation" is corrected to describe what actually varies.

What varies is whether the schedule exists, and that is the **commitment schedule projection**:

| Item | PK | SK | Mutability |
|---|---|---|---|
| Commitment schedule projection | `NS#n#CASE#k` | `COMMITMENT_SCHEDULE#c` | mutable, versioned |

carrying `{status: PENDING_SCHEDULE | CREATED, schedule_name, generation, attempts, last_error_code?}`. `PENDING_SCHEDULE` is an **operational projection and not a `CommitmentStatus`** — the enum is unchanged. A commitment whose schedule failed is genuinely `PENDING`; what failed is the alarm clock, not the promise, and encoding an infrastructure outcome as a domain status is how the two come to be confused.

Sequence: transaction B commits (commitment `PENDING`, projection `PENDING_SCHEDULE`) → `create_due_schedule` → transaction **B′**, two participants: the guarded projection update to `CREATED` and the `schedule.created` audit event. A failure writes `schedule.failed`, increments `attempts`, records the typed code, and leaves the case visibly unscheduled with a banner. Retry uses the **same name and the same client token**; a lost create response is reconciled by `describe_schedule` on the exact name and configuration, never by creating a second differently-named schedule. There is no schedule-ARN field on the commitment: the name is deterministic, so an ARN would be a second copy of a derivable value that can disagree.

### 5. One clock, and the demo advances it

The deployment supplies exactly one `Clock` to the watcher. In `demo` it is a **logical clock**, monotonic, persisted in the demo manifest partition and advanced only by `POST /v1/demo/clock/advance`. Elsewhere it is `SystemClock`. The watcher never holds two clocks and never chooses between them, so step 4's comparison means the same thing on both paths.

The scheduler adapter's demo mapping is unchanged from [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md): `actual_now + max(10 minutes, logical_due - logical_now)`, with both values audited. A **real** one-time schedule is created — the demo does not fake the resource — and the later real invocation is a harmless replay under step 3, because the logical clock has by then long passed `due_at` and the commitment is no longer `PENDING`.

`/demo/clock/advance` invokes the same watcher function with the same `CommitmentDueEvent` and a `trigger=DEMO_CLOCK` audit field. It does not mutate a commitment, and it cannot: the demo route holds no repository write path to `COMMITMENT#`.

### 6. IAM: the watcher's denies, and the application's narrowed scheduler grant

The trust matrix row for the commitment watcher is corrected. It reads `Core: D | Share: R/W(commitment/case projection)`; the case row is in **Core**, which the watcher is denied, and the Shareable `NS#n#CASE#k` partition is where the commitment lives.

| Resource | Commitment watcher |
|---|---|
| Core table | `D` — it reads and writes nothing there, and it takes no case edge |
| Shareable table | `R/W` scoped by `dynamodb:LeadingKeys` to `NS#*#CASE#*` |
| Audit table | `W` |
| Private S3, Export S3 | `D` |
| Bedrock | `D` — the watcher invokes no model |
| SES | `D` — the watcher sends nothing |
| EventBridge Scheduler | `D` — the watcher creates no schedule; it is a target, not a client |
| Compiler, sender, agent runtimes | `D` |

The application's `Scheduler: W` is narrowed to `scheduler:CreateSchedule` and `scheduler:GetSchedule` on `arn:…:schedule/chorus-{env}/*`, plus `iam:PassRole` on the scheduler execution role alone. No `DeleteSchedule`, no `UpdateSchedule` — the port has no method for either, and a grant wider than its caller is a grant waiting for a second caller.

Static negative-capability assertions over the synthesized policies, in the manner [ADR-019](ADR-019-send-fence-partition-isolation.md) established: the watcher role contains no `bedrock:*`, no `ses:*`, no `scheduler:*`, no Core-table action, and no S3 action; the application's scheduler statement contains no delete or update action and names the one schedule group.

### 7. What Phase 9 builds and what Phase 11 deploys

Phase 9 owns the port, the `CreateDueSchedule` command, the deterministic due check, `functions/commitment_watcher` and its composition root, the boto3 EventBridge Scheduler adapter, a manual in-memory adapter for local runs and tests, and the **static** CDK — schedule group, watcher Lambda, its role and log group, the DLQ and its key, and the alarms — synthesized and asserted, deployed by nobody.

**Phase 9's exit criterion is corrected.** "Live reply creates one real future schedule" moves to Phase 11, alongside the live SES inbound wiring and every other live resource. Phase 9's exit criterion becomes: against the local composition and DynamoDB Local, an attested fixture reply produces exactly one commitment, exactly one schedule request with the deterministic name and client token, exactly one `PENDING → DUE` transition across duplicate and early invocations, exactly one verification request, and the two human outcomes — `FULFILLED` alone resolving, `MISSED` returning the case to `READY_FOR_ACTION` with the action pointer invalidated. Phase 11 then proves the same path against the real schedule.

## Alternatives considered

- **Sign the due event and verify it in the watcher.** Rejected: every field is re-verified against the durable commitment, which is strictly stronger, and a second HMAC boundary would suggest the event carries authority it does not.
- **Let the watcher mark a commitment `MISSED` at the deadline.** Rejected in [ADR-027](ADR-027-commitment-extraction-grounding-and-authority.md) § 5 and restated here because this is the component that would do it: time passage is evidence about the clock.
- **Make `PENDING_SCHEDULE` a `CommitmentStatus`.** Rejected: it is an infrastructure outcome, it would need edges into and out of every early state, and a domain status that means "AWS did not answer" invites a domain decision based on one.
- **Store the schedule ARN on the commitment.** Rejected: the name is deterministic, so the ARN is a derivable value stored twice.
- **Reschedule when the watcher fires early.** Rejected: the one-time schedule fires again at its own time, and a rescheduling watcher is a watcher that can be made to schedule.
- **Periodic polling instead of one-time schedules.** Already rejected by [ADR-006](ADR-006-eventbridge-commitment-scheduling.md); its revisit condition is volume and cost, neither of which V1 reaches.

## Why chosen

It removes a word that promised a mechanism nobody built, replaces it with the stronger check that was already there, and makes every derived value derived in one place. It keeps the demo honest — a real schedule, a real watcher, a logical clock that advances the same code path — without letting the demo become the mechanism. And it gives the watcher the smallest role in the system: one edge, one table partition, no model, no mail, and no clock of its own.

## Consequences

- New: `chorus.ports.scheduler`, `chorus.application.commands.create_due_schedule`, `chorus.application.commands.record_commitment_due`, `chorus.infrastructure.scheduler` (boto3) and its local manual adapter, `functions/commitment_watcher`, `infra/cdk/stacks/watcher.py`.
- `COMMITMENT_SCHEDULE#c` and `VERIFICATION_REQUEST#{commitment_id}#{generation}` join the Shareable table mapping.
- `CommitmentStatus` is unchanged; `PENDING_SCHEDULE` is documented as a projection value.
- The word "signed" is removed from [07-action-ses-and-commitments.md](../architecture/07-action-ses-and-commitments.md) § Demo clock; "attach scheduler name/generation" is corrected in [06-persistence-and-evidence.md](../architecture/06-persistence-and-evidence.md).
- The watcher row of the trust matrix is corrected to `Core: D | Share: R/W(NS#*#CASE#*)`; the application's scheduler grant is narrowed.
- Phase 9's live-schedule exit criterion moves to Phase 11; [build-order.md](../plans/build-order.md)'s `9→10` gate changes with it.
- **T19** is extended to name the early-firing and unknown-commitment branches. New **T40**: a watcher invocation for a commitment in another namespace or case.

## Residual risk

**A schedule can exist for a commitment that no longer needs it.** A commitment satisfied before its deadline leaves a live one-time schedule that fires and no-ops. Accepted: the alternative is a delete grant, and a no-op is cheaper than a permission.

**EventBridge Scheduler delivery is at-least-once and eventually consistent.** That is exactly what steps 2–4 and the create-only verification-request projection are for, and [ADR-006](ADR-006-eventbridge-commitment-scheduling.md) already accepts it.

**The demo's logical clock is a real authority in the demo namespace.** A presenter can advance time. It cannot change a commitment outcome, because advancing time reaches only `PENDING → DUE`, and both outcomes still require a contributor.

## Revisit condition

Reopen when a deadline must be changeable — that needs a reschedule verb, a generation increment, an `update_schedule` or delete-and-recreate path, and a rule for what happens to a `DUE` commitment whose deadline moves — or when schedule volume, cost, or quotas make one-time schedules unattractive, which is [ADR-006](ADR-006-eventbridge-commitment-scheduling.md)'s own revisit condition. Recurring calendars and Step Functions remain out of scope by [implementation-plan.md](../plans/implementation-plan.md) Phase 9.
