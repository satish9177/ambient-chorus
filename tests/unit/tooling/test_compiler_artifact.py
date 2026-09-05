"""What the compiler artifact is allowed to contain, checked by reading it.

The compiler is the one component whose answers must never become probabilistic, and the first
step toward that is an import. So this scans the artifact's own source for the modules and
clients it must not reach -- not the runtime behaviour, the *text*, because a dependency that
is only imported on some branch is still a dependency that shipped.

The scan reads the import AST rather than matching substrings: a comment mentioning Bedrock,
or a docstring explaining why SES is denied, is exactly the kind of thing this file is full of
and must not fail on.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ARTIFACT_ROOT = Path(__file__).resolve().parents[3] / "functions" / "compiler"

FORBIDDEN_MODULES = frozenset(
    {
        "strands",
        "chorus.contracts",
        "chorus_api",
        "runtimes",
    }
)
"""No model SDK, no agent contract, no transport, and no agent runtime.

``chorus.contracts`` is on the list for the same reason as ``strands``: the compiler evaluating
anything an agent produced, in any shape, would put a model back inside the decision.
"""

FORBIDDEN_CLIENT_CALLS = frozenset({"bedrock", "bedrock-runtime", "bedrock-agentcore", "ses"})
"""Service names no ``boto3.client`` call in this artifact may name."""


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
                assert argument.value not in FORBIDDEN_CLIENT_CALLS, (
                    f"{path.name} constructs a client for {argument.value}"
                )


def test_the_artifact_creates_no_deployed_resource() -> None:
    """Phase 6 owns the code and the identity; Phase 11 owns the deployed function."""

    for path in source_files():
        modules = imported_modules(path)
        assert not any(module.startswith("aws_cdk") for module in modules)


def test_the_artifact_reaches_the_privacy_compiler_and_nothing_that_decides_for_it() -> None:
    """It must import the compiler -- that is its whole job -- and no second policy source."""

    imported = {module for path in source_files() for module in imported_modules(path)}

    assert "chorus.privacy.compiler" in imported
    assert not any(
        module.startswith("chorus.privacy.") and module != "chorus.privacy.compiler"
        for module in imported
    ), "the composition root evaluates policy through the compiler alone"
