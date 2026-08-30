# Ambient CHORUS engineering source of truth

This directory is the implementation contract for Ambient CHORUS. Future engineers and coding agents must begin here.

## Authority and precedence

Use this precedence order:

1. Security invariants and frozen product decisions in this index and [01-principles-and-invariants.md](architecture/01-principles-and-invariants.md).
2. Accepted ADRs in [`docs/adr`](adr/). A newer accepted ADR overrides an older assumption; update the affected architecture documents in the same change.
3. Normative contracts in [`docs/architecture`](architecture/), read in the order below.
4. Implementation sequencing in [`docs/plans`](plans/).
5. Examples and diagrams, which illustrate but do not override normative prose.

Keywords **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative. If two authoritative documents disagree and no ADR resolves the conflict, stop implementation and repair the documentation first.

## Frozen decisions

- Three LLM agents: Monitor/Intake, Investigator/Skeptic, and Action Coordinator.
- Explicit application-code orchestration; no Strands Graph, swarm, or agents-as-tools control plane.
- Three distinct, tool-less AgentCore Runtime deployments with different IAM roles. Agent invocations are stateless and fully payload-driven.
- The Action runtime receives only an immutable `ShareableCaseView` and has zero database, S3, compiler, SES, and private-data access.
- Deterministic mandate/privacy compiler as a dedicated Lambda and the sole creator of safe views.
- Deterministic proposal validator, renderer, SES sender, and commitment watcher.
- Evidence corroboration is two independent sources; aggregate privacy is three distinct contributors. They are never interchangeable.
- Three DynamoDB tables (`core`, `shareable`, `audit`) and two S3 buckets (`private-evidence`, `export-evidence`).
- Python 3.12, FastAPI, Pydantic v2, Strands Agents, Amazon Bedrock Nova 2 Lite, AgentCore Runtime, Lambda, API Gateway HTTP API, DynamoDB, S3, SES, EventBridge Scheduler, CloudWatch, and CDK v2.
- React, TypeScript strict mode, Vite, TanStack Query, native `fetch`, CSS Modules, `npm`.
- Python dependencies and workspaces with `uv`; lint with Ruff, static typing with mypy, tests with pytest.
- One synthetic deterministic ambient adapter and one elevator scenario in V1.
- Three UI surfaces only: Ambient Signal Feed, Private Mandate Thread, and Case + Action.

Any change to a frozen decision requires an accepted ADR first.

## Architecture dependency order

1. [System overview](architecture/00-system-overview.md): scope, actors, flows, component boundaries, stack.
2. [Principles and invariants](architecture/01-principles-and-invariants.md): non-negotiable behavior and import rules.
3. [Trust, IAM, deployment, and configuration](architecture/02-trust-iam-deployment-configuration.md).
4. [Agent architecture](architecture/03-agent-architecture.md): exact inputs/outputs and orchestration.
5. [Domain, state, and events](architecture/04-domain-state-and-events.md).
6. [Privacy compiler and shareable view](architecture/05-privacy-compiler-and-shareable-view.md).
7. [Persistence and evidence storage](architecture/06-persistence-and-evidence.md).
8. [Action, SES, and commitments](architecture/07-action-ses-and-commitments.md).
9. [V1 API](architecture/08-api-design.md).
10. [Observability, errors, and failure semantics](architecture/09-observability-errors-and-failures.md).
11. [Security threat model](architecture/10-security-threat-model.md).
12. [Frontend and demo architecture](architecture/11-frontend-and-demo.md).
13. [Evaluation and testing](architecture/12-evaluation-and-testing.md).
14. [Repository and coding standards](architecture/13-repository-and-coding-standards.md).

## ADR index

| ADR | Decision |
|---|---|
| [ADR-001](adr/ADR-001-explicit-agent-orchestration.md) | Explicit hand-written orchestration |
| [ADR-002](adr/ADR-002-deterministic-privacy-compiler.md) | Deterministic privacy compiler boundary |
| [ADR-003](adr/ADR-003-action-runtime-isolation.md) | Isolated Action Agent runtime with no tools/data access |
| [ADR-004](adr/ADR-004-three-table-dynamodb.md) | Three-table DynamoDB persistence |
| [ADR-005](adr/ADR-005-synthetic-ambient-adapter.md) | Synthetic deterministic primary channel |
| [ADR-006](adr/ADR-006-eventbridge-commitment-scheduling.md) | EventBridge Scheduler and idempotent watcher |
| [ADR-007](adr/ADR-007-structured-claims-renderer.md) | Structured cited claims and deterministic rendering |
| [ADR-008](adr/ADR-008-aws-cdk.md) | AWS CDK v2 in Python |
| [ADR-009](adr/ADR-009-uv-and-npm.md) | `uv` and `npm` package management |
| [ADR-010](adr/ADR-010-agentcore-runtime.md) | AgentCore Runtime for all three agents |

## Implementation control

The detailed plan is [implementation-plan.md](plans/implementation-plan.md). [build-order.md](plans/build-order.md) is the dependency gate, [demo-plan.md](plans/demo-plan.md) is the five-minute live path, [risk-register.md](plans/risk-register.md) records residual risk, and [cut-list.md](plans/cut-list.md) prevents scope drift.

Architecture is ready only when the documents, ADRs, diagrams, failure matrix, and validation report are internally consistent. Application implementation must wait for explicit user approval.
