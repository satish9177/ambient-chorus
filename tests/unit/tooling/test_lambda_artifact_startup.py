"""Can an unpacked Lambda ZIP resolve and import its own handler -- using its **own**
dependency tree, not the host's? Asked of the five real artifacts the session build fixture
produces (review P2-5, P2-6; deployment contract SS 9, SS 47).

Each archive is unpacked into a directory outside the repository, with the repository checkout,
its ``.venv``, and every host ``site-packages`` off ``sys.path``, and AWS credential resolution
disabled.

Three probes:

* **resolution** (every platform) -- only the archive root on ``sys.path``; ``find_spec`` proves
  the handler module, ``chorus``, and ``functions.envelope`` resolve out of the archive and
  nowhere else. Needs no compiled extension.
* **self-contained import** -- ``python -S -E`` with ``sys.path`` rebuilt to **only** the
  extracted archive plus the interpreter's own standard-library directories (``stdlib``,
  ``platstdlib``, and the compiled ``lib-dynload`` beneath it -- discovered via ``sysconfig``).
  The repository checkout, ``.venv``, host site-packages, and user site are all excluded, so
  third-party and first-party modules (including compiled ones like ``pydantic_core._pydantic_core``
  and, for the compiler, ``PIL``) come from the **archive** while ``importlib`` / ``json`` /
  ``pathlib`` come from **Python itself**. On Linux/x86-64 this really runs; on another platform
  the archive's Linux ``.so`` files cannot load, so the probe is a ``sys.platform`` skip (never
  a "not built" skip) and the structural checks below run instead.
* **structural native check** (every platform) -- the archive actually contains the compiled
  extensions each closure needs (``pydantic_core``; ``PIL`` for the compiler).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from tools import build_lambda_artifacts as build

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
STARTUP_TIMEOUT_SECONDS = 180
LINUX_X86 = sys.platform.startswith("linux") and sys.maxsize > 2**32

RESOLUTION_PROBE = """
import importlib.util, json, sys
from pathlib import Path
root = Path.cwd().resolve()
sys.path[:] = [str(root)]
handler_module = sys.argv[1]
origins = {}
for name in (handler_module, "chorus", "chorus.settings", "functions.envelope"):
    spec = importlib.util.find_spec(name)
    origin = None if spec is None or spec.origin is None else str(Path(spec.origin).resolve())
    origins[name] = origin
sys.stdout.write("PROBE:" + json.dumps({"origins": origins}))
"""

SELF_CONTAINED_PROBE = r"""
import json, socket, sys, sysconfig
from pathlib import Path

root = str(Path.cwd().resolve())
handler_module = sys.argv[1]
repo = str(Path(sys.argv[2]).resolve())
extras = sys.argv[3:]

# Under -S -E there is no site-packages, no .pth processing (so no editable install), and no
# PYTHONPATH. Rebuild sys.path from ONLY the extracted archive plus the interpreter's own
# standard-library directories -- stdlib, platstdlib, the compiled lib-dynload beneath them,
# and the frozen-stdlib zip -- and nothing from the repository checkout, .venv, host
# site-packages, or user site.
_std = {sysconfig.get_path("stdlib"), sysconfig.get_path("platstdlib"), sys.base_prefix}
_std_resolved = tuple(str(Path(p).resolve()) for p in _std if p)
_bad = ("site-packages", "dist-packages", ".venv", "site-python")
kept = []
for entry in sys.path:
    if not entry or Path(entry).resolve() == Path(root):
        continue
    rp = str(Path(entry).resolve())
    if any(marker in rp for marker in _bad):
        continue
    if repo and (rp == repo or rp.startswith(repo + "/") or rp.startswith(repo + "\\")):
        continue
    if rp.startswith(_std_resolved) or "lib-dynload" in rp or rp.endswith(".zip"):
        kept.append(entry)  # a stdlib / lib-dynload / frozen-stdlib-zip directory
sys.path[:] = [root, *kept]

# Any outbound socket during import is a failure.
def _blocked(self, *a, **k):
    raise AssertionError("import attempted a network connection")
socket.socket.connect = _blocked

import importlib
mod = importlib.import_module(handler_module)
import chorus.settings
import pydantic_core
from pydantic_core import _pydantic_core  # the compiled extension

def _origin(module):
    return str(Path(getattr(module, "__file__", "") or "").resolve())

third_party = {"pydantic_core": _origin(pydantic_core), "_pydantic_core": _origin(_pydantic_core)}
for name in extras:
    third_party[name] = _origin(importlib.import_module(name))

stdlib = {}
for name in ("importlib", "json", "pathlib"):
    stdlib[name] = _origin(importlib.import_module(name))

cache = getattr(mod, "_composition", getattr(mod, "_adapter", "missing"))
sys.stdout.write("PROBE:" + json.dumps({
    "handler_callable": callable(mod.handler),
    "handler_file": _origin(mod),
    "chorus_file": _origin(chorus.settings),
    "composition_cache_is_none": cache is None,
    "third_party": third_party,
    "stdlib": stdlib,
    "stdlib_dirs": list(_std_resolved),
    "sys_path": sys.path,
}))
"""

CREDENTIAL_FREE = {
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_ACCESS_KEY_ID": "",
    "AWS_SECRET_ACCESS_KEY": "",
    "AWS_SESSION_TOKEN": "",
    "AWS_PROFILE": "",
    "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_CONFIG_FILE": os.devnull,
}

# Compiled extensions each function's closure must actually carry in the archive.
NATIVE_EXTRAS = {"compiler": ("PIL",)}


@pytest.fixture(params=build.FUNCTION_DIRS)
def directory(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def manifest(directory: str) -> build.LambdaManifest:
    return build.load_lambda_manifest(directory)


@pytest.fixture
def extracted(directory: str, built_artifact_paths: dict[str, Path], tmp_path: Path) -> Path:
    destination = tmp_path / "unpacked"
    destination.mkdir()
    with zipfile.ZipFile(built_artifact_paths[directory]) as archive:
        archive.extractall(destination)
    assert REPOSITORY_ROOT not in destination.parents
    return destination


def _run(
    cwd: Path, source: str, *arguments: str, flags: list[str]
) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    environment.update(CREDENTIAL_FREE)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, source from this file
        [sys.executable, *flags, "-c", source, *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT_SECONDS,
        check=False,
    )


def _report(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert completed.returncode == 0, completed.stderr
    assert "ModuleNotFoundError" not in completed.stderr, completed.stderr
    assert "PROBE:" in completed.stdout, f"{completed.stdout}\n{completed.stderr}"
    parsed: dict[str, object] = json.loads(completed.stdout.split("PROBE:", 1)[1])
    return parsed


def test_the_archive_resolves_its_handler_with_only_the_archive_on_the_path(
    extracted: Path, manifest: build.LambdaManifest
) -> None:
    report = _report(_run(extracted, RESOLUTION_PROBE, manifest.handler_module, flags=["-E"]))
    origins = report["origins"]
    assert isinstance(origins, dict)
    for name, origin in origins.items():
        assert origin is not None, f"{name} did not resolve inside the archive"
        assert extracted in Path(origin).parents, f"{name} resolved to {origin}, outside it"


def test_the_archive_carries_the_compiled_extensions_its_closure_needs(
    directory: str, built_artifact_paths: dict[str, Path]
) -> None:
    with zipfile.ZipFile(built_artifact_paths[directory]) as archive:
        names = archive.namelist()
    assert any(n.startswith("pydantic_core/") and n.endswith(".so") for n in names)
    for pkg in NATIVE_EXTRAS.get(directory, ()):
        assert any(n.startswith(f"{pkg}/") for n in names), f"{directory} archive lacks {pkg}"


@pytest.mark.skipif(
    not LINUX_X86,
    reason=f"the archive's wheels are Linux/x86_64; {sys.platform} runs structural checks only",
)
def test_the_handler_imports_with_the_artifacts_own_dependency_tree(
    extracted: Path, directory: str, manifest: build.LambdaManifest
) -> None:
    """``python -S -E`` with ``sys.path`` rebuilt to **only** the extracted archive plus the
    interpreter's own standard-library directories -- no host site-packages, no repo checkout,
    no ``.venv``. First-party (``functions.<x>.handler``, ``chorus``) and third-party
    (``pydantic_core`` + its compiled ``_pydantic_core``; ``PIL`` for the compiler) resolve from
    the **archive**; ``importlib`` / ``json`` / ``pathlib`` resolve from the **stdlib**; and no
    network connection is made (review R2 / P2-6)."""

    report = _report(
        _run(
            extracted,
            SELF_CONTAINED_PROBE,
            manifest.handler_module,
            str(REPOSITORY_ROOT),
            *NATIVE_EXTRAS.get(directory, ()),
            flags=["-S", "-E"],
        )
    )
    assert report["handler_callable"] is True
    assert report["composition_cache_is_none"] is True

    for key in ("handler_file", "chorus_file"):
        loaded = report[key]
        assert isinstance(loaded, str) and extracted in Path(loaded).parents

    third_party = report["third_party"]
    assert isinstance(third_party, dict)
    for name, path in third_party.items():
        assert isinstance(path, str) and path, name
        assert extracted in Path(path).parents, f"{name} loaded from {path}, not the archive"
        assert "site-packages" not in path and ".venv" not in path
        assert str(REPOSITORY_ROOT) not in path

    stdlib_dirs = report["stdlib_dirs"]
    assert isinstance(stdlib_dirs, list) and stdlib_dirs
    stdlib = report["stdlib"]
    assert isinstance(stdlib, dict)
    for name, path in stdlib.items():
        assert isinstance(path, str) and path, name
        assert any(path.startswith(d) for d in stdlib_dirs), f"{name} is {path}, not a stdlib dir"
        assert extracted not in Path(path).parents, f"{name} came from the archive"
