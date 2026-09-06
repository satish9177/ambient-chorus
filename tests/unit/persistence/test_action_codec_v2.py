"""The Phase-7 schema bumps, read back through the codec that has to survive them.

Four items moved to ``/v2`` at the Phase-7 gate, and each moved because it gained a field a
reader must never guess at. What is asserted here is the pair of properties that makes such a
bump safe:

* the new shape **round-trips** -- every state, including the ``DRAFT`` execution that six
  phases of fixtures had never built;
* the old shape **fails closed** -- an item without the new attribute raises ``IntegrityError``
  rather than defaulting, inferring, or guessing, because a guessed-low epoch is a view that
  looks fresher than it is.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.fixtures.persistence import World

from chorus.domain.entities import (
    EXECUTION_FIELD_PRESENCE,
    ActionExecutionState,
    ActionTone,
    FieldPresence,
)
from chorus.domain.errors import IntegrityError
from chorus.infrastructure.dynamodb import codec_core, codec_share
from chorus.ports.scopes import ActionScope, CaseScope

WORLD = World(seed="codec-v2")


def _case_scope() -> CaseScope:
    return CaseScope(
        namespace=WORLD.namespace, community_id=WORLD.community_id, case_id=WORLD.case_id
    )


def _action_scope() -> ActionScope:
    return ActionScope(
        namespace=WORLD.namespace,
        community_id=WORLD.community_id,
        case_id=WORLD.case_id,
        action_id=WORLD.action_id,
    )


# ---------------------------------------------------------------------------------------
# The DRAFT execution -- the shape ADR-022 made expressible
# ---------------------------------------------------------------------------------------


def test_draft_execution_round_trips_through_codec() -> None:
    """The named Phase-7 test (evaluation test 40).

    ``tests/fixtures/persistence.py`` only ever built executions from ``APPROVED`` onward, so
    nothing in the suite had exercised the one shape Phase 7 must write. A ``DRAFT`` carries no
    approval, no send key, no rendered hash, and no SES token -- and a codec that wrote a
    sentinel for any of them would produce a record storage could not distinguish from a real
    binding.
    """

    scope = _action_scope()
    draft = WORLD.execution(state=ActionExecutionState.DRAFT)

    item = codec_share.encode_execution(scope, draft)
    _, decoded = codec_share.decode_execution(item)

    assert decoded == draft
    assert decoded.approval_id is None
    assert decoded.idempotency_key is None
    assert decoded.rendered_message_hash is None
    assert decoded.ses_request_token_hash is None
    assert item["approval_id"] is None
    assert item["rendered_message_hash"] is None


@pytest.mark.parametrize("state", sorted(ActionExecutionState, key=str))
def test_every_execution_state_round_trips(state: ActionExecutionState) -> None:
    """Presence is a function of state, so every column of the table is exercised."""

    scope = _action_scope()
    execution = WORLD.execution(state=state)

    _, decoded = codec_share.decode_execution(codec_share.encode_execution(scope, execution))

    assert decoded == execution
    decoded.require_state_presence()


@pytest.mark.parametrize("state", sorted(ActionExecutionState, key=str))
def test_the_encoded_item_carries_a_null_for_every_absent_field(
    state: ActionExecutionState,
) -> None:
    """Absent means ``None`` on the wire, never a placeholder digest or an empty string."""

    item = codec_share.encode_execution(_action_scope(), WORLD.execution(state=state))

    for name, row in EXECUTION_FIELD_PRESENCE.items():
        if row[state] is FieldPresence.ABSENT:
            assert item[name] is None, name


@pytest.mark.parametrize("superseded", ["action-execution/v1", "action-execution/v2"])
def test_the_execution_schema_is_v3_and_no_earlier_version_is_accepted(superseded: str) -> None:
    """No data has been deployed at any version, so a reader that still accepted an earlier one
    would be a reader accepting an item whose nullable shape it cannot interpret.

    ``/v3`` is the shape carrying ``claim_owner_hash``, which a ``/v2`` row cannot supply -- and
    a ``SENDING`` row without one cannot prove which attempt owns the claim.
    """

    assert frozenset({"action-execution/v3"}) == codec_share.EXECUTION_SCHEMA_VERSIONS

    item = dict(codec_share.encode_execution(_action_scope(), WORLD.execution()))
    item["schema_version"] = superseded
    with pytest.raises(IntegrityError):
        codec_share.decode_execution(item)


# ---------------------------------------------------------------------------------------
# The proposal -- structured caveats, the epoch, the preview hash, the closed tone
# ---------------------------------------------------------------------------------------


def test_the_proposal_round_trips_with_structured_caveats() -> None:
    scope = _action_scope()
    proposal = WORLD.proposal()

    _, decoded = codec_share.decode_proposal(codec_share.encode_proposal(scope, proposal))

    assert decoded == proposal
    assert decoded.caveats[0].export_fact_ids
    assert decoded.caveats[0].caveat_hash.value.startswith("sha256:")
    assert decoded.tone is ActionTone.NEUTRAL
    assert decoded.preview_hash.value.startswith("sha256:")
    assert decoded.authorization_version >= 1


def test_a_proposal_item_without_an_authorization_version_fails_closed() -> None:
    """It must never default, infer it from ``case_version``, or guess."""

    item = dict(codec_share.encode_proposal(_action_scope(), WORLD.proposal()))
    del item["authorization_version"]

    with pytest.raises(IntegrityError):
        codec_share.decode_proposal(item)


def test_a_proposal_item_without_a_preview_hash_fails_closed() -> None:
    item = dict(codec_share.encode_proposal(_action_scope(), WORLD.proposal()))
    del item["preview_hash"]

    with pytest.raises(IntegrityError):
        codec_share.decode_proposal(item)


def test_a_bare_string_caveat_is_no_longer_decodable() -> None:
    """The old shape discarded the caveat-to-fact proof the validator had relied on."""

    item = dict(codec_share.encode_proposal(_action_scope(), WORLD.proposal()))
    item["caveats"] = ("Reported by residents; not independently inspected.",)

    with pytest.raises(IntegrityError):
        codec_share.decode_proposal(item)


def test_an_out_of_vocabulary_tone_fails_closed() -> None:
    """``"PROFESSIONAL"`` used to pass through a free string. It no longer exists."""

    item = dict(codec_share.encode_proposal(_action_scope(), WORLD.proposal()))
    item["tone"] = "PROFESSIONAL"

    with pytest.raises(IntegrityError):
        codec_share.decode_proposal(item)


# ---------------------------------------------------------------------------------------
# The view and the two pointers
# ---------------------------------------------------------------------------------------


def test_the_view_round_trips_with_both_versions() -> None:
    scope = _case_scope()
    view = WORLD.view()

    _, decoded = codec_share.decode_view(codec_share.encode_view(scope, view))

    assert decoded == view
    assert decoded.case_version >= 1
    assert decoded.authorization_version >= 1


def test_a_view_item_without_an_authorization_version_fails_closed() -> None:
    item = dict(codec_share.encode_view(_case_scope(), WORLD.view()))
    del item["authorization_version"]

    with pytest.raises(IntegrityError):
        codec_share.decode_view(item)


def test_a_v1_view_item_is_refused_by_schema_version_alone() -> None:
    item = dict(codec_share.encode_view(_case_scope(), WORLD.view()))
    item["schema_version"] = "shareable-case-view/v1"

    with pytest.raises(IntegrityError):
        codec_share.decode_view(item)


def test_both_pointers_round_trip_with_the_epoch() -> None:
    """A caller can perform the exact staleness comparison from one strongly read pointer."""

    scope = _case_scope()
    view_pointer = WORLD.view_pointer(authorization_version=4)
    action_pointer = WORLD.action_pointer(authorization_version=4)

    _, decoded_view = codec_share.decode_view_pointer(
        codec_share.encode_view_pointer(scope, view_pointer)
    )
    _, decoded_action = codec_share.decode_action_pointer(
        codec_share.encode_action_pointer(scope, action_pointer)
    )

    assert decoded_view.authorization_version == 4
    assert decoded_action.authorization_version == 4
    assert decoded_view == view_pointer
    assert decoded_action == action_pointer


@pytest.mark.parametrize(
    ("encode", "decode", "build"),
    [
        (
            codec_share.encode_view_pointer,
            codec_share.decode_view_pointer,
            lambda: WORLD.view_pointer(),
        ),
        (
            codec_share.encode_action_pointer,
            codec_share.decode_action_pointer,
            lambda: WORLD.action_pointer(),
        ),
    ],
)
def test_a_pointer_without_an_authorization_version_fails_closed(
    encode: object, decode: object, build: object
) -> None:
    item = dict(encode(_case_scope(), build()))  # type: ignore[operator]
    del item["authorization_version"]

    with pytest.raises(IntegrityError):
        decode(item)  # type: ignore[operator]


# ---------------------------------------------------------------------------------------
# The case row
# ---------------------------------------------------------------------------------------


def test_the_case_row_round_trips_with_two_independent_counters() -> None:
    scope = _case_scope()
    case = WORLD.case(version=5, authorization_version=2)

    _, decoded = codec_core.decode_case(codec_core.encode_case(scope, case))

    assert decoded == case
    assert decoded.version == 5
    assert decoded.authorization_version == 2


def test_a_case_row_without_an_authorization_version_fails_closed() -> None:
    """ADR-020's fail-closed rule, at the one boundary where a stored row can lack it.

    Not a default, not an inference from ``case_version``, and not a guess -- because a
    guessed-low epoch makes a stale view look fresh, which is the failure the split exists to
    prevent.
    """

    item = dict(codec_core.encode_case(_case_scope(), WORLD.case()))
    del item["authorization_version"]

    with pytest.raises(IntegrityError):
        codec_core.decode_case(item)


def test_the_two_counters_are_stored_as_separate_attributes() -> None:
    """So a reader cannot satisfy one condition by reading the other."""

    item = codec_core.encode_case(_case_scope(), WORLD.case(version=7, authorization_version=3))

    assert item["version"] == 7
    assert item["authorization_version"] == 3


def test_an_execution_row_that_violates_the_presence_table_fails_closed() -> None:
    """The entity's invariant is enforced on the way *in* from storage as well as out.

    A row carrying a rendered hash at ``DRAFT`` would describe a message that was never built,
    and decoding it into a valid-looking entity would launder that into the domain.
    """

    scope = _action_scope()
    item = dict(
        codec_share.encode_execution(scope, WORLD.execution(state=ActionExecutionState.DRAFT))
    )
    item["rendered_message_hash"] = "sha256:" + "c" * 64

    with pytest.raises(IntegrityError):
        codec_share.decode_execution(item)


def test_a_draft_execution_cannot_be_constructed_with_an_approval() -> None:
    approved = WORLD.execution(state=ActionExecutionState.APPROVED)

    with pytest.raises(ValueError):
        replace(approved, state=ActionExecutionState.DRAFT)
