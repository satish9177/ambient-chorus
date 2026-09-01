"""Category E: explicit transaction composition is validated before any storage call."""

from __future__ import annotations

import pytest
from tests.fixtures.persistence import PRIMARY, digest

from chorus.domain.ids import Sha256Digest
from chorus.ports.errors import (
    TransactionLimitExceededError,
    UnauditedMutationError,
)
from chorus.ports.idempotency import REQUEST_HASH_ATTRIBUTE
from chorus.ports.limits import TRANSACTION_MAX_OPERATIONS
from chorus.ports.storage import (
    AttributeEqualsNumber,
    CheckItem,
    DeleteItem,
    ItemKey,
    KeyAbsent,
    KeyPresent,
    PutItem,
    TableName,
)
from chorus.ports.unit_of_work import CommitProof, TransactionPlan


def key(table: TableName, index: int) -> ItemKey:
    return ItemKey(table=table, partition_key=f"NS#TEST_PERSISTENCE#CASE#{index:04d}", sort_key="X")


def put(table: TableName, index: int, *, create_only: bool = True) -> PutItem:
    return PutItem(
        key=key(table, index),
        item={"PK": key(table, index).partition_key, "SK": "X"},
        condition=KeyAbsent() if create_only else AttributeEqualsNumber(name="version", value=1),
    )


def audit_put(index: int = 0) -> PutItem:
    return put(TableName.AUDIT, index)


def proof_put(index: int, request_hash: Sha256Digest) -> PutItem:
    """A create-only write shaped like the idempotency record a real proof names."""

    item_key = key(TableName.CORE, index)
    return PutItem(
        key=item_key,
        item={
            "PK": item_key.partition_key,
            "SK": item_key.sort_key,
            REQUEST_HASH_ATTRIBUTE: request_hash.value,
        },
        condition=KeyAbsent(),
    )


def test_a_plan_requires_at_least_one_operation() -> None:
    with pytest.raises(ValueError, match="at least one operation"):
        TransactionPlan(name="empty", operations=(), audit_required=False)


def test_a_plan_is_bounded_at_the_frozen_operation_limit() -> None:
    operations = tuple(put(TableName.CORE, index) for index in range(TRANSACTION_MAX_OPERATIONS))
    plan = TransactionPlan(name="at-limit", operations=operations, audit_required=False)
    assert len(plan.operations) == TRANSACTION_MAX_OPERATIONS

    too_many = (*operations, put(TableName.CORE, TRANSACTION_MAX_OPERATIONS))
    with pytest.raises(TransactionLimitExceededError):
        TransactionPlan(name="over-limit", operations=too_many, audit_required=False)


def test_two_operations_cannot_address_the_same_item() -> None:
    duplicate = (put(TableName.CORE, 1), put(TableName.CORE, 1))

    with pytest.raises(ValueError, match="same item twice"):
        TransactionPlan(name="duplicate", operations=duplicate, audit_required=False)


def test_the_same_key_in_two_tables_is_not_a_duplicate() -> None:
    plan = TransactionPlan(
        name="cross-table",
        operations=(put(TableName.CORE, 1), put(TableName.SHAREABLE, 1), audit_put(1)),
        audit_required=True,
    )

    assert len(plan.operations) == 3


def test_an_audited_mutation_must_carry_its_audit_write() -> None:
    with pytest.raises(UnauditedMutationError):
        TransactionPlan(
            name="unaudited",
            operations=(put(TableName.CORE, 1),),
            audit_required=True,
        )


def test_an_audit_write_must_be_append_only() -> None:
    replaced = put(TableName.AUDIT, 1, create_only=False)

    with pytest.raises(UnauditedMutationError):
        TransactionPlan(
            name="mutable-audit",
            operations=(put(TableName.CORE, 1), replaced),
            audit_required=True,
        )


def test_an_audit_delete_is_never_accepted() -> None:
    deletion = DeleteItem(key=key(TableName.AUDIT, 1), condition=KeyPresent())

    with pytest.raises(UnauditedMutationError):
        TransactionPlan(
            name="audit-delete",
            operations=(put(TableName.CORE, 1), deletion),
            audit_required=True,
        )


def test_an_unaudited_plan_cannot_smuggle_an_audit_write() -> None:
    with pytest.raises(UnauditedMutationError):
        TransactionPlan(
            name="smuggled-audit",
            operations=(put(TableName.CORE, 1), audit_put(1)),
            audit_required=False,
        )


def test_an_audit_condition_check_is_not_a_mutation() -> None:
    check = CheckItem(key=key(TableName.AUDIT, 5), condition=KeyPresent())

    plan = TransactionPlan(
        name="audit-check",
        operations=(put(TableName.CORE, 1), check),
        audit_required=False,
    )

    assert plan.operations[1] is check


def test_a_commit_proof_must_be_created_by_its_own_plan() -> None:
    proof = CommitProof(key=key(TableName.CORE, 42), request_hash=digest("request"))

    with pytest.raises(ValueError, match="commit proof"):
        TransactionPlan(
            name="unbacked-proof",
            operations=(put(TableName.CORE, 1),),
            audit_required=False,
            commit_proof=proof,
        )


def test_a_commit_proof_must_name_a_create_only_write() -> None:
    proof = CommitProof(key=key(TableName.CORE, 1), request_hash=digest("request"))

    with pytest.raises(ValueError, match="commit proof"):
        TransactionPlan(
            name="replaced-proof",
            operations=(put(TableName.CORE, 1, create_only=False),),
            audit_required=False,
            commit_proof=proof,
        )


def test_a_backed_commit_proof_is_accepted() -> None:
    request_hash = digest("request")
    proof = CommitProof(key=key(TableName.CORE, 1), request_hash=request_hash)

    plan = TransactionPlan(
        name="proved",
        operations=(proof_put(1, request_hash), audit_put(1)),
        audit_required=True,
        commit_proof=proof,
    )

    assert plan.commit_proof is proof


def test_a_commit_proof_must_bind_the_request_hash_its_plan_persists() -> None:
    """A proof naming a different request would resolve against someone else's record."""

    with pytest.raises(ValueError, match="request hash"):
        TransactionPlan(
            name="proved",
            operations=(proof_put(1, digest("the-request-actually-written")), audit_put(1)),
            audit_required=True,
            commit_proof=CommitProof(
                key=key(TableName.CORE, 1), request_hash=digest("a-different-request")
            ),
        )


def test_a_commit_proof_must_bind_a_persisted_request_hash_at_all() -> None:
    """A create-only write that records no request hash is not evidence of anything."""

    with pytest.raises(ValueError, match="request hash"):
        TransactionPlan(
            name="proved",
            operations=(put(TableName.CORE, 1), audit_put(1)),
            audit_required=True,
            commit_proof=CommitProof(key=key(TableName.CORE, 1), request_hash=digest("request")),
        )


def test_the_client_request_token_is_deterministic_and_content_bound() -> None:
    operations = (put(TableName.CORE, 1), audit_put(1))
    first = TransactionPlan(name="token", operations=operations, audit_required=True)
    same = TransactionPlan(name="token", operations=operations, audit_required=True)
    renamed = TransactionPlan(name="token-2", operations=operations, audit_required=True)
    reordered = TransactionPlan(
        name="token", operations=(operations[1], operations[0]), audit_required=True
    )

    assert first.client_request_token == same.client_request_token
    assert len(first.client_request_token) == 36
    assert first.client_request_token != renamed.client_request_token
    assert first.client_request_token != reordered.client_request_token


def test_a_plan_name_is_bounded() -> None:
    with pytest.raises(ValueError, match="transaction name"):
        TransactionPlan(name="", operations=(put(TableName.CORE, 1),), audit_required=False)
    with pytest.raises(ValueError, match="transaction name"):
        TransactionPlan(name="x" * 65, operations=(put(TableName.CORE, 1),), audit_required=False)


def test_a_realistic_case_mutation_composes_within_the_limit() -> None:
    """The V1 per-case bounds keep a whole-case mutation inside one transaction."""

    world = PRIMARY
    operations = tuple(
        put(TableName.CORE, index) for index in range(TRANSACTION_MAX_OPERATIONS - 1)
    )
    plan = TransactionPlan(
        name=f"case-mutation-{world.seed}",
        operations=(*operations, audit_put(999)),
        audit_required=True,
    )

    assert len(plan.operations) == TRANSACTION_MAX_OPERATIONS


def test_the_token_changes_when_item_content_changes() -> None:
    """A second, different mutation of the same items must not look like a replay."""

    first = PutItem(
        key=key(TableName.CORE, 1),
        item={"PK": key(TableName.CORE, 1).partition_key, "SK": "X", "state": "OPEN"},
        condition=KeyAbsent(),
    )
    second = PutItem(key=first.key, item={**first.item, "state": "CLOSED"}, condition=KeyAbsent())

    plan = TransactionPlan(name="state", operations=(first,), audit_required=False)
    changed = TransactionPlan(name="state", operations=(second,), audit_required=False)

    assert plan.client_request_token != changed.client_request_token


def test_the_token_changes_when_a_condition_changes() -> None:
    guarded = PutItem(
        key=key(TableName.CORE, 1),
        item={"PK": key(TableName.CORE, 1).partition_key, "SK": "X"},
        condition=AttributeEqualsNumber(name="version", value=1),
    )
    later = PutItem(
        key=guarded.key,
        item=guarded.item,
        condition=AttributeEqualsNumber(name="version", value=2),
    )

    assert (
        TransactionPlan(
            name="guarded", operations=(guarded,), audit_required=False
        ).client_request_token
        != TransactionPlan(
            name="guarded", operations=(later,), audit_required=False
        ).client_request_token
    )


def test_the_token_is_stable_across_equal_item_orderings() -> None:
    """Attribute insertion order is not part of a request's identity."""

    forward = PutItem(
        key=key(TableName.CORE, 1),
        item={"PK": key(TableName.CORE, 1).partition_key, "SK": "X", "a": 1, "b": 2},
        condition=KeyAbsent(),
    )
    reordered = PutItem(
        key=forward.key,
        item={"b": 2, "a": 1, "SK": "X", "PK": key(TableName.CORE, 1).partition_key},
        condition=KeyAbsent(),
    )

    assert (
        TransactionPlan(
            name="order", operations=(forward,), audit_required=False
        ).client_request_token
        == TransactionPlan(
            name="order", operations=(reordered,), audit_required=False
        ).client_request_token
    )


def test_nested_values_cannot_be_confused_for_one_another() -> None:
    """Length-prefixed tagging stops two different shapes hashing to one token."""

    ambiguous = PutItem(
        key=key(TableName.CORE, 1),
        item={"PK": key(TableName.CORE, 1).partition_key, "SK": "X", "v": ("ab", "c")},
        condition=KeyAbsent(),
    )
    other = PutItem(
        key=ambiguous.key,
        item={"PK": key(TableName.CORE, 1).partition_key, "SK": "X", "v": ("a", "bc")},
        condition=KeyAbsent(),
    )

    assert (
        TransactionPlan(
            name="nested", operations=(ambiguous,), audit_required=False
        ).client_request_token
        != TransactionPlan(
            name="nested", operations=(other,), audit_required=False
        ).client_request_token
    )


CLIENT_REQUEST_TOKEN_MAX_LENGTH = 36
"""DynamoDB accepts a ``ClientRequestToken`` of 1 to 36 characters."""

TOKEN_CHARACTERS = frozenset("0123456789abcdef-")


def test_the_token_fits_the_service_length_and_character_bounds() -> None:
    """A token longer than 36 characters is rejected by DynamoDB before it is evaluated."""

    tokens = {
        TransactionPlan(
            name="x" * 64,
            operations=tuple(put(TableName.CORE, index) for index in range(20)),
            audit_required=False,
        ).client_request_token,
        TransactionPlan(
            name="t", operations=(put(TableName.CORE, 1),), audit_required=False
        ).client_request_token,
    }

    for token in tokens:
        assert 1 <= len(token) <= CLIENT_REQUEST_TOKEN_MAX_LENGTH
        assert set(token) <= TOKEN_CHARACTERS


def test_the_token_discloses_no_content_of_the_request() -> None:
    """The token travels to AWS in the clear, so it must be a digest and nothing else."""

    private = "PRIVATE_SENTINEL_VALUE"
    operation = PutItem(
        key=key(TableName.CORE, 1),
        item={"PK": key(TableName.CORE, 1).partition_key, "SK": "X", "text": private},
        condition=KeyAbsent(),
    )
    plan = TransactionPlan(name=private, operations=(operation,), audit_required=False)

    assert private not in plan.client_request_token
    assert private.lower() not in plan.client_request_token
    assert key(TableName.CORE, 1).partition_key not in plan.client_request_token


def test_two_distinct_mutations_of_one_item_never_share_a_token() -> None:
    """Reusing a token across different requests would let DynamoDB drop the second one."""

    target = key(TableName.CORE, 1)

    def plan_writing(value: int) -> TransactionPlan:
        return TransactionPlan(
            name="same-name",
            operations=(
                PutItem(
                    key=target,
                    item={"PK": target.partition_key, "SK": target.sort_key, "version": value},
                    condition=AttributeEqualsNumber(name="version", value=value - 1),
                ),
            ),
            audit_required=False,
        )

    assert plan_writing(2).client_request_token != plan_writing(3).client_request_token


def test_a_delete_and_a_check_of_one_item_never_share_a_token() -> None:
    """The operation family is part of the request and so must be part of the token."""

    target = key(TableName.CORE, 1)
    deleting = TransactionPlan(
        name="op",
        operations=(DeleteItem(key=target, condition=KeyPresent()),),
        audit_required=False,
    )
    checking = TransactionPlan(
        name="op",
        operations=(CheckItem(key=target, condition=KeyPresent()),),
        audit_required=False,
    )

    assert deleting.client_request_token != checking.client_request_token
