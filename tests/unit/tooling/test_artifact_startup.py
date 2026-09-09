"""Can an unpacked artifact actually resolve its own code? Asked of a real archive, from outside.

Every other packaging test reads the zip. These run it, because the failure they exist to catch
is invisible to reading: the archive preserves repo-relative paths, so ``chorus`` lands under
``src/`` while ``python main.py`` puts only the archive **root** on ``sys.path``. Every import
check that ran inside this repository passed anyway, because the editable install had already
made ``chorus`` importable -- which is precisely how a build ships an artifact that cannot start.

Two probes, because one cannot answer both halves
--------------------------------------------------
The archive's wheels are **Linux/ARM64 by design**, so ``pydantic_core``'s compiled extension
cannot load on this build machine. Executing the artifact's full import graph here is therefore
impossible, and a probe that pretended otherwise would be testing the host's packages.

* **The layout probe** runs with ``-S -E``: no site-packages, no environment, nothing on the
  path but the archive itself. It proves the thing the defect was about -- that ``chorus``,
  ``chorus.contracts.common`` and the runtime modules *resolve*, out of the archive and from
  nowhere else -- using ``find_spec``, which needs no native extension. Every negative case
  belongs here, because here there is nothing else that could satisfy an import.
* **The execution probe** lets the host interpreter supply third-party packages and then really
  runs ``main.py``, asserting the module built its ``AgentCoreServer``. It proves the entrypoint
  executes end to end; it says nothing about the shipped wheels, which the ELF inspection in
  ``test_artifact_build.py`` proves instead.

``serve()`` is never called. It binds a socket and blocks forever, so a probe that reached it
would hang rather than fail. ``main.py`` is run under ``runpy`` with a ``run_name`` other than
``__main__``, which covers every import the deployed process performs before it listens.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from tools import build_runtime_artifacts as build

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ARTIFACT_ROOT = build.DEFAULT_OUTPUT_ROOT

STARTUP_TIMEOUT_SECONDS = 120
"""Bounded, because an unbounded probe of a process that may bind a socket is a hung suite."""

LAYOUT_PROBE = """
import importlib.util, json, sys
from pathlib import Path

archive_root = Path.cwd().resolve()
agent = sys.argv[1]

from runtimes.bootstrap import ensure_shared_package_importable

added = ensure_shared_package_importable(archive_root)

import runtimes.server

# ``find_spec`` executes a module's *parent packages*, so only packages that import nothing
# heavier than the standard library can be resolved here: `runtimes/__init__.py`,
# `chorus/__init__.py` and `chorus/contracts/__init__.py` are all docstrings. Resolving
# `runtimes.<agent>.entrypoint` would execute `runtimes/<agent>/__init__.py`, which reaches
# pydantic and therefore a Linux/ARM64 extension this machine cannot load -- so that module is
# checked by path here and actually executed by the second probe.
origins = {}
for name in ("chorus", "chorus.contracts.common", "runtimes", "runtimes.server"):
    spec = importlib.util.find_spec(name)
    found = None if spec is None or spec.origin is None else str(Path(spec.origin).resolve())
    origins[name] = found

entrypoint = archive_root / "runtimes" / agent / "entrypoint.py"

report = {
    "added": None if added is None else str(added),
    "origins": origins,
    "entrypoint_present": entrypoint.is_file(),
    "sys_path_head": sys.path[:3],
    "server_file": str(Path(runtimes.server.__file__).resolve()),
    "port": runtimes.server.PORT,
}
sys.stdout.write("PROBE:" + json.dumps(report))
"""

EXECUTION_PROBE = """
import json, runpy, sys
from pathlib import Path

archive_root = Path.cwd().resolve()
module = runpy.run_path(str(archive_root / "main.py"), run_name="chorus_artifact_probe")

import chorus
import chorus.contracts.common

report = {
    "app": type(module["app"]).__name__,
    "handler_module": module["app"].handler.__module__,
    "chorus_file": str(Path(chorus.__file__).resolve()),
    "entrypoint_file": str(Path(sys.modules[module["app"].handler.__module__].__file__).resolve()),
}
sys.stdout.write("PROBE:" + json.dumps(report))
"""


def _artifact(agent: str) -> Path:
    path = ARTIFACT_ROOT / f"runtime-{agent}.zip"
    if not path.is_file():
        pytest.skip(
            f"{path} is not built; run `uv run python -m tools.build_runtime_artifacts` first"
        )
    return path


@pytest.fixture(params=build.RUNTIME_NAMES)
def agent(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def extracted(agent: str, tmp_path: Path) -> Path:
    """One artifact, unpacked outside the repository, as AgentCore would mount it."""

    destination = tmp_path / agent
    destination.mkdir()
    with zipfile.ZipFile(_artifact(agent)) as archive:
        archive.extractall(destination)
    assert (destination / "main.py").is_file()
    assert REPOSITORY_ROOT not in destination.parents
    return destination


def _run(
    extracted: Path, source: str, *arguments: str, isolated: bool
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    flags = ["-S", "-E"] if isolated else []
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, source supplied by this file
        [sys.executable, *flags, "-c", source, *arguments],
        cwd=extracted,
        env=environment,
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT_SECONDS,
        check=False,
    )


def _report(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert completed.returncode == 0, completed.stderr
    assert "ModuleNotFoundError" not in completed.stderr, completed.stderr
    marker = "PROBE:"
    assert marker in completed.stdout, f"{completed.stdout}\n{completed.stderr}"
    parsed: dict[str, object] = json.loads(completed.stdout.split(marker, 1)[1])
    return parsed


# -- the layout, with nothing on the path but the archive -------------------------------------


def test_the_archive_resolves_its_own_code_with_nothing_else_available(
    extracted: Path, agent: str
) -> None:
    """``-S -E``: no site-packages, no environment, no repository. Only the zip.

    This is the exact condition the defect was found under, and the exact condition a deployed
    runtime starts in.
    """

    completed = _run(extracted, LAYOUT_PROBE, agent, isolated=True)
    report = _report(completed)

    origins = report["origins"]
    assert isinstance(origins, dict)
    for name, origin in origins.items():
        assert origin is not None, f"{name} did not resolve inside the archive"
        assert extracted in Path(origin).parents, (
            f"{name} resolved to {origin}, outside the archive"
        )

    assert report["entrypoint_present"] is True
    assert report["added"] == str(extracted / "src"), "the bootstrap must add the archive's own src"
    assert report["port"] == 8080


def test_the_archive_needs_no_repository_directory_on_the_path(extracted: Path, agent: str) -> None:
    """Stated as a property of what the child could see, not as a hope about the environment."""

    completed = _run(extracted, LAYOUT_PROBE, agent, isolated=True)
    report = _report(completed)

    head = report["sys_path_head"]
    assert isinstance(head, list)
    for entry in head:
        assert isinstance(entry, str)
        if not entry:
            continue
        resolved = Path(entry).resolve()
        assert resolved != REPOSITORY_ROOT
        assert REPOSITORY_ROOT not in resolved.parents


def test_the_artifact_fails_loudly_when_its_source_tree_is_missing(
    extracted: Path, agent: str
) -> None:
    """The bootstrap's guard has to be able to fire, or it is decoration.

    Removing ``src/`` reproduces the original defect exactly. Under ``-S -E`` there is nothing
    else that could supply ``chorus``, so the artifact must refuse to start with a message that
    names the layout rather than raising ``ModuleNotFoundError`` from somewhere deeper.
    """

    shutil.rmtree(extracted / "src")

    completed = _run(extracted, LAYOUT_PROBE, agent, isolated=True)

    assert completed.returncode != 0
    assert "ArchiveLayoutError" in completed.stderr
    assert "not importable" in completed.stderr


def test_the_bootstrap_is_a_no_op_where_there_is_no_source_tree_beside_it() -> None:
    """In this repository ``main.py`` sits inside its package and has no ``src`` sibling.

    The same file has to work in both layouts, and the guard is what decides: it adds a
    directory only when one is there, and verifies the package is reachable either way.
    """

    from runtimes.bootstrap import ensure_shared_package_importable

    assert ensure_shared_package_importable(REPOSITORY_ROOT / "runtimes" / "monitor") is None


def test_the_bootstrap_refuses_a_layout_it_cannot_import_from(tmp_path: Path) -> None:
    import importlib.util
    from unittest.mock import patch

    from runtimes.bootstrap import ArchiveLayoutError, ensure_shared_package_importable

    with (
        patch.object(importlib.util, "find_spec", return_value=None),
        pytest.raises(ArchiveLayoutError, match="not importable"),
    ):
        ensure_shared_package_importable(tmp_path)


# -- the entrypoint actually running ----------------------------------------------------------


@pytest.fixture
def first_party_view(extracted: Path, tmp_path: Path) -> Path:
    """The archive's own first-party files, with its vendored packages set aside.

    The archive's ``pydantic`` is a Linux/ARM64 build and sits at the archive root, so it
    *shadows* the host's copy and no amount of path ordering makes the full graph importable on
    this machine. Moving the vendored packages out separates the two questions cleanly: this
    view holds the exact ``main.py``, ``runtimes/`` and ``src/`` bytes that came out of the zip,
    and the interpreter supplies the third-party packages -- whose shipped ARM64 builds are
    proved by the ELF inspection in ``test_artifact_build.py``, not here.
    """

    view = tmp_path / "first-party"
    view.mkdir()
    shutil.copyfile(extracted / "main.py", view / "main.py")
    for directory in ("runtimes", "src"):
        shutil.copytree(extracted / directory, view / directory)
    return view


def test_main_py_executes_and_builds_this_runtime_s_application(
    first_party_view: Path, agent: str
) -> None:
    """``main.py`` run as a script, reaching the server binding.

    What is proved here is that ``chorus`` and the runtime modules come out of the **artifact**:
    the archive's own bootstrap puts its ``src`` ahead of everything, and the assertions below
    check the files that were actually loaded rather than trusting the ordering. Without the
    bootstrap this is the exact call that raised ``ModuleNotFoundError: No module named
    'chorus'``.
    """

    completed = _run(first_party_view, EXECUTION_PROBE, isolated=False)
    report = _report(completed)

    assert report["app"] == "AgentCoreServer"
    assert report["handler_module"] == f"runtimes.{agent}.entrypoint"
    for key in ("chorus_file", "entrypoint_file"):
        loaded = report[key]
        assert isinstance(loaded, str)
        assert first_party_view in Path(loaded).parents, f"{key} was loaded from {loaded}"


def test_every_artifact_carries_the_bootstrap_and_the_package_markers(agent: str) -> None:
    """The layout the bootstrap depends on is declared, so it cannot drift silently."""

    manifest = build.load_manifest(agent)

    assert "runtimes/bootstrap.py" in manifest.include
    assert "src/chorus/__init__.py" in manifest.include, (
        "the archive's package layout must match the repository's, not rely on a namespace package"
    )
    with zipfile.ZipFile(_artifact(agent)) as archive:
        names = set(archive.namelist())
    assert "runtimes/bootstrap.py" in names
    assert "src/chorus/__init__.py" in names
    assert "src/chorus/contracts/__init__.py" in names
