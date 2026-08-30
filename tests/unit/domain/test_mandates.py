from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from tests.fixtures.elevator import build_elevator_fixture


def test_mandate_version_is_immutable() -> None:
    mandate = build_elevator_fixture().context.mandates[0]

    with pytest.raises(FrozenInstanceError):
        setattr(mandate, "version", 2)  # noqa: B010
