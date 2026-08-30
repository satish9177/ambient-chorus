# ADR-006: EventBridge Scheduler for commitment deadlines

**Status:** Accepted
**Date:** 2026-08-30
**Deciders:** Ambient CHORUS maintainers and product owner

## Context

External promises can be due days later. `ACTIONED` must not imply resolution, scheduler delivery/retries can repeat, and the five-minute demo cannot wait for wall-clock precision.

## Decision

Create one-time EventBridge Scheduler schedules targeting an idempotent watcher Lambda. Use deterministic schedule name/client token/generation/event ID, flexible window off, auto-delete, bounded retry, and DLQ. A controlled demo clock invokes the same watcher event path; a real future schedule is still created.

## Alternatives considered

- In-process timers: lost on process/Lambda termination.
- Periodic DynamoDB polling: simpler resource model but repeated scans/latency and harder per-commitment audit.
- Step Functions wait: durable but introduces workflow engine/state complexity.
- Agent memory/reminders: nondeterministic and wrong trust owner.

## Why chosen

Scheduler is purpose-built for one-time invocations and keeps commitment logic deterministic. Replay safety lives in the watcher, not assumed delivery semantics.

## Consequences

- Schedule creation is an external side effect requiring reconciliation and a DLQ.
- Due events request verification; they cannot resolve a case.
- Demo logical time and actual schedule time are both audited.

## Revisit condition

Consider periodic polling only if schedule volume/cost/quotas become material or commitments require bulk/calendar rescheduling. Do not introduce Step Functions for this flow alone.
