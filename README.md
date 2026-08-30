# Ambient CHORUS

> One complaint is easy to ignore. Chorus finds the pattern.

**STATUS: Architecture / Design phase. No application functionality is implemented yet.**

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

Implementation begins only after explicit approval. The build sequence is in [docs/plans/implementation-plan.md](docs/plans/implementation-plan.md); local setup commands will be added in Phase 0 and are intentionally not fabricated here.

## Hackathon scope

CHORUS is designed as a credible, least-privilege hackathon build—not a production-ready resident communications platform. V1 has one community, one synthetic input adapter, one scenario, one external destination type, one human approver role, and exactly three UI surfaces. Production identity, broad multi-tenancy, Slack/WhatsApp integrations, general OCR, vector search, and workflow engines are outside the approved scope.
