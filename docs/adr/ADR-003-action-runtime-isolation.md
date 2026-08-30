# ADR-003: Isolate the Action Agent with zero data-plane tools

**Status:** Accepted
**Date:** 2026-08-30
**Deciders:** Ambient CHORUS maintainers and product owner

## Context

The Action Agent creates externally oriented wording and processes untrusted safe facts. Giving it repositories, retrieval, private evidence, compiler, or sender access would turn prompt injection/model error into a disclosure or side-effect path.

## Decision

Deploy Action in its own AgentCore Runtime/resource role. It receives exactly a current `ShareableCaseView`, has no Strands tools, no conversational/session memory, and no DynamoDB, S3, SES, compiler, sender, shell, browser, or general network permission. It returns a structured cited proposal only. Application code validates and persists; a separate sender sends.

## Alternatives considered

- Same process/role as API: cheapest but relies on application discipline and ambient credentials.
- Read-only Share table access: unnecessary; orchestrator can supply one immutable payload and direct access invites stale/unbounded reads.
- Safe S3 access: unnecessary in V1; opaque caption/ref is sufficient.
- Agent calls SES after approval: conflates probabilistic generation with deterministic execution.

## Why chosen

The architecture, not the prompt, guarantees the agent cannot retrieve protected facts or act autonomously. Separate deployment artifacts/IAM make the proof inspectable.

## Consequences

- The orchestrator must load/hash/project the view.
- Action cannot dynamically research or inspect evidence bytes.
- IAM/deployed canary tests become release gates.

## Revisit condition

Revisit only for a specific external-safe tool whose data/actions cannot be represented as an immutable input. The tool needs a separate least-privilege proxy, threat model, policy contract, and superseding ADR; private access and SES remain forbidden.
