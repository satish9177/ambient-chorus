# Repository structure, dependency rules, and coding standards

## Repository layout

```text
ambient-chorus/
├── AGENTS.md                        # mandatory instructions for coding agents
├── README.md
├── pyproject.toml                   # uv project, dependency groups, tool configuration
├── uv.lock
├── package.json                     # npm workspace command facade
├── package-lock.json
├── .env.example
├── compose.yaml                     # DynamoDB Local only
├── apps/
│   ├── api/chorus_api/              # FastAPI app: main.py, asgi.py, dependencies, problem details, routes/
│   └── web/                         # React + Vite SPA
│       ├── openapi/openapi.json     # committed OpenAPI export
│       ├── src/
│       │   ├── api/                 # generated schema.d.ts + the one hand-written fetch client
│       │   ├── pages/               # the three surfaces: feed, mandate thread, case + action
│       │   ├── components/          # feed, mandate, case, private, shareable, shared
│       │   ├── context/, hooks/, styles/
│       │   ├── app.tsx
│       │   └── main.tsx
│       └── tests/                   # Playwright smoke and hero-flow specs
├── src/chorus/
│   ├── domain/                      # standard-library entities, values, state machines, errors
│   ├── privacy/                     # pure policy/v1 compiler, transformations, canonicalization
│   ├── contracts/                   # agent boundary DTOs: monitor, investigation, action, commitment
│   ├── application/                 # commands, queries, services, operations
│   ├── ports/                       # narrow Protocols: repositories, agents, clock, storage, mail, scheduler
│   ├── infrastructure/              # adapters: dynamodb, s3, agentcore, ses, scheduler, secrets, local, ...
│   ├── composition/                 # local composition root, demo reset, CLI entry points
│   └── settings.py
├── runtimes/                        # AgentCore runtimes: monitor, investigator, action, shared server
├── functions/                       # Lambda entry points: api, worker, compiler, sender,
│                                    #   commitment_watcher, inbound_mail, demo_reset
├── infra/cdk/                       # CDK app, configuration, support modules, stacks/
├── demo/fixtures/elevator-v1/       # frozen synthetic corpus and evidence
├── tools/                           # artifact build/publish, link/license/secret checks, live evaluation
├── tests/
│   ├── unit/                        # domain, privacy, application, contracts, infra, tooling, ...
│   ├── property/                    # Hypothesis invariants
│   ├── contract/                    # cross-layer contracts, including DynamoDB Local persistence
│   ├── smoke/                       # local hero flow, reset, and negative paths
│   ├── repair/                      # regressions for independent review findings
│   ├── evaluation/                  # live model evaluations; skipped without a live binding
│   └── fixtures/
└── docs/                            # architecture, ADRs, deployment and demo documents
```

Directories exist only when they hold real code; empty pattern directories are not scaffolded.

## Python dependency boundaries

| Package | May import | Must not import |
|---|---|---|
| `domain` | standard library | Pydantic, FastAPI, boto3, Strands, infrastructure/application |
| `privacy` | domain, privacy-local canonicalization | Pydantic boundary models only at adapter edge; AWS, FastAPI, Strands, persistence |
| `contracts` | Pydantic, domain enums/ID serialization; `contracts.action` is self-contained public-safe primitives | infrastructure, repositories, application |
| `application` | domain, privacy, contracts, ports | boto3, FastAPI, concrete infrastructure, runtime entrypoints |
| `ports` | domain/contract types, `typing.Protocol` | concrete SDKs |
| `infrastructure` | ports/domain/contracts, external SDKs | FastAPI routes, agent prompts |
| API | application/query DTOs, composition dependencies | policy logic, raw boto3 |
| Lambda entrypoints | selected application/infrastructure modules | business rules in handlers |
| Action runtime | `contracts.action`, Strands runtime adapter | `domain` private entities, monitor/investigation contracts, application, ports, infrastructure, boto3 |

`import-linter` encodes these contracts. The Action direct-code zip build uses an explicit file allowlist and then scans artifact contents and Python import AST for `boto3`, `botocore`, `chorus.domain`, `chorus.application`, `chorus.infrastructure`, monitor/investigation contracts, database/S3/SES clients, shell/network tools, and prompt fixture secrets. A package-wide `chorus.contracts.__init__` must not re-export private contracts.

Composition root constructor injection is preferred over a DI framework. Ports are use-case-specific (`LoadCurrentCase`, `StoreShareableView`, `InvokeActionAgent`) rather than a generic repository/service locator. Add an abstraction only when it enforces a boundary, enables deterministic tests, or has two real adapters.

## Python standards

- Python 3.12, `from __future__ import annotations`, Ruff format at 100 columns, Ruff selected correctness/security/import rules, strict mypy with Pydantic plugin where required.
- Domain entities/value objects: frozen, slotted dataclasses; no behaviorless ORM models.
- Transport/agent/config/persistence DTOs: Pydantic v2 with `ConfigDict(extra='forbid', strict=True)` and bounded fields. Convert at boundaries; do not pass API models through the domain.
- No `dict[str, Any]` metadata bags. Use closed discriminated unions and explicit versioned schemas. `Any` requires a boundary-local comment and validation.
- Exceptions are typed, safe, and carry enum code/refs—not raw content. Domain/application never raises `HTTPException` or SDK exceptions.
- Use structured logger event functions with allowlisted fields; no f-string logging of objects. Sensitive wrappers redact `repr`/serialization by default.
- Application use cases are `async`. Blocking boto3/SDK calls are contained in infrastructure adapters and run through bounded `anyio.to_thread.run_sync`; no hidden thread spawning in domain/privacy.
- Read the injected `Clock` once per command. All domain times are aware UTC; local timezone conversion occurs only in adapters/UI.
- IDs come from injected `IdGenerator`; production UUIDv4, demo UUIDv5. Do not use random/module globals in tests.
- RFC 8785 and SHA-256 live in one canonicalization module with golden vectors. No ad hoc `json.dumps(sort_keys=True)` for authorization hashes.
- Repository methods accept a typed scope and expected version. No scan/list-all API, no save of generic entity, no last-write-wins.
- Transactions and external side effects are named methods with documented idempotency. Never wrap SES or Scheduler in a generic retry decorator.
- Use enums/literals for reason/state/scope/purpose. `match` statements over closed enums include an unreachable assertion so new variants fail tests/type checks.
- Comments explain why/security assumptions; docstrings are required on public contracts and non-obvious transformations, not every trivial function.

Test naming is `test_<unit>_<condition>_<outcome>`. Arrange/act/assert should be visually clear without comments. Time, UUID, model, storage, mail, and schedule dependencies are fakeable. Do not mock pure domain functions.

## TypeScript and React standards

- TypeScript `strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `noImplicitOverride`, and `useUnknownInCatchVariables`; no `any`, enums duplicated by hand, non-null assertion without a guard, or `@ts-ignore`.
- OpenAPI generates transport types; a thin native-fetch client handles token, actor, correlation, Problem Details, and abort signal. UI adapters convert generated transport shapes to display models.
- TanStack Query is the only server cache. No copied server state in context/local storage. Mutations have stable keys and explicit invalidation.
- Components are presentational unless they own a surface/query/form. Keep policy, hashes, and case transition calculations on the server.
- Forms submit server versions/hashes and display field/typed errors. Never use `dangerouslySetInnerHTML`; deterministic HTML email preview is rendered in a sandboxed/text representation, not injected.
- CSS Modules, semantic HTML, accessible labels, keyboard/focus, reduced motion, AA contrast. Tests query accessible roles/text rather than class names.
- Vitest/Testing Library for components/query states; Playwright for the three-surface journey. Snapshot only small stable presentational fragments, not whole pages.

## Dependency and package management

`uv` owns the Python lock and workspace. Dependency groups: `dev`, `test`, `agents`, `infra`; runtime packages declare the minimal subset used in their deployment artifact. Exact resolved versions live in `uv.lock`, not copied into docs. Direct dependencies are pinned to compatible ranges, and their licenses are checked by `tools/check_license.py`.

`npm` workspaces own `apps/web` and the root `package-lock.json`. CI uses `npm ci`, never mixed package managers. Dependabot/Renovate is optional after the demo; dependency updates are separate, tested changes.

No runtime dependency is added for a helper under roughly 30 lines unless security/standards correctness favors a maintained library (RFC 8785 is such a case). No dynamic agent tool packages are installed.

## Configuration, migrations, and compatibility

Settings follow [02-trust-iam-deployment-configuration.md](02-trust-iam-deployment-configuration.md). Persisted/input/output records have explicit schema versions. V1 readers accept only their current major version and known backward-compatible minor forms. Writes always use current. Schema migration is additive/read-old-write-new; no destructive in-place migration under the demo deadline.

OpenAPI, prompt, policy, compiler, renderer, fixture, and CDK build versions are independently identifiable. A changed authorization schema/policy/hash requires ADR and new major/minor contract as appropriate.

## Commit/review discipline

- One coherent change per commit after explicit user permission; no automatic commit or push.
- Generated OpenAPI types and locks are committed with the source change that generated them.
- Security-sensitive changes require doc/ADR/test updates in the same review.
- `git diff --check`, relevant validation, secret scan, and no private fixture leakage before commit.
- Do not claim implemented/deployed/tested functionality without command evidence.
