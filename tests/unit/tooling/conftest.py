"""Shared build fixture for the Lambda-artifact release gate (review P2-5).

A clean checkout has no ``build/lambda/`` -- it is gitignored. Rather than let the packaging
and isolated-import tests silently ``pytest.skip`` (which is not a release gate), one
session-scoped fixture builds all five real ZIPs once, into a session temp directory, and the
tests that need a finished artifact depend on it. The result does not depend on any other test
having run first, and nothing is written into the repository.

Building resolves the locked wheel set with ``uv`` and takes a few minutes; the fixture is
lazy, so a targeted run that touches none of these tests pays nothing. CI runs the full suite
on a clean Linux runner, so this proof runs there for real.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tools import build_lambda_artifacts as build
from tools.build_runtime_artifacts import BuildError


@pytest.fixture(scope="session")
def built_lambda_artifacts(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Build all five Lambda ZIPs into a session temp directory; yield that directory.

    ``pytest.fail`` (never ``skip``) on a build error -- a release gate does not pass by not
    running.
    """

    output_root = tmp_path_factory.mktemp("lambda-artifacts")
    try:
        built = [
            build.build_lambda(directory, output_root=output_root)
            for directory in build.FUNCTION_DIRS
        ]
    except BuildError as error:  # pragma: no cover - exercised only on a real failure
        pytest.fail(f"Lambda artifact release build failed: {error}")
    build.write_manifest_file(built, output_root=output_root)
    yield output_root


@pytest.fixture(scope="session")
def built_artifact_paths(built_lambda_artifacts: Path) -> dict[str, Path]:
    """``function directory -> built ZIP path`` for the session's freshly built artifacts."""

    return {
        directory: built_lambda_artifacts / f"{build.load_lambda_manifest(directory).name}.zip"
        for directory in build.FUNCTION_DIRS
    }
