"""``chorus-api serve``: run the local composition under uvicorn.

[11-frontend-and-demo.md § The local composition
root](../../../docs/architecture/11-frontend-and-demo.md#the-local-composition-root):

    uv run chorus-api serve --port 8080    uvicorn over that factory
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chorus-api")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the local ASGI app under uvicorn.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--reload", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("chorus_api.asgi:app", host=args.host, port=args.port, reload=args.reload)
        return 0

    parser.error(f"unknown command: {args.command}")  # pragma: no cover - argparse exits first
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
