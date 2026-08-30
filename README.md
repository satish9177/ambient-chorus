# Ambient CHORUS

> One complaint is easy to ignore. Chorus finds the pattern.

**STATUS: Phase 0–1 deterministic foundation implemented; later phases are intentionally
deferred.**

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

The planned AWS stack is Python 3.12, FastAPI, Pydantic v2, Strands Agents, Amazon Bedrock with Nova 2 Lite, Bedrock AgentCore Runtime, DynamoDB, S3, SES, EventBridge Scheduler, Lambda, CloudWatch, React, TypeScript, Vite, AWS CDK v2, `uv`, and `npm`.

## Engineering source of truth

Start with [docs/README.md](docs/README.md). It defines the document precedence, frozen decisions, reading order, ADR index, and implementation-plan entry point. [AGENTS.md](AGENTS.md) contains mandatory instructions for coding agents.

The build sequence is in
[docs/plans/implementation-plan.md](docs/plans/implementation-plan.md). Phase 0 and Phase 1
were explicitly approved; implementation must stop before Phase 2 until separately approved.

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
