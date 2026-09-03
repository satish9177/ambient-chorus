"""Schema-level strictness of the Monitor contract.

These are the checks that happen before any semantic validation: unknown fields, missing
fields, coerced values, malformed enums, and the two structural rules the contract enforces on
its own -- a span that describes itself consistently, and a client reference that is not
pretending to be a durable identifier.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from tests.fixtures.monitor_outputs import (
    build_invocation,
    build_output,
    message_id,
    span_for,
)

from chorus.contracts.common import (
    AGENT_INPUT_SCHEMA_VERSION,
    AgentInputEnvelope,
    AgentName,
)
from chorus.contracts.monitor import (
    IncidentOccurrenceValue,
    IssueType,
    MonitorFactValue,
    MonitorInput,
    MonitorOutput,
    MonitorSourceSpan,
    ProposedFact,
    ProposedReport,
)
from chorus.domain.entities import FactType, SensitivityCategory
from chorus.domain.facts import FailureMode


def _output_json(**changes: object) -> str:
    invocation = build_invocation()
    payload = json.loads(build_output(invocation).model_dump_json())
    payload.update(changes)
    return json.dumps(payload)


def test_a_valid_answer_round_trips_through_json() -> None:
    invocation = build_invocation()
    original = build_output(invocation)

    parsed = MonitorOutput.model_validate_json(original.model_dump_json())

    assert parsed == original


def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        MonitorOutput.model_validate_json(_output_json(authorised="true"))


def test_a_missing_required_field_is_refused() -> None:
    invocation = build_invocation()
    payload = json.loads(build_output(invocation).model_dump_json())
    del payload["message_results"]

    with pytest.raises(ValidationError):
        MonitorOutput.model_validate_json(json.dumps(payload))


def test_an_unknown_enum_value_is_refused() -> None:
    invocation = build_invocation()
    payload = json.loads(build_output(invocation).model_dump_json())
    payload["message_results"][0]["classification"] = "VERIFIED"

    with pytest.raises(ValidationError):
        MonitorOutput.model_validate_json(json.dumps(payload))


def test_a_string_where_a_number_belongs_is_not_coerced() -> None:
    invocation = build_invocation()
    payload = json.loads(build_output(invocation).model_dump_json())
    payload["proposed_facts"][0]["source_spans"][0]["start"] = "0"

    with pytest.raises(ValidationError):
        MonitorOutput.model_validate_json(json.dumps(payload))


def test_an_unknown_fact_discriminator_is_refused() -> None:
    invocation = build_invocation()
    payload = json.loads(build_output(invocation).model_dump_json())
    payload["proposed_facts"][0]["typed_value"]["fact_type"] = "CONTRADICTION"

    with pytest.raises(ValidationError):
        MonitorOutput.model_validate_json(json.dumps(payload))


def test_a_fact_type_disagreeing_with_its_typed_value_is_refused() -> None:
    message = build_invocation().payload.messages[0]

    with pytest.raises(ValidationError):
        ProposedFact(
            client_ref="fact-1",
            report_client_ref="report-1",
            fact_type=FactType.SERVICE_IMPACT,
            typed_value=IncidentOccurrenceValue(
                fact_type=FactType.INCIDENT_OCCURRENCE,
                occurred_at=message.sent_at,
                failure_mode=FailureMode.STUCK,
            ),
            sensitivity=SensitivityCategory.GENERAL,
            source_spans=(span_for(message),),
        )


def test_a_span_whose_length_disagrees_with_its_quotation_is_refused() -> None:
    with pytest.raises(ValidationError):
        MonitorSourceSpan(message_id=uuid4(), start=0, end=10, quote="short")


def test_an_inverted_span_is_refused() -> None:
    with pytest.raises(ValidationError):
        MonitorSourceSpan(message_id=uuid4(), start=9, end=4, quote="abcde")


def test_a_client_reference_shaped_like_an_identifier_is_refused() -> None:
    """The model may not name durable state, so it may not answer with an identifier."""

    message = build_invocation().payload.messages[0]

    with pytest.raises(ValidationError):
        ProposedReport(
            client_ref=str(uuid4()),
            message_ids=(message.message_id,),
            contributor_pseudonym_id=message.contributor_pseudonym_id,
            issue_type=IssueType.ELEVATOR_FAILURE,
            summary="a summary",
        )


def test_the_contract_has_no_field_for_a_durable_identifier() -> None:
    """A report, fact, or case identifier has no place to arrive from the model."""

    forbidden = {"report_id", "fact_id", "case_id", "message_id"}
    assert forbidden.isdisjoint(ProposedReport.model_fields)
    assert forbidden.isdisjoint(ProposedFact.model_fields)


def test_the_only_identifier_a_link_may_echo_is_an_existing_case() -> None:
    from chorus.contracts.monitor import CandidateLink

    assert "existing_case_id" in CandidateLink.model_fields
    assert "candidate_group_ref" in CandidateLink.model_fields
    assert not {"new_case_id", "report_id", "case_id"} & set(CandidateLink.model_fields)


def test_duplicate_message_results_are_refused() -> None:
    invocation = build_invocation()
    payload = json.loads(build_output(invocation).model_dump_json())
    payload["message_results"].append(payload["message_results"][0])

    with pytest.raises(ValidationError):
        MonitorOutput.model_validate_json(json.dumps(payload))


def test_a_report_appearing_in_two_candidate_links_is_refused() -> None:
    invocation = build_invocation()
    payload = json.loads(build_output(invocation).model_dump_json())
    payload["candidate_links"].append(payload["candidate_links"][0])

    with pytest.raises(ValidationError):
        MonitorOutput.model_validate_json(json.dumps(payload))


def test_a_confidence_outside_zero_to_one_is_refused() -> None:
    invocation = build_invocation()
    payload = json.loads(build_output(invocation).model_dump_json())
    payload["candidate_links"][0]["confidence"] = "1.5"

    with pytest.raises(ValidationError):
        MonitorOutput.model_validate_json(json.dumps(payload))


def test_a_floating_point_confidence_is_refused() -> None:
    invocation = build_invocation()
    payload = json.loads(build_output(invocation).model_dump_json())
    payload["candidate_links"][0]["confidence"] = 0.7

    with pytest.raises(ValidationError):
        MonitorOutput.model_validate_json(json.dumps(payload))


def test_a_naive_timestamp_is_refused() -> None:
    invocation = build_invocation()
    payload = json.loads(build_output(invocation).model_dump_json())
    payload["proposed_reports"][0]["occurred_at"] = "2030-01-08T09:00:00"

    with pytest.raises(ValidationError):
        MonitorOutput.model_validate_json(json.dumps(payload))


def test_an_input_batch_beyond_the_frozen_bound_is_refused() -> None:
    invocation = build_invocation()
    single = invocation.payload.messages[0]
    too_many = tuple(
        single.model_copy(update={"message_id": message_id(f"overflow-{index}")})
        for index in range(51)
    )

    with pytest.raises(ValidationError):
        MonitorInput(messages=too_many)


def test_an_envelope_naming_a_case_without_a_version_is_refused() -> None:
    with pytest.raises(ValidationError):
        AgentInputEnvelope[MonitorInput](
            schema_version=AGENT_INPUT_SCHEMA_VERSION,
            invocation_id=uuid4(),
            namespace="DEMO",
            agent_name=AgentName.MONITOR,
            case_id=uuid4(),
            case_version=None,
            requested_at=datetime(2030, 1, 14, tzinfo=UTC),
            policy_version="policy/v1",
            payload=build_invocation().payload,
        )


def test_an_envelope_with_a_naive_request_time_is_refused() -> None:
    with pytest.raises(ValidationError):
        AgentInputEnvelope[MonitorInput](
            schema_version=AGENT_INPUT_SCHEMA_VERSION,
            invocation_id=uuid4(),
            namespace="DEMO",
            agent_name=AgentName.MONITOR,
            case_id=None,
            case_version=None,
            requested_at=datetime(2030, 1, 14),
            policy_version="policy/v1",
            payload=build_invocation().payload,
        )


def test_intake_cannot_propose_a_contradiction_or_a_commitment_term() -> None:
    """Those two shapes belong to later phases and have no contract to arrive through."""

    variants = {
        variant.model_fields["fact_type"].annotation
        for variant in MonitorFactValue.__value__.__args__
    }
    rendered = {str(variant) for variant in variants}
    assert not any("CONTRADICTION" in value for value in rendered)
    assert not any("COMMITMENT_TERM" in value for value in rendered)
