"""Audit retention is a deployment choice, not a property of an audit event.

The frozen contract expires audit events after 90 days in the demo deployment. Everywhere
else the trail is kept, because an audit row that silently disappears is the record of a
security decision disappearing. The table keeps its TTL attribute configured in every
environment -- that costs nothing and cannot delete an item that carries no TTL value.
"""

from __future__ import annotations

import pytest
from tests.fixtures.persistence import PRIMARY, build_repositories

from chorus.domain.time import epoch_seconds_ceiling
from chorus.infrastructure.dynamodb import codec_audit
from chorus.infrastructure.dynamodb.codec import ATTR_EXPIRES_AT_EPOCH
from chorus.infrastructure.local.memory import InMemoryStorageDriver
from chorus.ports.limits import AUDIT_TTL_SECONDS
from chorus.ports.pagination import PageRequest
from chorus.ports.retention import AuditRetention
from chorus.settings import Environment, audit_retention_for

pytestmark = pytest.mark.anyio


def test_only_the_demo_environment_expires_audit_events() -> None:
    assert audit_retention_for(Environment.DEMO) == AuditRetention.demo()
    assert audit_retention_for(Environment.DEVELOPMENT) == AuditRetention.durable()
    assert audit_retention_for(Environment.TEST) == AuditRetention.durable()


def test_the_demo_retention_is_the_frozen_ninety_days() -> None:
    event = PRIMARY.audit_event()

    expiry = AuditRetention.demo().expires_at_epoch(event.occurred_at)

    assert expiry == epoch_seconds_ceiling(event.occurred_at) + AUDIT_TTL_SECONDS
    assert AUDIT_TTL_SECONDS == 90 * 24 * 60 * 60


def test_a_durable_environment_writes_no_ttl_attribute_at_all() -> None:
    """Not a far-future TTL: the attribute is absent, so the table sweeps nothing."""

    item = codec_audit.encode_case_event(
        PRIMARY.case_scope, PRIMARY.audit_event(), retention=AuditRetention.durable()
    )

    assert ATTR_EXPIRES_AT_EPOCH not in item


def test_a_demo_environment_writes_the_frozen_ttl_attribute() -> None:
    event = PRIMARY.audit_event()

    item = codec_audit.encode_case_event(PRIMARY.case_scope, event, retention=AuditRetention.demo())

    assert item[ATTR_EXPIRES_AT_EPOCH] == (
        epoch_seconds_ceiling(event.occurred_at) + AUDIT_TTL_SECONDS
    )


def test_a_namespace_event_follows_the_same_retention_rule() -> None:
    event = PRIMARY.audit_event(case_scoped=False)

    durable = codec_audit.encode_namespace_event(
        PRIMARY.namespace_scope, event, retention=AuditRetention.durable()
    )
    demo = codec_audit.encode_namespace_event(
        PRIMARY.namespace_scope, event, retention=AuditRetention.demo()
    )

    assert ATTR_EXPIRES_AT_EPOCH not in durable
    assert ATTR_EXPIRES_AT_EPOCH in demo


def test_both_retentions_decode_back_to_the_same_event() -> None:
    """Retention changes the item, never the record it represents."""

    event = PRIMARY.audit_event()

    for retention in (AuditRetention.durable(), AuditRetention.demo()):
        item = codec_audit.encode_case_event(PRIMARY.case_scope, event, retention=retention)
        _, decoded = codec_audit.decode_audit_event(item)
        assert decoded == event


def test_an_injected_expiry_is_still_rejected() -> None:
    """A TTL this codec could not have written is corruption, not a retention choice."""

    item = dict(
        codec_audit.encode_case_event(
            PRIMARY.case_scope, PRIMARY.audit_event(), retention=AuditRetention.demo()
        )
    )
    item[ATTR_EXPIRES_AT_EPOCH] = 1

    with pytest.raises(Exception, match="INTEGRITY_ERROR"):
        codec_audit.decode_audit_event(item)


def test_retention_must_be_positive_when_expiry_is_enabled() -> None:
    with pytest.raises(ValueError, match="positive"):
        AuditRetention(0)


async def test_a_durable_repository_persists_an_unexpiring_audit_row() -> None:
    driver = InMemoryStorageDriver()
    repositories = build_repositories(driver, audit_retention=AuditRetention.durable())

    await driver.write_item(
        repositories.audit.stage_append_case_event(PRIMARY.case_scope, PRIMARY.audit_event())
    )

    page = await repositories.audit.read_case_events(PRIMARY.case_scope, PageRequest(limit=10))
    assert page.items == (PRIMARY.audit_event(),)
    stored = await driver.get_item(
        codec_audit.case_event_key(PRIMARY.case_scope, PRIMARY.audit_event()), consistent=True
    )
    assert stored is not None
    assert ATTR_EXPIRES_AT_EPOCH not in stored
