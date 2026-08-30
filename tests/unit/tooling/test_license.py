from pathlib import Path

from tools.check_license import license_errors


def test_repository_declares_and_contains_mit_license() -> None:
    repository_root = Path(__file__).resolve().parents[3]

    assert license_errors(repository_root) == ()
