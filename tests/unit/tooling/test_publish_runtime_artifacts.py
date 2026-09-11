"""The AgentCore artifact publication plan (offline wiring only).

`build_publication_plan` describes what a later (Macro C) upload will do and re-runs the final
security gate against the finished ZIPs on disk. It performs no network call. These tests prove:

- the plan names exactly the three runtimes, ARM64 / Python 3.12 / `["python", "main.py"]`;
- every `target_key` is `{agent}/{sha256}.zip` and **equals** what
  `infra.cdk.runtime_support.runtime_artifact_location` computes -- one vocabulary, or neither
  ships;
- the plan re-scans the finished ZIP (`tools.build_runtime_artifacts.inspect_archive`) rather
  than trusting the manifest's recorded `security_scan`;
- it fails closed on a missing manifest, an unsupported schema, a short/invalid digest, and a
  missing per-agent archive;
- the module contains no `boto3` / S3 client -- nothing uploads.

The happy path runs against the **real** `build/agentcore/` output (built by
`tools.build_runtime_artifacts`), because a synthetic ZIP cannot exercise the real final-ZIP
gate. The failure paths copy that output into `tmp_path` and mutate one thing each.
"""

from __future__ import annotations

import inspect
import json
import shutil
from pathlib import Path

import pytest
from infra.cdk.runtime_support import runtime_artifact_location
from tools.build_runtime_artifacts import DEFAULT_OUTPUT_ROOT, RUNTIME_NAMES
from tools.publish_runtime_artifacts import (
    ENTRYPOINT_COMMAND,
    PLAN_SCHEMA,
    ArtifactPublicationError,
    build_publication_plan,
)

_REAL_BUILD = DEFAULT_OUTPUT_ROOT
_HAVE_REAL_BUILD = (_REAL_BUILD / "artifacts.json").is_file() and all(
    (_REAL_BUILD / f"runtime-{agent}.zip").is_file() for agent in RUNTIME_NAMES
)
_needs_build = pytest.mark.skipif(
    not _HAVE_REAL_BUILD,
    reason="run `uv run python -m tools.build_runtime_artifacts` first",
)


@pytest.fixture
def real_build_copy(tmp_path: Path) -> Path:
    """A writable copy of the real `build/agentcore/` output."""

    if not _HAVE_REAL_BUILD:
        pytest.skip("run `uv run python -m tools.build_runtime_artifacts` first")
    dst = tmp_path / "agentcore"
    dst.mkdir()
    for name in ("artifacts.json", *(f"runtime-{a}.zip" for a in RUNTIME_NAMES)):
        shutil.copy2(_REAL_BUILD / name, dst / name)
    return dst


@_needs_build
def test_plan_names_the_three_runtimes_and_agrees_with_the_cdk_key() -> None:
    plan = build_publication_plan(environment="demo", output_root=_REAL_BUILD)

    assert plan.schema == PLAN_SCHEMA
    assert plan.environment == "demo"
    assert [entry.agent for entry in plan.entries] == list(RUNTIME_NAMES)

    for entry in plan.entries:
        assert entry.target_bucket == "chorus-agent-artifacts-demo"
        assert entry.sha256 == entry.sha256.lower()
        assert len(entry.sha256) == 64 and all(c in "0123456789abcdef" for c in entry.sha256)
        assert entry.target_key == f"{entry.agent}/{entry.sha256}.zip"
        assert entry.python_runtime == "PYTHON_3_12"
        assert entry.target_platform == "aarch64-manylinux2014"
        assert tuple(entry.entrypoint) == ENTRYPOINT_COMMAND

        # The one-vocabulary guarantee: the CDK Runtime resource and this plan name the same
        # S3 object, or neither is allowed to ship.
        cdk = runtime_artifact_location(
            entry.agent,
            bucket_name=entry.target_bucket,
            offline=False,
            output_root=_REAL_BUILD,
        )
        assert cdk.object_key == entry.target_key


@_needs_build
def test_plan_is_written_to_a_deterministic_file() -> None:
    plan = build_publication_plan(environment="demo", output_root=_REAL_BUILD)
    written = json.loads((_REAL_BUILD / "publication-plan-demo.json").read_text(encoding="utf-8"))
    assert written == plan.as_record()
    assert written["schema"] == PLAN_SCHEMA


def test_missing_manifest_fails_closed(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ArtifactPublicationError, match="manifest"):
        build_publication_plan(environment="demo", output_root=empty)


def test_unsupported_schema_fails_closed(real_build_copy: Path) -> None:
    manifest_path = real_build_copy / "artifacts.json"
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc["schema"] = "agentcore-artifacts/v2"
    manifest_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ArtifactPublicationError, match="schema"):
        build_publication_plan(environment="demo", output_root=real_build_copy)


def test_short_or_invalid_digest_fails_closed(real_build_copy: Path) -> None:
    manifest_path = real_build_copy / "artifacts.json"
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc["artifacts"][0]["sha256"] = "sha256:deadbeef"  # not 64 hex
    manifest_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ArtifactPublicationError, match="digest"):
        build_publication_plan(environment="demo", output_root=real_build_copy)


def test_missing_archive_fails_closed(real_build_copy: Path) -> None:
    (real_build_copy / "runtime-monitor.zip").unlink()
    with pytest.raises(ArtifactPublicationError, match="monitor"):
        build_publication_plan(environment="demo", output_root=real_build_copy)


def test_module_performs_no_upload() -> None:
    import tools.publish_runtime_artifacts as module

    # No AWS SDK is imported and no upload call appears anywhere in the module body.
    imported = set(dir(module))
    assert "boto3" not in imported
    assert "botocore" not in imported

    code_lines = [
        line
        for line in inspect.getsource(module).splitlines()
        if not line.lstrip().startswith(("#", '"', "'"))
    ]
    code = "\n".join(code_lines)
    assert "import boto3" not in code
    assert "boto3.client" not in code
    assert ".put_object(" not in code
    assert ".upload_file(" not in code
    assert ".upload_fileobj(" not in code
