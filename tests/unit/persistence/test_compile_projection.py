"""The compiler audit projection: its bounds, its shape, and its size proof.

One row holds a whole compile's private lineage, so the interesting question is not whether it
round-trips -- ``test_codec`` already asserts that -- but whether the *largest legal* row still
fits. A hundred requested facts is reachable at the frozen per-case maxima, so the answer has
to be a calculation against the real codec rather than a guess about typical size.
"""

from __future__ import annotations

import pytest
from tests.fixtures.persistence import DEMO_RETENTION, NOW, PRIMARY, digest

from chorus.domain.entities import DisclosureScope, Purpose
from chorus.domain.ids import EvidenceItemId, ExportFactId, FactId, SafeEvidenceRefId
from chorus.infrastructure.dynamodb import codec_audit
from chorus.infrastructure.dynamodb.attributes import encode_item
from chorus.ports.limits import (
    MAX_COMPILE_REQUESTED_EVIDENCE,
    MAX_COMPILE_REQUESTED_FACTS,
    MAX_COMPILER_GATES,
)
from chorus.ports.records import (
    CompileDecisionOutcome,
    CompiledEvidenceRecord,
    CompiledFactRecord,
    CompileItemOutcome,
    CompilerAuditProjection,
    CompilerGateRecord,
)
from chorus.privacy.policy import (
    MAX_REQUESTED_EVIDENCE,
    MAX_REQUESTED_FACTS,
    CompileReasonCode,
    CompilerGate,
)

DYNAMODB_ITEM_LIMIT_BYTES = 400 * 1024
"""DynamoDB's hard per-item bound. Not a tuning knob; a wall."""


def test_the_port_bounds_equal_the_frozen_policy_bounds() -> None:
    """Ports restate these numbers rather than importing privacy, so they must be checked.

    A drift here would size the projection against a request bound the compiler no longer
    enforces, which is the one way this row could legitimately grow past what was measured.
    """

    assert MAX_COMPILE_REQUESTED_FACTS == MAX_REQUESTED_FACTS
    assert MAX_COMPILE_REQUESTED_EVIDENCE == MAX_REQUESTED_EVIDENCE
    assert max(gate.value for gate in CompilerGate) == MAX_COMPILER_GATES


def _maximal_projection() -> CompilerAuditProjection:
    """Build the largest row policy/v1 permits: every bound at its ceiling.

    Every string field is filled to its own maximum too -- the longest reason codes the closed
    enum contains, a full-length transformation rule identifier, and the longest gate name --
    so the measurement is of the worst case rather than of the demo.
    """

    longest_reason = max((code.value for code in CompileReasonCode), key=len)
    longest_gate_name = max((gate.name for gate in CompilerGate), key=len)
    world = PRIMARY
    return CompilerAuditProjection(
        namespace=world.namespace,
        community_id=world.community_id,
        case_id=world.case_id,
        compile_id=world.uuid("max-compile"),
        audit_event_id=world.uuid("max-audit"),
        requested_at=NOW,
        created_at=NOW,
        based_on_case_version=2**31 - 1,
        compiler_version="c" * 64,
        policy_version="p" * 64,
        destination_id=world.compile_projection().destination_id,
        destination_registry_version=2**31 - 1,
        destination_routing_token=world.uuid("max-routing"),
        purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
        decision=CompileDecisionOutcome.ALLOW,
        reason_codes=(longest_reason,),
        gates=tuple(
            CompilerGateRecord(
                gate=gate.value,
                gate_name=longest_gate_name,
                outcome="EXCLUDED",
                reason_codes=(longest_reason,),
            )
            for gate in CompilerGate
        ),
        facts=tuple(
            CompiledFactRecord(
                fact_id=FactId(world.uuid(f"max-fact:{index}")),
                necessity="REQUIRED",
                intended_usage="AGGREGATION_INPUT",
                granted_scope=DisclosureScope.EXTERNAL_ACTION,
                outcome=CompileItemOutcome.INCLUDED,
                reason_codes=(longest_reason,),
                export_fact_ids=(ExportFactId(world.uuid(f"max-export:{index}")),),
                transformation_rule_id="r" * 64,
            )
            for index in range(MAX_COMPILE_REQUESTED_FACTS)
        ),
        evidence=tuple(
            CompiledEvidenceRecord(
                source_evidence_id=EvidenceItemId(world.uuid(f"max-evidence:{index}")),
                outcome=CompileItemOutcome.INCLUDED,
                reason_codes=(longest_reason,),
                safe_evidence_ref_id=SafeEvidenceRefId(world.uuid(f"max-ref:{index}")),
                export_handle_id=world.uuid(f"max-handle:{index}"),
                derivative_sha256=digest(f"max-derivative:{index}"),
            )
            for index in range(MAX_COMPILE_REQUESTED_EVIDENCE)
        ),
        view_id=world.view_id,
        view_hash=digest("max-view"),
    )


def test_the_largest_legal_projection_fits_inside_the_item_limit() -> None:
    """Measure the real encoded item, not an estimate of one."""

    item = codec_audit.encode_compile_projection(
        PRIMARY.case_scope, _maximal_projection(), retention=DEMO_RETENTION
    )
    encoded = encode_item(item)
    size = len(repr(encoded).encode("utf-8"))

    assert size < DYNAMODB_ITEM_LIMIT_BYTES
    # A margin, not a coincidence. Half the limit means the bound could double before this
    # became a design problem rather than a test failure.
    assert size < DYNAMODB_ITEM_LIMIT_BYTES // 2


def test_the_maximal_projection_round_trips() -> None:
    """Size is only interesting if the row at that size is still readable."""

    projection = _maximal_projection()
    item = codec_audit.encode_compile_projection(
        PRIMARY.case_scope, projection, retention=DEMO_RETENTION
    )

    _, decoded = codec_audit.decode_compile_projection(item)

    assert decoded == projection


def test_a_denied_compile_names_no_view() -> None:
    """The pair is filled exactly when there is an artifact to name."""

    with pytest.raises(ValueError, match="view is named exactly"):
        CompilerAuditProjection(
            namespace=PRIMARY.namespace,
            community_id=PRIMARY.community_id,
            case_id=PRIMARY.case_id,
            compile_id=PRIMARY.uuid("denied"),
            audit_event_id=PRIMARY.uuid("denied-audit"),
            requested_at=NOW,
            created_at=NOW,
            based_on_case_version=1,
            compiler_version="compiler/1.1.0",
            policy_version="policy/v1",
            destination_id=PRIMARY.compile_projection().destination_id,
            destination_registry_version=1,
            destination_routing_token=PRIMARY.uuid("routing-token"),
            purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
            decision=CompileDecisionOutcome.DENY,
            view_id=PRIMARY.view_id,
            view_hash=digest("view"),
        )


def test_a_denied_compile_cannot_carry_an_included_fact() -> None:
    """A denial is whole. A row claiming otherwise would contradict its own decision."""

    with pytest.raises(ValueError, match="denied compile included nothing"):
        CompilerAuditProjection(
            namespace=PRIMARY.namespace,
            community_id=PRIMARY.community_id,
            case_id=PRIMARY.case_id,
            compile_id=PRIMARY.uuid("denied"),
            audit_event_id=PRIMARY.uuid("denied-audit"),
            requested_at=NOW,
            created_at=NOW,
            based_on_case_version=1,
            compiler_version="compiler/1.1.0",
            policy_version="policy/v1",
            destination_id=PRIMARY.compile_projection().destination_id,
            destination_registry_version=1,
            destination_routing_token=PRIMARY.uuid("routing-token"),
            purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
            decision=CompileDecisionOutcome.DENY,
            facts=(
                CompiledFactRecord(
                    fact_id=PRIMARY.fact_id,
                    necessity="OPTIONAL",
                    intended_usage="CLAIM",
                    granted_scope=DisclosureScope.ANONYMOUS_CASE,
                    outcome=CompileItemOutcome.INCLUDED,
                    export_fact_ids=(ExportFactId(PRIMARY.uuid("export")),),
                ),
            ),
        )


@pytest.mark.parametrize(
    "codes",
    [
        ("a lowercase sentence",),
        ("The elevator trapped Leela on the fourth floor.",),
        ("OK", "OK"),
    ],
    ids=["lowercase", "sentence", "duplicate"],
)
def test_reason_codes_reject_anything_that_is_not_a_closed_code(codes: tuple[str, ...]) -> None:
    """The shape is the control. A free-text field is where a rationale eventually lands."""

    with pytest.raises(ValueError):
        CompiledFactRecord(
            fact_id=PRIMARY.fact_id,
            necessity="OPTIONAL",
            intended_usage="CLAIM",
            granted_scope=None,
            outcome=CompileItemOutcome.EXCLUDED,
            reason_codes=codes,
        )
