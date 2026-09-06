"""F10 -- the regenerated preview has an API read path, on the frozen URL.

``ReadCurrentAction`` existed, was correct, and was reachable by nothing: it was absent from
``ApiContainer``, from dependency construction, and from every registered router. A preview that
no authorized caller can retrieve is a preview nobody approves, and Phase 8's approval body
binds ``preview_hash`` to bytes a human is supposed to have seen.

The route is ``GET /v1/cases/{case_id}``, which
[08-api-design.md](../../../docs/architecture/08-api-design.md) § Case surfaces already freezes,
returning ``CaseSurfaceResponse`` with a ``current_action`` section. No alternative URL was
invented because an easier one was available.

The load-bearing assertion here is that the preview is **regenerated**. ADR-022 § 3 persists
``preview_hash`` and neither body, so a response carrying bodies proves the renderer ran on
read; a stored body could only ever agree with a regenerated one or be a second version of the
truth.
"""

from __future__ import annotations

from typing import Any

import pytest

from chorus.application.commands.propose_action import ProposeActionResult
from chorus.domain.entities import ActionExecutionState, ActionProposalStatus
from chorus.ports.scopes import ActionScope
from chorus.ports.storage import StorageDriver
from tests.contract.api.conftest import ApiHarness, build_harness
from tests.fixtures.action import ActionHarness

pytestmark = pytest.mark.anyio


@pytest.fixture
def proposed(storage: StorageDriver) -> ActionHarness:
    return ActionHarness(driver=storage)


async def _surface(
    proposed: ActionHarness, storage: StorageDriver, *, actor: str = "presenter_admin"
) -> tuple[Any, ApiHarness, ProposeActionResult]:
    """Propose for real, then read the case surface through the real application."""

    await proposed.prepare()
    result = await proposed.propose_action().execute(await proposed.command())

    api = build_harness(storage, "recording")
    with api.client:
        api.bind_compile(proposed.compile)
        response = api.client.get(
            f"/v1/cases/{proposed.case_id}",
            headers={"X-Chorus-Demo-Actor": actor},
        )
    return response, api, result


# ---------------------------------------------------------------------------------------
# The route exists, is wired, and answers
# ---------------------------------------------------------------------------------------


async def test_the_case_surface_returns_the_current_action(
    proposed: ActionHarness, storage: StorageDriver
) -> None:
    response, _api, result = await _surface(proposed, storage)

    assert response.status_code == 200
    action = response.json()["current_action"]
    assert action is not None
    assert action["action_id"] == str(result.action_id)
    assert action["proposal_hash"] == result.proposal_hash.value
    assert action["status"] == ActionProposalStatus.DRAFT.value


async def test_the_read_current_action_use_case_is_in_the_container(
    proposed: ActionHarness, storage: StorageDriver
) -> None:
    """The defect was a wiring absence, so the wiring is asserted directly."""

    from chorus.application.queries.current_action import ReadCurrentAction

    api = build_harness(storage, "recording")
    with api.client:
        assert isinstance(api.app.state.container.read_current_action, ReadCurrentAction)


async def test_the_route_is_registered_on_the_frozen_url(
    proposed: ActionHarness, storage: StorageDriver
) -> None:
    api = build_harness(storage, "recording")
    with api.client:
        paths = set(api.app.openapi()["paths"])

    assert "/v1/cases/{case_id}" in paths


async def test_a_case_with_no_proposal_returns_a_null_section(
    proposed: ActionHarness, storage: StorageDriver
) -> None:
    """A state, not an error: a ``READY_FOR_ACTION`` case legitimately has no action yet."""

    await proposed.prepare()
    api = build_harness(storage, "recording")
    with api.client:
        api.bind_compile(proposed.compile)
        response = api.client.get(
            f"/v1/cases/{proposed.case_id}",
            headers={"X-Chorus-Demo-Actor": "presenter_admin"},
        )

    assert response.status_code == 200
    assert response.json()["current_action"] is None


# ---------------------------------------------------------------------------------------
# The preview is regenerated, not loaded
# ---------------------------------------------------------------------------------------


async def test_the_preview_is_regenerated_rather_than_loaded_from_storage(
    proposed: ActionHarness, storage: StorageDriver
) -> None:
    """No body is persisted anywhere, so bodies in the response can only be freshly rendered."""

    response, _api, result = await _surface(proposed, storage)
    preview = response.json()["current_action"]["preview"]

    assert preview["text_body"]
    assert preview["html_body"]
    assert preview["preview_hash"] == result.preview_hash.value
    assert preview["matches_committed_hash"] is True

    # The proof that nothing was loaded: the stored proposal item carries the digest and
    # neither body, so there is no persisted text for the route to have returned.
    stored = await proposed.compile.shareable.load_proposal(
        ActionScope(
            namespace=proposed.scope.namespace,
            community_id=proposed.scope.community_id,
            case_id=proposed.scope.case_id,
            action_id=result.action_id,
        )
    )
    assert not hasattr(stored, "text_body")
    assert not hasattr(stored, "html_body")
    assert stored.preview_hash == result.preview_hash


async def test_the_regenerated_bodies_match_the_renderer_run_directly(
    proposed: ActionHarness, storage: StorageDriver
) -> None:
    """Same pure function, same immutable inputs, same bytes."""

    response, _api, _result = await _surface(proposed, storage)
    projection = await proposed.read_current_action().execute(proposed.scope)
    assert projection is not None
    preview = response.json()["current_action"]["preview"]

    assert preview["text_body"] == projection.text_body
    assert preview["html_body"] == projection.html_body
    assert preview["template_version"] == projection.template_version


async def test_the_draft_execution_is_projected_without_approving_anything(
    proposed: ActionHarness, storage: StorageDriver
) -> None:
    """Reading a ``DRAFT`` decides nothing about it.

    This test originally also asserted that no approval or execution route existed at all,
    which was true of Phase 7 and is the thing Phase 8 was chartered to change. What it was
    *really* protecting survives the change and is asserted directly instead: **a read has no
    side effect.** The execution is still ``DRAFT`` at its original version after the surface
    has been served, so serving a preview never advances anything -- which is the same rule
    ADR-025 SS 10 states for reconciliation, applied to the surface a human approves from.
    """

    response, _api, result = await _surface(proposed, storage)
    execution = response.json()["current_action"]["execution"]

    assert execution["execution_id"] == str(result.execution_id)
    assert execution["state"] == ActionExecutionState.DRAFT.value

    stored = await proposed.compile.shareable.load_execution(
        ActionScope(
            namespace=proposed.scope.namespace,
            community_id=proposed.scope.community_id,
            case_id=proposed.scope.case_id,
            action_id=result.action_id,
        ),
        result.execution_id,
    )
    assert stored.state is ActionExecutionState.DRAFT
    assert stored.version == 1
    assert stored.approval_id is None


async def test_the_phase_eight_verbs_are_commands_and_never_a_second_read_route(
    proposed: ActionHarness, storage: StorageDriver
) -> None:
    """Approving, clearing, and sending are ``POST``; the execution keeps one address.

    ADR-025 SS 15 freezes a minimal surface: three commands, and **no new read route**, because
    an execution is already part of ``current_action`` here and a second address for one row is
    a second thing to keep consistent.
    """

    _response, api, _result = await _surface(proposed, storage)
    with api.client:
        paths: dict[str, Any] = api.app.openapi()["paths"]

    for suffix in ("approvals", "invalidation", "executions"):
        matching = [path for path in paths if path.endswith(f"/{suffix}")]
        assert len(matching) == 1, suffix
        assert set(paths[matching[0]]) == {"post"}, suffix

    # No route reads an execution by identifier, and none offers a retry.
    assert not any("/executions/" in path for path in paths)
    assert not any("retry" in path for path in paths)


# ---------------------------------------------------------------------------------------
# What the surface may never carry
# ---------------------------------------------------------------------------------------


async def test_the_surface_exposes_no_private_or_internal_value(
    proposed: ActionHarness, storage: StorageDriver
) -> None:
    """The whole forbidden list, asserted against the serialized response."""

    response, _api, _result = await _surface(proposed, storage)
    body = response.text
    payload = response.json()

    case = await proposed.compile.core.load_case(proposed.scope)
    assert case.title not in body
    assert case.issue_type not in body
    for report_id in case.report_ids:
        assert str(report_id) not in body

    context = proposed.compile.fixture.context
    for identifier in (
        *(str(fact.fact_id) for fact in context.facts),
        *(str(report.report_id) for report in context.reports),
        *(str(report.contributor_id) for report in context.reports),
    ):
        assert identifier not in body, identifier

    from tests.fixtures.compile import SENTINEL_PATTERN

    assert SENTINEL_PATTERN.search(body) is None
    assert "@" not in body
    assert "chorus-demo-sender" not in body
    for absent in (
        "prompt",
        "mandate",
        "assessment",
        "excluded",
        "routing_token",
        "invocation",
        "binding_hash",
        "s3://",
    ):
        assert absent not in body.lower(), absent

    action = payload["current_action"]
    assert set(action["preview"]) == {
        "template_version",
        "text_body",
        "html_body",
        "preview_hash",
        "matches_committed_hash",
    }
    assert set(action["execution"]) == {"execution_id", "state"}


# ---------------------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------------------


async def test_the_approver_may_read_the_surface(
    proposed: ActionHarness, storage: StorageDriver
) -> None:
    """The frozen access table names presenter and approver for this surface."""

    response, _api, _result = await _surface(proposed, storage, actor="case_approver")

    assert response.status_code == 200
    assert response.json()["current_action"] is not None


async def test_a_resident_may_not_read_the_surface(
    proposed: ActionHarness, storage: StorageDriver
) -> None:
    response, _api, _result = await _surface(proposed, storage, actor="resident_a")

    assert response.status_code == 403


async def test_an_actor_header_is_required(proposed: ActionHarness, storage: StorageDriver) -> None:
    await proposed.prepare()
    api = build_harness(storage, "recording")
    with api.client:
        api.bind_compile(proposed.compile)
        response = api.client.get(f"/v1/cases/{proposed.case_id}")

    assert response.status_code == 401
