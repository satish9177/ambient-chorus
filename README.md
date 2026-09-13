# Ambient CHORUS

> One complaint is easy to ignore. Chorus finds the pattern.

**STATUS: Phases 0–11 implemented — ambient intake through AWS deployment.** Seven of eleven
AWS CDK stacks are independently deployed and verified live in `us-east-1`: Foundation, Network,
Data, Reset, Compiler, Sender, and Watcher, including a verified SES sending identity, a live
`SendEmail` canary, and confirmed delivery to a real Gmail inbox. The three Bedrock AgentCore
agent runtimes (Monitor, Investigator, Action) are code-complete and validated under offline CDK
synthesis; live agent execution is currently blocked by AWS account provisioning (AgentCore
quota and Bedrock Nova 2 Lite model access), tracked in an open, escalated AWS Support case. The
Application, Inbound, and Observability stacks are built but not yet independently reverified
live. See [phase-11-deployment-contract.md](docs/plans/phase-11-deployment-contract.md) for the
deployment architecture and [phase-13-submission-kit.md](docs/plans/phase-13-submission-kit.md)
for the current live/blocked breakdown and demo plan.

Ambient CHORUS is a background community investigator for the AWS Agents for Humans Hackathon, Good Neighbor Agents track. It watches a channel a community already uses, recognizes when independent fragments point to the same unresolved problem, asks affected people privately what may be shared, and turns only authorized facts into one evidence-backed action.

The V1 demonstration is deliberately narrow: four residents, six apartment-elevator incidents, unrelated building chatter, a contradictory management statement, a repair promise, and a missed deadline.

## The architecture in one sentence

Private messages become typed facts and immutable disclosure mandates; a deterministic privacy compiler creates an external-safe `ShareableCaseView`; only that view enters the Action Agent trust zone.

This is **compile, don't filter**. CHORUS does not give an LLM private data and hope a redaction prompt works. The model cannot leak what the architecture never gives it.

## Three agents, two deterministic safety systems

- **Monitor / Intake Agent:** extracts facts and suggests recurring issues and mandate requests. It cannot grant disclosure.
- **Investigator / Skeptic Agent:** tests whether reports are truly related, distinguishes independent from duplicated evidence, surfaces contradictions, and classifies evidence.
- **Action Coordinator Agent:** receives only an immutable safe view and proposes structured, cited claims. It cannot read databases or private S3, compile a view, or call SES.
- **Mandate / Privacy Compiler:** deterministic, fail-closed authorization code that is the only creator of `ShareableCaseView`.
- **Commitment Watcher:** deterministic scheduling and replay-safe deadline verification. `ACTIONED` never means `RESOLVED`.

The AWS stack is Python 3.12, FastAPI, Pydantic v2, Strands Agents, Amazon Bedrock with Nova 2 Lite, Bedrock AgentCore Runtime, DynamoDB, S3, SES, EventBridge Scheduler, Lambda, CloudWatch, React, TypeScript, Vite, AWS CDK v2, `uv`, and `npm`. Nova 2 Lite and AgentCore are code-complete but not yet live — see the status note above.

## Judge Quickstart (local, no AWS)

This runs the checked-in local composition
([`src/chorus/composition/local.py`](src/chorus/composition/local.py)) — the same production
application and privacy-compiler code as the deployed stack, wired to local/in-memory adapters
instead of AWS. It is a separate build from the seven CDK stacks independently deployed and
verified live in `us-east-1` described in the status note above; this quickstart never calls
that deployment and needs no AWS account.

**No AWS credentials. No AgentCore. No Bedrock Nova.** The Monitor, Investigator, and Action
agents run as deterministic, fixture-driven fake adapters shipped in
[`src/chorus/infrastructure/local/`](src/chorus/infrastructure/local/) (`LexicalFakeMonitorAgent`,
`CautiousFakeInvestigatorAgent`, `CautiousFakeActionAgent`, `LiteralSpanCommitmentExtractor`) —
not live models, and not to be described as one. Storage is in-memory, the "sender" writes to a
local filesystem outbox instead of calling SES, and the scheduler/clock are in-process stand-ins.

Prerequisites: Python 3.12, `uv`, Node.js, npm. No Docker, no AWS account, no `.env`.

```text
uv sync --frozen
npm ci
uv run chorus-api serve --port 8080            # terminal A — leave running
npm run dev --workspace @ambient-chorus/web    # terminal B — leave running
```

Open the URL Vite prints (default `http://127.0.0.1:5173`) and click through in order:

1. **Reset demo** — seeds the frozen `elevator/v1` fixture corpus (24 messages, 4 residents).
2. **Detect pattern** — runs the fake Monitor agent; open the case it surfaces.
3. **Propose mandates**, then use the persona selector to step through each resident and decide
   their own mandate (Resident B intentionally narrows one fact to `INTERNAL_ONLY`).
4. **Run investigation** — the fake Investigator agent flags the contradiction; the private panel
   still legitimately shows the sentinel private fact.
5. **Compile shareable view** — the deterministic privacy compiler excludes the private facts;
   the shareable panel does not contain them.
6. **Propose action**, switch persona to `case_approver`, **Approve**, then **Execute / send** —
   this writes to the local outbox file, not SES.
7. **Deliver reply** (a fixture manager reply) → a commitment appears `PENDING`; **Advance clock
   past due date** → `DUE`; switch to the affected resident and mark **Missed**.
8. The case returns to **Ready for action**, not **Resolved** — `ACTIONED != RESOLVED` is the
   point of the demo.

This is the same sequence exercised automatically by
[`apps/web/tests/hero.spec.ts`](apps/web/tests/hero.spec.ts) (UI) and
[`tests/smoke/test_local_hero_flow.py`](tests/smoke/test_local_hero_flow.py) (backend-only);
running either is optional verification, not a substitute for watching the flow in a browser.

## Engineering source of truth

Start with [docs/README.md](docs/README.md). It defines the document precedence, frozen decisions, reading order, ADR index, and implementation-plan entry point. [AGENTS.md](AGENTS.md) contains mandatory instructions for coding agents.

The build sequence is in
[docs/plans/implementation-plan.md](docs/plans/implementation-plan.md). Phases 0 through 11 have
been implemented and approved in sequence; Phase 13 (hackathon submission) is in progress. Any
further architectural change still requires an accepted ADR first.

## Developer commands

Prerequisites are Python 3.12, `uv`, Node.js, npm, and Docker only when DynamoDB Local is
needed. Install and validate the current foundation from the repository root:

```text
uv sync --frozen
npm ci
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests infra tools
uv run pytest
uv run lint-imports
uv run python tools/check_architecture_links.py
uv run python tools/check_license.py
uv run python tools/check_secrets.py
uv run pip-licenses --from=mixed --format=plain
npm run lint
npm run typecheck
npm test
npm run build
npm run e2e:list
npm exec cdk -- --app "uv run python -m infra.cdk.app" synth
```

The pure compiler uses the small `rfc8785` package for RFC 8785/JCS bytes; it does not
approximate canonical JSON with `json.dumps`. The Python and npm locks are the authoritative
resolved-version and transitive-license inputs.

## Hackathon scope

CHORUS is designed as a credible, least-privilege hackathon build—not a production-ready resident communications platform. V1 has one community, one synthetic input adapter, one scenario, one external destination type, one human approver role, and exactly three UI surfaces. Production identity, broad multi-tenancy, Slack/WhatsApp integrations, general OCR, vector search, and workflow engines are outside the approved scope.
