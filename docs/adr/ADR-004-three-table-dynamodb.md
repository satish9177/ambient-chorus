# ADR-004: Three trust-aligned DynamoDB tables

**Status:** Accepted
**Date:** 2026-08-30
**Deciders:** Ambient CHORUS maintainers and product owner

## Context

CHORUS needs transactional case aggregates, immutable authorization artifacts, least-privilege principals, and simple known access patterns. A single physical table makes private/shareable IAM depend heavily on key conditions; a table per entity adds operational/transaction ceremony.

## Decision

Use three DynamoDB tables: private `core`, external-safe/action `shareable`, and append-only `audit`. Within each table, use case/community item collections with `PK/SK`. Use cross-table `TransactWriteItems` where state and audit must be atomic. Add no V1 GSI because all approved endpoints know community/case IDs.

## Alternatives considered

- One single table: fewer resources but weaker trust separation and broader accidental reads.
- Table per entity: obvious schemas but many resources, repositories, and cross-table operations.
- Relational database: useful constraints/joins but unnecessary operations/VPC/migrations for bounded key access.

## Why chosen

Three tables align with IAM/trust zones while keeping partitions and transactions simple. It is a security-driven split, not single-table fashion.

## Consequences

- Some transactions span tables and must remain under DynamoDB limits.
- No list-all-case query exists in V1; scans are forbidden in handlers.
- Safe/private duplication is explicit and compiler-owned.

## Revisit condition

Add a GSI or change storage only when a concrete access pattern/scale measurement requires it. Merging trust zones requires a security ADR; splitting tables requires demonstrated transaction/throughput benefit.
