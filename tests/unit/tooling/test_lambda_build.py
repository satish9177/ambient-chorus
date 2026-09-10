"""The first-party Lambda packaging tool: deterministic, lock-based, and gated (SS 6, SS 8, SS 47).

The tests that need a finished archive depend on the ``built_lambda_artifacts`` session fixture
(``conftest.py``), which builds all five ZIPs once into a temp directory -- so a clean checkout
proves the build and the final-ZIP scans without a checked-in artifact and without a silent
skip (review P2-5). Everything else (the manifest schema, the command argv, the path remap, the
narrow secret-scan exceptions) runs unconditionally.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from tools import build_lambda_artifacts as build
from tools.build_runtime_artifacts import (
    ELF_MACHINE_BY_PLATFORM,
    export_command,
    is_shared_object,
)
from tools.build_runtime_artifacts import _native_module_problems as native_module_problems

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


# -- the manifest schema ---------------------------------------------------------------------


@pytest.mark.parametrize("directory", build.FUNCTION_DIRS)
def test_every_function_declares_a_complete_manifest(directory: str) -> None:
    manifest = build.load_lambda_manifest(directory)

    assert manifest.name.startswith("chorus-")
    assert manifest.handler.endswith(".handler")
    assert manifest.handler_module.endswith(".handler")
    assert manifest.handler_archive_path.endswith("/handler.py")
    assert manifest.architecture == "x86_64"
    assert manifest.python_version == "3.12"
    assert 128 <= manifest.memory_mb <= 10240
    assert 1 <= manifest.timeout_seconds <= 900
    assert manifest.first_party  # a non-empty allowlist


def test_the_five_functions_are_exactly_the_production_set() -> None:
    assert set(build.FUNCTION_DIRS) == {
        "api",
        "worker",
        "compiler",
        "sender",
        "commitment_watcher",
    }
    names = {build.load_lambda_manifest(d).name for d in build.FUNCTION_DIRS}
    assert names == {
        "chorus-api",
        "chorus-worker",
        "chorus-compiler",
        "chorus-sender",
        "chorus-commitment-watcher",
    }


def test_only_the_api_manifest_ships_chorus_api() -> None:
    """``chorus_api`` is the API's alone; nothing else imports it."""

    for directory in build.FUNCTION_DIRS:
        first_party = build.load_lambda_manifest(directory).first_party
        has_api = any("apps/api/chorus_api" in path for path in first_party)
        assert has_api is (directory == "api"), directory


# -- the commands are lock-respecting and cross-platform ------------------------------------


def test_the_export_command_pins_the_base_requirements_and_no_groups() -> None:
    command = export_command(Path("requirements.txt"), only_group=None)

    assert "--frozen" in command
    assert "--no-default-groups" in command
    assert "--only-group" not in command  # never the agents/dev/test/infra groups


def test_the_install_command_targets_linux_and_forbids_source_builds() -> None:
    manifest = build.load_lambda_manifest("compiler")
    command = build.install_command(manifest, requirements=Path("r.txt"), target=Path("t"))

    platform = command[command.index("--python-platform") + 1]
    assert platform == "x86_64-manylinux_2_28"
    assert platform in ELF_MACHINE_BY_PLATFORM
    assert command[command.index("--python-version") + 1] == "3.12"
    assert command[command.index("--only-binary") + 1] == ":all:"


# -- the archive-path remap ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("src/chorus/settings.py", "chorus/settings.py"),
        ("apps/api/chorus_api/main.py", "chorus_api/main.py"),
        ("functions/envelope.py", "functions/envelope.py"),
        ("functions/api/handler.py", "functions/api/handler.py"),
    ],
)
def test_source_roots_are_stripped_so_the_import_path_is_the_archive_path(
    relative: str, expected: str
) -> None:
    assert build.remap_archive_path(relative) == expected


# -- the staged-file secret scan has no exception list ------------------------------------


def test_the_staged_first_party_scan_flags_a_planted_secret(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    (staging / "chorus").mkdir(parents=True)
    planted = staging / "chorus" / "leak.py"
    planted.write_text('api_key = "AKIA' + "A" * 16 + '"\n', encoding="utf-8")

    problems = build.scan_staged_first_party(staging, frozenset({"chorus/leak.py"}))

    assert any("credential-shaped" in p for p in problems)


def test_inspect_rejects_a_repository_directory_at_the_archive_root(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("chorus/__init__.py", "")
        zf.writestr("functions/api/handler.py", "def handler(e, c=None): return {}\n")
        zf.writestr("tests/unit/test_x.py", "")  # this repository's own tests must never ship

    problems = build.inspect_lambda_archive(
        archive,
        target_platform="x86_64-manylinux_2_28",
        first_party=frozenset({"chorus/__init__.py", "functions/api/handler.py"}),
        handler_path="functions/api/handler.py",
    )

    assert any("repository directory" in p and "tests/" in p for p in problems)


def test_inspect_requires_the_handler_module_and_the_shared_package(tmp_path: Path) -> None:
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("functions/__init__.py", "")

    problems = build.inspect_lambda_archive(
        archive,
        target_platform="x86_64-manylinux_2_28",
        first_party=frozenset({"functions/__init__.py"}),
        handler_path="functions/api/handler.py",
    )

    assert any("handler module functions/api/handler.py" in p for p in problems)
    assert any("chorus/" in p for p in problems)


# -- the real archives (built by the session fixture; no skip) ----------------------------


@pytest.fixture(params=build.FUNCTION_DIRS)
def directory(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def archive_path(directory: str, built_artifact_paths: dict[str, Path]) -> Path:
    return built_artifact_paths[directory]


def test_the_built_archive_is_clean_and_importable_in_shape(
    directory: str, archive_path: Path
) -> None:
    manifest = build.load_lambda_manifest(directory)

    problems = build.inspect_lambda_archive(
        archive_path,
        target_platform=manifest.target_platform,
        first_party=manifest.archive_first_party(REPOSITORY_ROOT),
        handler_path=manifest.handler_archive_path,
    )
    assert problems == []

    with zipfile.ZipFile(archive_path) as zf:
        names = set(zf.namelist())
    assert manifest.handler_archive_path in names
    assert "chorus/__init__.py" in names
    assert "chorus/settings.py" in names
    assert "functions/envelope.py" in names
    assert ("chorus_api/main.py" in names) is (directory == "api")
    assert not any(n.startswith(("tests/", "docs/", "infra/", "src/", "apps/")) for n in names)
    assert not any(n.endswith((".pyc", ".pyo")) for n in names)
    assert not any("__pycache__" in n for n in names)
    assert not any(n.startswith("bin/") for n in names)
    assert not any(n.endswith((".exe", ".bat", ".dll")) for n in names)
    assert not any(Path(n).name in {"credentials", ".env", "id_rsa"} for n in names)


def test_every_shared_object_in_a_built_archive_is_the_target_processor(
    directory: str, archive_path: Path
) -> None:
    """The ELF check covers versioned shared libraries too (``libjpeg-*.so.62``), not only
    names ending exactly ``.so`` (review P2-7)."""

    manifest = build.load_lambda_manifest(directory)
    with zipfile.ZipFile(archive_path) as zf:
        names = zf.namelist()
        sos = [n for n in names if is_shared_object(n)]
        assert sos, f"{directory} archive vendors no shared object at all"
        # the compiler's Pillow ships versioned libs a plain endswith('.so') would miss
        if directory == "compiler":
            assert any(".so." in n for n in sos)
        problems = native_module_problems(zf, sos, manifest.target_platform)
    assert problems == []


# -- P2-7: the PIL/ImageFont.py exception is narrow, not a whole-file skip ---------------


def _pillow_imagefont_bytes(built_artifact_paths: dict[str, Path]) -> bytes:
    with zipfile.ZipFile(built_artifact_paths["compiler"]) as zf:
        return zf.read("PIL/ImageFont.py")


def test_pristine_pillow_imagefont_passes_the_vendored_scan(
    built_artifact_paths: dict[str, Path],
) -> None:
    data = _pillow_imagefont_bytes(built_artifact_paths)
    assert hashlib.sha256(data).hexdigest() == build._PILLOW_IMAGEFONT_SHA256
    assert build._vendored_content_problem("PIL/ImageFont.py", data) is None


def test_pillow_imagefont_plus_a_private_key_still_fails(
    built_artifact_paths: dict[str, Path],
) -> None:
    # assembled from parts so this repository's own secret scan does not flag this test file
    private_key_header = b"-----BEGIN RSA " + b"PRIVATE KEY-----"
    tampered = _pillow_imagefont_bytes(built_artifact_paths) + b"\n# " + private_key_header + b"\n"
    problem = build._vendored_content_problem("PIL/ImageFont.py", tampered)
    assert problem is not None and "private-key" in problem


def test_pillow_imagefont_plus_a_different_credential_still_fails(
    built_artifact_paths: dict[str, Path],
) -> None:
    credential_line = b"secret_access" + b'_key = "' + b"n0t-a-real-key-000000" + b'"'
    tampered = _pillow_imagefont_bytes(built_artifact_paths) + b"\n" + credential_line + b"\n"
    problem = build._vendored_content_problem("PIL/ImageFont.py", tampered)
    assert problem is not None and "pattern-" in problem


def test_a_first_party_file_with_the_same_text_is_never_excepted(
    built_artifact_paths: dict[str, Path], tmp_path: Path
) -> None:
    """A first-party file gets no content exception at any path -- even one byte-identical to
    the pinned ``PIL/ImageFont.py``."""

    data = _pillow_imagefont_bytes(built_artifact_paths)
    archive = tmp_path / "fp.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("chorus/__init__.py", "")
        zf.writestr("chorus/looks_like_imagefont.py", data)
        zf.writestr("functions/api/handler.py", "def handler(e, c=None): return {}\n")
    problems = build.inspect_lambda_archive(
        archive,
        target_platform="x86_64-manylinux_2_28",
        first_party=frozenset(
            {"chorus/__init__.py", "chorus/looks_like_imagefont.py", "functions/api/handler.py"}
        ),
        handler_path="functions/api/handler.py",
    )
    assert any("looks_like_imagefont.py" in p and "credential-shaped" in p for p in problems)


def test_an_unexpected_pillow_digest_gets_the_full_unsuppressed_scan(
    built_artifact_paths: dict[str, Path],
) -> None:
    """If a Pillow bump changes the file, the suppression does not apply and the base64 blob's
    ``AKIA`` match refuses the build -- forcing a human to re-verify and re-pin (review P2-7)."""

    changed = _pillow_imagefont_bytes(built_artifact_paths) + b"\n# a byte that moves the digest\n"
    assert hashlib.sha256(changed).hexdigest() != build._PILLOW_IMAGEFONT_SHA256
    problem = build._vendored_content_problem("PIL/ImageFont.py", changed)
    assert problem is not None and "pattern-0" in problem


def test_the_archive_writer_is_deterministic_for_one_staging_tree(tmp_path: Path) -> None:
    """SS 6: identical staged input produces the same bytes -- fixed 1980 timestamps, sorted
    entries, fixed permissions -- so the digest changes with an input and not with the clock."""

    staging = tmp_path / "staging"
    (staging / "chorus").mkdir(parents=True)
    (staging / "chorus" / "__init__.py").write_text("x\n", encoding="utf-8")
    (staging / "functions").mkdir()
    (staging / "functions" / "envelope.py").write_text("y\n", encoding="utf-8")

    first, count_a = build.write_archive(staging, tmp_path / "a.zip")
    second, count_b = build.write_archive(staging, tmp_path / "b.zip")

    assert first == second
    assert count_a == count_b == 2


def test_the_artifacts_manifest_matches_the_lambda_manifests(
    built_lambda_artifacts: Path,
) -> None:
    """The build record's handler / runtime / budget per function is the one its ``lambda.toml``
    declares -- no drift between the record and the source of truth the CDK stacks also read."""

    import json

    document = json.loads((built_lambda_artifacts / "artifacts.json").read_text(encoding="utf-8"))
    by_name = {entry["name"]: entry for entry in document["artifacts"]}
    for directory in build.FUNCTION_DIRS:
        manifest = build.load_lambda_manifest(directory)
        entry = by_name[manifest.name]
        assert entry["handler"] == manifest.handler
        assert entry["architecture"] == manifest.architecture
        assert entry["memory_mb"] == manifest.memory_mb
        assert entry["timeout_seconds"] == manifest.timeout_seconds
        assert entry["python_runtime"] == "PYTHON_3_12"


def test_the_build_is_deterministic_across_two_full_runs(
    built_lambda_artifacts: Path, tmp_path: Path
) -> None:
    """SS 6: a second full build from the same source and lock produces byte-identical ZIPs."""

    for directory in build.FUNCTION_DIRS:
        first = build.build_lambda(directory, output_root=built_lambda_artifacts)
        second = build.build_lambda(directory, output_root=tmp_path)
        assert first.sha256 == second.sha256, directory
