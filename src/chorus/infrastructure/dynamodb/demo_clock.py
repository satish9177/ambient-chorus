"""The deployed demo clock's durable row: a strong read, and one guarded forward write.

The adapter half of [ADR-029](../../../../docs/adr/ADR-029-deployed-demo-clock-authority.md).
It owns exactly one item -- ``NS#{namespace}#CLOCK`` / ``DEMO_CLOCK`` in the **Shareable**
table -- and it can do exactly two things to it: read it strongly, and move it forward under
three stored conditions.

Why the instants are stored twice
----------------------------------
``logical_time`` and ``seed_instant`` are persisted both as the canonical RFC 3339 strings every
other instant in the system uses **and** as exact epoch microseconds. The strings are what a
person reads and what every decoder parses; the microsecond twins exist because ADR-029 SS 3
makes "the new reading is later than the stored one" a **condition the store evaluates**, and
DynamoDB compares numbers, not RFC 3339 text. The two are cross-checked on decode, so a row
whose halves disagree is refused rather than silently preferring one -- the same pairing, for
the same reason, that the send fence already uses for ``expires_at``/``expires_at_micros``.

What is deliberately absent
----------------------------
There is **no reset, no reseed, and no unconditional write**. Restoring the seed instant is the
one legitimate backward transition, it happens behind ``DEMO_RESET_LOCK``, and it belongs to the
dedicated reset principal (ADR-029 SS 3) -- so it is not reachable from the type the API holds.
A test asserts that this class exposes no such method.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from chorus.domain.errors import IntegrityError
from chorus.domain.ids import Namespace
from chorus.domain.time import epoch_micros, require_utc
from chorus.infrastructure.dynamodb import keys
from chorus.infrastructure.dynamodb.codec import (
    EntityType,
    ItemReader,
    build_entity,
    envelope,
    instant,
    read_envelope,
)
from chorus.ports.demo_clock import (
    DemoClockConflictError,
    DemoClockNotAdvancedError,
    DemoClockRecord,
    DemoClockResetConflictError,
    DemoClockUnavailableError,
)
from chorus.ports.errors import PersistenceConflictError, PersistenceError
from chorus.ports.storage import (
    AllOf,
    AttributeEqualsNumber,
    AttributeLessThanNumber,
    ItemKey,
    KeyAbsent,
    PutItem,
    StorageDriver,
    StoredItem,
    StoredValue,
    TableName,
)

DEMO_CLOCK_SCHEMA_VERSION: Final = "demo-clock/v1"
DEMO_CLOCK_SCHEMA_VERSIONS: Final = frozenset({DEMO_CLOCK_SCHEMA_VERSION})

ATTR_LOGICAL_TIME: Final = "logical_time"
ATTR_LOGICAL_TIME_MICROS: Final = "logical_time_micros"
"""The exact reading every forward condition compares against."""
ATTR_VERSION: Final = "version"
ATTR_RESET_GENERATION: Final = "reset_generation"
ATTR_SEED_INSTANT: Final = "seed_instant"
ATTR_SEED_INSTANT_MICROS: Final = "seed_instant_micros"
ATTR_ADVANCE_COUNT: Final = "advance_count"

_ENTITY_REF: Final = "DEMO_CLOCK"


def demo_clock_key(namespace: Namespace) -> ItemKey:
    """The one address a deployed clock ever has."""

    return ItemKey(
        table=TableName.SHAREABLE,
        partition_key=keys.demo_clock_partition(namespace),
        sort_key=keys.DEMO_CLOCK_SORT_KEY,
    )


def encode_demo_clock(namespace: Namespace, record: DemoClockRecord) -> StoredItem:
    """Serialize the clock row.

    The envelope names no community and no case, and that is the point ADR-029 SS 1 makes about
    where this item lives: **a timestamp is not private data**. There is no content here to
    scope to a case, so there is nothing a shareable-zone reader learns from it.
    """

    key = demo_clock_key(namespace)
    item: dict[str, StoredValue] = envelope(
        entity_type=EntityType.DEMO_CLOCK,
        schema_version=DEMO_CLOCK_SCHEMA_VERSION,
        key=key,
        namespace=namespace,
        community_id=None,
        case_id=None,
    )
    item.update(
        {
            ATTR_LOGICAL_TIME: instant(record.logical_time),
            ATTR_LOGICAL_TIME_MICROS: epoch_micros(record.logical_time),
            ATTR_VERSION: record.version,
            ATTR_RESET_GENERATION: record.reset_generation,
            ATTR_SEED_INSTANT: instant(record.seed_instant),
            ATTR_SEED_INSTANT_MICROS: epoch_micros(record.seed_instant),
            ATTR_ADVANCE_COUNT: record.advance_count,
        }
    )
    return item


def decode_demo_clock(namespace: Namespace, item: StoredItem) -> DemoClockRecord:
    """Parse the clock row exactly, or refuse it.

    ``ItemReader.finish`` rejects any attribute the decoder did not read, so a row that grew a
    field nobody declared is a corrupt clock rather than a clock with an extra field.
    """

    reader = ItemReader(item, entity_ref=_ENTITY_REF)
    scope, _ = read_envelope(
        reader,
        expected_type=EntityType.DEMO_CLOCK,
        accepted_schema_versions=DEMO_CLOCK_SCHEMA_VERSIONS,
    )
    if scope.namespace != namespace or scope.partition_key != keys.demo_clock_partition(namespace):
        raise IntegrityError(f"{_ENTITY_REF}:scope")
    logical_time = reader.instant(ATTR_LOGICAL_TIME)
    logical_micros = reader.number(ATTR_LOGICAL_TIME_MICROS)
    version = reader.number(ATTR_VERSION)
    reset_generation = reader.number(ATTR_RESET_GENERATION)
    seed_instant = reader.instant(ATTR_SEED_INSTANT)
    seed_micros = reader.number(ATTR_SEED_INSTANT_MICROS)
    advance_count = reader.number(ATTR_ADVANCE_COUNT)
    reader.finish()
    if epoch_micros(logical_time) != logical_micros or epoch_micros(seed_instant) != seed_micros:
        # The condition attribute and the readable attribute are the same instant or the row is
        # not a clock. Preferring either one would make the value a condition compares against
        # differ from the value a reader believes.
        raise IntegrityError(f"{_ENTITY_REF}:instant_disagreement")
    return build_entity(
        _ENTITY_REF,
        DemoClockRecord,
        logical_time=logical_time,
        version=version,
        reset_generation=reset_generation,
        seed_instant=seed_instant,
        advance_count=advance_count,
    )


@dataclass(frozen=True, slots=True)
class DynamoDbDemoClockStore:
    """The one authoritative clock, behind the storage driver every other adapter uses.

    ``namespace`` is composition configuration and never a caller's field. A store that took the
    namespace per call would be a store whose grant -- an exact ``dynamodb:LeadingKeys`` literal
    -- could be aimed at a partition the deployment does not have.
    """

    driver: StorageDriver
    namespace: Namespace

    async def read(self) -> DemoClockRecord:
        """Strongly read the clock, or fail closed.

        Strong on every path, always: an eventually consistent read of the authority a deadline
        is judged against is a deadline judged against a guess (ADR-029 SS 4).
        """

        try:
            item = await self.driver.get_item(demo_clock_key(self.namespace), consistent=True)
        except PersistenceError as error:
            raise DemoClockUnavailableError("the demo clock could not be read") from error
        if item is None:
            raise DemoClockUnavailableError("the demo clock row does not exist")
        try:
            return decode_demo_clock(self.namespace, item)
        except (IntegrityError, ValueError) as error:
            raise DemoClockUnavailableError("the demo clock row is not a clock") from error

    async def advance(self, expected: DemoClockRecord, *, to: datetime) -> DemoClockRecord:
        """Apply the guarded forward CAS of ADR-029 SS 3, and return what is now stored.

        Three stored conditions, and the store evaluates all three:

        * ``version == expected.version`` -- ordinary optimistic concurrency;
        * ``reset_generation == expected.reset_generation`` -- the fence that makes the version
          safe across a reset, which restarts the version sequence;
        * ``logical_time_micros < to`` -- the forward rule, so a clock that went backwards is
          refused by the table rather than by whichever process happened to check.

        The whole item is rewritten because the driver has no attribute-level update path, by
        design (:mod:`chorus.ports.storage`). ``seed_instant`` and ``reset_generation`` are
        carried through unchanged: a normal advance is not the reseed authority.
        """

        require_utc(to)
        if to <= expected.logical_time:
            raise DemoClockNotAdvancedError("the demo clock only moves forward")
        moved = DemoClockRecord(
            logical_time=to,
            version=expected.version + 1,
            reset_generation=expected.reset_generation,
            seed_instant=expected.seed_instant,
            advance_count=expected.advance_count + 1,
        )
        operation = PutItem(
            key=demo_clock_key(self.namespace),
            item=encode_demo_clock(self.namespace, moved),
            condition=AllOf(
                (
                    AttributeEqualsNumber(name=ATTR_VERSION, value=expected.version),
                    AttributeEqualsNumber(
                        name=ATTR_RESET_GENERATION, value=expected.reset_generation
                    ),
                    AttributeLessThanNumber(name=ATTR_LOGICAL_TIME_MICROS, value=epoch_micros(to)),
                )
            ),
        )
        try:
            await self.driver.write_item(operation)
        except PersistenceConflictError as error:
            # Definite: the condition was evaluated and it failed, so nothing was written.
            raise DemoClockConflictError("the demo clock moved under this advance") from error
        except PersistenceError as error:
            raise DemoClockUnavailableError("the demo clock could not be advanced") from error
        return moved


@dataclass(frozen=True, slots=True)
class DynamoDbDemoClockResetStore:
    """The reset principal's fenced reseed of the one clock row (ADR-029 § 3).

    Deliberately a different type from :class:`DynamoDbDemoClockStore` -- the normal adapter
    exposes ``read`` and ``advance`` and nothing else, and a test asserts that surface, so the
    one legitimate backward transition lives only here and is reachable only from the reset
    role. ``namespace`` is composition configuration, never a caller's field: a store that took
    it per call would be a store whose exact ``dynamodb:LeadingKeys`` literal could be aimed at
    a partition the deployment does not have.
    """

    driver: StorageDriver
    namespace: Namespace

    async def read(self) -> DemoClockRecord | None:
        """Strongly read the clock row. ``None`` when absent; fail closed when unparseable."""

        try:
            item = await self.driver.get_item(demo_clock_key(self.namespace), consistent=True)
        except PersistenceError as error:
            raise DemoClockUnavailableError("the demo clock could not be read") from error
        if item is None:
            return None
        try:
            return decode_demo_clock(self.namespace, item)
        except (IntegrityError, ValueError) as error:
            # Present but corrupt: reset never blind-overwrites a row it could not parse.
            raise DemoClockUnavailableError("the demo clock row is not a clock") from error

    async def reseed(
        self, *, seed_instant: datetime, current: DemoClockRecord | None
    ) -> DemoClockRecord:
        """Bump the generation, restore the seed instant, and start a fresh version sequence.

        One conditional whole-item put (the driver has no attribute-level update path). When
        ``current`` is ``None`` the condition is "the row does not exist" and the new
        generation is ``1``; otherwise it is ``current``'s exact ``version`` **and**
        ``reset_generation``, and the new generation is ``current.reset_generation + 1`` -- a
        value never previously stored, because the sequence only ever increases.
        """

        require_utc(seed_instant)
        new_generation = 1 if current is None else current.reset_generation + 1
        reseeded = DemoClockRecord(
            logical_time=seed_instant,
            version=1,
            reset_generation=new_generation,
            seed_instant=seed_instant,
            advance_count=0,
        )
        if current is None:
            condition: object = KeyAbsent()
        else:
            condition = AllOf(
                (
                    AttributeEqualsNumber(name=ATTR_VERSION, value=current.version),
                    AttributeEqualsNumber(
                        name=ATTR_RESET_GENERATION, value=current.reset_generation
                    ),
                )
            )
        operation = PutItem(
            key=demo_clock_key(self.namespace),
            item=encode_demo_clock(self.namespace, reseeded),
            condition=condition,  # type: ignore[arg-type]
        )
        try:
            await self.driver.write_item(operation)
        except PersistenceConflictError as error:
            raise DemoClockResetConflictError(
                "the demo clock moved between the reset read and the reseed"
            ) from error
        except PersistenceError as error:
            raise DemoClockUnavailableError("the demo clock could not be reseeded") from error
        return reseeded


__all__ = [
    "ATTR_ADVANCE_COUNT",
    "ATTR_LOGICAL_TIME",
    "ATTR_LOGICAL_TIME_MICROS",
    "ATTR_RESET_GENERATION",
    "ATTR_SEED_INSTANT",
    "ATTR_SEED_INSTANT_MICROS",
    "ATTR_VERSION",
    "DEMO_CLOCK_SCHEMA_VERSION",
    "DEMO_CLOCK_SCHEMA_VERSIONS",
    "DynamoDbDemoClockResetStore",
    "DynamoDbDemoClockStore",
    "decode_demo_clock",
    "demo_clock_key",
    "encode_demo_clock",
]
