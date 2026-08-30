# ADR-005: Synthetic deterministic ambient feed as V1 adapter

**Status:** Accepted
**Date:** 2026-08-30
**Deciders:** Ambient CHORUS maintainers and product owner

## Context

The hackathon must demonstrate background discovery among realistic noise without OAuth setup, platform policy risk, flaky external APIs, or nondeterministic message timing.

## Decision

Implement one `AmbientChannelPort` adapter: a synthetic deterministic community feed with 24 fixed messages, four residents, six elevator incidents, noise, photo, contradiction, and malicious evidence. Keep adapter IDs/idempotency contracts suitable for future Slack/email/ticket adapters, but do not implement them.

## Alternatives considered

- Slack: recognizable but requires OAuth, workspace/admin setup, event retries, and privacy review.
- WhatsApp: policy/API/template constraints and demo account setup.
- Shared email: possible but inbound receipt/DNS/parsing increases setup and spoofing surface.
- Hard-coded case/report IDs: reliable but fails to demonstrate discovery.

## Why chosen

It isolates the product differentiator—pattern discovery, mandates, skeptical investigation, and privacy compilation—and makes evaluation/replay exact.

## Consequences

- Integration credibility relies on the adapter contract/documentation, not a live community service.
- Seed data must remain synthetic and cannot encode expected agent output IDs.

## Revisit condition

After the demo, implement exactly one real adapter chosen from validated user demand. It must preserve source IDs, contributor mapping, replay safety, provenance, and untrusted-data treatment.
