"""No private value may appear in a Shareable or Audit item.

The three-table split is a security control, so it is checked at the serialization boundary
rather than argued for in a comment. Every Shareable and Audit item the codecs can produce is
rendered and searched for the private strings and private identifiers of the same case.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.fixtures.persistence import DEMO_RETENTION, PRIMARY

from chorus.infrastructure.dynamodb import codec_audit, codec_share
from chorus.ports.storage import StoredItem, StoredValue, TableName

PRIVATE_STRINGS = (
    PRIMARY.message().raw_text.reveal(),
    PRIMARY.report().private_summary.reveal(),
    PRIMARY.contributor().pseudonym,
    PRIMARY.evidence_item().private_object_key.reveal(),
)

PRIVATE_IDENTIFIERS = (
    str(PRIMARY.contributor_id),
    str(PRIMARY.fact_id),
    str(PRIMARY.report_id),
    str(PRIMARY.evidence_id),
    str(PRIMARY.message_id),
)


def shareable_items() -> dict[str, StoredItem]:
    case = PRIMARY.case_scope
    action = PRIMARY.action_scope
    return {
        "SHAREABLE_VIEW": codec_share.encode_view(case, PRIMARY.view()),
        "CURRENT_VIEW_POINTER": codec_share.encode_view_pointer(case, PRIMARY.view_pointer()),
        "VIEW_HISTORY_LOCATOR": codec_share.encode_view_history(case, PRIMARY.view_history()),
        "ACTION_PROPOSAL": codec_share.encode_proposal(action, PRIMARY.proposal()),
        "APPROVAL": codec_share.encode_approval(action, PRIMARY.approval()),
        "ACTION_EXECUTION": codec_share.encode_execution(action, PRIMARY.execution()),
        "CURRENT_ACTION_POINTER": codec_share.encode_action_pointer(case, PRIMARY.action_pointer()),
        "ACTION_HISTORY_LOCATOR": codec_share.encode_action_history(case, PRIMARY.action_history()),
        "COMMITMENT": codec_share.encode_commitment(case, PRIMARY.commitment()),
    }


def audit_items() -> dict[str, StoredItem]:
    return {
        "AUDIT_CASE_EVENT": codec_audit.encode_case_event(
            PRIMARY.case_scope, PRIMARY.audit_event(), retention=DEMO_RETENTION
        ),
        "AUDIT_NAMESPACE_EVENT": codec_audit.encode_namespace_event(
            PRIMARY.namespace_scope,
            PRIMARY.audit_event(case_scoped=False),
            retention=DEMO_RETENTION,
        ),
    }


def strings(value: StoredValue) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, tuple):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)


def rendered(item: StoredItem) -> str:
    return "".join(strings(item))


@pytest.mark.parametrize("name", sorted(shareable_items()))
def test_no_shareable_item_carries_private_text(name: str) -> None:
    text = rendered(shareable_items()[name])

    for private in PRIVATE_STRINGS:
        assert private not in text


# The frozen model deliberately stores two private identifiers in the Shareable table:
# `Approval.approver_id` (docs/architecture/04, "Approver identity is operationally
# sensitive and not sent externally") and `Commitment.source_evidence_id` /
# `verified_by_contributor_id` (same document, Commitment). Both are withheld from the
# outbound artifact by the renderer, not by the table. Everything else that feeds an
# external send must carry no private lineage at all.
ITEMS_WITH_FROZEN_PRIVATE_REFERENCES = frozenset({"APPROVAL", "COMMITMENT"})

EXTERNALLY_BOUND_ITEMS = frozenset(shareable_items()) - ITEMS_WITH_FROZEN_PRIVATE_REFERENCES


@pytest.mark.parametrize("name", sorted(EXTERNALLY_BOUND_ITEMS))
def test_no_externally_bound_item_carries_a_private_identifier(name: str) -> None:
    """Export identifiers exist so a safe artifact cannot be joined back to private rows."""

    text = rendered(shareable_items()[name])

    for identifier in PRIVATE_IDENTIFIERS:
        assert identifier not in text


def test_the_set_of_items_holding_private_references_cannot_grow_silently() -> None:
    """A new Shareable shape cannot join the exception list without this test failing."""

    holders = {
        name
        for name, item in shareable_items().items()
        if any(identifier in rendered(item) for identifier in PRIVATE_IDENTIFIERS)
    }

    assert holders == ITEMS_WITH_FROZEN_PRIVATE_REFERENCES


@pytest.mark.parametrize("name", sorted(audit_items()))
def test_no_audit_item_carries_private_text(name: str) -> None:
    text = rendered(audit_items()[name])

    for private in PRIVATE_STRINGS:
        assert private not in text


@pytest.mark.parametrize("name", sorted(shareable_items()))
def test_every_shareable_item_is_addressed_in_the_shareable_table(name: str) -> None:
    item = shareable_items()[name]

    assert isinstance(item["PK"], str)
    assert item["PK"].startswith("NS#")


def test_the_shareable_and_audit_tables_are_addressed_separately() -> None:
    case = PRIMARY.case_scope

    assert codec_share.view_key(case, PRIMARY.view_id).table is TableName.SHAREABLE
    assert codec_audit.case_event_key(case, PRIMARY.audit_event()).table is TableName.AUDIT
