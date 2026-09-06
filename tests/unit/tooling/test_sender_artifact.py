"""What the sender artifact is allowed to contain, checked by reading it.

The sender is the one component whose output leaves the building, so what it can *reach* is the
whole of its trustworthiness. This scans its own source for the modules and clients it must not
touch -- not the runtime behaviour, the *text*, because a dependency that is only imported on
some branch is still a dependency that shipped.

The scan reads the import AST rather than matching substrings: this artifact's docstrings are
full of the words ``Bedrock``, ``Strands``, and ``scheduler``, explaining why each is denied,
and a substring scan would fail on the explanation rather than on the import.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ARTIFACT_ROOT = Path(__file__).resolve().parents[3] / "functions" / "sender"

FORBIDDEN_MODULES = frozenset(
    {
        "strands",
        "chorus.contracts",
        "chorus_api",
        "runtimes",
    }
)
"""No model SDK, no agent contract, no transport, and no agent runtime.

``chorus.contracts`` is on the list for the same reason as ``strands``: the sender is where an
already-approved message becomes bytes on the wire, and anything that could evaluate agent
output there would put a model between the approval and the send.
"""

FORBIDDEN_CLIENT_SERVICES = frozenset(
    {"bedrock", "bedrock-runtime", "bedrock-agentcore", "scheduler", "events"}
)
"""Service names no client call in this artifact may name.

``sesv2`` is deliberately absent, because constructing that client is the artifact's job. What
must not appear is a model, a scheduler, or an event bus -- the three ways a sender could
acquire an opinion, a timer, or a second trigger.
"""


def source_files() -> list[Path]:
    return sorted(path for path in ARTIFACT_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            found.add(node.module)
    return found


def test_the_artifact_has_source_files_to_scan() -> None:
    """A scan over an empty directory passes and proves nothing."""

    assert source_files()


@pytest.mark.parametrize("path", source_files(), ids=lambda path: path.name)
def test_no_forbidden_module_is_imported(path: Path) -> None:
    for module in imported_modules(path):
        for forbidden in FORBIDDEN_MODULES:
            assert module != forbidden and not module.startswith(f"{forbidden}."), (
                f"{path.name} imports {module}"
            )


@pytest.mark.parametrize("path", source_files(), ids=lambda path: path.name)
def test_no_client_is_constructed_for_a_denied_service(path: Path) -> None:
    """The IAM deny is defence in depth. This is the code not asking in the first place."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                assert argument.value not in FORBIDDEN_CLIENT_SERVICES, (
                    f"{path.name} constructs a client for {argument.value}"
                )


def test_the_artifact_creates_no_deployed_resource() -> None:
    """Phase 8 owns the code and the identity; Phase 11 owns the deployed function."""

    for path in source_files():
        assert not any(module.startswith("aws_cdk") for module in imported_modules(path)), (
            f"{path.name} imports CDK"
        )


def test_the_artifact_pins_the_sdk_to_one_attempt() -> None:
    """Without it botocore retries underneath the single deliberate attempt.

    The whole safety property is a statement about the number of *deliberate* attempts, so an
    SDK retry is a second attempt nothing records and nothing can see -- exactly the shape of
    duplication this phase exists to prevent. Asserted on the configuration object rather than
    on a string, so a change to the value fails here.
    """

    from functions.sender.composition import SINGLE_ATTEMPT_CLIENT_CONFIG

    assert SINGLE_ATTEMPT_CLIENT_CONFIG.retries == {
        "total_max_attempts": 1,
        "mode": "standard",
    }


def test_the_ses_transport_deadlines_are_bounded_by_the_fence_and_stated_not_defaulted() -> None:
    """Both timeouts are decisions, and the read one is bounded by the fence's maximum life.

    They decide which side of the ``FAILED``/``SEND_UNKNOWN`` boundary a slow network lands on,
    which makes them safety-relevant rather than performance tuning. The read timeout must not
    *exceed* ``SEND_FENCE_LIFETIME``, so an in-flight request cannot outlive the window that
    authorized it by more than the window itself; it must not be much shorter either, because a
    read timeout is classified ``SEND_UNKNOWN`` -- a quarantine no path may retry -- so an
    aggressive value turns slow successes into permanent uncertainty.
    """

    from functions.sender.composition import (
        SES_CONNECT_TIMEOUT_SECONDS,
        SINGLE_ATTEMPT_CLIENT_CONFIG,
    )

    from chorus.application.services.action_authorization import SEND_FENCE_LIFETIME

    fence_seconds = SEND_FENCE_LIFETIME.total_seconds()
    assert SINGLE_ATTEMPT_CLIENT_CONFIG.read_timeout == fence_seconds
    assert 0 < SES_CONNECT_TIMEOUT_SECONDS < fence_seconds
    assert SINGLE_ATTEMPT_CLIENT_CONFIG.connect_timeout == SES_CONNECT_TIMEOUT_SECONDS


def test_the_artifact_reaches_the_send_command_and_no_second_ordering_authority() -> None:
    """It must import the send use case -- that is its whole job -- and decide nothing itself."""

    imported = {module for path in source_files() for module in imported_modules(path)}

    assert "chorus.application.commands.send_action" in imported
    assert not any(
        module.startswith("chorus.application.commands.")
        and module != "chorus.application.commands.send_action"
        for module in imported
    ), "the composition root runs one command and composes no second one"


def test_no_address_shaped_string_appears_in_the_artifact() -> None:
    """The sender resolves addresses from a secret. None is written down in its source."""

    import re

    address = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    for path in source_files():
        assert not address.findall(path.read_text(encoding="utf-8")), (
            f"{path.name} contains an address-shaped string"
        )
