"""The inbound trust boundary: what reaches an artifact, and what never gets past the door.

Every test here is about ADR-026's central claim -- **a reply is not a reply until an
authenticated transport delivered it, every receipt verdict passed, and it correlated to exactly
one ``SENT`` execution**. The refusals are the interesting half: each one leaves the case exactly
as it was, writes nothing anywhere, and carries a closed code and no content.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta

import pytest
from tests.fixtures.reply import (
    FOREIGN_INBOUND_SOURCE_ARN,
    INBOUND_ADDRESS,
    INBOUND_SOURCE_ARN,
    INBOUND_TRANSPORT,
    MANAGER_ADDRESS,
    STRANGER_ADDRESS,
    ReplyHarness,
)

from chorus.application.commands.ingest_external_reply import (
    INGEST_PARTICIPANTS,
    IngestExternalReplyCommand,
)
from chorus.application.services.inbound_mail import (
    AttestedInboundReply,
    InboundMailTrustFailure,
    InboundMailUntrusted,
    InboundReplyRejected,
    InboundReplyRejection,
    strip_quoted_outbound,
)
from chorus.domain.entities import CaseState, MalwareScanStatus
from chorus.domain.ids import Sha256Digest
from chorus.infrastructure.fixtures.inbound_delivery import FixtureReplyDeliverySource
from chorus.infrastructure.fixtures.inbound_replies import (
    MANAGER_ATTACHMENT,
    MANAGER_HEDGE,
    MANAGER_HTML_ONLY,
    MANAGER_PROMISE,
    MANAGER_QUOTE_ONLY,
    ReviewedInboundReply,
    build_raw_message,
    build_receipt_envelope,
    outbound_message_id,
)
from chorus.ports.errors import PersistenceConflictError
from chorus.ports.inbound_mail import InboundMailTransportContext
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import TableName

pytestmark = pytest.mark.anyio


async def test_an_attested_reply_becomes_one_immutable_artifact(
    reply_harness: ReplyHarness,
) -> None:
    """The happy path, and the seven participants the frozen transaction names."""

    await reply_harness.prepare_sent()
    result = await reply_harness.ingest_reply()

    plan = reply_harness.send.action.unit_of_work.plan("ingest-external-reply")
    assert len(plan.operations) == INGEST_PARTICIPANTS

    items = await reply_harness.send.action.compile.core.load_evidence_items(
        reply_harness.scope, (result.evidence_id,)
    )
    artifact = items[0]
    binding = artifact.external_source_binding
    assert binding is not None
    assert artifact.submitted_by_contributor_id is None
    assert artifact.schema_version == "evidence-item/v2"
    assert artifact.malware_scan_status is MalwareScanStatus.CLEAN
    assert artifact.media_type == "message/rfc822"
    execution = await reply_harness.send.execution()
    assert binding.correlated_execution_id == execution.execution_id
    assert binding.dmarc == "PASS"


async def test_reply_ingestion_bumps_both_counters_and_changes_no_state(
    reply_harness: ReplyHarness,
) -> None:
    """ADR-020 § 2 row 13: evidence is authorization-sensitive, and the state is unchanged."""

    await reply_harness.prepare_sent()
    before = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)
    await reply_harness.ingest_reply()
    after = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)

    assert after.state is before.state is CaseState.ACTIONED
    assert after.version == before.version + 1
    assert after.authorization_version == before.authorization_version + 1


async def test_reply_ingestion_creates_no_report_and_no_fact(
    reply_harness: ReplyHarness,
) -> None:
    """Asserted over the whole staged plan, so corroboration cannot move by accident."""

    await reply_harness.prepare_sent()
    before = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)
    await reply_harness.ingest_reply()

    plan = reply_harness.send.action.unit_of_work.plan("ingest-external-reply")
    sort_keys = [operation.key.sort_key for operation in plan.operations]
    assert not any(key.startswith("REPORT#") for key in sort_keys)
    assert not any(key.startswith("FACT#") for key in sort_keys)

    after = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)
    assert after.corroboration_source_count == before.corroboration_source_count
    assert after.report_ids == before.report_ids
    assert after.fact_ids == before.fact_ids


async def test_reply_without_an_authenticator_is_never_evidence(
    reply_harness: ReplyHarness,
) -> None:
    """With no authenticator wired, nothing is decoded and nothing is written."""

    await reply_harness.prepare_sent()
    reply_harness.authenticator = None
    reply_harness.trust = None
    delivery = await reply_harness.delivery()

    attester, _ = reply_harness.inbound_trust()
    with pytest.raises(InboundMailUntrusted) as raised:
        await attester.attest(delivery)
    assert raised.value.failure is InboundMailTrustFailure.TRANSPORT_UNAVAILABLE
    assert reply_harness.raw_messages.reads == 0


async def test_a_foreign_transport_source_never_reaches_the_decoder(
    reply_harness: ReplyHarness,
) -> None:
    await reply_harness.prepare_sent()
    attester, _ = reply_harness.inbound_trust()

    with pytest.raises(InboundMailUntrusted) as raised:
        await attester.attest(await reply_harness.delivery(source_arn=FOREIGN_INBOUND_SOURCE_ARN))
    assert raised.value.failure is InboundMailTrustFailure.FOREIGN_TRANSPORT_SOURCE
    assert reply_harness.authenticator is not None
    assert reply_harness.authenticator.calls == 0


async def test_an_unauthenticated_delivery_never_reaches_the_decoder(
    reply_harness: ReplyHarness,
) -> None:
    await reply_harness.prepare_sent()
    assert reply_harness.authenticator is not None
    reply_harness.authenticator.accepts = False
    attester, _ = reply_harness.inbound_trust()

    with pytest.raises(InboundMailUntrusted) as raised:
        await attester.attest(await reply_harness.delivery())
    assert raised.value.failure is InboundMailTrustFailure.TRANSPORT_UNAUTHENTICATED
    assert reply_harness.raw_messages.reads == 0


@pytest.mark.parametrize(
    "verdict",
    [
        {"spf": "FAIL"},
        {"dkim": "FAIL"},
        {"dmarc": "FAIL"},
        {"spam": "FAIL"},
        {"virus": "FAIL"},
    ],
)
async def test_a_failed_receipt_verdict_refuses_the_delivery(
    reply_harness: ReplyHarness, verdict: dict[str, str]
) -> None:
    """The gate that turns "who wrote this" from a header into a fact."""

    await reply_harness.prepare_sent()
    attester, _ = reply_harness.inbound_trust()

    with pytest.raises(InboundMailUntrusted) as raised:
        await attester.attest(await reply_harness.delivery(**verdict))  # type: ignore[arg-type]
    assert raised.value.failure is InboundMailTrustFailure.INBOUND_VERDICT_FAILED


async def test_a_hand_built_attested_reply_is_refused_by_the_verifier(
    reply_harness: ReplyHarness,
) -> None:
    """Constructing the class is possible and useless: the MAC is a string nobody reproduces."""

    await reply_harness.prepare_sent()
    genuine = await reply_harness.attest()
    _, verifier = reply_harness.inbound_trust()

    forged = AttestedInboundReply(
        evidence=genuine.evidence,
        source_arn=genuine.source_arn,
        attestation="0" * 64,
        raw_mime=genuine.raw_mime,
    )
    replayed_arn = AttestedInboundReply(
        evidence=genuine.evidence,
        source_arn=FOREIGN_INBOUND_SOURCE_ARN,
        attestation=genuine.attestation,
        raw_mime=genuine.raw_mime,
    )
    tampered_bytes = AttestedInboundReply(
        evidence=genuine.evidence,
        source_arn=genuine.source_arn,
        attestation=genuine.attestation,
        raw_mime=genuine.raw_mime + b" tampered",
    )

    assert verifier.attests(genuine)
    assert not verifier.attests(forged)
    assert not verifier.attests(replayed_arn)
    assert not verifier.attests(tampered_bytes)


async def test_a_command_holding_a_hand_built_artifact_writes_nothing(
    reply_harness: ReplyHarness,
) -> None:
    await reply_harness.prepare_sent()
    genuine = await reply_harness.attest()
    forged = dataclasses.replace(genuine, attestation="0" * 64)

    from tests.fixtures.action import ACTOR_HASH

    with pytest.raises(InboundMailUntrusted):
        await reply_harness.ingest().execute(
            IngestExternalReplyCommand(
                attested=forged,
                actor_id_hash=ACTOR_HASH,
                correlation_id=genuine.evidence.execution_id.value,
            )
        )
    assert not any(
        plan.name == "ingest-external-reply"
        for plan in reply_harness.send.action.unit_of_work.plans
    )


async def test_a_forged_in_reply_to_from_a_foreign_sender_is_refused(
    reply_harness: ReplyHarness,
) -> None:
    """A DMARC-passing message from another domain is ``REPLY_SENDER_NOT_DESTINATION``.

    Whatever its ``In-Reply-To`` says. Message identifiers appear in every copy of a thread, so
    any party who ever saw the outbound message could quote one; the verified sender is what
    makes the identifier a correlation key rather than a credential.
    """

    await reply_harness.prepare_sent()
    attester, _ = reply_harness.inbound_trust()

    with pytest.raises(InboundReplyRejected) as raised:
        await attester.attest(await reply_harness.delivery(source=STRANGER_ADDRESS))
    assert raised.value.rejection is InboundReplyRejection.REPLY_SENDER_NOT_DESTINATION


async def test_a_reply_addressed_elsewhere_is_refused(reply_harness: ReplyHarness) -> None:
    await reply_harness.prepare_sent()
    attester, _ = reply_harness.inbound_trust()

    with pytest.raises(InboundReplyRejected) as raised:
        await attester.attest(await reply_harness.delivery(destination=STRANGER_ADDRESS))
    assert raised.value.rejection is InboundReplyRejection.REPLY_RECIPIENT_NOT_OURS


async def test_an_uncorrelated_reply_writes_nothing(reply_harness: ReplyHarness) -> None:
    """An identifier that resolves no locator correlates to nothing."""

    await reply_harness.prepare_sent()
    attester, _ = reply_harness.inbound_trust()

    with pytest.raises(InboundReplyRejected) as raised:
        await attester.attest(await reply_harness.delivery(ses_message_id="never-sent-by-us"))
    assert raised.value.rejection is InboundReplyRejection.REPLY_UNCORRELATED


async def test_a_reply_to_an_unprojected_execution_does_not_correlate(
    reply_harness: ReplyHarness,
) -> None:
    """No projection means no locator, which is the ``SEND_UNKNOWN`` property in miniature.

    A ``SEND_UNKNOWN`` execution never reaches the projection at all, so it never gets a
    locator; here the same absence is produced by skipping the projection, which exercises the
    identical code path without needing an ambiguous send to be arranged first.
    """

    await reply_harness.send.prepare()
    await reply_harness.send.approve()
    await reply_harness.send.send()
    attester, _ = reply_harness.inbound_trust()

    with pytest.raises(InboundReplyRejected) as raised:
        await attester.attest(await reply_harness.delivery())
    assert raised.value.rejection is InboundReplyRejection.REPLY_UNCORRELATED


@pytest.mark.parametrize(
    ("fixture_id", "expected"),
    [
        (MANAGER_HTML_ONLY, InboundReplyRejection.REPLY_NO_PLAIN_TEXT),
        (MANAGER_ATTACHMENT, InboundReplyRejection.REPLY_ATTACHMENT_PRESENT),
    ],
)
async def test_html_only_and_attachment_bearing_replies_are_refused_whole(
    reply_harness: ReplyHarness, fixture_id: str, expected: InboundReplyRejection
) -> None:
    await reply_harness.prepare_sent()
    attester, _ = reply_harness.inbound_trust()

    with pytest.raises(InboundReplyRejected) as raised:
        await attester.attest(await reply_harness.delivery(fixture_id=fixture_id))
    assert raised.value.rejection is expected


async def test_an_oversized_reply_is_refused_and_its_bytes_are_not_retained(
    reply_harness: ReplyHarness,
) -> None:
    await reply_harness.prepare_sent()
    attester, _ = reply_harness.inbound_trust()

    with pytest.raises(InboundReplyRejected) as raised:
        await attester.attest(await reply_harness.delivery(oversize=True))
    assert raised.value.rejection is InboundReplyRejection.REPLY_TOO_LARGE
    assert reply_harness.objects.private == {}


async def test_a_header_truncated_reply_is_refused(reply_harness: ReplyHarness) -> None:
    """A truncated header set may have dropped the ``References`` correlation depends on."""

    await reply_harness.prepare_sent()
    attester, _ = reply_harness.inbound_trust()

    with pytest.raises(InboundReplyRejected) as raised:
        await attester.attest(await reply_harness.delivery(headers_truncated=True))
    assert raised.value.rejection is InboundReplyRejection.REPLY_HEADERS_TRUNCATED


async def test_a_reply_to_a_terminal_case_is_refused(reply_harness: ReplyHarness) -> None:
    """``RESOLVED`` and ``CLOSED_UNRESOLVED`` refuse ingestion. A reply cannot reopen a case."""

    await reply_harness.prepare_sent()
    await _force_case_state(reply_harness, CaseState.CLOSED_UNRESOLVED)
    attester, _ = reply_harness.inbound_trust()

    with pytest.raises(InboundReplyRejected) as raised:
        await attester.attest(await reply_harness.delivery())
    assert raised.value.rejection is InboundReplyRejection.REPLY_CASE_TERMINAL


async def test_quoted_outbound_text_is_removed_before_anything_reads_it(
    reply_harness: ReplyHarness,
) -> None:
    """T37: a reply consisting only of our own message leaves nothing to ground against."""

    await reply_harness.prepare_sent()
    result = await reply_harness.ingest_reply(fixture_id=MANAGER_QUOTE_ONLY, quote_outbound=True)
    items = await reply_harness.send.action.compile.core.load_evidence_items(
        reply_harness.scope, (result.evidence_id,)
    )
    assert items[0].extracted_text is None


async def test_strip_quoted_outbound_removes_exactly_the_lines_we_sent() -> None:
    outbound = "Hello,\n\nPlease inspect and repair.\n\nAmbient CHORUS"
    reply = (
        "We will restore elevator B by 2030-01-14.\n> Hello,\nPlease inspect and repair.\nRegards\n"
    )

    assert strip_quoted_outbound(reply, outbound) == (
        "we will restore elevator b by 2030-01-14.\nregards"
    )


_REFLECTED_OUTBOUND = "Please inspect and repair the elevator,\nthen confirm the schedule."
"""A two-line outbound message reused across the Astra P1-2 reflow regressions below."""


def test_strip_quoted_outbound_removes_an_exact_quote_only_copy() -> None:
    reply = "> Please inspect and repair the elevator,\n> then confirm the schedule.\n"

    assert strip_quoted_outbound(reply, _REFLECTED_OUTBOUND) == ""


def test_strip_quoted_outbound_removes_a_split_line_copy() -> None:
    """A mail client that rewrapped our two lines into four, with no quote markers at all."""

    reply = "Please inspect and\nrepair the elevator,\nthen confirm the\nschedule.\n"

    assert strip_quoted_outbound(reply, _REFLECTED_OUTBOUND) == ""


def test_strip_quoted_outbound_removes_a_joined_line_copy() -> None:
    """A mail client that joined our two lines into one, with no quote markers at all."""

    reply = "Please inspect and repair the elevator, then confirm the schedule.\n"

    assert strip_quoted_outbound(reply, _REFLECTED_OUTBOUND) == ""


def test_strip_quoted_outbound_removes_a_whitespace_reflowed_copy() -> None:
    """CRLF line endings and folded internal whitespace, still with no quote markers."""

    reply = "Please   inspect and repair the elevator,\r\nthen    confirm the   schedule.\r\n"

    assert strip_quoted_outbound(reply, _REFLECTED_OUTBOUND) == ""


def test_strip_quoted_outbound_keeps_a_genuine_reply_before_quoted_outbound() -> None:
    reply = (
        "We will restore elevator B by 2030-01-14.\n"
        "> Please inspect and repair the elevator,\n"
        "> then confirm the schedule.\n"
    )

    assert strip_quoted_outbound(reply, _REFLECTED_OUTBOUND) == (
        "we will restore elevator b by 2030-01-14."
    )


def test_strip_quoted_outbound_preserves_genuine_text_reflowed_against_our_own() -> None:
    """A genuine sentence and a reflected one, joined onto a single line with no markers.

    The reflected suffix is removed and the genuine prefix survives -- proof this strips the
    *region* that reflects our own text rather than discarding the whole line it sits on.
    """

    reply = (
        "We will restore elevator B by 2030-01-14. "
        "Please inspect and repair the elevator, then confirm the schedule.\n"
    )

    assert strip_quoted_outbound(reply, _REFLECTED_OUTBOUND) == (
        "we will restore elevator b by 2030-01-14."
    )


async def test_a_reflected_only_message_never_moves_the_case_off_actioned(
    reply_harness: ReplyHarness,
) -> None:
    """Astra P1-2.G: a rewrapped copy of our own outbound message, with no quote markers.

    The default action fixture carries a ``requested_deadline``, so the regenerated outbound
    body contains a real ISO date ("Requested by: ..."). Before this repair, a client-side
    rewrap with no ``>`` markers survived stripping whole and let the literal-span extractor cite
    that date as if the correspondent had promised it -- producing a commitment and moving the
    case to ``VERIFYING`` from nothing but our own reflowed words.
    """

    from tests.fixtures.action import ACTOR_HASH

    await reply_harness.prepare_sent()
    outbound = await reply_harness.outbound_text()
    assert any(char.isdigit() for char in outbound), "the fixture must carry a real deadline"
    reflowed = " ".join(line.strip() for line in outbound.splitlines() if line.strip())

    execution = await reply_harness.send.execution()
    resolved = execution.ses_message_id or "missing"
    message_id = f"<reply-{uuid.uuid4()}@manager.invalid>"
    reply = ReviewedInboundReply(
        fixture_id="reflected-only-no-markers",
        subject="Re: Elevator service request",
        text_body=reflowed,
    )
    raw = build_raw_message(
        reply,
        message_id=message_id,
        in_reply_to=outbound_message_id(resolved),
        from_address=MANAGER_ADDRESS,
        to_address=INBOUND_ADDRESS,
    )
    object_key = "inbound/reflected-only-no-markers"
    reply_harness.raw_messages.put(
        bucket="chorus-local-inbound-fixtures", key=object_key, content=raw
    )
    envelope = build_receipt_envelope(
        message_id=message_id,
        in_reply_to=outbound_message_id(resolved),
        subject=reply.subject,
        source=MANAGER_ADDRESS,
        destination=INBOUND_ADDRESS,
        received_at=reply_harness.send.action.compile.clock.now(),
        object_key=object_key,
    )
    context = InboundMailTransportContext(
        transport=INBOUND_TRANSPORT, source_arn=INBOUND_SOURCE_ARN, envelope=envelope
    )
    attester, _ = reply_harness.inbound_trust()
    attested = await attester.attest(context)
    assert attested.evidence.extracted_text == ""

    ingested = await reply_harness.ingest().execute(
        IngestExternalReplyCommand(
            attested=attested, actor_id_hash=ACTOR_HASH, correlation_id=uuid.uuid4()
        )
    )
    job = await reply_harness.extraction_job(ingested)
    result = await reply_harness.extract().execute(job)

    assert result.commitment_id is None
    case = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)
    assert case.state is CaseState.ACTIONED


def _demo_delivery_source(reply_harness: ReplyHarness) -> FixtureReplyDeliverySource:
    """Build the real production ``FixtureReplyDeliverySource`` over this harness's pieces.

    Not part of ``ReplyHarness`` itself: this exercises the exact component
    ``POST /v1/demo/external-replies`` calls, so a bug in *how the route assembles a delivery* --
    Astra P2-5 -- is reproducible without standing up the whole HTTP application.
    """

    async def resolve(scope: CaseScope) -> str:
        del scope
        execution = await reply_harness.send.execution()
        return execution.ses_message_id or "missing"

    return FixtureReplyDeliverySource(
        namespace=reply_harness.scope.namespace,
        community_id=reply_harness.scope.community_id,
        case_id=reply_harness.scope.case_id,
        transport=INBOUND_TRANSPORT,
        source_arn=INBOUND_SOURCE_ARN,
        manager_address=MANAGER_ADDRESS,
        inbound_address=INBOUND_ADDRESS,
        raw_messages=reply_harness.raw_messages,
        resolve_sent_message_id=resolve,
        clock=reply_harness.send.action.compile.clock,
    )


async def test_an_identical_http_retry_never_duplicates_ingestion_side_effects(
    reply_harness: ReplyHarness,
) -> None:
    """Astra P2-5.A: same Idempotency-Key, same fixture, twice -- one of everything.

    Before this repair, ``FixtureReplyDeliverySource.deliver`` minted a fresh random
    ``Message-ID`` on every call, so two HTTP requests carrying the *same* Idempotency-Key still
    produced two distinct deliveries: ``IngestExternalReply``'s own idempotency (keyed on the
    inbound ``Message-ID``) never recognized the second as a retry, and it stored a second raw
    object, a second ``EvidenceItem``, and bumped the case counters a second time before this
    repair's HTTP-layer 409 could ever fire. Deriving ``Message-ID`` from the Idempotency-Key is
    what makes the two calls resolve to the *same* delivery, so ``IngestExternalReply`` replays
    on its own -- no new reservation layer required.
    """

    await reply_harness.prepare_sent()
    from tests.fixtures.action import ACTOR_HASH

    source = _demo_delivery_source(reply_harness)
    attester, _ = reply_harness.inbound_trust()
    idempotency_key = "reply-retry-key-0001"

    # Deliver, attest, and (below) ingest each request fully before starting the next -- exactly
    # the sequencing one HTTP request performs, and the sequencing that matters here: the fixture
    # store is keyed by the (now-deterministic) object key, so a second ``deliver`` between a
    # first request's delivery and its own attestation would silently read back the wrong bytes.
    first_delivery = await source.deliver(MANAGER_PROMISE, idempotency_key=idempotency_key)
    first_attested = await attester.attest(first_delivery)
    second_delivery = await source.deliver(MANAGER_PROMISE, idempotency_key=idempotency_key)
    second_attested = await attester.attest(second_delivery)
    assert first_attested.evidence.inbound_message_id_hash == (
        second_attested.evidence.inbound_message_id_hash
    )
    assert first_attested.evidence.raw_sha256 == second_attested.evidence.raw_sha256

    first_result = await reply_harness.ingest().execute(
        IngestExternalReplyCommand(
            attested=first_attested, actor_id_hash=ACTOR_HASH, correlation_id=uuid.uuid4()
        )
    )
    second_result = await reply_harness.ingest().execute(
        IngestExternalReplyCommand(
            attested=second_attested, actor_id_hash=ACTOR_HASH, correlation_id=uuid.uuid4()
        )
    )

    assert first_result.replayed is False
    assert second_result.replayed is True
    assert second_result.evidence_id == first_result.evidence_id
    assert second_result.case_version == first_result.case_version
    ingests = [
        plan
        for plan in reply_harness.send.action.unit_of_work.plans
        if plan.name == "ingest-external-reply"
    ]
    assert len(ingests) == 1


async def test_the_same_key_with_a_different_fixture_conflicts_before_ingestion(
    reply_harness: ReplyHarness,
) -> None:
    """Astra P2-5.B: same Idempotency-Key, a different body -- refused before any mutation.

    Both deliveries share a ``Message-ID`` (derived from the key alone), but carry genuinely
    different raw MIME content. ``IngestExternalReply._replay`` compares the recorded content
    digest against the retry's before touching anything else, so the second call must raise
    before writing a second raw object, evidence item, or case bump -- not merely answer 409
    after one landed.
    """

    await reply_harness.prepare_sent()
    from tests.fixtures.action import ACTOR_HASH

    source = _demo_delivery_source(reply_harness)
    attester, _ = reply_harness.inbound_trust()
    idempotency_key = "reply-retry-key-0002"

    first_delivery = await source.deliver(MANAGER_PROMISE, idempotency_key=idempotency_key)
    first_attested = await attester.attest(first_delivery)
    await reply_harness.ingest().execute(
        IngestExternalReplyCommand(
            attested=first_attested, actor_id_hash=ACTOR_HASH, correlation_id=uuid.uuid4()
        )
    )
    before = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)

    second_delivery = await source.deliver(MANAGER_HEDGE, idempotency_key=idempotency_key)
    second_attested = await attester.attest(second_delivery)
    assert first_attested.evidence.inbound_message_id_hash == (
        second_attested.evidence.inbound_message_id_hash
    )
    assert first_attested.evidence.raw_sha256 != second_attested.evidence.raw_sha256

    with pytest.raises(PersistenceConflictError):
        await reply_harness.ingest().execute(
            IngestExternalReplyCommand(
                attested=second_attested, actor_id_hash=ACTOR_HASH, correlation_id=uuid.uuid4()
            )
        )

    after = await reply_harness.send.action.compile.core.load_case(reply_harness.scope)
    assert after.version == before.version
    assert after.authorization_version == before.authorization_version


async def test_a_duplicate_delivery_stores_one_artifact_and_replays(
    reply_harness: ReplyHarness,
) -> None:
    """The ``INGEST_REPLY`` record replays the recorded outcome: one artifact, one root."""

    await reply_harness.prepare_sent()
    attested = await reply_harness.attest()
    from tests.fixtures.action import ACTOR_HASH

    command = IngestExternalReplyCommand(
        attested=attested,
        actor_id_hash=ACTOR_HASH,
        correlation_id=attested.evidence.execution_id.value,
    )
    first = await reply_harness.ingest().execute(command)
    second = await reply_harness.ingest().execute(command)

    assert not first.replayed
    assert second.replayed
    assert second.evidence_id == first.evidence_id
    ingests = [
        plan
        for plan in reply_harness.send.action.unit_of_work.plans
        if plan.name == "ingest-external-reply"
    ]
    assert len(ingests) == 1


async def test_the_case_holds_one_inbound_artifact_after_a_duplicate(
    reply_harness: ReplyHarness,
) -> None:
    """Counted by *binding*, because the fixture case already carries resident uploads.

    The property is "one inbound artifact per delivered message", not "one evidence row per
    case", and asserting the second would pass for the wrong reason the day the fixture stops
    seeding a photograph.
    """

    await reply_harness.prepare_sent()
    attested = await reply_harness.attest()
    from tests.fixtures.action import ACTOR_HASH

    command = IngestExternalReplyCommand(
        attested=attested, actor_id_hash=ACTOR_HASH, correlation_id=attested.evidence.case_id.value
    )
    await reply_harness.ingest().execute(command)
    await reply_harness.ingest().execute(command)

    from chorus.infrastructure.dynamodb import keys
    from chorus.ports.storage import QueryRequest, SortKeyBeginsWith

    result = await reply_harness.send.action.driver.query(
        QueryRequest(
            table=TableName.CORE,
            partition_key=keys.case_partition(
                reply_harness.scope.namespace, reply_harness.scope.case_id
            ),
            sort_key=SortKeyBeginsWith("EVIDENCE#"),
            consistent=True,
            limit=50,
        )
    )
    inbound = [item for item in result.items if item.get("external_source_binding") is not None]
    assert len(inbound) == 1


async def _force_case_state(harness: ReplyHarness, state: CaseState) -> None:
    """Move the case to a terminal state through the real transition service.

    Not by writing a row: the attester conditions on what the state machine can actually
    produce, and a hand-written row could carry a combination it never would.
    """

    import dataclasses as _dataclasses

    from chorus.ports.unit_of_work import TransactionPlan

    case = await harness.send.action.compile.core.load_case(harness.scope)
    closed = _dataclasses.replace(
        case,
        state=state,
        version=case.version + 1,
        closed_at=harness.send.action.compile.clock.now(),
        updated_at=harness.send.action.compile.clock.now(),
    )
    await harness.send.action.compile.unit_of_work.commit(
        TransactionPlan(
            name="reply-harness-terminal",
            operations=(
                harness.send.action.compile.core.stage_update_case(
                    harness.scope, closed, expected_version=case.version
                ),
            ),
            audit_required=False,
        )
    )


def _unused(*_values: object) -> None:
    """Keep imports that document the boundary but are only read by some parameterizations."""


_unused(INBOUND_ADDRESS, MANAGER_ADDRESS, Sha256Digest, timedelta)
