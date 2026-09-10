"""Build the three AgentCore direct-code zips from the manifests and the lockfile.

One artifact per runtime, each containing exactly what its own ``runtime.toml`` allowlists plus
the locked dependency set, packaged for the platform the runtime actually runs on. Nothing is
uploaded: this stage is offline and produces files under ``build/agentcore/`` for a later
publish step to take.

What decides the contents
-------------------------
Two files and no judgement of this module's own. ``runtime.toml`` decides which source files
ship, which one becomes the archive's ``main.py``, and which platform to resolve for.
``uv.lock`` decides every dependency version, through ``uv export --frozen``: a build that
resolved dependencies afresh would produce an artifact whose contents depend on the day it ran.

Cross-platform packaging, and why it is not optional
----------------------------------------------------
The build machine is Windows on x86-64 and the runtime is Linux on ARM64. ``pydantic-core``,
``rpds-py``, ``cryptography``, and ``jiter`` all ship compiled wheels, so a plain install would
vendor wheels the runtime cannot load -- and the failure surfaces as an import error inside a
deployed container, which is the most expensive place to discover it. So dependencies are
installed for an explicit target:

    uv pip install --python-platform aarch64-manylinux2014 --python-version 3.12
                   --only-binary :all: --target <staging> -r <exported requirements>

``--only-binary :all:`` is what makes the target meaningful. Without it a package with no
matching wheel is built from source *for the build machine*, silently, and the resulting
artifact looks fine until it runs.

Reproducibility
---------------
Contents are lock-controlled and the archive is written in sorted order with a fixed timestamp
and fixed permissions, so two builds from one commit and one lockfile produce the same bytes and
therefore the same SHA-256. That is not a byte-for-byte reproducible-build guarantee across
machines -- wheels are unpacked by ``uv`` and a future version could order or compile something
differently -- and it is not claimed to be one. What it is: a hash that changes when an input
changes and not when the clock does.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tokenize
import tomllib
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final

from tools.check_secrets import PATTERNS

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
RUNTIMES_ROOT: Final = REPOSITORY_ROOT / "runtimes"
DEFAULT_OUTPUT_ROOT: Final = REPOSITORY_ROOT / "build" / "agentcore"

RUNTIME_NAMES: Final = ("monitor", "investigator", "action")

type CommandRunner = Callable[[list[str], Path], None]
"""An injected replacement for the subprocess call.

Exists so a test can assert the exact argv a build would run -- the target platform, the
Python version, the ``--frozen`` flag -- without spending a dependency download to prove it.
"""

DEPENDENCY_GROUP: Final = "agents"
"""The locked group the runtimes' dependencies come from.

``strands-agents`` and its closure, which is what the three artifacts import. The application
groups are deliberately not exported: an agent runtime that shipped ``boto3`` clients for
DynamoDB or SES would carry code for capabilities its IAM role denies.
"""

ARCHIVE_ENTRYPOINT: Final = "main.py"
FIXED_TIMESTAMP: Final = (1980, 1, 1, 0, 0, 0)
"""The earliest timestamp a zip entry can carry.

Every entry gets it, so the archive hash reflects the files and not the minute they were copied.
"""

FILE_PERMISSIONS: Final = 0o644
DIRECTORY_PERMISSIONS: Final = 0o755

FORBIDDEN_ARCHIVE_PARTS: Final = frozenset(
    {".git", ".github", ".env", ".venv", ".aws", "node_modules", "cdk.out", ".ruff_cache"}
)
"""Path segments that must never appear anywhere in an artifact.

Working directories, credential stores, and version-control state. None of them is ever a
legitimate part of any path, at any depth, in any package.
"""

FORBIDDEN_ARCHIVE_ROOTS: Final = frozenset(
    {"tests", "docs", "infra", "apps", "demo", "functions", "build"}
)
"""Repository directories that must never become a top-level entry in an artifact.

Checked at the **root only**, deliberately. Third-party packages legitimately ship their own
``tests`` and ``docs`` subpackages -- ``botocore/docs`` is imported by botocore itself -- so a
check at any depth would refuse a correct archive. What must never appear is this repository's
own test suite, documentation, CDK app, frontend, or Lambda sources, and each of those would
arrive as a top-level directory of exactly that name.

Not a substitute for the allowlist -- the allowlist is what decides the contents -- but a second,
differently shaped check, so a mistake would have to pass both a per-file declaration and a
categorical refusal.
"""


SCRIPT_DIRECTORY: Final = "bin"
HOST_EXECUTABLE_SUFFIXES: Final = (".exe", ".dll", ".pyd", ".bat")
"""Console-script launchers, and the extensions that give away a build-host binary.

``uv pip install --target`` resolves *wheels* for the declared platform but generates console
scripts for the **build host**, so a Windows build writes ``bin/opentelemetry-instrument.exe``
into a Linux/ARM64 archive. Those launchers cannot run on the runtime and ``bin`` is not on its
path in any case, so they are removed before packaging rather than shipped as dead weight --
and then refused by the inspection, so a future build that reintroduces them fails here rather
than at a cold start.

The consequence is recorded rather than papered over: the artifact starts with ``main.py`` as
an ordinary Python program. An entrypoint command that wraps it in ``opentelemetry-instrument``
needs that command resolvable in the runtime environment, which is a deployment question the
AgentCore runtime resource owns and this batch does not create.
"""


ARTIFACT_SCANNER_VERSION: Final = "artifact-secret-scan/v1"
"""The gate an artifact passed, named so a publish step can tell which one ran."""


class BuildError(RuntimeError):
    """The build cannot produce a correct artifact and refuses to produce an incorrect one."""


class UndecodableFirstPartyError(BuildError):
    """A first-party file could not be read as text, so it could not be scanned.

    A :class:`BuildError` subclass rather than a warning, because the alternative -- shipping a
    file the secret gate never inspected -- is the failure this whole gate exists to prevent.
    It carries the path and nothing from inside the file.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"first-party file is not decodable text and cannot be scanned: {name}")


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    """The parts of one ``runtime.toml`` the build reads."""

    agent: str
    name: str
    deployed_name: str
    python_version: str
    target_platform: str
    only_binary: bool
    root_entrypoint: str
    include: tuple[str, ...]

    @property
    def archive_name(self) -> str:
        return f"runtime-{self.agent}.zip"

    @property
    def archive_first_party(self) -> frozenset[str]:
        """Every archive entry this build copied out of the repository.

        The allowlist at its repo-relative path, plus the entrypoint at the archive root. It is
        derived from the manifest rather than guessed from a prefix, so a first-party file
        cannot become "vendored" -- and therefore eligible for a content exception -- by being
        written somewhere new.
        """

        return frozenset(self.include) | {ARCHIVE_ENTRYPOINT}


@dataclass(frozen=True, slots=True)
class BuiltArtifact:
    """One finished zip, described by what a publish step needs to know about it."""

    runtime: str
    deployed_name: str
    archive: str
    sha256: str
    size_bytes: int
    python_runtime: str
    target_platform: str
    entrypoint: str
    file_count: int
    security_scan: str = ARTIFACT_SCANNER_VERSION
    """Which gate this artifact passed, recorded as a fact and never as an authority.

    A publish step reads this to know a scan ran and which one; it does **not** read it instead
    of re-scanning. A manifest travels with the zip and a manifest can be edited, so the zip is
    what gets checked before it is published.
    """

    def as_record(self) -> dict[str, object]:
        return {
            "runtime": self.runtime,
            "deployed_name": self.deployed_name,
            "archive": self.archive,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "python_runtime": self.python_runtime,
            "target_platform": self.target_platform,
            "entrypoint": self.entrypoint,
            "file_count": self.file_count,
            "security_scan": self.security_scan,
        }


def load_manifest(agent: str, *, root: Path = RUNTIMES_ROOT) -> RuntimeManifest:
    """Read one manifest, refusing anything it does not fully declare."""

    path = root / agent / "runtime.toml"
    if not path.is_file():
        raise BuildError(f"no manifest for runtime {agent}")
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    runtime = document["runtime"]
    artifact = document["artifact"]
    packaging = document["packaging"]
    return RuntimeManifest(
        agent=agent,
        name=str(runtime["name"]),
        deployed_name=str(runtime["deployed_name"]),
        python_version=str(packaging["target_python"]),
        target_platform=str(packaging["target_platform"]),
        only_binary=bool(packaging["only_binary"]),
        root_entrypoint=str(artifact["root_entrypoint"]),
        include=tuple(str(item) for item in artifact["include"]),
    )


def export_command(output: Path, *, only_group: str | None = DEPENDENCY_GROUP) -> list[str]:
    """The lock-respecting export. ``--frozen`` is what makes it lock-respecting.

    ``only_group`` selects one locked dependency group -- ``agents`` for the AgentCore artifacts,
    its default. Pass ``None`` to export the project's **base** requirements instead, with every
    default group (``dev``/``test``/``agents``/``infra``) excluded: that is what the first-party
    Lambda artifacts need, since they ship ``boto3``/``fastapi``/``mangum`` and never Strands.
    """

    command = ["uv", "export", "--frozen"]
    if only_group is not None:
        command += ["--only-group", only_group]
    else:
        command += ["--no-default-groups"]
    command += [
        "--no-emit-project",
        "--no-hashes",
        "--no-annotate",
        "--no-header",
        "--output-file",
        str(output),
    ]
    return command


def install_command(manifest: RuntimeManifest, *, requirements: Path, target: Path) -> list[str]:
    """The cross-platform install. Every flag here is load-bearing; see the module docstring."""

    command = [
        "uv",
        "pip",
        "install",
        "--python-platform",
        manifest.target_platform,
        "--python-version",
        manifest.python_version,
        "--target",
        str(target),
        "--requirement",
        str(requirements),
    ]
    # The wheel cache is left on deliberately. It is content-addressed and the versions are
    # pinned by the lockfile, so it changes how long a build takes and not what it produces.
    if manifest.only_binary:
        command.extend(["--only-binary", ":all:"])
    return command


def stage_sources(manifest: RuntimeManifest, *, repository: Path, staging: Path) -> None:
    """Copy the allowlisted files, then the entrypoint, refusing anything undeclared."""

    for relative in manifest.include:
        source = repository / relative
        if not source.is_file():
            raise BuildError(f"{manifest.agent}: {relative} is declared but absent")
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    entrypoint = repository / manifest.root_entrypoint
    if not entrypoint.is_file():
        raise BuildError(f"{manifest.agent}: {manifest.root_entrypoint} is absent")
    shutil.copyfile(entrypoint, staging / ARCHIVE_ENTRYPOINT)


def archive_members(staging: Path) -> list[Path]:
    """Every file to write, in one stable order.

    Sorted by POSIX path rather than by whatever order the filesystem enumerates, so two builds
    of the same tree write the same entries in the same sequence.
    """

    return sorted(
        (path for path in staging.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(staging).as_posix(),
    )


def write_archive(staging: Path, destination: Path) -> tuple[str, int]:
    """Write the zip with normalised metadata; return its digest and entry count."""

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


def file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


ELF_MAGIC: Final = b"\x7fELF"
ELF_MACHINE_OFFSET: Final = 18
ELF_MACHINE_BY_PLATFORM: Final = {
    "aarch64-manylinux2014": 0xB7,
    "x86_64-manylinux2014": 0x3E,
    # The first-party Lambda artifacts target a newer glibc floor (``manylinux_2_28``) because
    # Pillow 12 no longer ships ``manylinux2014`` wheels, and the Lambda Python 3.12 runtime
    # (Amazon Linux 2023, glibc 2.34) satisfies it. Same processors, same ``e_machine`` values.
    "aarch64-manylinux_2_28": 0xB7,
    "x86_64-manylinux_2_28": 0x3E,
}
"""The ``e_machine`` value each supported target's compiled extensions must carry.

The strongest check available offline, and the one that would actually have caught a silently
mis-targeted build: a wheel filename can say anything, but the ELF header of the ``.so`` inside
it says what processor the code was compiled for. ``cryptography`` ships an ``abi3`` wheel whose
extension is named ``_rust.abi3.so`` with no platform tag at all, so a filename check alone
would pass over exactly the file most expensive to get wrong.
"""


SECRET_PATTERNS: Final = PATTERNS
"""The repository's own credential patterns, imported rather than restated.

A second copy would be the one that fell behind. ``tools/check_secrets.py`` is the definition of
what a secret looks like in this repository, and the artifact gate is a *second place it is
applied*, never a second answer to the question.
"""

PRIVATE_KEY_PATTERN: Final = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----"
)
"""Private-key material, detected by content rather than by extension.

Deliberately not a ban on ``.pem`` or ``.key``: ``certifi`` ships ``cacert.pem``, a bundle of
public CA **certificates** that every TLS call in the artifact depends on. Banning the extension
would refuse a correct archive and teach the next reader to add exceptions until the rule means
nothing. What must never ship is a private key, and a private key says so in its first line.
"""

SENSITIVE_BASENAMES: Final = frozenset(
    {"credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".pgpass", ".htpasswd"}
)
SENSITIVE_NAME_PREFIXES: Final = (".env",)
SENSITIVE_PATH_PARTS: Final = frozenset({".aws", ".ssh", ".gnupg", ".docker"})
"""Credential and configuration files refused by **name**, whatever their content holds.

An empty ``.env.production`` matches no pattern and is still a file that must never be in a
deployment artifact: its presence says a build reached into somewhere it should not have. The
prefix rule covers ``.env``, ``.env.local``, ``.env.production`` and every other suffix, and the
path rule covers a whole ``.aws/`` or ``.ssh/`` directory however deep it was copied from.
"""

VENDOR_CONTENT_EXCEPTIONS: Final = frozenset(
    {
        "boto3/examples/cloudfront.rst",
        "botocore/data/iam/2010-05-08/examples-1.json",
        "botocore/data/sts/2011-06-15/examples-1.json",
        "cryptography/hazmat/primitives/serialization/ssh.py",
    }
)
"""The four vendored files whose *documentation* is credential-shaped, named exactly.

``boto3``'s CloudFront example and ``botocore``'s IAM and STS example documents contain
placeholder access keys; ``cryptography``'s SSH parser contains the PEM header strings it
parses. Each is a fixture of a third-party package, each is reachable only by its exact path,
and each is listed here rather than waved through by a directory rule.

**This set is consulted only for entries the build did not put in the archive itself.** A
first-party file gets no content exception at any path, which is the property that makes the
gate worth having: a secret in ``main.py``, in ``runtimes/``, or in ``src/chorus/`` fails the
build however the exception list grows.
"""


def inspect_archive(
    path: Path,
    *,
    target_platform: str | None = None,
    first_party: frozenset[str] | None = None,
) -> list[str]:
    """Return every reason this archive is not shippable, or an empty list.

    ``first_party`` names the archive entries this build copied out of the repository. It
    defaults to ``None``, meaning *nothing is known to be vendored*, and every entry is then
    scanned with zero content exceptions -- so inspecting an archive whose provenance the caller
    cannot vouch for is the strictest case rather than the weakest.
    """

    problems: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        problems.extend(_native_module_problems(archive, names, target_platform))
        problems.extend(_secret_problems(archive, names, first_party))
    if ARCHIVE_ENTRYPOINT not in names:
        problems.append(f"{path.name}: no {ARCHIVE_ENTRYPOINT} at the archive root")
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


def sensitive_name_problem(name: str) -> str | None:
    """Refuse a credential file by its name, before anything is read."""

    parts = Path(name).parts
    if set(parts) & SENSITIVE_PATH_PARTS:
        return f"contains a credential directory: {name}"
    basename = parts[-1] if parts else name
    if basename in SENSITIVE_BASENAMES:
        return f"contains a credential file: {name}"
    if basename.startswith(SENSITIVE_NAME_PREFIXES):
        return f"contains an environment file: {name}"
    return None


def decode_first_party(name: str, data: bytes) -> str:
    """Decode a file this build put in the artifact, or refuse it.

    **This never returns "skip"** (Astra P2-B). Catching ``UnicodeDecodeError`` and returning no
    finding is how a first-party file evades the scanner entirely: Python accepts a module with
    a ``# -*- coding: latin-1 -*-`` declaration and non-UTF-8 bytes, imports it, runs it -- and a
    scanner that only tried UTF-8 saw nothing to report. A credential written that way shipped.

    Python source is decoded the way *Python* decodes it: :func:`tokenize.detect_encoding` reads
    the PEP 263 declaration (or the BOM) from the first two lines and answers with the encoding
    the interpreter itself will use, so the text scanned is the text that runs. Anything else
    first-party is UTF-8, because everything this repository ships is.

    A file that decodes under neither raises. There is no third answer: an artifact holding a
    first-party file nobody can read is not an artifact anybody should publish.
    """

    encoding = "utf-8"
    if name.endswith(".py"):
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        except SyntaxError as error:
            raise UndecodableFirstPartyError(name) from error
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError) as error:
        raise UndecodableFirstPartyError(name) from error


def scan_secret_matches(text: str) -> list[tuple[str, str]]:
    """Every credential-shaped match in ``text`` as ``(kind, matched_text)``.

    ``kind`` is ``"private-key"`` or ``"pattern-<index>"``. Used where a caller needs to reason
    about *which specific match* it is looking at -- e.g. to suppress one known third-party
    false positive by its exact substring while every other finding still fails (review P2-7).
    """

    out: list[tuple[str, str]] = []
    for match in PRIVATE_KEY_PATTERN.finditer(text):
        out.append(("private-key", match.group(0)))
    for index, pattern in enumerate(SECRET_PATTERNS):
        for match in pattern.finditer(text):
            out.append((f"pattern-{index}", match.group(0)))
    return out


def secret_content_problem(name: str, data: bytes, *, first_party: bool) -> str | None:
    """Refuse a file whose *content* is credential-shaped.

    The two classes are decoded differently, and deliberately so.

    **First-party** content is always read, through :func:`decode_first_party`, which honours a
    Python source encoding declaration and raises rather than skipping. Every first-party path
    is on a runtime's allowlist and every one of them is source or configuration, so "this is
    binary" is never a true answer here -- it is only ever a way to not look.

    **Vendored** content may genuinely be binary: a compiled extension is not text, and decoding
    one would report whatever byte sequence happened to resemble a pattern. Those are skipped by
    content and still checked by name, and their provenance is the lockfile and the ELF check.

    The finding names the file and the pattern index; it never quotes the matched value, for the
    same reason ``tools/check_secrets.py`` does not.
    """

    if first_party:
        text = decode_first_party(name, data)
    else:
        if name in VENDOR_CONTENT_EXCEPTIONS:
            return None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if PRIVATE_KEY_PATTERN.search(text):
        return f"contains private-key material: {name}"
    for index, pattern in enumerate(SECRET_PATTERNS):
        if pattern.search(text):
            return f"contains credential-shaped content (pattern {index}): {name}"
    return None


def _secret_problems(
    archive: zipfile.ZipFile, names: list[str], first_party: frozenset[str] | None
) -> list[str]:
    """Scan the final archive, member by member, for names and content that must not ship."""

    problems: list[str] = []
    for name in names:
        named = sensitive_name_problem(name)
        if named is not None:
            problems.append(named)
        is_first_party = first_party is None or name in first_party
        try:
            found = secret_content_problem(name, archive.read(name), first_party=is_first_party)
        except UndecodableFirstPartyError as error:
            # Reported rather than raised, so one inspection still returns every reason an
            # archive is unshippable instead of only the first. The build refuses either way.
            problems.append(str(error))
            continue
        if found is not None:
            problems.append(found)
    return problems


def scan_staged_first_party(staging: Path, relative_paths: Iterable[str]) -> list[str]:
    """Scan the repository files this build copied, before anything is packaged.

    Deliberately separate from the archive scan and deliberately run first, so a secret written
    into a source file is reported against the file a person can open rather than against a zip
    entry. There is no exception list here at all: every path this reads was put in the artifact
    by this build, from this repository -- and a file that cannot be decoded is a finding, never
    a file quietly passed over.
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


SHARED_OBJECT_RE: Final = re.compile(r"\.so(\.\d+)*$")
"""A compiled shared object -- ``_mod.cpython-312-x86_64-linux-gnu.so`` **and** versioned
libraries like ``libjpeg-*.so.62`` that a plain ``endswith('.so')`` would miss (review P2-7)."""


def is_shared_object(name: str) -> bool:
    return bool(SHARED_OBJECT_RE.search(name))


def _native_module_problems(
    archive: zipfile.ZipFile, names: list[str], target_platform: str | None
) -> list[str]:
    """Read each compiled extension's ELF header and check what it was built for."""

    if target_platform is None:
        return []
    expected = ELF_MACHINE_BY_PLATFORM.get(target_platform)
    if expected is None:
        return [f"no ELF machine is recorded for target platform {target_platform}"]
    problems: list[str] = []
    for name in sorted(name for name in names if is_shared_object(name)):
        header = archive.read(name)[: ELF_MACHINE_OFFSET + 2]
        if header[:4] != ELF_MAGIC:
            problems.append(f"{name} is not an ELF object")
            continue
        machine = int.from_bytes(header[ELF_MACHINE_OFFSET : ELF_MACHINE_OFFSET + 2], "little")
        if machine != expected:
            problems.append(f"{name} was compiled for machine {machine:#04x}, not {expected:#04x}")
    return problems


def build_runtime(
    agent: str,
    *,
    repository: Path = REPOSITORY_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    runner: CommandRunner | None = None,
) -> BuiltArtifact:
    """Build one artifact end to end and return its record."""

    manifest = load_manifest(agent, root=repository / "runtimes")
    staging = output_root / "staging" / agent
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    requirements = output_root / "staging" / f"{agent}-requirements.txt"
    requirements.parent.mkdir(parents=True, exist_ok=True)

    _run(export_command(requirements), cwd=repository, runner=runner)
    _run(
        install_command(manifest, requirements=requirements, target=staging),
        cwd=repository,
        runner=runner,
    )
    _purge_host_artifacts(staging)
    stage_sources(manifest, repository=repository, staging=staging)

    # First gate: the repository files this build just copied, reported against their own paths.
    staged = scan_staged_first_party(staging, manifest.archive_first_party)
    if staged:
        raise BuildError(f"{agent}: " + "; ".join(staged))

    archive = output_root / manifest.archive_name
    digest, count = write_archive(staging, archive)
    # Second gate: the finished zip, because staging is not what gets published. A builder bug,
    # a post-staging mutation, or a file added between the two would be invisible to the first
    # scan and is exactly what this one is for.
    problems = inspect_archive(
        archive,
        target_platform=manifest.target_platform,
        first_party=manifest.archive_first_party,
    )
    if problems:
        raise BuildError("; ".join(problems))
    return BuiltArtifact(
        runtime=manifest.name,
        deployed_name=manifest.deployed_name,
        archive=manifest.archive_name,
        sha256=digest,
        size_bytes=archive.stat().st_size,
        python_runtime=f"PYTHON_{manifest.python_version.replace('.', '_')}",
        target_platform=manifest.target_platform,
        entrypoint=ARCHIVE_ENTRYPOINT,
        file_count=count,
    )


def _purge_host_artifacts(staging: Path) -> None:
    """Remove what the install left behind for the *build* machine rather than the runtime.

    ``.pyc`` files are compiled for the build interpreter and would be stale or wrong in the
    runtime. ``bin/`` holds console-script launchers generated for the build host, which on
    Windows are ``.exe`` stubs a Linux/ARM64 runtime cannot execute. ``dist-info`` records stay,
    because a package's metadata is how it is found.
    """

    for cache in sorted(staging.rglob("__pycache__"), reverse=True):
        shutil.rmtree(cache, ignore_errors=True)
    shutil.rmtree(staging / SCRIPT_DIRECTORY, ignore_errors=True)


def _run(command: list[str], *, cwd: Path, runner: CommandRunner | None) -> None:
    """Run one build command, or hand it to an injected runner.

    ``runner`` exists so a test can assert the exact argv -- the platform, the Python version,
    the lock flag -- without downloading a dependency set to prove it.
    """

    if runner is not None:
        runner(command, cwd)
        return
    completed = subprocess.run(command, cwd=cwd, check=False)  # noqa: S603 - fixed argv, no shell
    if completed.returncode != 0:
        raise BuildError(f"build command failed: {command[0]} {command[1]}")


def write_manifest_file(artifacts: list[BuiltArtifact], *, output_root: Path) -> Path:
    """Record what was built, for the publish stage that uploads it.

    Deliberately a file and not a service. The publish step needs a name, a digest, a Python
    runtime, and an entrypoint per artifact; anything more would be an artifact registry, which
    is not a thing this system has decided to have.
    """

    path = output_root / "artifacts.json"
    document = {
        "schema": "agentcore-artifacts/v1",
        "artifacts": [artifact.as_record() for artifact in artifacts],
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", action="append", choices=RUNTIME_NAMES, default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
    arguments = parser.parse_args(argv)
    selected = tuple(arguments.runtime) if arguments.runtime else RUNTIME_NAMES

    built: list[BuiltArtifact] = []
    for agent in selected:
        try:
            built.append(build_runtime(agent, output_root=arguments.output))
        except BuildError as error:
            sys.stderr.write(f"build failed for {agent}: {error}\n")
            return 1
    manifest_path = write_manifest_file(built, output_root=arguments.output)
    for artifact in built:
        sys.stdout.write(f"{artifact.archive} {artifact.sha256} {artifact.file_count} files\n")
    sys.stdout.write(f"manifest: {manifest_path}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - the command line
    # Run as a module -- ``uv run python -m tools.build_runtime_artifacts`` -- so the repository
    # root is on the path and ``tools.check_secrets`` resolves. Running the file by path puts
    # ``tools/`` on the path instead, and the secret patterns this build gates on would not be
    # importable; failing at import is the right answer there, not a second copy of them.
    raise SystemExit(main())
