"""Fail when a local Markdown link in the engineering source of truth is broken."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

LINK = re.compile(r"(?<!!)\[[^]]*]\(([^)]+)\)")


def broken_links(root: Path) -> list[str]:
    """Return deterministic descriptions of unresolved local documentation links."""

    failures: list[str] = []
    for document in sorted((root / "docs").rglob("*.md")):
        text = document.read_text(encoding="utf-8")
        for raw_target in LINK.findall(text):
            target = raw_target.split("#", maxsplit=1)[0].strip().strip("<>")
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (document.parent / unquote(target)).resolve()
            if not resolved.exists():
                failures.append(f"{document.relative_to(root)} -> {target}")
    return failures


def main() -> int:
    """Check links beneath the repository containing this script."""

    root = Path(__file__).resolve().parents[1]
    failures = broken_links(root)
    if failures:
        print("Broken documentation links:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Architecture documentation links are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
