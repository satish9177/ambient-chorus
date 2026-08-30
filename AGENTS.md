# Coding-agent instructions for Ambient CHORUS

## Mission and current phase

Ambient CHORUS discovers recurring community problems and produces an evidence-backed external action without disclosing anything a contributor did not authorize. The architecture is frozen. The repository is currently in the **design phase**; do not implement application code until the user explicitly approves implementation.

Read [docs/README.md](docs/README.md) before making changes. It is the source-of-truth index. Follow the authoritative documents in the dependency order listed there, then the accepted ADRs, then the implementation plans. If prose conflicts with an accepted ADR, the newer accepted ADR wins and the conflicting document must be corrected in the same change.

## Hard security invariants

These are release-blocking and must be enforced in types, deterministic code, IAM, and tests:

1. No `INTERNAL_ONLY` fact enters a `ShareableCaseView`.
2. The Action Agent receives only a current `ShareableCaseView`; it has no repository, DynamoDB, S3, compiler, SES, shell, browser, network, or general tool.
3. Only the deterministic privacy compiler creates a `ShareableCaseView`.
4. Only the deterministic sender calls SES.
5. Cross-case fact references deny the entire compile.
6. Expired, revoked, refused, unapproved, or stale mandates cannot authorize a new export.
7. Content permission and identity permission are independent.
8. `AGGREGATE_ONLY` output requires at least three distinct contributors; evidence corroboration separately requires at least two independent sources.
9. Every Action claim cites current exported fact IDs; every ID is in the matching view.
10. Execution requires a current view, matching proposal, matching one-use approval, and matching hashes.
11. A repeated execution request cannot send twice. `SEND_UNKNOWN` is never automatically retried.
12. Prompt or evidence text is untrusted data and never policy authority.
13. `ACTIONED` is not `RESOLVED`; only verification can resolve a case.

## Dependency rules

- `chorus.domain` depends only on the Python standard library.
- `chorus.privacy` may depend on domain types and canonicalization, never on FastAPI, AWS, Strands, or an LLM.
- `chorus.agent_contracts` contains Pydantic boundary DTOs and no persistence implementations.
- `chorus.application` coordinates ports and domain services; it contains no boto3 calls.
- `chorus.ports` contains narrow protocols; `chorus.infrastructure` implements them.
- API routes map transport DTOs to application commands and do not hold policy or persistence logic.
- Agent runtime entry points may import their own public contract package only. The Action runtime must not import private model or repository packages.
- AWS Lambda handlers are composition roots and adapters, not domain-service implementations.
- The web app consumes generated OpenAPI types and never redefines authoritative backend enums by hand.

Forbidden directions are documented in [docs/architecture/13-repository-and-coding-standards.md](docs/architecture/13-repository-and-coding-standards.md).

## Frozen architecture

Do not replace explicit orchestration with Strands Graph, agents-as-tools, a swarm, Step Functions, or an LLM-controlled router. Do not collapse the three AgentCore runtimes or grant an agent data-plane access. Do not merge the private/shareable DynamoDB tables or private/export S3 buckets. Do not add Cognito, a vector database, AgentCore Memory, a knowledge graph, Kafka, a general policy DSL, microservices, broad multi-tenancy, or extra UI surfaces.

Any architecture-changing implementation requires an accepted ADR and updates to all affected source-of-truth documents **before** application code changes. A change is architectural when it alters trust zones, permissions, persisted contracts, state transitions, hashing/canonicalization, agent inputs, external side effects, package boundaries, or frozen stack choices.

## Implementation and test discipline

Follow [docs/plans/build-order.md](docs/plans/build-order.md) and the active phase in [docs/plans/implementation-plan.md](docs/plans/implementation-plan.md). Implement the smallest vertical contract for the phase; do not pull future-phase scope forward.

- Preserve typed IDs, UTC timestamps, optimistic versions, idempotency keys, and structured errors end to end.
- Time, UUID generation, agent invocation, storage, and external side effects must be injected behind narrow ports in deterministic tests.
- Validate all LLM output with strict Pydantic models and deterministic semantic checks; schema-valid does not mean authorized or true.
- Prefer table-driven tests for compiler policy and state transitions. Add property tests only for high-value invariants.
- Never include raw messages, health details, apartment numbers, private URIs, email addresses, or agent prompt/completion bodies in normal logs.
- Never weaken an invariant to make a test or demo pass.

Expected validation after Phase 0 defines the commands:

```text
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest
npm --prefix apps/web run lint
npm --prefix apps/web run test
npm --prefix apps/web run build
npm exec cdk -- --app "uv run python -m infra.cdk.app" synth
```

If a command is not yet available in the current phase, say so; do not invent successful validation.

## Change hygiene

- Keep commits scoped to one implementation phase or coherent correction.
- Run the relevant validation before proposing a commit.
- Do not commit generated secrets, `.env`, resident data, private evidence, local outboxes, or CDK assets.
- Do not commit or amend unless explicitly asked. Never push automatically.
- Preserve user changes and avoid unrelated formatting churn.
- Update design docs, examples, tests, and ADR status together when a contract changes.

The risk register is [docs/plans/risk-register.md](docs/plans/risk-register.md), and the explicit non-goals/kill list is [docs/plans/cut-list.md](docs/plans/cut-list.md).
