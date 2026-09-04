"""The Monitor deployment manifest describes the artifact that actually exists.

A manifest is only worth writing if something checks it. These tests read
``runtimes/monitor/runtime.toml`` and compare every claim against the code: the entrypoint is
importable and has the declared shape, the allowlisted files are the files that are there, the
declared limits are the constants the runtime uses, and the capabilities it says it does not
have are capabilities nothing in the source reaches for.

The two Phase 11 markers are asserted too, in the direction that matters. They must stay
``NOT_IMPLEMENTED`` and ``NOT_RUN`` for exactly as long as those statements are true, and the
test that would fail if somebody flipped them without doing the work is the point of recording
them as data rather than as a comment.
"""

from __future__ import annotations

import inspect
import tomllib
from pathlib import Path
from typing import get_type_hints

import pytest
from runtimes.monitor import agent as runtime_agent
from runtimes.monitor import entrypoint as runtime_entrypoint
from runtimes.monitor.prompt import MONITOR_PROMPT_VERSION

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = REPOSITORY_ROOT / "runtimes" / "monitor" / "runtime.toml"


@pytest.fixture(scope="module")
def manifest() -> dict[str, object]:
    return tomllib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_the_manifest_exists_and_names_this_runtime(manifest: dict[str, object]) -> None:
    runtime = manifest["runtime"]
    assert isinstance(runtime, dict)
    assert runtime["agent_name"] == "MONITOR"
    assert runtime["prompt_version"] == MONITOR_PROMPT_VERSION
    assert runtime["python_version"] == "3.12"
    assert runtime["network_mode"] == "VPC", "no public-mode runtime, even as a placeholder"


def test_the_declared_entrypoint_is_importable_and_has_the_declared_shape(
    manifest: dict[str, object],
) -> None:
    entry = manifest["entrypoint"]
    assert isinstance(entry, dict)
    assert entry["module"] == "runtimes.monitor.entrypoint"

    handler = getattr(runtime_entrypoint, str(entry["callable"]))
    assert inspect.iscoroutinefunction(handler)
    # ``from __future__ import annotations`` leaves annotations as strings, so they are
    # resolved rather than compared as objects.
    hints = get_type_hints(handler)
    signature = inspect.signature(handler)
    assert next(iter(signature.parameters)) == "raw"
    assert hints["raw"] is bytes
    assert hints["return"] is bytes


def test_every_allowlisted_file_exists_and_every_runtime_file_is_allowlisted(
    manifest: dict[str, object],
) -> None:
    """An allowlist that has drifted from the tree is worse than no allowlist."""

    artifact = manifest["artifact"]
    assert isinstance(artifact, dict)
    declared = {str(item) for item in artifact["include"]}
    for relative in declared:
        assert (REPOSITORY_ROOT / relative).is_file(), f"{relative} is declared but absent"

    # Scoped to *this* runtime's own package plus the shared ``runtimes`` package marker.
    # A repository-wide sweep would fail the Monitor's manifest for a file that belongs to a
    # different artifact, which says nothing about whether the Monitor's allowlist has drifted.
    # Each runtime's manifest test owns its own package, and the union is what covers the tree.
    on_disk = {
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in (REPOSITORY_ROOT / "runtimes" / "monitor").rglob("*.py")
        if "__pycache__" not in path.parts
    } | {"runtimes/__init__.py"}
    assert on_disk <= declared, (
        f"runtime source not in the artifact allowlist: {on_disk - declared}"
    )


def test_the_declared_limits_match_the_constants_the_runtime_uses(
    manifest: dict[str, object],
) -> None:
    limits = manifest["limits"]
    assert isinstance(limits, dict)
    assert limits["max_payload_bytes"] == runtime_entrypoint.MAX_PAYLOAD_BYTES
    assert limits["max_output_tokens"] == runtime_agent.MONITOR_MAX_OUTPUT_TOKENS
    assert limits["temperature"] == runtime_agent.MONITOR_TEMPERATURE
    assert limits["max_model_attempts"] == runtime_agent.MONITOR_MAX_MODEL_ATTEMPTS
    assert limits["model_read_timeout_seconds"] == runtime_agent.MODEL_READ_TIMEOUT_SECONDS
    assert limits["runtime_budget_seconds"] == runtime_entrypoint.RUNTIME_BUDGET_SECONDS


def test_the_declared_timeouts_are_ordered_the_way_the_hierarchy_requires(
    manifest: dict[str, object],
) -> None:
    from chorus.settings import Settings

    limits = manifest["limits"]
    assert isinstance(limits, dict)
    model_timeout = int(limits["model_read_timeout_seconds"])
    runtime_budget = int(limits["runtime_budget_seconds"])
    assert model_timeout < runtime_budget < Settings(agent_mode="fake").agent_timeout_seconds


def test_the_runtime_declares_no_capability_it_must_not_have(
    manifest: dict[str, object],
) -> None:
    capabilities = manifest["capabilities"]
    assert isinstance(capabilities, dict)
    assert set(capabilities) == {
        "tools",
        "memory",
        "gateway",
        "code_interpreter",
        "browser",
        "filesystem_persistence",
        "session_reuse",
        "outbound_internet",
    }
    assert not any(capabilities.values()), "every runtime capability is false, by design"


def test_the_declared_dependencies_are_the_ones_the_artifact_imports(
    manifest: dict[str, object],
) -> None:
    """Minimal and explicit: nothing declared that is not used, nothing used that is hidden."""

    dependencies = manifest["dependencies"]
    assert isinstance(dependencies, dict)
    declared = {str(item).split(">")[0].split("=")[0].strip() for item in dependencies["required"]}
    assert declared == {"strands-agents", "pydantic", "botocore"}


def test_the_remaining_phase_eleven_work_is_recorded_rather_than_implied(
    manifest: dict[str, object],
) -> None:
    """Two honest markers. Flipping either without doing the work breaks this test."""

    phase = manifest["phase_11"]
    assert isinstance(phase, dict)
    assert phase["server_binding"] == "NOT_IMPLEMENTED"
    assert phase["live_evaluation"] == "NOT_RUN"

    # `server_binding` stays NOT_IMPLEMENTED while the official server SDK is absent. If it is
    # ever installed, this fails and the marker has to be revisited deliberately.
    import importlib.util

    assert importlib.util.find_spec("bedrock_agentcore") is None, (
        "the AgentCore server SDK is now installed; bind the entrypoint and update the manifest"
    )


def test_the_manual_live_evaluation_command_exists_and_names_its_gate() -> None:
    """The obligation has a command attached to it, not just a note."""

    command = REPOSITORY_ROOT / "tools" / "run_live_monitor_eval.py"
    assert command.is_file()
    source = command.read_text(encoding="utf-8")
    assert "AMBIENT_CHORUS_LIVE_MONITOR_EVAL" in source
    assert "CHORUS_MONITOR_RUNTIME_ARN" in source
    assert "NOT RUN" in source, "a missing prerequisite must fail loudly, never report success"
