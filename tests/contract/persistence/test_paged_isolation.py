"""Every paged read denies a foreign row as strictly as a direct get does.

A query names a partition, not an item, so a row that reached the right partition has already
passed the only check a key can perform. These tests plant a record whose *address* is exactly
the one the caller is entitled to read and whose *scope attributes* belong to somebody else,
then assert the whole page fails rather than the row being filtered out.

Storage-level revalidation is what these exercise. The decoded entity's own identifiers are
checked too, but for every paged entity the codec derives them from the stored envelope, so
that defence is asserted directly against the guard in ``test_a_disowning_entity_fails_its_page``
rather than pretended to be reachable through a codec that cannot produce it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.fixtures.persistence import (
    DEMO_RETENTION,
    OTHER_CASE,
    OTHER_NAMESPACE_WORLD,
    PRIMARY,
    build_repositories,
    relocated,
)

from chorus.domain.ids import FactId
from chorus.infrastructure.dynamodb import codec_audit, codec_case, codec_core, codec_mandate
from chorus.infrastructure.dynamodb import codec_share as share_codec
from chorus.infrastructure.dynamodb.guards import EntityIdentity, validate_page_scope
from chorus.ports.errors import CrossCaseViolationError
from chorus.ports.pagination import PageRequest
from chorus.ports.scopes import CaseScope
from chorus.ports.storage import StorageDriver

pytestmark = pytest.mark.anyio

PAGE = PageRequest(limit=25)
WINDOW_START = PRIMARY.message().sent_at - timedelta(days=1)
WINDOW_END = PRIMARY.message().sent_at + timedelta(days=1)


def foreign_community_case_scope() -> CaseScope:
    """The requested namespace and case, but another community's identifier."""

    return CaseScope(
        namespace=PRIMARY.namespace,
        community_id=OTHER_CASE.community_id,
        case_id=PRIMARY.case_id,
    )


# -- core case pages ----------------------------------------------------------------------


async def test_a_foreign_community_fact_fails_the_facts_page(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    foreign = replace(PRIMARY.fact(), community_id=OTHER_CASE.community_id)
    await storage.write_item(
        relocated(
            codec_case.encode_fact(PRIMARY.case_scope, foreign),
            codec_case.fact_key(PRIMARY.case_scope, PRIMARY.fact_id),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_case_facts(PRIMARY.case_scope, PAGE)


async def test_a_foreign_case_fact_fails_the_facts_page(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    await storage.write_item(
        relocated(
            codec_case.encode_fact(OTHER_CASE.case_scope, OTHER_CASE.fact()),
            codec_case.fact_key(PRIMARY.case_scope, OTHER_CASE.fact_id),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_case_facts(PRIMARY.case_scope, PAGE)


async def test_a_foreign_namespace_fact_fails_the_facts_page(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    foreign_world = OTHER_NAMESPACE_WORLD
    await storage.write_item(
        relocated(
            codec_case.encode_fact(foreign_world.case_scope, foreign_world.fact()),
            codec_case.fact_key(PRIMARY.case_scope, foreign_world.fact_id),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_case_facts(PRIMARY.case_scope, PAGE)


async def test_a_foreign_row_fails_the_page_instead_of_being_filtered(
    storage: StorageDriver,
) -> None:
    """A partial page would silently hide that an isolation boundary was crossed."""

    repositories = build_repositories(storage)
    await storage.write_item(
        repositories.core.stage_create_fact(PRIMARY.case_scope, PRIMARY.fact())
    )
    await storage.write_item(
        relocated(
            codec_case.encode_fact(OTHER_CASE.case_scope, OTHER_CASE.fact()),
            codec_case.fact_key(PRIMARY.case_scope, OTHER_CASE.fact_id),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_case_facts(PRIMARY.case_scope, PAGE)


async def test_a_foreign_community_report_fails_the_reports_page(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    foreign = replace(PRIMARY.report(), community_id=OTHER_CASE.community_id)
    await storage.write_item(
        relocated(
            codec_case.encode_report(PRIMARY.case_scope, foreign),
            codec_case.report_key(PRIMARY.case_scope, PRIMARY.report_id),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_case_reports(PRIMARY.case_scope, PAGE)


async def test_a_foreign_community_assessment_fails_the_assessments_page(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    assessment = PRIMARY.assessment()
    await storage.write_item(
        relocated(
            codec_case.encode_assessment(foreign_community_case_scope(), assessment),
            codec_case.assessment_key(PRIMARY.case_scope, assessment),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_case_assessments(PRIMARY.case_scope, PAGE)


# -- the strongly consistent mandate authorization page -----------------------------------


async def test_a_foreign_community_mandate_pointer_fails_the_authorization_page(
    storage: StorageDriver,
) -> None:
    """The strong read that decides which mandates authorize an export must not leak.

    A direct pointer get already denied this; the paged form is the one an authorization
    caller actually uses, so it has to deny it too.
    """

    repositories = build_repositories(storage)
    foreign = replace(PRIMARY.mandate_pointer(), community_id=OTHER_CASE.community_id)
    await storage.write_item(
        relocated(
            codec_mandate.encode_mandate_pointer(PRIMARY.case_scope, foreign),
            codec_mandate.mandate_pointer_key(PRIMARY.case_scope, PRIMARY.mandate_id),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.load_current_mandate_pointers(PRIMARY.case_scope, PAGE)


async def test_a_foreign_case_mandate_pointer_fails_the_authorization_page(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    await storage.write_item(
        relocated(
            codec_mandate.encode_mandate_pointer(
                OTHER_CASE.case_scope, OTHER_CASE.mandate_pointer()
            ),
            codec_mandate.mandate_pointer_key(PRIMARY.case_scope, OTHER_CASE.mandate_id),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.load_current_mandate_pointers(PRIMARY.case_scope, PAGE)


# -- the community feed --------------------------------------------------------------------


async def test_a_foreign_community_message_fails_the_feed(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    message = PRIMARY.message()
    foreign = replace(message, community_id=OTHER_CASE.community_id)
    await storage.write_item(
        relocated(
            codec_core.encode_message(PRIMARY.community_scope, foreign),
            codec_core.message_key(
                PRIMARY.community_scope, sent_at=message.sent_at, message_id=message.message_id
            ),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_message_feed(
            PRIMARY.community_scope, start=WINDOW_START, end=WINDOW_END, request=PAGE
        )


async def test_a_foreign_namespace_message_fails_the_feed(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    message = OTHER_NAMESPACE_WORLD.message()
    await storage.write_item(
        relocated(
            codec_core.encode_message(OTHER_NAMESPACE_WORLD.community_scope, message),
            codec_core.message_key(
                PRIMARY.community_scope, sent_at=message.sent_at, message_id=message.message_id
            ),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_message_feed(
            PRIMARY.community_scope, start=WINDOW_START, end=WINDOW_END, request=PAGE
        )


# -- shareable pages -----------------------------------------------------------------------


async def test_a_foreign_community_view_history_locator_fails_its_page(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    locator = replace(PRIMARY.view_history(), community_id=OTHER_CASE.community_id)
    await storage.write_item(
        relocated(
            share_codec.encode_view_history(PRIMARY.case_scope, locator),
            share_codec.view_history_key(PRIMARY.case_scope, PRIMARY.view_history()),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.shareable.read_view_history(PRIMARY.case_scope, PAGE)


async def test_a_foreign_community_action_history_locator_fails_its_page(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    locator = replace(PRIMARY.action_history(), community_id=OTHER_CASE.community_id)
    await storage.write_item(
        relocated(
            share_codec.encode_action_history(PRIMARY.case_scope, locator),
            share_codec.action_history_key(PRIMARY.case_scope, PRIMARY.action_history()),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.shareable.read_action_history(PRIMARY.case_scope, PAGE)


async def test_a_foreign_community_commitment_fails_its_page(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    commitment = PRIMARY.commitment()
    await storage.write_item(
        relocated(
            share_codec.encode_commitment(foreign_community_case_scope(), commitment),
            share_codec.commitment_key(PRIMARY.case_scope, commitment.commitment_id),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.shareable.read_case_commitments(PRIMARY.case_scope, PAGE)


async def test_a_foreign_case_commitment_fails_its_page(storage: StorageDriver) -> None:
    repositories = build_repositories(storage)
    await storage.write_item(
        relocated(
            share_codec.encode_commitment(OTHER_CASE.case_scope, OTHER_CASE.commitment()),
            share_codec.commitment_key(PRIMARY.case_scope, OTHER_CASE.commitment_id),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.shareable.read_case_commitments(PRIMARY.case_scope, PAGE)


# -- audit pages ---------------------------------------------------------------------------


async def test_a_foreign_community_audit_event_fails_the_case_page(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    event = replace(PRIMARY.audit_event(), community_id=OTHER_CASE.community_id)
    await storage.write_item(
        relocated(
            codec_audit.encode_case_event(
                foreign_community_case_scope(), event, retention=DEMO_RETENTION
            ),
            codec_audit.case_event_key(PRIMARY.case_scope, event),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.audit.read_case_events(PRIMARY.case_scope, PAGE)


async def test_a_foreign_namespace_audit_event_fails_the_namespace_page(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    event = OTHER_NAMESPACE_WORLD.audit_event(case_scoped=False)
    await storage.write_item(
        relocated(
            codec_audit.encode_namespace_event(
                OTHER_NAMESPACE_WORLD.namespace_scope, event, retention=DEMO_RETENTION
            ),
            codec_audit.namespace_event_key(PRIMARY.namespace_scope, event),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.audit.read_namespace_events(PRIMARY.namespace_scope, PAGE)


# -- the entity-identity half of the guard -------------------------------------------------


def test_a_disowning_entity_fails_its_page() -> None:
    """A decoded entity that claims another scope is denied even at the right address.

    No shipped codec can currently produce this, because every paged entity takes its
    namespace, community, and case from the stored envelope. The guard is asserted directly
    so that a future codec which reads one of those from the item body cannot quietly become
    the one path where the body is trusted.
    """

    expected_key = share_codec.commitment_key(PRIMARY.case_scope, PRIMARY.commitment_id)
    envelope_scope = share_codec.decode_commitment(
        share_codec.encode_commitment(PRIMARY.case_scope, PRIMARY.commitment())
    )[0]

    validate_page_scope(
        envelope_scope,
        EntityIdentity(
            namespace=PRIMARY.namespace,
            community_id=PRIMARY.community_id,
            case_id=PRIMARY.case_id,
        ),
        expected_key=expected_key,
        entity_ref="COMMITMENT",
        namespace=PRIMARY.namespace,
        community_id=PRIMARY.community_id,
        case_id=PRIMARY.case_id,
    )

    for disowning in (
        EntityIdentity(namespace=OTHER_NAMESPACE_WORLD.namespace),
        EntityIdentity(community_id=OTHER_CASE.community_id),
        EntityIdentity(case_id=OTHER_CASE.case_id),
    ):
        with pytest.raises(CrossCaseViolationError):
            validate_page_scope(
                envelope_scope,
                disowning,
                expected_key=expected_key,
                entity_ref="COMMITMENT",
                namespace=PRIMARY.namespace,
                community_id=PRIMARY.community_id,
                case_id=PRIMARY.case_id,
            )


def test_a_page_row_found_away_from_its_own_address_is_denied() -> None:
    """The address a decoded entity claims must be the address it was found at."""

    envelope_scope = share_codec.decode_commitment(
        share_codec.encode_commitment(PRIMARY.case_scope, PRIMARY.commitment())
    )[0]
    identity = EntityIdentity(
        namespace=PRIMARY.namespace,
        community_id=PRIMARY.community_id,
        case_id=PRIMARY.case_id,
    )

    for elsewhere in (
        share_codec.commitment_key(PRIMARY.case_scope, OTHER_CASE.commitment_id),
        share_codec.commitment_key(OTHER_CASE.case_scope, PRIMARY.commitment_id),
    ):
        with pytest.raises(CrossCaseViolationError):
            validate_page_scope(
                envelope_scope,
                identity,
                expected_key=elsewhere,
                entity_ref="COMMITMENT",
                namespace=PRIMARY.namespace,
                community_id=PRIMARY.community_id,
                case_id=PRIMARY.case_id,
            )


# -- address / body identity through the real repositories ---------------------------------
#
# Every paged entity carries its own identifier, and the sort key it lives at is built from
# that identifier. A row whose body names a different entity than its address satisfies every
# namespace/community/case check and is still a corrupted address, so each of these plants
# entity B's record at entity A's key and requires the whole page to fail. A direct get has
# always rejected this, because it builds the key from the caller's identifier; the page has
# to rebuild the key from the decoded body to reach the same answer.


async def test_a_fact_stored_at_another_facts_address_fails_the_page(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    await storage.write_item(
        relocated(
            codec_case.encode_fact(
                PRIMARY.case_scope, replace(PRIMARY.fact(), fact_id=OTHER_CASE.fact_id)
            ),
            codec_case.fact_key(PRIMARY.case_scope, PRIMARY.fact_id),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_case_facts(PRIMARY.case_scope, PAGE)


async def test_a_report_stored_at_another_reports_address_fails_the_page(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    await storage.write_item(
        relocated(
            codec_case.encode_report(
                PRIMARY.case_scope, replace(PRIMARY.report(), report_id=OTHER_CASE.report_id)
            ),
            codec_case.report_key(PRIMARY.case_scope, PRIMARY.report_id),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_case_reports(PRIMARY.case_scope, PAGE)


async def test_a_message_stored_at_another_messages_address_fails_the_feed(
    storage: StorageDriver,
) -> None:
    message = PRIMARY.message()
    repositories = build_repositories(storage)
    await storage.write_item(
        relocated(
            codec_core.encode_message(
                PRIMARY.community_scope,
                replace(message, message_id=OTHER_CASE.message_id),
            ),
            codec_core.message_key(
                PRIMARY.community_scope, sent_at=message.sent_at, message_id=message.message_id
            ),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_message_feed(
            PRIMARY.community_scope, start=WINDOW_START, end=WINDOW_END, request=PAGE
        )


async def test_a_message_claiming_another_instant_fails_the_feed(
    storage: StorageDriver,
) -> None:
    """The instant is half of a message's address, so it is checked like the identifier."""

    message = PRIMARY.message()
    repositories = build_repositories(storage)
    await storage.write_item(
        relocated(
            codec_core.encode_message(
                PRIMARY.community_scope,
                replace(message, sent_at=message.sent_at + timedelta(seconds=1)),
            ),
            codec_core.message_key(
                PRIMARY.community_scope, sent_at=message.sent_at, message_id=message.message_id
            ),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_message_feed(
            PRIMARY.community_scope, start=WINDOW_START, end=WINDOW_END, request=PAGE
        )


async def test_an_assessment_stored_at_another_assessments_address_fails_the_page(
    storage: StorageDriver,
) -> None:
    assessment = PRIMARY.assessment()
    repositories = build_repositories(storage)
    await storage.write_item(
        relocated(
            codec_case.encode_assessment(
                PRIMARY.case_scope,
                replace(assessment, assessment_id=OTHER_CASE.assessment_id),
            ),
            codec_case.assessment_key(PRIMARY.case_scope, assessment),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_case_assessments(PRIMARY.case_scope, PAGE)


async def test_a_mandate_pointer_stored_at_another_mandates_address_fails_the_page(
    storage: StorageDriver,
) -> None:
    """The authorization page must not let one mandate's pointer answer for another.

    A pointer carries the version, status, and terms hash that decide whether a mandate
    authorizes an export. Reading it at a key that names a different mandate would attribute
    one contributor's decision to another's.
    """

    repositories = build_repositories(storage)
    stored = PRIMARY.mandate_pointer()
    await storage.write_item(
        relocated(
            codec_mandate.encode_mandate_pointer(
                PRIMARY.case_scope,
                replace(stored, pointer=replace(stored.pointer, mandate_id=OTHER_CASE.mandate_id)),
            ),
            codec_mandate.mandate_pointer_key(PRIMARY.case_scope, PRIMARY.mandate_id),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.load_current_mandate_pointers(PRIMARY.case_scope, PAGE)


async def test_a_view_locator_stored_at_another_views_address_fails_the_page(
    storage: StorageDriver,
) -> None:
    locator = PRIMARY.view_history()
    repositories = build_repositories(storage)
    await storage.write_item(
        relocated(
            share_codec.encode_view_history(
                PRIMARY.case_scope, replace(locator, view_id=OTHER_CASE.view_id)
            ),
            share_codec.view_history_key(PRIMARY.case_scope, locator),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.shareable.read_view_history(PRIMARY.case_scope, PAGE)


async def test_an_action_locator_stored_at_another_actions_address_fails_the_page(
    storage: StorageDriver,
) -> None:
    locator = PRIMARY.action_history()
    repositories = build_repositories(storage)
    await storage.write_item(
        relocated(
            share_codec.encode_action_history(
                PRIMARY.case_scope, replace(locator, action_id=OTHER_CASE.action_id)
            ),
            share_codec.action_history_key(PRIMARY.case_scope, locator),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.shareable.read_action_history(PRIMARY.case_scope, PAGE)


async def test_a_commitment_stored_at_another_commitments_address_fails_the_page(
    storage: StorageDriver,
) -> None:
    repositories = build_repositories(storage)
    await storage.write_item(
        relocated(
            share_codec.encode_commitment(
                PRIMARY.case_scope,
                replace(PRIMARY.commitment(), commitment_id=OTHER_CASE.commitment_id),
            ),
            share_codec.commitment_key(PRIMARY.case_scope, PRIMARY.commitment_id),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.shareable.read_case_commitments(PRIMARY.case_scope, PAGE)


async def test_a_case_audit_event_stored_at_another_events_address_fails_the_page(
    storage: StorageDriver,
) -> None:
    event = PRIMARY.audit_event()
    repositories = build_repositories(storage)
    await storage.write_item(
        relocated(
            codec_audit.encode_case_event(
                PRIMARY.case_scope,
                replace(event, audit_event_id=PRIMARY.uuid("some-other-audit-event")),
                retention=DEMO_RETENTION,
            ),
            codec_audit.case_event_key(PRIMARY.case_scope, event),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.audit.read_case_events(PRIMARY.case_scope, PAGE)


async def test_a_namespace_audit_event_stored_at_another_events_address_fails_the_page(
    storage: StorageDriver,
) -> None:
    event = PRIMARY.audit_event(case_scoped=False)
    repositories = build_repositories(storage)
    await storage.write_item(
        relocated(
            codec_audit.encode_namespace_event(
                PRIMARY.namespace_scope,
                replace(event, occurred_at=event.occurred_at + timedelta(seconds=1)),
                retention=DEMO_RETENTION,
            ),
            codec_audit.namespace_event_key(PRIMARY.namespace_scope, event),
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.audit.read_namespace_events(PRIMARY.namespace_scope, PAGE)


async def test_one_misaddressed_row_fails_a_page_of_otherwise_valid_rows(
    storage: StorageDriver,
) -> None:
    """A partial page would hide that a stored address and its body disagree."""

    repositories = build_repositories(storage)
    await storage.write_item(
        repositories.core.stage_create_fact(PRIMARY.case_scope, PRIMARY.fact())
    )
    other_address = codec_case.fact_key(PRIMARY.case_scope, FactId(PRIMARY.uuid("decoy-address")))
    await storage.write_item(
        relocated(
            codec_case.encode_fact(
                PRIMARY.case_scope, replace(PRIMARY.fact(), fact_id=OTHER_CASE.fact_id)
            ),
            other_address,
        )
    )

    with pytest.raises(CrossCaseViolationError):
        await repositories.core.read_case_facts(PRIMARY.case_scope, PAGE)
