"""F11 -- ``action_id`` and ``execution_id`` are UUIDv4, as the frozen model says.

The implementation had introduced ``action_id_for(invocation_id)`` and
``execution_id_for(action_id)``, both UUIDv5 derivations. No accepted ADR authorizes them:
ADR-020, ADR-021, and ADR-022 leave ``ActionProposal.action_id`` and
``ActionExecution.execution_id`` exactly as the domain declares them, minted through the
injected :class:`~chorus.domain.ids.IdGenerator`.

The derivations existed to make ambiguous recovery easier -- an attempt could name the apply's
commit-proof partition without reading anything first. That is not a reason to mint an identity
the contract does not authorize. ADR-022 § 10 already says what recovery reads: the durable
``ACTION`` invocation record and the apply commit proof, neither of which needs a predictable
artifact identity.

The one thing the derivation genuinely provided -- "which execution belongs to this action" --
is now *recorded* on ``CurrentActionPointer`` rather than recomputed.
"""

from __future__ import annotations

import pytest

from chorus.application.commands import propose_action
from chorus.domain.ids import Uuid4Generator
from chorus.ports.records import AgentInvocationOutcome
from chorus.ports.scopes import ActionScope, CaseScope
from tests.fixtures.action import ActionHarness

pytestmark = pytest.mark.anyio


def _action_scope(harness: ActionHarness, action_id: object) -> ActionScope:
    return ActionScope(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
        action_id=action_id,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------------------
# The identity contract
# ---------------------------------------------------------------------------------------


async def test_both_identities_are_uuid_version_four(harness: ActionHarness) -> None:
    """Minted through the production generator, which is the only authorized source."""

    await harness.prepare()
    result = await harness.propose_action(ids=Uuid4Generator()).execute(await harness.command())

    assert result.action_id.value.version == 4
    assert result.execution_id.value.version == 4
    assert result.action_id.value != result.execution_id.value


async def test_the_persisted_artifacts_carry_the_version_four_identities(
    harness: ActionHarness,
) -> None:
    await harness.prepare()
    result = await harness.propose_action(ids=Uuid4Generator()).execute(await harness.command())

    scope = _action_scope(harness, result.action_id)
    proposal = await harness.compile.shareable.load_proposal(scope)
    execution = await harness.compile.shareable.load_execution(scope, result.execution_id)

    assert proposal.action_id.value.version == 4
    assert execution.execution_id.value.version == 4
    assert execution.action_id == proposal.action_id


async def test_the_unauthorized_derivation_helpers_are_gone(harness: ActionHarness) -> None:
    """Deleted rather than left unused: an unused derivation is one somebody re-adopts."""

    assert not hasattr(propose_action, "action_id_for")
    assert not hasattr(propose_action, "execution_id_for")
    source = propose_action.__file__
    assert source is not None
    from pathlib import Path

    text = Path(source).read_text(encoding="utf-8")
    assert "uuid5" not in text


# ---------------------------------------------------------------------------------------
# Recovery still works with random identities
# ---------------------------------------------------------------------------------------


async def test_recovery_reads_the_durable_record_rather_than_recomputing_an_identity(
    harness: ActionHarness,
) -> None:
    """A redelivery answers from participant 6, keyed by the *invocation* identity.

    The invocation identity is server-generated and written onto the durable operation before
    dispatch, so it is stable across deliveries without any artifact identity being derivable.
    """

    await harness.prepare()
    first = await harness.propose_action(ids=Uuid4Generator()).execute(await harness.command())

    scope = CaseScope(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
    )
    record = await harness.compile.core.load_agent_invocation(scope, harness.invocation_id)
    assert record is not None
    assert record.outcome is AgentInvocationOutcome.SUCCEEDED
    refs = {ref.entity_type: ref.entity_id for ref in record.result_refs}
    assert refs["ACTION_PROPOSAL"] == first.action_id.value
    assert refs["ACTION_EXECUTION"] == first.execution_id.value

    replayed = await harness.propose_action(ids=Uuid4Generator()).execute(await harness.command())

    assert replayed.replayed is True
    assert replayed.action_id == first.action_id
    assert replayed.execution_id == first.execution_id
    assert len(harness.agent.invocations) == 1


async def test_the_pointer_names_the_execution_instead_of_deriving_it(
    harness: ActionHarness,
) -> None:
    """The one thing the derivation bought, recorded where a strong read can see it.

    That two *different* proposals for one case mint different identities is asserted by
    ``test_a_second_proposal_may_replace_an_invalidated_pointer_whose_execution_failed`` in
    the races suite, which now runs against the production UUIDv4 generator.
    """

    await harness.prepare()
    result = await harness.propose_action(ids=Uuid4Generator()).execute(await harness.command())

    pointer = await harness.compile.shareable.load_current_action_pointer(harness.scope)

    assert pointer is not None
    assert pointer.action_id == result.action_id
    assert pointer.execution_id == result.execution_id


async def test_the_preview_query_finds_the_execution_through_the_pointer(
    harness: ActionHarness,
) -> None:
    await harness.prepare()
    result = await harness.propose_action(ids=Uuid4Generator()).execute(await harness.command())

    projection = await harness.read_current_action().execute(harness.scope)

    assert projection is not None
    assert projection.execution_id == result.execution_id
    assert projection.action_id == result.action_id
