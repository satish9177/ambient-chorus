"""Envelope helpers for the validator-level repair regressions.

The semantic validator takes an invocation, a result, and the exact stored view. Building the
two envelopes by hand -- rather than driving a whole storage harness -- is what lets a
regression name one field, move it by one microsecond, and assert the answer.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from chorus.application.commands.propose_action import to_action_input
from chorus.contracts.action import (
    ActionCaveatDraft,
    ActionClaimDraft,
    ActionInput,
    ActionProposalDraft,
    ActionRequestDraft,
    ActionToneValue,
)
from chorus.contracts.common import (
    AGENT_INPUT_SCHEMA_VERSION,
    AGENT_OUTPUT_SCHEMA_VERSION,
    AgentInputEnvelope,
    AgentName,
    AgentResultEnvelope,
)
from chorus.domain.ids import Namespace
from chorus.ports.agents import ActionInvocation, ActionResult
from chorus.ports.records import StoredShareableView
from tests.repair.pinned import INVOCATION_ID, pinned_view

NAMESPACE = Namespace("TEST_REPAIR_PHASE7")
POLICY_VERSION = "policy/v1"
ACTION_PROMPT = "action/v1"
MODEL_PROFILE_HASH = "sha256:" + "5c" * 32
REQUESTED_AT = datetime.fromisoformat("2030-01-20T09:29:00+00:00")
STARTED_AT = datetime.fromisoformat("2030-01-20T09:30:00+00:00")
COMPLETED_AT = datetime.fromisoformat("2030-01-20T09:30:05+00:00")

CLAIM_ID = UUID("c1a10000-0000-4000-8000-00000000000b")
CAVEAT_ID = UUID("cae10000-0000-4000-8000-00000000000d")


def draft(
    *,
    requested_deadline: datetime | None = None,
    view: StoredShareableView | None = None,
) -> ActionProposalDraft:
    """A proposal that passes every other check, so one field decides the answer."""

    bound = view or pinned_view()
    fact = bound.shareable_facts[0]
    return ActionProposalDraft(
        view_id=bound.view_id.value,
        view_hash=bound.view_hash.value,
        case_id=bound.case_id.value,
        case_version=bound.case_version,
        authorization_version=bound.authorization_version,
        subject="Repeated elevator outages at Maple Court",
        claims=(
            ActionClaimDraft(
                claim_id=CLAIM_ID,
                text=fact.safe_text,
                export_fact_ids=(fact.export_fact_id.value,),
            ),
        ),
        request=ActionRequestDraft(
            requested_action="Please inspect and repair the elevator, then confirm the schedule.",
            requested_deadline=requested_deadline,
            request_fact_ids=(fact.export_fact_id.value,),
        ),
        caveats=(
            ActionCaveatDraft(
                caveat_id=CAVEAT_ID,
                text="Resident counts are aggregated and not independently inspected.",
                export_fact_ids=(fact.export_fact_id.value,),
            ),
        ),
        tone=ActionToneValue.NEUTRAL,
    )


def invocation(view: StoredShareableView | None = None) -> ActionInvocation:
    """The envelope exactly as ``ProposeAction._envelope`` builds it."""

    bound = view or pinned_view()
    payload: ActionInput = to_action_input(bound)
    return AgentInputEnvelope[ActionInput](
        schema_version=AGENT_INPUT_SCHEMA_VERSION,
        invocation_id=INVOCATION_ID,
        namespace=NAMESPACE.value,
        agent_name=AgentName.ACTION,
        case_id=bound.case_id.value,
        case_version=bound.case_version,
        requested_at=REQUESTED_AT,
        policy_version=POLICY_VERSION,
        payload=payload,
    )


def result(output: ActionProposalDraft, *, view: StoredShareableView | None = None) -> ActionResult:
    """The runtime's answer envelope, naming the reviewed prompt artifact."""

    bound = view or pinned_view()
    return AgentResultEnvelope[ActionProposalDraft](
        schema_version=AGENT_OUTPUT_SCHEMA_VERSION,
        invocation_id=INVOCATION_ID,
        namespace=NAMESPACE.value,
        agent_name=AgentName.ACTION,
        case_id=bound.case_id.value,
        case_version=bound.case_version,
        model_profile_arn_hash=MODEL_PROFILE_HASH,
        prompt_version=ACTION_PROMPT,
        started_at=STARTED_AT,
        completed_at=COMPLETED_AT,
        output=output,
    )
