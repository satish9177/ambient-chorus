"""Every way a mandate decision can be abused, answered through the real API.

The organising rule is that a security failure fails *closed and whole*: the response is a
typed Problem Details document, and the durable state afterwards is byte-identical to the state
before. Several tests assert both halves, because a route that returns 403 and writes anyway is
worse than one that returns 200 -- it looks safe in a log.

The other rule is non-enumeration. A foreign fact, an absent fact, a withdrawn fact and a
neighbour's fact all answer the same way, and a mandate from another case answers exactly like
one that never existed. Tests compare the two responses rather than asserting each in isolation,
because "both are 404" is not the property; "a caller cannot tell them apart" is.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver
from tests.fixtures.mandates import (
    MandateWorld,
    adjust_body,
    approve_body,
    build_mandate_world,
    fact_ids_by_type,
    grant,
    json_of,
    terminal_body,
)

from chorus.domain.entities import MandateStatus
from chorus.domain.time import format_utc
from chorus.ports.pagination import PageRequest
from chorus.ports.storage import StorageDriver

pytestmark = pytest.mark.anyio


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    yield from storage_driver(str(request.param), prefix="mandate-adv")


async def accepted_world(storage: StorageDriver) -> MandateWorld:
    world = await build_mandate_world(storage)
    response = await world.accept_candidate()
    assert response.status_code == 200, response.text
    return world


async def snapshot(world: MandateWorld) -> tuple[Any, ...]:
    """Everything a refused command must leave exactly as it found it."""

    core = world.api.harness.core
    case = await core.load_case(world.case_scope)
    pointers = await core.load_current_mandate_pointers(world.case_scope, PageRequest(limit=50))
    return (
        case.version,
        case.state,
        tuple(
            sorted(
                (
                    str(item.pointer.mandate_id),
                    item.pointer.version,
                    item.version,
                    item.status,
                    item.pointer.terms_hash.value,
                )
                for item in pointers.items
            )
        ),
    )


async def assert_nothing_changed(world: MandateWorld, before: tuple[Any, ...]) -> None:
    assert await snapshot(world) == before


# -- ownership and cross-case -----------------------------------------------------------


async def test_a_contributor_cannot_grant_another_contributors_fact(
    storage: StorageDriver,
) -> None:
    """Resident A holds a mandate; the fact they name belongs to Resident B."""

    world = await accepted_world(storage)
    a_thread = json_of(world.thread("resident-a"))
    b_fact = fact_ids_by_type(json_of(world.thread("resident-b")))["HEALTH_DETAIL"]
    before = await snapshot(world)

    response = world.decide(
        "resident-a",
        a_thread["mandate_id"],
        {
            "expected_version": 1,
            "decision": "ADJUST",
            "fact_grants": [grant(b_fact, "ANONYMOUS_CASE")],
            "identity_grant": {"externally_shareable": False, "max_scope": "ANONYMOUS_CASE"},
            "expires_at": None,
        },
        key="foreign-fact-0001",
    )

    assert response.status_code == 422
    assert json_of(response)["code"] == "POLICY_DENIED"
    assert json_of(response)["errors"] == ["UNKNOWN_FACT"]
    await assert_nothing_changed(world, before)


async def test_a_nonexistent_fact_and_a_neighbours_fact_are_indistinguishable(
    storage: StorageDriver,
) -> None:
    world = await accepted_world(storage)
    a_thread = json_of(world.thread("resident-a"))
    b_fact = fact_ids_by_type(json_of(world.thread("resident-b")))["INCIDENT_OCCURRENCE"]

    def body(fact_id: str) -> dict[str, Any]:
        return {
            "expected_version": 1,
            "decision": "ADJUST",
            "fact_grants": [grant(fact_id, "ANONYMOUS_CASE")],
            "identity_grant": {"externally_shareable": False, "max_scope": "ANONYMOUS_CASE"},
            "expires_at": None,
        }

    neighbour = world.decide(
        "resident-a", a_thread["mandate_id"], body(b_fact), key="probe-neighbour"
    )
    invented = world.decide(
        "resident-a", a_thread["mandate_id"], body(str(uuid4())), key="probe-invented"
    )

    assert neighbour.status_code == invented.status_code == 422
    assert json_of(neighbour)["errors"] == json_of(invented)["errors"] == ["UNKNOWN_FACT"]
    assert json_of(neighbour)["detail"] == json_of(invented)["detail"]


async def test_a_fact_from_another_case_and_community_is_refused(
    storage: StorageDriver,
) -> None:
    """A second discovered case in a second community, and a fact borrowed from it."""

    world = await accepted_world(storage)
    other = await build_mandate_world(storage, prefix="other-case", seed=False)
    await other.accept_candidate()
    assert other.case_id != world.case_id
    foreign_fact = fact_ids_by_type(json_of(other.thread("resident-a")))["INCIDENT_OCCURRENCE"]
    a_thread = json_of(world.thread("resident-a"))
    before = await snapshot(world)

    response = world.decide(
        "resident-a",
        a_thread["mandate_id"],
        {
            "expected_version": 1,
            "decision": "ADJUST",
            "fact_grants": [grant(foreign_fact, "ANONYMOUS_CASE")],
            "identity_grant": {"externally_shareable": False, "max_scope": "ANONYMOUS_CASE"},
            "expires_at": None,
        },
        key="cross-case-0001",
    )

    assert response.status_code == 422
    assert json_of(response)["errors"] == ["UNKNOWN_FACT"]
    await assert_nothing_changed(world, before)


async def test_a_mandate_id_belonging_to_another_case_is_not_found(
    storage: StorageDriver,
) -> None:
    world = await accepted_world(storage)
    other = await build_mandate_world(storage, prefix="other-case", seed=False)
    await other.accept_candidate()
    assert other.case_id != world.case_id
    foreign_mandate = json_of(other.thread("resident-a"))["mandate_id"]
    before = await snapshot(world)

    response = world.decide(
        "resident-a",
        foreign_mandate,
        terminal_body({"current_version": 1}, "REFUSE"),
        key="foreign-mandate-0001",
    )

    assert response.status_code == 404
    await assert_nothing_changed(world, before)


async def test_a_resident_cannot_read_another_residents_thread(
    storage: StorageDriver,
) -> None:
    world = await accepted_world(storage)

    response = world.thread("resident-b", actor="resident_a")

    assert response.status_code == 404
    assert json_of(response)["code"] == "NOT_FOUND"


async def test_a_contributor_id_that_names_nobody_answers_the_same_as_a_neighbour(
    storage: StorageDriver,
) -> None:
    world = await accepted_world(storage)

    neighbour = world.thread("resident-b", actor="resident_a")
    invented = world.client.get(
        f"/v1/contributors/{uuid4()}/mandates/current",
        params={"case_id": str(world.case_id)},
        headers=world.api.actor_headers("resident_a"),
    )

    assert neighbour.status_code == invented.status_code == 404
    assert json_of(neighbour) == pytest.approx(json_of(neighbour))
    assert json_of(neighbour)["detail"] == json_of(invented)["detail"]
    assert json_of(neighbour)["code"] == json_of(invented)["code"]


# -- overbroad scope --------------------------------------------------------------------


@pytest.mark.parametrize("fact_type", ["HEALTH_DETAIL", "UNIT_LOCATION"])
async def test_an_internal_only_fact_cannot_be_granted_any_export_scope(
    storage: StorageDriver, fact_type: str
) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-b"))
    before = await snapshot(world)

    response = world.decide(
        "resident-b",
        thread["mandate_id"],
        adjust_body(thread, {fact_type: "EXTERNAL_ACTION"}),
        key=f"overbroad-{fact_type}",
    )

    assert response.status_code == 422
    assert json_of(response)["errors"] == ["SCOPE_EXCEEDS_POLICY_MAXIMUM"]
    await assert_nothing_changed(world, before)


async def test_an_identity_fact_cannot_be_granted_at_external_action(
    storage: StorageDriver,
) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-b"))

    response = world.decide(
        "resident-b",
        thread["mandate_id"],
        adjust_body(thread, {"IDENTITY_ATTRIBUTE": "EXTERNAL_ACTION"}),
        key="identity-overbroad",
    )

    assert response.status_code == 422
    assert json_of(response)["errors"] == ["SCOPE_EXCEEDS_POLICY_MAXIMUM"]


async def test_identity_permission_cannot_exceed_its_own_ceiling(
    storage: StorageDriver,
) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-b"))

    response = world.decide(
        "resident-b",
        thread["mandate_id"],
        adjust_body(
            thread,
            {},
            identity={"externally_shareable": True, "max_scope": "EXTERNAL_ACTION"},
        ),
        key="identity-ceiling",
    )

    assert response.status_code == 422
    assert json_of(response)["errors"] == ["IDENTITY_EXCEEDS_POLICY_MAXIMUM"]


async def test_a_duplicate_fact_grant_is_refused(storage: StorageDriver) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    fact = thread["fact_permissions"][0]["fact_id"]
    before = await snapshot(world)

    response = world.decide(
        "resident-a",
        thread["mandate_id"],
        {
            "expected_version": 1,
            "decision": "ADJUST",
            "fact_grants": [
                grant(fact, "ANONYMOUS_CASE"),
                grant(fact, "INTERNAL_ONLY"),
            ],
            "identity_grant": {"externally_shareable": False, "max_scope": "ANONYMOUS_CASE"},
            "expires_at": None,
        },
        key="duplicate-grant",
    )

    assert response.status_code == 422
    assert json_of(response)["errors"] == ["DUPLICATE_FACT_GRANT"]
    await assert_nothing_changed(world, before)


# -- identity and content stay separate -------------------------------------------------


async def test_granting_every_fact_at_its_ceiling_confers_no_identity_permission(
    storage: StorageDriver,
) -> None:
    """The pairwise property, end to end: maximal content, zero identity."""

    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-b"))

    response = world.decide(
        "resident-b",
        thread["mandate_id"],
        adjust_body(
            thread,
            {
                "INCIDENT_OCCURRENCE": "EXTERNAL_ACTION",
                "SERVICE_IMPACT": "EXTERNAL_ACTION",
                "EVIDENCE_DESCRIPTION": "EXTERNAL_ACTION",
                "IDENTITY_ATTRIBUTE": "NAMED_CASE",
            },
        ),
        key="max-content-no-identity",
    )

    assert response.status_code == 200, response.text
    after = json_of(world.thread("resident-b"))
    assert after["identity_permission"]["externally_shareable"] is False
    assert after["identity_permission"]["max_scope"] == "ANONYMOUS_CASE"


async def test_named_case_content_without_identity_permission_is_recorded_as_written(
    storage: StorageDriver,
) -> None:
    """A NAMED_CASE grant on a name is legal and, on its own, still exports nothing.

    Content and identity are independent truth dimensions, so the mandate records exactly what
    was said rather than inferring the missing half. The compiler's identity gate is what
    refuses to export the name; the mandate's job is to not quietly invent consent.
    """

    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-b"))

    response = world.decide(
        "resident-b",
        thread["mandate_id"],
        adjust_body(thread, {"IDENTITY_ATTRIBUTE": "NAMED_CASE"}),
        key="named-without-identity",
    )

    assert response.status_code == 200, response.text
    after = json_of(world.thread("resident-b"))
    identity_row = next(
        row for row in after["fact_permissions"] if row["fact_type"] == "IDENTITY_ATTRIBUTE"
    )
    assert identity_row["current_scope"] == "NAMED_CASE"
    assert after["identity_permission"]["externally_shareable"] is False


# -- decision shape ---------------------------------------------------------------------


async def test_approve_with_altered_terms_is_refused(storage: StorageDriver) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    body = approve_body(thread)
    body["fact_grants"][0]["max_scope"] = "EXTERNAL_ACTION"
    before = await snapshot(world)

    response = world.decide("resident-a", thread["mandate_id"], body, key="approve-altered")

    assert response.status_code == 409
    assert json_of(response)["code"] == "STATE_TRANSITION_ERROR"
    await assert_nothing_changed(world, before)


async def test_approve_that_drops_a_grant_is_refused(storage: StorageDriver) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-b"))
    body = approve_body(thread)
    body["fact_grants"] = body["fact_grants"][:-1]

    response = world.decide("resident-b", thread["mandate_id"], body, key="approve-partial")

    assert response.status_code == 409


@pytest.mark.parametrize("decision", ["REFUSE", "REVOKE"])
async def test_a_terminal_decision_carrying_a_grant_is_refused(
    storage: StorageDriver, decision: str
) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    if decision == "REVOKE":
        approved = world.decide(
            "resident-a", thread["mandate_id"], approve_body(thread), key="approve-first"
        )
        assert approved.status_code == 200
        thread = json_of(world.thread("resident-a"))
    body = terminal_body(thread, decision)
    body["fact_grants"] = [grant(thread["fact_permissions"][0]["fact_id"], "ANONYMOUS_CASE")]
    before = await snapshot(world)

    response = world.decide("resident-a", thread["mandate_id"], body, key=f"{decision}-grants")

    assert response.status_code == 409
    await assert_nothing_changed(world, before)


@pytest.mark.parametrize("decision", ["REFUSE", "REVOKE"])
async def test_a_terminal_decision_carrying_identity_permission_is_refused(
    storage: StorageDriver, decision: str
) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    if decision == "REVOKE":
        world.decide("resident-a", thread["mandate_id"], approve_body(thread), key="approve-first")
        thread = json_of(world.thread("resident-a"))
    body = terminal_body(thread, decision)
    body["identity_grant"] = {"externally_shareable": True, "max_scope": "NAMED_CASE"}

    response = world.decide("resident-a", thread["mandate_id"], body, key=f"{decision}-identity")

    assert response.status_code == 409


async def test_revoking_a_proposal_is_refused(storage: StorageDriver) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    before = await snapshot(world)

    response = world.decide(
        "resident-a", thread["mandate_id"], terminal_body(thread, "REVOKE"), key="revoke-proposal"
    )

    assert response.status_code == 409
    await assert_nothing_changed(world, before)


async def test_a_refused_mandate_accepts_no_further_decision(storage: StorageDriver) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    refused = world.decide(
        "resident-a", thread["mandate_id"], terminal_body(thread, "REFUSE"), key="refuse-0001"
    )
    assert refused.status_code == 200
    assert json_of(refused)["status"] == MandateStatus.REFUSED.value

    after = json_of(world.thread("resident-a"))
    before = await snapshot(world)
    response = world.decide(
        "resident-a", after["mandate_id"], approve_body(after), key="approve-after-refuse"
    )

    assert response.status_code == 409
    await assert_nothing_changed(world, before)


async def test_a_revoked_mandate_accepts_no_further_decision(storage: StorageDriver) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    world.decide("resident-a", thread["mandate_id"], approve_body(thread), key="approve-0001")
    approved = json_of(world.thread("resident-a"))
    revoked = world.decide(
        "resident-a", approved["mandate_id"], terminal_body(approved, "REVOKE"), key="revoke-0001"
    )
    assert revoked.status_code == 200
    assert json_of(revoked)["status"] == MandateStatus.REVOKED.value
    assert json_of(revoked)["revoked_at"] is not None

    after = json_of(world.thread("resident-a"))
    before = await snapshot(world)
    response = world.decide(
        "resident-a", after["mandate_id"], adjust_body(after, {}), key="adjust-after-revoke"
    )

    assert response.status_code == 409
    await assert_nothing_changed(world, before)


# -- staleness --------------------------------------------------------------------------


async def test_a_stale_expected_mandate_version_is_refused(storage: StorageDriver) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    world.decide("resident-a", thread["mandate_id"], approve_body(thread), key="approve-stale-0")
    before = await snapshot(world)

    # The caller still believes version 1 is current.
    response = world.decide(
        "resident-a",
        thread["mandate_id"],
        {
            "expected_version": 1,
            "decision": "REFUSE",
            "fact_grants": [],
            "identity_grant": {"externally_shareable": False, "max_scope": "ANONYMOUS_CASE"},
            "expires_at": None,
        },
        key="stale-version-0001",
    )

    assert response.status_code == 409
    assert json_of(response)["code"] == "STALE_AUTHORIZATION"
    assert json_of(response)["errors"] == ["STALE_MANDATE_VERSION"]
    await assert_nothing_changed(world, before)


async def test_a_stale_expected_case_version_refuses_acceptance(
    storage: StorageDriver,
) -> None:
    world = await build_mandate_world(storage)
    current = await world.case_version()

    response = world.propose(expected_case_version=current + 5, key="stale-case-0001")

    assert response.status_code == 409
    assert json_of(response)["code"] == "STALE_AUTHORIZATION"


# -- expiry -----------------------------------------------------------------------------


async def test_an_expiry_at_the_decision_instant_is_refused(storage: StorageDriver) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    now = world.api.harness.clock.now()

    response = world.decide(
        "resident-a",
        thread["mandate_id"],
        adjust_body(thread, {}, expires_at=format_utc(now)),
        key="expiry-equal",
    )

    assert response.status_code == 422
    assert json_of(response)["errors"] == ["EXPIRY_ALREADY_PASSED"]


async def test_an_expiry_in_the_past_is_refused(storage: StorageDriver) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    now = world.api.harness.clock.now()

    response = world.decide(
        "resident-a",
        thread["mandate_id"],
        adjust_body(thread, {}, expires_at=format_utc(now - timedelta(microseconds=1))),
        key="expiry-past",
    )

    assert response.status_code == 422


async def test_a_decision_on_an_expired_mandate_is_refused(storage: StorageDriver) -> None:
    """Set an expiry, step the clock exactly onto it, and try to act."""

    world = await accepted_world(storage)
    clock = world.api.harness.clock
    thread = json_of(world.thread("resident-a"))
    expiry = clock.now() + timedelta(seconds=60)
    adjusted = world.decide(
        "resident-a",
        thread["mandate_id"],
        adjust_body(
            thread, {"INCIDENT_OCCURRENCE": "ANONYMOUS_CASE"}, expires_at=format_utc(expiry)
        ),
        key="set-expiry",
    )
    assert adjusted.status_code == 200, adjusted.text

    clock.advance(seconds=60)
    current = json_of(world.thread("resident-a"))
    assert current["status"] == MandateStatus.EXPIRED.value
    before = await snapshot(world)

    response = world.decide(
        "resident-a", current["mandate_id"], terminal_body(current, "REVOKE"), key="revoke-expired"
    )

    assert response.status_code == 422
    assert json_of(response)["errors"] == ["MANDATE_EXPIRED"]
    await assert_nothing_changed(world, before)


async def test_a_decision_one_microsecond_before_expiry_still_works(
    storage: StorageDriver,
) -> None:
    world = await accepted_world(storage)
    clock = world.api.harness.clock
    thread = json_of(world.thread("resident-a"))
    expiry = clock.now() + timedelta(seconds=60)
    world.decide(
        "resident-a",
        thread["mandate_id"],
        adjust_body(
            thread, {"INCIDENT_OCCURRENCE": "ANONYMOUS_CASE"}, expires_at=format_utc(expiry)
        ),
        key="set-expiry-2",
    )

    clock.advance(seconds=59)
    current = json_of(world.thread("resident-a"))
    assert current["status"] == MandateStatus.APPROVED.value
    response = world.decide(
        "resident-a", current["mandate_id"], terminal_body(current, "REVOKE"), key="revoke-in-time"
    )

    assert response.status_code == 200, response.text


# -- transport ---------------------------------------------------------------------------


async def test_an_unknown_request_field_is_refused_without_being_echoed(
    storage: StorageDriver,
) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    body = approve_body(thread)
    body["motherLeelaAsthma4B"] = "please export this"
    before = await snapshot(world)

    response = world.decide("resident-a", thread["mandate_id"], body, key="unknown-field")

    assert response.status_code == 422
    assert "motherLeelaAsthma4B" not in response.text
    assert "asthma" not in response.text.lower()
    await assert_nothing_changed(world, before)


async def test_an_unknown_scope_is_refused_by_the_transport_schema(
    storage: StorageDriver,
) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    body = approve_body(thread)
    body["fact_grants"][0]["max_scope"] = "EVERYTHING_FOREVER"

    response = world.decide("resident-a", thread["mandate_id"], body, key="unknown-scope")

    assert response.status_code == 422
    assert "EVERYTHING_FOREVER" not in response.text


async def test_an_unknown_decision_word_is_refused(storage: StorageDriver) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    body = approve_body(thread)
    body["decision"] = "APPROVE_EVERYTHING"

    response = world.decide("resident-a", thread["mandate_id"], body, key="unknown-decision")

    assert response.status_code == 422


async def test_a_missing_idempotency_key_is_refused(storage: StorageDriver) -> None:
    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    before = await snapshot(world)

    response = world.client.post(
        f"/v1/cases/{world.case_id}/mandates/{thread['mandate_id']}/decisions",
        json=approve_body(thread),
        headers=world.api.actor_headers("resident_a"),
    )

    assert response.status_code == 422
    await assert_nothing_changed(world, before)


async def test_a_private_value_in_a_malformed_request_is_never_echoed(
    storage: StorageDriver,
) -> None:
    """The error path is not a disclosure channel, whatever a caller writes into it."""

    world = await accepted_world(storage)
    thread = json_of(world.thread("resident-a"))
    secret = "mother Leela has asthma and we live in apartment 4B"
    body = approve_body(thread)
    body["fact_grants"][0]["fact_id"] = secret

    response = world.decide("resident-a", thread["mandate_id"], body, key="private-in-error")

    assert response.status_code == 422
    assert secret not in response.text
    assert "asthma" not in response.text.lower()
    assert "4B" not in response.text


async def test_the_thread_never_carries_a_raw_fact_value(storage: StorageDriver) -> None:
    """Resident B's health text, unit label, and name exist; none of them is in the response."""

    world = await accepted_world(storage)

    text = world.thread("resident-b").text

    assert "asthma" not in text.lower()
    assert "4B" not in text
    assert "Resident B" not in text
    assert "stuck between floors" not in text
    # What is there instead is contributor-facing wording built from closed typed fields.
    assert "A health detail you shared." in text
    assert "Your apartment or unit." in text
