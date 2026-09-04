"""Phase 5 persistence: the ADR-017 locator grammar, and reading v1 assessments as v2.

Two schema moves land in this phase and both are read-old-write-new. The evidence-root ID
locator is new, so its grammar is pinned here before anything depends on it; the assessment and
the application operation both gain a v2 spelling of values that did not change meaning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid5

import pytest

from chorus.domain.entities import (
    ASSESSMENT_SCHEMA_VERSION_V1,
    ASSESSMENT_SCHEMA_VERSION_V2,
    ApplicationOperation,
    ApplicationOperationKind,
    ApplicationOperationStatus,
    AssessmentAlternative,
    AssessmentContradiction,
    ContradictionMateriality,
    EvidenceFinding,
    EvidenceStatus,
    InvestigationAssessment,
)
from chorus.domain.ids import (
    AssessmentId,
    CaseId,
    CommunityId,
    EvidenceItemId,
    EvidenceRootId,
    FactId,
    Namespace,
    OperationId,
    ReportId,
    Sha256Digest,
)
from chorus.infrastructure.dynamodb import codec_case, codec_core, keys
from chorus.infrastructure.dynamodb.codec import EntityType
from chorus.ports.records import EvidenceRootLocator
from chorus.ports.scopes import CaseScope, CommunityScope, NamespaceScope

SEED = UUID("3a4b5c6d-7e8f-5901-a2b3-c4d5e6f70819")
NOW = datetime(2030, 5, 1, 9, 0, 0, tzinfo=UTC)
NAMESPACE = Namespace("TEST_RECORDS")


def uuid(name: str) -> UUID:
    return uuid5(SEED, name)


def digest(value: str) -> Sha256Digest:
    from hashlib import sha256

    return Sha256Digest(f"sha256:{sha256(value.encode()).hexdigest()}")


COMMUNITY = CommunityId(uuid("community"))
CASE = CaseId(uuid("case"))
COMMUNITY_SCOPE = CommunityScope(namespace=NAMESPACE, community_id=COMMUNITY)
CASE_SCOPE = CaseScope(namespace=NAMESPACE, community_id=COMMUNITY, case_id=CASE)
NAMESPACE_SCOPE = NamespaceScope(namespace=NAMESPACE)


# -- ADR-017: the locator grammar ------------------------------------------------------------


def test_the_locator_sort_key_grammar_is_pinned() -> None:
    root_id = EvidenceRootId(uuid("root"))
    assert keys.evidence_root_id_sort_key(root_id) == f"EVIDENCE_ROOT_ID#{root_id}"


def test_the_locator_and_the_canonical_root_are_distinct_addresses() -> None:
    """``EVIDENCE_ROOT#`` is not a prefix of ``EVIDENCE_ROOT_ID#``, so neither query sees both."""

    root_id = EvidenceRootId(uuid("root"))
    content = keys.evidence_root_sort_key(digest("bytes"))
    locator = keys.evidence_root_id_sort_key(root_id)
    assert not locator.startswith("EVIDENCE_ROOT#")
    assert not content.startswith("EVIDENCE_ROOT_ID#")


def test_a_locator_round_trips_and_carries_exactly_one_value() -> None:
    locator = EvidenceRootLocator(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        root_id=EvidenceRootId(uuid("root")),
        root_sha256=digest("bytes"),
        created_at=NOW,
    )
    item = codec_core.encode_evidence_root_locator(COMMUNITY_SCOPE, locator)
    assert item["entity_type"] == EntityType.EVIDENCE_ROOT_LOCATOR.value
    _, decoded = codec_core.decode_evidence_root_locator(item)
    assert decoded == locator
    # It is not a second copy of the root, so the two can never disagree about anything but
    # existence. Everything the canonical row holds is deliberately absent here.
    assert "media_type" not in item
    assert "derivation_kind" not in item
    assert "parent_root_id" not in item


# -- the assessment, v1 read and v2 write -----------------------------------------------------


def assessment(**overrides: object) -> InvestigationAssessment:
    base: dict[str, object] = dict(  # noqa: C408 - kwargs mirror the model signature
        assessment_id=AssessmentId(uuid("assessment")),
        case_id=CASE,
        based_on_case_version=3,
        agent_invocation_id=uuid("invocation"),
        linkage_decision="SAME_ISSUE",
        findings=(
            EvidenceFinding(
                fact_id=FactId(uuid("fact:a")),
                evidence_status=EvidenceStatus.CORROBORATED,
                reason_code="MULTIPLE_INDEPENDENT_SOURCES",
            ),
        ),
        contradictions=(
            AssessmentContradiction(
                statement_fact_ids=(FactId(uuid("fact:a")), FactId(uuid("fact:b"))),
                description="Two accounts of one morning.",
                materiality=ContradictionMateriality.MEDIUM,
            ),
        ),
        alternative_explanations=(
            AssessmentAlternative(
                description="Scheduled maintenance.",
                cited_report_ids=(ReportId(uuid("report:a")),),
                cited_fact_ids=(FactId(uuid("fact:a")),),
                cited_evidence_ids=(EvidenceItemId(uuid("evidence:a")),),
            ),
        ),
        independent_source_count=2,
        is_corroborated=True,
        recommended_disposition="READY_FOR_ACTION",
        assessment_hash=digest("assessment"),
        created_at=NOW,
    )
    base.update(overrides)
    return InvestigationAssessment(**base)  # type: ignore[arg-type]


def test_an_assessment_round_trips_with_its_structure_intact() -> None:
    """Neither materiality nor a citation may be flattened away by the codec."""

    original = assessment()
    item = codec_case.encode_assessment(CASE_SCOPE, original)
    _, decoded = codec_case.decode_assessment(item)
    assert decoded == original
    assert decoded.contradictions[0].materiality is ContradictionMateriality.MEDIUM
    assert decoded.alternative_explanations[0].cited_evidence_ids


def test_writers_always_emit_v2() -> None:
    item = codec_case.encode_assessment(CASE_SCOPE, assessment())
    assert item["schema_version"] == ASSESSMENT_SCHEMA_VERSION_V2


def test_a_v1_row_decodes_and_its_unrecorded_materiality_reads_conservatively() -> None:
    """A v1 row records no materiality, and a missing block-only value is read as blocking."""

    v2 = codec_case.encode_assessment(CASE_SCOPE, assessment())
    v1 = {
        key: value
        for key, value in v2.items()
        if key not in {"contradictions", "alternative_explanations"}
    }
    v1["schema_version"] = ASSESSMENT_SCHEMA_VERSION_V1
    v1["contradiction_fact_ids"] = (str(uuid("fact:a")), str(uuid("fact:b")))
    v1["alternative_explanations"] = ("Scheduled maintenance.",)

    _, decoded = codec_case.decode_assessment(v1)

    assert decoded.schema_version == ASSESSMENT_SCHEMA_VERSION_V2
    assert decoded.contradictions[0].materiality is ContradictionMateriality.HIGH
    assert decoded.contradictions[0].description == codec_case.V1_CONTRADICTION_DESCRIPTION
    assert decoded.blocking_contradiction is True
    assert decoded.alternative_explanations[0].description == "Scheduled maintenance."
    assert decoded.alternative_explanations[0].cited_fact_ids == ()


def test_a_v1_row_with_one_cited_fact_carries_no_contradiction_forward() -> None:
    """Fewer than two cited facts names no conflict, so there is nothing to carry."""

    v2 = codec_case.encode_assessment(CASE_SCOPE, assessment(contradictions=()))
    v1 = {
        key: value
        for key, value in v2.items()
        if key not in {"contradictions", "alternative_explanations"}
    }
    v1["schema_version"] = ASSESSMENT_SCHEMA_VERSION_V1
    v1["contradiction_fact_ids"] = (str(uuid("fact:a")),)
    v1["alternative_explanations"] = ()

    _, decoded = codec_case.decode_assessment(v1)

    assert decoded.contradictions == ()


def test_the_contradicted_fact_ids_helper_is_sorted_and_deduplicated() -> None:
    subject = assessment(
        contradictions=(
            AssessmentContradiction(
                statement_fact_ids=(FactId(uuid("fact:a")), FactId(uuid("fact:b"))),
                description="One.",
                materiality=ContradictionMateriality.LOW,
            ),
            AssessmentContradiction(
                statement_fact_ids=(FactId(uuid("fact:b")), FactId(uuid("fact:c"))),
                description="Two.",
                materiality=ContradictionMateriality.LOW,
            ),
        )
    )
    cited = subject.contradicted_fact_ids
    assert len(cited) == 3
    assert list(cited) == sorted(cited, key=str)


def test_an_assessment_refuses_a_corroboration_flag_that_disagrees() -> None:
    with pytest.raises(ValueError):
        assessment(independent_source_count=1, is_corroborated=True)


# -- ADR-016: the generalized operation handover ------------------------------------------------


def operation(**overrides: object) -> ApplicationOperation:
    base: dict[str, object] = dict(  # noqa: C408 - kwargs mirror the model signature
        operation_id=OperationId(uuid("operation")),
        kind=ApplicationOperationKind.INVESTIGATE,
        namespace=NAMESPACE,
        actor_id_hash=digest("actor"),
        case_id=CASE,
        request_hash=digest("request"),
        status=ApplicationOperationStatus.PENDING,
        result_refs=(),
        error_code=None,
        expires_at_epoch=1_900_000_000,
        version=1,
        created_at=NOW,
        updated_at=NOW,
        agent_invocation_id=uuid("invocation"),
        agent_binding_hash=digest("binding"),
    )
    base.update(overrides)
    return ApplicationOperation(**base)  # type: ignore[arg-type]


def test_an_operation_round_trips_under_the_v2_attribute_names() -> None:
    item = codec_core.encode_operation(NAMESPACE_SCOPE, operation())
    assert item["schema_version"] == codec_core.OPERATION_SCHEMA_VERSION_V2
    assert "agent_invocation_id" in item
    assert "monitor_invocation_id" not in item
    _, decoded = codec_core.decode_operation(item)
    assert decoded.agent_invocation_id == uuid("invocation")
    assert decoded.agent_binding_hash == digest("binding")


def test_a_v1_row_decodes_into_the_generalized_fields_and_rewrites_as_v2() -> None:
    """No stored value changes meaning; only the attribute names moved."""

    v2 = codec_core.encode_operation(NAMESPACE_SCOPE, operation())
    v1 = {
        key: value
        for key, value in v2.items()
        if key not in {"agent_invocation_id", "agent_binding_hash"}
    }
    v1["schema_version"] = codec_core.OPERATION_SCHEMA_VERSION_V1
    v1["monitor_invocation_id"] = str(uuid("invocation"))
    v1["monitor_locator_hash"] = digest("binding").value

    _, decoded = codec_core.decode_operation(v1)

    assert decoded.agent_invocation_id == uuid("invocation")
    assert decoded.agent_binding_hash == digest("binding")
    rewritten = codec_core.encode_operation(NAMESPACE_SCOPE, decoded)
    assert rewritten["schema_version"] == codec_core.OPERATION_SCHEMA_VERSION_V2
    assert rewritten["agent_invocation_id"] == str(uuid("invocation"))


@pytest.mark.parametrize(
    "kind",
    [ApplicationOperationKind.SEND_ACTION, ApplicationOperationKind.DEMO_DUE],
)
def test_a_non_agent_kind_carrying_a_handover_is_refused_at_construction(
    kind: ApplicationOperationKind,
) -> None:
    """The null-together rule cannot decay into a convention nobody checks."""

    with pytest.raises(ValueError):
        operation(kind=kind)


@pytest.mark.parametrize(
    "kind",
    [
        ApplicationOperationKind.MONITOR,
        ApplicationOperationKind.INVESTIGATE,
        ApplicationOperationKind.PROPOSE_ACTION,
    ],
)
def test_every_agent_invoking_kind_may_carry_a_handover(
    kind: ApplicationOperationKind,
) -> None:
    assert operation(kind=kind).agent_invocation_id is not None


def test_an_operation_is_never_half_bound() -> None:
    with pytest.raises(ValueError):
        operation(agent_binding_hash=None)
    with pytest.raises(ValueError):
        operation(agent_invocation_id=None)
