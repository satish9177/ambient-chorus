"""F08 -- current deployment configuration is proved before the model is called.

Codex found the pre-invocation checks proving the *old view's* integrity -- its recomputed
``view_hash``, its ``authorization_snapshot_hash`` -- and concluding from that that the view was
still usable. It is not the same statement. A snapshot verifies because the view recomputes to
the values it was built from, which is exactly what a view compiled by a **superseded** policy
build or against a **rotated** destination registry entry also does. Integrity of the old
artifact says nothing about the current deployment.

ADR-020 § 3 is explicit that these values live outside the case authorization epoch precisely
because they are deployment-owned, and that they "are re-checked by exact equality at proposal
and fence time":

* ``policy_version`` and ``compiler_version``;
* the **policy build hash** -- the rule set itself, which can move without either version
  string changing;
* the exact current destination: identifier, kind, registry version, routing token, and the
  display label the contract makes authoritative.

The V2 implementation had computed ``policy_build_hash`` inside the compiler and buried it in
``authorization_snapshot_hash``, where no consumer could compare it. The smallest compliant
correction adds the safe digest to ``ShareableCaseView`` V2 and its stored mirror -- no V2 data
has been deployed, so this is a code change with re-cut vectors and no migration tooling.

Every failure below costs **zero** model calls.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from chorus.application.commands.propose_action import ProposalDenial, ProposalDeniedError
from chorus.domain.ids import DestinationId, Sha256Digest
from chorus.ports.records import StoredSafeDestination
from chorus.privacy.canonical import hash_value, verify_hash
from chorus.privacy.compiler import POLICY_BUILD_HASH
from tests.fixtures.action import ActionHarness

pytestmark = pytest.mark.anyio


async def _denied(
    harness: ActionHarness,
    *,
    destination: StoredSafeDestination | None = None,
    policy_version: str | None = None,
    compiler_version: str | None = None,
    policy_build_hash: Sha256Digest | None = None,
) -> ProposalDenial:
    """Run a proposal whose deployment configuration differs in exactly one dimension.

    Each keyword names one member of the current deployment configuration, spelled out rather
    than passed through ``**kwargs`` so a typo is a type error instead of a silently ignored
    override -- which would make a stale-configuration test pass by not testing anything.
    """

    use_case = harness.propose_action()
    if destination is not None:
        use_case = replace(use_case, destination=destination)
    if policy_version is not None:
        use_case = replace(use_case, policy_version=policy_version)
    if compiler_version is not None:
        use_case = replace(use_case, compiler_version=compiler_version)
    if policy_build_hash is not None:
        use_case = replace(use_case, policy_build_hash=policy_build_hash)

    with pytest.raises(ProposalDeniedError) as caught:
        await use_case.execute(await harness.command())
    assert harness.agent.invocations == [], "a stale configuration must cost zero model calls"
    return caught.value.denial


def _rotated(
    harness: ActionHarness,
    *,
    destination_id: DestinationId | None = None,
    registry_version: int | None = None,
    routing_token: UUID | None = None,
    display_label: str | None = None,
) -> StoredSafeDestination:
    """The deployment's registry entry with exactly one member rotated."""

    current = harness.compile.stored_destination()
    if destination_id is not None:
        current = replace(current, destination_id=destination_id)
    if registry_version is not None:
        current = replace(current, registry_version=registry_version)
    if routing_token is not None:
        current = replace(current, routing_token=routing_token)
    if display_label is not None:
        current = replace(current, display_label=display_label)
    return current


# ---------------------------------------------------------------------------------------
# The view now carries the value the check needs
# ---------------------------------------------------------------------------------------


async def test_the_compiled_view_carries_the_policy_build_hash(
    harness: ActionHarness,
) -> None:
    """Present, safe, and equal to the deployment's own build digest."""

    await harness.prepare()

    assert harness.view.policy_build_hash == POLICY_BUILD_HASH
    assert harness.view.policy_build_hash.value.startswith("sha256:")


async def test_the_policy_build_hash_is_covered_by_the_view_hash(
    harness: ActionHarness,
) -> None:
    """A field outside the view hash would be a value anybody could edit after the fact."""

    await harness.prepare()

    assert verify_hash(harness.view, harness.view.view_hash, omit_fields=frozenset({"view_hash"}))
    tampered = replace(harness.view, policy_build_hash=hash_value({"other": "build"}))
    assert not verify_hash(tampered, harness.view.view_hash, omit_fields=frozenset({"view_hash"}))


async def test_the_action_payload_mirrors_it(harness: ActionHarness) -> None:
    """The mirror stays a mirror: a field on the view is a field on the payload."""

    from chorus.application.commands.propose_action import to_action_input

    await harness.prepare()
    payload = to_action_input(harness.view)

    assert payload.policy_build_hash == harness.view.policy_build_hash.value


# ---------------------------------------------------------------------------------------
# One stale dimension at a time, each before any model call
# ---------------------------------------------------------------------------------------


async def test_a_moved_policy_build_refuses_before_the_model(harness: ActionHarness) -> None:
    """The rule set moved. Neither version string has to change for that to happen.

    The *deployment* is moved rather than the stored view, because that is the direction the
    hazard actually runs: views are immutable and content-addressed, and what changes under
    them is the configuration this process is running.
    """

    await harness.prepare()
    denial = await _denied(harness, policy_build_hash=hash_value({"rules": "moved"}))

    assert denial is ProposalDenial.POLICY_BUILD_MISMATCH


async def test_a_moved_policy_version_refuses_before_the_model(harness: ActionHarness) -> None:
    await harness.prepare()

    assert await _denied(harness, policy_version="policy/v0") is (
        ProposalDenial.POLICY_VERSION_MISMATCH
    )


async def test_a_moved_compiler_version_refuses_before_the_model(
    harness: ActionHarness,
) -> None:
    await harness.prepare()

    assert await _denied(harness, compiler_version="compiler/v0") is (
        ProposalDenial.COMPILER_VERSION_MISMATCH
    )


async def test_a_different_destination_identifier_refuses_before_the_model(
    harness: ActionHarness,
) -> None:
    await harness.prepare()
    rotated = _rotated(harness, destination_id=DestinationId("property_manager:other"))

    assert await _denied(harness, destination=rotated) is ProposalDenial.DESTINATION_MISMATCH


async def test_a_rotated_registry_version_refuses_before_the_model(
    harness: ActionHarness,
) -> None:
    """The registry entry moved under a view that still verifies against its own snapshot."""

    await harness.prepare()
    rotated = _rotated(harness, registry_version=harness.view.destination.registry_version + 1)

    denial = await _denied(harness, destination=rotated)

    assert denial is ProposalDenial.DESTINATION_REGISTRY_VERSION_MISMATCH
    # The old view is still internally coherent, which is the point: integrity of the old
    # artifact is not evidence about the current deployment.
    assert verify_hash(harness.view, harness.view.view_hash, omit_fields=frozenset({"view_hash"}))


async def test_a_rotated_routing_token_refuses_before_the_model(
    harness: ActionHarness,
) -> None:
    """The one that would route an external message by a token nobody currently uses."""

    await harness.prepare()
    rotated = _rotated(harness, routing_token=uuid4())

    denial = await _denied(harness, destination=rotated)

    assert denial is ProposalDenial.DESTINATION_ROUTING_TOKEN_MISMATCH


async def test_a_changed_display_label_refuses_before_the_model(
    harness: ActionHarness,
) -> None:
    """Authoritative by contract: the label is inside ``preview_hash`` and groundable copy."""

    await harness.prepare()
    rotated = _rotated(harness, display_label="Somebody Else Property Management")

    assert await _denied(harness, destination=rotated) is ProposalDenial.DESTINATION_LABEL_MISMATCH


async def test_the_current_configuration_check_is_not_the_snapshot_check(
    harness: ActionHarness,
) -> None:
    """State the distinction the repair rests on, so it cannot be weakened back.

    The view's own ``authorization_snapshot_hash`` is untouched and still verifies while the
    deployment has moved out from under it -- so "the old snapshot verified" can never be
    accepted as the current-configuration proof.
    """

    await harness.prepare()
    snapshot_before = harness.view.authorization_snapshot_hash
    rotated = _rotated(harness, routing_token=uuid4())

    await _denied(harness, destination=rotated)

    assert harness.view.authorization_snapshot_hash == snapshot_before


async def test_a_matching_configuration_still_proposes(harness: ActionHarness) -> None:
    """The control: with the deployment unchanged, nothing new is refused."""

    await harness.prepare()
    result = await harness.propose_action().execute(await harness.command())

    assert result.claim_count == 1
    assert len(harness.agent.invocations) == 1
