# ADR-007: Structured cited claims and deterministic email rendering

**Status:** Accepted
**Date:** 2026-08-30
**Deciders:** Ambient CHORUS maintainers and product owner

## Context

A free-form email can contain unsupported facts, hidden private details, header/HTML injection, or text that is impossible to validate against a safe view. Human approval must bind exact immutable content.

## Decision

Action returns `subject`, cited `claims`, structured request/deadline, caveats, and tone. Deterministic code verifies every factual claim against export fact IDs, blocks unsupported tokens/foreign IDs, and renders plain-text/escaped HTML with a versioned template. Proposal/view/render hashes are approval inputs.

## Alternatives considered

- Free-form email plus redaction: rejected; citations and completeness cannot be guaranteed.
- Model-generated HTML: rejected; injection/layout and factual-surface risks.
- Fully deterministic prose: safest but loses useful proportionate summarization and agent contribution.
- Human-written email: reliable but misses product/action-agent demonstration and repeatability.

## Why chosen

Structured generation preserves useful language synthesis while deterministic validation/rendering owns factual and transport safety.

## Consequences

- Conservative lexical checks may reject good proposals; re-proposal is preferred over bypass.
- Template changes invalidate previews/approvals.
- V1 sends no attachment/link even when safe evidence is displayed.

## Revisit condition

Expand claim grammar/attachments only with deterministic validation, safe-view provenance, preview binding, and adversarial tests. Never return to free-form factual bodies.
