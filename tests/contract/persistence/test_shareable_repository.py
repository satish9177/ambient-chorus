"""Shareable-table repository contract: pointers, history, capacity, and scope denial."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from tests.fixtures.persistence import (
    OTHER_CASE,
    OTHER_NAMESPACE,
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
from chorus.ports.storage import CheckItem, PutItem, StorageDriver
from chorus.ports.unit_of_work import TransactionPlan
from chorus.privacy.canonical import (
    APPROVAL_HASH_OMITTED_FIELDS,
    hash_approval,
    verify_hash,
)

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


async def test_an_approval_is_create_only_and_never_written_twice(
    storage: StorageDriver,
) -> None:
    """The approval row is written once. There is no second legal write of any kind.

    ``consumed_at`` is gone (ADR-023 SS 1): the execution reaching ``SENDING`` *is* the
    consumption, it is one-time by compare-and-swap on the execution's row version, and it is
    already durable in the state the sender must write anyway. Recording it here a second time
    put a mutable field inside an authorization digest and required its writer to hold a write
    grant on the immutable proposal's own partition.
    """

    repositories = build_repositories(storage)
    scope = PRIMARY.action_scope
    approval = PRIMARY.approval()

    await storage.write_item(repositories.shareable.stage_append_approval(scope, approval))
    with pytest.raises(PersistenceConflictError):
        await storage.write_item(repositories.shareable.stage_append_approval(scope, approval))

    assert await repositories.shareable.load_approval(scope, approval.approval_id) == approval


async def test_approval_hash_survives_every_legal_later_write(storage: StorageDriver) -> None:
    """There are none, and that is the assertion (T32).

    An integrity check that a legitimate write invalidates cannot prove anything, and it gets
    deleted by the second person who meets it. So the digest is recomputed here from the row as
    stored, through the production authority, and the reason it still matches is that the only
    write this row ever accepts is the one that created it.
    """

    repositories = build_repositories(storage)
    scope = PRIMARY.action_scope
    approval = PRIMARY.approval()
    await storage.write_item(repositories.shareable.stage_append_approval(scope, approval))

    stored = await repositories.shareable.load_approval(scope, approval.approval_id)
    assert hash_approval(stored) == stored.approval_hash
    assert verify_hash(stored, stored.approval_hash, omit_fields=APPROVAL_HASH_OMITTED_FIELDS)
    assert not hasattr(stored, "consumed_at")
    assert not hasattr(repositories.shareable, "stage_consume_approval")


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


APPROVAL_DECISION_FIELDS: tuple[tuple[str, Callable[[Approval], Approval]], ...] = (
    ("proposal_hash", lambda a: replace(a, proposal_hash=digest("another-proposal"))),
    ("view_hash", lambda a: replace(a, view_hash=digest("another-view"))),
    ("decision", lambda a: replace(a, decision=ApprovalDecision.REJECTED)),
    ("approver_id_hash", lambda a: replace(a, approver_id_hash=digest("another-approver"))),
    ("expires_at", lambda a: replace(a, expires_at=a.expires_at + timedelta(minutes=1))),
    ("request_key_hash", lambda a: replace(a, request_key_hash=digest("another-key"))),
    ("approved_at", lambda a: replace(a, approved_at=a.approved_at - timedelta(hours=1))),
    ("approval_id", lambda a: replace(a, approval_id=ApprovalId(PRIMARY.uuid("approval:other")))),
    ("case_id", lambda a: replace(a, case_id=OTHER_CASE.case_id)),
    ("action_id", lambda a: replace(a, action_id=OTHER_CASE.action_id)),
    ("execution_id", lambda a: replace(a, execution_id=OTHER_CASE.execution_id)),
    ("authorization_version", lambda a: replace(a, authorization_version=99)),
    ("namespace", lambda a: replace(a, namespace=OTHER_NAMESPACE)),
    ("schema_version", lambda a: replace(a, schema_version="approval/v3")),
)
"""Every field that is part of the *decision*, and therefore inside the digest."""

APPROVAL_BOOKKEEPING_FIELDS: tuple[tuple[str, Callable[[Approval], Approval]], ...] = (
    ("version", lambda a: replace(a, version=a.version + 1)),
    ("created_at", lambda a: replace(a, created_at=a.created_at - timedelta(seconds=1))),
    ("updated_at", lambda a: replace(a, updated_at=a.updated_at + timedelta(seconds=1))),
)
"""The three fields the omit set covers, beside ``approval_hash`` itself.

They describe the *row* -- which revision it is and when it was touched -- rather than the
decision. Nothing ever moves them, because the row is written once; the omit set states what
the digest is about rather than licensing a write.
"""


@pytest.mark.parametrize(
    ("field_name", "mutate"),
    APPROVAL_DECISION_FIELDS,
    ids=[name for name, _ in APPROVAL_DECISION_FIELDS],
)
def test_every_decision_field_is_inside_the_approval_digest(
    field_name: str, mutate: Callable[[Approval], Approval]
) -> None:
    """Changing any part of the decision changes the digest.

    Parameterized over the whole field list rather than spot-checked, so a field added to the
    entity without being covered fails here rather than silently escaping the binding.
    """

    approval = PRIMARY.approval()
    assert hash_approval(mutate(approval)) != approval.approval_hash


@pytest.mark.parametrize(
    ("field_name", "mutate"),
    APPROVAL_BOOKKEEPING_FIELDS,
    ids=[name for name, _ in APPROVAL_BOOKKEEPING_FIELDS],
)
def test_row_bookkeeping_is_outside_the_approval_digest(
    field_name: str, mutate: Callable[[Approval], Approval]
) -> None:
    """The omit set is exactly ``{approval_hash, version, created_at, updated_at}``.

    This is what makes recomputation meaningful at any later instant: the digest covers only
    fields that never move, so a check performed at send time is checking the decision rather
    than the storage layer's opinion of the row.
    """

    approval = PRIMARY.approval()
    assert hash_approval(mutate(approval)) == approval.approval_hash


def test_the_approval_omit_set_is_exactly_the_four_bookkeeping_fields() -> None:
    """Asserted against the constant the production hasher actually uses."""

    assert (
        frozenset({"approval_hash", "version", "created_at", "updated_at"})
        == APPROVAL_HASH_OMITTED_FIELDS
    )


# ---------------------------------------------------------------------------------------
# The read-only current-view guard (ADR-022 § 7)
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_current_view_guard_is_a_check_and_never_a_write(
    storage: StorageDriver,
) -> None:
    """It must be a ``CheckItem``.

    A ``PutItem`` here would mean the application had been handed a write on a compiler-owned
    prefix in order to perform a read-only guard -- which is the lazy route ADR-022 § 7 names
    so it is refused once rather than proposed repeatedly. DynamoDB authorizes a transaction
    through the permission each participant needs, so the *kind* of this participant is what
    decides which IAM action the application has to hold.
    """

    shareable = build_repositories(storage).shareable
    pointer = PRIMARY.view_pointer()

    item = shareable.stage_require_current_view_pointer(
        PRIMARY.case_scope,
        expected=ViewPointerExpectation(
            row_version=pointer.version, view_hash=pointer.view_hash, view_id=pointer.view_id
        ),
    )

    assert isinstance(item, CheckItem)
    assert not isinstance(item, PutItem)
    assert item.key == codec_share.view_pointer_key(PRIMARY.case_scope)


@pytest.mark.anyio
async def test_the_current_view_guard_names_the_exact_view_it_read(
    storage: StorageDriver,
) -> None:
    """The whole identity of the row, not only its shape.

    The compile's conditional *replace* already knows which row it read from the version it is
    superseding. This guard sits on the far side of a model invocation, so it names ``view_id``
    as well -- otherwise a compile that replaced the pointer with a different view at the same
    row version would satisfy it.
    """

    shareable = build_repositories(storage).shareable
    pointer = PRIMARY.view_pointer()

    item = shareable.stage_require_current_view_pointer(
        PRIMARY.case_scope,
        expected=ViewPointerExpectation(
            row_version=pointer.version, view_hash=pointer.view_hash, view_id=pointer.view_id
        ),
    )

    rendered = str(item.condition)
    assert str(pointer.view_id) in rendered
    assert pointer.view_hash.value in rendered
    assert str(pointer.version) in rendered


@pytest.mark.anyio
async def test_the_current_view_guard_refuses_an_expectation_without_a_view_id(
    storage: StorageDriver,
) -> None:
    """Refused at staging rather than silently degrading to a weaker condition."""

    shareable = build_repositories(storage).shareable
    pointer = PRIMARY.view_pointer()

    with pytest.raises(ValueError):
        shareable.stage_require_current_view_pointer(
            PRIMARY.case_scope,
            expected=ViewPointerExpectation(
                row_version=pointer.version, view_hash=pointer.view_hash
            ),
        )


@pytest.mark.anyio
async def test_the_current_view_guard_passes_on_the_exact_row_and_fails_on_a_moved_one(
    storage: StorageDriver,
) -> None:
    """Committed against real storage, both directions, because a condition nobody evaluated
    is a condition nobody has checked."""

    repositories = build_repositories(storage)
    shareable = repositories.shareable
    unit_of_work = repositories.unit_of_work
    pointer = PRIMARY.view_pointer()
    await unit_of_work.commit(
        TransactionPlan(
            name="seed-view-pointer",
            operations=(
                shareable.stage_replace_current_view_pointer(
                    PRIMARY.case_scope, pointer, expected=None
                ),
            ),
            audit_required=False,
        )
    )

    matching = ViewPointerExpectation(
        row_version=pointer.version, view_hash=pointer.view_hash, view_id=pointer.view_id
    )
    await unit_of_work.commit(
        TransactionPlan(
            name="guard-passes",
            operations=(
                shareable.stage_require_current_view_pointer(PRIMARY.case_scope, expected=matching),
                shareable.stage_append_action_history_locator(
                    PRIMARY.case_scope, PRIMARY.action_history()
                ),
            ),
            audit_required=False,
        )
    )

    moved = ViewPointerExpectation(
        row_version=pointer.version, view_hash=pointer.view_hash, view_id=ViewId(uuid4())
    )
    with pytest.raises(PersistenceConflictError):
        await unit_of_work.commit(
            TransactionPlan(
                name="guard-fails",
                operations=(
                    shareable.stage_require_current_view_pointer(
                        PRIMARY.case_scope, expected=moved
                    ),
                    shareable.stage_append_action_history_locator(
                        PRIMARY.case_scope, PRIMARY.action_history(index=1)
                    ),
                ),
                audit_required=False,
            )
        )
