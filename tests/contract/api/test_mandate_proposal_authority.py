"""Who decides what a version-1 proposal says, and what it is allowed to say.

Every assertion here is about one of two claims the Phase-4 freeze gate would not accept on
trust.

**The Monitor contributes nothing.** ADR-014 removed the last field through which a model
could name a scope, a purpose, or a set of facts that may travel. The first group of tests
proves the removal at the level that matters — the schema the model is handed — rather than by
checking that some validator copes with the field, which is what the old design did.

**A policy ceiling is not a proposed grant.** ``policy_maximum_scope`` caps what any decision
may say; ``proposed_scope`` is what version 1 actually offers. For a general incident fact
those are ``EXTERNAL_ACTION`` and ``ANONYMOUS_CASE``, and the difference between them is the
whole security margin of the mandate thread. The second and third groups read the *stored*
version-1 rows and the *stored* approval, because a projection that rendered the right number
while the durable row said something wider is exactly the failure worth catching.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError
from tests.fixtures.drivers import DRIVER_PARAMS, storage_driver
from tests.fixtures.mandates import (
    MandateWorld,
    approve_body,
    build_mandate_world,
    json_of,
)

from chorus.contracts.monitor import MonitorOutput
from chorus.domain.entities import CaseState, DisclosureScope, FactType, MandateStatus
from chorus.domain.facts import Fact, FactStatus
from chorus.domain.ids import ContributorId, FactId, MandateId
from chorus.domain.mandates import DisclosureMandate
from chorus.ports.pagination import PageRequest
from chorus.ports.storage import StorageDriver
from chorus.ports.unit_of_work import TransactionPlan
from chorus.privacy.policy import (
    ALLOWED_PURPOSES,
    MandateDenialCode,
    policy_maximum_scope,
    proposed_scope,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(params=DRIVER_PARAMS)
def storage(request: pytest.FixtureRequest) -> Iterator[StorageDriver]:
    yield from storage_driver(str(request.param), prefix="mandate-authority")


async def stored_proposals(world: MandateWorld) -> dict[ContributorId, DisclosureMandate]:
    """Every version-1 row this case holds, read from storage rather than from a response."""

    core = world.api.harness.core
    page = await core.load_current_mandate_pointers(world.case_scope, PageRequest(limit=100))
    return {
        pointer.pointer.contributor_id: await core.load_mandate_version(
            world.case_scope, pointer.pointer.mandate_id, 1
        )
        for pointer in page.items
    }


async def facts_by_id(world: MandateWorld) -> dict[FactId, Fact]:
    scope = world.case_scope
    case = await world.api.harness.core.load_case(scope)
    facts = await world.api.harness.core.load_facts(scope, case.fact_ids)
    return {fact.fact_id: fact for fact in facts}


# ---------------------------------------------------------------------------------------
# 1-3, 6. The Monitor has no field through which to influence any of this
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "suggestion",
    [
        pytest.param(
            {
                "report_client_ref": "report-000",
                "fact_client_refs": ["fact-000"],
                "suggested_max_scope": "ANONYMOUS_CASE",
                "suggested_purpose": "REQUEST_ELEVATOR_REPAIR_AND_RESPONSE",
            },
            id="narrower-than-the-ceiling",
        ),
        pytest.param(
            {
                "report_client_ref": "report-000",
                "fact_client_refs": ["fact-000"],
                "suggested_max_scope": "EXTERNAL_ACTION",
                "suggested_purpose": "REQUEST_ELEVATOR_REPAIR_AND_RESPONSE",
            },
            id="wider-than-the-ceiling",
        ),
        pytest.param(
            {
                "report_client_ref": "report-000",
                "fact_client_refs": ["fact-belonging-to-someone-else"],
                "suggested_max_scope": "INTERNAL_ONLY",
                "suggested_purpose": "REQUEST_ELEVATOR_REPAIR_AND_RESPONSE",
            },
            id="foreign-fact",
        ),
        pytest.param(
            {
                "report_client_ref": "report-000",
                "fact_client_refs": ["fact-000"],
                "suggested_max_scope": "ANONYMOUS_CASE",
                "suggested_purpose": "SOMETHING_POLICY_NEVER_HEARD_OF",
            },
            id="unsupported-purpose",
        ),
    ],
)
def test_no_monitor_answer_can_express_a_disclosure_term(suggestion: dict[str, Any]) -> None:
    """Scenarios 1, 2, 4, 5 and 6, refused at the schema rather than downstream.

    Each of these used to be a distinct question about what the proposal builder would do with
    a suggestion. Since ADR-014 they are one question with one answer: the answer does not
    parse, so nothing further in the pipeline is ever asked. That is a stronger guarantee than
    "the builder clamps it", because it does not depend on the builder.
    """

    with pytest.raises(PydanticValidationError):
        MonitorOutput.model_validate(
            {
                "message_results": [
                    {"message_id": str(uuid4()), "classification": "NOISE", "reason": "none"}
                ],
                "mandate_suggestions": [suggestion],
            }
        )


def test_no_monitor_output_model_carries_a_scope_or_a_purpose() -> None:
    """The removal is structural: no nested model smuggled the vocabulary back in."""

    def fields_of(model: type[Any]) -> set[str]:
        return set(getattr(model, "model_fields", {}))

    seen: set[type[Any]] = set()
    pending: list[type[Any]] = [MonitorOutput]
    names: set[str] = set()
    while pending:
        model = pending.pop()
        if model in seen or not fields_of(model):
            continue
        seen.add(model)
        for name, field in model.model_fields.items():
            names.add(name)
            annotation = field.annotation
            for candidate in (annotation, *getattr(annotation, "__args__", ())):
                if isinstance(candidate, type) and fields_of(candidate):
                    pending.append(candidate)

    assert "mandate_suggestions" not in names
    assert not {"suggested_max_scope", "suggested_purpose", "max_scope"} & names


# ---------------------------------------------------------------------------------------
# 1, 2, 3. What a version-1 proposal actually says, read from storage
# ---------------------------------------------------------------------------------------


async def test_a_general_incident_fact_is_proposed_anonymous_not_at_its_ceiling(
    storage: StorageDriver,
) -> None:
    """Scenario 1, stated as the gate stated it.

    An ``INCIDENT_OCCURRENCE`` carrying ``GENERAL`` sensitivity has a policy ceiling of
    ``EXTERNAL_ACTION``. The stored version-1 grant is ``ANONYMOUS_CASE``. If these two were
    ever allowed to coincide, approving a proposal would authorize the broadest capability
    policy permits, and the contributor would never have been asked a question.
    """

    world = await build_mandate_world(storage)
    await world.accept_candidate()

    facts = await facts_by_id(world)
    proposals = await stored_proposals(world)
    incident_grants = [
        grant
        for mandate in proposals.values()
        for grant in mandate.fact_grants
        if facts[grant.fact_id].fact_type is FactType.INCIDENT_OCCURRENCE
    ]

    assert incident_grants, "the discovered case must hold at least one incident fact"
    for grant in incident_grants:
        fact = facts[grant.fact_id]
        assert policy_maximum_scope(fact.fact_type, fact.sensitivity) is (
            DisclosureScope.EXTERNAL_ACTION
        )
        assert grant.max_scope is DisclosureScope.ANONYMOUS_CASE


async def test_no_proposed_grant_ever_reaches_its_own_ceiling_where_one_is_higher(
    storage: StorageDriver,
) -> None:
    """Scenarios 2 and 3 together: nothing widens, and nothing is manufactured.

    Every stored grant equals ``proposed_scope`` for that exact fact — not the ceiling, and not
    something in between. A fact whose ceiling is ``INTERNAL_ONLY`` is proposed
    ``INTERNAL_ONLY`` and carries no transformation permission either, so a health detail and
    an apartment number appear in the thread as visibly locked rows rather than as absences the
    contributor has to notice.
    """

    world = await build_mandate_world(storage)
    await world.accept_candidate()

    facts = await facts_by_id(world)
    proposals = await stored_proposals(world)
    saw_a_gap = False
    for mandate in proposals.values():
        for grant in mandate.fact_grants:
            fact = facts[grant.fact_id]
            offered = proposed_scope(fact.fact_type, fact.sensitivity)
            ceiling = policy_maximum_scope(fact.fact_type, fact.sensitivity)
            assert grant.max_scope is offered
            if offered is not ceiling:
                saw_a_gap = True
            if grant.max_scope is DisclosureScope.INTERNAL_ONLY:
                assert grant.allow_safe_transformation is False

    assert saw_a_gap, "a case where offer and ceiling always coincide proves nothing"


async def test_a_proposal_offers_no_identity_permission_however_wide_the_content_is(
    storage: StorageDriver,
) -> None:
    world = await build_mandate_world(storage)
    await world.accept_candidate()

    for mandate in (await stored_proposals(world)).values():
        assert mandate.identity_grant.externally_shareable is False
        assert mandate.identity_grant.max_scope is DisclosureScope.ANONYMOUS_CASE


# ---------------------------------------------------------------------------------------
# 4, 5. A proposal contains exactly its owner's own facts
# ---------------------------------------------------------------------------------------


async def test_a_proposal_contains_every_fact_its_owner_holds_and_no_other(
    storage: StorageDriver,
) -> None:
    """Scenarios 4 and 5: membership is decided by stored ownership, never by a citation.

    A fact reaches a contributor's proposal because the *stored* fact names them as its owner,
    is ``ACTIVE``, and sits in this case, community, and namespace. There is no other route in,
    so a suggestion naming a neighbour's fact — or a fact of the right owner attached to the
    wrong report — has nothing to attach itself to.
    """

    world = await build_mandate_world(storage)
    await world.accept_candidate()

    facts = await facts_by_id(world)
    proposals = await stored_proposals(world)

    granted_ids: list[FactId] = []
    for owner, mandate in proposals.items():
        expected = {
            fact_id
            for fact_id, fact in facts.items()
            if fact.contributor_id == owner and fact.status is FactStatus.ACTIVE
        }
        assert {grant.fact_id for grant in mandate.fact_grants} == expected
        assert mandate.contributor_id == owner
        granted_ids.extend(grant.fact_id for grant in mandate.fact_grants)

    # No fact is offered to two people, which is the same statement read the other way round.
    assert len(granted_ids) == len(set(granted_ids))


async def test_a_facts_owner_is_the_only_person_whose_thread_can_see_it(
    storage: StorageDriver,
) -> None:
    world = await build_mandate_world(storage)
    await world.accept_candidate()

    facts = await facts_by_id(world)
    sensitive = [
        fact_id for fact_id, fact in facts.items() if fact.fact_type is FactType.HEALTH_DETAIL
    ]
    assert sensitive, "the fixture must carry a health detail for this to mean anything"

    for pseudonym in ("resident-a", "resident-c", "resident-d"):
        thread = json_of(world.thread(pseudonym))
        rows = {row["fact_id"] for row in thread["fact_permissions"]}
        assert not rows & {str(fact_id) for fact_id in sensitive}


# ---------------------------------------------------------------------------------------
# 6. Purpose and destination come from policy, never from a request or an answer
# ---------------------------------------------------------------------------------------


async def test_every_proposal_carries_exactly_the_policy_purpose_set(
    storage: StorageDriver,
) -> None:
    world = await build_mandate_world(storage)
    await world.accept_candidate()

    for mandate in (await stored_proposals(world)).values():
        assert set(mandate.allowed_purposes) == set(ALLOWED_PURPOSES)
        assert len(mandate.allowed_destination_ids) == 1


# ---------------------------------------------------------------------------------------
# 7. A participating contributor with nothing suggested about them
# ---------------------------------------------------------------------------------------


async def test_every_participating_owner_is_asked_even_though_nothing_suggested_them(
    storage: StorageDriver,
) -> None:
    """Scenario 7's architecturally correct behaviour, stated positively.

    There is no suggestion for anybody, so "no suggestion for this contributor" is the only
    case there is. Participation is owning at least one ``ACTIVE`` fact, and every participant
    gets a complete proposal at the deterministic defaults. Nothing is skipped for want of a
    hint, and nothing is invented at maximum scope to fill the hint's place.
    """

    world = await build_mandate_world(storage)
    await world.accept_candidate()

    facts = await facts_by_id(world)
    owners = {fact.contributor_id for fact in facts.values() if fact.status is FactStatus.ACTIVE}
    proposals = await stored_proposals(world)

    assert set(proposals) == owners
    for mandate in proposals.values():
        assert mandate.version == 1
        assert mandate.status is MandateStatus.PROPOSED
        assert mandate.fact_grants


async def test_a_case_whose_facts_are_all_withdrawn_is_refused_rather_than_accepted(
    storage: StorageDriver,
) -> None:
    """The other half of scenario 7: no participant means no transition, not an empty one.

    Every fact is withdrawn first, so the case is real, is still ``CANDIDATE``, and has nobody
    who could answer a mandate. It stays a candidate. Transitioning it would satisfy the frozen
    "proposals exist for every participating owner" guard vacuously and leave an
    ``AWAITING_MANDATES`` case that can never reach ``INVESTIGATING``, because the decision that
    would move it can never arrive.
    """

    world = await build_mandate_world(storage)
    await _withdraw_every_fact(world)

    response = world.propose(expected_case_version=await world.case_version())

    assert response.status_code == 422, response.text
    assert json_of(response)["code"] == "POLICY_DENIED"
    assert json_of(response)["errors"] == [MandateDenialCode.NO_GRANTABLE_FACT.value]
    case = await world.api.harness.core.load_case(world.case_scope)
    assert case.state is CaseState.CANDIDATE
    page = await world.api.harness.core.load_current_mandate_pointers(
        world.case_scope, PageRequest(limit=100)
    )
    assert page.items == ()


async def _withdraw_every_fact(world: MandateWorld) -> None:
    """Mark the case's facts ``WITHDRAWN`` through the real repository and unit of work."""

    harness = world.api.harness
    scope = world.case_scope
    case = await harness.core.load_case(scope)
    facts = await harness.core.load_facts(scope, case.fact_ids)
    operations = tuple(
        harness.core.stage_update_fact(
            scope,
            replace(fact, status=FactStatus.WITHDRAWN, version=fact.version + 1),
            expected_version=fact.version,
        )
        for fact in facts
    )
    await harness.unit_of_work.commit(
        TransactionPlan(name="withdraw-facts", operations=operations, audit_required=False)
    )


# ---------------------------------------------------------------------------------------
# 8. Approval stores the reviewed proposal, not a recomputed ceiling
# ---------------------------------------------------------------------------------------


async def test_approving_stores_the_reviewed_terms_and_not_the_policy_ceiling(
    storage: StorageDriver,
) -> None:
    """Scenario 8, proved against the durable row.

    The approval is submitted as the thread rendered it, and version 2 is then read back out of
    storage and compared *grant for grant* against version 1. The incident fact is called out
    separately because it is the one where a recomputation would be invisible in a count and
    obvious in a scope: ``ANONYMOUS_CASE`` approved, ``EXTERNAL_ACTION`` available.
    """

    world = await build_mandate_world(storage)
    await world.accept_candidate()

    thread = json_of(world.thread("resident-b"))
    mandate_id = thread["mandate_id"]
    response = world.decide(
        "resident-b", mandate_id, approve_body(thread), key="approve-exactly-0001"
    )
    assert response.status_code == 200, response.text

    core = world.api.harness.core
    scope = world.case_scope
    version_one = await core.load_mandate_version(scope, MandateId(UUID(mandate_id)), 1)
    version_two = await core.load_mandate_version(scope, MandateId(UUID(mandate_id)), 2)

    assert version_two.status is MandateStatus.APPROVED
    assert version_two.supersedes_version == 1
    assert set(version_two.fact_grants) == set(version_one.fact_grants)
    assert version_two.identity_grant == version_one.identity_grant
    assert version_two.expires_at == version_one.expires_at
    assert version_two.allowed_purposes == version_one.allowed_purposes
    assert version_two.allowed_destination_ids == version_one.allowed_destination_ids

    facts = await facts_by_id(world)
    incident = [
        grant
        for grant in version_two.fact_grants
        if facts[grant.fact_id].fact_type is FactType.INCIDENT_OCCURRENCE
    ]
    assert incident and all(grant.max_scope is DisclosureScope.ANONYMOUS_CASE for grant in incident)
    locked = [
        grant
        for grant in version_two.fact_grants
        if facts[grant.fact_id].fact_type in {FactType.HEALTH_DETAIL, FactType.UNIT_LOCATION}
    ]
    assert locked and all(grant.max_scope is DisclosureScope.INTERNAL_ONLY for grant in locked)


async def test_the_current_pointer_names_the_approved_version_and_its_hash(
    storage: StorageDriver,
) -> None:
    """The pointer is what the compiler reads, so it must agree with the row it names."""

    world = await build_mandate_world(storage)
    await world.accept_candidate()

    thread = json_of(world.thread("resident-a"))
    mandate_id = MandateId(UUID(thread["mandate_id"]))
    world.decide("resident-a", str(mandate_id), approve_body(thread), key="approve-pointer-1")

    core = world.api.harness.core
    pointer = await core.load_current_mandate_pointer(world.case_scope, mandate_id)
    stored = await core.load_mandate_version(world.case_scope, mandate_id, pointer.pointer.version)

    assert pointer.pointer.version == 2
    assert pointer.pointer.terms_hash == stored.terms_hash
    assert pointer.status is MandateStatus.APPROVED
