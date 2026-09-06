"""The happy path, the ten participants, and the two counters that make it possible.

This is the file that proves Phase 7 actually works end to end: a real compiled view, a real
model call through the port, a validated proposal, a rendered preview, and one transaction that
either commits everything or nothing.

The two most load-bearing assertions here are the ones that would have been impossible before
the Phase-7 gate:

* :func:`test_proposal_apply_participant_count_is_exactly_ten` reads the count off the *staged
  plan* rather than off the constant, so a silently added participant fails here rather than at
  storage;
* :func:`test_action_proposal_does_not_stale_its_own_bound_view` is the named regression for the
  defect ADR-020 removed. Under one counter it fails, and the first send of every case would
  have failed with it.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from tests.fixtures.action import ActionHarness, grounded_draft

from chorus.application.commands.propose_action import (
    PROPOSAL_FIXED_TRANSACTION_PARTICIPANTS,
)
from chorus.application.services.action_renderer import TEMPLATE_VERSION
from chorus.contracts.action import ActionCaveatDraft, ActionProposalDraft
from chorus.domain.entities import (
    ActionExecutionState,
    ActionProposalStatus,
    ActionTone,
    CaseState,
)
from chorus.ports.scopes import ActionScope
from chorus.ports.storage import CheckItem, PutItem
from chorus.privacy.canonical import hash_action_proposal, verify_hash

pytestmark = pytest.mark.anyio


async def test_a_valid_proposal_persists_its_artifacts_and_moves_the_case(
    harness: ActionHarness,
) -> None:
    await harness.prepare()
    before = await harness.compile.core.load_case(harness.scope)

    result = await harness.propose_action().execute(await harness.command())

    assert result.replayed is False
    assert len(harness.agent.invocations) == 1

    action_scope = ActionScope(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
        action_id=result.action_id,
    )
    proposal = await harness.compile.shareable.load_proposal(action_scope)
    execution = await harness.compile.shareable.load_execution(action_scope, result.execution_id)
    pointer = await harness.compile.shareable.load_current_action_pointer(harness.scope)
    after = await harness.compile.core.load_case(harness.scope)

    assert proposal.status is ActionProposalStatus.DRAFT
    assert proposal.view_id == harness.view.view_id
    assert proposal.view_hash == harness.view.view_hash
    assert execution.state is ActionExecutionState.DRAFT
    assert pointer is not None and pointer.action_id == result.action_id
    assert after.state is CaseState.ACTION_PROPOSED
    assert after.version == before.version + 1


async def test_the_case_moves_its_occ_version_and_carries_the_epoch_forward(
    harness: ActionHarness,
) -> None:
    """The central ADR-020 invariant, asserted on the row the apply actually wrote.

    Lifecycle progress is not itself disclosure authority. Recording that a proposal exists
    changes no fact, status, mandate, or count, so the epoch must not move.
    """

    await harness.prepare()
    before = await harness.compile.core.load_case(harness.scope)

    await harness.propose_action().execute(await harness.command())
    after = await harness.compile.core.load_case(harness.scope)

    assert after.version == before.version + 1
    assert after.authorization_version == before.authorization_version


async def test_action_proposal_does_not_stale_its_own_bound_view(
    harness: ActionHarness,
) -> None:
    """The named Phase-7 regression (ADR-020, evaluation test 35).

    Under one counter this fails: the proposal's own transition bumped the number the view's
    snapshot was bound to, so the send fence would have found the case at ``N+1`` against a
    proposal recorded at ``N`` and failed every first send in the system.

    What is asserted is the state *after* the apply. The case has advanced its row version, and
    the view, the pointer, the proposal, and the case still agree about the authorization epoch
    -- which is exactly the comparison the Phase-8 fence will perform.
    """

    await harness.prepare()
    result = await harness.propose_action().execute(await harness.command())

    case = await harness.compile.core.load_case(harness.scope)
    view_pointer = await harness.compile.shareable.load_current_view_pointer(harness.scope)
    action_pointer = await harness.compile.shareable.load_current_action_pointer(harness.scope)
    proposal = await harness.compile.shareable.load_proposal(
        ActionScope(
            namespace=harness.scope.namespace,
            community_id=harness.scope.community_id,
            case_id=harness.scope.case_id,
            action_id=result.action_id,
        )
    )
    assert view_pointer is not None
    assert action_pointer is not None

    epoch = case.authorization_version
    assert harness.view.authorization_version == epoch
    assert view_pointer.authorization_version == epoch
    assert action_pointer.authorization_version == epoch
    assert proposal.authorization_version == epoch

    # The OCC version moved, and that is fine: the fence checks state and the epoch, never this.
    assert case.version != proposal.case_version
    assert case.state is CaseState.ACTION_PROPOSED


async def test_proposal_apply_participant_count_is_exactly_ten(
    harness: ActionHarness,
) -> None:
    """Counted from the staged plan, not from the constant it is compared against.

    Independent of how many claims or caveats the proposal contains, because both live inside
    the proposal item rather than as rows of their own.
    """

    await harness.prepare()
    await harness.propose_action().execute(await harness.command())

    plan = harness.unit_of_work.plan("apply-action-proposal")

    assert len(plan.operations) == PROPOSAL_FIXED_TRANSACTION_PARTICIPANTS == 10


async def test_the_transaction_has_exactly_two_condition_only_participants(
    harness: ActionHarness,
) -> None:
    """The current-view check and the no-live-send-fence check, and nothing else read-only.

    The view check is the one ADR-022 § 7 added, and it must be a ``CheckItem``: a ``PutItem``
    there would mean the application had been handed a write on a compiler-owned prefix in
    order to perform a read-only guard.
    """

    await harness.prepare()
    await harness.propose_action().execute(await harness.command())

    operations = harness.unit_of_work.plan("apply-action-proposal").operations
    checks = [item for item in operations if isinstance(item, CheckItem)]
    writes = [item for item in operations if isinstance(item, PutItem)]

    assert len(checks) == 2
    assert len(writes) == 8


async def test_the_draft_execution_carries_none_of_the_approval_dependent_fields(
    harness: ActionHarness,
) -> None:
    """The shape ADR-022 made expressible, read back from storage.

    None of these exists before a human has approved anything, and the send idempotency key is
    defined *over* the approval. A sentinel digest in any of them would be a lie the storage
    layer could not later distinguish from a real binding.
    """

    await harness.prepare()
    result = await harness.propose_action().execute(await harness.command())
    execution = await harness.compile.shareable.load_execution(
        ActionScope(
            namespace=harness.scope.namespace,
            community_id=harness.scope.community_id,
            case_id=harness.scope.case_id,
            action_id=result.action_id,
        ),
        result.execution_id,
    )

    assert execution.state is ActionExecutionState.DRAFT
    assert execution.approval_id is None
    assert execution.idempotency_key is None
    assert execution.rendered_message_hash is None
    assert execution.ses_request_token_hash is None
    assert execution.started_at is None
    assert execution.finished_at is None
    assert execution.attempt_number == 1


async def test_the_execution_identity_is_named_by_the_current_action_pointer(
    harness: ActionHarness,
) -> None:
    """One action, one execution, and the pointer says which -- it is not recomputed.

    Both identities are UUIDv4. The relationship between them is *recorded* on the strongly
    read pointer rather than derived, so no caller has to know a derivation rule to find the
    execution belonging to the current proposal.
    """

    await harness.prepare()
    result = await harness.propose_action().execute(await harness.command())

    pointer = await harness.compile.shareable.load_current_action_pointer(harness.scope)
    assert pointer is not None
    assert pointer.action_id == result.action_id
    assert pointer.execution_id == result.execution_id
    assert result.action_id.value.version == 4
    assert result.execution_id.value.version == 4


async def test_the_proposal_hash_covers_the_preview_hash(harness: ActionHarness) -> None:
    """So an approval binding ``proposal_hash`` transitively binds the preview."""

    await harness.prepare()
    result = await harness.propose_action().execute(await harness.command())
    proposal = await harness.compile.shareable.load_proposal(
        ActionScope(
            namespace=harness.scope.namespace,
            community_id=harness.scope.community_id,
            case_id=harness.scope.case_id,
            action_id=result.action_id,
        )
    )

    assert verify_hash(proposal, proposal.proposal_hash, omit_fields=frozenset({"proposal_hash"}))
    moved = replace(proposal, preview_hash=harness.view.view_hash)
    assert hash_action_proposal(moved) != proposal.proposal_hash


async def test_the_preview_regenerates_to_the_committed_hash(harness: ActionHarness) -> None:
    """The rendered bodies are never persisted; the query regenerates and re-checks them."""

    await harness.prepare()
    await harness.propose_action().execute(await harness.command())

    projection = await harness.read_current_action().execute(harness.scope)

    assert projection is not None
    assert projection.preview_matches_committed_hash
    assert projection.template_version == TEMPLATE_VERSION
    assert projection.text_body
    assert projection.html_body
    assert projection.execution_state is ActionExecutionState.DRAFT


async def test_no_rendered_body_is_persisted_anywhere(harness: ActionHarness) -> None:
    """Only ``preview_hash`` is stored (ADR-022 § 3).

    A stored body could only ever agree with a regenerated one or be a second version of the
    truth, and it would put the exact external message text into a table the observability rules
    forbid it from reaching in logs.
    """

    await harness.prepare()
    await harness.propose_action().execute(await harness.command())
    projection = await harness.read_current_action().execute(harness.scope)
    assert projection is not None

    proposal = await harness.compile.shareable.load_proposal(
        ActionScope(
            namespace=harness.scope.namespace,
            community_id=harness.scope.community_id,
            case_id=harness.scope.case_id,
            action_id=projection.action_id,
        )
    )
    persisted = str(proposal)
    assert projection.html_body not in persisted
    assert "<h1>" not in persisted


async def test_the_tone_is_stored_as_a_closed_enum(harness: ActionHarness) -> None:
    await harness.prepare()
    result = await harness.propose_action().execute(await harness.command())
    proposal = await harness.compile.shareable.load_proposal(
        ActionScope(
            namespace=harness.scope.namespace,
            community_id=harness.scope.community_id,
            case_id=harness.scope.case_id,
            action_id=result.action_id,
        )
    )

    assert proposal.tone is ActionTone.NEUTRAL


async def test_caveats_round_trip_with_their_citations(harness: ActionHarness) -> None:
    """Structured, not bare strings: the artifact a human approves carries its own proof."""

    view = await harness.prepare()
    fact = view.shareable_facts[0]
    harness.agent.responder = lambda invocation: replace_caveat(
        grounded_draft(invocation.payload), fact.export_fact_id.value
    )

    result = await harness.propose_action().execute(await harness.command())
    proposal = await harness.compile.shareable.load_proposal(
        ActionScope(
            namespace=harness.scope.namespace,
            community_id=harness.scope.community_id,
            case_id=harness.scope.case_id,
            action_id=result.action_id,
        )
    )

    assert len(proposal.caveats) == 1
    assert proposal.caveats[0].export_fact_ids == (fact.export_fact_id.value,)
    assert proposal.caveats[0].caveat_hash.value.startswith("sha256:")


def replace_caveat(draft: ActionProposalDraft, fact_id: UUID) -> ActionProposalDraft:
    """The grounded draft with one caveat citing ``fact_id``, and nothing else changed."""

    return draft.model_copy(
        update={
            "caveats": (
                ActionCaveatDraft(
                    caveat_id=uuid4(),
                    text="This observation has not been independently inspected.",
                    export_fact_ids=(fact_id,),
                ),
            )
        }
    )
