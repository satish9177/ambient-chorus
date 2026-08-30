# ADR-009: `uv` for Python and `npm` for frontend packages

**Status:** Accepted
**Date:** 2026-08-30
**Deciders:** Ambient CHORUS maintainers and product owner

## Context

The monorepo needs fast reproducible Python environments for app/runtimes/CDK/tests and a familiar locked Vite/React toolchain. Mixing managers increases CI/developer drift.

## Decision

Use a `uv` workspace with one `pyproject.toml` and `uv.lock`; groups separate runtime/dev/test/infra dependencies. Use npm workspaces with one root `package-lock.json`, `npm ci`, and `apps/web`. Do not use Poetry, pip-tools/plain requirements as source of truth, pnpm, or yarn.

## Alternatives considered

- Poetry: capable packaging but slower/heavier for this workspace.
- pip-tools/plain pip: simple but less integrated environment/workspace/script handling.
- pnpm: efficient, but npm is sufficient for one web package and universally available.
- yarn: no specific benefit here.

## Why chosen

`uv` gives fast deterministic resolution and commands; npm minimizes frontend setup decisions. Each ecosystem has one lock owner.

## Consequences

- Agent deployment artifacts must install/export only their minimal uv package set.
- Lock changes are reviewed and committed with dependency changes.
- CI uses `uv sync --frozen` and `npm ci`.

## Revisit condition

Only if a chosen deployment platform cannot consume uv-produced artifacts or the frontend becomes a multi-package workspace whose measured performance warrants pnpm. A manager change is repository-wide and requires lock/CI migration.
