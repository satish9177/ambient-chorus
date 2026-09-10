"""Build the five first-party Lambda deployment zips from the manifests and the lockfile.

One artifact per production function -- API, worker, compiler, sender, commitment watcher --
each containing the first-party trees its ``lambda.toml`` allowlists plus the project's locked
dependency closure, packaged for the platform Lambda actually runs on. Nothing is uploaded:
this stage is offline and produces files under ``build/lambda/`` for a later deploy step to
hand to ``lambda.Code.from_asset`` (deployment contract SS 16 stage 3-9).

What decides the contents
-------------------------
Two files and no judgement of this module's own. ``functions/<name>/lambda.toml`` decides which
first-party paths ship, which module answers an invocation, and which architecture to resolve
for. ``uv.lock`` decides every dependency version, through ``uv export --frozen``: a build that
resolved dependencies afresh would produce an artifact whose contents depend on the day it ran.
The dependency set is the project's **base** requirements (``--no-default-groups``) -- never an
unconstrained requirements file, and never the ``dev``/``test``/``agents``/``infra`` groups.

Import layout, and why it differs from the AgentCore artifact
------------------------------------------------------------
AgentCore runs ``python main.py`` and puts only the archive root on ``sys.path``, so that
artifact keeps ``src/chorus`` and bootstraps it. Lambda imports the handler module by dotted
name and puts the archive **root** (``/var/task``) on ``sys.path`` itself, so the first-party
packages are placed at the archive root directly: ``src/chorus`` -> ``chorus/``,
``apps/api/chorus_api`` -> ``chorus_api/``, ``functions/**`` unchanged. ``import
functions.api.handler``, ``import chorus_api.main`` and ``import chorus...`` then resolve from
the archive and from nowhere else -- proved by the isolated-import test that unpacks a real zip
outside the repository with the repo root off ``sys.path``.

Cross-platform packaging, and why it is not optional
----------------------------------------------------
The build machine is Windows on x86-64 and the runtime is Linux. ``pydantic-core`` and
``pillow`` ship compiled wheels, so a plain install would vendor wheels the runtime cannot load
-- and the failure surfaces as an import error inside a deployed function. So dependencies are
installed for an explicit target::

    uv pip install --python-platform x86_64-manylinux2014 --python-version 3.12
                   --only-binary :all: --target <staging> -r <exported requirements>

``--only-binary :all:`` is what makes the target meaningful: without it a package with no
matching wheel is built from source for the build machine, silently.

Artifact security
-----------------
Every generated zip is a deployment artifact and is gated with the repository's own credential
patterns twice -- once over the first-party files this build staged, and once over the finished
zip -- and, for the compiled extensions it vendored, by an ELF ``e_machine`` check. First-party
entries get **no** content exception at any path. The AgentCore ZIP gate is untouched; this is
a second application of the same patterns, not a second answer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tools.build_runtime_artifacts import (
    DIRECTORY_PERMISSIONS,
    FILE_PERMISSIONS,
    FIXED_TIMESTAMP,
    HOST_EXECUTABLE_SUFFIXES,
    SCRIPT_DIRECTORY,
    VENDOR_CONTENT_EXCEPTIONS,
    BuildError,
    CommandRunner,
    UndecodableFirstPartyError,
    _native_module_problems,
    _purge_host_artifacts,
    _run,
    archive_members,
    export_command,
    file_digest,
    scan_secret_matches,
    secret_content_problem,
    sensitive_name_problem,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
FUNCTIONS_ROOT: Final = REPOSITORY_ROOT / "functions"
DEFAULT_OUTPUT_ROOT: Final = REPOSITORY_ROOT / "build" / "lambda"

FUNCTION_DIRS: Final = ("api", "worker", "compiler", "sender", "commitment_watcher")
"""The five production function packages, by directory name under ``functions/``."""

PLATFORM_BY_ARCHITECTURE: Final = {
    "x86_64": "x86_64-manylinux_2_28",
    "arm64": "aarch64-manylinux_2_28",
}
"""``lambda.toml`` names a Lambda architecture; this maps it to the ``uv`` target triple.

``manylinux_2_28`` rather than ``manylinux2014``: Pillow 12 (the compiler's image sanitizer)
dropped ``manylinux2014`` wheels, and the Lambda Python 3.12 runtime is Amazon Linux 2023 with
glibc 2.34, which satisfies the 2.28 floor.
"""

SOURCE_ROOT_PREFIXES: Final = ("src/", "apps/api/")
"""Repo-relative prefixes stripped when a first-party path is placed in the archive.

``src/chorus/...`` -> ``chorus/...`` and ``apps/api/chorus_api/...`` -> ``chorus_api/...`` so
the deployed import graph is the repository's, not a re-rooted approximation of it.
"""

FORBIDDEN_ARCHIVE_PARTS: Final = frozenset(
    {".git", ".github", ".env", ".venv", ".aws", ".ssh", "node_modules", "cdk.out", ".ruff_cache"}
)
"""Path segments that must never appear anywhere in a Lambda artifact, at any depth."""

FORBIDDEN_ARCHIVE_ROOTS: Final = frozenset(
    {"tests", "docs", "infra", "apps", "demo", "build", "src", "runtimes"}
)
"""Repository directories that must never be a top-level archive entry.

``src`` is here because the build **strips** the ``src/`` prefix -- a surviving ``src/`` root
means the remap did not run. ``functions`` is deliberately absent: the Lambda artifact carries
``functions/<name>/`` by design. ``apps`` is forbidden because ``chorus_api`` is re-rooted out
of it; an ``apps/`` root means the same remap bug.
"""

ARTIFACT_SCANNER_VERSION: Final = "lambda-secret-scan/v2"

_PILLOW_IMAGEFONT_PATH: Final = "PIL/ImageFont.py"
_PILLOW_IMAGEFONT_SHA256: Final = "24fa5feeb91b4bf63eaad0ebba08a8161e9c889d9fd056a37c928134097b9649"
"""SHA-256 of ``PIL/ImageFont.py`` as the pinned ``pillow==12.3.0`` Linux wheel ships it.

Pillow embeds a base64 test font in this module and the blob contains one substring that
matches the repository's ``AKIA[0-9A-Z]{16}`` access-key pattern. The suppression below is
scoped to **this exact file at this exact digest and that one exact matched substring** -- every
other pattern (a private key, a different credential shape) and any *other* ``AKIA`` match in
the same file still fails the scan, and if the digest ever changes (a Pillow bump) the file
gets the full unsuppressed scan and a human must re-verify and re-pin.
"""
# The exact substring of Pillow's embedded base64 test font that trips the AKIA pattern,
# assembled from parts so this repository's own credential scan does not flag *this* file.
_PILLOW_IMAGEFONT_ALLOWED_MATCHES: Final = frozenset({("pattern-0", "AKIA" + "AQAAAAAABAAHAM0A")})
"""The single ``(kind, exact matched text)`` pair suppressed for the pinned ``PIL/ImageFont.py``."""


class LambdaBuildError(BuildError):
    """The Lambda build cannot produce a correct artifact and refuses an incorrect one."""


@dataclass(frozen=True, slots=True)
class LambdaManifest:
    """The parts of one ``functions/<name>/lambda.toml`` the build and the CDK stacks read."""

    directory: str
    name: str
    handler: str
    description: str
    architecture: str
    python_version: str
    memory_mb: int
    timeout_seconds: int
    first_party: tuple[str, ...]

    @property
    def target_platform(self) -> str:
        platform = PLATFORM_BY_ARCHITECTURE.get(self.architecture)
        if platform is None:
            raise LambdaBuildError(
                f"{self.directory}: unsupported architecture {self.architecture!r}"
            )
        return platform

    @property
    def archive_name(self) -> str:
        return f"{self.name}.zip"

    @property
    def handler_module(self) -> str:
        """``functions.api.handler`` from ``functions.api.handler.handler``."""

        module, _, symbol = self.handler.rpartition(".")
        if not module or not symbol:
            raise LambdaBuildError(f"{self.directory}: handler {self.handler!r} is not dotted")
        return module

    @property
    def handler_archive_path(self) -> str:
        """The archive path of the module file the handler lives in, e.g.
        ``functions/api/handler.py``."""

        return self.handler_module.replace(".", "/") + ".py"

    def archive_first_party(self, repository: Path) -> frozenset[str]:
        """Every archive-relative path this build copies out of the repository.

        Expanded from the manifest's ``first_party`` (files and directories) with the source-root
        prefixes stripped, so a first-party file cannot become "vendored" -- and eligible for a
        content exception -- by being written somewhere new.
        """

        found: set[str] = set()
        for relative in self.first_party:
            source = repository / relative
            if source.is_file():
                found.add(remap_archive_path(relative))
            elif source.is_dir():
                for path in source.rglob("*"):
                    if path.is_file():
                        rel = path.relative_to(repository).as_posix()
                        found.add(remap_archive_path(rel))
        return frozenset(found)


@dataclass(frozen=True, slots=True)
class BuiltLambdaArtifact:
    """One finished Lambda zip, described by what a deploy step needs to know about it."""

    function: str
    name: str
    archive: str
    sha256: str
    size_bytes: int
    handler: str
    python_runtime: str
    architecture: str
    target_platform: str
    memory_mb: int
    timeout_seconds: int
    file_count: int
    security_scan: str = ARTIFACT_SCANNER_VERSION

    def as_record(self) -> dict[str, object]:
        return {
            "function": self.function,
            "name": self.name,
            "archive": self.archive,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "handler": self.handler,
            "python_runtime": self.python_runtime,
            "architecture": self.architecture,
            "target_platform": self.target_platform,
            "memory_mb": self.memory_mb,
            "timeout_seconds": self.timeout_seconds,
            "file_count": self.file_count,
            "security_scan": self.security_scan,
        }


def remap_archive_path(relative: str) -> str:
    """Strip a known source-root prefix so the archive path is the deployed import path."""

    for prefix in SOURCE_ROOT_PREFIXES:
        if relative.startswith(prefix):
            return relative[len(prefix) :]
    return relative


def load_lambda_manifest(directory: str, *, root: Path = FUNCTIONS_ROOT) -> LambdaManifest:
    """Read one ``lambda.toml``, refusing anything it does not fully declare."""

    path = root / directory / "lambda.toml"
    if not path.is_file():
        raise LambdaBuildError(f"no lambda.toml for function {directory}")
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    function = document["function"]
    runtime = document["runtime"]
    artifact = document["artifact"]
    return LambdaManifest(
        directory=directory,
        name=str(function["name"]),
        handler=str(function["handler"]),
        description=str(function["description"]),
        architecture=str(runtime["architecture"]),
        python_version=str(runtime["python_version"]),
        memory_mb=int(runtime["memory_mb"]),
        timeout_seconds=int(runtime["timeout_seconds"]),
        first_party=tuple(str(item) for item in artifact["first_party"]),
    )


def all_manifests() -> tuple[LambdaManifest, ...]:
    return tuple(load_lambda_manifest(directory) for directory in FUNCTION_DIRS)


def install_command(manifest: LambdaManifest, *, requirements: Path, target: Path) -> list[str]:
    """The cross-platform install. Every flag is load-bearing; see the module docstring."""

    return [
        "uv",
        "pip",
        "install",
        "--python-platform",
        manifest.target_platform,
        "--python-version",
        manifest.python_version,
        "--only-binary",
        ":all:",
        "--target",
        str(target),
        "--requirement",
        str(requirements),
    ]


def stage_first_party(manifest: LambdaManifest, *, repository: Path, staging: Path) -> None:
    """Copy the allowlisted first-party files, remapped, refusing anything undeclared."""

    for relative in manifest.first_party:
        source = repository / relative
        if source.is_file():
            destination = staging / remap_archive_path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        elif source.is_dir():
            for path in sorted(source.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(repository).as_posix()
                if rel.endswith((".pyc", ".pyo")) or "__pycache__" in Path(rel).parts:
                    continue
                destination = staging / remap_archive_path(rel)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
        else:
            raise LambdaBuildError(
                f"{manifest.directory}: {relative} is declared but is neither file nor directory"
            )


def write_archive(staging: Path, destination: Path) -> tuple[str, int]:
    """Write the zip with normalised metadata; return its digest and entry count.

    Sorted entries, a fixed 1980-01-01 timestamp, and fixed permissions, so two builds from one
    commit and one lockfile produce the same bytes and the same SHA-256 (deployment contract
    SS 6 -- deterministic given identical source/lock input, not a cross-machine reproducible
    build).
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    members = archive_members(staging)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in members:
            relative = path.relative_to(staging).as_posix()
            info = zipfile.ZipInfo(relative, date_time=FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = FILE_PERMISSIONS << 16
            archive.writestr(info, path.read_bytes())
    return file_digest(destination), len(members)


def inspect_lambda_archive(
    path: Path, *, target_platform: str, first_party: frozenset[str], handler_path: str
) -> list[str]:
    """Return every reason this archive is not shippable, or an empty list.

    The Lambda-specific counterpart to ``build_runtime_artifacts.inspect_archive``: there is no
    ``main.py`` at the root, and ``functions/`` is a legitimate top-level entry, so the two
    checks cannot share one function.
    """

    problems: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        problems.extend(_native_module_problems(archive, names, target_platform))
        problems.extend(_secret_problems(archive, names, first_party))
    if handler_path not in names:
        problems.append(f"{path.name}: handler module {handler_path} is not in the archive")
    if "chorus/__init__.py" not in names:
        problems.append(f"{path.name}: the shared package chorus/ is not at the archive root")
    for name in names:
        parts = Path(name).parts
        if set(parts) & FORBIDDEN_ARCHIVE_PARTS:
            problems.append(f"{path.name}: contains {name}")
        if parts and parts[0] in FORBIDDEN_ARCHIVE_ROOTS:
            problems.append(f"{path.name}: contains repository directory {name}")
        if name.endswith((".pyc", ".pyo")) or "__pycache__" in parts:
            problems.append(f"{path.name}: contains compiled bytecode {name}")
        if name.endswith(HOST_EXECUTABLE_SUFFIXES):
            problems.append(f"{path.name}: contains a build-host executable {name}")
        if parts and parts[0] == SCRIPT_DIRECTORY:
            problems.append(f"{path.name}: contains a console-script launcher {name}")
    return problems


def _vendored_content_problem(name: str, data: bytes) -> str | None:
    """Credential-pattern scan of one vendored file.

    Two exception layers, both narrow:

    * the four ``boto3`` / ``botocore`` / ``cryptography`` **example/doc** files already
      documented in ``build_runtime_artifacts.VENDOR_CONTENT_EXCEPTIONS`` -- pure RST/JSON
      documentation shipped by the wheels, the same accepted exception the AgentCore builder
      uses -- are skipped by exact path;
    * ``PIL/ImageFont.py`` is **still fully scanned**; only the single known false-positive
      ``(kind, exact matched text)`` pair is suppressed, and only when the file's SHA-256
      matches the pinned digest. A private key, any other credential pattern, any *other* match
      in the same file, a different digest, or a different path all still produce a finding
      (review P2-7).
    """

    if name in VENDOR_CONTENT_EXCEPTIONS:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None  # a compiled extension is not text; its provenance is the lockfile + ELF check
    allowed: frozenset[tuple[str, str]] = frozenset()
    if (
        name == _PILLOW_IMAGEFONT_PATH
        and hashlib.sha256(data).hexdigest() == _PILLOW_IMAGEFONT_SHA256
    ):
        allowed = _PILLOW_IMAGEFONT_ALLOWED_MATCHES
    for kind, matched in scan_secret_matches(text):
        if (kind, matched) in allowed:
            continue
        if kind == "private-key":
            return f"contains private-key material: {name}"
        return f"contains credential-shaped content ({kind}): {name}"
    return None


def _secret_problems(
    archive: zipfile.ZipFile, names: list[str], first_party: frozenset[str]
) -> list[str]:
    problems: list[str] = []
    for name in names:
        named = sensitive_name_problem(name)
        if named is not None:
            problems.append(named)
        data = archive.read(name)
        if name in first_party:
            try:
                found = secret_content_problem(name, data, first_party=True)
            except UndecodableFirstPartyError as error:
                problems.append(str(error))
                continue
        else:
            found = _vendored_content_problem(name, data)
        if found is not None:
            problems.append(found)
    return problems


def scan_staged_first_party(staging: Path, relative_paths: frozenset[str]) -> list[str]:
    """Scan the repository files this build copied, before anything is packaged.

    No exception list: every path here was staged by this build, from this repository. A file
    that cannot be decoded is a finding, never a file quietly passed over.
    """

    problems: list[str] = []
    for relative in sorted(relative_paths):
        path = staging / relative
        if not path.is_file():
            continue
        named = sensitive_name_problem(relative)
        if named is not None:
            problems.append(named)
        try:
            found = secret_content_problem(relative, path.read_bytes(), first_party=True)
        except UndecodableFirstPartyError as error:
            problems.append(str(error))
            continue
        if found is not None:
            problems.append(found)
    return problems


def build_lambda(
    directory: str,
    *,
    repository: Path = REPOSITORY_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    runner: CommandRunner | None = None,
) -> BuiltLambdaArtifact:
    """Build one Lambda artifact end to end and return its record."""

    manifest = load_lambda_manifest(directory, root=repository / "functions")
    staging = output_root / "staging" / directory
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    requirements = output_root / "staging" / f"{directory}-requirements.txt"
    requirements.parent.mkdir(parents=True, exist_ok=True)

    _run(export_command(requirements, only_group=None), cwd=repository, runner=runner)
    _run(
        install_command(manifest, requirements=requirements, target=staging),
        cwd=repository,
        runner=runner,
    )
    _purge_host_artifacts(staging)
    stage_first_party(manifest, repository=repository, staging=staging)

    first_party = manifest.archive_first_party(repository)
    staged = scan_staged_first_party(staging, first_party)
    if staged:
        raise LambdaBuildError(f"{directory}: " + "; ".join(staged))

    archive = output_root / manifest.archive_name
    digest, count = write_archive(staging, archive)
    problems = inspect_lambda_archive(
        archive,
        target_platform=manifest.target_platform,
        first_party=first_party,
        handler_path=manifest.handler_archive_path,
    )
    if problems:
        raise LambdaBuildError("; ".join(problems))
    return BuiltLambdaArtifact(
        function=directory,
        name=manifest.name,
        archive=manifest.archive_name,
        sha256=digest,
        size_bytes=archive.stat().st_size,
        handler=manifest.handler,
        python_runtime=f"PYTHON_{manifest.python_version.replace('.', '_')}",
        architecture=manifest.architecture,
        target_platform=manifest.target_platform,
        memory_mb=manifest.memory_mb,
        timeout_seconds=manifest.timeout_seconds,
        file_count=count,
    )


def write_manifest_file(artifacts: list[BuiltLambdaArtifact], *, output_root: Path) -> Path:
    path = output_root / "artifacts.json"
    document = {
        "schema": "lambda-artifacts/v1",
        "artifacts": [artifact.as_record() for artifact in artifacts],
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--function", action="append", choices=FUNCTION_DIRS, default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
    arguments = parser.parse_args(argv)
    selected = tuple(arguments.function) if arguments.function else FUNCTION_DIRS

    built: list[BuiltLambdaArtifact] = []
    for directory in selected:
        try:
            built.append(build_lambda(directory, output_root=arguments.output))
        except BuildError as error:
            sys.stderr.write(f"lambda build failed for {directory}: {error}\n")
            return 1
    manifest_path = write_manifest_file(built, output_root=arguments.output)
    for artifact in built:
        sys.stdout.write(f"{artifact.archive} {artifact.sha256} {artifact.file_count} files\n")
    sys.stdout.write(f"manifest: {manifest_path}\n")
    return 0


# ``DIRECTORY_PERMISSIONS`` is imported for parity with the AgentCore builder's constant set and
# to keep the two modules' vocabularies aligned; the archive writer applies file permissions and
# lets the zip format imply directories.
_ = DIRECTORY_PERMISSIONS


if __name__ == "__main__":  # pragma: no cover - the command line
    raise SystemExit(main())
