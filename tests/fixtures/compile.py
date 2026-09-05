"""A Phase-6 harness: the elevator fixture, persisted, plus a wired compile use case.

The Phase-1 fixture already builds a complete ``CompileContext``. Phase 6's job is to prove the
*adapter* reconstructs exactly that context from storage and persists the compiler's answer, so
this harness writes the fixture into a real driver and hands back a ``CompileView`` wired to the
same repositories the application uses. Nothing here re-implements a compiler decision; the
assertions live in the tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from io import BytesIO
from uuid import UUID, uuid5

from PIL import Image

from chorus.application.commands.compile_view import (
    CompileView,
    CompileViewCommand,
    RequestedFactInput,
)
from chorus.application.services.safe_evidence import PrepareSafeEvidence
from chorus.domain.entities import (
    CommunityCase,
    EvidenceItem,
    EvidenceRoot,
    MandateStatus,
    Purpose,
)
from chorus.domain.facts import Fact
from chorus.domain.ids import (
    EvidenceItemId,
    IdGenerator,
    Namespace,
    Sha256Digest,
    Uuid5Generator,
)
from chorus.domain.mandates import CurrentMandatePointer, DisclosureMandate
from chorus.infrastructure.dynamodb.audit import AuditRepository
from chorus.infrastructure.dynamodb.core import CoreRepository
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.idempotency import IdempotencyRepository
from chorus.infrastructure.dynamodb.shareable import ShareableRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.imaging.sanitizer import sanitize_image
from chorus.infrastructure.local.objects import InMemoryObjectStore
from chorus.ports.evidence_review import EvidenceReviewInput
from chorus.ports.imaging import SafeImage
from chorus.ports.records import (
    EvidenceRootLocator,
    StoredCurrentMandatePointer,
    StoredSafeDestination,
)
from chorus.ports.retention import AuditRetention
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import StorageDriver, TableName, WriteOperation
from chorus.ports.unit_of_work import TransactionPlan
from chorus.privacy.compiler import PrivacyCompiler
from chorus.privacy.policy import IntendedUsage, Necessity
from tests.fixtures.elevator import NAMESPACE, NOW, ElevatorFixture, build_elevator_fixture

COMMUNITY_PUBLIC_LABEL = "Example Community Building"
CURSOR_SECRET = b"compile-harness-cursor-secret-0123456789abcdef"

_FIXTURE_NAMESPACE = UUID("6b1f2f2c-2c7d-5f2b-9f6d-4b1f2f2c2c7d")


def harness_uuid(name: str) -> UUID:
    return uuid5(_FIXTURE_NAMESPACE, name)


def photo_bytes() -> bytes:
    """A real, tiny JPEG that decodes -- the fixture's own file is a 1x1 placeholder.

    Built in-test rather than committed, because the interesting property is that a *decodable*
    image with real dimensions survives the sanitizer identically on every run.
    """

    buffer = BytesIO()
    Image.new("RGB", (24, 18), (32, 64, 96)).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def photo_bytes_with_metadata() -> bytes:
    """A JPEG carrying EXIF orientation, GPS, a maker string, and a comment sentinel.

    The committed elevator photo is a 1x1 placeholder with only a JFIF marker, so it cannot
    demonstrate metadata stripping at all. Adversarial metadata inputs are therefore built here
    and never committed: no fixture in this repository gains real location data.
    """

    buffer = BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F] = "SECRET_SENTINEL_MAKE"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (51.0, 30.0, 0.0)
    gps[3] = "W"
    gps[4] = (0.0, 7.0, 0.0)
    Image.new("RGB", (40, 30), (10, 20, 30)).save(
        buffer, format="JPEG", exif=exif, comment=b"SECRET_SENTINEL_COMMENT"
    )
    return buffer.getvalue()


@dataclass(slots=True)
class RecordingSanitizer:
    """The real sanitizer, counting calls so a replay can prove it did not run again."""

    calls: int = 0

    def sanitize(self, source: bytes, *, declared_media_type: str) -> SafeImage:
        self.calls += 1
        return sanitize_image(source, declared_media_type=declared_media_type)


@dataclass(slots=True)
class StubReviewRegistry:
    """A read-only registry, seeded exactly like the fixture manifest's curated records."""

    reviews: dict[EvidenceItemId, EvidenceReviewInput] = field(default_factory=dict)

    def review_for(self, evidence_id: EvidenceItemId) -> EvidenceReviewInput | None:
        return self.reviews.get(evidence_id)


@dataclass(slots=True)
class FixedClock:
    instant: datetime

    def now(self) -> datetime:
        return self.instant


@dataclass(slots=True)
class CompileHarness:
    """Persist the elevator fixture and wire the compile use case over one driver."""

    driver: StorageDriver
    fixture: ElevatorFixture = field(default_factory=build_elevator_fixture)
    objects: InMemoryObjectStore = field(default_factory=InMemoryObjectStore)
    sanitizer: RecordingSanitizer = field(default_factory=RecordingSanitizer)
    reviews: StubReviewRegistry = field(default_factory=StubReviewRegistry)
    clock: FixedClock = field(default_factory=lambda: FixedClock(NOW))
    ids: IdGenerator = field(
        default_factory=lambda: Uuid5Generator(namespace=harness_uuid("ids"), prefix="compile")
    )

    seeded_items: tuple[EvidenceItem, ...] = ()

    core: CoreRepository = field(init=False)
    shareable: ShareableRepository = field(init=False)
    audit: AuditRepository = field(init=False)
    idempotency: IdempotencyRepository = field(init=False)
    unit_of_work: StorageUnitOfWork = field(init=False)

    def __post_init__(self) -> None:
        cursors = SignedCursorCodec(secret=CURSOR_SECRET)
        self.core = CoreRepository(driver=self.driver, cursors=cursors)
        self.shareable = ShareableRepository(driver=self.driver, cursors=cursors)
        self.audit = AuditRepository(
            driver=self.driver, cursors=cursors, retention=AuditRetention.demo()
        )
        self.idempotency = IdempotencyRepository(driver=self.driver, table=TableName.SHAREABLE)
        self.unit_of_work = StorageUnitOfWork(driver=self.driver)

    # -- scope ------------------------------------------------------------------------

    @property
    def case(self) -> CommunityCase:
        return self.fixture.context.case

    @property
    def scope(self) -> CaseScope:
        return CaseScope(
            namespace=NAMESPACE,
            community_id=self.case.community_id,
            case_id=self.case.case_id,
        )

    # -- seeding ----------------------------------------------------------------------

    async def seed(
        self,
        *,
        case: CommunityCase | None = None,
        facts: tuple[Fact, ...] | None = None,
        mandates: tuple[DisclosureMandate, ...] | None = None,
        pointers: tuple[CurrentMandatePointer, ...] | None = None,
        evidence_items: tuple[EvidenceItem, ...] | None = None,
        roots: tuple[EvidenceRoot, ...] | None = None,
        photo: bytes | None = None,
    ) -> None:
        """Write the fixture into storage, one bounded transaction per entity family."""

        context = self.fixture.context
        subject = case or context.case
        scope = CaseScope(
            namespace=NAMESPACE,
            community_id=subject.community_id,
            case_id=subject.case_id,
        )
        await self._commit((self.core.stage_create_case(scope, subject),))
        for report in context.reports:
            await self._commit((self.core.stage_create_report(scope, report),))
        for fact in facts if facts is not None else context.facts:
            await self._commit((self.core.stage_create_fact(scope, fact),))
        for root in roots if roots is not None else context.evidence_roots:
            await self._commit(
                (
                    self.core.stage_create_evidence_root(scope.community_scope, root),
                    self.core.stage_create_evidence_root_locator(
                        scope.community_scope,
                        EvidenceRootLocator(
                            namespace=NAMESPACE,
                            community_id=root.community_id,
                            root_id=root.root_id,
                            root_sha256=root.root_sha256,
                            created_at=root.created_at,
                        ),
                    ),
                )
            )
        effective_items = evidence_items if evidence_items is not None else context.evidence_items
        for item in effective_items:
            await self._commit((self.core.stage_create_evidence_item(scope, item),))
        for mandate in mandates if mandates is not None else context.mandates:
            await self._commit((self.core.stage_append_mandate_version(scope, mandate),))
        for pointer in pointers if pointers is not None else context.mandate_pointers:
            await self._commit(
                (
                    self.core.stage_replace_current_mandate_pointer(
                        scope,
                        StoredCurrentMandatePointer(
                            namespace=NAMESPACE,
                            community_id=subject.community_id,
                            pointer=pointer,
                            status=self._status_of(pointer, mandates or context.mandates),
                            version=1,
                            created_at=NOW - timedelta(days=2),
                            updated_at=NOW - timedelta(days=2),
                        ),
                        expected=None,
                    ),
                )
            )
        self.seeded_items = effective_items
        self.seed_photo_object(photo if photo is not None else photo_bytes())
        self.seed_review()

    @staticmethod
    def _status_of(
        pointer: CurrentMandatePointer, mandates: tuple[DisclosureMandate, ...]
    ) -> MandateStatus:
        for mandate in mandates:
            if mandate.mandate_id == pointer.mandate_id and mandate.version == pointer.version:
                return mandate.status
        raise AssertionError("a pointer must name a seeded mandate version")

    def seed_photo_object(self, content: bytes) -> None:
        """Place the source bytes where ingestion would have written them."""

        item = self.evidence_item(self.fixture.photo_evidence_id)
        self.objects.seed_private_evidence(
            namespace=NAMESPACE,
            community_id=item.community_id,
            case_id=item.case_id,
            evidence_id=item.evidence_id,
            content=content,
            media_type=item.media_type,
        )

    def seed_review(self, *, cleared: bool = True) -> None:
        item = self.evidence_item(self.fixture.photo_evidence_id)
        self.reviews.reviews[item.evidence_id] = EvidenceReviewInput(
            no_face=cleared,
            no_unit=True,
            no_name=True,
            no_health=True,
            safe_caption="A reviewed elevator out-of-service indicator photo is available.",
            reviewed_by="fixture-curation:elevator-v1",
            source_sha256=item.sha256,
        )

    def evidence_item(self, evidence_id: EvidenceItemId) -> EvidenceItem:
        """Return the item as *seeded*, so a review binds the digest storage actually holds."""

        source = self.seeded_items or self.fixture.context.evidence_items
        for item in source:
            if item.evidence_id == evidence_id:
                return item
        raise AssertionError("the fixture has no such evidence item")

    def align_photo_digest(self, content: bytes) -> tuple[EvidenceItem, ...]:
        """Return evidence items whose photo record matches the bytes actually seeded.

        The fixture's digest is synthetic. The prepare step compares the stored ``sha256`` and
        ``byte_length`` against the object it reads, so a test that seeds real bytes must seed a
        record that describes them -- which is the same integrity check the production path
        relies on.
        """

        from hashlib import sha256 as _sha256

        digest = Sha256Digest(f"sha256:{_sha256(content).hexdigest()}")
        return tuple(
            replace(item, sha256=digest, byte_length=len(content))
            if item.evidence_id == self.fixture.photo_evidence_id
            else item
            for item in self.fixture.context.evidence_items
        )

    async def _commit(self, operations: tuple[WriteOperation, ...]) -> None:
        await self.unit_of_work.commit(
            TransactionPlan(
                name="compile-harness-seed", operations=operations, audit_required=False
            )
        )

    # -- use case ---------------------------------------------------------------------

    def stored_destination(self) -> StoredSafeDestination:
        """The fixture registry entry in the shape the composition root actually holds."""

        entry = self.fixture.context.destination_registry_entry
        return StoredSafeDestination(
            destination_id=entry.destination_id,
            kind=entry.kind,
            registry_version=entry.registry_version,
            routing_token=entry.routing_token,
            display_label=entry.display_label,
        )

    def compile_view(self) -> CompileView:
        return CompileView(
            core=self.core,
            shareable=self.shareable,
            audit=self.audit,
            idempotency=self.idempotency,
            unit_of_work=self.unit_of_work,
            compiler=PrivacyCompiler(
                id_generator_factory=lambda compile_id: Uuid5Generator(
                    namespace=compile_id, prefix="compile"
                )
            ),
            evidence=PrepareSafeEvidence(
                objects=self.objects, sanitizer=self.sanitizer, ids=self.ids
            ),
            reviews=self.reviews,
            clock=self.clock,
            ids=self.ids,
            community_public_label=COMMUNITY_PUBLIC_LABEL,
        )

    def command(
        self,
        *,
        compile_id: UUID | None = None,
        idempotency_key: str = "compile-key-0001",
        expected_case_version: int | None = None,
        fact_ids: tuple[object, ...] | None = None,
        evidence_ids: tuple[EvidenceItemId, ...] | None = None,
        necessity: Necessity = Necessity.OPTIONAL,
        necessity_required: bool = False,
        purpose: Purpose = Purpose.REQUEST_ELEVATOR_REPAIR_AND_RESPONSE,
        destination: object | None = None,
        case_id: object | None = None,
    ) -> CompileViewCommand:
        """Request every active fact as ``OPTIONAL``, plus the photo, unless told otherwise."""

        context = self.fixture.context
        chosen = Necessity.REQUIRED if necessity_required else necessity
        selected = (
            fact_ids if fact_ids is not None else tuple(fact.fact_id for fact in context.facts)
        )
        return CompileViewCommand(
            namespace=NAMESPACE,
            community_id=self.case.community_id,
            case_id=case_id if case_id is not None else self.case.case_id,  # type: ignore[arg-type]
            compile_id=compile_id or harness_uuid("compile:1"),
            expected_case_version=(
                expected_case_version if expected_case_version is not None else self.case.version
            ),
            requested_facts=tuple(
                RequestedFactInput(
                    fact_id=fact_id,  # type: ignore[arg-type]
                    necessity=chosen.value,
                    intended_usage=(
                        IntendedUsage.EVIDENCE.value
                        if fact_id == self.fixture.photo_fact_id
                        else IntendedUsage.CLAIM.value
                    ),
                )
                for fact_id in selected
            ),
            requested_evidence_ids=(
                evidence_ids if evidence_ids is not None else (self.fixture.photo_evidence_id,)
            ),
            destination=destination or self.stored_destination(),  # type: ignore[arg-type]
            purpose=purpose,
            actor_id_hash=Sha256Digest("sha256:" + "a" * 64),
            idempotency_key=idempotency_key,
        )


def namespace_of() -> Namespace:
    return NAMESPACE


SENTINEL_PATTERN = re.compile(r"(?i)SECRET_SENTINEL|mother_health|Apartment\s*4B|Leela")
"""Everything the fixture plants that must never reach an external-safe artifact."""
