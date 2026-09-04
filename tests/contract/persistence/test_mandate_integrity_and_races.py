"""Two failures that only exist between components: tampered storage, and a true race.

Both are deliberately made *deterministic* rather than probabilistic. A concurrency test that
fires two requests and hopes they interleave passes on a fast machine and proves nothing; the
one here forces the exact interleaving that matters by committing the duplicate from inside the
driver call the first attempt is making.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

import pytest
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver
from tests.fixtures.mandates import (
    MandateWorld,
    approve_body,
    build_mandate_world,
    decision_command,
    json_of,
)

from chorus.application.errors import PolicyDeniedError
from chorus.domain.entities import MandateStatus
from chorus.domain.ids import DestinationId, MandateId, Sha256Digest
from chorus.infrastructure.dynamodb import codec_mandate
from chorus.ports.errors import NotFoundError
from chorus.ports.pagination import PageRequest
from chorus.ports.storage import (
    DeleteItem,
    ItemKey,
    KeyPresent,
    PutItem,
    QueryRequest,
    QueryResult,
    StorageDriver,
    StoredItem,
    WriteOperation,
)

pytestmark = pytest.mark.anyio

MANDATE_VERSION_MARKER = "VERSION#"
TAMPERED_HASH = Sha256Digest("sha256:" + "ab" * 32)


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    yield from storage_driver(str(request.param), prefix="mandate-integrity")


def decision_transaction(operations: tuple[WriteOperation, ...]) -> bool:
    return any(MANDATE_VERSION_MARKER in operation.key.sort_key for operation in operations)


@dataclass(slots=True)
class InterleavingDriver:
    """Run a callback once, immediately before a chosen transaction reaches storage.

    This is the whole trick. The callback commits the *same* command through a second path, so
    when the outer attempt's create-only writes finally land they collide with a decision that
    already exists -- which is precisely the state two genuinely concurrent callers produce, and
    is otherwise reachable only by luck.
    """

    inner: StorageDriver
    before: Callable[[], Awaitable[None]] | None = None
    selects: Callable[[tuple[WriteOperation, ...]], bool] = decision_transaction
    fired: int = field(default=0, init=False)

    async def get_item(self, key: ItemKey, *, consistent: bool) -> StoredItem | None:
        return await self.inner.get_item(key, consistent=consistent)

    async def batch_get_items(
        self, keys: tuple[ItemKey, ...], *, consistent: bool
    ) -> tuple[StoredItem, ...]:
        return await self.inner.batch_get_items(keys, consistent=consistent)

    async def query(self, request: QueryRequest) -> QueryResult:
        return await self.inner.query(request)

    async def write_item(self, operation: PutItem | DeleteItem) -> None:
        await self.inner.write_item(operation)

    async def transact_write(
        self, operations: tuple[WriteOperation, ...], *, client_request_token: str
    ) -> None:
        if self.before is not None and self.fired == 0 and self.selects(operations):
            self.fired += 1
            callback, self.before = self.before, None
            await callback()
        await self.inner.transact_write(operations, client_request_token=client_request_token)


# -- a genuinely concurrent duplicate ------------------------------------------------------


async def test_two_concurrent_identical_decisions_produce_exactly_one(
    storage: StorageDriver,
) -> None:
    """Both callers read version 1 and both build version 2. Only one of them is ever durable."""

    interleaving = InterleavingDriver(inner=storage)
    world = await build_mandate_world(interleaving)
    assert (await world.accept_candidate()).status_code == 200
    thread = json_of(world.thread("resident-a"))
    body = approve_body(thread)

    command = decision_command(world, "resident-a", thread, body, key="concurrent-decision-key")
    twin: dict[str, Any] = {}

    async def commit_the_twin() -> None:
        # Runs inside the first attempt's transact call, before its writes reach storage.
        twin["result"] = await world.api.harness.decide_mandate.execute(command)

    interleaving.before = commit_the_twin
    first = await world.api.harness.decide_mandate.execute(command)

    assert interleaving.fired == 1, "the interleaving never happened; this proves nothing"
    # One logical decision: both callers are told the same version, and it is the only one.
    assert first.version == twin["result"].version == 2
    assert first.terms_hash == twin["result"].terms_hash
    assert first.case_version == twin["result"].case_version
    assert first.replayed is True, "the losing attempt resolved to the winner's decision"

    core = world.api.harness.core
    pointers = await core.load_current_mandate_pointers(world.case_scope, PageRequest(limit=50))
    mine = next(
        item for item in pointers.items if str(item.pointer.mandate_id) == thread["mandate_id"]
    )
    assert mine.pointer.version == 2
    assert mine.version == 2, "the pointer row advanced once, not twice"


async def test_one_concurrent_duplicate_bumps_the_case_version_once(
    storage: StorageDriver,
) -> None:
    interleaving = InterleavingDriver(inner=storage)
    world = await build_mandate_world(interleaving)
    assert (await world.accept_candidate()).status_code == 200
    thread = json_of(world.thread("resident-a"))
    body = approve_body(thread)
    command = decision_command(world, "resident-a", thread, body, key="concurrent-case-key")
    before = await world.case_version()

    async def commit_the_twin() -> None:
        await world.api.harness.decide_mandate.execute(command)

    interleaving.before = commit_the_twin
    await world.api.harness.decide_mandate.execute(command)

    assert interleaving.fired == 1
    assert await world.case_version() == before + 1


# -- tampered storage ----------------------------------------------------------------------


async def _tamper_pointer(world: MandateWorld, mandate_id: str, **changes: Any) -> None:
    """Rewrite the stored current pointer so it disagrees with the version it names.

    Written through the codec at the exact key the repository reads, because the failure being
    modelled is a *stored* row that is wrong -- not a caller sending something odd.
    """

    core = world.api.harness.core
    pointers = await core.load_current_mandate_pointers(world.case_scope, PageRequest(limit=50))
    stored = next(item for item in pointers.items if str(item.pointer.mandate_id) == mandate_id)
    pointer = replace(stored.pointer, **changes)
    await world.api.harness.driver.write_item(
        PutItem(
            key=codec_mandate.mandate_pointer_key(world.case_scope, stored.pointer.mandate_id),
            item=codec_mandate.encode_mandate_pointer(
                world.case_scope, replace(stored, pointer=pointer)
            ),
            condition=KeyPresent(),
        )
    )


async def test_a_pointer_whose_terms_hash_was_tampered_fails_closed(
    storage: StorageDriver,
) -> None:
    """The pointer and the version it names restate the same hash; disagreement is the signal."""

    world = await build_mandate_world(storage)
    assert (await world.accept_candidate()).status_code == 200
    thread = json_of(world.thread("resident-a"))
    await _tamper_pointer(world, thread["mandate_id"], terms_hash=TAMPERED_HASH)

    response = world.decide(
        "resident-a", thread["mandate_id"], approve_body(thread), key="tampered-hash-key"
    )

    assert response.status_code == 500
    assert json_of(response)["code"] == "INTEGRITY_ERROR"
    # Nothing was decided against terms nobody agreed to.
    core = world.api.harness.core
    with pytest.raises(NotFoundError):
        await core.load_mandate_version(world.case_scope, _id(thread), 2)


async def test_a_pointer_aimed_at_a_version_that_does_not_exist_fails_closed(
    storage: StorageDriver,
) -> None:
    world = await build_mandate_world(storage)
    assert (await world.accept_candidate()).status_code == 200
    thread = json_of(world.thread("resident-a"))
    await _tamper_pointer(world, thread["mandate_id"], version=7)

    response = world.decide(
        "resident-a", thread["mandate_id"], approve_body(thread), key="tampered-version-key"
    )

    assert response.status_code in {404, 500}
    assert json_of(response)["code"] in {"NOT_FOUND", "INTEGRITY_ERROR"}


async def test_a_pointer_naming_a_foreign_contributor_is_not_decidable(
    storage: StorageDriver,
) -> None:
    """The row claims Resident A's mandate belongs to Resident B. Neither of them may use it."""

    world = await build_mandate_world(storage)
    assert (await world.accept_candidate()).status_code == 200
    a_thread = json_of(world.thread("resident-a"))
    await _tamper_pointer(
        world, a_thread["mandate_id"], contributor_id=world.contributor_id("resident-b")
    )

    by_owner = world.decide(
        "resident-a", a_thread["mandate_id"], approve_body(a_thread), key="tampered-owner-a"
    )
    by_claimant = world.decide(
        "resident-b",
        a_thread["mandate_id"],
        approve_body(a_thread),
        key="tampered-owner-b",
        actor="resident_b",
    )

    # The real owner is refused because the pointer no longer names them; the claimant is
    # refused because the pointer and the immutable version disagree about who owns it.
    assert by_owner.status_code == 404
    assert by_claimant.status_code == 500
    assert json_of(by_claimant)["code"] == "INTEGRITY_ERROR"


async def test_an_untampered_pointer_still_decides_normally(storage: StorageDriver) -> None:
    """The control: the integrity check refuses disagreement, not ordinary state."""

    world = await build_mandate_world(storage)
    assert (await world.accept_candidate()).status_code == 200
    thread = json_of(world.thread("resident-a"))

    response = world.decide(
        "resident-a", thread["mandate_id"], approve_body(thread), key="untampered-control"
    )

    assert response.status_code == 200, response.text
    assert json_of(response)["status"] == MandateStatus.APPROVED.value


def _id(thread: dict[str, Any]) -> MandateId:
    return MandateId(UUID(thread["mandate_id"]))


async def test_a_mandate_whose_destination_left_the_registry_stops_authorizing(
    storage: StorageDriver,
) -> None:
    """Destination and purpose are carried forward, so they are re-derived rather than trusted.

    The decision request cannot express either, which means the only route by which a bad one
    reaches a new version is a stored record that was already wrong -- or a registry that moved
    on underneath a record that was right when it was written. Checking against configuration
    rather than against the record is what makes the second case detectable.
    """

    world = await build_mandate_world(storage)
    assert (await world.accept_candidate()).status_code == 200
    thread = json_of(world.thread("resident-a"))
    body = approve_body(thread)
    command = decision_command(world, "resident-a", thread, body, key="moved-destination-key")
    moved = replace(command, destination_id=DestinationId("property_manager:successor"))

    with pytest.raises(PolicyDeniedError) as raised:
        await world.api.harness.decide_mandate.execute(moved)

    assert raised.value.reason_codes == ("DESTINATION_NOT_ALLOWED",)
    core = world.api.harness.core
    with pytest.raises(NotFoundError):
        await core.load_mandate_version(world.case_scope, _id(thread), 2)
