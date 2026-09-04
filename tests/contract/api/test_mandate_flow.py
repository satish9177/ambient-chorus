"""The Phase-4 exit path, driven entirely through the real HTTP API.

Nothing here fakes a decision. The case is discovered by the Monitor apply, the proposals are
created by the acceptance route, and A, C and D approve while B adjusts -- every one of them a
real ``POST`` that goes through actor resolution, policy validation, the domain edge table, and
one atomic transaction before anything is durable.

The assertions afterwards read *storage*, not the responses, because a route that returned the
right JSON while writing something else is exactly the failure a contract test exists to catch.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver
from tests.fixtures.mandates import (
    MandateWorld,
    adjust_body,
    approve_body,
    build_mandate_world,
    fact_ids_by_type,
    json_of,
)

from chorus.domain.entities import CaseState, DisclosureScope, MandateStatus
from chorus.domain.ids import MandateId
from chorus.domain.mandates import derived_status
from chorus.ports.pagination import PageRequest
from chorus.ports.storage import StorageDriver

pytestmark = pytest.mark.anyio


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    yield from storage_driver(str(request.param), prefix="mandate-flow")


# -- acceptance -------------------------------------------------------------------------


async def test_accepting_a_candidate_proposes_a_mandate_to_every_owner(
    storage: StorageDriver,
) -> None:
    world = await build_mandate_world(storage)
    before = await world.case_version()

    response = await world.accept_candidate()

    assert response.status_code == 200, response.text
    body = json_of(response)
    assert body["state"] == CaseState.AWAITING_MANDATES.value
    assert body["case_version"] == before + 1
    assert len(body["proposals"]) == 4
    assert {item["contributor_id"] for item in body["proposals"]} == {
        str(world.contributor_id(pseudonym))
        for pseudonym in ("resident-a", "resident-b", "resident-c", "resident-d")
    }
    assert all(item["status"] == MandateStatus.PROPOSED.value for item in body["proposals"])
    assert all(item["version"] == 1 for item in body["proposals"])
    assert all(item["terms_hash"].startswith("sha256:") for item in body["proposals"])


async def test_a_proposal_carries_every_fact_its_owner_holds(storage: StorageDriver) -> None:
    """Resident B owns six facts, including the two policy never lets out."""

    world = await build_mandate_world(storage)
    await world.accept_candidate()

    thread = json_of(world.thread("resident-b"))
    types = {row["fact_type"] for row in thread["fact_permissions"]}
    assert types == {
        "INCIDENT_OCCURRENCE",
        "SERVICE_IMPACT",
        "IDENTITY_ATTRIBUTE",
        "UNIT_LOCATION",
        "HEALTH_DETAIL",
        "EVIDENCE_DESCRIPTION",
    }


async def test_the_proposal_is_capped_by_policy_not_by_the_agent(storage: StorageDriver) -> None:
    world = await build_mandate_world(storage)
    await world.accept_candidate()

    rows = {
        row["fact_type"]: row for row in json_of(world.thread("resident-b"))["fact_permissions"]
    }
    assert rows["UNIT_LOCATION"]["policy_maximum_scope"] == "INTERNAL_ONLY"
    assert rows["HEALTH_DETAIL"]["policy_maximum_scope"] == "INTERNAL_ONLY"
    assert rows["IDENTITY_ATTRIBUTE"]["policy_maximum_scope"] == "NAMED_CASE"
    assert rows["INCIDENT_OCCURRENCE"]["policy_maximum_scope"] == "EXTERNAL_ACTION"
    # And the default underneath every ceiling is the least permissive useful one.
    assert rows["INCIDENT_OCCURRENCE"]["proposed_scope"] == "ANONYMOUS_CASE"
    assert rows["EVIDENCE_DESCRIPTION"]["proposed_scope"] == "INTERNAL_ONLY"
    assert rows["IDENTITY_ATTRIBUTE"]["proposed_scope"] == "INTERNAL_ONLY"


async def test_a_locked_fact_says_why_it_is_locked(storage: StorageDriver) -> None:
    world = await build_mandate_world(storage)
    await world.accept_candidate()

    rows = {
        row["fact_type"]: row for row in json_of(world.thread("resident-b"))["fact_permissions"]
    }
    assert rows["HEALTH_DETAIL"]["locked_reason"] is not None
    assert rows["INCIDENT_OCCURRENCE"]["locked_reason"] is None


async def test_accepting_a_case_twice_under_one_key_is_a_replay(storage: StorageDriver) -> None:
    world = await build_mandate_world(storage)
    version = await world.case_version()

    first = json_of(world.propose(expected_case_version=version, key="accept-0001"))
    second = json_of(world.propose(expected_case_version=version, key="accept-0001"))

    assert first == second
    assert await world.case_version() == version + 1


async def test_accepting_an_already_accepted_case_is_refused(storage: StorageDriver) -> None:
    world = await build_mandate_world(storage)
    await world.accept_candidate()

    response = world.propose(expected_case_version=await world.case_version(), key="accept-0002")

    assert response.status_code == 409
    assert json_of(response)["code"] == "STATE_TRANSITION_ERROR"


async def test_a_resident_cannot_accept_a_candidate(storage: StorageDriver) -> None:
    world = await build_mandate_world(storage)

    response = world.propose(
        expected_case_version=await world.case_version(), key="accept-0003", actor="resident_a"
    )

    assert response.status_code == 403


# -- the exit path ----------------------------------------------------------------------


async def _approve(world: MandateWorld, pseudonym: str, key: str) -> dict[str, object]:
    thread = json_of(world.thread(pseudonym))
    response = world.decide(pseudonym, thread["mandate_id"], approve_body(thread), key=key)
    assert response.status_code == 200, response.text
    return json_of(response)


async def test_a_c_and_d_approve_and_b_adjusts_through_the_real_api(
    storage: StorageDriver,
) -> None:
    """The frozen 4->5 gate: four immutable decisions, three shapes, one case."""

    world = await build_mandate_world(storage)
    await world.accept_candidate()

    approvals = {
        pseudonym: await _approve(world, pseudonym, f"approve-{pseudonym}")
        for pseudonym in ("resident-a", "resident-c", "resident-d")
    }
    for pseudonym, result in approvals.items():
        assert result["status"] == MandateStatus.APPROVED.value, pseudonym
        assert result["version"] == 2
        assert result["supersedes_version"] == 1
        assert result["decided_at"] is not None
        assert result["revoked_at"] is None

    # Resident B narrows: the incident stays anonymous, the photo becomes exportable, and the
    # name, unit and health detail go internal. Identity stays off.
    thread = json_of(world.thread("resident-b"))
    adjusted = world.decide(
        "resident-b",
        thread["mandate_id"],
        adjust_body(
            thread,
            {
                "INCIDENT_OCCURRENCE": "ANONYMOUS_CASE",
                "SERVICE_IMPACT": "ANONYMOUS_CASE",
                "EVIDENCE_DESCRIPTION": "EXTERNAL_ACTION",
                "IDENTITY_ATTRIBUTE": "INTERNAL_ONLY",
                "UNIT_LOCATION": "INTERNAL_ONLY",
                "HEALTH_DETAIL": "INTERNAL_ONLY",
            },
        ),
        key="adjust-resident-b",
    )
    assert adjusted.status_code == 200, adjusted.text
    assert json_of(adjusted)["status"] == MandateStatus.APPROVED.value
    assert json_of(adjusted)["version"] == 2

    await _assert_exit_state(world)


async def _assert_exit_state(world: MandateWorld) -> None:
    """Read storage directly: pointers, histories, case version, and the locked facts."""

    core = world.api.harness.core
    scope = world.case_scope
    now = world.api.harness.clock.now()

    pointers = await core.load_current_mandate_pointers(scope, PageRequest(limit=50))
    assert len(pointers.items) == 4

    for stored in pointers.items:
        mandate_id = stored.pointer.mandate_id
        # The current pointer names version 2 and agrees with it field for field.
        assert stored.pointer.version == 2
        current = await core.load_mandate_version(scope, mandate_id, 2)
        assert stored.pointer.terms_hash == current.terms_hash
        assert stored.status is current.status is MandateStatus.APPROVED
        assert current.decision_actor_id == current.contributor_id

        # Version 1 is still exactly the proposal it always was.
        proposal = await core.load_mandate_version(scope, mandate_id, 1)
        assert proposal.status is MandateStatus.PROPOSED
        assert proposal.decided_at is None
        assert proposal.decision_actor_id is None
        assert derived_status(proposal, current_version=2, now=now) is MandateStatus.SUPERSEDED

    # Resident B's locked facts are granted nothing, whatever anybody asked for.
    b_pointer = next(
        item
        for item in pointers.items
        if item.pointer.contributor_id == world.contributor_id("resident-b")
    )
    b_current = await core.load_mandate_version(scope, b_pointer.pointer.mandate_id, 2)
    b_thread = json_of(world.thread("resident-b"))
    by_type = fact_ids_by_type(b_thread)
    scopes = {str(item.fact_id): item.max_scope for item in b_current.fact_grants}
    assert scopes[by_type["UNIT_LOCATION"]] is DisclosureScope.INTERNAL_ONLY
    assert scopes[by_type["HEALTH_DETAIL"]] is DisclosureScope.INTERNAL_ONLY
    assert scopes[by_type["IDENTITY_ATTRIBUTE"]] is DisclosureScope.INTERNAL_ONLY
    assert scopes[by_type["EVIDENCE_DESCRIPTION"]] is DisclosureScope.EXTERNAL_ACTION

    # Identity stayed separately controlled and stayed off, despite an EXTERNAL_ACTION content
    # grant sitting beside it.
    assert b_current.identity_grant.externally_shareable is False
    assert b_current.identity_grant.max_scope is DisclosureScope.ANONYMOUS_CASE

    # Four authorization-sensitive changes plus the acceptance moved the case version, and the
    # first decision moved it out of AWAITING_MANDATES.
    case = await core.load_case(scope)
    assert case.state is CaseState.INVESTIGATING
    assert case.version >= 6


# -- the mandate thread -----------------------------------------------------------------


async def test_the_thread_shows_the_immutable_history_after_a_decision(
    storage: StorageDriver,
) -> None:
    world = await build_mandate_world(storage)
    await world.accept_candidate()
    await _approve(world, "resident-a", "approve-history")

    thread = json_of(world.thread("resident-a"))

    assert thread["current_version"] == 2
    assert thread["status"] == MandateStatus.APPROVED.value
    assert [item["version"] for item in thread["history"]] == [1, 2]
    assert thread["history"][0]["status"] == MandateStatus.SUPERSEDED.value
    assert thread["history"][1]["status"] == MandateStatus.APPROVED.value
    assert thread["history"][1]["supersedes_version"] == 1


async def test_the_thread_keeps_content_and_identity_permission_apart(
    storage: StorageDriver,
) -> None:
    world = await build_mandate_world(storage)
    await world.accept_candidate()

    thread = json_of(world.thread("resident-b"))

    assert "identity_permission" in thread
    assert thread["identity_permission"]["policy_maximum_scope"] == "NAMED_CASE"
    identity_row = next(
        row for row in thread["fact_permissions"] if row["fact_type"] == "IDENTITY_ATTRIBUTE"
    )
    assert identity_row["requires_identity_grant"] is True
    incident_row = next(
        row for row in thread["fact_permissions"] if row["fact_type"] == "INCIDENT_OCCURRENCE"
    )
    assert incident_row["requires_identity_grant"] is False


async def test_the_thread_carries_the_destination_purpose_and_validity(
    storage: StorageDriver,
) -> None:
    world = await build_mandate_world(storage)
    await world.accept_candidate()

    thread = json_of(world.thread("resident-c"))

    assert thread["allowed_destination_ids"] == ["property_manager:demo"]
    assert thread["allowed_purposes"] == ["REQUEST_ELEVATOR_REPAIR_AND_RESPONSE"]
    assert thread["valid_from"] is not None
    assert thread["expires_at"] is None
    assert thread["case_state"] == CaseState.AWAITING_MANDATES.value


async def test_a_presenter_may_read_a_thread_but_never_decide_one(
    storage: StorageDriver,
) -> None:
    world = await build_mandate_world(storage)
    await world.accept_candidate()

    read = world.thread("resident-a", actor="presenter_admin")
    assert read.status_code == 200

    thread = json_of(read)
    decided = world.decide(
        "resident-a",
        thread["mandate_id"],
        approve_body(thread),
        key="presenter-decides",
        actor="presenter_admin",
    )
    assert decided.status_code == 403


async def test_a_mandate_id_from_another_contributor_is_not_decidable(
    storage: StorageDriver,
) -> None:
    """A real identifier, a real case, and the wrong owner: 404, exactly like a fake one."""

    world = await build_mandate_world(storage)
    await world.accept_candidate()

    b_thread = json_of(world.thread("resident-b"))
    response = world.decide(
        "resident-b",
        b_thread["mandate_id"],
        approve_body(b_thread),
        key="wrong-owner",
        actor="resident_a",
    )

    assert response.status_code == 404
    fabricated = world.decide(
        "resident-a",
        str(MandateId(UUID("99999999-9999-4999-8999-999999999999"))),
        b_thread
        and {
            "expected_version": 1,
            "decision": "REFUSE",
            "fact_grants": [],
            "identity_grant": {"externally_shareable": False, "max_scope": "ANONYMOUS_CASE"},
            "expires_at": None,
        },
        key="fabricated-mandate",
        actor="resident_a",
    )
    assert fabricated.status_code == 404
    assert json_of(response)["code"] == json_of(fabricated)["code"] == "NOT_FOUND"
    assert json_of(response)["detail"] == json_of(fabricated)["detail"]
