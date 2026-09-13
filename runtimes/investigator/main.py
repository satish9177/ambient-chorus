"""The Investigator artifact's direct-code entrypoint, unpacked to the archive root as ``main.py``.

AgentCore starts a direct-code runtime by running this file, so everything the deployed process
does begins here: make the archive's own ``src`` tree importable, build one ASGI application
around this runtime's ``handle``, and listen on the address the service reaches. Nothing else.
There is no configuration read, no client constructed, no state initialised, and no branch.

The bootstrap runs **before** the runtime imports, and that ordering is the whole point: the
archive preserves repo-relative paths, so ``chorus`` lives under ``src/`` and is unreachable
until that directory is on the path. Everything below the bootstrap therefore imports late, and
the linter is told so rather than the ordering being quietly rearranged.

The build copies this file to the root of the zip, which is why it uses absolute imports of
``runtimes.investigator`` -- the package ships beside it under the same root, exactly as it sits in
the repository.
"""

from __future__ import annotations

from pathlib import Path

from runtimes.bootstrap import ensure_shared_package_importable

ensure_shared_package_importable(Path(__file__).resolve().parent)

from runtimes.investigator.entrypoint import (  # noqa: E402 - imports chorus; needs the bootstrap above
    RuntimeBudgetExceededError,
    RuntimeContractError,
    handle,
)
from runtimes.server import (  # noqa: E402 - kept beside the import it pairs with
    AgentCoreServer,
    serve,
)

app = AgentCoreServer(
    handler=handle,
    contract_error=RuntimeContractError,
    budget_error=RuntimeBudgetExceededError,
)
"""This runtime's ASGI application, bound to its own handler and no other.

Built at import time so the artifact fails at start-up rather than on first invocation if the
binding is wrong, and exposed as a module attribute so a test can drive it in-process without
binding a socket.
"""


if __name__ == "__main__":  # pragma: no cover - exercised by the deployed process
    serve(app)
