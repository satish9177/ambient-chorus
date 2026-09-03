"""Adversarial validation: every way a Monitor answer can be refused, and proof it is.

Each test changes exactly one thing about an otherwise valid answer and asserts both that the
whole output is rejected and *which* gate rejected it. Asserting the reason code matters: a
test that only checked "something failed" would still pass if the wrong check happened to fire,
which is precisely how a removed rule hides behind a neighbouring one.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.fixtures.monitor_outputs import (
    CONTRIBUTORS,
    NAMESPACE,
    ValidatorCase,
    build_invocation,
    build_output,
    build_result,
    case_id,
    evidence_id,
    message_id,
    span_for,
    valid_case,
)

from chorus.application.services.monitor_validation import validate_monitor_result
from chorus.contracts.common import MONITOR_PROMPT_VERSION
from chorus.contracts.monitor import (
    CandidateLink,
    IssueType,
    MandateSuggestion,
    MessageClassification,
    MonitorCandidateSummary,
    MonitorSourceSpan,
    SensitiveSignal,
)
from chorus.domain.entities import DisclosureScope, FactType, Purpose, SensitivityCategory
from chorus.ports.agents import AgentContractViolationError, AgentRejection


def _validate(case: ValidatorCase) -> object:
    return validate_monitor_result(
        invocation=case.invocation,
        result=case.result,
        namespace=NAMESPACE,
        contributor_by_pseudonym=dict(CONTRIBUTORS),
    )


def _expect(case: ValidatorCase, reason: AgentRejection) -> None:
    with pytest.raises(AgentContractViolationError) as raised:
        _validate(case)
    assert reason.value in raised.value.reason_codes
    assert raised.value.retryable is False


def _mutate(case: ValidatorCase, **changes: object) -> ValidatorCase:
    return ValidatorCase(invocation=case.invocation, output=case.output.model_copy(update=changes))


def test_a_well_formed_answer_about_its_own_input_is_accepted() -> None:
    validated = _validate(valid_case())

    assert len(validated.reports) == 2  # type: ignore[attr-defined]
    assert len(validated.facts) == 2  # type: ignore[attr-defined]
    assert len(validated.groups) == 1  # type: ignore[attr-defined]
    assert validated.groups[0].existing_case_id is None  # type: ignore[attr-defined]


def test_an_answer_for_a_different_invocation_is_refused() -> None:
    case = valid_case()
    foreign = build_result(case.invocation, case.output).model_copy(
        update={"invocation_id": uuid4()}
    )

    with pytest.raises(AgentContractViolationError) as raised:
        validate_monitor_result(
            invocation=case.invocation,
            result=foreign,
            namespace=NAMESPACE,
            contributor_by_pseudonym=dict(CONTRIBUTORS),
        )
    assert AgentRejection.ENVELOPE_MISMATCH.value in raised.value.reason_codes


def test_an_answer_from_an_unreviewed_prompt_version_is_refused() -> None:
    """The answer is compared against the reviewed prompt, not against the request.

    The request names no prompt version at all, which is the point: a runtime that echoed a
    caller-supplied version would satisfy an equality check while running whatever text it
    liked. The only thing worth comparing against is the one version this application has
    actually reviewed.
    """

    case = valid_case()
    stale = build_result(case.invocation, case.output, prompt_version="monitor/v1")

    with pytest.raises(AgentContractViolationError) as raised:
        validate_monitor_result(
            invocation=case.invocation,
            result=stale,
            namespace=NAMESPACE,
            contributor_by_pseudonym=dict(CONTRIBUTORS),
        )
    assert AgentRejection.PROMPT_VERSION_MISMATCH.value in raised.value.reason_codes
    assert "prompt_version" not in type(case.invocation).model_fields
    assert MONITOR_PROMPT_VERSION == "monitor/v2"


def test_a_missing_message_classification_is_refused() -> None:
    case = valid_case()
    trimmed = _mutate(case, message_results=case.output.message_results[:-1])

    _expect(trimmed, AgentRejection.MESSAGE_RESULT_COVERAGE)


def test_a_classification_for_a_message_that_was_not_sent_is_refused() -> None:
    case = valid_case()
    invented = case.output.message_results[0].model_copy(update={"message_id": uuid4()})
    poisoned = _mutate(case, message_results=(invented, *case.output.message_results[1:]))

    _expect(poisoned, AgentRejection.UNKNOWN_MESSAGE_ID)


def test_a_hallucinated_message_citation_is_refused() -> None:
    case = valid_case()
    report = case.output.proposed_reports[0].model_copy(update={"message_ids": (uuid4(),)})
    poisoned = _mutate(case, proposed_reports=(report, *case.output.proposed_reports[1:]))

    _expect(poisoned, AgentRejection.UNKNOWN_MESSAGE_ID)


def test_a_foreign_message_from_another_batch_is_refused() -> None:
    """An identifier that is real elsewhere is still not in *this* invocation's input."""

    case = valid_case()
    report = case.output.proposed_reports[0].model_copy(
        update={"message_ids": (message_id("feed-999"),)}
    )
    poisoned = _mutate(case, proposed_reports=(report, *case.output.proposed_reports[1:]))

    _expect(poisoned, AgentRejection.UNKNOWN_MESSAGE_ID)


def test_two_reports_citing_one_message_are_refused() -> None:
    case = valid_case()
    first, second = case.output.proposed_reports
    duplicated = second.model_copy(update={"message_ids": first.message_ids})
    poisoned = _mutate(case, proposed_reports=(first, duplicated))

    _expect(poisoned, AgentRejection.DUPLICATE_CITATION)


def test_a_report_attributed_to_someone_who_did_not_send_it_is_refused() -> None:
    case = valid_case()
    report = case.output.proposed_reports[0].model_copy(
        update={"contributor_pseudonym_id": "resident-b"}
    )
    poisoned = _mutate(case, proposed_reports=(report, *case.output.proposed_reports[1:]))

    _expect(poisoned, AgentRejection.SOURCE_OWNERSHIP_INVALID)


def test_a_report_attributed_to_an_unknown_pseudonym_is_refused() -> None:
    case = valid_case()
    report = case.output.proposed_reports[0].model_copy(
        update={"contributor_pseudonym_id": "resident-z"}
    )
    poisoned = _mutate(case, proposed_reports=(report, *case.output.proposed_reports[1:]))

    _expect(poisoned, AgentRejection.SOURCE_OWNERSHIP_INVALID)


def test_an_occurrence_outside_the_observation_window_is_refused() -> None:
    from datetime import timedelta

    case = valid_case()
    report = case.output.proposed_reports[0]
    shifted = report.model_copy(
        update={"occurred_at": report.occurred_at + timedelta(days=1)}  # type: ignore[operator]
    )
    poisoned = _mutate(case, proposed_reports=(shifted, *case.output.proposed_reports[1:]))

    _expect(poisoned, AgentRejection.TIMESTAMP_OUT_OF_RANGE)


def test_a_span_whose_quotation_is_not_in_the_message_is_refused() -> None:
    case = valid_case()
    fact = case.output.proposed_facts[0]
    span = fact.source_spans[0]
    forged = span.model_copy(update={"quote": "x" * (span.end - span.start)})
    poisoned_fact = fact.model_copy(update={"source_spans": (forged,)})
    poisoned = _mutate(case, proposed_facts=(poisoned_fact, *case.output.proposed_facts[1:]))

    _expect(poisoned, AgentRejection.SOURCE_SPAN_INVALID)


def test_a_span_beyond_the_end_of_the_message_is_refused() -> None:
    case = valid_case()
    message = case.invocation.payload.messages[0]
    fact = case.output.proposed_facts[0]
    beyond = MonitorSourceSpan(
        message_id=message.message_id,
        start=len(message.text) - 2,
        end=len(message.text) + 3,
        quote="12345",
    )
    poisoned_fact = fact.model_copy(update={"source_spans": (beyond,)})
    poisoned = _mutate(case, proposed_facts=(poisoned_fact, *case.output.proposed_facts[1:]))

    _expect(poisoned, AgentRejection.SOURCE_SPAN_INVALID)


def test_a_span_quoting_a_message_the_report_does_not_own_is_refused() -> None:
    case = valid_case()
    other = case.invocation.payload.messages[2]
    fact = case.output.proposed_facts[0]
    poisoned_fact = fact.model_copy(update={"source_spans": (span_for(other),)})
    poisoned = _mutate(case, proposed_facts=(poisoned_fact, *case.output.proposed_facts[1:]))

    _expect(poisoned, AgentRejection.SOURCE_OWNERSHIP_INVALID)


def test_a_fact_hanging_off_no_report_is_refused() -> None:
    case = valid_case()
    fact = case.output.proposed_facts[0].model_copy(update={"report_client_ref": "report-9"})
    poisoned = _mutate(case, proposed_facts=(fact, *case.output.proposed_facts[1:]))

    _expect(poisoned, AgentRejection.UNKNOWN_CLIENT_REF)


def test_a_fact_citing_evidence_that_was_never_attached_is_refused() -> None:
    case = valid_case()
    fact = case.output.proposed_facts[0].model_copy(
        update={"evidence_ids": (evidence_id("never-sent"),)}
    )
    poisoned = _mutate(case, proposed_facts=(fact, *case.output.proposed_facts[1:]))

    _expect(poisoned, AgentRejection.UNKNOWN_EVIDENCE_ID)


def test_a_protected_fact_type_with_the_wrong_sensitivity_is_refused() -> None:
    from chorus.contracts.monitor import HealthDetailValue
    from chorus.domain.facts import SubjectRelation

    case = valid_case()
    fact = case.output.proposed_facts[0].model_copy(
        update={
            "fact_type": FactType.HEALTH_DETAIL,
            "typed_value": HealthDetailValue(
                fact_type=FactType.HEALTH_DETAIL,
                subject_relation=SubjectRelation.FAMILY,
                detail="a private health detail",
            ),
            "sensitivity": SensitivityCategory.GENERAL,
        }
    )
    poisoned = _mutate(case, proposed_facts=(fact, *case.output.proposed_facts[1:]))

    _expect(poisoned, AgentRejection.SENSITIVITY_MISMATCH)


def test_a_link_to_a_case_that_was_not_in_the_input_is_refused() -> None:
    case = valid_case()
    link = case.output.candidate_links[0].model_copy(
        update={"existing_case_id": case_id("foreign"), "candidate_group_ref": None}
    )
    poisoned = _mutate(case, candidate_links=(link, *case.output.candidate_links[1:]))

    _expect(poisoned, AgentRejection.FOREIGN_CASE_ID)


def test_linking_a_report_to_a_case_of_a_different_issue_type_is_refused() -> None:
    summary = MonitorCandidateSummary(
        case_id=case_id("other-issue"),
        case_version=1,
        title="Something else entirely",
        issue_type=IssueType.OTHER,
        location_area=None,
    )
    invocation = build_invocation(summaries=(summary,))
    output = build_output(invocation)
    link = output.candidate_links[0].model_copy(
        update={"existing_case_id": summary.case_id, "candidate_group_ref": None}
    )
    case = ValidatorCase(
        invocation=invocation,
        output=output.model_copy(update={"candidate_links": (link, *output.candidate_links[1:])}),
    )

    _expect(case, AgentRejection.UNSUPPORTED_CANDIDATE_TRANSITION)


def test_a_report_with_no_candidate_link_is_refused() -> None:
    """Nothing is silently dropped: an unlinked report fails the whole answer."""

    case = valid_case()
    poisoned = _mutate(case, candidate_links=case.output.candidate_links[:1])

    _expect(poisoned, AgentRejection.UNLINKED_REPORT)


def test_a_sensitive_signal_with_an_invalid_span_is_refused() -> None:
    case = valid_case()
    message = case.invocation.payload.messages[0]
    signal = SensitiveSignal(
        message_id=message.message_id,
        category=SensitivityCategory.HEALTH,
        source_span=MonitorSourceSpan(message_id=message.message_id, start=0, end=5, quote="ZZZZZ"),
    )
    poisoned = _mutate(case, sensitive_signals=(signal,))

    _expect(poisoned, AgentRejection.SOURCE_SPAN_INVALID)


def test_a_mandate_suggestion_naming_an_unknown_fact_is_refused() -> None:
    case = valid_case()
    suggestion = MandateSuggestion(
        report_client_ref="report-1",
        fact_client_refs=("fact-does-not-exist",),
        suggested_max_scope=DisclosureScope.ANONYMOUS_CASE,
        suggested_purpose=Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
    )
    poisoned = _mutate(case, mandate_suggestions=(suggestion,))

    _expect(poisoned, AgentRejection.UNKNOWN_CLIENT_REF)


def test_a_noise_classification_is_reported_without_creating_anything() -> None:
    validated = _validate(valid_case())

    assert len(validated.noise_message_ids) == 1  # type: ignore[attr-defined]
    assert validated.policy_like_message_ids == ()  # type: ignore[attr-defined]


def test_a_policy_like_message_is_observed_but_confers_nothing() -> None:
    case = valid_case()
    results = tuple(
        item.model_copy(update={"classification": MessageClassification.POLICY_LIKE_INSTRUCTION})
        if item.classification is MessageClassification.NOISE
        else item
        for item in case.output.message_results
    )
    observed = _validate(_mutate(case, message_results=results))

    assert len(observed.policy_like_message_ids) == 1  # type: ignore[attr-defined]
    # It changed nothing else: the same reports and facts are still proposed.
    assert len(observed.reports) == 2  # type: ignore[attr-defined]
    assert len(observed.facts) == 2  # type: ignore[attr-defined]


def test_an_existing_case_link_groups_onto_that_case() -> None:
    summary = MonitorCandidateSummary(
        case_id=case_id("elevator"),
        case_version=3,
        title="Recurring lift failures",
        issue_type=IssueType.ELEVATOR_FAILURE,
        location_area=None,
        fact_summaries=("INCIDENT_OCCURRENCE: value withheld (REPORTED)",),
    )
    invocation = build_invocation(summaries=(summary,))
    output = build_output(invocation)
    links = tuple(
        link.model_copy(update={"existing_case_id": summary.case_id, "candidate_group_ref": None})
        for link in output.candidate_links
    )
    validated = validate_monitor_result(
        invocation=invocation,
        result=build_result(invocation, output.model_copy(update={"candidate_links": links})),
        namespace=NAMESPACE,
        contributor_by_pseudonym=dict(CONTRIBUTORS),
    )

    assert len(validated.groups) == 1
    assert validated.groups[0].existing_case_id is not None
    assert str(validated.groups[0].existing_case_id) == str(summary.case_id)


def test_a_link_naming_a_report_that_was_not_proposed_is_refused() -> None:
    case = valid_case()
    link = CandidateLink(
        report_client_ref="report-unknown",
        candidate_group_ref="orphan-group",
        proposed_case_title="Something",
        similarity_reasons=("because",),
        confidence="0.5",
    )
    poisoned = _mutate(case, candidate_links=(*case.output.candidate_links, link))

    _expect(poisoned, AgentRejection.UNKNOWN_CLIENT_REF)
