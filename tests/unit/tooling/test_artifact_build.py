"""What the artifact build promises, checked without downloading a dependency set.

Two halves. The **commands** are asserted argv by argv, with the subprocess call replaced, so
the lock flag, the target platform, the Python version, and the wheels-only flag are proved
rather than assumed -- and proving them costs nothing, which means it can happen on every run.
The **archive** is then built from a fake dependency install and read back: the entrypoint is at
the root, forbidden paths are absent, ordering is stable, and the digest is a function of the
contents rather than of the clock.

What is deliberately not here: an ARM64 wheel actually executing. That needs an ARM64 Linux
machine, and pretending otherwise would be the kind of check that passes everywhere and proves
nothing. The live cold start (canary J) is what settles it.
"""

from __future__ import annotations

import json
import tomllib
import zipfile
from pathlib import Path

import pytest
from tools import build_runtime_artifacts as build

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

EXPECTED_PLATFORM = "aarch64-manylinux2014"
EXPECTED_PYTHON = "3.12"

VENDORED_ONLY = frozenset({"main.py"})
"""Provenance for the synthetic archives below: the entrypoint is ours, the rest is vendored.

Stated rather than left to the strict default, because these tests are about the ELF header
of a compiled extension -- and a compiled extension is only legitimately undecodable when it
is vendored. A first-party file that could not be read as text is a finding of its own.
"""


@pytest.fixture(params=build.RUNTIME_NAMES)
def agent(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def manifest(agent: str) -> build.RuntimeManifest:
    return build.load_manifest(agent)


# -- the commands ---------------------------------------------------------------------------


def test_the_dependency_export_is_lock_based(manifest: build.RuntimeManifest) -> None:
    """``--frozen`` is what makes the artifact's dependency set a property of the lockfile."""

    command = build.export_command(Path("requirements.txt"))

    assert command[:2] == ["uv", "export"]
    assert "--frozen" in command
    assert command[command.index("--only-group") + 1] == "agents"
    assert "--no-emit-project" in command


def test_the_install_targets_linux_arm64_on_the_accepted_python(
    manifest: build.RuntimeManifest,
) -> None:
    """The build machine is Windows on x86-64. Every one of these flags is why that is fine."""

    command = build.install_command(
        manifest, requirements=Path("requirements.txt"), target=Path("stage")
    )

    assert command[:3] == ["uv", "pip", "install"]
    assert command[command.index("--python-platform") + 1] == EXPECTED_PLATFORM
    assert command[command.index("--python-version") + 1] == EXPECTED_PYTHON
    assert command[command.index("--only-binary") + 1] == ":all:"
    assert command[command.index("--target") + 1] == "stage"


def test_the_target_python_matches_the_runtime_the_manifest_declares(agent: str) -> None:
    """One version, stated in two places for two audiences, and asserted to agree.

    ``python_version`` is what the AgentCore runtime resource will declare; ``target_python`` is
    what the wheels are resolved for. A build that resolved 3.11 wheels for a 3.12 runtime would
    fail at import, so the two are checked against each other rather than trusted.
    """

    document = tomllib.loads(
        (REPOSITORY_ROOT / "runtimes" / agent / "runtime.toml").read_text(encoding="utf-8")
    )
    assert document["runtime"]["python_version"] == EXPECTED_PYTHON
    assert document["packaging"]["target_python"] == EXPECTED_PYTHON
    assert document["packaging"]["target_platform"] == EXPECTED_PLATFORM
    assert document["packaging"]["only_binary"] is True


def test_the_deployed_runtime_name_is_one_agentcore_will_accept(agent: str) -> None:
    """``^[a-zA-Z][a-zA-Z0-9_]{0,47}$``. The hyphenated name cannot be a runtime name."""

    import re

    manifest = build.load_manifest(agent)
    assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,47}", manifest.deployed_name)
    assert "-" in manifest.name, "the IAM role and log group keep their hyphens"


def test_no_manifest_still_declares_the_binding_as_missing(agent: str) -> None:
    document = tomllib.loads(
        (REPOSITORY_ROOT / "runtimes" / agent / "runtime.toml").read_text(encoding="utf-8")
    )
    assert document["phase_11"]["server_binding"] != "NOT_IMPLEMENTED"


# -- the archive ----------------------------------------------------------------------------


def _fake_install(target: Path) -> None:
    """Stand in for the dependency install with a package shaped like a real one."""

    package = target / "pydantic"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("VERSION = '2'\n", encoding="utf-8")
    cache = package / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "__init__.cpython-312.pyc").write_bytes(b"\x00compiled")
    metadata = target / "pydantic-2.13.5.dist-info"
    metadata.mkdir(exist_ok=True)
    (metadata / "METADATA").write_text("Name: pydantic\n", encoding="utf-8")
    # `uv pip install --target` resolves wheels for the declared platform but writes console
    # scripts for the build host, so a Windows build leaves `.exe` launchers here.
    scripts = target / "bin"
    scripts.mkdir(exist_ok=True)
    (scripts / "opentelemetry-instrument.exe").write_bytes(b"MZ windows launcher")
    (scripts / "uvicorn.exe").write_bytes(b"MZ windows launcher")


def _build(agent: str, output: Path) -> build.BuiltArtifact:
    commands: list[list[str]] = []

    def runner(command: list[str], cwd: Path) -> None:
        commands.append(command)
        if command[:3] == ["uv", "pip", "install"]:
            _fake_install(Path(command[command.index("--target") + 1]))

    artifact = build.build_runtime(agent, output_root=output, runner=runner)
    assert [command[1] for command in commands] == ["export", "pip"]
    return artifact


def test_the_archive_carries_the_entrypoint_at_its_root(agent: str, tmp_path: Path) -> None:
    """AgentCore runs ``main.py`` from the archive root, so that is where it must be."""

    artifact = _build(agent, tmp_path)

    with zipfile.ZipFile(tmp_path / artifact.archive) as archive:
        names = set(archive.namelist())
        entrypoint = archive.read("main.py").decode("utf-8")

    assert "main.py" in names
    assert artifact.entrypoint == "main.py"
    assert f"from runtimes.{agent}.entrypoint import" in entrypoint
    assert "AgentCoreServer" in entrypoint


def test_the_import_layout_matches_the_repository(agent: str, tmp_path: Path) -> None:
    """``runtimes.<agent>`` and ``chorus.contracts`` must resolve from the unpacked root.

    The allowlist is a set of repo-relative paths and the archive preserves them verbatim, so
    the deployed import graph is the one every test in this repository already exercises.
    """

    artifact = _build(agent, tmp_path)
    manifest = build.load_manifest(agent)

    with zipfile.ZipFile(tmp_path / artifact.archive) as archive:
        names = set(archive.namelist())

    for declared in manifest.include:
        assert declared in names, f"{declared} is declared but not in the archive"
    assert f"runtimes/{agent}/entrypoint.py" in names
    assert "runtimes/__init__.py" in names
    assert "runtimes/server.py" in names


def test_the_archive_contains_nothing_it_must_not(agent: str, tmp_path: Path) -> None:
    artifact = _build(agent, tmp_path)

    with zipfile.ZipFile(tmp_path / artifact.archive) as archive:
        names = archive.namelist()

    for name in names:
        parts = set(Path(name).parts)
        assert not parts & build.FORBIDDEN_ARCHIVE_PARTS, name
        assert "__pycache__" not in parts, name
        assert not name.endswith((".pyc", ".pyo")), name
    assert not any(name.startswith("tests/") or name.startswith("docs/") for name in names)


def test_no_build_host_executable_reaches_the_archive(agent: str, tmp_path: Path) -> None:
    """The build runs on Windows and the runtime is Linux/ARM64.

    Wheels are resolved for the target, but console scripts are generated for the build host, so
    the install leaves ``bin/*.exe`` behind. Shipping those would put an executable in the
    archive that cannot run on the machine that unpacks it -- exactly the failure the explicit
    platform target exists to prevent -- so they are removed and then refused.
    """

    artifact = _build(agent, tmp_path)

    with zipfile.ZipFile(tmp_path / artifact.archive) as archive:
        names = archive.namelist()

    assert not [name for name in names if name.startswith("bin/")]
    assert not [name for name in names if name.endswith(build.HOST_EXECUTABLE_SUFFIXES)]


def test_the_inspection_refuses_a_console_script_launcher(tmp_path: Path) -> None:
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("main.py", "")
        archive.writestr("bin/opentelemetry-instrument.exe", "MZ")

    problems = build.inspect_archive(broken)

    assert any("console-script launcher" in problem for problem in problems)
    assert any("build-host executable" in problem for problem in problems)


def _elf(machine: int) -> bytes:
    """The first twenty bytes of a 64-bit little-endian ELF object for one machine."""

    header = bytearray(b"\x7fELF\x02\x01\x01" + bytes(9) + b"\x03\x00")
    header.extend(machine.to_bytes(2, "little"))
    return bytes(header)


def test_a_compiled_extension_built_for_the_wrong_processor_is_refused(tmp_path: Path) -> None:
    """The check that would actually catch a mis-targeted build.

    A wheel filename can claim anything, and ``cryptography`` ships ``_rust.abi3.so`` with no
    platform tag at all. The ELF header says what the code was really compiled for.
    """

    archive_path = tmp_path / "wrong-arch.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.py", "")
        archive.writestr("pydantic_core/_pydantic_core.so", _elf(0x3E))
        archive.writestr("cryptography/hazmat/bindings/_rust.abi3.so", _elf(0xB7))

    problems = build.inspect_archive(
        archive_path, target_platform=EXPECTED_PLATFORM, first_party=VENDORED_ONLY
    )

    assert len(problems) == 1
    assert "_pydantic_core.so" in problems[0]
    assert "0x3e" in problems[0]


def test_a_compiled_extension_built_for_the_declared_processor_is_accepted(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "right-arch.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.py", "")
        archive.writestr("pydantic_core/_pydantic_core.so", _elf(0xB7))

    assert (
        build.inspect_archive(
            archive_path, target_platform=EXPECTED_PLATFORM, first_party=VENDORED_ONLY
        )
        == []
    )


def test_something_that_is_not_an_elf_object_at_all_is_refused(tmp_path: Path) -> None:
    """A Windows extension renamed ``.so`` would otherwise pass a name-shaped check."""

    archive_path = tmp_path / "not-elf.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.py", "")
        archive.writestr("pydantic_core/_pydantic_core.so", b"MZ\x90\x00 a PE image")

    problems = build.inspect_archive(
        archive_path, target_platform=EXPECTED_PLATFORM, first_party=VENDORED_ONLY
    )

    assert problems == ["pydantic_core/_pydantic_core.so is not an ELF object"]


def test_a_package_with_its_own_docs_or_tests_subpackage_is_not_refused(tmp_path: Path) -> None:
    """``botocore.docs`` is imported by botocore. A depth-blind check would refuse a good zip."""

    archive_path = tmp_path / "fine.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.py", "")
        archive.writestr("botocore/docs/__init__.py", "")
        archive.writestr("jsonschema/tests/__init__.py", "")

    assert build.inspect_archive(archive_path) == []

    repository_leak = tmp_path / "leaky.zip"
    with zipfile.ZipFile(repository_leak, "w") as archive:
        archive.writestr("main.py", "")
        archive.writestr("tests/conftest.py", "")
        archive.writestr("docs/adr/ADR-027.md", "")

    problems = build.inspect_archive(repository_leak)
    assert len(problems) == 2
    assert all("repository directory" in problem for problem in problems)


def test_the_action_archive_ships_no_private_module(tmp_path: Path) -> None:
    """The strictest allowlist, asserted against the built artifact rather than the manifest.

    The Action runtime is the one whose output becomes text somebody outside the community
    reads, so what it can import is what decides how much it could ever say.
    """

    artifact = _build("action", tmp_path)

    with zipfile.ZipFile(tmp_path / artifact.archive) as archive:
        names = set(archive.namelist())

    for forbidden in (
        "src/chorus/contracts/investigation.py",
        "src/chorus/contracts/monitor.py",
        "src/chorus/contracts/commitment.py",
        "src/chorus/domain/entities.py",
    ):
        assert forbidden not in names, f"{forbidden} must not ship in the Action artifact"


def test_the_investigator_archive_carries_both_reviewed_prompts(tmp_path: Path) -> None:
    """``EXTRACT_COMMITMENT`` runs in this artifact, so its prompt and contract ship in it."""

    artifact = _build("investigator", tmp_path)

    with zipfile.ZipFile(tmp_path / artifact.archive) as archive:
        names = set(archive.namelist())

    assert "runtimes/investigator/prompt.py" in names
    assert "runtimes/investigator/commitment_prompt.py" in names
    assert "src/chorus/contracts/commitment.py" in names
    assert "src/chorus/contracts/agentcore.py" in names


def test_the_archive_is_written_in_a_stable_order_with_a_fixed_timestamp(
    tmp_path: Path,
) -> None:
    """Filesystem enumeration order and the wall clock must not reach the digest."""

    artifact = _build("monitor", tmp_path)

    with zipfile.ZipFile(tmp_path / artifact.archive) as archive:
        names = archive.namelist()
        stamps = {info.date_time for info in archive.infolist()}

    assert names == sorted(names)
    assert stamps == {build.FIXED_TIMESTAMP}


def test_two_builds_of_the_same_inputs_produce_the_same_digest(tmp_path: Path) -> None:
    first = _build("monitor", tmp_path / "one")
    second = _build("monitor", tmp_path / "two")

    assert first.sha256 == second.sha256
    assert first.sha256.startswith("sha256:")
    assert first.file_count == second.file_count


def test_a_changed_source_file_changes_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hash that did not move when an input moved would be worse than no hash."""

    original = _build("monitor", tmp_path / "one")

    real_stage = build.stage_sources

    def stage_and_edit(manifest: build.RuntimeManifest, *, repository: Path, staging: Path) -> None:
        real_stage(manifest, repository=repository, staging=staging)
        (staging / "main.py").write_text("# a different entrypoint\n", encoding="utf-8")

    monkeypatch.setattr(build, "stage_sources", stage_and_edit)
    changed = _build("monitor", tmp_path / "two")

    assert changed.sha256 != original.sha256


def test_the_inspection_refuses_an_archive_missing_its_entrypoint(tmp_path: Path) -> None:
    """The check has to be able to fail, or it proves nothing about the ones that pass."""

    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("runtimes/__init__.py", "")
        archive.writestr(".env", "CHORUS_DEMO_ACCESS_SECRET_ARN=arn:aws:secretsmanager:...")

    problems = build.inspect_archive(broken)

    assert any("main.py" in problem for problem in problems)
    assert any(".env" in problem for problem in problems)


def test_a_build_whose_archive_fails_inspection_raises_rather_than_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        build,
        "inspect_archive",
        lambda path, *, target_platform=None, first_party=None: ["deliberate failure"],
    )

    with pytest.raises(build.BuildError, match="deliberate failure"):
        _build("monitor", tmp_path)


# -- the build manifest ----------------------------------------------------------------------


def test_the_build_records_what_a_publish_step_needs_and_nothing_more(tmp_path: Path) -> None:
    artifacts = [_build(agent, tmp_path) for agent in build.RUNTIME_NAMES]

    path = build.write_manifest_file(artifacts, output_root=tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["schema"] == "agentcore-artifacts/v1"
    assert len(document["artifacts"]) == 3
    for record in document["artifacts"]:
        assert set(record) == {
            "runtime",
            "deployed_name",
            "archive",
            "sha256",
            "size_bytes",
            "python_runtime",
            "target_platform",
            "entrypoint",
            "file_count",
            "security_scan",
        }
        assert record["sha256"].startswith("sha256:")
        assert record["python_runtime"] == "PYTHON_3_12"
        assert record["target_platform"] == EXPECTED_PLATFORM
        assert record["entrypoint"] == "main.py"
        assert record["security_scan"] == build.ARTIFACT_SCANNER_VERSION
    names = {record["deployed_name"] for record in document["artifacts"]}
    assert names == {"chorus_monitor", "chorus_investigator", "chorus_action"}


def test_the_build_output_directory_is_ignored_by_git() -> None:
    """Artifacts are build products. A zip in a commit is a zip nobody reviewed."""

    ignored = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "build/" in ignored
    assert build.DEFAULT_OUTPUT_ROOT.is_relative_to(REPOSITORY_ROOT / "build")


def test_the_build_uploads_nothing(tmp_path: Path) -> None:
    """This stage is offline. Publication is a later stage with an AWS identity behind it."""

    import ast
    import inspect

    tree = ast.parse(inspect.getsource(build))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & {"upload_file", "put_object", "upload_fileobj", "client", "resource"}
    assert "boto3" not in inspect.getsource(build).split('"""')[0]
