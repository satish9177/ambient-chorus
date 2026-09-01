"""Shareable-table repository contract: pointers, history, capacity, and scope denial."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

import pytest
from tests.fixtures.persistence import (
    NOW,
    OTHER_CASE,
    PRIMARY,
    build_repositories,
    digest,
    relocated,
)

from chorus.domain.entities import ActionExecutionState, Approval, ApprovalDecision
from chorus.domain.ids import ApprovalId, CommitmentId, ViewId
from chorus.infrastructure.dynamodb import codec_share
from chorus.ports.errors import (
    CrossCaseViolationError,
    ModelLimitExceededError,
    NotFoundError,
    PersistenceConflictError,
)
from chorus.ports.limits import (
    MAX_ACTIONS_PER_CASE,
    MAX_COMMITMENTS_PER_CASE,
    MAX_VIEWS_PER_CASE,
)
from chorus.ports.pagination import PageRequest
from chorus.ports.records import ActionPointerExpectation, ViewPointerExpectation
from chorus.ports.storage import StorageDriver

pytestmark = pytest.mark.anyio


async def test_a_compiled_view_round_trips(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    view = PRIMARY.view()

    await storage.write_item(repositories.shareable.stage_append_view(PRIMARY.case_scope, view))

    assert await repositories.shareable.load_view(PRIMARY.case_scope, view.view_id) == view


async def test_a_view_is_immutable_once_written(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    operation = repositories.shareable.stage_append_view(PRIMARY.case_scope, PRIMARY.view())
    await storage.write_item(operation)

    with pytest.raises(PersistenceConflictError):
        await storage.write_item(operation)


async def test_a_view_from_another_case_is_denied(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    foreign = codec_share.encode_view(OTHER_CASE.case_scope, OTHER_CASE.view())
    key = codec_share.view_key(PRIMARY.case_scope, OTHER_CASE.view_id)

    await storage.write_item(relocated(foreign, key))

    with pytest.raises(CrossCaseViolationError):
        await repositories.shareable.load_view(PRIMARY.case_scope, OTHER_CASE.view_id)


async def test_an_absent_view_is_not_found(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)

    with pytest.raises(NotFoundError):
        await repositories.shareable.load_view(PRIMARY.case_scope, PRIMARY.view_id)


async def test_the_current_view_pointer_is_created_once_then_guarded(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    first = PRIMARY.view_pointer(version=1, index=0)
    await storage.write_item(
        repositories.shareable.stage_replace_current_view_pointer(
            PRIMARY.case_scope, first, expected=None
        )
    )

    with pytest.raises(PersistenceConflictError):
        await storage.write_item(
            repositories.shareable.stage_replace_current_view_pointer(
                PRIMARY.case_scope, first, expected=None
            )
        )

    assert await repositories.shareable.load_current_view_pointer(PRIMARY.case_scope) == first


async def test_a_view_pointer_replace_is_guarded_on_the_exact_view_hash(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    await storage.write_item(
        repositories.shareable.stage_replace_current_view_pointer(
            PRIMARY.case_scope, PRIMARY.view_pointer(version=1, index=0), expected=None
        )
    )
    second = PRIMARY.view_pointer(version=2, index=1)

    with pytest.raises(PersistenceConflictError):
        await storage.write_item(
            repositories.shareable.stage_replace_current_view_pointer(
                PRIMARY.case_scope,
                second,
                expected=ViewPointerExpectation(
                    row_version=1, view_hash=digest("a-view-nobody-published")
                ),
            )
        )

    await storage.write_item(
        repositories.shareable.stage_replace_current_view_pointer(
            PRIMARY.case_scope,
            second,
            expected=ViewPointerExpectation(
                row_version=1, view_hash=PRIMARY.view_pointer(index=0).view_hash
            ),
        )
    )
    assert await repositories.shareable.load_current_view_pointer(PRIMARY.case_scope) == second


async def test_an_action_pointer_replace_is_guarded_on_the_exact_proposal_hash(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    await storage.write_item(
        repositories.shareable.stage_replace_current_action_pointer(
            PRIMARY.case_scope, PRIMARY.action_pointer(version=1, index=0), expected=None
        )
    )
    second = PRIMARY.action_pointer(version=2, index=1)

    with pytest.raises(PersistenceConflictError):
        await storage.write_item(
            repositories.shareable.stage_replace_current_action_pointer(
                PRIMARY.case_scope,
                second,
                expected=ActionPointerExpectation(
                    row_version=1, proposal_hash=digest("a-proposal-nobody-made")
                ),
            )
        )

    await storage.write_item(
        repositories.shareable.stage_replace_current_action_pointer(
            PRIMARY.case_scope,
            second,
            expected=ActionPointerExpectation(
                row_version=1, proposal_hash=PRIMARY.action_pointer(index=0).proposal_hash
            ),
        )
    )
    assert await repositories.shareable.load_current_action_pointer(PRIMARY.case_scope) == second


async def test_a_first_pointer_write_must_be_version_one(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)

    with pytest.raises(ValueError, match="version 1"):
        repositories.shareable.stage_replace_current_view_pointer(
            PRIMARY.case_scope, PRIMARY.view_pointer(version=2), expected=None
        )


async def test_view_history_paginates_in_generation_order(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    locators = [PRIMARY.view_history(index=index) for index in range(5)]
    for locator in locators:
        await storage.write_item(
            repositories.shareable.stage_append_view_history_locator(PRIMARY.case_scope, locator)
        )

    seen: list[ViewId] = []
    request = PageRequest(limit=2)
    for _ in range(10):
        page = await repositories.shareable.read_view_history(PRIMARY.case_scope, request)
        seen.extend(item.view_id for item in page.items)
        if page.next_cursor is None:
            break
        request = PageRequest(limit=2, cursor=page.next_cursor)

    assert seen == [locator.view_id for locator in sorted(locators, key=lambda x: x.generated_at)]


async def test_the_view_capacity_bound_is_enforced(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    await repositories.shareable.assert_view_capacity(PRIMARY.case_scope)

    for index in range(MAX_VIEWS_PER_CASE):
        await storage.write_item(
            repositories.shareable.stage_append_view_history_locator(
                PRIMARY.case_scope, PRIMARY.view_history(index=index)
            )
        )

    with pytest.raises(ModelLimitExceededError):
        await repositories.shareable.assert_view_capacity(PRIMARY.case_scope)


async def test_the_action_capacity_bound_is_enforced(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    await repositories.shareable.assert_action_capacity(PRIMARY.case_scope)

    for index in range(MAX_ACTIONS_PER_CASE):
        await storage.write_item(
            repositories.shareable.stage_append_action_history_locator(
                PRIMARY.case_scope, PRIMARY.action_history(index=index)
            )
        )

    with pytest.raises(ModelLimitExceededError):
        await repositories.shareable.assert_action_capacity(PRIMARY.case_scope)


async def test_the_commitment_capacity_bound_is_enforced(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    await repositories.shareable.assert_commitment_capacity(PRIMARY.case_scope)

    for index in range(MAX_COMMITMENTS_PER_CASE):
        await storage.write_item(
            repositories.shareable.stage_create_commitment(
                PRIMARY.case_scope,
                PRIMARY.commitment(
                    commitment_id=CommitmentId(PRIMARY.uuid(f"commitment:{index}")), index=index
                ),
            )
        )

    with pytest.raises(ModelLimitExceededError):
        await repositories.shareable.assert_commitment_capacity(PRIMARY.case_scope)


async def test_capacity_counts_only_the_requested_case(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    for index in range(MAX_VIEWS_PER_CASE):
        await storage.write_item(
            repositories.shareable.stage_append_view_history_locator(
                OTHER_CASE.case_scope, OTHER_CASE.view_history(index=index)
            )
        )

    await repositories.shareable.assert_view_capacity(PRIMARY.case_scope)


async def test_a_proposal_approval_and_execution_round_trip(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    scope = PRIMARY.action_scope
    proposal = PRIMARY.proposal()
    approval = PRIMARY.approval()
    execution = PRIMARY.execution()

    await storage.write_item(repositories.shareable.stage_append_proposal(scope, proposal))
    await storage.write_item(repositories.shareable.stage_append_approval(scope, approval))
    await storage.write_item(repositories.shareable.stage_create_execution(scope, execution))

    assert await repositories.shareable.load_proposal(scope) == proposal
    assert await repositories.shareable.load_approval(scope, approval.approval_id) == approval
    assert await repositories.shareable.load_execution(scope, execution.execution_id) == execution


async def test_an_approval_is_consumed_exactly_once(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    scope = PRIMARY.action_scope
    approved = PRIMARY.approval(version=1)
    await storage.write_item(repositories.shareable.stage_append_approval(scope, approved))
    consumed = PRIMARY.approval(version=2, consumed=True)

    await storage.write_item(
        repositories.shareable.stage_consume_approval(scope, consumed, expected=approved)
    )
    with pytest.raises(PersistenceConflictError):
        await storage.write_item(
            repositories.shareable.stage_consume_approval(scope, consumed, expected=approved)
        )

    stored = await repositories.shareable.load_approval(scope, consumed.approval_id)
    assert stored.consumed_at is not None


async def test_consuming_an_approval_requires_a_consumption_timestamp(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)

    with pytest.raises(ValueError, match="consumed_at"):
        repositories.shareable.stage_consume_approval(
            PRIMARY.action_scope,
            PRIMARY.approval(version=2),
            expected=PRIMARY.approval(version=1),
        )


async def test_an_execution_advances_under_optimistic_concurrency(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    scope = PRIMARY.action_scope
    await storage.write_item(
        repositories.shareable.stage_create_execution(scope, PRIMARY.execution(version=1))
    )
    sending = PRIMARY.execution(state=ActionExecutionState.SENDING, version=2)

    await storage.write_item(
        repositories.shareable.stage_update_execution(scope, sending, expected_version=1)
    )
    with pytest.raises(PersistenceConflictError):
        await storage.write_item(
            repositories.shareable.stage_update_execution(scope, sending, expected_version=1)
        )

    stored = await repositories.shareable.load_execution(scope, sending.execution_id)
    assert stored.state is ActionExecutionState.SENDING


async def test_an_execution_from_another_action_is_denied(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    foreign = codec_share.encode_execution(OTHER_CASE.action_scope, OTHER_CASE.execution())
    key = codec_share.execution_key(PRIMARY.action_scope, OTHER_CASE.execution_id)

    await storage.write_item(relocated(foreign, key))

    with pytest.raises(CrossCaseViolationError):
        await repositories.shareable.load_execution(PRIMARY.action_scope, OTHER_CASE.execution_id)


async def test_commitments_page_within_their_case(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    ids = [CommitmentId(PRIMARY.uuid(f"commitment:{index}")) for index in range(4)]
    for index, commitment_id in enumerate(ids):
        await storage.write_item(
            repositories.shareable.stage_create_commitment(
                PRIMARY.case_scope,
                PRIMARY.commitment(commitment_id=commitment_id, index=index),
            )
        )
    await storage.write_item(
        repositories.shareable.stage_create_commitment(
            OTHER_CASE.case_scope, OTHER_CASE.commitment()
        )
    )

    page = await repositories.shareable.read_case_commitments(
        PRIMARY.case_scope, PageRequest(limit=100)
    )

    assert {item.commitment_id for item in page.items} == set(ids)


IMMUTABLE_APPROVAL_MUTATIONS: tuple[tuple[str, Callable[[Approval], Approval]], ...] = (
    ("proposal_hash", lambda a: replace(a, proposal_hash=digest("another-proposal"))),
    ("view_hash", lambda a: replace(a, view_hash=digest("another-view"))),
    ("decision", lambda a: replace(a, decision=ApprovalDecision.REJECTED)),
    ("approver_id", lambda a: replace(a, approver_id=OTHER_CASE.contributor_id)),
    ("expires_at", lambda a: replace(a, expires_at=NOW + timedelta(days=30))),
    ("approval_hash", lambda a: replace(a, approval_hash=digest("another-approval"))),
    ("idempotency_key", lambda a: replace(a, idempotency_key="approve-something-else")),
    ("approved_at", lambda a: replace(a, approved_at=a.approved_at - timedelta(hours=1))),
    ("approval_id", lambda a: replace(a, approval_id=ApprovalId(PRIMARY.uuid("approval:other")))),
    ("case_id", lambda a: replace(a, case_id=OTHER_CASE.case_id)),
    ("created_at", lambda a: replace(a, created_at=a.created_at - timedelta(seconds=1))),
    ("schema_version", lambda a: replace(a, schema_version="approval/v2")),
)


@pytest.mark.parametrize(
    ("field_name", "mutate"),
    IMMUTABLE_APPROVAL_MUTATIONS,
    ids=[name for name, _ in IMMUTABLE_APPROVAL_MUTATIONS],
)
async def test_consuming_an_approval_cannot_rewrite_the_decision(
    storage: StorageDriver, field_name: str, mutate: Callable[[Approval], Approval]
) -> None:
    """An approval is one immutable human decision; consumption records a timestamp only.

    A whole-item put could otherwise carry a different proposal, view, approver, or expiry
    alongside ``consumed_at``, and the version condition alone would happily accept it.
    """

    repositories = build_repositories(storage)
    scope = PRIMARY.action_scope
    approved = PRIMARY.approval(version=1)
    await storage.write_item(repositories.shareable.stage_append_approval(scope, approved))
    tampered = mutate(PRIMARY.approval(version=2, consumed=True))

    with pytest.raises(ValueError, match="rewrite the decision"):
        repositories.shareable.stage_consume_approval(scope, tampered, expected=approved)

    stored = await repositories.shareable.load_approval(scope, approved.approval_id)
    assert stored == approved


async def test_consuming_an_approval_twice_is_refused_before_persistence(
    storage: StorageDriver,
) -> None:
    """One-time consumption is a local invariant, not only a race the condition loses."""

    repositories = build_repositories(storage)
    consumed = PRIMARY.approval(version=2, consumed=True)

    with pytest.raises(ValueError, match="consumed exactly once"):
        repositories.shareable.stage_consume_approval(
            PRIMARY.action_scope, PRIMARY.approval(version=3, consumed=True), expected=consumed
        )


async def test_consuming_an_approval_must_increment_the_version_by_one(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)

    with pytest.raises(ValueError, match="increment the version"):
        repositories.shareable.stage_consume_approval(
            PRIMARY.action_scope,
            PRIMARY.approval(version=5, consumed=True),
            expected=PRIMARY.approval(version=1),
        )


async def test_consumption_is_bound_to_the_stored_approval_hash(
    storage: StorageDriver,
) -> None:
    """The condition names the hash as well as the version, so a swapped row cannot be consumed."""

    repositories = build_repositories(storage)
    scope = PRIMARY.action_scope
    approved = PRIMARY.approval(version=1)
    await storage.write_item(
        repositories.shareable.stage_append_approval(
            scope, replace(approved, approval_hash=digest("a-different-decision"))
        )
    )

    with pytest.raises(PersistenceConflictError):
        await storage.write_item(
            repositories.shareable.stage_consume_approval(
                scope, PRIMARY.approval(version=2, consumed=True), expected=approved
            )
        )
