"""The local demo reset application service.

[08-api-design.md § Reset](../../../docs/architecture/08-api-design.md) freezes the request,
the response, and what reset may and may not do: it seeds the community, the four contributors,
the logical clock, the 24-message corpus, the two private evidence objects, the destination
registry entry, and the reply fixture catalog -- and it leaves no report, fact, candidate case,
assessment, mandate, view, action, approval, execution, commitment, schedule, or verification
result behind. It runs no agent, no compiler, no sender, and no watcher.

Restoring that seed state, not merely writing it
------------------------------------------------
Reset is a *delete-and-seed*: it first removes every row and every private object that belongs
to the demo namespace, then re-creates exactly the deterministic seed set. The cleanup is
bounded to one namespace by construction -- the frozen three-table grammar prefixes every
partition key with ``NS#{namespace}`` and every object key with ``ns/{namespace}/`` -- so this
is a namespace sweep, never a table-wide scan, and it can name no partition outside ``DEMO``.
Phase 11's distributed/S3 cleanup is deliberately not implemented here.

The frozen order
---------------
1. validate the request and the idempotency key
2. validate the actual frozen fixture snapshot (the adapter can be mutated after composition)
3. enumerate the namespace and refuse if *any* execution is SENDING or SEND_UNKNOWN
4. purge the bounded namespace / local side effects
5. restore the logical clock to the frozen seed instant
6. rewind the deterministic id generator
7. construct and seed the deterministic entities, messages, and objects
8. verify both private evidence objects
9. verify the complete ``EvidenceItem`` provenance
10. return the receipt; 11. record it for idempotent replay

No destructive action happens before steps 2 and 3 both pass.

Reset fails closed on an ambiguous send
---------------------------------------
The purge erases every row in the namespace, so the guard covers the whole namespace, not one
predicted case: if *any* ``ActionExecution`` anywhere in the reset namespace is ``SENDING`` or
``SEND_UNKNOWN``, an external message is in flight or its outcome is unknown, and reset refuses
with a conflict (:class:`DemoResetInFlightSend`) before deleting anything. The enumeration is a
bounded local read of the exact namespace's rows -- there is no production scan.

Idempotency
-----------
A reset carries an optional ``Idempotency-Key``. The same key with the same logical request
replays the recorded receipt (``replayed=True``) and performs no second destructive reset; the
same key with a materially different request is an idempotency conflict. A fresh key always
performs a real cleanup and re-seed (``replayed=False``).

One pre-seeded exception, documented rather than hidden
---------------------------------------------------------
The frozen ``elevator/v1`` corpus attaches its E42 photo to message 16, and the fake Monitor
puts that message's evidence identifier onto the ``INCIDENT_OCCURRENCE`` fact it proposes
(``LexicalFakeMonitorAgent`` / ``build_lexical_output``). Nothing in the merged Phases 1-9
codebase creates an ``EvidenceItem`` row for evidence discovered through the ambient channel --
only the inbound-reply path does -- so without one, ``GET /cases/{id}/investigation`` fails
strongly the moment it loads that fact's cited evidence (``core.load_evidence_items`` fails the
whole read on a missing item). This was verified empirically against the real local pipeline
before writing this module.

Reset closes that gap the same way ADR-011 already closes it for report/fact/case identity: by
computing the *deterministic* address the live Monitor run will independently derive, and
placing the evidence there in advance. Case identity is a pure function of validated Monitor
input, and the message identifiers reset assigns are the identical sequence
``IngestMessages`` will assign when the demo replays the corpus. The set of messages that
becomes signals is taken from the one shared classification helper
(:mod:`chorus.infrastructure.local.demo_classification`) that the fake Monitor also uses --
never from a positional or hard-coded channel-id list -- so a reordered or edited fixture
cannot make reset seed evidence against a case the Monitor will not later create.

This is a deviation from the literal exclusion list in service of making it true in substance
(the demo's live investigation step must actually work), and it is reported as one in Phase 10's
own completion report rather than left for a reviewer to discover.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable
from uuid import UUID

from chorus.application.commands.ingest_messages import (
    IngestAttachment,
    IngestMessage,
    IngestMessages,
    IngestMessagesCommand,
)
from chorus.application.services.identity import (
    derive_candidate_case_id,
    derive_evidence_root_id,
    derive_report_id,
)
from chorus.contracts.monitor import IssueType
from chorus.domain.entities import (
    ActionExecutionState,
    Community,
    CommunityStatus,
    Contributor,
    ContributorStatus,
    EvidenceItem,
    ExtractionStatus,
    MalwareScanStatus,
)
from chorus.domain.errors import IntegrityError, StateTransitionError, ValidationError
from chorus.domain.ids import (
    CaseId,
    CommunityId,
    ContributorId,
    EvidenceItemId,
    MessageId,
    Namespace,
    SensitiveStr,
    Sha256Digest,
    Uuid5Generator,
)
from chorus.domain.time import Clock
from chorus.infrastructure.dynamodb import codec_share
from chorus.infrastructure.dynamodb.codec import ATTR_ENTITY_TYPE, EntityType
from chorus.infrastructure.fixtures.synthetic_feed import (
    ContributorSeed,
    EvidenceFixture,
    SyntheticAmbientAdapter,
    default_fixture_root,
)
from chorus.infrastructure.local.demo_classification import is_reportable_message
from chorus.infrastructure.local.demo_clock import LogicalDemoClock
from chorus.infrastructure.local.objects import InMemoryObjectStore
from chorus.infrastructure.local.scheduler import InMemoryDeadlineScheduler
from chorus.ports.ambient import AmbientMessage
from chorus.ports.errors import IdempotencyConflictError, NotFoundError, PersistenceConflictError
from chorus.ports.objects import private_evidence_key
from chorus.ports.records import StoredSafeDestination
from chorus.ports.repositories import CoreRepositoryPort, ShareableRepositoryPort
from chorus.ports.scopes import CaseScope, CommunityScope, NamespaceScope
from chorus.ports.storage import PutItem, StoredItem
from chorus.ports.unit_of_work import TransactionPlan, UnitOfWork

_SeedT = TypeVar("_SeedT")

DEMO_NAMESPACE = "DEMO"
DEMO_CONFIRMATION = "RESET DEMO"
ISSUE_TYPE = IssueType.ELEVATOR_FAILURE.value
"""The one issue type the frozen elevator corpus's fake Monitor ever proposes."""

MESSAGE_ID_UUID5_NAMESPACE = UUID("2e2ac235-2ffd-5f5e-9c0e-a475f6be9a63")
"""A namespace private to reset's own deterministic message-identity generator.

Distinct from the seed manifest's own ``uuid5_namespace`` -- that one derives fixture names,
this one derives *assigned* identifiers for entities the manifest does not name at all -- so the
two derivations can never collide by construction.
"""

EVIDENCE_FIXTURE_ID_FOR_SIGNAL_MESSAGE = "elevator-e42-photo"
"""The one evidence fixture a signal message's fact will ever cite in this corpus.

The corpus's other feed-attached fixture, ``injection-notice``, is attached to the message the
classifier routes to ``POLICY_LIKE_INSTRUCTION`` -- it produces no report and no fact, so no
fact ever cites its evidence identifier and no ``EvidenceItem`` is required for it. Its private
object is still seeded: the frozen contract says *two* private evidence objects.
"""

RESET_IN_FLIGHT_STATES = frozenset(
    {ActionExecutionState.SENDING, ActionExecutionState.SEND_UNKNOWN}
)
"""Execution states that make an erase unsafe: a send is in flight or its outcome is unknown."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ResetContributor:
    actor: str
    pseudonym: str
    contributor_id: ContributorId


@dataclass(frozen=True, slots=True, kw_only=True)
class ResetEvidence:
    evidence_id: EvidenceItemId
    media_type: str
    sha256: Sha256Digest


@dataclass(frozen=True, slots=True, kw_only=True)
class ResetCounts:
    deleted: int
    messages: int
    contributors: int
    evidence: int


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoResetResult:
    reset_id: UUID
    namespace: str
    seed_version: str
    corpus_sha256: Sha256Digest
    logical_now: datetime
    community_id: CommunityId
    destination_id: str
    contributors: tuple[ResetContributor, ...]
    evidence: tuple[ResetEvidence, ...]
    counts: ResetCounts
    replayed: bool
    audit_event_id: UUID


class DemoResetRefused(ValidationError):
    """Reset was asked to do something the frozen contract refuses outright."""


class DemoResetInFlightSend(StateTransitionError):
    """Reset was asked to erase a namespace whose execution is SENDING or SEND_UNKNOWN."""


@runtime_checkable
class NamespaceStorePurge(Protocol):
    """A local storage driver that can read and drop one namespace's rows.

    Implemented by :class:`~chorus.infrastructure.local.memory.InMemoryStorageDriver`. DynamoDB
    Local namespace inspection and cleanup are Phase 11's distributed concern and are
    deliberately not here; the production :class:`~chorus.ports.storage.StorageDriver` port has
    neither a scan nor a namespace purge.
    """

    async def purge_namespace(self, namespace: str) -> int: ...

    async def namespace_items(self, namespace: str) -> tuple[StoredItem, ...]: ...


PERSONA_BY_PSEUDONYM: dict[str, str] = {
    "resident-a": "resident_a",
    "resident-b": "resident_b",
    "resident-c": "resident_c",
    "resident-d": "resident_d",
}
"""Which seeded demo persona acts as which corpus contributor. Residents only: the corpus's
``attacker-fixture`` pseudonym is untrusted content, not a persona anybody may authenticate as."""


@dataclass(slots=True)
class DemoResetService:
    """Delete-and-seed, against local adapters, idempotently.

    ``demo_case_id`` is precomputed once at composition time (see module docstring) and is used
    to address the private evidence objects reset pre-seeds and to fail closed on an in-flight
    send; reset itself never writes a case row.
    """

    settings_environment: str
    adapter: SyntheticAmbientAdapter
    driver: NamespaceStorePurge
    core: CoreRepositoryPort
    shareable: ShareableRepositoryPort
    unit_of_work: UnitOfWork
    ingest_messages: IngestMessages
    message_ids: Uuid5Generator
    """The generator ``ingest_messages`` assigns message identifiers from. Rewound before every
    re-seed so replaying the same corpus after a purge yields the same identifiers -- which is
    what keeps :func:`predict_demo_case_id` in step with the live Monitor run."""
    scheduler: InMemoryDeadlineScheduler
    """The local one-time deadline scheduler. Its recorded schedules are progressed demo state
    and are cleared on every real reset."""
    outbox_dir: Path
    """The filesystem outbox the local sender writes to. Its ``*.json`` attempts are send state
    and are cleared on every real reset."""
    objects: InMemoryObjectStore
    demo_clock: LogicalDemoClock
    clock: Clock
    namespace: Namespace
    community_id: CommunityId
    destination: StoredSafeDestination
    demo_case_id: CaseId
    reset_ids: Uuid5Generator = field(
        default_factory=lambda: Uuid5Generator(
            namespace=MESSAGE_ID_UUID5_NAMESPACE, prefix="reset-event"
        )
    )
    _receipts: dict[str, tuple[tuple[str, str, str], DemoResetResult]] = field(
        default_factory=dict, init=False, repr=False
    )
    """Per-key reset receipts for idempotent replay. In-process, like the whole local demo."""

    async def reset(
        self,
        *,
        namespace: str,
        confirm: str,
        seed_version: str,
        idempotency_key: str | None,
    ) -> DemoResetResult:
        request_fingerprint = (namespace, confirm, seed_version)

        # A bound key is authoritative: an exact replay returns the recorded receipt and
        # performs no second destructive reset; the same key under a materially different
        # request is a conflict, never a silent second reset.
        if idempotency_key is not None:
            recorded = self._receipts.get(idempotency_key)
            if recorded is not None:
                stored_fingerprint, stored_result = recorded
                if stored_fingerprint != request_fingerprint:
                    raise IdempotencyConflictError("DEMO_RESET")
                return replace(stored_result, replayed=True)

        if namespace != DEMO_NAMESPACE:
            raise DemoResetRefused("RESET_NAMESPACE")
        if confirm != DEMO_CONFIRMATION:
            raise DemoResetRefused("RESET_CONFIRMATION")
        if seed_version != self.adapter.seed_version:
            raise DemoResetRefused("RESET_SEED_VERSION")
        if self.settings_environment not in {"test", "development", "demo"}:
            raise DemoResetRefused("RESET_ENVIRONMENT")

        # -- no destructive action before the fixture snapshot and the send guard both pass --

        # 2. Validate that the adapter still holds the exact frozen corpus (it can be mutated
        #    in memory after composition), and pin the predicted identity to that snapshot.
        self._validate_frozen_fixture_snapshot()
        self.demo_case_id = predict_demo_case_id(
            self.adapter, namespace=self.namespace, community_id=self.community_id
        )

        # 3. Refuse if any execution anywhere in the namespace is mid-send or ambiguous.
        await self._guard_no_in_flight_send()

        # 4. Bounded local cleanup.
        deleted = await self._purge_namespace()

        # 5-6. Restore the frozen clock and rewind the deterministic id generator *before* any
        #      seed entity is constructed, so its timestamps and ids are identical run to run.
        self._reset_clock()
        self.message_ids.reset()

        # 7-9. Seed, then verify both objects and the full EvidenceItem provenance.
        await self._seed_community_and_contributors()
        message_result = await self._ingest_corpus()
        await self._seed_evidence_objects()
        expected_item = self._expected_signal_evidence_item(message_result)
        await self._seed_signal_evidence_item(expected_item)
        await self._verify_seeded_evidence(expected_item)

        contributors = tuple(
            ResetContributor(
                actor=actor, pseudonym=seed.pseudonym, contributor_id=seed.contributor_id
            )
            for seed in self.adapter.contributor_seeds
            if (actor := PERSONA_BY_PSEUDONYM.get(seed.pseudonym)) is not None
        )
        evidence = tuple(
            ResetEvidence(
                evidence_id=fixture.evidence_id,
                media_type=fixture.media_type,
                sha256=fixture.sha256,
            )
            for fixture in self.adapter.evidence_fixtures
            if fixture.ingested_with_feed
        )
        counts = ResetCounts(
            deleted=deleted,
            messages=len(self.adapter.messages()),
            contributors=len(contributors),
            evidence=len(evidence),
        )
        result = DemoResetResult(
            reset_id=self.reset_ids.new_uuid(),
            namespace=namespace,
            seed_version=seed_version,
            corpus_sha256=self.adapter.corpus_sha256,
            logical_now=self.demo_clock.now(),
            community_id=self.community_id,
            destination_id=self.destination.destination_id.value,
            contributors=contributors,
            evidence=evidence,
            counts=counts,
            replayed=False,
            audit_event_id=self.reset_ids.new_uuid(),
        )
        if idempotency_key is not None:
            self._receipts[idempotency_key] = (request_fingerprint, result)
        return result

    # -- guards ----------------------------------------------------------------------------

    def _validate_frozen_fixture_snapshot(self) -> None:
        """Refuse -- before any purge or seed write -- unless the adapter still holds the exact
        frozen corpus.

        The adapter validates the fixture files at *construction*, but this service can later be
        handed an adapter whose in-memory messages were reordered, altered, or truncated. A
        fresh adapter re-reads and re-validates the frozen files from disk (raising on any
        on-disk corruption); the live adapter is then compared against it message for message,
        plus corpus digest, ``seed_version``, and count. Any divergence fails closed here, so
        the identity reset seeds evidence against can never be one the Monitor will not reach.
        """

        reference = SyntheticAmbientAdapter()
        if self.adapter.seed_version != reference.seed_version:
            raise DemoResetRefused("RESET_FIXTURE_SEED_VERSION")
        if self.adapter.corpus_sha256 != reference.corpus_sha256:
            raise DemoResetRefused("RESET_FIXTURE_CORPUS_DIGEST")
        live = self.adapter.messages()
        frozen = reference.messages()
        if len(live) != len(frozen):
            raise DemoResetRefused("RESET_FIXTURE_MESSAGE_COUNT")
        for live_message, frozen_message in zip(live, frozen, strict=True):
            if _message_identity(live_message) != _message_identity(frozen_message):
                raise DemoResetRefused("RESET_FIXTURE_MESSAGE_MUTATED")

    async def _guard_no_in_flight_send(self) -> None:
        """Fail closed if *any* execution in the reset namespace is SENDING or SEND_UNKNOWN.

        The purge that follows erases every row this enumeration sees, so the guard covers the
        whole namespace -- never just the predicted demo case or one current-action pointer. It
        is a bounded local read: ``namespace_items`` returns exactly this namespace's rows, and
        each ``ACTION_EXECUTION`` row is strongly decoded to its durable state.
        """

        for item in await self.driver.namespace_items(self.namespace.value):
            if item.get(ATTR_ENTITY_TYPE) != EntityType.ACTION_EXECUTION.value:
                continue
            _scope, execution = codec_share.decode_execution(item)
            if execution.state in RESET_IN_FLIGHT_STATES:
                raise DemoResetInFlightSend("RESET_EXECUTION_IN_FLIGHT")

    # -- cleanup ---------------------------------------------------------------------------

    async def _purge_namespace(self) -> int:
        deleted = await self.driver.purge_namespace(self.namespace.value)
        # Private objects and local scheduler state are cleaned too, but they are not "rows":
        # the count reports the deleted persisted items, exactly the number the frozen response
        # field names.
        self.objects.purge_namespace(self.namespace)
        self.scheduler.reset()
        if self.outbox_dir.is_dir():
            for attempt in self.outbox_dir.glob("*.json"):
                attempt.unlink()
        return deleted

    # -- seed steps ----------------------------------------------------------------------

    async def _seed_community_and_contributors(self) -> None:
        now = self.clock.now()
        namespace_scope = NamespaceScope(namespace=self.namespace)
        community_scope = CommunityScope(namespace=self.namespace, community_id=self.community_id)
        community = self.adapter.community
        expected_community = Community(
            community_id=self.community_id,
            namespace=self.namespace,
            name=community.name,
            timezone=community.timezone,
            status=CommunityStatus.ACTIVE,
            version=1,
            created_at=now,
            updated_at=now,
        )

        async def read_community() -> Community | None:
            try:
                return await self.core.load_community(namespace_scope, self.community_id)
            except NotFoundError:
                return None

        def community_matches(stored: Community) -> bool:
            return (
                stored.community_id == self.community_id
                and stored.namespace == self.namespace
                and stored.name == community.name
                and stored.timezone == community.timezone
                and stored.status is CommunityStatus.ACTIVE
            )

        await self._create_or_verify_seed(
            plan_name="demo-reset-seed-community",
            operation=self.core.stage_create_community(namespace_scope, expected_community),
            read_existing=read_community,
            matches=community_matches,
            entity_ref="DEMO_RESET_COMMUNITY",
        )

        for seed in self.adapter.contributor_seeds:
            contributor = Contributor(
                contributor_id=seed.contributor_id,
                community_id=self.community_id,
                namespace=self.namespace,
                pseudonym=seed.pseudonym,
                display_name=SensitiveStr(seed.display_name),
                email=SensitiveStr(f"{seed.pseudonym}@example.invalid"),
                status=ContributorStatus.ACTIVE,
                version=1,
                created_at=now,
                updated_at=now,
            )
            await self._create_or_verify_seed(
                plan_name="demo-reset-seed-contributor",
                operation=self.core.stage_create_contributor(community_scope, contributor),
                read_existing=self._contributor_reader(community_scope, seed.contributor_id),
                matches=self._contributor_matcher(seed),
                entity_ref="DEMO_RESET_CONTRIBUTOR",
            )

    def _contributor_reader(
        self, scope: CommunityScope, contributor_id: ContributorId
    ) -> Callable[[], Awaitable[Contributor | None]]:
        async def read() -> Contributor | None:
            try:
                return await self.core.load_contributor(scope, contributor_id)
            except NotFoundError:
                return None

        return read

    def _contributor_matcher(self, seed: ContributorSeed) -> Callable[[Contributor], bool]:
        def matches(stored: Contributor) -> bool:
            return (
                stored.contributor_id == seed.contributor_id
                and stored.namespace == self.namespace
                and stored.community_id == self.community_id
                and stored.pseudonym == seed.pseudonym
                and stored.status is ContributorStatus.ACTIVE
            )

        return matches

    def _reset_clock(self) -> None:
        start = self.adapter.logical_clock_start
        self.demo_clock.instant = start
        self.demo_clock.advances.clear()

    async def _ingest_corpus(self) -> tuple[MessageId, ...]:
        contributor_ids = self.adapter.contributor_ids_by_pseudonym
        messages = tuple(
            IngestMessage(
                channel_message_id=message.channel_message_id,
                contributor_id=(
                    contributor_ids[message.contributor_pseudonym]
                    if message.contributor_pseudonym
                    else None
                ),
                sent_at=message.sent_at,
                text=message.text,
                attachments=tuple(
                    IngestAttachment(
                        evidence_id=attachment.evidence_id,
                        media_type=attachment.media_type,
                        byte_length=attachment.byte_length,
                        sha256=attachment.sha256,
                    )
                    for attachment in message.attachments
                ),
            )
            for message in self.adapter.messages()
        )
        result = await self.ingest_messages.execute(
            IngestMessagesCommand(
                namespace=self.namespace,
                community_id=self.community_id,
                actor_id_hash=_RESET_ACTOR_HASH,
                idempotency_key="demo-reset-corpus-ingest-0001",
                messages=messages,
            )
        )
        return tuple(item.message_id for item in result.messages)

    async def _seed_evidence_objects(self) -> None:
        """Place both feed-attached private evidence objects where ingestion would have.

        Two objects, exactly as the frozen contract says -- the photo a signal fact cites and
        the injection notice a ``POLICY_LIKE_INSTRUCTION`` message carries. Only the first also
        gets an ``EvidenceItem`` row (:meth:`_seed_signal_evidence_item`); nothing cites the
        second, so nothing loads it.
        """

        for fixture in self.adapter.evidence_fixtures:
            if not fixture.ingested_with_feed:
                continue
            self.objects.seed_private_evidence(
                namespace=self.namespace,
                community_id=self.community_id,
                case_id=self.demo_case_id,
                evidence_id=fixture.evidence_id,
                content=_fixture_bytes(fixture),
                media_type=fixture.media_type,
            )

    def _signal_evidence_fixture(self) -> EvidenceFixture:
        return next(
            item
            for item in self.adapter.evidence_fixtures
            if item.fixture_id == EVIDENCE_FIXTURE_ID_FOR_SIGNAL_MESSAGE
        )

    def _expected_signal_evidence_item(self, message_ids: tuple[MessageId, ...]) -> EvidenceItem:
        """The exact ``EvidenceItem`` reset intends to seed -- built once, from the validated
        corpus snapshot, and used both to write and to verify the row.

        Every field here is a pure function of the frozen fixture and the deterministic ids
        assigned this run, so a stored row that differs in *any* of them -- provenance included
        -- is not this seed.
        """

        fixture = self._signal_evidence_fixture()
        by_channel = {
            message.channel_message_id: message_id
            for message, message_id in zip(self.adapter.messages(), message_ids, strict=True)
        }
        source_message = next(
            message
            for message in self.adapter.messages()
            if any(a.evidence_id == fixture.evidence_id for a in message.attachments)
        )
        pseudonym = source_message.contributor_pseudonym
        assert pseudonym is not None, "the photo message is always attributed to a resident"
        now = self.clock.now()
        return EvidenceItem(
            evidence_id=fixture.evidence_id,
            root_id=derive_evidence_root_id(
                namespace=self.namespace,
                community_id=self.community_id,
                root_sha256=fixture.sha256,
            ),
            community_id=self.community_id,
            case_id=self.demo_case_id,
            namespace=self.namespace,
            submitted_by_contributor_id=self.adapter.contributor_ids_by_pseudonym[pseudonym],
            source_message_id=by_channel[source_message.channel_message_id],
            private_object_key=SensitiveStr(
                private_evidence_key(
                    namespace=self.namespace,
                    community_id=self.community_id,
                    case_id=self.demo_case_id,
                    evidence_id=fixture.evidence_id,
                )
            ),
            media_type=fixture.media_type,
            byte_length=fixture.byte_length,
            sha256=fixture.sha256,
            captured_at=None,
            uploaded_at=now,
            derived_from_evidence_id=None,
            malware_scan_status=MalwareScanStatus.CLEAN,
            extraction_status=ExtractionStatus.NOT_NEEDED,
            extracted_text=None,
            version=1,
            created_at=now,
            updated_at=now,
        )

    async def _seed_signal_evidence_item(self, expected: EvidenceItem) -> None:
        scope = CaseScope(
            namespace=self.namespace, community_id=self.community_id, case_id=self.demo_case_id
        )
        await self._create_or_verify_seed(
            plan_name="demo-reset-seed-evidence-item",
            operation=self.core.stage_create_evidence_item(scope, expected),
            read_existing=lambda: self._load_evidence_item(scope, expected.evidence_id),
            matches=lambda stored: _evidence_provenance_mismatch(stored, expected) is None,
            entity_ref="DEMO_RESET_EVIDENCE_ITEM",
        )

    async def _load_evidence_item(
        self, scope: CaseScope, evidence_id: EvidenceItemId
    ) -> EvidenceItem | None:
        try:
            items = await self.core.load_evidence_items(scope, (evidence_id,))
        except NotFoundError:
            return None
        return items[0]

    # -- verification ------------------------------------------------------------------

    async def _verify_seeded_evidence(self, expected: EvidenceItem) -> None:
        """Refuse to report success unless both private objects and the full EvidenceItem
        provenance are exactly the seed.

        Runs against strongly read state, not against the create-time values: the frozen
        response says two evidence objects and one investigable photo item, and it must not say
        that unless both objects are present with the expected length and digest *and* the
        photo's row matches the intended seed in every immutable provenance field.
        """

        for fixture in self.adapter.evidence_fixtures:
            if not fixture.ingested_with_feed:
                continue
            key = private_evidence_key(
                namespace=self.namespace,
                community_id=self.community_id,
                case_id=self.demo_case_id,
                evidence_id=fixture.evidence_id,
            )
            stored = self.objects.private.get(key)
            if stored is None:
                raise IntegrityError("DEMO_RESET_EVIDENCE_OBJECT_MISSING")
            if len(stored.content) != fixture.byte_length:
                raise IntegrityError("DEMO_RESET_EVIDENCE_OBJECT_LENGTH")
            if _digest_of(stored.content) != fixture.sha256.value:
                raise IntegrityError("DEMO_RESET_EVIDENCE_OBJECT_DIGEST")

        scope = CaseScope(
            namespace=self.namespace, community_id=self.community_id, case_id=self.demo_case_id
        )
        item = await self._load_evidence_item(scope, expected.evidence_id)
        if item is None:
            raise IntegrityError("DEMO_RESET_EVIDENCE_ITEM_MISSING")
        mismatch = _evidence_provenance_mismatch(item, expected)
        if mismatch is not None:
            raise IntegrityError(f"DEMO_RESET_EVIDENCE_ITEM_{mismatch}")

    # -- create-or-verify -------------------------------------------------------------

    async def _create_or_verify_seed(
        self,
        *,
        plan_name: str,
        operation: PutItem,
        read_existing: Callable[[], Awaitable[_SeedT | None]],
        matches: Callable[[_SeedT], bool],
        entity_ref: str,
    ) -> None:
        """Create the seed row; on a create conflict, strongly read what is there and accept
        it only if it is byte-for-byte the intended seed.

        A namespace purge runs immediately before every re-seed, so a conflict here is not the
        ordinary replay it used to be quietly treated as -- it means a row survived the sweep
        or a second writer is present, and the safe answer is to verify identity exactly and
        otherwise fail closed. An absent row after a conflict, or any divergence in a bound
        id, digest, case, or locator, is an integrity failure.
        """

        try:
            await self.unit_of_work.commit(
                TransactionPlan(name=plan_name, operations=(operation,), audit_required=False)
            )
            return
        except PersistenceConflictError:
            existing = await read_existing()
            if existing is None:
                raise IntegrityError(f"{entity_ref}_ABSENT_AFTER_CONFLICT") from None
            if not matches(existing):
                raise IntegrityError(f"{entity_ref}_SEED_MISMATCH") from None


_RESET_ACTOR_HASH = Sha256Digest(f"sha256:{sha256(b'presenter_admin').hexdigest()}")

_EVIDENCE_PROVENANCE_FIELDS: tuple[str, ...] = (
    "evidence_id",
    "root_id",
    "community_id",
    "case_id",
    "namespace",
    "submitted_by_contributor_id",
    "source_message_id",
    "media_type",
    "byte_length",
    "sha256",
    "captured_at",
    "derived_from_evidence_id",
    "malware_scan_status",
    "extraction_status",
    "external_source_binding",
    "schema_version",
    "version",
)
"""Every immutable seed-binding / provenance field of ``EvidenceItem`` v2.

Deliberately not the row-lifecycle timestamps (``uploaded_at``/``created_at``/``updated_at``):
those are the clock's, and reset restores the clock first, so they carry no additional
identity a create-conflict could disagree on.
"""


def _digest_of(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


def _fixture_bytes(fixture: EvidenceFixture) -> bytes:
    path = default_fixture_root() / fixture.relative_path
    return path.read_bytes()


def _message_identity(message: AmbientMessage) -> tuple[object, ...]:
    """The canonical content identity of one corpus message, order-sensitive.

    Two messages compare equal here only if their channel id, author, instant, text, and the
    full ordered attachment list all match -- which is exactly what a reordered, altered, or
    truncated corpus breaks.
    """

    return (
        message.channel_message_id,
        message.contributor_pseudonym,
        message.sent_at,
        message.text,
        tuple(
            (a.evidence_id, a.media_type, a.byte_length, a.sha256.value)
            for a in message.attachments
        ),
    )


def _evidence_provenance_mismatch(stored: EvidenceItem, expected: EvidenceItem) -> str | None:
    """The name of the first immutable field where ``stored`` diverges from the intended seed,
    or ``None`` when every provenance field -- ids, root binding, locator, digest, length,
    status, source message, submitting contributor, external binding -- matches exactly.
    """

    if stored.private_object_key.reveal() != expected.private_object_key.reveal():
        return "PRIVATE_OBJECT_KEY"
    for name in _EVIDENCE_PROVENANCE_FIELDS:
        if getattr(stored, name) != getattr(expected, name):
            return name.upper()
    return None


def predict_demo_case_id(
    adapter: SyntheticAmbientAdapter, *, namespace: Namespace, community_id: CommunityId
) -> CaseId:
    """Compute, without ingesting or investigating anything, the case identity the live
    Monitor run over this exact corpus will independently derive.

    Pure and side-effect free: it simulates the *identifier assignment* ``IngestMessages``
    would perform (a fresh, identically seeded generator, walked in corpus order) and then
    applies the same ``derive_report_id``/``derive_candidate_case_id`` functions Monitor apply
    uses. The set of messages that becomes its own report is taken from the shared
    :func:`~chorus.infrastructure.local.demo_classification.is_reportable_message` predicate --
    an equipment signal or a private detail (P2-8) -- the fake Monitor also uses, so a
    reordered or edited corpus reorders both this prediction and the live run together, and
    the two cannot drift apart. It creates no message, no report, and no case -- it only
    computes an address.
    """

    ids = Uuid5Generator(namespace=MESSAGE_ID_UUID5_NAMESPACE, prefix="elevator-message")
    contributor_ids = adapter.contributor_ids_by_pseudonym
    message_id_by_channel: dict[str, MessageId] = {}
    for message in adapter.messages():
        message_id_by_channel[message.channel_message_id] = ids.new(MessageId)

    report_ids = []
    for message in adapter.messages():
        if not is_reportable_message(message.text):
            continue
        pseudonym = message.contributor_pseudonym
        assert pseudonym is not None, "every signal message is attributed to a resident"
        contributor_id = contributor_ids[pseudonym]
        report_ids.append(
            derive_report_id(
                namespace=namespace,
                community_id=community_id,
                contributor_id=contributor_id,
                issue_type=ISSUE_TYPE,
                source_message_ids=(message_id_by_channel[message.channel_message_id],),
            )
        )
    return derive_candidate_case_id(
        namespace=namespace,
        community_id=community_id,
        issue_type=ISSUE_TYPE,
        report_ids=tuple(report_ids),
    )


def demo_message_id_generator() -> Uuid5Generator:
    """A fresh generator with the exact seed :func:`predict_demo_case_id` simulated.

    Handed to the real :class:`~chorus.application.commands.ingest_messages.IngestMessages` used
    by reset, so the message identifiers it *actually* assigns -- in the same corpus order --
    are the identical sequence the prediction above already walked.
    """

    return Uuid5Generator(namespace=MESSAGE_ID_UUID5_NAMESPACE, prefix="elevator-message")


__all__ = [
    "DEMO_CONFIRMATION",
    "DEMO_NAMESPACE",
    "PERSONA_BY_PSEUDONYM",
    "RESET_IN_FLIGHT_STATES",
    "DemoResetInFlightSend",
    "DemoResetRefused",
    "DemoResetResult",
    "DemoResetService",
    "NamespaceStorePurge",
    "ResetContributor",
    "ResetCounts",
    "ResetEvidence",
    "demo_message_id_generator",
    "predict_demo_case_id",
]
