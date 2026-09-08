"""P10: the current-action read says whether a durable approval is still authorization-current.

Finding 3 of the final three-repair pass. A browser that had approved an action kept showing
"Execute" after a mandate revocation bumped the case's ``authorization_version`` -- the durable
``approval_id`` was still readable, and nothing in the read projection said the epoch it was
made against had moved on.

The fix is one server-derived field. ``ReadCurrentAction`` surfaces the epoch recorded on the
``Approval`` row (``approval_authorization_version``), and the ``GET /v1/cases/{id}`` route
turns it into ``approval_authorization_current`` by comparing it against the case's *current*
``authorization_version``. No new approval authority: the Phase-8 send fence still re-derives
send-time authorization from live Core state regardless of this hint.
"""

from __future__ import annotations

import pytest
from chorus_api.routes.cases import _project

from chorus.domain.entities import ActionExecutionState
from chorus.ports.storage import StorageDriver
from tests.fixtures.send import SendHarness

pytestmark = pytest.mark.anyio


@pytest.fixture
def send(storage: StorageDriver) -> SendHarness:
    from tests.fixtures.action import ActionHarness

    return SendHarness(action=ActionHarness(driver=storage))


async def test_no_durable_approval_leaves_the_epoch_field_unset(send: SendHarness) -> None:
    await send.prepare()

    projection = await send.action.read_current_action().execute(send.scope)

    assert projection is not None
    assert projection.execution_state is ActionExecutionState.DRAFT
    assert projection.approval_authorization_version is None
    # With no approval, the derived boolean is vacuously true at every current epoch.
    assert _project(projection, current_authorization_version=1).approval_authorization_current
    assert _project(projection, current_authorization_version=9).approval_authorization_current


async def test_a_fresh_approval_reads_back_its_own_authorization_epoch(send: SendHarness) -> None:
    await send.prepare()
    approval = await send.approve()

    projection = await send.action.read_current_action().execute(send.scope)

    assert projection is not None
    assert projection.execution_state is ActionExecutionState.APPROVED
    assert projection.approval_authorization_version is not None
    # It is the epoch the approval row itself recorded, not a recomputation.
    stored = await send.action.compile.shareable.load_approval(
        await send.action_scope(), approval.approval_id
    )
    assert projection.approval_authorization_version == stored.authorization_version


async def test_current_is_true_when_the_epoch_matches_and_false_when_it_has_moved(
    send: SendHarness,
) -> None:
    await send.prepare()
    await send.approve()
    projection = await send.action.read_current_action().execute(send.scope)
    assert projection is not None
    epoch = projection.approval_authorization_version
    assert epoch is not None

    # Same epoch: the approval a browser holds is still current -- Execute may be offered.
    same = _project(projection, current_authorization_version=epoch)
    assert same.approval_authorization_current is True
    assert same.execution.approval_id is not None

    # A later mandate revocation bumps the case epoch; the durable approval no longer
    # authorizes a send, and the read says so while still exposing the historical id.
    moved = _project(projection, current_authorization_version=epoch + 1)
    assert moved.approval_authorization_current is False
    assert moved.execution.approval_id is not None


async def test_a_phase_seven_fallback_surface_asserts_nothing_new(send: SendHarness) -> None:
    """No case header (``current_authorization_version is None``) -> the field stays ``True``."""

    await send.prepare()
    await send.approve()
    projection = await send.action.read_current_action().execute(send.scope)
    assert projection is not None

    assert (
        _project(projection, current_authorization_version=None).approval_authorization_current
        is True
    )
