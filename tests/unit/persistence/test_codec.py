"""Category B: strict serialization round-trips and fail-closed deserialization."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from tests.fixtures.persistence import DEMO_RETENTION, NOW, PRIMARY, World, digest

from chorus.domain.entities import ActionExecutionState, MandateStatus
from chorus.domain.errors import IntegrityError
from chorus.infrastructure.dynamodb import (
    codec,
    codec_audit,
    codec_case,
    codec_core,
    codec_fence,
    codec_idempotency,
    codec_mandate,
    codec_share,
)
from chorus.infrastructure.dynamodb.attributes import decode_item, encode_item
from chorus.infrastructure.dynamodb.codec import (
    ATTR_ENTITY_TYPE,
    ATTR_EXPIRES_AT_EPOCH,
    ATTR_NAMESPACE,
    ATTR_SCHEMA_VERSION,
)
from chorus.ports.idempotency import (
    IdempotencyRecord,
    IdempotencyStatus,
)
from chorus.ports.limits import AUDIT_TTL_SECONDS, ORDINARY_IDEMPOTENCY_TTL_SECONDS
from chorus.ports.storage import StoredItem, TableName

type Case = tuple[str, Callable[[World], StoredItem], Callable[[StoredItem], Any], object]


def _cases() -> tuple[Case, ...]:
    """Every persisted shape, its encoder, its decoder, and the value it must reproduce."""

    world = PRIMARY
    case_scope = world.case_scope
    community_scope = world.community_scope
    namespace_scope = world.namespace_scope
    operation_scope = world.operation_scope
    action_scope = world.action_scope
    idempotency_key = world.idempotency_key()
    record = IdempotencyRecord(
        key=idempotency_key,
        request_hash=digest("request"),
        status=IdempotencyStatus.COMPLETED,
        result_entity_refs=(),
        response_status=200,
        created_at=NOW,
        updated_at=NOW,
        expires_at_epoch=int(NOW.timestamp()) + ORDINARY_IDEMPOTENCY_TTL_SECONDS,
        version=1,
    )
    return (
        (
            "COMMUNITY",
            lambda w: codec_core.encode_community(namespace_scope, w.community()),
            codec_core.decode_community,
            world.community(),
        ),
        (
            "CONTRIBUTOR",
            lambda w: codec_core.encode_contributor(community_scope, w.contributor()),
            codec_core.decode_contributor,
            world.contributor(),
        ),
        (
            "COMMUNITY_MESSAGE",
            lambda w: codec_core.encode_message(community_scope, w.message()),
            codec_core.decode_message,
            world.message(),
        ),
        (
            "CHANNEL_UNIQUENESS_LOCK",
            lambda w: codec_core.encode_channel_lock(community_scope, w.channel_lock()),
            codec_core.decode_channel_lock,
            world.channel_lock(),
        ),
        (
            "FEED_SIGNAL_PROJECTION",
            lambda w: codec_core.encode_feed_signal(community_scope, w.feed_signal()),
            codec_core.decode_feed_signal,
            world.feed_signal(),
        ),
        (
            "MONITOR_APPLY_PROGRESS",
            lambda w: codec_core.encode_monitor_progress(operation_scope, w.monitor_progress()),
            codec_core.decode_monitor_progress,
            world.monitor_progress(),
        ),
        (
            "MONITOR_SNAPSHOT_MANIFEST",
            lambda w: codec_core.encode_monitor_snapshot_manifest(
                operation_scope, w.monitor_snapshot_manifest()
            ),
            codec_core.decode_monitor_snapshot_manifest,
            world.monitor_snapshot_manifest(),
        ),
        (
            "MONITOR_SNAPSHOT_CHUNK",
            lambda w: codec_core.encode_monitor_snapshot_chunk(
                operation_scope, w.monitor_snapshot_chunk()
            ),
            codec_core.decode_monitor_snapshot_chunk,
            world.monitor_snapshot_chunk(),
        ),
        (
            "APPLICATION_OPERATION",
            lambda w: codec_core.encode_operation(namespace_scope, w.operation()),
            codec_core.decode_operation,
            world.operation(),
        ),
        (
            "EVIDENCE_ROOT",
            lambda w: codec_core.encode_evidence_root(community_scope, w.evidence_root()),
            codec_core.decode_evidence_root,
            world.evidence_root(),
        ),
        (
            "COMMUNITY_CASE",
            lambda w: codec_core.encode_case(case_scope, w.case()),
            codec_core.decode_case,
            world.case(),
        ),
        (
            "REPORT",
            lambda w: codec_case.encode_report(case_scope, w.report()),
            codec_case.decode_report,
            world.report(),
        ),
        (
            "FACT",
            lambda w: codec_case.encode_fact(case_scope, w.fact()),
            codec_case.decode_fact,
            world.fact(),
        ),
        (
            "EVIDENCE_ITEM",
            lambda w: codec_case.encode_evidence_item(case_scope, w.evidence_item()),
            codec_case.decode_evidence_item,
            world.evidence_item(),
        ),
        (
            "INVESTIGATION_ASSESSMENT",
            lambda w: codec_case.encode_assessment(case_scope, w.assessment()),
            codec_case.decode_assessment,
            world.assessment(),
        ),
        (
            "DISCLOSURE_MANDATE",
            lambda w: codec_mandate.encode_mandate(case_scope, w.mandate()),
            codec_mandate.decode_mandate,
            world.mandate(),
        ),
        (
            "CURRENT_MANDATE_POINTER",
            lambda w: codec_mandate.encode_mandate_pointer(case_scope, w.mandate_pointer()),
            codec_mandate.decode_mandate_pointer,
            world.mandate_pointer(),
        ),
        (
            "FACT_MANDATE_ASSOCIATION",
            lambda w: codec_mandate.encode_fact_mandate(case_scope, w.fact_mandate()),
            codec_mandate.decode_fact_mandate,
            world.fact_mandate(),
        ),
        (
            "AGENT_INVOCATION_RESULT",
            lambda w: codec_fence.encode_agent_invocation(case_scope, w.agent_invocation()),
            codec_fence.decode_agent_invocation,
            world.agent_invocation(),
        ),
        (
            "SEND_FENCE",
            lambda w: codec_fence.encode_send_fence(case_scope, w.send_fence()),
            codec_fence.decode_send_fence,
            world.send_fence(),
        ),
        (
            "IDEMPOTENCY_RECORD",
            lambda _: codec_idempotency.encode_idempotency(record, table=TableName.CORE),
            codec_idempotency.decode_idempotency,
            record,
        ),
        (
            "SHAREABLE_VIEW",
            lambda w: codec_share.encode_view(case_scope, w.view()),
            codec_share.decode_view,
            world.view(),
        ),
        (
            "CURRENT_VIEW_POINTER",
            lambda w: codec_share.encode_view_pointer(case_scope, w.view_pointer()),
            codec_share.decode_view_pointer,
            world.view_pointer(),
        ),
        (
            "VIEW_HISTORY_LOCATOR",
            lambda w: codec_share.encode_view_history(case_scope, w.view_history()),
            codec_share.decode_view_history,
            world.view_history(),
        ),
        (
            "ACTION_PROPOSAL",
            lambda w: codec_share.encode_proposal(action_scope, w.proposal()),
            codec_share.decode_proposal,
            world.proposal(),
        ),
        (
            "APPROVAL",
            lambda w: codec_share.encode_approval(action_scope, w.approval()),
            codec_share.decode_approval,
            world.approval(),
        ),
        (
            "ACTION_EXECUTION",
            lambda w: codec_share.encode_execution(action_scope, w.execution()),
            codec_share.decode_execution,
            world.execution(),
        ),
        (
            "CURRENT_ACTION_POINTER",
            lambda w: codec_share.encode_action_pointer(case_scope, w.action_pointer()),
            codec_share.decode_action_pointer,
            world.action_pointer(),
        ),
        (
            "ACTION_HISTORY_LOCATOR",
            lambda w: codec_share.encode_action_history(case_scope, w.action_history()),
            codec_share.decode_action_history,
            world.action_history(),
        ),
        (
            "COMMITMENT",
            lambda w: codec_share.encode_commitment(case_scope, w.commitment()),
            codec_share.decode_commitment,
            world.commitment(),
        ),
        (
            "AUDIT_EVENT",
            lambda w: codec_audit.encode_case_event(
                case_scope, w.audit_event(), retention=DEMO_RETENTION
            ),
            codec_audit.decode_audit_event,
            world.audit_event(),
        ),
    )


CASES = _cases()
CASE_IDS = tuple(case[0] for case in CASES)


def test_every_entity_type_has_a_round_trip_case() -> None:
    """A new persisted shape cannot be added without a round-trip test."""

    assert set(CASE_IDS) == {member.value for member in codec.EntityType}


@pytest.mark.parametrize(("name", "encode", "decode", "expected"), CASES, ids=CASE_IDS)
def test_round_trip_preserves_the_entity_exactly(
    name: str,
    encode: Callable[[World], StoredItem],
    decode: Callable[[StoredItem], Any],
    expected: object,
) -> None:
    item = encode(PRIMARY)

    _, decoded = decode(item)

    assert decoded == expected


@pytest.mark.parametrize(("name", "encode", "decode", "expected"), CASES, ids=CASE_IDS)
def test_round_trip_survives_the_dynamodb_attribute_encoding(
    name: str,
    encode: Callable[[World], StoredItem],
    decode: Callable[[StoredItem], Any],
    expected: object,
) -> None:
    item = encode(PRIMARY)

    _, decoded = decode(decode_item(encode_item(item)))

    assert decoded == expected


@pytest.mark.parametrize(("name", "encode", "decode", "expected"), CASES, ids=CASE_IDS)
def test_an_unexpected_attribute_fails_closed(
    name: str,
    encode: Callable[[World], StoredItem],
    decode: Callable[[StoredItem], Any],
    expected: object,
) -> None:
    """Exact key sets: an injected attribute is never silently ignored."""

    item = dict(encode(PRIMARY))
    item["injected_attribute"] = "smuggled"

    with pytest.raises(IntegrityError):
        decode(item)


@pytest.mark.parametrize(("name", "encode", "decode", "expected"), CASES, ids=CASE_IDS)
def test_a_missing_attribute_fails_closed(
    name: str,
    encode: Callable[[World], StoredItem],
    decode: Callable[[StoredItem], Any],
    expected: object,
) -> None:
    item = dict(encode(PRIMARY))
    # ``expires_at_epoch`` on an audit item is written only by a deployment whose retention
    # policy expires audit events, so a durable environment legitimately omits it. Its own
    # presence/absence contract is asserted in the audit retention tests.
    optional_by_policy = {ATTR_EXPIRES_AT_EPOCH} if name == "AUDIT_EVENT" else set()
    removable = [key for key in item if key not in {"PK", "SK"} | optional_by_policy]
    for attribute in removable:
        partial = {key: value for key, value in item.items() if key != attribute}
        with pytest.raises(IntegrityError):
            decode(partial)


@pytest.mark.parametrize(("name", "encode", "decode", "expected"), CASES, ids=CASE_IDS)
def test_a_wrong_discriminator_fails_closed(
    name: str,
    encode: Callable[[World], StoredItem],
    decode: Callable[[StoredItem], Any],
    expected: object,
) -> None:
    item = dict(encode(PRIMARY))
    item[ATTR_ENTITY_TYPE] = "COMMITMENT" if name != "COMMITMENT" else "FACT"

    with pytest.raises(IntegrityError):
        decode(item)


@pytest.mark.parametrize(("name", "encode", "decode", "expected"), CASES, ids=CASE_IDS)
def test_an_unknown_schema_version_fails_closed(
    name: str,
    encode: Callable[[World], StoredItem],
    decode: Callable[[StoredItem], Any],
    expected: object,
) -> None:
    item = dict(encode(PRIMARY))
    item[ATTR_SCHEMA_VERSION] = "something/v99"

    with pytest.raises(IntegrityError):
        decode(item)


@pytest.mark.parametrize(("name", "encode", "decode", "expected"), CASES, ids=CASE_IDS)
def test_an_invalid_namespace_fails_closed(
    name: str,
    encode: Callable[[World], StoredItem],
    decode: Callable[[StoredItem], Any],
    expected: object,
) -> None:
    item = dict(encode(PRIMARY))
    # Lowercase is outside the frozen namespace grammar, so the envelope must refuse the item
    # rather than hand a malformed isolation identifier to the scope comparison above it.
    item[ATTR_NAMESPACE] = "production"

    with pytest.raises(IntegrityError):
        decode(item)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("version", "1"),
        ("version", True),
        ("created_at", "2026-08-31T12:00:00Z"),
        ("created_at", "2026-08-31 12:00:00.000000Z"),
        ("content_sha256", "sha256:NOTHEX"),
        ("content_sha256", "0" * 64),
        ("processing_status", "SOMETHING_ELSE"),
        ("message_id", "not-a-uuid"),
    ],
)
def test_out_of_domain_values_fail_closed(attribute: str, value: object) -> None:
    item = dict(codec_core.encode_message(PRIMARY.community_scope, PRIMARY.message()))
    assert attribute in item
    item[attribute] = value  # type: ignore[assignment]

    with pytest.raises(IntegrityError):
        codec_core.decode_message(item)


def test_a_tampered_audit_expiry_fails_closed() -> None:
    event = PRIMARY.audit_event()
    item = dict(codec_audit.encode_case_event(PRIMARY.case_scope, event, retention=DEMO_RETENTION))
    assert item["expires_at_epoch"] == int(event.occurred_at.timestamp()) + AUDIT_TTL_SECONDS
    item["expires_at_epoch"] = 0

    with pytest.raises(IntegrityError):
        codec_audit.decode_audit_event(item)


def test_a_tampered_fence_expiry_fails_closed() -> None:
    fence = PRIMARY.send_fence()
    item = dict(codec_fence.encode_send_fence(PRIMARY.case_scope, fence))
    item["expires_at_epoch"] = int(fence.expires_at_epoch) + 3600

    with pytest.raises(IntegrityError):
        codec_fence.decode_send_fence(item)


def test_stored_keys_are_the_keys_the_builders_produce() -> None:
    """A stored PK/SK pair always matches the typed key builder for that entity."""

    world = PRIMARY
    key = codec_case.fact_key(world.case_scope, world.fact_id)
    item = codec_case.encode_fact(world.case_scope, world.fact())

    assert item["PK"] == key.partition_key
    assert item["SK"] == key.sort_key


def test_private_text_is_stored_only_where_the_mapping_says_so() -> None:
    """Private strings live in the Core table; no Shareable item carries one."""

    private = PRIMARY.message().raw_text.reveal()
    core_item = codec_core.encode_message(PRIMARY.community_scope, PRIMARY.message())
    assert private in core_item.values()

    view_item = codec_share.encode_view(PRIMARY.case_scope, PRIMARY.view())
    assert private not in repr(view_item)
    assert str(PRIMARY.contributor_id) not in repr(view_item)


def test_mandate_status_transitions_survive_serialization() -> None:
    revoked = PRIMARY.mandate(status=MandateStatus.REVOKED)
    item = codec_mandate.encode_mandate(PRIMARY.case_scope, revoked)

    _, decoded = codec_mandate.decode_mandate(item)

    assert decoded.status is MandateStatus.REVOKED
    assert decoded.revoked_at == revoked.revoked_at


def test_execution_states_survive_serialization() -> None:
    for state in ActionExecutionState:
        execution = PRIMARY.execution(state=state)
        item = codec_share.encode_execution(PRIMARY.action_scope, execution)

        _, decoded = codec_share.decode_execution(item)

        assert decoded.state is state


# ``int`` accepts this one and the canonical grammar does not, which is the whole point.
ARABIC_INDIC_FIVE = chr(0x0665)


@pytest.mark.parametrize(
    "spelling",
    ["1_0", "+5", " 5", "5 ", "0005", "-0", "1.5", "1e3", "0x10", "", ARABIC_INDIC_FIVE],
    ids=[
        "underscore",
        "leading-plus",
        "leading-space",
        "trailing-space",
        "leading-zeros",
        "negative-zero",
        "decimal",
        "exponent",
        "hexadecimal",
        "empty",
        "non-ascii-digit",
    ],
)
def test_a_non_canonical_number_fails_closed(spelling: str) -> None:
    """Only the spelling this codec writes is read back.

    ``int`` accepts far more than ``str(int)`` produces, so two stored spellings could
    otherwise decode to the same authorization value -- a version, a fence deadline, or a
    contributor count. DynamoDB returns exactly what was written, so nothing legitimate is
    lost by refusing everything else.
    """

    with pytest.raises(IntegrityError):
        decode_item({"n": {"N": spelling}})


@pytest.mark.parametrize("value", [0, 1, -1, 10**18, -(10**18), AUDIT_TTL_SECONDS])
def test_every_number_this_codec_writes_round_trips(value: int) -> None:
    assert decode_item(encode_item({"n": value})) == {"n": value}
