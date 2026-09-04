"""A wired Phase 3 API over each storage driver under contract.

The application is built the way a composition root builds it: real use cases, real
repositories, and a dispatcher chosen explicitly so a test can decide whether the Monitor runs
inside the request or is merely handed over.

It is parametrized over the drivers rather than pinned to the in-memory one. The route's
command idempotency is now a two-phase reservation -- a conditional create, then a guarded
transition committed together with the operation row -- and the conditions that make it safe
are storage conditions. Proving them against an emulator alone would prove that the emulator
agrees with itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from chorus_api.dependencies import ApiContainer, DemoActor
from chorus_api.main import build_app
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver
from tests.fixtures.monitor import DESTINATION_ID, FIXTURE_ID_NAMESPACE, MonitorHarness

from chorus.domain.ids import Uuid5Generator
from chorus.infrastructure.local.dispatch import (
    InProcessOperationDispatcher,
    RecordingOperationDispatcher,
)
from chorus.infrastructure.local.monitor_agent import LexicalFakeMonitorAgent
from chorus.ports.agents import MonitorAgentPort
from chorus.ports.storage import StorageDriver


@dataclass(slots=True)
class ApiHarness:
    """The wired application plus the harness whose storage it shares."""

    harness: MonitorHarness
    app: FastAPI
    client: TestClient
    dispatcher: RecordingOperationDispatcher | InProcessOperationDispatcher

    def presenter_headers(self, **extra: str) -> dict[str, str]:
        headers = {"X-Chorus-Demo-Actor": "presenter_admin"}
        headers.update(extra)
        return headers

    def actor_headers(self, actor: str, **extra: str) -> dict[str, str]:
        """Headers for any seeded persona, so a test names the actor it means to be."""

        headers = {"X-Chorus-Demo-Actor": actor}
        headers.update(extra)
        return headers


def build_harness(
    driver: StorageDriver,
    dispatcher_kind: str,
    *,
    agent: MonitorAgentPort | None = None,
    ids_prefix: str | None = None,
) -> ApiHarness:
    """Wire one application over an explicit driver.

    Exposed so a test can put a fault-injecting driver underneath the *real* route and see what
    an interrupted request actually leaves behind, rather than reasoning about it. ``agent``
    substitutes the Monitor the in-process dispatcher runs, for a scenario the keyword stand-in
    cannot express -- everything from the route down is still the production path.
    """

    harness = MonitorHarness(driver=driver)
    if ids_prefix is not None:
        # Before the container is built, because the use cases below capture this generator by
        # reference. Two harnesses over one namespace otherwise mint identical identifiers, and
        # the second one's create-only writes collide with the first one's rows.
        harness.ids = Uuid5Generator(
            namespace=FIXTURE_ID_NAMESPACE, prefix=f"{harness.namespace.value}:{ids_prefix}"
        )
    dispatcher: RecordingOperationDispatcher | InProcessOperationDispatcher
    if dispatcher_kind == "recording":
        dispatcher = RecordingOperationDispatcher()
    else:
        dispatcher = InProcessOperationDispatcher(
            worker=harness.worker(agent or LexicalFakeMonitorAgent())
        )
    container = ApiContainer(
        namespace=harness.namespace,
        community_id=harness.community_id,
        destination_id=DESTINATION_ID,
        contributor_by_actor={
            DemoActor(actor): contributor_id
            for actor, contributor_id in harness.contributor_by_actor.items()
        },
        ingest_messages=harness.ingest,
        read_feed=harness.read_feed,
        operations=harness.operations,
        propose_mandates=harness.propose_mandates,
        decide_mandate=harness.decide_mandate,
        read_mandate_thread=harness.read_mandate_thread,
        dispatcher=dispatcher,
    )
    app = build_app(container)
    return ApiHarness(harness=harness, app=app, client=TestClient(app), dispatcher=dispatcher)


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    """Yield one empty storage driver per test, for each adapter under contract."""

    yield from storage_driver(str(request.param), prefix="api")


@pytest.fixture
def api(storage: StorageDriver) -> Iterator[ApiHarness]:
    built = build_harness(storage, "recording")
    with built.client:
        yield built


@pytest.fixture
def live_api(storage: StorageDriver) -> Iterator[ApiHarness]:
    """An application whose dispatcher actually runs the Monitor worker."""

    built = build_harness(storage, "in-process")
    with built.client:
        yield built
