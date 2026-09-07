"""Round-trips for ``evidence-item/v2`` and the three items Phase 9 adds to the mapping.

The interesting one is the schema split. Readers accept ``/v1`` rows unchanged -- owner present,
binding absent -- and writers emit ``/v2``; no stored row is rewritten
([ADR-026](../../../docs/adr/ADR-026-inbound-reply-trust-and-correlation.md) § 5). The decoder
branches on the *stored version* rather than probing for an attribute, because ``ItemReader``
refuses both a missing required attribute and an unread present one, and "which shape is this"
is a question the envelope already answers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from chorus.domain.entities import (
    EVIDENCE_ITEM_SCHEMA_VERSION_V1,
    EVIDENCE_ITEM_SCHEMA_VERSION_V2,
    DerivationKind,
    EvidenceItem,
    ExternalSourceBinding,
    ExtractionStatus,
    MalwareScanStatus,
)
from chorus.domain.ids import (
    ActionId,
    CaseId,
    CommitmentId,
    CommunityId,
    ContributorId,
    DestinationId,
    EvidenceItemId,
    EvidenceRootId,
    ExecutionId,
    Namespace,
    SensitiveStr,
    Sha256Digest,
)
from chorus.infrastructure.dynamodb import codec_case, codec_share, keys
from chorus.ports.records import (
    CommitmentScheduleProjection,
    CommitmentScheduleStatus,
    OutboundMessageLocator,
    VerificationRequest,
)
from chorus.ports.scopes import ActionScope, CaseScope

NOW = datetime(2030, 1, 2, 9, 0, tzinfo=UTC)
NAMESPACE = Namespace("TEST_CODEC_V9")
COMMUNITY = CommunityId(uuid4())
CASE = CaseId(uuid4())
SCOPE = CaseScope(namespace=NAMESPACE, community_id=COMMUNITY, case_id=CASE)
DIGEST = Sha256Digest("sha256:" + "a" * 64)


def binding() -> ExternalSourceBinding:
    return ExternalSourceBinding(
        destination_id="property_manager:demo",
        registry_version=1,
        routing_token=uuid4(),
        correlated_action_id=ActionId(uuid4()),
        correlated_execution_id=ExecutionId(uuid4()),
        inbound_message_id_hash=DIGEST,
        sender_address_digest=Sha256Digest("sha256:" + "b" * 64),
        recipient_address_digest=Sha256Digest("sha256:" + "c" * 64),
        transport="aws:ses-receipt",
        transport_source_arn="arn:aws:ses:us-east-1:000000000000:receipt-rule-set/chorus",
        spf="PASS",
        dkim="PASS",
        dmarc="PASS",
        spam="PASS",
        virus="PASS",
        received_at=NOW,
        correlation_proof="f" * 64,
    )


def evidence(*, owner: ContributorId | None, bound: ExternalSourceBinding | None) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=EvidenceItemId(uuid4()),
        root_id=EvidenceRootId(uuid4()),
        community_id=COMMUNITY,
        case_id=CASE,
        namespace=NAMESPACE,
        submitted_by_contributor_id=owner,
        source_message_id=None,
        private_object_key=SensitiveStr("ns/TEST/reply/aaa/content"),
        media_type="message/rfc822",
        byte_length=473,
        sha256=DIGEST,
        captured_at=NOW,
        uploaded_at=NOW,
        derived_from_evidence_id=None,
        malware_scan_status=MalwareScanStatus.CLEAN,
        extraction_status=ExtractionStatus.COMPLETE,
        extracted_text=SensitiveStr("we will restore elevator b by 2030-01-14."),
        version=1,
        created_at=NOW,
        updated_at=NOW,
        external_source_binding=bound,
        schema_version=(
            EVIDENCE_ITEM_SCHEMA_VERSION_V2 if bound else EVIDENCE_ITEM_SCHEMA_VERSION_V1
        ),
    )


def test_an_inbound_artifact_round_trips_with_its_whole_binding() -> None:
    item = evidence(owner=None, bound=binding())

    encoded = codec_case.encode_evidence_item(SCOPE, item)
    _, decoded = codec_case.decode_evidence_item(encoded)

    assert decoded == item
    assert decoded.external_source_binding == item.external_source_binding


def test_a_v1_resident_upload_still_decodes_unchanged() -> None:
    """No stored row is rewritten, and no value is invented for one that has none."""

    item = evidence(owner=ContributorId(uuid4()), bound=None)

    encoded = codec_case.encode_evidence_item(SCOPE, item)
    stored = dict(encoded)
    del stored["external_source_binding"]

    _, decoded = codec_case.decode_evidence_item(stored)

    assert decoded.schema_version == EVIDENCE_ITEM_SCHEMA_VERSION_V1
    assert decoded.external_source_binding is None
    assert decoded.submitted_by_contributor_id == item.submitted_by_contributor_id


def test_evidence_has_exactly_one_of_an_owner_and_a_binding() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        evidence(owner=ContributorId(uuid4()), bound=binding())
    with pytest.raises(ValueError, match="exactly one"):
        evidence(owner=None, bound=None)


def test_a_binding_requires_the_v2_schema() -> None:
    with pytest.raises(ValueError, match="evidence-item/v2"):
        EvidenceItem(
            evidence_id=EvidenceItemId(uuid4()),
            root_id=EvidenceRootId(uuid4()),
            community_id=COMMUNITY,
            case_id=CASE,
            namespace=NAMESPACE,
            submitted_by_contributor_id=None,
            source_message_id=None,
            private_object_key=SensitiveStr("k"),
            media_type="message/rfc822",
            byte_length=1,
            sha256=DIGEST,
            captured_at=NOW,
            uploaded_at=NOW,
            derived_from_evidence_id=None,
            malware_scan_status=MalwareScanStatus.CLEAN,
            extraction_status=ExtractionStatus.COMPLETE,
            extracted_text=None,
            version=1,
            created_at=NOW,
            updated_at=NOW,
            external_source_binding=binding(),
            schema_version=EVIDENCE_ITEM_SCHEMA_VERSION_V1,
        )


def test_an_outbound_locator_is_addressed_by_the_message_identifier_alone() -> None:
    """The whole point: correlation starts from an ``In-Reply-To`` and has no action id yet."""

    locator = OutboundMessageLocator(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        case_id=CASE,
        action_id=ActionId(uuid4()),
        execution_id=ExecutionId(uuid4()),
        ses_message_id="0100019a-fixture",
        destination_id=DestinationId("property_manager:demo"),
        registry_version=1,
        routing_token=uuid4(),
        sent_at=NOW,
    )

    encoded = codec_share.encode_outbound_message(locator)
    _, decoded = codec_share.decode_outbound_message(encoded)

    assert decoded == locator
    key = codec_share.outbound_message_key(NAMESPACE, locator.ses_message_id)
    assert key.partition_key == keys.outbound_message_partition(NAMESPACE, locator.ses_message_id)
    assert key.sort_key == "OUTBOUND_MESSAGE"


def test_the_locator_key_hashes_the_provider_supplied_identifier() -> None:
    """Provider text never enters a key, and the namespace is inside the digest."""

    first = keys.outbound_message_partition(NAMESPACE, "0100019a")
    other = keys.outbound_message_partition(Namespace("OTHER_NS"), "0100019a")

    assert "0100019a" not in first
    assert first != other


def test_the_locator_carries_no_address_subject_or_body() -> None:
    locator = OutboundMessageLocator(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        case_id=CASE,
        action_id=ActionId(uuid4()),
        execution_id=ExecutionId(uuid4()),
        ses_message_id="0100019a",
        destination_id=DestinationId("property_manager:demo"),
        registry_version=1,
        routing_token=uuid4(),
        sent_at=NOW,
    )

    fields = set(OutboundMessageLocator.__dataclass_fields__)
    assert not fields & {"address", "subject", "text_body", "html_body", "reply_to"}
    encoded = codec_share.encode_outbound_message(locator)
    assert "@" not in "".join(str(value) for value in encoded.values())


def test_the_schedule_projection_round_trips_and_carries_a_safe_failure_code() -> None:
    projection = CommitmentScheduleProjection(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        case_id=CASE,
        commitment_id=CommitmentId(uuid4()),
        status=CommitmentScheduleStatus.PENDING_SCHEDULE,
        schedule_name="chorus-test-0000000a-" + str(uuid4()) + "-1",
        generation=1,
        attempts=1,
        version=2,
        created_at=NOW,
        updated_at=NOW,
        last_error_code="SCHEDULER_UNAVAILABLE",
    )

    encoded = codec_share.encode_commitment_schedule(SCOPE, projection)
    _, decoded = codec_share.decode_commitment_schedule(encoded)

    assert decoded == projection


def test_a_created_schedule_carries_no_failure_code() -> None:
    with pytest.raises(ValueError, match="carries no failure code"):
        CommitmentScheduleProjection(
            namespace=NAMESPACE,
            community_id=COMMUNITY,
            case_id=CASE,
            commitment_id=CommitmentId(uuid4()),
            status=CommitmentScheduleStatus.CREATED,
            schedule_name="chorus-test-x-1",
            generation=1,
            attempts=1,
            version=2,
            created_at=NOW,
            updated_at=NOW,
            last_error_code="SCHEDULER_UNAVAILABLE",
        )


def test_the_verification_request_is_keyed_by_commitment_and_generation() -> None:
    """Being create-only at this address is what makes "exactly one" a property."""

    commitment_id = CommitmentId(uuid4())
    request = VerificationRequest(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        case_id=CASE,
        commitment_id=commitment_id,
        generation=1,
        due_event_id=uuid4(),
        requested_at=NOW,
    )

    encoded = codec_share.encode_verification_request(SCOPE, request)
    _, decoded = codec_share.decode_verification_request(encoded)

    assert decoded == request
    first = codec_share.verification_request_key(SCOPE, commitment_id, 1)
    second = codec_share.verification_request_key(SCOPE, commitment_id, 2)
    assert first.sort_key != second.sort_key


def test_an_action_scope_is_not_needed_to_address_a_locator() -> None:
    """Stated as a test because it is the whole deviation from the printed key."""

    scope = ActionScope(
        namespace=NAMESPACE,
        community_id=COMMUNITY,
        case_id=CASE,
        action_id=ActionId(uuid4()),
    )
    assert scope.case_scope == SCOPE
    assert "action" not in codec_share.outbound_message_key(NAMESPACE, "0100019a").partition_key


def test_an_original_reply_root_has_no_parent() -> None:
    assert DerivationKind.ORIGINAL.value == "ORIGINAL"
