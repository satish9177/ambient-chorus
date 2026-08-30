# Scope and cut list

## Non-negotiable V1 spine

These cannot be cut without making the product claim/security architecture false:

- synthetic feed with unrelated noise and discovered recurring elevator case;
- all three named agents with strict structured contracts;
- deterministic skeptical ID/independence validation;
- per-contributor approve/adjust/refuse/revoke mandate versions;
- content/identity separation and 2-source vs 3-contributor thresholds;
- deterministic compiler, immutable safe view, hashes, exclusion audit;
- private-vs-shareable UI proof and prompt-injection secret absence;
- isolated tool-less Action runtime and cited claims;
- deterministic renderer, exact human approval, sender-only SES;
- freshness/revocation fence, idempotency, `SEND_UNKNOWN` no-retry behavior;
- external promise, real EventBridge schedule, idempotent watcher, human verification;
- `ACTIONED` not `RESOLVED` and missed-deadline outcome;
- three UI surfaces, reset, high-value tests/IAM canaries.

If the non-negotiable spine cannot pass the privacy gates, the honest outcome is “not ready,” not a narrower misleading success claim.

## Cut first if schedule slips

1. HTML email alternative; retain deterministic plain text and preview (requires small ADR/doc update because renderer/v1 currently binds both).
2. CloudWatch dashboard polish; retain structured logs, critical metrics/alarms, and audit API.
3. Animated feed timing/transitions; retain fixed ordered feed and live Monitor.
4. Safe photo external derivative display; keep photo private evidence/status and explicitly denied unless the demo claim requires export (update evaluation/demo wording). Never weaken sanitizer.
5. Audit drawer filtering/export; retain complete audit records and basic chronological display.
6. Responsive narrow-screen polish beyond accessible functional stacking.
7. Live configuration-set automatic reconciliation UI; retain `SEND_UNKNOWN`, no retry, and operator reconciliation procedure.
8. CloudFront hosting; run the same local web app against deployed API if hackathon rules allow, keeping API token/CORS.

Each cut must update docs/tests/status before presentation. Cutting a UI embellishment cannot cut the underlying security event/audit.

## Explicitly out of scope / do not start

- Slack, WhatsApp, email-feed, ticket-feed implementations;
- a second scenario/domain/community or broad multi-tenancy;
- Cognito or production RBAC/identity;
- AgentCore Memory, Gateway, Browser, Code Interpreter, MCP/A2A;
- Strands Graph, Workflow, Swarm, agents-as-tools, dynamic spawning;
- vector database, embeddings service, knowledge graph, Kafka, Step Functions, general event bus/platform;
- general policy DSL, consent/legal workflow, standing mandates;
- arbitrary file upload, broad OCR/malware/document pipeline;
- multiple recipients/destinations, CC/BCC, attachments, campaigns;
- autonomous sends/approvals/retries/resolution;
- voice/mobile/universal ingestion;
- admin/settings/analytics screens, realtime websockets;
- multi-region, HA, autoscaling work, production compliance certification;
- microservice split beyond security-principal Lambda/runtimes;
- generic repository/workflow/DI frameworks.

## Kill criteria

Remove/defer a planned optional item when it consumes more than half a day without directly advancing a phase exit criterion, introduces a new trust/data boundary, requires new credentials/external approval that is not already ready, or makes the five-minute critical path less deterministic. Stop the whole release/demo-send claim if any privacy/IAM/duplicate-send gate is nonzero, SES/AgentCore prerequisites are absent, or docs and behavior disagree materially.

## After V1

The first post-demo discovery work is user validation for one real channel adapter and production identity—not more agent autonomy. Any expansion begins with threat model/data-retention/ADR work and must preserve the compile boundary.
