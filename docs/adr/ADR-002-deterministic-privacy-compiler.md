# ADR-002: Deterministic privacy compiler boundary

**Status:** Accepted
**Date:** 2026-08-30
**Deciders:** Ambient CHORUS maintainers and product owner

## Context

Filtering/redacting LLM prose after private data is present cannot provide reliable authorization, minimum necessity, freshness, or auditability. Contributor grants are fact-, identity-, destination-, purpose-, version-, and time-specific.

## Decision

Only a deterministic Python compiler in a dedicated Lambda may construct `ShareableCaseView`. It reloads current private state, evaluates policy/v1 in a fixed order, uses allowlisted transformations, creates separate safe types/evidence derivatives, hashes canonical RFC 8785 JSON, and fails closed. It also performs the final send-snapshot fence check.

## Alternatives considered

- LLM-generated email followed by redaction: rejected; secrets already reach the external-writing model.
- Prompt-only privacy instructions: rejected; untrusted evidence can attack prompts and output is nondeterministic.
- General policy engine/DSL: capable but excessive and harder to audit under the hackathon deadline.
- Compiler embedded in API process: simpler deployment but weakens sole-writer IAM and side-effect separation.

## Why chosen

The compiler makes authorization reproducible, testable, immutable, and independent of agent quality. A dedicated principal enforces sole creation of views.

## Consequences

- Policy changes require code/version/tests and may require recompile/reapproval.
- Safe transformation coverage is intentionally narrow; unsupported facts stay private.
- Compiler code and role are high-value and require focused review.

## Revisit condition

Revisit the hard-coded registry only when multiple policies/destinations create verified duplication that cannot be handled by versioned Python tables. A DSL must preserve closed schemas, deterministic order, provenance, and equivalent testability.
