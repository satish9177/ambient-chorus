"""Make ``chorus`` importable from inside an unpacked direct-code archive.

The archive preserves repo-relative paths, so the shared package lands at ``src/chorus/...``
while ``main.py`` sits at the root. Running ``python main.py`` puts the **root** on ``sys.path``
and nothing else, so ``runtimes.<agent>`` resolves and ``chorus`` does not:

    ModuleNotFoundError: No module named 'chorus'

The repository does not hit this because the editable install already exposes ``src`` as a
source root. The deployed artifact has no install, no ``.venv``, no ``PYTHONPATH``, and no
working directory it may rely on -- so the one directory that has to be on the path is added
here, computed from the archive's own layout and from nothing else.

Why a helper rather than three copies
-------------------------------------
The same four lines in three ``main.py`` files is three places for the layout to drift from the
manifests. This module can be imported before the fix is applied because it imports only the
standard library: ``runtimes/__init__.py`` and ``runtimes/bootstrap.py`` both sit at the archive
root, so ``runtimes.bootstrap`` resolves on the path Python has already set up, and nothing it
touches reaches ``chorus``.

Why flattening the package instead was rejected
------------------------------------------------
Copying ``src/chorus`` to the archive root would remove the bootstrap and break the frozen
import layout: the deployment contract's ``[artifact].include`` allowlist is a set of
repo-relative paths preserved verbatim, and the promise attached to it is that ``runtimes.<agent>``
and ``chorus.contracts`` resolve inside the zip exactly as they do in the repository. A second
copy at a different path would make the deployed import graph one this repository's tests have
never exercised.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Final

SOURCE_DIRECTORY: Final = "src"
"""The archive-relative directory holding the shared package, matching the repository layout."""

SHARED_PACKAGE: Final = "chorus"


class ArchiveLayoutError(ImportError):
    """The artifact cannot import its own shared package, so it must not start.

    Raised at start-up rather than at first invocation. A runtime that accepted a request and
    then failed to import a contract would answer with an unclassified failure for every call;
    failing here means the deployment surfaces as a cold-start error, which is what it is.
    """


def ensure_shared_package_importable(archive_root: Path) -> Path | None:
    """Put the archive's ``src`` directory on the path, and prove the package is reachable.

    Returns the directory that was added, or ``None`` when none was needed -- which is the
    repository case, where ``main.py`` sits inside ``runtimes/<agent>/`` and has no ``src``
    sibling, and where the editable install has already made ``chorus`` importable.

    Idempotent: importing ``main`` twice, or running under a server that reloads it, adds
    nothing a second time.
    """

    source_root = archive_root / SOURCE_DIRECTORY
    added: Path | None = None
    if source_root.is_dir() and str(source_root) not in sys.path:
        # Ahead of everything else: the artifact's own copy of the shared package is the one it
        # must run, never a same-named package that happens to be installed beside it.
        sys.path.insert(0, str(source_root))
        added = source_root
    if importlib.util.find_spec(SHARED_PACKAGE) is None:
        raise ArchiveLayoutError(
            f"{SHARED_PACKAGE!r} is not importable from {archive_root}; "
            f"the artifact is missing its {SOURCE_DIRECTORY}/ tree"
        )
    return added


__all__ = [
    "SHARED_PACKAGE",
    "SOURCE_DIRECTORY",
    "ArchiveLayoutError",
    "ensure_shared_package_importable",
]
