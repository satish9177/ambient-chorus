"""Small deterministic credential-pattern scan for source-controlled files."""

from __future__ import annotations

import re
from pathlib import Path

PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:secret_access_key|api_key|access_token)\s*[:=]\s*['\"][^'\"]{8,}"),
)
IGNORED_PARTS = {".git", ".local", ".tools", ".venv", "node_modules", "cdk.out", "build", "dist"}
"""Directories that hold no source-controlled file.

``build`` is on the list because the AgentCore artifact build unpacks the locked dependency set
under ``build/agentcore/``, and third-party packages ship credential-shaped *example* text --
``botocore``'s IAM and STS example documents, ``cryptography``'s SSH key parser. Scanning them
reports a vendored wheel's documentation as a committed secret, which is a false positive that
teaches a reader to ignore this check. What the check is for is source under version control,
and every ignored name here is a build or tool directory Git does not track.
"""


def findings(root: Path) -> list[str]:
    """Return paths containing credential-shaped values, never the values themselves."""

    matches: list[str] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        if any(part in IGNORED_PARTS for part in path.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(pattern.search(content) for pattern in PATTERNS):
            matches.append(str(path.relative_to(root)))
    return matches


def main() -> int:
    """Scan the current repository for committed credential patterns."""

    root = Path(__file__).resolve().parents[1]
    matches = findings(root)
    if matches:
        print("Credential-shaped content found in:")
        for match in matches:
            print(f"- {match}")
        return 1
    print("No credential patterns found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
