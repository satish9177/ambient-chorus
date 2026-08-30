from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.fixtures.elevator import NOW, _uuid, build_elevator_fixture

from chorus.domain.entities import (
    ActionClaim,
    ActionProposal,
    ActionProposalStatus,
    Approval,
    ApprovalDecision,
)
from chorus.domain.ids import ActionId, ApprovalId, Sha256Digest, ViewId
from chorus.privacy.canonical import (
    canonical_bytes,
    hash_action_claim,
    hash_action_proposal,
    hash_approval,
    hash_mandate_terms,
    hash_value,
    verify_hash,
)


def test_rfc8785_captured_integer_string_vector() -> None:
    value = {"string": '€$\u000f\nA\'B"\\"/', "literals": [None, True, False], "integer": 42}

    rendered = canonical_bytes(value)

    assert (
        rendered == b'{"integer":42,"literals":[null,true,false],'
        b'"string":"\xe2\x82\xac$\\u000f\\nA\'B\\"\\\\\\"/"}'
    )


def test_canonicalization_rejects_floats() -> None:
    with pytest.raises(TypeError, match="floating-point"):
        canonical_bytes({"unsafe": 1.5})


def test_mapping_keys_that_collide_after_nfc_normalization_are_rejected() -> None:
    with pytest.raises(TypeError, match="duplicate NFC-normalized keys"):
        canonical_bytes({"é": 1, "e\u0301": 2})


def test_hash_round_trip_and_constant_time_verification() -> None:
    value = {"b": 2, "a": 1}
    digest = hash_value(value)

    assert digest == Sha256Digest(
        "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    )
    assert verify_hash(value, digest)


def test_mandate_terms_hash_changes_for_authorization_terms() -> None:
    mandate = build_elevator_fixture().context.mandates[0]
    assert mandate.expires_at is not None
    changed = replace(mandate, expires_at=mandate.expires_at.replace(microsecond=1))

    assert hash_mandate_terms(changed) != mandate.terms_hash


def test_proposal_and_approval_hash_primitives_bind_authorization_tuple() -> None:
    fixture = build_elevator_fixture()
    empty = Sha256Digest("sha256:" + "0" * 64)
    export_id = _uuid("export-fact:hash-test")
    claim_draft = ActionClaim(
        claim_id=_uuid("claim:hash-test"),
        text="A supported elevator incident occurred.",
        export_fact_ids=(export_id,),
        claim_hash=empty,
    )
    claim = replace(claim_draft, claim_hash=hash_action_claim(claim_draft))
    proposal_draft = ActionProposal(
        action_id=ActionId(_uuid("action:hash-test")),
        case_id=fixture.context.case.case_id,
        case_version=fixture.context.case.version,
        view_id=ViewId(_uuid("view:hash-test")),
        view_hash=Sha256Digest("sha256:" + "1" * 64),
        subject="Elevator repair request",
        claims=(claim,),
        requested_action="Inspect and repair the elevator.",
        requested_deadline=None,
        request_fact_ids=(export_id,),
        caveats=(),
        tone="PROFESSIONAL",
        agent_invocation_id=_uuid("invocation:hash-test"),
        prompt_version="action/v1",
        proposal_hash=empty,
        status=ActionProposalStatus.DRAFT,
        created_at=NOW,
    )
    proposal = replace(
        proposal_draft,
        proposal_hash=hash_action_proposal(proposal_draft),
    )
    approval_draft = Approval(
        approval_id=ApprovalId(_uuid("approval:hash-test")),
        action_id=proposal.action_id,
        case_id=proposal.case_id,
        proposal_hash=proposal.proposal_hash,
        view_hash=proposal.view_hash,
        approver_id=fixture.contributor_ids[0],
        decision=ApprovalDecision.APPROVED,
        approved_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        consumed_at=None,
        approval_hash=empty,
        idempotency_key="approval-hash-test",
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    approval = replace(approval_draft, approval_hash=hash_approval(approval_draft))

    assert proposal.proposal_hash != empty
    assert approval.approval_hash != empty
    changed = replace(approval, view_hash=Sha256Digest("sha256:" + "2" * 64))
    assert hash_approval(changed) != approval.approval_hash
