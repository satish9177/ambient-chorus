from __future__ import annotations

from pathlib import Path

from tools.check_architecture_links import broken_links


def test_architecture_documentation_links_resolve() -> None:
    root = Path(__file__).resolve().parents[3]

    assert broken_links(root) == []
