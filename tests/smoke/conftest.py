"""Shared fixtures for the Phase 10 local-composition smoke suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chorus.composition.local import LocalComposition, build_local
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.settings import Environment, Settings

PRESENTER = {"X-Chorus-Demo-Actor": "presenter_admin"}
APPROVER = {"X-Chorus-Demo-Actor": "case_approver"}


def actor(name: str) -> dict[str, str]:
    return {"X-Chorus-Demo-Actor": name}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def composition(tmp_path: Path) -> LocalComposition:
    return build_local(
        Settings(environment=Environment.DEVELOPMENT, local_data_dir=tmp_path),
        storage=InMemoryStorageDriver(),
    )


@pytest.fixture
def client(composition: LocalComposition) -> Iterator[TestClient]:
    from chorus_api.main import build_app

    app = build_app(composition.container)
    with TestClient(app) as test_client:
        yield test_client
