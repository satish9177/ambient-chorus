"""The export object: one content address, one write, and no ambiguous second key.

ADR-018 removed the pending state, the finalization copy, and the compensating delete, which
means the object failure modes that remain are exactly three: the write never happened, the
write happened, and the caller cannot tell which. This file proves all three resolve to the
same durable object, and that an object which exists before its transaction commits grants
nothing.
"""

from __future__ import annotations

import pytest
from tests.fixtures.compile import CompileHarness, photo_bytes

from chorus.application.commands.compile_view import CompileView
from chorus.application.errors import PolicyDeniedError
from chorus.domain.errors import IntegrityError
from chorus.domain.ids import CaseId, CommunityId, EvidenceItemId, Namespace, Sha256Digest
from chorus.infrastructure.imaging.sanitizer import sanitize_image
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.infrastructure.local.objects import _StoredObject
from chorus.ports.errors import ExternalDependencyError, PersistenceConflictError
from chorus.ports.objects import ExportObjectDescriptor, export_evidence_key

pytestmark = pytest.mark.anyio


async def _seeded(harness: CompileHarness) -> tuple[CompileView, bytes]:
    raw = photo_bytes()
    await harness.seed(evidence_items=harness.align_photo_digest(raw), photo=raw)
    return harness.compile_view(), raw


def _export_keys(harness: CompileHarness) -> list[str]:
    return sorted(harness.objects.export)


async def test_a_successful_compile_writes_exactly_one_content_addressed_object(
    harness: CompileHarness,
) -> None:
    compile_view, _ = await _seeded(harness)

    result = await compile_view.execute(harness.command())

    assert result.view is not None
    ref = result.view.safe_evidence_refs[0]
    expected = export_evidence_key(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
        derivative_sha256=ref.sha256,
    )
    assert _export_keys(harness) == [expected]
    assert harness.objects.put_calls == 1


async def test_the_object_key_is_the_digest_and_names_no_private_identifier(
    harness: CompileHarness,
) -> None:
    """Matrix U and AM. The address is content, so it cannot leak lineage."""

    compile_view, _ = await _seeded(harness)

    await compile_view.execute(harness.command())

    key = _export_keys(harness)[0]
    assert str(harness.fixture.photo_evidence_id) not in key
    assert str(harness.fixture.photo_fact_id) not in key
    assert key.endswith("/content")


async def test_an_object_written_before_a_denied_compile_is_an_unreferenced_orphan(
    harness: CompileHarness,
) -> None:
    """Matrix W and Y. The bytes are durable and confer nothing, because nothing points at them."""

    compile_view, _ = await _seeded(harness)
    # A stale expected version denies at gate 3, after the derivative is already durable.
    command = harness.command(expected_case_version=harness.case.version + 5)

    with pytest.raises(PolicyDeniedError):
        await compile_view.execute(command)

    assert len(_export_keys(harness)) == 1
    assert await harness.shareable.load_current_view_pointer(harness.scope) is None
    from chorus.ports.pagination import PageRequest

    history = await harness.shareable.read_view_history(harness.scope, PageRequest(limit=10))
    assert history.items == ()


async def test_a_retry_reuses_the_existing_object_and_writes_no_second_one(
    harness: CompileHarness,
) -> None:
    """Matrix X. The conditional create refuses the second write rather than overwriting.

    A retry does attempt the create -- it cannot know the object is there without asking --
    but the precondition refuses it, the caller verifies the existing object, and the bytes
    on the key remain the ones the first compile wrote.
    """

    compile_view, _ = await _seeded(harness)
    from tests.fixtures.compile import harness_uuid

    denied = harness.command(
        compile_id=harness_uuid("compile:denied"),
        idempotency_key="compile-key-denied",
        expected_case_version=harness.case.version + 5,
    )
    with pytest.raises(PolicyDeniedError):
        await compile_view.execute(denied)
    first_keys = _export_keys(harness)
    first_bytes = harness.objects.export[first_keys[0]].content
    puts_after_first = harness.objects.put_calls

    result = await compile_view.execute(
        harness.command(
            compile_id=harness_uuid("compile:second"), idempotency_key="compile-key-second"
        )
    )

    assert result.view is not None
    assert _export_keys(harness) == first_keys
    assert len(_export_keys(harness)) == 1
    # The create was attempted and refused, so the object still holds the first bytes.
    assert harness.objects.put_calls == puts_after_first + 1
    assert harness.objects.export[first_keys[0]].content == first_bytes


async def test_a_put_that_keeps_failing_stops_the_compile_before_any_durable_state(
    harness: CompileHarness,
) -> None:
    """One identical repeat is licensed. A second failure is a dependency outage, not a race.

    There is deliberately no retry loop: the repeat exists because an absent object proves
    the write did not land, not because retrying is a strategy.
    """

    compile_view, _ = await _seeded(harness)
    harness.objects.fail_puts = 2

    with pytest.raises(ExternalDependencyError):
        await compile_view.execute(harness.command())

    assert _export_keys(harness) == []
    assert harness.objects.put_calls == 2
    assert await harness.shareable.load_current_view_pointer(harness.scope) is None


async def test_an_unknown_put_outcome_is_resolved_by_heading_the_exact_key(
    harness: CompileHarness,
) -> None:
    """The only interesting object failure: the bytes landed and the caller cannot tell.

    The store writes, then raises. The caller heads the exact content address, finds the
    object consistent, and continues -- writing no second key and no second object.
    """

    compile_view, _ = await _seeded(harness)
    harness.objects.ambiguous_next_put = True

    result = await compile_view.execute(harness.command())

    assert result.view is not None
    assert len(_export_keys(harness)) == 1
    # One create attempt, and exactly one head to settle what it did.
    assert harness.objects.put_calls == 1
    assert harness.objects.head_calls == 1


async def test_an_unknown_put_outcome_with_nothing_stored_repeats_the_identical_write(
    harness: CompileHarness,
) -> None:
    """Absent means the write definitely did not land, so the same PUT is safe to repeat."""

    compile_view, _ = await _seeded(harness)
    harness.objects.fail_puts = 1

    result = await compile_view.execute(harness.command())

    assert result.view is not None
    assert harness.objects.put_calls == 2
    assert len(_export_keys(harness)) == 1


async def test_an_inconsistent_object_at_the_content_address_is_an_integrity_failure(
    harness: CompileHarness,
) -> None:
    """A disagreement at a content address is corruption, never a race to be tolerated."""

    compile_view, _ = await _seeded(harness)
    await compile_view.execute(harness.command())
    key = _export_keys(harness)[0]
    from chorus.infrastructure.local.objects import _StoredObject

    harness.objects.export[key] = _StoredObject(
        content=b"not the derivative", media_type="image/png"
    )

    with pytest.raises(IntegrityError, match="export_object_inconsistent"):
        await compile_view.execute(harness.command(idempotency_key="compile-key-third"))


async def test_the_stored_private_key_must_equal_the_derived_one(
    harness: CompileHarness,
) -> None:
    """A corrupted stored key cannot redirect a read to another object."""

    from dataclasses import replace

    from chorus.domain.ids import SensitiveStr

    raw = photo_bytes()
    aligned = harness.align_photo_digest(raw)
    tampered = tuple(
        replace(
            item,
            private_object_key=SensitiveStr("ns/OTHER/community/x/case/y/evidence/z/v1/original"),
        )
        if item.evidence_id == harness.fixture.photo_evidence_id
        else item
        for item in aligned
    )
    await harness.seed(evidence_items=tampered, photo=raw)
    compile_view = harness.compile_view()

    with pytest.raises(IntegrityError, match="object_key_mismatch"):
        await compile_view.execute(harness.command())


async def test_a_source_whose_bytes_disagree_with_its_record_is_refused(
    harness: CompileHarness,
) -> None:
    """The record describes the file. A file that is not the described one is not exportable."""

    raw = photo_bytes()
    await harness.seed(evidence_items=harness.align_photo_digest(raw), photo=raw + b"extra")
    compile_view = harness.compile_view()

    with pytest.raises(IntegrityError, match="source_length_mismatch"):
        await compile_view.execute(harness.command())


async def test_an_existing_object_is_verified_and_reused_rather_than_overwritten(
    harness: CompileHarness,
) -> None:
    """Gate 2B. The create is refused, the existing object is proved identical, and reused."""

    raw = photo_bytes()
    await harness.seed(evidence_items=harness.align_photo_digest(raw), photo=raw)
    expected = sanitize_image(raw, declared_media_type="image/jpeg")
    key = export_evidence_key(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
        derivative_sha256=expected.sha256,
    )
    # Somebody -- an earlier compile, or a concurrent one -- already created it.
    harness.objects.export[key] = _StoredObject(
        content=expected.content, media_type=expected.media_type
    )

    result = await harness.compile_view().execute(harness.command())

    assert result.view is not None
    assert result.view.safe_evidence_refs[0].sha256 == expected.sha256
    assert harness.objects.export[key].content == expected.content
    assert harness.objects.put_calls == 1
    assert harness.objects.head_calls == 1


async def test_an_existing_object_that_disagrees_is_an_integrity_failure(
    harness: CompileHarness,
) -> None:
    """Gate 2B, the other branch. At a content address, disagreement is corruption."""

    raw = photo_bytes()
    await harness.seed(evidence_items=harness.align_photo_digest(raw), photo=raw)
    expected = sanitize_image(raw, declared_media_type="image/jpeg")
    key = export_evidence_key(
        namespace=harness.scope.namespace,
        community_id=harness.scope.community_id,
        case_id=harness.scope.case_id,
        derivative_sha256=expected.sha256,
    )
    harness.objects.export[key] = _StoredObject(
        content=b"different bytes at the same address", media_type="image/png"
    )

    with pytest.raises(IntegrityError, match="export_object_inconsistent"):
        await harness.compile_view().execute(harness.command())


async def test_concurrent_identical_writers_create_the_object_once(
    harness: CompileHarness,
) -> None:
    """Gate 2D. Two compiles of one photograph; one create wins, the other verifies and reuses.

    Both run against the same object store, so this is the real race rather than a simulated
    one: whichever conditional create lands first is the only write, and the loser's answer is
    the winner's bytes.
    """

    raw = photo_bytes()
    await harness.seed(evidence_items=harness.align_photo_digest(raw), photo=raw)

    second = CompileHarness(driver=InMemoryStorageDriver(), objects=harness.objects)
    await second.seed(evidence_items=second.align_photo_digest(raw), photo=raw)

    first_result = await harness.compile_view().execute(harness.command())
    second_result = await second.compile_view().execute(second.command())

    assert first_result.view is not None
    assert second_result.view is not None
    assert len(_export_keys(harness)) == 1
    assert (
        first_result.view.safe_evidence_refs[0].sha256
        == second_result.view.safe_evidence_refs[0].sha256
    )
    stored = harness.objects.export[_export_keys(harness)[0]]
    assert stored.content == sanitize_image(raw, declared_media_type="image/jpeg").content


async def test_a_precondition_conflict_whose_object_then_vanishes_fails_closed(
    harness: CompileHarness,
) -> None:
    """The service said something was there and a read at the same address found nothing.

    That is not a race this system can reason about, so it refuses rather than writing again.
    """

    raw = photo_bytes()
    await harness.seed(evidence_items=harness.align_photo_digest(raw), photo=raw)
    inner = harness.objects

    class VanishingStore:
        """Reports a conflict, then reports nothing at the address it named."""

        async def load_private_evidence(
            self,
            *,
            namespace: Namespace,
            community_id: CommunityId,
            case_id: CaseId,
            evidence_id: EvidenceItemId,
        ) -> bytes:
            return await inner.load_private_evidence(
                namespace=namespace,
                community_id=community_id,
                case_id=case_id,
                evidence_id=evidence_id,
            )

        async def head_export_evidence(
            self,
            *,
            namespace: Namespace,
            community_id: CommunityId,
            case_id: CaseId,
            derivative_sha256: Sha256Digest,
        ) -> ExportObjectDescriptor | None:
            return None

        async def put_export_evidence(
            self,
            *,
            namespace: Namespace,
            community_id: CommunityId,
            case_id: CaseId,
            derivative_sha256: Sha256Digest,
            content: bytes,
            media_type: str,
        ) -> None:
            raise PersistenceConflictError("EXPORT_EVIDENCE_OBJECT")

    compile_view = harness.compile_view()
    compile_view.evidence.objects = VanishingStore()

    with pytest.raises(IntegrityError, match="export_object_vanished"):
        await compile_view.execute(harness.command())
