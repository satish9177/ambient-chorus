"""The one mandate-decision transaction, and the guarantees that only storage can prove.

This is the layer where "atomic" stops being a word in a docstring. Every test here runs the
real use case over a real storage driver, and asserts what is *durable* afterwards -- including
the cases where the answer is "nothing".

Four properties, and each has a failure mode that a unit test cannot reach:

* **one transaction.** The new version, the pointer move, the case bump, the fence condition,
  the audit row, and the idempotency record commit together or not at all. A partial commit
  would leave a pointer naming a version that does not exist, or an authorization change with
  no audit row.
* **compare-and-set.** Two concurrent decisions cannot both advance the pointer.
* **the send fence.** A live fence refuses the mutation; an expired one does not.
* **replay.** The same key and body answer identically and write nothing the second time; the
  same key with a different body conflicts and writes nothing at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver
from tests.fixtures.mandates import (
    MandateWorld,
    adjust_body,
    approve_body,
    build_mandate_world,
    json_of,
    terminal_body,
)

from chorus.domain.entities import CaseState, MandateStatus
from chorus.domain.ids import (
    ActionId,
    ApprovalId,
    ExecutionId,
    MandateId,
    Sha256Digest,
    ViewId,
)
from chorus.ports.errors import NotFoundError
from chorus.ports.pagination import PageRequest
from chorus.ports.records import SendFence
from chorus.ports.storage import StorageDriver
from chorus.ports.unit_of_work import TransactionPlan

pytestmark = pytest.mark.anyio


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    yield from storage_driver(str(request.param), prefix="mandate-tx")


async def accepted(storage: StorageDriver) -> MandateWorld:
    world = await build_mandate_world(storage)
    assert (await world.accept_candidate()).status_code == 200
    return world


async def audit_events(world: MandateWorld) -> tuple[str, ...]:
    page = await world.api.harness.audit.read_case_events(world.case_scope, PageRequest(limit=100))
    return tuple(event.event_type for event in page.items)


# -- atomicity --------------------------------------------------------------------------


async def test_one_decision_writes_every_part_of_the_frozen_transaction(
    storage: StorageDriver,
) -> None:
    world = await accepted(storage)
    core = world.api.harness.core
    thread = json_of(world.thread("resident-a"))
    case_before = await core.load_case(world.case_scope)

    response = world.decide(
        "resident-a", thread["mandate_id"], approve_body(thread), key="tx-decision-0001"
    )
    assert response.status_code == 200, response.text

    # 1. the immutable new version
    version_two = await core.load_mandate_version(world.case_scope, _mandate_id(thread), 2)
    assert version_two.status is MandateStatus.APPROVED
    assert version_two.supersedes_version == 1

    # 2. the current pointer, moved and agreeing with the version it names
    pointer = await core.load_current_mandate_pointer(world.case_scope, _mandate_id(thread))
    assert pointer.pointer.version == 2
    assert pointer.version == 2
    assert pointer.pointer.terms_hash == version_two.terms_hash
    assert pointer.status is MandateStatus.APPROVED

    # 3. the case version
    case_after = await core.load_case(world.case_scope)
    assert case_after.version == case_before.version + 1

    # 4. version 1 is untouched
    version_one = await core.load_mandate_version(world.case_scope, _mandate_id(thread), 1)
    assert version_one.status is MandateStatus.PROPOSED
    assert version_one.decided_at is None

    # 5. one safe audit row
    assert "mandate.decided" in await audit_events(world)


async def test_the_audit_row_holds_only_identifiers_hashes_and_codes(
    storage: StorageDriver,
) -> None:
    world = await accepted(storage)
    thread = json_of(world.thread("resident-b"))
    world.decide("resident-b", thread["mandate_id"], approve_body(thread), key="tx-audit-0001")

    page = await world.api.harness.audit.read_case_events(world.case_scope, PageRequest(limit=100))
    decided = next(event for event in page.items if event.event_type == "mandate.decided")

    assert decided.reason_codes == ("APPROVE", "APPROVED")
    assert decided.safe_details.count == len(thread["fact_permissions"])
    assert decided.output_hash is not None and decided.output_hash.value.startswith("sha256:")
    assert decided.idempotency_key_hash is not None
    # Nothing about the facts themselves: only the case and the mandate, by identifier.
    assert {ref.entity_type for ref in decided.entity_refs} == {
        "DISCLOSURE_MANDATE",
        "COMMUNITY_CASE",
    }
    rendered = repr(decided)
    assert "asthma" not in rendered.lower()
    assert "4B" not in rendered


# -- optimistic concurrency -------------------------------------------------------------


async def test_two_different_decisions_race_and_exactly_one_wins(
    storage: StorageDriver,
) -> None:
    """Both callers read version 1; the pointer's compare-and-set decides between them."""

    world = await accepted(storage)
    thread = json_of(world.thread("resident-a"))

    first = world.decide(
        "resident-a", thread["mandate_id"], approve_body(thread), key="race-alpha-0001"
    )
    second = world.decide(
        "resident-a", thread["mandate_id"], terminal_body(thread, "REFUSE"), key="race-beta-0001"
    )

    assert first.status_code == 200
    assert second.status_code == 409
    pointer = await world.api.harness.core.load_current_mandate_pointer(
        world.case_scope, _mandate_id(thread)
    )
    assert pointer.pointer.version == 2
    assert pointer.status is MandateStatus.APPROVED
    # The loser wrote nothing at all -- there is no version 3.
    with pytest.raises(NotFoundError):
        await world.api.harness.core.load_mandate_version(world.case_scope, _mandate_id(thread), 3)


async def test_a_case_version_bump_from_elsewhere_does_not_strand_a_decision(
    storage: StorageDriver,
) -> None:
    """Four residents decide in sequence; each takes the case version the previous one left."""

    world = await accepted(storage)
    for pseudonym in ("resident-a", "resident-b", "resident-c", "resident-d"):
        thread = json_of(world.thread(pseudonym))
        response = world.decide(
            pseudonym, thread["mandate_id"], approve_body(thread), key=f"seq-{pseudonym}"
        )
        assert response.status_code == 200, response.text

    case = await world.api.harness.core.load_case(world.case_scope)
    assert case.state is CaseState.INVESTIGATING
    pointers = await world.api.harness.core.load_current_mandate_pointers(
        world.case_scope, PageRequest(limit=50)
    )
    assert all(item.pointer.version == 2 for item in pointers.items)


# -- idempotency ------------------------------------------------------------------------


async def test_the_same_key_and_body_replays_without_a_second_version(
    storage: StorageDriver,
) -> None:
    world = await accepted(storage)
    core = world.api.harness.core
    thread = json_of(world.thread("resident-a"))
    body = approve_body(thread)

    first = world.decide("resident-a", thread["mandate_id"], body, key="replay-0001")
    case_after_first = await core.load_case(world.case_scope)
    events_after_first = await audit_events(world)
    second = world.decide("resident-a", thread["mandate_id"], body, key="replay-0001")

    assert first.status_code == second.status_code == 200
    assert json_of(first) == json_of(second)
    # No second version, no second case bump, no duplicate audit event.
    assert (await core.load_case(world.case_scope)).version == case_after_first.version
    assert await audit_events(world) == events_after_first
    with pytest.raises(NotFoundError):
        await core.load_mandate_version(world.case_scope, _mandate_id(thread), 3)


async def test_the_same_key_with_a_different_body_conflicts_and_writes_nothing(
    storage: StorageDriver,
) -> None:
    world = await accepted(storage)
    core = world.api.harness.core
    thread = json_of(world.thread("resident-a"))

    first = world.decide(
        "resident-a", thread["mandate_id"], approve_body(thread), key="conflict-0001"
    )
    assert first.status_code == 200
    case_after = await core.load_case(world.case_scope)

    second = world.decide(
        "resident-a",
        thread["mandate_id"],
        terminal_body(thread, "REFUSE"),
        key="conflict-0001",
    )

    assert second.status_code == 409
    assert json_of(second)["code"] == "IDEMPOTENCY_CONFLICT"
    assert (await core.load_case(world.case_scope)).version == case_after.version
    pointer = await core.load_current_mandate_pointer(world.case_scope, _mandate_id(thread))
    assert pointer.status is MandateStatus.APPROVED


async def test_one_key_is_scoped_to_one_actor(storage: StorageDriver) -> None:
    """Two residents may use the same client key without colliding.

    The key is scoped to ``{namespace, command, actor}``, so Resident A's ``decision-1`` and
    Resident B's ``decision-1`` are different commands. Sharing a partition without the actor
    in the key would make the second caller replay the first caller's decision.
    """

    world = await accepted(storage)
    a_thread = json_of(world.thread("resident-a"))
    b_thread = json_of(world.thread("resident-b"))

    first = world.decide(
        "resident-a", a_thread["mandate_id"], approve_body(a_thread), key="shared-client-key"
    )
    second = world.decide(
        "resident-b", b_thread["mandate_id"], approve_body(b_thread), key="shared-client-key"
    )

    assert first.status_code == second.status_code == 200
    assert json_of(first)["mandate_id"] != json_of(second)["mandate_id"]


# -- the send fence ---------------------------------------------------------------------


async def test_a_live_send_fence_refuses_a_revocation_with_a_retryable_conflict(
    storage: StorageDriver,
) -> None:
    """The sender committed first. The contributor waits, and is told so."""

    world = await accepted(storage)
    core = world.api.harness.core
    thread = json_of(world.thread("resident-a"))
    world.decide("resident-a", thread["mandate_id"], approve_body(thread), key="fence-approve")
    approved = json_of(world.thread("resident-a"))
    before_version = (await core.load_case(world.case_scope)).version

    await core.acquire_send_fence(world.case_scope, _fence(world))

    response = world.decide(
        "resident-a", approved["mandate_id"], terminal_body(approved, "REVOKE"), key="fence-revoke"
    )

    assert response.status_code == 409
    body = json_of(response)
    assert body["code"] == "SEND_AUTHORIZATION_IN_PROGRESS"
    assert body["retryable"] is True
    # Nothing moved: the authorization the send is relying on is exactly as it was.
    assert (await core.load_case(world.case_scope)).version == before_version
    pointer = await core.load_current_mandate_pointer(world.case_scope, _mandate_id(thread))
    assert pointer.status is MandateStatus.APPROVED


async def test_the_revocation_commits_once_the_fence_expires(storage: StorageDriver) -> None:
    """The other half of the same order: the fence is a delay, never a veto."""

    world = await accepted(storage)
    core = world.api.harness.core
    clock = world.api.harness.clock
    thread = json_of(world.thread("resident-a"))
    world.decide("resident-a", thread["mandate_id"], approve_body(thread), key="fence2-approve")
    approved = json_of(world.thread("resident-a"))

    await core.acquire_send_fence(world.case_scope, _fence(world, lifetime=60))
    refused = world.decide(
        "resident-a", approved["mandate_id"], terminal_body(approved, "REVOKE"), key="fence2-revoke"
    )
    assert refused.status_code == 409

    clock.advance(seconds=60)
    retried = world.decide(
        "resident-a", approved["mandate_id"], terminal_body(approved, "REVOKE"), key="fence2-revoke"
    )

    assert retried.status_code == 200, retried.text
    assert json_of(retried)["status"] == MandateStatus.REVOKED.value
    pointer = await core.load_current_mandate_pointer(world.case_scope, _mandate_id(thread))
    assert pointer.pointer.version == 3
    assert pointer.status is MandateStatus.REVOKED


async def test_a_revocation_that_commits_first_leaves_the_sender_a_changed_case(
    storage: StorageDriver,
) -> None:
    """The first branch of the frozen order, from the contributor's side.

    The revocation commits while no fence is held, which moves the case version and the
    mandate pointer. A sender that had already read the old snapshot now holds one that no
    longer matches -- which is exactly what makes its later fence acquisition fail stale.
    """

    world = await accepted(storage)
    core = world.api.harness.core
    thread = json_of(world.thread("resident-a"))
    world.decide("resident-a", thread["mandate_id"], approve_body(thread), key="order-approve")
    approved = json_of(world.thread("resident-a"))
    snapshot_case_version = (await core.load_case(world.case_scope)).version
    snapshot_terms = approved["terms_hash"]

    revoked = world.decide(
        "resident-a", approved["mandate_id"], terminal_body(approved, "REVOKE"), key="order-revoke"
    )

    assert revoked.status_code == 200
    case_now = await core.load_case(world.case_scope)
    pointer = await core.load_current_mandate_pointer(world.case_scope, _mandate_id(thread))
    assert case_now.version != snapshot_case_version
    assert pointer.pointer.terms_hash.value != snapshot_terms
    assert pointer.status is MandateStatus.REVOKED
    # And the fence the sender would now try to take is still available, which is the point:
    # nothing is blocking it, and what it would fence has changed underneath it.
    assert await core.load_send_fence(world.case_scope) is None


async def test_an_expired_fence_does_not_block_a_decision(storage: StorageDriver) -> None:
    world = await accepted(storage)
    core = world.api.harness.core
    clock = world.api.harness.clock
    thread = json_of(world.thread("resident-a"))

    await core.acquire_send_fence(world.case_scope, _fence(world, lifetime=30))
    clock.advance(seconds=31)

    response = world.decide(
        "resident-a", thread["mandate_id"], approve_body(thread), key="expired-fence"
    )

    assert response.status_code == 200, response.text


async def test_acceptance_also_takes_the_fence_condition(storage: StorageDriver) -> None:
    """Asking for a mandate is authorization-sensitive too, so it waits on a send as well."""

    world = await build_mandate_world(storage)
    await world.api.harness.core.acquire_send_fence(world.case_scope, _fence(world))

    response = world.propose(expected_case_version=await world.case_version(), key="fenced-accept")

    assert response.status_code == 409
    assert json_of(response)["code"] == "SEND_AUTHORIZATION_IN_PROGRESS"


# -- readiness reconciliation -----------------------------------------------------------


async def test_the_first_decision_moves_the_case_out_of_awaiting_mandates(
    storage: StorageDriver,
) -> None:
    world = await accepted(storage)
    core = world.api.harness.core
    assert (await core.load_case(world.case_scope)).state is CaseState.AWAITING_MANDATES
    thread = json_of(world.thread("resident-a"))

    world.decide(
        "resident-a",
        thread["mandate_id"],
        terminal_body(thread, "REFUSE"),
        key="first-decision-key",
    )

    case = await core.load_case(world.case_scope)
    assert case.state is CaseState.INVESTIGATING
    assert case.state_reason_code == "MANDATE_DECISION_RECORDED"


async def test_a_later_decision_bumps_the_version_without_changing_state(
    storage: StorageDriver,
) -> None:
    world = await accepted(storage)
    core = world.api.harness.core
    first = json_of(world.thread("resident-a"))
    world.decide("resident-a", first["mandate_id"], approve_body(first), key="reconcile-first")
    after_first = await core.load_case(world.case_scope)

    second = json_of(world.thread("resident-b"))
    world.decide("resident-b", second["mandate_id"], approve_body(second), key="reconcile-second")

    after_second = await core.load_case(world.case_scope)
    assert after_second.state is after_first.state is CaseState.INVESTIGATING
    assert after_second.version == after_first.version + 1
    assert after_second.state_reason_code == "MANDATE_DECIDED"


async def test_an_adjustment_that_narrows_is_recorded_as_withdrawn_authorization(
    storage: StorageDriver,
) -> None:
    """The reason code distinguishes "a decision happened" from "authority was taken back"."""

    world = await accepted(storage)
    core = world.api.harness.core
    a_thread = json_of(world.thread("resident-a"))
    world.decide("resident-a", a_thread["mandate_id"], approve_body(a_thread), key="narrow-first")

    b_thread = json_of(world.thread("resident-b"))
    world.decide("resident-b", b_thread["mandate_id"], approve_body(b_thread), key="narrow-b1")
    b_after = json_of(world.thread("resident-b"))
    world.decide(
        "resident-b",
        b_after["mandate_id"],
        adjust_body(b_after, {}),
        key="narrow-b2",
    )

    case = await core.load_case(world.case_scope)
    # The case is INVESTIGATING rather than READY_FOR_ACTION, so readiness reconciliation has
    # nothing to remove; the version still moves, which is what stales a compiled view.
    assert case.state is CaseState.INVESTIGATING
    version_three = await core.load_mandate_version(world.case_scope, _mandate_id(b_after), 3)
    assert all(grant.max_scope.value == "INTERNAL_ONLY" for grant in version_three.fact_grants)


# -- helpers ------------------------------------------------------------------------------

FENCE_SNAPSHOT = Sha256Digest(f"sha256:{sha256(b'mandate-fence').hexdigest()}")


def _mandate_id(thread: dict[str, Any]) -> MandateId:
    return MandateId(UUID(str(thread["mandate_id"])))


def _fence(world: MandateWorld, *, lifetime: int = 60) -> SendFence:
    """A fence exactly as the compiler boundary would create one immediately before a send."""

    harness = world.api.harness
    now = harness.clock.now()
    return SendFence(
        namespace=harness.namespace,
        community_id=harness.community_id,
        case_id=world.case_id,
        execution_id=ExecutionId(harness.ids.new_uuid()),
        action_id=ActionId(harness.ids.new_uuid()),
        approval_id=ApprovalId(harness.ids.new_uuid()),
        view_id=ViewId(harness.ids.new_uuid()),
        authorization_snapshot_hash=FENCE_SNAPSHOT,
        acquired_at=now,
        expires_at=now + timedelta(seconds=lifetime),
    )


async def _force_state(world: MandateWorld, state: CaseState) -> int:
    """Put the case into a state Phase 4 cannot reach on its own, and return its version.

    Reaching ``READY_FOR_ACTION`` legitimately needs a validated assessment and a compile
    preflight, which are Phase 5 and Phase 6. Staging the case row is fixture setup, not a
    faked decision: every mandate decision below still goes through the real API, the real
    policy validation, and the real transaction.
    """

    core = world.api.harness.core
    case = await core.load_case(world.case_scope)
    moved = replace(
        case,
        state=state,
        version=case.version + 1,
        updated_at=world.api.harness.clock.now(),
    )
    await world.api.harness.unit_of_work.commit(
        TransactionPlan(
            name="stage-case-state",
            operations=(
                core.stage_update_case(world.case_scope, moved, expected_version=case.version),
            ),
            audit_required=False,
        )
    )
    return moved.version


@pytest.mark.parametrize("state", [CaseState.READY_FOR_ACTION, CaseState.ACTION_PROPOSED])
async def test_a_revocation_returns_a_ready_case_to_investigating(
    storage: StorageDriver, state: CaseState
) -> None:
    """Readiness reconciliation: taking authority back removes readiness deterministically."""

    world = await accepted(storage)
    core = world.api.harness.core
    thread = json_of(world.thread("resident-a"))
    world.decide("resident-a", thread["mandate_id"], approve_body(thread), key="ready-approve-key")
    await _force_state(world, state)
    approved = json_of(world.thread("resident-a"))

    response = world.decide(
        "resident-a",
        approved["mandate_id"],
        terminal_body(approved, "REVOKE"),
        key="ready-revoke-key",
    )

    assert response.status_code == 200, response.text
    case = await core.load_case(world.case_scope)
    assert case.state is CaseState.INVESTIGATING
    assert case.state_reason_code == "MANDATE_AUTHORIZATION_WITHDRAWN"


async def test_an_approval_leaves_a_ready_case_ready(storage: StorageDriver) -> None:
    """The other half: adding authority never removes readiness, it only stales the artifacts."""

    world = await accepted(storage)
    core = world.api.harness.core
    first = json_of(world.thread("resident-a"))
    world.decide("resident-a", first["mandate_id"], approve_body(first), key="stay-ready-first")
    await _force_state(world, CaseState.READY_FOR_ACTION)
    before = await core.load_case(world.case_scope)

    second = json_of(world.thread("resident-b"))
    response = world.decide(
        "resident-b", second["mandate_id"], approve_body(second), key="stay-ready-second"
    )

    assert response.status_code == 200, response.text
    case = await core.load_case(world.case_scope)
    assert case.state is CaseState.READY_FOR_ACTION
    assert case.version == before.version + 1
    assert case.state_reason_code == "MANDATE_DECIDED"


@pytest.mark.parametrize("state", [CaseState.RESOLVED, CaseState.CLOSED_UNRESOLVED])
async def test_a_terminal_case_refuses_every_mandate_decision(
    storage: StorageDriver, state: CaseState
) -> None:
    world = await accepted(storage)
    core = world.api.harness.core
    thread = json_of(world.thread("resident-a"))
    await _force_state(world, state)
    before = await core.load_case(world.case_scope)

    response = world.decide(
        "resident-a", thread["mandate_id"], approve_body(thread), key="terminal-case-key"
    )

    assert response.status_code == 409
    body = json_of(response)
    assert body["code"] == "STALE_AUTHORIZATION"
    assert body["errors"] == ["CASE_TERMINAL"]
    assert (await core.load_case(world.case_scope)).version == before.version
