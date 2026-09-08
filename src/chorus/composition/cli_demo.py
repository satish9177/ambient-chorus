"""``chorus-demo reset``: the one-command local reset, against the running API.

[11-frontend-and-demo.md § One-command
reset](../../../docs/architecture/11-frontend-and-demo.md#one-command-reset):

    uv run chorus-demo reset --namespace DEMO --confirm "RESET DEMO" --seed elevator/v1

``chorus-demo reset`` calls ``POST /v1/demo/reset`` on the *running* local API -- the same
protected route a browser calls -- rather than building a throwaway in-memory composition of
its own. A reset that seeded a container nobody is serving from left the developer's actual
server untouched, which is the defect this command exists to not have.

The API base URL defaults to the local dev server (:data:`DEFAULT_API_BASE_URL`) and can be
overridden with ``--api-base-url`` or the ``CHORUS_API_BASE_URL`` environment variable. The
presenter actor header the route requires is sent automatically, the ``Idempotency-Key`` is
propagated (a fresh one is minted per invocation when not given), and a Problem Details
response is printed verbatim to stderr with a non-zero exit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlparse

DEFAULT_API_BASE_URL = "http://127.0.0.1:8080"
"""Where ``chorus-api serve`` listens by default. A localhost dev address, never a deployed one."""

RESET_PATH = "/v1/demo/reset"
PRESENTER_ACTOR = "presenter_admin"
ACTOR_HEADER = "X-Chorus-Demo-Actor"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chorus-demo")
    subparsers = parser.add_subparsers(dest="command", required=True)

    reset = subparsers.add_parser(
        "reset", help="Reset the running local demo namespace via the API."
    )
    reset.add_argument("--namespace", required=True)
    reset.add_argument("--confirm", required=True)
    reset.add_argument("--seed", dest="seed_version", required=True)
    reset.add_argument(
        "--api-base-url",
        default=os.environ.get("CHORUS_API_BASE_URL", DEFAULT_API_BASE_URL),
        help=f"Base URL of the running local API (default: {DEFAULT_API_BASE_URL}).",
    )
    reset.add_argument(
        "--idempotency-key",
        default=None,
        help="Reuse a specific Idempotency-Key; a fresh one is minted per run otherwise.",
    )

    args = parser.parse_args(argv)

    if args.command == "reset":
        return _reset(
            base_url=args.api_base_url,
            namespace=args.namespace,
            confirm=args.confirm,
            seed_version=args.seed_version,
            idempotency_key=args.idempotency_key,
        )

    parser.error(f"unknown command: {args.command}")  # pragma: no cover - argparse exits first
    return 2


def _reset(
    *,
    base_url: str,
    namespace: str,
    confirm: str,
    seed_version: str,
    idempotency_key: str | None,
) -> int:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        print(f"invalid --api-base-url: {base_url!r}", file=sys.stderr)
        return 2

    body = json.dumps(
        {"namespace": namespace, "confirm": confirm, "seed_version": seed_version}
    ).encode("utf-8")
    key = idempotency_key or f"chorus-demo-reset-{uuid.uuid4()}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        ACTOR_HEADER: PRESENTER_ACTOR,
        "Idempotency-Key": key,
    }

    connection_class = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection = connection_class(parsed.hostname, port, timeout=10)
    try:
        connection.request("POST", RESET_PATH, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        payload = response.read().decode("utf-8", "replace")
    except OSError as error:
        print(
            f"could not reach the local API at {base_url} ({error}); "
            "is `chorus-api serve` running?",
            file=sys.stderr,
        )
        return 1
    finally:
        connection.close()

    if status != 200:
        print(f"reset refused: HTTP {status}\n{payload}", file=sys.stderr)
        return 1

    print(json.dumps(json.loads(payload), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
