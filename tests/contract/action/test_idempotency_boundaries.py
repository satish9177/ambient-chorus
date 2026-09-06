"""Two idempotency records under one command family, and what each of them settles.

ADR-022 § 9 keeps them separate on purpose, and the separation is only meaningful if both
records actually exist in different places:

* the **route/start reservation** lives in the ``NAMESPACE`` partition and binds the caller's
  ``Idempotency-Key`` to one durable ``PROPOSE_ACTION`` operation and one
  ``agent_invocation_id``;
* the **action-apply commit proof** lives in the ``ACTION`` partition under a domain-separated
  key hash, and proves the ten-participant transaction committed.

They are commit proofs for two different transactions. Conflating them would make a lost apply
indistinguishable from a lost dispatch -- and the honest answer to each is different: a lost
dispatch is re-dispatched, while a lost apply is *read*, never re-run.
"""

from __future__ import annotations

import pytest
from tests.fixtures.action import ACTOR_HASH, ActionHarness
from tests.fixtures.elevator import NAMESPACE

from chorus.application.commands.propose_action import PROPOSAL_APPLY_TRANSACTION
from chorus.application.services.mandate_terms import key_hash
from chorus.domain.ids import ActionId
from chorus.ports.idempotency import (
    IdempotencyKey,
    IdempotencyPartition,
    IdempotencyPartitionKind,
    IdempotencyStatus,
    IdempotentCommand,
)

pytestmark = pytest.mark.anyio

KEY = "propose-key-0001"


def _apply_key(action_id: ActionId) -> IdempotencyKey:
    """The apply proof's own key, derived exactly as the use case derives it.

    The action identity is passed in rather than recomputed. Both action and execution
    identities are UUIDv4, so the only way to name this key is to have minted -- or read back
    -- the identity the apply actually wrote.
    """

    return IdempotencyKey(
        partition=IdempotencyPartition(
            kind=IdempotencyPartitionKind.ACTION,
            namespace=NAMESPACE,
            action_id=action_id,
        ),
        command=IdempotentCommand.PROPOSE_ACTION,
        actor_id_hash=ACTOR_HASH,
        key_hash=key_hash(f"propose-action\x1f{KEY}"),
    )


def _start_key() -> IdempotencyKey:
    """The route reservation's key, in the namespace partition."""

    return IdempotencyKey(
        partition=IdempotencyPartition(
            kind=IdempotencyPartitionKind.NAMESPACE, namespace=NAMESPACE
        ),
        command=IdempotentCommand.PROPOSE_ACTION,
        actor_id_hash=ACTOR_HASH,
        key_hash=key_hash(f"propose-action-start\x1f{KEY}"),
    )


async def test_the_apply_writes_a_completed_commit_proof_in_the_action_partition(
    harness: ActionHarness,
) -> None:
    await harness.prepare()
    result = await harness.propose_action().execute(await harness.command(idempotency_key=KEY))

    record = await harness.compile.idempotency.load(_apply_key(result.action_id))

    assert record is not None
    assert record.status is IdempotencyStatus.COMPLETED
    refs = {ref.entity_type: ref.entity_id for ref in record.result_entity_refs}
    assert refs["ACTION_PROPOSAL"] == result.action_id.value
    assert refs["ACTION_EXECUTION"] == result.execution_id.value


async def test_the_apply_proof_is_the_transaction_plans_own_commit_proof(
    harness: ActionHarness,
) -> None:
    """An unknown transaction outcome is resolved by *reading* this, never by re-invoking.

    The plan carries the proof, so the unit of work can classify an ambiguous write before any
    retry -- which is what stops a lost acknowledgement from becoming a second model pass and a
    second candidate message.
    """

    await harness.prepare()
    await harness.propose_action().execute(await harness.command(idempotency_key=KEY))

    plan = harness.unit_of_work.plan("apply-action-proposal")

    assert plan.commit_proof is not None
    assert plan.audit_required is True


async def test_the_two_records_live_in_different_partitions(
    harness: ActionHarness,
) -> None:
    """One command family, two records, distinguished by partition and by key hash."""

    await harness.prepare()
    await harness.start_operation(idempotency_key=KEY)
    result = await harness.propose_action().execute(await harness.command(idempotency_key=KEY))

    start = await harness.compile.idempotency.load(_start_key())
    apply_key = _apply_key(result.action_id)
    apply = await harness.compile.idempotency.load(apply_key)

    assert start is not None
    assert apply is not None
    assert _start_key().partition.kind is not apply_key.partition.kind
    assert _start_key().key_hash != apply_key.key_hash


async def test_the_key_hash_is_domain_separated_so_one_key_cannot_settle_both(
    harness: ActionHarness,
) -> None:
    """The same caller key produces two different digests, by prefix rather than by partition.

    Partition alone would be enough for storage; the domain separation is what makes the two
    *hashes* differ as well, so a reader that got the partition wrong still cannot mistake one
    proof for the other.
    """

    assert key_hash(f"propose-action\x1f{KEY}") != key_hash(f"propose-action-start\x1f{KEY}")
    assert key_hash(f"propose-action\x1f{KEY}") != key_hash(KEY)


async def test_a_replay_writes_no_second_proof_and_no_second_proposal(
    harness: ActionHarness,
) -> None:
    await harness.prepare()
    first = await harness.propose_action().execute(await harness.command(idempotency_key=KEY))
    applies = [plan for plan in harness.unit_of_work.plans if plan.name == "apply-action-proposal"]

    replayed = await harness.propose_action().execute(await harness.command(idempotency_key=KEY))

    assert replayed.action_id == first.action_id
    assert replayed.replayed is True
    still = [plan for plan in harness.unit_of_work.plans if plan.name == "apply-action-proposal"]
    assert len(still) == len(applies) == 1


async def test_the_apply_proof_is_addressable_by_the_attempt_that_staged_it(
    harness: ActionHarness,
) -> None:
    """The commit proof lives in the ``ACTION`` partition, keyed by the minted action.

    The action identity used to be a UUIDv5 of the invocation identity so any attempt could
    predict it. That derivation is not authorized by ADR-020/021/022, and it is not needed:
    the only caller that resolves an ambiguous outcome *in process* is the attempt that staged
    the plan, and it holds the identity it just minted. A later delivery recovers from the
    durable ``ACTION`` invocation record keyed by the invocation identity instead.
    """

    await harness.prepare()
    result = await harness.propose_action().execute(await harness.command(idempotency_key=KEY))

    plan = harness.unit_of_work.plan(PROPOSAL_APPLY_TRANSACTION)
    assert plan.commit_proof is not None
    assert str(result.action_id) in plan.commit_proof.key.partition_key


async def test_the_send_fence_record_carries_no_core_occ_version_to_compare(
    harness: ActionHarness,
) -> None:
    """The Phase-7-visible half of ADR-020 § 6, which Phase 8's fence will rely on.

    The fence must check the case's **state** and its ``authorization_version``, and must not
    require the current ``CommunityCase.version`` to equal the proposal's recorded
    ``case_version`` -- lifecycle progression moved that number on purpose, and requiring it
    would fail every first send in the system.

    Phase 7 does not implement fence acquisition, so what is asserted here is the fact Phase 7
    *does* own: the ``SendFence`` record has no field for a Core OCC version at all, so a fence
    cannot compare one even if a later implementer wanted to. The comparison it can make is the
    ``authorization_snapshot_hash``, whose coarse case term is now the epoch.
    """

    from dataclasses import fields

    from chorus.ports.records import SendFence

    names = {field.name for field in fields(SendFence)}

    assert "case_version" not in names
    assert "authorization_snapshot_hash" in names
    assert harness is not None
