# ADR-031: DEMO reset mutation interlock

**Status:** Accepted — the Phase 11 final residual-fix instruction explicitly requires the
atomic Core reset-lock condition on all normal DEMO mutations, including sender and watcher.

## Decision

Every normal DEMO storage mutation includes `ConditionCheckItem` on Core `NS#DEMO` /
`DEMO_RESET_LOCK`, requiring absence, in the same transaction. This includes single-item
repository writes. Non-DEMO writes are unchanged. Only reset composition bypasses this
condition while it owns the lock. Transaction size is checked after adding participants.

Sender and watcher retain no Core item reads or writes. Their former total Core deny gains
one read-only condition exception at the exact `NS#DEMO` leading key; all other Core actions
and all other Core condition targets remain explicitly denied. Compiler gains the same
condition. This exception cannot read a private item or mutate the reset lock.

The first case write atomically registers its CASE, FENCE, VIEW_CURRENT, and ACTION_CURRENT
roots. Reset follows every registered case's pointer/history chains, not one predicted case.
Registration markers survive interrupted purges and are cleared only after their targets
are removed and the fresh manifest and seed verification are complete.

Before an evidence write or schedule creation, a normal command reserves a unique attempt
using the existing `idempotency-record/v1` IN_PROGRESS contract in its existing contextual
partition. The reservation is subject to the same atomic lock condition. It carries only
identifiers/digests, grants no export authority, and is not pending evidence publication.
After lock acquisition, reset strongly enumerates these reservations and refuses before
purge if any attempt remains in progress. A successful external call completes its attempt;
an exception or ambiguous outcome retains it and fails reset closed. No elapsed-time guess
or negative object/schedule listing proves an active attempt is finished.

The existing evidence authorization commit, content addressing, send states, one-use
approval, and no-automatic-retry SEND_UNKNOWN rules are unchanged. Reset still refuses
SENDING and SEND_UNKNOWN. Per-attempt reservations do not authorize retries of SES.

Reset rechecks its receipt under lock. Seed transaction tokens include the persisted reset
generation as the durable reset identity: stable within a stage retry, distinct after a
new reset. The authoritative clock row is never deleted to obtain this identity.

## Consequences

A reset concurrent with an unresolved external attempt is refused rather than reporting
successful cleanup while that attempt can still create an object. Crash/unknown attempts
must remain closed until their outcome and quiescence are established; this change does not
add an automatic ambiguous-outcome retry or a new recovery service.
