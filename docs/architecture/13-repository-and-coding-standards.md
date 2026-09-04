# Repository structure, dependency rules, and coding standards

## Final repository layout

```text
ambient-chorus/
├── AGENTS.md
├── README.md
├── pyproject.toml                 # uv workspace/tool config
├── uv.lock
├── package.json                   # npm workspace command facade
├── package-lock.json
├── .env.example
├── compose.yaml                   # DynamoDB Local only
├── apps/
│   ├── api/
│   │   └── chorus_api/
│   │       ├── main.py            # FastAPI composition root
│   │       ├── dependencies.py
│   │       ├── problem_details.py
│   │       └── routes/             # feed, cases, mandates, actions, demo, operations
│   └── web/
│       ├── package.json
│       ├── vite.config.ts
│       └── src/
│           ├── api/                # generated schema + hand-written fetch wrapper
│           ├── components/
│           ├── surfaces/           # ambient, mandate, case-action only
│           ├── styles/
│           ├── app.tsx
│           └── main.tsx
├── src/chorus/
│   ├── domain/                     # standard-library entities, values, state machines, errors
│   ├── privacy/                    # pure policy/v1 compiler, transformations, canonicalization
│   ├── contracts/
│   │   ├── common.py
│   │   ├── monitor.py              # private runtime contract
│   │   ├── investigation.py        # private runtime contract
│   │   └── action.py               # safe view/proposal contract only
│   ├── application/
│   │   ├── commands/
│   │   ├── queries/
│   │   ├── services/
│   │   └── operations.py
│   ├── ports/                       # narrow Protocols: repositories, agents, clock, IDs, storage, mail, scheduler
│   └── infrastructure/
│       ├── dynamodb/
│       ├── s3/
│       ├── agentcore/
│       ├── ses/
│       ├── scheduler/
│       ├── observability/
│       └── local/
├── runtimes/
│   ├── monitor/                     # entrypoint + monitor/v3 prompt + deployment manifest
│   ├── investigator/                # entrypoint + investigator/v1 prompt
│   └── action/                      # entrypoint + action/v1 prompt; allowlisted artifact build
├── functions/
│   ├── worker/                      # async application commands
│   ├── compiler/                    # compiler/fence Lambda composition root
│   ├── sender/                      # deterministic renderer/SES/reconciliation
│   └── commitment_watcher/
├── infra/cdk/
│   ├── app.py
│   ├── config.py
│   └── stacks/                      # data, agents, compute, web, observability
├── demo/
│   ├── fixtures/elevator-v1/
│   ├── evaluation/elevator-v1/
│   └── README.md
├── tests/
│   ├── unit/{domain,privacy,application,contracts}/
│   ├── property/
│   ├── integration/{dynamodb,aws_adapters,local_flow}/
│   ├── iam/
│   ├── evaluation/
│   └── e2e/
└── docs/                            # current source of truth
```

Only directories with a concrete first implementation task are created in that phase. Empty pattern directories are not scaffolded early.

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

`uv` owns the Python lock and workspace. Dependency groups: `dev`, `test`, `infra`; runtime packages declare the minimal subset used in their deployment artifact. Exact resolved versions live in `uv.lock`, not copied into docs. Phase 0 pins direct dependency compatible ranges and records licenses.

`npm` workspaces own `apps/web` and the root `package-lock.json`. CI uses `npm ci`, never mixed package managers. Dependabot/Renovate is optional after the demo; dependency updates are separate, tested changes.

No runtime dependency is added for a helper under roughly 30 lines unless security/standards correctness favors a maintained library (RFC 8785 is such a case). No dynamic agent tool packages are installed.

## Configuration, migrations, and compatibility

Settings follow [02-trust-iam-deployment-configuration.md](02-trust-iam-deployment-configuration.md). Persisted/input/output records have explicit schema versions. V1 readers accept only their current major version and known backward-compatible minor forms. Writes always use current. Schema migration is additive/read-old-write-new; no destructive in-place migration under the demo deadline.

OpenAPI, prompt, policy, compiler, renderer, fixture, and CDK build versions are independently identifiable. A changed authorization schema/policy/hash requires ADR and new major/minor contract as appropriate.

## Commit/review discipline

- One coherent phase/slice per commit after explicit user permission; no automatic commit or push.
- Generated OpenAPI types and locks are committed with the source change that generated them.
- Security-sensitive changes require doc/ADR/test updates in the same review.
- `git diff --check`, relevant validation, secret scan, and no private fixture leakage before commit.
- Do not claim implemented/deployed/tested functionality without command evidence.
