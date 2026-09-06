"""The execution's own partition, and the six idempotency domains that must not collide.

ADR-024 is the ADR-019 defect found a second time. The execution shared ``NS#n#ACTION#a`` with
the immutable proposal and the immutable approval, and ``dynamodb:LeadingKeys`` constrains the
partition key while nothing constrains the sort key -- so the narrowest grant that could write
an execution also authorized overwriting the message a human approved.

These tests assert the key grammar itself, because that is where the guarantee now lives. A
policy assertion proves the grant is narrow; only this proves the narrow grant reaches the row
it has to reach and nothing else.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from tests.fixtures.persistence import PRIMARY, digest

from chorus.application.services.action_authorization import (
    approval_key,
    approval_start_key_hash,
    approval_transaction_key_hash,
    send_claim_key_hash,
    send_key,
    send_projection_key_hash,
    send_result_key_hash,
    send_start_key_hash,
)
from chorus.domain.ids import ActionId, Namespace
from chorus.infrastructure.dynamodb import codec_idempotency, codec_share, keys
from chorus.ports.idempotency import (
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotentCommand,
)
from chorus.ports.storage import TableName

NAMESPACE = Namespace("TEST_PARTITION")
ACTION_ID = ActionId(UUID("55555555-5555-4555-8555-555555555555"))


def test_the_execution_partition_is_keyed_by_action_and_prefixed_by_execution() -> None:
    """``NS#{namespace}#EXECUTION#{action_id}``.

    Keyed by *action* rather than by execution so it stays the per-action collection the access
    patterns describe; V1 puts exactly one item in it, and the current action pointer already
    carries both identifiers, so every read is still a direct get with no query and no GSI.
    """

    partition = keys.execution_partition(NAMESPACE, ACTION_ID)

    assert partition == f"NS#TEST_PARTITION#EXECUTION#{ACTION_ID}"
    assert partition != keys.action_partition(NAMESPACE, ACTION_ID)


def test_the_execution_row_does_not_share_a_partition_with_the_proposal_or_the_approval() -> None:
    """The whole of T33, expressed as three addresses.

    A principal granted ``PutItem`` over the execution's partition cannot reach either
    immutable artifact, because they are not in it. That is what makes "the sender can send the
    message a human approved, or send nothing" true rather than documented.
    """

    scope = PRIMARY.action_scope
    execution = codec_share.execution_key(scope, PRIMARY.execution_id)
    proposal = codec_share.proposal_key(scope)
    approval = codec_share.approval_key(scope, PRIMARY.approval_id)

    assert execution.partition_key.startswith(f"NS#{scope.namespace.value}#EXECUTION#")
    assert proposal.partition_key.startswith(f"NS#{scope.namespace.value}#ACTION#")
    assert approval.partition_key == proposal.partition_key
    assert execution.partition_key != proposal.partition_key
    assert execution.table is proposal.table is TableName.SHAREABLE


def test_the_send_idempotency_records_move_with_the_execution() -> None:
    """A record only one principal must write must not sit where only that one is denied.

    The mirror image of the reason the compile record lives under ``VIEW_CURRENT``. All three
    send domains land in the execution's partition, which is the only Shareable prefix the
    sender's ``LeadingKeys`` grant permits it to write.
    """

    partition = IdempotencyPartition(
        kind=IdempotencyPartitionKind.EXECUTION, namespace=NAMESPACE, action_id=ACTION_ID
    )
    key = send_key(
        namespace=NAMESPACE,
        action_id=ACTION_ID,
        actor_id_hash=digest("actor"),
        key_hash=send_claim_key_hash("execution-send-key"),
    )

    assert key.partition == partition
    item = codec_idempotency.idempotency_item_key(key, table=TableName.SHAREABLE)
    assert item.partition_key == keys.execution_partition(NAMESPACE, ACTION_ID)


def test_the_approval_records_stay_in_the_action_partition() -> None:
    """The two approval domains sit beside the immutable proposal they are about.

    The application writes both, and the application holds both prefixes; there is no principal
    for whom this placement is a denial, which is why it stays a filing choice here and a
    permission fact on the send side.
    """

    key = approval_key(
        namespace=NAMESPACE,
        action_id=ACTION_ID,
        actor_id_hash=digest("actor"),
        key_hash=approval_transaction_key_hash("client-key"),
    )

    assert key.partition.kind is IdempotencyPartitionKind.ACTION
    item = codec_idempotency.idempotency_item_key(key, table=TableName.SHAREABLE)
    assert item.partition_key == keys.action_partition(NAMESPACE, ACTION_ID)


def test_an_execution_partition_requires_an_action_and_refuses_anything_else() -> None:
    """The required-field table gains a row rather than a default."""

    with pytest.raises(ValueError, match="do not match its kind"):
        IdempotencyPartition(kind=IdempotencyPartitionKind.EXECUTION, namespace=NAMESPACE)
    with pytest.raises(ValueError, match="do not match its kind"):
        IdempotencyPartition(
            kind=IdempotencyPartitionKind.EXECUTION,
            namespace=NAMESPACE,
            action_id=ACTION_ID,
            case_id=PRIMARY.case_id,
        )


# ---------------------------------------------------------------------------------------
# The six domains
# ---------------------------------------------------------------------------------------


def test_the_six_idempotency_domains_are_pairwise_distinct() -> None:
    """Separate namespaces, because they are commit proofs for different things.

    A single reused row would make one outcome answer for another: a completed send-claim
    record would look like proof that the *result* had been persisted. Asserted pairwise over
    the whole set rather than spot-checked, so a seventh domain added with a colliding prefix
    fails here.
    """

    client_key = "one-client-key"
    hashes = {
        "approval-start": approval_start_key_hash(client_key),
        "approval-transaction": approval_transaction_key_hash(client_key),
        "send-start": send_start_key_hash(client_key),
        "send-claim": send_claim_key_hash(client_key),
        "send-result": send_result_key_hash(client_key),
        "send-projection": send_projection_key_hash(client_key),
    }

    assert len({value.value for value in hashes.values()}) == len(hashes)


def test_the_domain_separator_cannot_be_forged_from_a_client_key() -> None:
    """``\\x1f`` is the separator, and the transport pattern admits printable ASCII only.

    A colon or a hyphen could be typed into an ``Idempotency-Key``, which would let a caller
    make two domains collide by choosing a key that contained the delimiter. A unit separator
    cannot appear in a key the route will accept.
    """

    assert approval_start_key_hash("approve-action\x1fx") != approval_transaction_key_hash("x")
    assert send_claim_key_hash("send-result\x1fx") != send_result_key_hash("x")


def test_the_send_family_is_the_send_command_and_the_approval_family_is_its_own() -> None:
    """Two command families, so retention and replay semantics cannot be confused."""

    approval = approval_key(
        namespace=NAMESPACE,
        action_id=ACTION_ID,
        actor_id_hash=digest("actor"),
        key_hash=approval_start_key_hash("k"),
    )
    send = send_key(
        namespace=NAMESPACE,
        action_id=ACTION_ID,
        actor_id_hash=digest("actor"),
        key_hash=send_start_key_hash("k"),
    )

    assert approval.command is IdempotentCommand.APPROVE_ACTION
    assert send.command is IdempotentCommand.SEND_ACTION
