from __future__ import annotations

import pytest
from pydantic import ValidationError

from chorus.settings import Settings


def test_settings_unknown_chorus_variable_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_UNKNOWN_SETTING", "unsafe")

    with pytest.raises(ValueError, match="unknown CHORUS"):
        Settings.load()


def test_settings_production_value_fails() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production")
