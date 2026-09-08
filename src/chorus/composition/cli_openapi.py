"""``chorus-openapi export``: the deterministic OpenAPI artifact.

[11-frontend-and-demo.md § Generated API types, never hand-written
ones](../../../docs/architecture/11-frontend-and-demo.md#generated-api-types-never-hand-written-ones):

    uv run chorus-openapi export --out apps/web/openapi/openapi.json

It builds the app from the same local container the dev server uses and writes ``app.openapi()``
with sorted keys and a trailing newline. CI re-runs this and fails on any diff, so the export
must be a pure function of the route definitions -- nothing here reads the clock, a random
generator, or process state.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chorus_api.main import build_app

from chorus.composition.local import build_local_container
from chorus.settings import Environment, Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chorus-openapi")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export", help="Write the OpenAPI schema to a file.")
    export.add_argument("--out", required=True, type=Path)

    args = parser.parse_args(argv)

    if args.command == "export":
        return _export(args.out)

    parser.error(f"unknown command: {args.command}")  # pragma: no cover - argparse exits first
    return 2


def _export(out: Path) -> int:
    # The export environment is fixed to `test` rather than read from the process's own
    # environment: the artifact must be identical regardless of who runs the export or what
    # they happen to have set locally, and `test` is guaranteed to build without a demo
    # namespace side effect.
    container = build_local_container(Settings(environment=Environment.TEST))
    app = build_app(container)
    schema = app.openapi()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
