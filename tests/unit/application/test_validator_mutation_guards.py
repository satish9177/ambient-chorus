"""Mutation guards: proof that each rejection comes from the check it is credited to.

An adversarial test that only asserts "this was refused" can keep passing after its check is
deleted, because some neighbouring rule happens to catch the same input. These tests close that
gap from the other side: each one disables exactly one check and asserts the adversarial input
is then *accepted*.

Read them as a pair with the corresponding test in ``test_monitor_validation.py``. Together
they say: with the check, refused; without it, accepted. Deleting the check therefore turns one
of the two red.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.fixtures.monitor_outputs import (
    CONTRIBUTORS,
    NAMESPACE,
    ValidatorCase,
    build_result,
    valid_case,
)

from chorus.application.services import monitor_validation
from chorus.application.services.monitor_validation import validate_monitor_result
from chorus.contracts.monitor import MonitorSourceSpan
from chorus.ports.agents import AgentContractViolationError


def _validate(case: ValidatorCase) -> object:
    return validate_monitor_result(
        invocation=case.invocation,
        result=case.result,
        namespace=NAMESPACE,
        contributor_by_pseudonym=dict(CONTRIBUTORS),
    )


def _with_forged_quotation() -> ValidatorCase:
    case = valid_case()
    fact = case.output.proposed_facts[0]
    span = fact.source_spans[0]
    forged = span.model_copy(update={"quote": "x" * (span.end - span.start)})
    poisoned = fact.model_copy(update={"source_spans": (forged,)})
    return ValidatorCase(
        invocation=case.invocation,
        output=case.output.model_copy(
            update={"proposed_facts": (poisoned, *case.output.proposed_facts[1:])}
        ),
    )


def _with_hallucinated_message() -> ValidatorCase:
    case = valid_case()
    report = case.output.proposed_reports[0].model_copy(update={"message_ids": (uuid4(),)})
    return ValidatorCase(
        invocation=case.invocation,
        output=case.output.model_copy(
            update={"proposed_reports": (report, *case.output.proposed_reports[1:])}
        ),
    )


def test_span_validation_is_what_rejects_a_forged_quotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _with_forged_quotation()
    with pytest.raises(AgentContractViolationError):
        _validate(case)

    monkeypatch.setattr(monitor_validation, "_validate_spans", lambda *_, **__: True)

    accepted = _validate(case)
    assert len(accepted.facts) == 2  # type: ignore[attr-defined]


def test_citation_membership_is_what_rejects_a_hallucinated_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _with_hallucinated_message()
    with pytest.raises(AgentContractViolationError):
        _validate(case)

    real = monitor_validation._validate_reports

    def permissive(**kwargs: object) -> object:
        """Accept any citation by pretending every named message was in the input."""

        messages = kwargs["messages_by_id"]
        assert isinstance(messages, dict)
        output = kwargs["output"]
        for report in output.proposed_reports:  # type: ignore[attr-defined]
            for message_id in report.message_ids:
                messages.setdefault(message_id, next(iter(messages.values())))
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(monitor_validation, "_validate_reports", permissive)

    # With membership no longer decided by the input set, the invented identifier is accepted.
    # That is what makes the original refusal attributable to this check and not another.
    accepted = _validate(case)
    assert len(accepted.reports) == 2  # type: ignore[attr-defined]


def test_the_envelope_check_is_what_rejects_a_foreign_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = valid_case()
    foreign = build_result(case.invocation, case.output).model_copy(
        update={"invocation_id": uuid4()}
    )

    with pytest.raises(AgentContractViolationError):
        validate_monitor_result(
            invocation=case.invocation,
            result=foreign,
            namespace=NAMESPACE,
            contributor_by_pseudonym=dict(CONTRIBUTORS),
        )

    monkeypatch.setattr(monitor_validation, "_validate_envelope", lambda *_, **__: None)

    accepted = validate_monitor_result(
        invocation=case.invocation,
        result=foreign,
        namespace=NAMESPACE,
        contributor_by_pseudonym=dict(CONTRIBUTORS),
    )
    assert len(accepted.reports) == 2


def test_coverage_checking_is_what_rejects_an_unclassified_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = valid_case()
    trimmed = ValidatorCase(
        invocation=case.invocation,
        output=case.output.model_copy(update={"message_results": case.output.message_results[:-1]}),
    )

    with pytest.raises(AgentContractViolationError):
        _validate(trimmed)

    monkeypatch.setattr(monitor_validation, "_validate_message_coverage", lambda *_, **__: None)

    accepted = _validate(trimmed)
    assert len(accepted.reports) == 2  # type: ignore[attr-defined]


def test_linkage_completeness_is_what_rejects_a_silently_dropped_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = valid_case()
    unlinked = ValidatorCase(
        invocation=case.invocation,
        output=case.output.model_copy(update={"candidate_links": case.output.candidate_links[:1]}),
    )

    with pytest.raises(AgentContractViolationError):
        _validate(unlinked)

    real = monitor_validation._validate_candidate_links

    def permissive(**kwargs: object) -> object:
        rejections = kwargs["rejections"]
        groups = real(**kwargs)  # type: ignore[arg-type]
        rejections._reasons.clear()  # type: ignore[attr-defined]
        return groups

    monkeypatch.setattr(monitor_validation, "_validate_candidate_links", permissive)

    accepted = _validate(unlinked)
    assert len(accepted.groups) == 1  # type: ignore[attr-defined]


def test_the_contract_itself_is_what_rejects_a_model_supplied_identifier() -> None:
    """This one cannot be mutated away, because the refusal is in the type."""

    from pydantic import ValidationError

    from chorus.contracts.common import reject_identifier_shaped
    from chorus.contracts.monitor import ProposedReport

    with pytest.raises(ValueError, match="must not be an identifier"):
        reject_identifier_shaped(str(uuid4()), "client_ref")

    case = valid_case()
    message = case.invocation.payload.messages[0]
    with pytest.raises(ValidationError):
        ProposedReport(
            client_ref=str(uuid4()),
            message_ids=(message.message_id,),
            contributor_pseudonym_id=message.contributor_pseudonym_id,
            issue_type=case.output.proposed_reports[0].issue_type,
            summary="a summary",
        )


def test_span_self_consistency_is_enforced_by_the_type_not_the_validator() -> None:
    """A span that lies about its own length never reaches the validator at all."""

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MonitorSourceSpan(message_id=uuid4(), start=0, end=12, quote="too short")
