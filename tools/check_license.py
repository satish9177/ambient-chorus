"""Fail when the repository or package metadata loses its required MIT license."""

from __future__ import annotations

import tomllib
from pathlib import Path

MIT_MARKERS = (
    "MIT License",
    "Permission is hereby granted, free of charge",
    'THE SOFTWARE IS PROVIDED "AS IS"',
)


def license_errors(repository_root: Path) -> tuple[str, ...]:
    """Return stable configuration errors without exposing unrelated file content."""

    errors: list[str] = []
    license_path = repository_root / "LICENSE"
    if not license_path.is_file():
        errors.append("root LICENSE file is missing")
    else:
        license_text = license_path.read_text(encoding="utf-8")
        if any(marker not in license_text for marker in MIT_MARKERS):
            errors.append("root LICENSE is not the expected MIT license text")

    metadata = tomllib.loads((repository_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata.get("project")
    if not isinstance(project, dict):
        errors.append("pyproject project metadata is missing")
        return tuple(errors)
    if project.get("license") != "MIT":
        errors.append("pyproject project.license must be the MIT SPDX expression")
    license_files = project.get("license-files")
    if license_files != ["LICENSE"]:
        errors.append("pyproject project.license-files must contain only LICENSE")
    return tuple(errors)


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    errors = license_errors(repository_root)
    if errors:
        for error in errors:
            print(f"License check failed: {error}")
        return 1
    print("MIT license and package metadata are present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
