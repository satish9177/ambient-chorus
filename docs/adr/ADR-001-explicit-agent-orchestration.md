# ADR-001: Explicit application-controlled agent orchestration

**Status:** Accepted
**Date:** 2026-08-30
**Deciders:** Ambient CHORUS maintainers and product owner

## Context

CHORUS has three agent roles separated by deterministic privacy, persistence, approval, and side-effect boundaries. Debuggability, typed payloads, retry ownership, and proof that private context does not flow into Action matter more than dynamic agent collaboration.

## Decision

Use hand-written application use cases that invoke individual stateless Strands agents. Agents never call one another. Do not use Strands Graph, Workflow, Swarm, agents-as-tools routing, shared agent state, or an LLM orchestrator in V1.

## Alternatives considered

- **Agents as tools:** concise but makes a model decide routing and can propagate implicit context.
- **Strands Graph/Workflow:** explicit edges but adds a second workflow/state abstraction around simple sequential use cases.
- **Swarm/dynamic routing:** flexible but incompatible with least privilege and deterministic debugging.
- **Hybrid graph plus deterministic nodes:** workable, but duplicates application state/retry semantics for only three agents.

## Why chosen

Application code makes every input projection, validation, persistence point, timeout, retry, and trust-zone crossing reviewable. The actual workflow has human pauses and durable state, so it is not one in-memory agent graph.

## Consequences

- More orchestration code and explicit DTO conversion.
- Clear unit tests, error ownership, IAM calls, and replay behavior.
- Adding an agent requires an explicit use case/contract/role rather than a dynamic registry.

## Revisit condition

Revisit only if V1 grows to at least five independent agent steps with stable, non-sensitive DAG dependencies and a graph demonstrably reduces code without weakening payload/IAM boundaries. Requires a superseding ADR and full privacy evaluation.
