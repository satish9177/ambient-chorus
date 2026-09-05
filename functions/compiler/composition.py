"""Wire the compiler Lambda's adapters to the one use case it exists to run.

A composition root and nothing else: it constructs, it does not decide. Every policy question
is answered inside ``chorus.privacy.compiler``, every persistence rule inside the repositories,
and every object rule inside the safe-evidence service. If this module ever grows a branch on a
scope, a mandate, or a media type, that branch is a second policy implementation.

**No deployed resource is created here.** Phase 6 owns this artifact and its static assertions;
the deployed function, the live invocation, and the post-deploy IAM canaries belong to Phase 11.
That is the same split the agent runtimes already use.

**What this artifact must never import** is asserted by test: no Strands, no Bedrock client, no
SES client, no agent contract, and no scheduler. The compiler is the one component whose answers
must not be able to become probabilistic, and an import is the first step toward that.
"""

from __future__ import annotations

from dataclasses import dataclass

from chorus.application.commands.compile_view import CompileView
from chorus.application.services.safe_evidence import PrepareSafeEvidence
from chorus.domain.ids import IdGenerator, Uuid4Generator
from chorus.infrastructure.dynamodb.audit import AuditRepository
from chorus.infrastructure.dynamodb.client import create_dynamodb_client
from chorus.infrastructure.dynamodb.core import CoreRepository
from chorus.infrastructure.dynamodb.cursor import SignedCursorCodec
from chorus.infrastructure.dynamodb.driver import DynamoDbStorageDriver
from chorus.infrastructure.dynamodb.idempotency import IdempotencyRepository
from chorus.infrastructure.dynamodb.shareable import ShareableRepository
from chorus.infrastructure.dynamodb.unit_of_work import StorageUnitOfWork
from chorus.infrastructure.fixtures.review_registry import FixtureEvidenceReviewRegistry
from chorus.infrastructure.fixtures.synthetic_feed import SyntheticAmbientAdapter
from chorus.infrastructure.imaging.sanitizer import sanitize_image
from chorus.infrastructure.s3.client import create_s3_client
from chorus.infrastructure.s3.objects import S3ObjectStore
from chorus.ports.clock import Clock
from chorus.ports.evidence_review import EvidenceReviewRegistryPort
from chorus.ports.imaging import SafeImage
from chorus.ports.records import StoredSafeDestination
from chorus.ports.retention import AuditRetention
from chorus.ports.storage import TableName
from chorus.privacy.compiler import PrivacyCompiler


class PillowImageSanitizer:
    """The frozen ADR-018 profile behind the port.

    A class rather than the bare function so the composition root passes a *port*, which is
    what lets a test substitute a recording or failing sanitizer without patching a module.
    """

    def sanitize(self, source: bytes, *, declared_media_type: str) -> SafeImage:
        return sanitize_image(source, declared_media_type=declared_media_type)


@dataclass(frozen=True, slots=True, kw_only=True)
class CompilerSettings:
    """Everything the composition root needs, and nothing it could decide policy from."""

    region: str
    core_table: str
    shareable_table: str
    audit_table: str
    private_evidence_bucket: str
    export_evidence_bucket: str
    community_public_label: str
    destination: StoredSafeDestination
    cursor_secret: bytes
    dynamodb_endpoint: str | None = None


def build_compile_view(
    settings: CompilerSettings,
    *,
    clock: Clock,
    ids: IdGenerator | None = None,
    reviews: EvidenceReviewRegistryPort | None = None,
) -> CompileView:
    """Construct the compile use case over deployed adapters.

    The identifier generator defaults to UUIDv4 because a view is an ordinary entity, not one of
    the ADR-011 replay identities. Determinism of the *view hash* comes from the inputs being
    fixed, which is why the golden tests inject a deterministic generator rather than this
    function producing one.
    """

    driver = DynamoDbStorageDriver(
        client=create_dynamodb_client(
            region_name=settings.region, endpoint_url=settings.dynamodb_endpoint
        ),
        table_names={
            TableName.CORE: settings.core_table,
            TableName.SHAREABLE: settings.shareable_table,
            TableName.AUDIT: settings.audit_table,
        },
    )
    cursors = SignedCursorCodec(secret=settings.cursor_secret)
    objects = S3ObjectStore(
        client=create_s3_client(region_name=settings.region),
        private_bucket=settings.private_evidence_bucket,
        export_bucket=settings.export_evidence_bucket,
    )
    generator = ids or Uuid4Generator()
    registry = reviews or FixtureEvidenceReviewRegistry.from_fixtures(
        SyntheticAmbientAdapter().evidence_fixtures
    )
    return CompileView(
        core=CoreRepository(driver=driver, cursors=cursors),
        shareable=ShareableRepository(driver=driver, cursors=cursors),
        audit=AuditRepository(driver=driver, cursors=cursors, retention=AuditRetention.demo()),
        # The compile idempotency record lives in the Shareable table, under the case's
        # view-current partition -- the only case-scoped partition there that the compiler's
        # ``LeadingKeys`` grant permits it to write.
        idempotency=IdempotencyRepository(driver=driver, table=TableName.SHAREABLE),
        unit_of_work=StorageUnitOfWork(driver=driver),
        compiler=PrivacyCompiler(id_generator_factory=lambda _: generator),
        evidence=PrepareSafeEvidence(
            objects=objects, sanitizer=PillowImageSanitizer(), ids=generator
        ),
        reviews=registry,
        clock=clock,
        ids=generator,
        community_public_label=settings.community_public_label,
    )
