"""Category L: properties of the key grammar, the codec, cursors, and transaction tokens."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fixtures.persistence import CURSOR_SECRET

from chorus.domain.ids import CaseId, CommunityId, MandateId, MessageId, Namespace, ViewId
from chorus.domain.time import format_utc, parse_utc
from chorus.infrastructure.dynamodb import keys
from chorus.infrastructure.dynamodb.attributes import decode_item, encode_item
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.ports.errors import InvalidCursorError
from chorus.ports.pagination import PageCursor, QueryBinding
from chorus.ports.storage import (
    ItemKey,
    KeyAbsent,
    PutItem,
    StoredValue,
    TableName,
)
from chorus.ports.unit_of_work import TransactionPlan

NAMESPACES = st.sampled_from(
    [Namespace("TEST_ALPHA"), Namespace("TEST_BETA"), Namespace("DEMO"), Namespace("LOCAL_DEV")]
)
UUIDS = st.uuids(version=4)
INSTANTS = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2099, 12, 31),
).map(lambda value: value.replace(tzinfo=UTC))
SORT_KEYS = st.text(min_size=1, max_size=120).filter(lambda value: value.strip() != "")


def stored_values(depth: int = 2) -> st.SearchStrategy[StoredValue]:
    leaves: st.SearchStrategy[StoredValue] = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(10**12), max_value=10**12),
        st.text(max_size=40),
    )
    if depth == 0:
        return leaves
    return st.one_of(
        leaves,
        st.lists(stored_values(depth - 1), max_size=4).map(tuple),
        st.dictionaries(st.text(min_size=1, max_size=12), stored_values(depth - 1), max_size=4),
    )


@given(namespace=NAMESPACES, case=UUIDS, other=UUIDS)
def test_two_cases_never_share_a_partition(namespace: Namespace, case: UUID, other: UUID) -> None:
    first = keys.case_partition(namespace, CaseId(case))
    second = keys.case_partition(namespace, CaseId(other))

    assert (first == second) == (case == other)


@given(first=NAMESPACES, second=NAMESPACES, case=UUIDS)
def test_two_namespaces_never_share_a_partition(
    first: Namespace, second: Namespace, case: UUID
) -> None:
    left = keys.case_partition(first, CaseId(case))
    right = keys.case_partition(second, CaseId(case))

    assert (left == right) == (first == second)


@given(namespace=NAMESPACES, community=UUIDS, case=UUIDS, view=UUIDS)
def test_partition_kinds_never_alias_one_another(
    namespace: Namespace, community: UUID, case: UUID, view: UUID
) -> None:
    partitions = {
        keys.namespace_partition(namespace),
        keys.community_partition(namespace, CommunityId(community)),
        keys.case_partition(namespace, CaseId(case)),
        keys.view_partition(namespace, ViewId(view)),
        keys.view_current_partition(namespace, CaseId(case)),
        keys.action_current_partition(namespace, CaseId(case)),
    }

    assert len(partitions) == 6


@given(instant=INSTANTS, message=UUIDS)
def test_a_message_sort_key_is_inside_its_own_instant_window(
    instant: datetime, message: UUID
) -> None:
    sort_key = keys.message_sort_key(instant, MessageId(message))

    assert keys.message_sort_key_lower_bound(instant) <= sort_key
    assert sort_key <= keys.message_sort_key_upper_bound(instant)


@given(earlier=INSTANTS, later=INSTANTS, message=UUIDS)
def test_message_sort_keys_order_as_their_instants_do(
    earlier: datetime, later: datetime, message: UUID
) -> None:
    left = keys.message_sort_key(earlier, MessageId(message))
    right = keys.message_sort_key(later, MessageId(message))

    assert (left < right) == (earlier < later)


@given(mandate=UUIDS, first=st.integers(1, 10**9), second=st.integers(1, 10**9))
def test_mandate_version_keys_sort_numerically(mandate: UUID, first: int, second: int) -> None:
    left = keys.mandate_version_sort_key(MandateId(mandate), first)
    right = keys.mandate_version_sort_key(MandateId(mandate), second)

    assert (left < right) == (first < second)


@given(instant=INSTANTS)
def test_the_canonical_instant_format_round_trips(instant: datetime) -> None:
    rendered = format_utc(instant)

    assert parse_utc(rendered) == instant
    assert rendered.endswith("Z")
    assert len(rendered.split(".")[1]) == 7


@given(item=st.dictionaries(st.text(min_size=1, max_size=12), stored_values(), max_size=6))
def test_the_attribute_codec_round_trips_every_stored_value(
    item: dict[str, StoredValue],
) -> None:
    assert decode_item(encode_item(item)) == item


@given(
    namespace=NAMESPACES,
    binding=st.sampled_from(list(QueryBinding)),
    partition=st.text(min_size=1, max_size=80),
    sort_key=SORT_KEYS,
)
@settings(max_examples=50)
def test_a_cursor_round_trips_only_under_its_own_binding(
    namespace: Namespace, binding: QueryBinding, partition: str, sort_key: str
) -> None:
    codec = SignedCursorCodec(CURSOR_SECRET)
    cursor = codec.issue(
        namespace=namespace, binding=binding, partition_key=partition, sort_key=sort_key
    )

    resumed = codec.verify(cursor, namespace=namespace, binding=binding, partition_key=partition)

    assert resumed == sort_key
    assert len(cursor.value) <= 2048


@given(
    namespace=NAMESPACES,
    partition=st.text(min_size=1, max_size=40),
    sort_key=SORT_KEYS,
    mutation=st.integers(min_value=0, max_value=255),
    position=st.integers(min_value=0, max_value=200),
)
@settings(max_examples=50)
def test_a_mutated_cursor_never_verifies(
    namespace: Namespace, partition: str, sort_key: str, mutation: int, position: int
) -> None:
    codec = SignedCursorCodec(CURSOR_SECRET)
    cursor = codec.issue(
        namespace=namespace,
        binding=QueryBinding.CORE_CASE_FACTS,
        partition_key=partition,
        sort_key=sort_key,
    )
    index = position % len(cursor.value)
    replacement = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"[mutation % 64]
    if cursor.value[index] == replacement:
        return
    mutated = PageCursor(cursor.value[:index] + replacement + cursor.value[index + 1 :])

    try:
        resumed = codec.verify(
            mutated,
            namespace=namespace,
            binding=QueryBinding.CORE_CASE_FACTS,
            partition_key=partition,
        )
    except InvalidCursorError:
        return
    # Rejection is the normal outcome; the only accepted alternative is a padding-equivalent
    # re-encoding that decodes to the very same signed bytes.
    assert resumed == sort_key


@given(
    left=st.dictionaries(st.text(min_size=1, max_size=8), stored_values(1), max_size=4),
    right=st.dictionaries(st.text(min_size=1, max_size=8), stored_values(1), max_size=4),
)
@settings(max_examples=50)
def test_a_transaction_token_distinguishes_different_requests(
    left: dict[str, StoredValue], right: dict[str, StoredValue]
) -> None:
    key = ItemKey(table=TableName.CORE, partition_key="NS#TEST_ALPHA", sort_key="ITEM#1")
    base = {"PK": key.partition_key, "SK": key.sort_key}

    def token(payload: dict[str, StoredValue]) -> str:
        operation = PutItem(key=key, item={**base, **payload}, condition=KeyAbsent())
        return TransactionPlan(
            name="property", operations=(operation,), audit_required=False
        ).client_request_token

    same_content = _identical_item({**base, **left}, {**base, **right})
    assert (token(left) == token(right)) == same_content


def _identical_item(left: StoredValue, right: StoredValue) -> bool:
    """Structural equality that distinguishes stored *types*, which ``==`` does not.

    Python's ``bool`` subclasses ``int``, so ``False == 0`` and ``True == 1``. DynamoDB draws
    no such equivalence: one is a ``BOOL`` attribute and the other an ``N``, and two requests
    differing only in that are genuinely different requests that must not share a token --
    ten minutes of token idempotency would otherwise discard the second as a replay of the
    first.

    So the oracle has to be at least as strict as the thing it is checking. Comparing with
    ``==`` made this property assert the opposite of the invariant for exactly those pairs.
    """

    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _identical_item(left[name], right[name]) for name in left
        )
    if isinstance(left, tuple) and isinstance(right, tuple):
        return len(left) == len(right) and all(
            _identical_item(one, other) for one, other in zip(left, right, strict=True)
        )
    return bool(left == right)
